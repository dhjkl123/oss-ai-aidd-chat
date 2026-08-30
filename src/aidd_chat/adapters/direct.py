import asyncio

from asyncio import AbstractEventLoop, Task
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from hashlib import sha256
from threading import Lock
from uuid import uuid4

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI
from pydantic_ai.direct import model_request, model_request_stream
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import (
    FinalResultEvent,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    SystemPromptPart,
    TextPart,
    TextPartDelta,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider

from aidd_chat.application import (
    INPUT_WARNING_CATEGORIES,
    SESSION_TTL_SECONDS,
    TRANSMITTED_FIELDS,
    ProviderPolicyMetadata,
)
from aidd_chat.contracts import (
    SERIALIZER_DIGEST,
    SYSTEM_INSTRUCTION,
    ContextIntegrityError,
    PreparedMessageV1,
    PreparedModelRequestV1,
    ProviderBindingV1,
    ToolPolicyV1,
    binding_digest,
    canonical_request_bytes,
    compute_provider_profile_digest,
    has_unsafe_code_point,
    has_visible_text,
    prepare_model_request,
    validate_messages,
)

from .retrieval import DISABLED_RETRIEVAL, RetrievalPort


LOCAL_TEST_TOKENIZER_NAME = "local-test-codepoint-v1"
LOCAL_TEST_TOKENIZER_VERSION = "1"
# The local_test binding's own advertised window -- a literal, not the application's
# CONTEXT_TOKEN_BUDGET policy constant, so _effective_budget()'s min() clamp is a real
# comparison between two independent numbers, not a tautology. It happens to equal the
# story's fixed budget today; a real Provider's own window (Story 1.11) will differ.
LOCAL_TEST_MAX_INPUT_TOKENS = 8_192

OLLAMA_TOKENIZER_NAME = "ollama-qwen35-conservative-v1"
OLLAMA_TOKENIZER_VERSION = "1"
OLLAMA_MAX_INPUT_TOKENS = 8_192
OLLAMA_MODEL_REVISION = "qwen3.5:9b"
# Content-free: a single ASCII character, no user text, no conversation state.
OLLAMA_PROBE_PROMPT = "."
# Bounded so a cold or chatty model cannot blow the 2s readiness deadline.
OLLAMA_PROBE_MAX_TOKENS = 8
# Client hygiene, not the Run deadline (Story 1.7 owns the 120s wall clock). openai's
# default is 600s, long enough that a proxy which accepts the connection and then
# stalls pins the readiness probe -- and therefore /ready and /policy -- for ten
# minutes. ponytail: one number for connect/read/write; split it if the LAN ever
# needs a slow-connect, fast-read profile.
OLLAMA_CLIENT_TIMEOUT_SECONDS = 30.0

DETERMINISTIC_RETENTION_SUMMARY = (
    "질문과 대화 문맥은 이 프로세스 메모리에서만 세션 만료 전까지 처리됩니다."
)
DETERMINISTIC_DELETION_SUMMARY = "세션 TTL이 지나면 대화에 접근할 수 없고 영구 저장하지 않습니다."
# The LAN binding forwards the question to a model this app does not run, so its
# summaries claim only what this app itself controls -- no third-party retention
# promise it cannot enforce, and no hint of where the model lives.
OLLAMA_RETENTION_SUMMARY = (
    "질문과 대화 문맥은 답변 생성 요청에만 사용되고, 이 앱은 세션 만료 전까지만 메모리에 보관합니다."
)
OLLAMA_DELETION_SUMMARY = (
    "세션 TTL이 지나면 이 앱에서 대화에 접근할 수 없으며, 모델 제공자 측 보관 정책은 이 앱이 보장하지 않습니다."
)

DETERMINISTIC_BINDING = ProviderBindingV1(
    deployment_profile="local_test",
    provider_extra="deterministic",
    provider_label="Deterministic local test provider",
    endpoint_disclosure="local-test",
    model_revision="deterministic-v1",
    processing_region="local process",
    retention_summary=DETERMINISTIC_RETENTION_SUMMARY,
    deletion_summary=DETERMINISTIC_DELETION_SUMMARY,
    credential_reference_names=(),
    tokenizer_authority_name=LOCAL_TEST_TOKENIZER_NAME,
    tokenizer_authority_version=LOCAL_TEST_TOKENIZER_VERSION,
    max_input_tokens=LOCAL_TEST_MAX_INPUT_TOKENS,
    tool_policy=ToolPolicyV1.zero,
)


def ollama_lan_proxy_binding(
    endpoint_origin: str, deployment_profile: str = "local_test"
) -> ProviderBindingV1:
    """The Ollama LAN proxy binding. `endpoint_origin` is validated and canonicalized
    by ProviderBindingV1 itself; it is digest input only and is never projected.
    `public_demo` + a private origin is rejected by the binding."""
    return ProviderBindingV1(
        deployment_profile=deployment_profile,
        provider_extra="openai",
        endpoint_origin=endpoint_origin,
        provider_label="Ollama LAN proxy",
        endpoint_disclosure="ollama-lan-proxy",
        model_revision=OLLAMA_MODEL_REVISION,
        processing_region="on-premise",
        retention_summary=OLLAMA_RETENTION_SUMMARY,
        deletion_summary=OLLAMA_DELETION_SUMMARY,
        credential_reference_names=("OLLAMA_API_KEY",),
        tokenizer_authority_name=OLLAMA_TOKENIZER_NAME,
        tokenizer_authority_version=OLLAMA_TOKENIZER_VERSION,
        max_input_tokens=OLLAMA_MAX_INPUT_TOKENS,
        tool_policy=ToolPolicyV1.zero,
    )


def verify_request_integrity(request: PreparedModelRequestV1, provider: object) -> None:
    """Recompute serializer/context/provider-profile digests from what `provider`
    is about to consume and fail closed on any mismatch. Shared by
    count_input_tokens(), complete() and stream() so all three see identical bytes.
    schema_version and system_instruction are pinned directly: canonical_bytes is
    recomputed FROM the request's own copy of both, so a request whose fields were
    swapped but re-digested consistently would otherwise pass unnoticed."""
    if request.schema_version != "1" or request.system_instruction != SYSTEM_INSTRUCTION:
        raise ContextIntegrityError
    try:
        validate_messages(request.messages)
    except ValueError:
        raise ContextIntegrityError
    expected_bytes = canonical_request_bytes(request.system_instruction, request.messages)
    if (
        request.canonical_bytes != expected_bytes
        or request.context_digest != sha256(expected_bytes).hexdigest()
        or request.serializer_digest != SERIALIZER_DIGEST
        or request.provider_profile_digest != provider.provider_profile_digest
    ):
        raise ContextIntegrityError


class ProviderCallHandle:
    """One in-flight Direct-API call, cancellable from another thread.

    The per-call event loop and its task are published from inside the call, so
    `cancel()` is safe before, during and after that window: a Cancel that lands
    before the loop exists still stops the call (bind() fires it), and one that
    lands after the call finished does nothing. It is one-shot -- at most one
    cancellation ever reaches the Provider no matter how many callers ask."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._loop: AbstractEventLoop | None = None
        self._task: Task | None = None
        self._cancelled = False
        self._fired = False

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def active(self) -> bool:
        """True only while an adapter task is actually running for this call."""
        with self._lock:
            return self._task is not None

    def bind(self, loop: AbstractEventLoop, task: Task) -> None:
        with self._lock:
            self._loop, self._task = loop, task
            pending = self._cancelled
        if pending:
            self._fire()

    def release(self) -> None:
        with self._lock:
            self._loop = self._task = None

    def cancel(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
        self._fire()

    def _fire(self) -> None:
        # cancel() and bind() can both reach here for the same handle; the
        # `_fired` latch is what makes "at most one cancellation ever reaches the
        # Provider" true rather than merely likely.
        with self._lock:
            loop, task = self._loop, self._task
            if loop is None or task is None or self._fired:
                return
            self._fired = True
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            # The per-call loop is already closed: the call finished on its own.
            pass


class BindingTokenizerMixin:
    """Authority identity and window come from `self.binding`, never from module
    constants: the binding_digest recorded on a Run then always describes the
    Authority and window the process actually used. Concrete mixins add only
    count_input_tokens."""

    @property
    def max_input_tokens(self) -> int:
        return self.binding.max_input_tokens

    @property
    def tokenizer_authority(self) -> tuple[str, str, int]:
        return (
            self.binding.tokenizer_authority_name,
            self.binding.tokenizer_authority_version,
            self.max_input_tokens,
        )

    @property
    def binding_digest(self) -> str:
        return binding_digest(self.binding)

    @property
    def provider_profile_digest(self) -> str:
        return compute_provider_profile_digest(asdict(self.policy_metadata), self.tokenizer_authority)


class LocalTestTokenizerMixin(BindingTokenizerMixin):
    """Tokenizer Authority for the deterministic binding: counts Unicode code points
    in the Canonical Request bytes (`local-test-codepoint-v1`)."""

    def count_input_tokens(self, request: PreparedModelRequestV1) -> int:
        verify_request_integrity(request, self)
        return len(request.canonical_bytes.decode("utf-8"))


class InvalidProviderOutput(Exception):
    def __init__(self, kind: str) -> None:
        self.kind = kind


def _deterministic_response(messages, _info) -> ModelResponse:
    prompt = messages[-1].parts[-1].content
    return ModelResponse(
        parts=[TextPart(f"테스트 응답: {prompt}")],
        finish_reason="stop",
        state="complete",
    )


async def _deterministic_stream(messages, _info):
    yield f"테스트 응답: {messages[-1].parts[-1].content}"


class _DeterministicModel(FunctionModel):
    """Stub model that reports the lifecycle a real provider would.

    `FunctionModel` never sets `finish_reason` on a streamed response, so without
    this the adapter would need a test-double branch inside its production
    validation chain. The stub owns the gap instead.
    """

    @asynccontextmanager
    async def request_stream(self, *args, **kwargs):
        async with super().request_stream(*args, **kwargs) as streamed:
            # ponytail: set eagerly rather than on drain — this stub yields a single
            # chunk and the adapter always drains fully. Hook _get_event_iterator if a
            # stub that can end early is ever needed.
            streamed.finish_reason = "stop"
            yield streamed


class CancellableCallMixin:
    """Cancel/Close capability for a Direct-API adapter: hand out the handle for the
    call this thread is about to make, take it back when that call starts, and
    declare the budget for closing its stream afterwards. Deliberately not on the
    Tokenizer mixin -- whether a Provider can be stopped has nothing to do with
    which Authority counts its tokens."""

    @property
    def close_grace_ms(self) -> int:
        """The Binding's declared budget for closing a stream."""
        return self.binding.close_grace_ms

    @property
    def close_grace_seconds(self) -> float:
        return self.close_grace_ms / 1_000

    def new_call_handle(self) -> ProviderCallHandle:
        """A fresh handle for one call. The caller keeps it (to cancel with) and
        passes it into stream()/complete(); nothing is stashed here, so a call made
        on another thread cannot silently end up uncancellable."""
        return ProviderCallHandle()

    def closes_stream(self) -> bool:
        """Close/Cancel readiness declaration: every call this adapter makes wraps
        its stream in _closing_stream, whose `finally` closes exactly once inside
        close_grace_ms. ponytail: a declaration, not a probe -- the deterministic
        binding's Model exposes no closable transport at all, so there is nothing to
        exercise until a binding wants to answer False."""
        return True


class PydanticAIDirectAdapter(CancellableCallMixin):
    """Direct-API adapter shared by every binding. It owns the validation ladder
    and nothing else: a Tokenizer Authority mixin and a binding are supplied by
    the concrete binding class, so no adapter can inherit the wrong Authority or
    record a digest for a binding it never called."""

    # The Retrieval Port this adapter reads `retrieval_status` from. Typed as the
    # Port, so the Protocol has a real consumer rather than being a shape nothing
    # refers to -- and so a Port that ever grew a query method would have to grow it
    # here first, in the one class that could call it.
    retrieval: RetrievalPort = DISABLED_RETRIEVAL

    def __init__(self, model: Model | None, binding: ProviderBindingV1) -> None:
        self._model = model
        self.binding = binding

    @asynccontextmanager
    async def _bound_model(self, handle: ProviderCallHandle | None = None):
        yield self._model

    def _model_settings(self) -> dict | None:
        """None keeps the pre-1.5.1 call shape byte-for-byte unchanged."""
        return None

    @property
    def policy_metadata(self) -> ProviderPolicyMetadata:
        return ProviderPolicyMetadata(
            schema_version="1",
            provider_label=self.binding.provider_label,
            endpoint_disclosure=self.binding.endpoint_disclosure,
            model_revision=self.binding.model_revision,
            transmitted_fields=TRANSMITTED_FIELDS,
            retention_summary=self.binding.retention_summary,
            deletion_summary=self.binding.deletion_summary,
            training_use="not_used",
            processing_region=self.binding.processing_region,
            subprocessors=(),
            session_ttl_seconds=SESSION_TTL_SECONDS,
            # Read from the Port, not spelled here: the only value that exists is
            # the one a Port with no query method can report.
            retrieval_status=self.retrieval.status,
            input_warning_categories=INPUT_WARNING_CATEGORIES,
        )

    def probe(self) -> bool:
        return self._model is not None

    def _call(self, coro, handle: ProviderCallHandle | None = None):
        """The one exception ladder. complete(), stream() and probe() all route
        through it, so the same outage cannot map to two different failure kinds
        depending on which entry point was used."""
        try:
            return _run(coro, handle)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            # BaseException, not Exception: an anyio task group raises a
            # BaseExceptionGroup wrapping only CancelledError, which is neither an
            # Exception nor a CancelledError and would escape the Run untyped.
            raise InvalidProviderOutput(_failure_kind(exc)) from exc

    def complete(
        self, request: PreparedModelRequestV1, handle: ProviderCallHandle | None = None
    ) -> str:
        verify_request_integrity(request, self)
        response = self._call(self._complete(request, handle), handle)
        if response.state != "complete":
            raise InvalidProviderOutput(f"provider_{response.state}")
        if response.finish_reason != "stop":
            mapping = {
                "length": "provider_incomplete",
                "content_filter": "provider_content_filtered",
                "tool_call": "provider_non_text",
            }
            raise InvalidProviderOutput(mapping.get(response.finish_reason, "provider_invalid_response"))
        if not response.parts or any(not isinstance(part, TextPart) for part in response.parts):
            raise InvalidProviderOutput("provider_non_text")
        text = "".join(part.content for part in response.parts)
        if not has_visible_text(text):
            raise InvalidProviderOutput("provider_empty")
        if has_unsafe_code_point(text):
            raise InvalidProviderOutput("provider_invalid_response")
        return text

    async def _complete(
        self, request: PreparedModelRequestV1, handle: ProviderCallHandle | None = None
    ) -> ModelResponse:
        async with self._bound_model(handle) as model:
            return await model_request(
                model,
                _messages(request),
                model_request_parameters=_request_parameters(self.binding.tool_policy),
                model_settings=self._model_settings(),
                instrument=False,
            )

    def stream(
        self,
        request: PreparedModelRequestV1,
        on_delta: Callable[[str], None],
        handle: ProviderCallHandle | None = None,
    ) -> str:
        verify_request_integrity(request, self)
        return self._call(self._stream(request, on_delta, handle), handle)

    async def _stream(
        self,
        request: PreparedModelRequestV1,
        on_delta: Callable[[str], None],
        handle: ProviderCallHandle | None = None,
    ) -> str:
        async with self._bound_model(handle) as model, model_request_stream(
            model,
            _messages(request),
            model_request_parameters=_request_parameters(self.binding.tool_policy),
            model_settings=self._model_settings(),
            instrument=False,
        ) as streamed, _closing_stream(streamed, self.close_grace_seconds, handle):
            return await _drain(streamed, on_delta)


async def _drain(streamed, on_delta: Callable[[str], None], *, capped_output: bool = False) -> str:
    """The single provider-output validation ladder: one TextPart Start/Delta*/End,
    exactly one FinalResultEvent, no tool or non-text part, and a terminal response
    that matches what was streamed. Readiness runs it too, so a binding that emits
    reasoning or tool parts fails closed at /ready instead of on a user's first
    question. `capped_output` is the probe's only relaxation: it caps max_tokens on
    purpose, so `finish_reason: "length"` and a whitespace-only answer are expected
    there. A Run keeps rejecting both."""
    buffer = ""
    started = ended = False
    final_count = 0
    async for event in streamed:
        if isinstance(event, PartStartEvent):
            if started or ended or event.index != 0 or not isinstance(event.part, TextPart):
                raise InvalidProviderOutput("provider_non_text")
            started = True
            if event.part.content:
                on_delta(event.part.content)
                buffer += event.part.content
        elif isinstance(event, PartDeltaEvent):
            if not started or ended or event.index != 0 or not isinstance(event.delta, TextPartDelta):
                raise InvalidProviderOutput("provider_non_text")
            if event.delta.content_delta:
                on_delta(event.delta.content_delta)
                buffer += event.delta.content_delta
        elif isinstance(event, PartEndEvent):
            if (
                not started
                or ended
                or event.index != 0
                or not isinstance(event.part, TextPart)
                or event.part.content != buffer
            ):
                raise InvalidProviderOutput("provider_invalid_response")
            ended = True
        elif isinstance(event, FinalResultEvent):
            if not started or final_count or event.tool_name is not None or event.tool_call_id is not None:
                raise InvalidProviderOutput("provider_non_text")
            final_count += 1
        else:
            raise InvalidProviderOutput("provider_non_text")
    response = streamed.get()
    if not capped_output:
        # Completeness of the answer, which only a Run cares about. The probe caps
        # max_tokens, so an empty or whitespace reply there says nothing about health
        # -- the shape rules above (single TextPart, no reasoning, no tool part) ran
        # on every event either way, and readiness still checks the usage invariant.
        if not started or not ended or final_count != 1 or not has_visible_text(buffer):
            raise InvalidProviderOutput("provider_incomplete" if started else "provider_empty")
        if (
            len(response.parts) != 1
            or not isinstance(response.parts[0], TextPart)
            or response.parts[0].content != buffer
        ):
            raise InvalidProviderOutput("provider_invalid_response")
    if response.state != "complete":
        raise InvalidProviderOutput(f"provider_{response.state}")
    if response.finish_reason != "stop" and not (capped_output and response.finish_reason == "length"):
        mapping = {
            "length": "provider_incomplete",
            "content_filter": "provider_content_filtered",
            "tool_call": "provider_non_text",
        }
        raise InvalidProviderOutput(mapping.get(response.finish_reason, "provider_invalid_response"))
    if has_unsafe_code_point(buffer):
        raise InvalidProviderOutput("provider_invalid_response")
    return buffer


def _quiet_asyncgen_shutdown(loop, context) -> None:
    """The provider SDK's HTTP body generators are finalized when the per-call event
    loop shuts down, and httpcore's own cleanup raises a bare RuntimeError there. It
    is pure teardown noise -- the Run, its answer and its log record are already
    committed -- but it lands on the operational log as a traceback.

    Keyed off the `asyncgen` context key and the exception type, not the handler's
    English wording: only generator finalization raising RuntimeError is dropped, so
    a real failure inside a generator (any other exception type) still gets logged."""
    if "asyncgen" in context and type(context.get("exception")) is RuntimeError:
        return
    loop.default_exception_handler(context)


def _run(coro, handle: ProviderCallHandle | None):
    """asyncio.run with the teardown handler above installed. With a handle the
    per-call loop and task are published so another thread can cancel them exactly
    once; the Runner context manager still owns the loop, so no adapter task can
    outlive this call (and therefore the Application's generation task)."""
    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        loop.set_exception_handler(_quiet_asyncgen_shutdown)
        if handle is None:
            return runner.run(coro)

        async def bound():
            handle.bind(loop, asyncio.current_task())
            try:
                return await coro
            finally:
                handle.release()

        return runner.run(bound())


@asynccontextmanager
async def _closing_stream(
    streamed, grace_seconds: float, handle: ProviderCallHandle | None = None
):
    """Release the Provider's HTTP response before its client -- and the event
    loop -- go away. Draining the SSE body to [DONE] leaves the underlying
    response open, and finalizing it later against an already-closed connection
    pool raises from the async generator shutdown hook on every request.

    Exactly one close per stream, on every path, because it is a `finally`, and it is
    bounded by the Binding's close_grace_ms on every path too -- a close that never
    returns would otherwise hang the generation (or probe) thread, and shutdown with
    it. The Terminal Fence decides only how a failure is *typed*: before it, a close
    failure is a real transport failure; after it the Run is already `cancelled` and
    nothing here may disturb it."""
    body_failed = False
    try:
        yield streamed
    except BaseException:
        body_failed = True
        raise
    finally:
        # Neither `return` nor `raise` may leave this block while a Provider failure
        # is already propagating through it -- both replace the real cause with a
        # teardown symptom.
        close = getattr(streamed, "close_stream", None)
        if close is not None:
            try:
                await asyncio.wait_for(close(), grace_seconds)
            except NotImplementedError:
                # StreamedResponse's default: no closable transport on this Model.
                pass
            except (Exception, asyncio.CancelledError) as exc:
                # Re-read the fence: a Cancel that landed *during* the close must not
                # come back as a transport failure on a Run the user already stopped.
                # KeyboardInterrupt/SystemExit still travel, exactly as in _call.
                if not (handle is not None and handle.cancelled) and not body_failed:
                    raise InvalidProviderOutput("provider_transport") from exc


def _messages(request: PreparedModelRequestV1) -> list[ModelRequest | ModelResponse]:
    messages: list[ModelRequest | ModelResponse] = [
        ModelRequest(parts=[SystemPromptPart(request.system_instruction)])
    ]
    for message in request.messages:
        if message.role == "user":
            messages.append(ModelRequest(parts=[UserPromptPart(message.text)]))
        elif message.role == "assistant":
            messages.append(ModelResponse(parts=[TextPart(message.text)]))
        else:
            raise InvalidProviderOutput("provider_unknown")
    return messages


def _request_parameters(policy: ToolPolicyV1 = ToolPolicyV1.zero) -> ModelRequestParameters:
    """Project the zero-tool policy. A real check, not an assert: `python -O` strips
    asserts, and this is the gate that keeps tools and image output off the wire."""
    if (
        policy.function_tool_count
        or policy.native_tool_count
        or policy.output_tool_count
        or policy.allow_image_output
        or not policy.allow_text_output
    ):
        raise InvalidProviderOutput("provider_non_text")
    return ModelRequestParameters(
        function_tools=[],
        native_tools=[],
        output_tools=[],
        output_mode="text",
        allow_text_output=policy.allow_text_output,
        allow_image_output=policy.allow_image_output,
    )


def _http_failure_kind(status_code: int) -> str:
    if status_code in {401, 403}:
        return "provider_auth"
    if status_code == 429:
        return "provider_rate_limit"
    if status_code in {408, 504}:
        return "provider_timeout"
    return "provider_unavailable" if status_code >= 500 else "provider_transport"


def _sdk_transport_kind(exc: BaseException) -> str:
    """openai raises APITimeoutError (a subclass of APIConnectionError) and pydantic-ai
    re-raises it as ModelAPIError, so the timeout can sit any number of links down the
    cause/context chain -- or inside a group. Walk the whole thing or a real timeout
    gets mislabelled as a transport error and loses its retry semantics."""
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, APITimeoutError):
            return "provider_timeout"
        pending.extend((current.__cause__, current.__context__))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return "provider_transport"


def _failure_kind(exc: BaseException) -> str:
    """Provider exception -> typed failure kind. ModelHTTPError is a ModelAPIError
    and APITimeoutError is an APIConnectionError, so order is load bearing."""
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        # anyio task groups wrap even a lone CancelledError in a group, which is
        # neither an Exception nor a CancelledError and matches nothing below.
        return _failure_kind(exc.exceptions[0])
    if isinstance(exc, InvalidProviderOutput):
        return exc.kind
    if isinstance(exc, asyncio.CancelledError):
        return "provider_cancelled"
    if isinstance(exc, ModelHTTPError):
        return _http_failure_kind(exc.status_code)
    if isinstance(exc, (APITimeoutError, APIConnectionError, ModelAPIError)):
        # Neither OSError nor TimeoutError, so the builtin clauses below never see
        # them; without this an outage read as provider_invalid_response.
        return _sdk_transport_kind(exc)
    if isinstance(exc, TimeoutError):
        return "provider_timeout"
    if isinstance(exc, (ConnectionError, OSError)):
        return "provider_transport"
    if isinstance(exc, UnexpectedModelBehavior):
        return "provider_invalid_response"
    # on_delta is application code; it signals rejection with its own `kind`.
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str):
        return kind
    if isinstance(exc, (AttributeError, TypeError, NameError)):
        # A defect in this adapter, not Provider misbehaviour. Reporting it as a bad
        # answer would hide the bug and feed the retry logic a lie; let it surface.
        raise exc
    return "provider_invalid_response"


class OllamaConservativeTokenizerMixin(BindingTokenizerMixin):
    """Tokenizer Authority for the Ollama binding. qwen is byte-level BPE, so the
    UTF-8 byte length of the Canonical Request is a provable upper bound on the
    real token count, and the Canonical JSON's field/quote overhead already covers
    the chat template's special tokens. probe() re-checks the invariant against
    the Provider's own reported usage.input_tokens."""

    # ponytail: byte-count upper bound -- measured ~5.2x conservative for Korean, so the
    # effective window is narrow. Swap in a real tokenizer (Authority name/version
    # are the only things that change) once the window actually pinches.
    def count_input_tokens(self, request: PreparedModelRequestV1) -> int:
        verify_request_integrity(request, self)
        return len(request.canonical_bytes)


class OllamaLanProxyAdapter(OllamaConservativeTokenizerMixin, PydanticAIDirectAdapter):
    """Real Provider binding: OpenAIChatModel over OllamaProvider, on the same
    Direct-API validation ladder as the deterministic binding.

    The AsyncOpenAI client is built here, per request, and never reused across
    event loops (each call runs its own loop). max_retries comes from the binding
    and is 0: the Provider's default of 2 would turn one user question into three
    outbound requests.

    ponytail: binding.transport_attempt_policy is still only carried in the digest --
    max_retries=0 happens to produce the same single attempt, but nothing verifies it.
    Enforce it when a binding wants to declare anything other than "single"."""

    def __init__(
        self,
        binding: ProviderBindingV1,
        api_key: str,
        http_client_factory: Callable[[], object] | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("Provider credential이 비어 있습니다")
        # model=None: this binding builds its Model per call in _bound_model(), and a
        # None here makes the inherited probe() fail closed if that override is lost.
        super().__init__(None, binding)
        # Held only here, never on the binding, in policy_metadata, or in any digest.
        self._api_key = api_key
        self._http_client_factory = http_client_factory

    # ponytail: one client and one event loop per call, so no connection is ever
    # reused across requests. Fine for a single-worker LAN proxy; if the extra TCP
    # handshake per question ever matters, move to one long-lived loop plus a
    # persistent client.
    @asynccontextmanager
    async def _bound_model(self, handle: ProviderCallHandle | None = None):
        client = AsyncOpenAI(
            base_url=f"{self.binding.endpoint_origin}/v1",
            api_key=self._api_key,
            max_retries=self.binding.max_retries,
            timeout=OLLAMA_CLIENT_TIMEOUT_SECONDS,
            http_client=None if self._http_client_factory is None else self._http_client_factory(),
        )
        body_failed = False
        try:
            yield OpenAIChatModel(
                self.binding.model_revision,
                provider=OllamaProvider(openai_client=client),
            )
        except BaseException:
            body_failed = True
            raise
        finally:
            # Same treatment as the stream close: bounded on every path so a LAN
            # proxy that stops responding cannot hang the Run or shutdown, typed as
            # a transport failure before the fence, silent after it, and never
            # allowed to mask a failure already on its way out.
            try:
                await asyncio.wait_for(client.close(), self.close_grace_seconds)
            except asyncio.CancelledError:
                # A Cancel landing during the close is the call being stopped, not a
                # close failure -- swallowing it would return as if nothing happened.
                raise
            except Exception as exc:
                if not (handle is not None and handle.cancelled) and not body_failed:
                    raise InvalidProviderOutput("provider_transport") from exc

    def _model_settings(self) -> dict:
        # Blocks reasoning deltas at the source; a reasoning part that arrives
        # anyway is a non-TextPart and the ladder fails it as provider_non_text.
        return {"openai_reasoning_effort": "none"}

    def probe(self) -> bool:
        """Content-free readiness probe: the same streaming method, instrument=False,
        zero-tool parameters and output-validation ladder a Run uses. Fails closed
        unless the Provider reports a positive input-token count that the local
        Tokenizer Authority meets or exceeds."""
        request = prepare_model_request(
            (PreparedMessageV1(uuid4(), "user", OLLAMA_PROBE_PROMPT),),
            provider_profile_digest=self.provider_profile_digest,
        )
        return self._call(self._probe(request)) is True

    async def _probe(self, request: PreparedModelRequestV1) -> bool:
        own_count = self.count_input_tokens(request)
        async with self._bound_model() as model, model_request_stream(
            model,
            _messages(request),
            model_request_parameters=_request_parameters(self.binding.tool_policy),
            # max_tokens caps the probe; everything else matches a Run, so a binding
            # that breaks a Run cannot pass readiness.
            model_settings={**self._model_settings(), "max_tokens": OLLAMA_PROBE_MAX_TOKENS},
            instrument=False,
        ) as streamed, _closing_stream(streamed, self.close_grace_seconds, None):
            await _drain(streamed, _ignore_delta, capped_output=True)
            reported = streamed.get().usage.input_tokens
        # A Provider that reports nothing gives the invariant nothing to check, and an
        # unverifiable Tokenizer Authority is not ready.
        return reported > 0 and own_count >= reported


def _ignore_delta(_delta: str) -> None:
    """The probe is content-free: its output is drained for validation and dropped."""


class DeterministicProvider(LocalTestTokenizerMixin, PydanticAIDirectAdapter):
    """Deterministic Test/CI binding. Kept alongside the real one so both bindings
    stay on one API/SSE/Policy/Readiness/Failure contract."""

    def __init__(
        self, model: Model | None = None, binding: ProviderBindingV1 = DETERMINISTIC_BINDING
    ) -> None:
        super().__init__(
            model
            or _DeterministicModel(
                _deterministic_response,
                stream_function=_deterministic_stream,
                model_name="deterministic-v1",
            ),
            binding,
        )
