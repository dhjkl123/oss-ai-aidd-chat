from contextlib import asynccontextmanager
from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from threading import Lock
from typing import Annotated
import asyncio
import json
import logging
import time
import unicodedata
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, TypeAdapter
from starlette.background import BackgroundTask

from .adapters import (
    ActiveRunConflict,
    CapacityExceeded,
    ConversationExpired,
    ConversationNotFound,
    IdempotencyConflict,
    RetryExhausted,
    RetryNotRetryable,
    RunNotFound,
    TurnLimitReached,
)
from .application import (
    RUN_DEADLINE_SECONDS,
    ChatApplication,
    ContextTooLarge,
    Conversation,
    PolicyProjectionV1,
    ProviderProfileChanged,
)
from .bootstrap import build_chat_application
from .contracts import (
    CancelResultV1,
    ContextIntegrityError,
    ErrorBodyV1,
    ErrorEnvelopeV1,
    RetryCommand,
    RunCommandV1,
    RunEventV1,
    RunProjectionV1,
)


_RUN_COMMAND = TypeAdapter(RunCommandV1)


WEB_ROOT = Path(__file__).with_name("web")
SSE_KEEPALIVE_SECONDS = 15
# At most this many X-Forwarded-For entries are ever parsed. The header is written
# by the client; without a cap it is an unbounded allocation on every request.
MAX_FORWARDED_HOPS = 16
# Load shedding, in Korean, with a code the client can allow-list. Every one of
# these is decided before any Domain state or Provider work exists.
# code -> (status, Korean sentence, Retry-After seconds). The header is not
# decoration: every one of these has a known window, and a client that has to guess
# retries too early or gives up too late. None means "not by waiting".
_CAPACITY_REJECTIONS = {
    "conversation_capacity_exceeded": (
        503, "지금은 새 대화를 시작할 수 없어요. 잠시 후 다시 시도해 주세요.", 30
    ),
    "run_capacity_exceeded": (503, "지금은 답변을 시작할 수 없어요. 잠시 후 다시 시도해 주세요.", 5),
    "session_run_capacity_exceeded": (
        429, "이 대화에서 동시에 처리할 수 있는 답변 수를 넘었어요. 잠시 후 다시 시도해 주세요.", 5
    ),
    "queue_capacity_exceeded": (503, "대기 중인 요청이 많아요. 잠시 후 다시 시도해 주세요.", 5),
    "provider_busy": (503, "지금은 처리 중인 요청이 많아요. 잠시 후 다시 시도해 주세요.", 5),
    "spend_limit_reached": (503, "지금은 새 답변을 만들 수 없어요. 잠시 후 다시 시도해 주세요.", 60),
    "rate_limited": (429, "요청이 너무 잦아요. 잠시 후 다시 시도해 주세요.", 60),
    "stream_capacity_exceeded": (
        503, "실시간 연결이 가득 찼어요. 잠시 후 다시 시도해 주세요.", 5
    ),
    # Only a redeploy clears this one, so it advertises no wait at all.
    "deployment_unconfigured": (
        503, "서비스 설정이 완료되지 않았습니다. 운영자에게 문의해 주세요.", None
    ),
}


# Every Korean Error Envelope this process can emit, as code -> (status, sentence).
# One table, because an Envelope that is not in the OpenAPI document is not a
# contract: `_envelopes()` below builds every Route's `responses=` from it, and
# tests/test_story_1_11.py walks every `error_response(...)` call site in this module
# and refuses a code, status or sentence that is not exactly the one declared here.
# The load-shed half is folded in rather than re-typed -- `_CAPACITY_REJECTIONS`
# already owns those, plus the Retry-After window they alone have.
# `retryable` is part of the contract, not decoration: `web/app.js` reads permanence
# off this flag and nowhere else, so a Code that ships True at one call site and False
# at another gives the same refusal two different meanings. It lives here with the
# status and the sentence, and the call-site test asserts all three.
#
# `None` means the CALL SITE decides, and there are exactly two reasons for it. Every
# load-shed Code carries the retryability of the `CapacityExceeded` that raised it.
# And `provider_unavailable` is transient when Readiness is merely not satisfied yet,
# permanent once the integrity poison has latched -- one Code, two facts, and adding a
# second public Code to split them is an Ask First.
_ERROR_ENVELOPES: dict[str, tuple[int, str, bool | None]] = {
    **{code: (status, message, None) for code, (status, message, _after) in _CAPACITY_REJECTIONS.items()},
    "invalid_request": (422, "요청 형식을 확인해 주세요.", False),
    "invalid_origin": (403, "요청 출처를 확인할 수 없습니다.", False),
    "invalid_idempotency_key": (400, "Idempotency-Key를 입력해 주세요.", False),
    "invalid_event_cursor": (400, "Event cursor를 확인해 주세요.", False),
    "conversation_expired": (410, "대화 세션이 만료되었습니다.", False),
    "idempotency_conflict": (409, "같은 Idempotency-Key에 다른 요청을 사용할 수 없습니다.", False),
    # True everywhere, now. Another Run being active is the definition of transient:
    # it ends, and the same command works. Two of the four call sites shipped the
    # `retryable=False` default, which told the client this Conversation was finished.
    "run_already_active": (409, "답변을 생성하고 있어요. 완료 후 다시 질문해 주세요.", True),
    "turn_limit_reached": (409, "이 대화에서 더 질문할 수 없어요. 새 대화를 시작해 주세요.", False),
    "retry_exhausted": (409, "재시도 가능 횟수를 모두 사용했어요. 새 대화를 시작해 주세요.", False),
    "retry_not_allowed": (409, "이 답변은 다시 시도할 수 없어요. 새 질문을 보내 주세요.", False),
    # Story 1.11. Deliberately NOT `retry_not_allowed`: that one means this Run was
    # never a retry target. This one means the Provider changed under a Run that WAS
    # retryable. `retryable=False` and the sentence agree -- re-sending THIS Run can
    # never succeed, because its target will keep carrying the old Digest -- so the
    # sentence points at the composer, which stays open, and not at the 재시도 button,
    # which the flag disables.
    "provider_profile_changed": (
        409, "Provider 설정이 바뀌어 이 답변은 다시 시도할 수 없어요. 같은 질문을 새로 보내 주세요.", False
    ),
    "context_too_large": (413, "질문이 너무 길어 처리할 수 없습니다. 내용을 줄여 다시 시도해 주세요.", False),
    "provider_unavailable": (503, "Provider가 준비되지 않았습니다.", None),
}

# What `ip_gate` alone can answer, in front of every /api/v1 Operation.
_GATE_CODES = ("rate_limited", "deployment_unconfigured")
# The Minimal 404, which carries no Envelope at all -- an unknown Resource and one
# belonging to another session must be indistinguishable, so there is nothing to
# describe but the fact that the body is empty.
_MINIMAL_404 = {404: {"description": "존재하지 않거나 이 Capability의 것이 아닌 Resource. 본문 없음."}}


_ENVELOPE_REF = {"$ref": "#/components/schemas/ErrorEnvelopeV1"}


def _envelopes(*codes: str, ref_only: bool = False) -> dict[int | str, dict]:
    """OpenAPI `responses` for the Error Codes one Operation can answer with, grouped
    by status. The Korean sentence goes into the description beside its code, so the
    document -- not this module's source -- is where a client reads what it will be
    told.

    `ref_only` exists for the SSE Route alone. Declaring `model=` makes FastAPI file
    the schema under the ROUTE's media type, which for that Route is
    `text/event-stream` -- and its error bodies are ordinary JSON. Referencing the
    already-registered component directly keeps them honest. The component is
    registered by the other Routes, which do pass `model=`;
    `test_openapi_every_ref_resolves` fails if that ever stops being true."""
    body = (
        {"content": {"application/json": {"schema": _ENVELOPE_REF}}}
        if ref_only
        else {"model": ErrorEnvelopeV1}
    )
    grouped: dict[int, list[str]] = {}
    for code in codes:
        status, _message, _retryable = _ERROR_ENVELOPES[code]
        grouped.setdefault(status, []).append(code)
    return {
        status: {
            **body,
            "description": " / ".join(
                f"`{code}` -- {_ERROR_ENVELOPES[code][1]}"
                + ("" if _ERROR_ENVELOPES[code][2] is None else f" (retryable: {str(_ERROR_ENVELOPES[code][2]).lower()})")
                for code in sorted(group)
            ),
        }
        for status, group in grouped.items()
    }


class EventStreamResponse(StreamingResponse):
    """Declared only so FastAPI files the SSE Event schema under `text/event-stream`
    instead of `application/json`. The Route builds its own StreamingResponse."""

    media_type = "text/event-stream"


# Every logger that writes a request line, a URL, a header or a raw Provider
# payload by default. Silenced outright rather than filtered: the only Telemetry
# this process is allowed to emit is the closed RunTelemetryV1 Record, and a
# free-form request/response log cannot be made to fit that Allowlist by tuning a
# level. Package roots only -- each gets propagate=False, so every child is silenced
# with it and naming children would be noise. `uvicorn.error` is deliberately NOT
# here: it carries startup failures and no request content, and it is what reports a
# refused public_demo Guard.
#
# That last one rests on an invariant rather than on a filter: uvicorn.error is also
# where an escaping ASGI traceback lands, and a pydantic ValidationError message
# embeds `input_value=...` -- so a Model answer that reached Event construction and
# was refused THERE would be logged verbatim. It cannot: every commit pre-checks with
# `is_valid_model_text` and answers False, so no hostile text ever reaches an Event
# constructor. That is the whole reason the predicate exists beside the validator,
# and tests/test_story_1_9.py pins it on both the delta and the completed path.
_SILENCED_LOGGERS = (
    "uvicorn.access",
    "httpx",
    "httpcore",
    "urllib3",
    "anyio",
    "asyncio",
    "openai",
    "pydantic_ai",
)


def silence_untrusted_loggers() -> None:
    """Irreversible for the life of the process, deliberately: there is no operator
    switch to turn request logging back on, because the switch would be the leak.
    An operator who needs these logs runs the app outside this entry point."""
    for name in _SILENCED_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = False
        logger.disabled = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    # First, before anything can be built or called: a Provider SDK constructed
    # under a debug root logger would otherwise log its first request line.
    silence_untrusted_loggers()
    if not hasattr(app.state, "chat_application"):
        app.state.chat_application = build_chat_application()
    try:
        yield
    finally:
        app.state.chat_application.shutdown()


API_DESCRIPTION = """익명 한국어 Q&A 대화 Shell의 `/api/v1` 계약.

모든 오류는 하나의 Envelope(`ErrorEnvelopeV1`)로 돌아오며 `code`·한국어 `message`·
`retryable`·`correlation_id`를 담는다. 각 Operation의 응답 설명이 그 Operation이
답할 수 있는 Code와 문장을 그대로 나열한다. 권한이 없거나 존재하지 않는 Resource는
Envelope 없이 동일한 Minimal 404다.

SSE(`GET /api/v1/runs/{run_id}/events`)의 Envelope·순서·`Last-Event-ID` Replay·
Terminal Close·만료 Tail은 OpenAPI가 표현할 수 없으므로 `docs/sse-contract.md`가
소유한다. 여기서는 Event Schema 집합(`RunEventV1`)만 선언한다.

배포·용량·Rate 설정은 `.env.example`, 만료와 재시도 규칙은 `README.md`에 있다."""

app = FastAPI(
    title="aidd-chat",
    version="1",
    summary="익명 한국어 Q&A 대화 Shell",
    description=API_DESCRIPTION,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def error_response(status_code: int, code: str, message: str, retryable: bool = False) -> JSONResponse:
    envelope = ErrorEnvelopeV1(
        error=ErrorBodyV1(
            code=code,
            message=message,
            retryable=retryable,
            correlation_id=uuid4(),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


def minimal_not_found() -> Response:
    return Response(status_code=404, headers={"Cache-Control": "no-store"})


def capacity_response(exc: CapacityExceeded, chat: ChatApplication) -> JSONResponse:
    """One envelope shape for every ceiling. The code names which ceiling, never
    what it is set to -- a client that can read the limit can plan around it. The
    window is a different matter: `Retry-After` is the one number that helps a
    client behave, and it says nothing about how big the ceiling is.

    Also the single point where every ceiling refusal in the process is observed:
    the Code goes into the Telemetry Record's `error_class`, and nothing else does.
    `chat` is required, with no default, for exactly the reason the public_demo
    Guards moved inside `build_provider`: an optional observation is one the next
    caller forgets, and forgetting it silently restores Story 1.8's unobserved
    ceilings."""
    chat.record_capacity_refusal(exc.reason)
    status_code, message, retry_after = _CAPACITY_REJECTIONS.get(
        exc.reason, (503, "지금은 요청을 처리할 수 없어요. 잠시 후 다시 시도해 주세요.", 5)
    )
    response = error_response(status_code, exc.reason, message, exc.retryable)
    if retry_after is not None and exc.retryable:
        response.headers["Retry-After"] = str(retry_after)
    return response


def client_ip(request: Request, chat: ChatApplication) -> str:
    """The Socket Peer, and only the Socket Peer, unless this deployment explicitly
    declared how many proxies sit in front of it. `X-Forwarded-For` is text the
    client wrote; trusting it without that setting hands every attacker an
    unlimited supply of identities.

    Even with the setting, only a value that parses as an IP address is believed:
    with two or more declared hops the client controls that slot outright, and an
    unparsed one would go straight into the rate limiter as an arbitrary-length key
    of the client's choosing. Parsing also normalizes, so one address cannot be
    spelled several ways to buy several budgets."""
    peer = request.client.host if request.client else ""
    hops = chat.trusted_proxy_hops
    if hops <= 0:
        return peer
    # rsplit, and the TAIL of the result: the entries this deployment trusts are the
    # ones its own proxies appended, which are the rightmost. Keeping the leftmost
    # slice instead lets a client prepend junk until the real hops fall off the end,
    # after which the selected value is entirely client-written -- an unlimited
    # supply of rate-limit identities, which is the whole gate defeated.
    forwarded = [
        value.strip()
        for value in request.headers.get("x-forwarded-for", "").rsplit(",", MAX_FORWARDED_HOPS)
        if value.strip()
    ][-MAX_FORWARDED_HOPS:]
    if len(forwarded) < hops:
        return peer
    try:
        return str(ip_address(forwarded[-hops]))
    except ValueError:
        return peer


def ip_gate(request: Request, chat: ChatApplication) -> JSONResponse | None:
    """The Trusted-client-IP half of the Abuse Gate, in front of every /api/v1
    endpoint and ahead of everything else those endpoints do.

    A deployment whose ceilings were never validated is refused here by name. Its
    per-IP budget is zero, so it would be refused anyway -- but as `rate_limited`,
    which tells an operator to wait for something only a redeploy can fix."""
    if not chat.is_configured:
        return capacity_response(
            CapacityExceeded("deployment_unconfigured", retryable=False), chat
        )
    if chat.allow_client_ip(client_ip(request, chat)):
        return None
    return capacity_response(CapacityExceeded("rate_limited"), chat)


@app.exception_handler(RequestValidationError)
def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    return error_response(422, "invalid_request", "요청 형식을 확인해 주세요.")


class ConversationResponse(BaseModel):
    conversation_id: UUID
    created_at: datetime
    expires_at: datetime


def get_chat_application(request: Request) -> ChatApplication:
    return request.app.state.chat_application


Chat = Annotated[ChatApplication, Depends(get_chat_application)]


@app.get("/", include_in_schema=False)
def shell(_chat: Chat) -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


# The Abuse Gate stands in front of the /api/v1 surface. Four things are outside it
# on purpose: /live and /ready, because a rate-limited health check reports the
# platform as down at exactly the moment it is under load, which is worse than an
# ungated one; and / plus /static/*, which are the immutable shell and its assets --
# no Conversation state, no Provider work, and gating them would make a reloading
# browser unable to fetch the very page that reports the refusal.
@app.get(
    "/live",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Liveness",
    responses={204: {"description": "Process가 살아 있다. 본문 없음."}},
)
def live(_chat: Chat) -> Response:
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/ready",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Readiness",
    responses={
        204: {"description": "Readiness Gate와 Provider Probe를 모두 통과했다. 본문 없음."},
        503: {
            "description": (
                "Gate 또는 2초 Probe가 거부했다. 본문 없음 -- 어떤 Gate가 거부했는지는 "
                "운영 Log에만 남는다."
            )
        },
    },
)
def ready(chat: Chat) -> Response:
    if not chat.is_ready():
        return Response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Cache-Control": "no-store"},
        )
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"Cache-Control": "no-store"},
    )


@app.get(
    "/api/v1/policy",
    response_model=PolicyProjectionV1,
    summary="Provider 정책 고지",
    responses={
        **_envelopes("invalid_request", *_GATE_CODES),
        503: {
            "description": (
                "Provider가 준비되지 않았으면 본문 없는 503, 배포 설정이 검증되지 않았으면 "
                f"`deployment_unconfigured` -- {_ERROR_ENVELOPES['deployment_unconfigured'][1]}"
            ),
            "model": ErrorEnvelopeV1,
        },
    },
)
def policy(request: Request, response: Response, chat: Chat) -> PolicyProjectionV1 | Response:
    response.headers["Cache-Control"] = "no-store"
    rejected = ip_gate(request, chat)
    if rejected is not None:
        return rejected
    projection, ready = chat.policy_readiness()
    if not ready or projection is None:
        return Response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Cache-Control": "no-store"},
        )
    return projection


@app.post(
    "/api/v1/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="새 대화 시작",
    responses=_envelopes(
        "invalid_request", "invalid_origin", "conversation_capacity_exceeded", *_GATE_CODES
    ),
)
def create_conversation(request: Request, response: Response, chat: Chat) -> Conversation | Response:
    # Origin first: cross-origin junk is refused without spending the caller's IP
    # budget on it, which is what would let a third-party page exhaust a real
    # user's budget from their browser.
    if not _has_exact_origin(request):
        return error_response(403, "invalid_origin", "요청 출처를 확인할 수 없습니다.")
    rejected = ip_gate(request, chat)
    if rejected is not None:
        return rejected
    # A resident Conversation slot is held for the full hour, so creation carries a
    # ceiling of its own beyond the per-minute request budget.
    if not chat.allow_conversation_creation(client_ip(request, chat)):
        return capacity_response(CapacityExceeded("conversation_capacity_exceeded"), chat)
    try:
        conversation, capability = chat.create_conversation()
    except CapacityExceeded as exc:
        # No Conversation was created, so no live Run and no completed Conversation
        # is disturbed by this refusal.
        return capacity_response(exc, chat)
    response.set_cookie(
        "conversation_capability",
        capability,
        max_age=3_600,
        expires=conversation.expires_at,
        path="/api/v1",
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return conversation


def _has_exact_origin(request: Request) -> bool:
    expected = f"{request.url.scheme}://{request.url.netloc}"
    return request.headers.get("origin") == expected


def _valid_idempotency_key(value: str | None) -> bool:
    return bool(
        value
        and value == value.strip()
        and not any(unicodedata.category(char).startswith("C") for char in value)
    )


@app.post(
    "/api/v1/conversations/{conversation_id}/runs",
    response_model=RunProjectionV1,
    status_code=status.HTTP_202_ACCEPTED,
    summary="질문 접수 또는 재시도",
    responses={
        **_MINIMAL_404,
        **_envelopes(
            "invalid_request",
            "invalid_origin",
            "invalid_idempotency_key",
            "conversation_expired",
            "idempotency_conflict",
            "run_already_active",
            "turn_limit_reached",
            "retry_exhausted",
            "retry_not_allowed",
            "provider_profile_changed",
            "context_too_large",
            "provider_unavailable",
            "run_capacity_exceeded",
            "session_run_capacity_exceeded",
            "queue_capacity_exceeded",
            "provider_busy",
            "spend_limit_reached",
            *_GATE_CODES,
        ),
    },
)
async def submit_question(
    conversation_id: str,
    request: Request,
    response: Response,
    chat: Chat,
) -> RunProjectionV1 | Response:
    response.headers["Cache-Control"] = "no-store"
    # Origin first, as in create_conversation: a cross-origin POST is refused
    # without spending the caller's IP budget.
    if not _has_exact_origin(request):
        return error_response(403, "invalid_origin", "요청 출처를 확인할 수 없습니다.")
    rejected = ip_gate(request, chat)
    if rejected is not None:
        return rejected
    capability = request.cookies.get("conversation_capability")
    if not capability:
        return minimal_not_found()
    try:
        parsed_conversation_id = UUID(conversation_id)
    except ValueError:
        return minimal_not_found()
    try:
        chat.authorize_conversation(parsed_conversation_id, capability)
    except ConversationNotFound:
        return minimal_not_found()
    except ConversationExpired:
        return error_response(410, "conversation_expired", "대화 세션이 만료되었습니다.")
    idempotency_key = request.headers.get("idempotency-key")
    if not _valid_idempotency_key(idempotency_key):
        return error_response(400, "invalid_idempotency_key", "Idempotency-Key를 입력해 주세요.")
    try:
        command = _RUN_COMMAND.validate_python(await request.json())
    except (TypeError, ValueError):
        return error_response(422, "invalid_request", "요청 형식을 확인해 주세요.")
    if isinstance(command, RetryCommand):
        return _retry(parsed_conversation_id, capability, idempotency_key, command, chat)
    try:
        replay = chat.replay_question(
            parsed_conversation_id,
            capability,
            idempotency_key,
            command.content,
        )
    except ConversationNotFound:
        return minimal_not_found()
    except ConversationExpired:
        return error_response(410, "conversation_expired", "대화 세션이 만료되었습니다.")
    except IdempotencyConflict:
        return error_response(409, "idempotency_conflict", "같은 Idempotency-Key에 다른 요청을 사용할 수 없습니다.")
    except ActiveRunConflict:
        return error_response(
            409, "run_already_active", "답변을 생성하고 있어요. 완료 후 다시 질문해 주세요.", True
        )
    if replay is not None:
        return replay
    if not chat.is_ready():
        return error_response(503, "provider_unavailable", "Provider가 준비되지 않았습니다.", True)
    try:
        return chat.submit_question(
            parsed_conversation_id,
            capability,
            idempotency_key,
            command.content,
        )
    except ConversationNotFound:
        return minimal_not_found()
    except ConversationExpired:
        return error_response(410, "conversation_expired", "대화 세션이 만료되었습니다.")
    except IdempotencyConflict:
        return error_response(409, "idempotency_conflict", "같은 Idempotency-Key에 다른 요청을 사용할 수 없습니다.")
    except ActiveRunConflict:
        return error_response(
            409, "run_already_active", "답변을 생성하고 있어요. 완료 후 다시 질문해 주세요.", True
        )
    except TurnLimitReached:
        return error_response(
            409, "turn_limit_reached", "이 대화에서 더 질문할 수 없어요. 새 대화를 시작해 주세요."
        )
    except CapacityExceeded as exc:
        return capacity_response(exc, chat)
    except ContextTooLarge:
        return error_response(413, "context_too_large", "질문이 너무 길어 처리할 수 없습니다. 내용을 줄여 다시 시도해 주세요.")
    except ContextIntegrityError:
        # Sticky poison, no automatic recovery without a restart -- retrying
        # immediately cannot succeed, unlike the transient is_ready() 503 above.
        return error_response(503, "provider_unavailable", "Provider가 준비되지 않았습니다.")


def _retry(
    conversation_id: UUID,
    capability: str,
    idempotency_key: str,
    command: RetryCommand,
    chat: ChatApplication,
) -> RunProjectionV1 | Response:
    """Same route, same Origin/Capability/Idempotency-Key ladder as a question --
    which is what makes a Retry and a question racing into one Conversation resolve
    against each other rather than in two different places. Every rejection here
    returns before a Run, a Message or a Provider call exists.

    Replay before readiness, exactly as the question path orders it: a duplicate
    POST during a Provider blip must return the Run the first one created, not a
    503 that invites the client to send a third."""
    try:
        replay = chat.replay_retry(conversation_id, capability, idempotency_key, command)
    except ConversationNotFound:
        return minimal_not_found()
    except ConversationExpired:
        return error_response(410, "conversation_expired", "대화 세션이 만료되었습니다.")
    except IdempotencyConflict:
        return error_response(409, "idempotency_conflict", "같은 Idempotency-Key에 다른 요청을 사용할 수 없습니다.")
    except ActiveRunConflict:
        return error_response(
            409, "run_already_active", "답변을 생성하고 있어요. 완료 후 다시 질문해 주세요.", True
        )
    if replay is not None:
        return replay
    if not chat.is_ready():
        return error_response(503, "provider_unavailable", "Provider가 준비되지 않았습니다.", True)
    try:
        return chat.retry_run(conversation_id, capability, idempotency_key, command)
    except (ConversationNotFound, RunNotFound):
        # A Run of another Conversation and a Run that does not exist are the same
        # Minimal 404: neither may confirm that the other kind was guessed.
        return minimal_not_found()
    except ConversationExpired:
        return error_response(410, "conversation_expired", "대화 세션이 만료되었습니다.")
    except IdempotencyConflict:
        return error_response(409, "idempotency_conflict", "같은 Idempotency-Key에 다른 요청을 사용할 수 없습니다.")
    except ActiveRunConflict:
        return error_response(
            409, "run_already_active", "답변을 생성하고 있어요. 완료 후 다시 질문해 주세요.", True
        )
    except RetryExhausted:
        return error_response(
            409, "retry_exhausted", "재시도 가능 횟수를 모두 사용했어요. 새 대화를 시작해 주세요."
        )
    except ProviderProfileChanged:
        # Before RetryNotRetryable in the ladder only for readability -- the two are
        # unrelated types, so order cannot decide between them. The client keeps this
        # Conversation open: only the Provider moved.
        return error_response(
            409,
            "provider_profile_changed",
            "Provider 설정이 바뀌어 이 답변은 다시 시도할 수 없어요. 같은 질문을 새로 보내 주세요.",
        )
    except RetryNotRetryable:
        return error_response(
            409, "retry_not_allowed", "이 답변은 다시 시도할 수 없어요. 새 질문을 보내 주세요."
        )
    except CapacityExceeded as exc:
        return capacity_response(exc, chat)
    except ContextTooLarge:
        return error_response(413, "context_too_large", "질문이 너무 길어 처리할 수 없습니다. 내용을 줄여 다시 시도해 주세요.")
    except ContextIntegrityError:
        return error_response(503, "provider_unavailable", "Provider가 준비되지 않았습니다.")


@app.get(
    "/api/v1/runs/{run_id}",
    response_model=RunProjectionV1,
    summary="Run 상태 조회 (Polling)",
    # `invalid_request` is declared on every Route that carries a path parameter or a
    # body: FastAPI would otherwise publish its own 422 shape, and this process
    # answers EVERY RequestValidationError with the same Korean Envelope.
    responses={
        **_MINIMAL_404,
        **_envelopes("invalid_request", "conversation_expired", *_GATE_CODES),
    },
)
def poll_run(run_id: str, request: Request, response: Response, chat: Chat) -> RunProjectionV1 | Response:
    response.headers["Cache-Control"] = "no-store"
    rejected = ip_gate(request, chat)
    if rejected is not None:
        return rejected
    capability = request.cookies.get("conversation_capability")
    if not capability:
        return minimal_not_found()
    try:
        parsed_run_id = UUID(run_id)
    except ValueError:
        return minimal_not_found()
    try:
        return chat.get_run(parsed_run_id, capability)
    except ConversationNotFound:
        return minimal_not_found()
    except ConversationExpired:
        return error_response(410, "conversation_expired", "대화 세션이 만료되었습니다.")
    except CapacityExceeded as exc:
        # A refused Poll changed nothing: the Run it asked about is untouched.
        return capacity_response(exc, chat)


@app.post(
    "/api/v1/runs/{run_id}/cancel",
    response_model=CancelResultV1,
    status_code=status.HTTP_200_OK,
    summary="생성 중지",
    responses={
        **_MINIMAL_404,
        **_envelopes(
            "invalid_request", "invalid_origin", "conversation_expired", *_GATE_CODES
        ),
    },
)
def cancel_run(run_id: str, request: Request, response: Response, chat: Chat) -> CancelResultV1 | Response:
    """Same Capability/UUID/404 ladder as poll_run. `accepted` and
    `already_terminal` are both 200: losing the terminal race is not a client
    error, and the body carries the Run that actually won."""
    response.headers["Cache-Control"] = "no-store"
    rejected = ip_gate(request, chat)
    if rejected is not None:
        return rejected
    capability = request.cookies.get("conversation_capability")
    if not capability:
        return minimal_not_found()
    try:
        parsed_run_id = UUID(run_id)
    except ValueError:
        return minimal_not_found()
    try:
        # Authorize before the Origin gate, exactly as submit_question does: an
        # unknown or foreign Run must be a Minimal 404 whatever the Origin says.
        # Not charged against the Poll ceiling -- this is the Cancel's own lookup.
        chat.get_run(parsed_run_id, capability, rate_limited=False)
    except ConversationNotFound:
        return minimal_not_found()
    except ConversationExpired:
        return error_response(410, "conversation_expired", "대화 세션이 만료되었습니다.")
    if not _has_exact_origin(request):
        return error_response(403, "invalid_origin", "요청 출처를 확인할 수 없습니다.")
    try:
        return chat.cancel_run(parsed_run_id, capability)
    except ConversationNotFound:
        return minimal_not_found()
    except ConversationExpired:
        return error_response(410, "conversation_expired", "대화 세션이 만료되었습니다.")
    except CapacityExceeded as exc:
        return capacity_response(exc, chat)


def _sse_frame(event: RunEventV1) -> str:
    data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.sequence}\nevent: {event.type}\ndata: {data}\n\n"


def _event_cursor(value: str | None) -> int:
    if value is None or value == "":
        return 0
    if len(value) > 20 or not value.isascii() or not value.isdecimal():
        raise ValueError
    return int(value)


@app.get(
    "/api/v1/runs/{run_id}/events",
    summary="Run Event Stream (SSE)",
    response_class=EventStreamResponse,
    responses={
        200: {
            "model": RunEventV1,
            "description": (
                "`text/event-stream`. 한 Frame은 `id:`(Base-10 Sequence)·`event:`(Event "
                "Type)·`data:`(아래 Schema의 compact JSON) 세 줄이다. Envelope·순서·"
                "`Last-Event-ID` Replay·Terminal Close·만료 Tail은 `docs/sse-contract.md`가 "
                "소유한다."
            ),
        },
        204: {
            "description": (
                "Terminal Run을 마지막 Sequence로 재연결했다. 더 보낼 Event가 없으므로 "
                "Stream을 열지 않는다. 본문 없음."
            )
        },
        **_MINIMAL_404,
        **_envelopes(
            "invalid_request",
            "invalid_event_cursor",
            "conversation_expired",
            "stream_capacity_exceeded",
            *_GATE_CODES,
            ref_only=True,
        ),
    },
)
async def stream_run(run_id: str, request: Request, chat: Chat) -> Response:
    rejected = ip_gate(request, chat)
    if rejected is not None:
        return rejected
    capability = request.cookies.get("conversation_capability")
    if not capability:
        return minimal_not_found()
    try:
        parsed_run_id = UUID(run_id)
    except ValueError:
        return minimal_not_found()
    try:
        projection = chat.get_run(parsed_run_id, capability)
    except ConversationNotFound:
        return minimal_not_found()
    except ConversationExpired:
        return error_response(410, "conversation_expired", "대화 세션이 만료되었습니다.")
    except CapacityExceeded as exc:
        return capacity_response(exc, chat)
    try:
        cursor = _event_cursor(request.headers.get("last-event-id"))
    except ValueError:
        return error_response(400, "invalid_event_cursor", "Event cursor를 확인해 주세요.")

    terminal = projection.state not in {"queued", "running"}
    if terminal and cursor > projection.latest_sequence:
        return error_response(400, "invalid_event_cursor", "Event cursor를 확인해 주세요.")
    if terminal and cursor == projection.latest_sequence:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    # Reserved before the generator exists. Released exactly once, whether the
    # generator ran, was closed after one frame, or was never iterated at all --
    # a slot that is taken and not returned is a slot gone for the process's life.
    # An existing stream is never disturbed to make room: the new one is refused.
    if not chat.reserve_stream():
        return capacity_response(CapacityExceeded("stream_capacity_exceeded"), chat)
    # Guarded, not just flagged: the generator's finally runs on the event loop and
    # the BackgroundTask on a threadpool worker, so an unsynchronized check-then-set
    # can release the same slot twice.
    release_guard = Lock()
    released = False

    def release_stream_once() -> None:
        nonlocal released
        with release_guard:
            if released:
                return
            released = True
        chat.release_stream()

    # What is left of THIS Run's own Deadline, read from the monotonic value the Run
    # stores -- the same clock every other Deadline and the Absolute Expiry use. A
    # wall-clock bound would close every live stream at once on an NTP step, or let
    # a stuck one hold its slot for ever. A client reconnecting with Last-Event-ID
    # gets what is left, never a fresh budget.
    deadline_monotonic = chat.run_deadline_monotonic(parsed_run_id, capability)

    async def event_source():
        next_sequence = cursor + 1
        loop = asyncio.get_running_loop()
        keepalive_at = loop.time() + SSE_KEEPALIVE_SECONDS
        remaining = (
            RUN_DEADLINE_SECONDS
            if deadline_monotonic is None
            else deadline_monotonic - time.monotonic()
        )
        close_at = loop.time() + max(0.0, remaining)
        try:
            while not await request.is_disconnected():
                try:
                    current, events = chat.get_run_slice(
                        parsed_run_id, capability, next_sequence - 1
                    )
                except (ConversationExpired, ConversationNotFound):
                    # The Conversation is gone from under this stream. Whether THIS
                    # read is the one that detected the expiry is irrelevant: the
                    # frozen contract makes the tail a property of the stream being
                    # writable, and the Sweeper or a concurrent poll may well have
                    # Purged first. The Store kept the Aggregate's own two Events on
                    # a tombstone, with its own sequence numbers, so every reader --
                    # and every reconnecting client -- is handed the identical pair.
                    # No tombstone means it vanished for some other reason, and
                    # claiming expiry then would wipe a still-valid transcript.
                    for event in chat.expired_stream_tail(
                        parsed_run_id, capability, next_sequence - 1
                    ) or ():
                        yield _sse_frame(event)
                    return
                for event in events:
                    if event.sequence < next_sequence:
                        continue
                    if event.sequence > next_sequence:
                        break
                    yield _sse_frame(event)
                    next_sequence += 1
                if current.state not in {"queued", "running"}:
                    return
                if loop.time() >= close_at:
                    # Past this Run's own Deadline it cannot still be live, so
                    # holding the connection open only holds an SSE slot.
                    return
                if loop.time() >= keepalive_at:
                    yield ": keepalive\n\n"
                    keepalive_at = loop.time() + SSE_KEEPALIVE_SECONDS
                await asyncio.sleep(0.02)
        finally:
            release_stream_once()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
        # Belt and braces: the generator's finally covers every path that iterates
        # it, this covers a response that is discarded before it ever does.
        background=BackgroundTask(release_stream_once),
    )
