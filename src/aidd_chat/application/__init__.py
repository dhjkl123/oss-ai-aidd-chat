from collections.abc import Callable, Mapping
import asyncio
from contextlib import suppress
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import logging
from secrets import token_urlsafe
from threading import Event, Lock, Thread, Timer
import re
import time
import unicodedata
from typing import Literal, Protocol, get_args, runtime_checkable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError, field_validator

from aidd_chat.domain import (
    ActiveRunConflict,
    RunObservationV1,
    CapacityExceeded,
    DeploymentLimits,
    OutputLimitExceeded,
    RetryExhausted,
    RetryNotRetryable,
    RunLease,
    UNCONFIGURED_LIMITS,
)
from aidd_chat.contracts import (
    CancelResultV1,
    ContextIntegrityError,
    RunStage,
    PreparedMessageV1,
    PreparedModelRequestV1,
    ProviderFailureV1,
    RetryCommand,
    RunEventV1,
    RunProjectionV1,
    TerminalRunState,
    has_unsafe_code_point,
    has_visible_text,
    prepare_model_request,
)


SESSION_TTL_SECONDS = 3_600
CONVERSATION_TTL = timedelta(seconds=SESSION_TTL_SECONDS)
# Wall-clock budget for one Run, measured from accept (accept_question/accept_retry)
# and covering everything after it: the queue, context selection, the token count,
# Provider I/O and the stream's Close Grace. Nothing gets time outside it.
RUN_DEADLINE_SECONDS = 120
RUN_DEADLINE = timedelta(seconds=RUN_DEADLINE_SECONDS)
PROBE_TIMEOUT_SECONDS = 2
READINESS_CACHE_SECONDS = 30.0
MAX_POLICY_TEXT_LENGTH = 256
MAX_SUBPROCESSOR_COUNT = 32
MAX_SUBPROCESSOR_LABEL_LENGTH = 128
# System + Current + History + role separators + special tokens, output reservation
# excluded -- the single budget the Tokenizer Authority counts against (see Design Notes).
CONTEXT_TOKEN_BUDGET = 8_192
# How many Telemetry Records this process keeps in memory for an operator to read
# back. Content-free and fixed-width, so the ceiling is about memory, nothing else.
MAX_TELEMETRY_RECORDS = 512

_log = logging.getLogger(__name__)


# Derived, never re-typed: a sixth Stage added to the contract is a sixth Stage
# here, and there is no second list to forget.
RUN_STAGES = get_args(RunStage)
TERMINAL_RUN_STATES = get_args(TerminalRunState)


@dataclass(frozen=True)
class RunTelemetryV1:
    """The whole of what this process may observe about a Run. A closed dataclass,
    not a log call with a message: the moment a free-form field exists the Allowlist
    is gone, and a Prompt, an answer, a Secret, a Run ID or a Model Revision can
    reach it by accident. `error_class` is a Code -- a ProviderFailureKind, a
    Capacity Code or a Readiness Code -- never Provider text, and never a success:
    a healthy readiness transition is `error_class=None`, so an operator filtering
    `error_class is not None` sees faults and only faults.

    `duration_ms` is None wherever the frozen Design Note's definition --
    Acceptance-to-terminal-commit -- does not apply: a Record about a Run that has
    not reached a terminal, and one about something that is not a Run at all
    (a Capacity refusal, a readiness transition). A `0` there would be a measurement
    nobody made.

    `__post_init__` is what makes "closed" true at RUNTIME. The Literal annotations
    are documentation to the interpreter; without this, a typo -- or a caller handing
    over a Provider sentence as an `error_class` -- enters the Allowlist silently,
    which is the one failure mode this Record exists to prevent."""

    correlation_id: UUID
    stage: RunStage
    duration_ms: int | None = None
    terminal_state: TerminalRunState | None = None
    error_class: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.correlation_id, UUID) or self.correlation_id.version != 4:
            raise ValueError("Telemetry correlation_id는 UUIDv4여야 합니다")
        if self.stage not in RUN_STAGES:
            raise ValueError("Telemetry stage가 Run Stage가 아닙니다")
        if self.terminal_state is not None and self.terminal_state not in TERMINAL_RUN_STATES:
            raise ValueError("Telemetry terminal_state가 Terminal State가 아닙니다")
        if self.duration_ms is not None and (
            not isinstance(self.duration_ms, int) or isinstance(self.duration_ms, bool)
        ):
            raise ValueError("Telemetry duration_ms는 정수여야 합니다")
        if self.error_class is not None and self.error_class not in TELEMETRY_ERROR_CLASSES:
            raise ValueError("Telemetry error_class가 알려진 Code가 아닙니다")


class ContextTooLarge(Exception):
    """System Instruction + current question alone exceed the token budget. Raised
    before any Conversation state or Message is created, mapped to a 413
    context_too_large envelope in main.py."""


class ProviderProfileChanged(Exception):
    """This Run could have been retried, but the Provider bound now is not the one
    it was sent to. A different fact from `RetryNotRetryable` -- that one means the
    target was never a retry target at all (completed, or not yet terminal), and the
    only way on is a new question. Here the same question is still askable, so the
    client must not fail the conversation closed. Raised before `accept_retry`, so
    no Run, Message, Attempt or Provider call exists; mapped to a 409
    provider_profile_changed envelope in main.py."""

TRANSMITTED_FIELDS = (
    "system_instruction",
    "current_message",
    "selected_prior_messages",
)
INPUT_WARNING_CATEGORIES = (
    "personal_information",
    "company_confidential_data",
    "credentials",
)
_POLICY_FIELDS = frozenset(
    {
        "schema_version",
        "provider_label",
        "endpoint_disclosure",
        "model_revision",
        "transmitted_fields",
        "retention_summary",
        "deletion_summary",
        "training_use",
        "processing_region",
        "subprocessors",
        "session_ttl_seconds",
        "retrieval_status",
        "input_warning_categories",
    }
)
_UNKNOWN_METADATA = re.compile(
    r"(?:unknown|undefined|not[ _-]?configured|unavailable|(?:^|[\s_:/-])n/?a(?:$|[\s_:/-])|none|null|미정|미확정|알\s*수\s*없음)",
    re.IGNORECASE,
)
_UNSAFE_METADATA = re.compile(
    r"(?:secret|credential|password|token|api[_ -]?key|authorization|bearer|private[_ -]?(?:endpoint|route|network|host|routing)|internal[_ -]?(?:endpoint|route|network|host|routing)|query(?:[_ -]?string)?|trace|raw[_ -]?response|stack[_ -]?trace|debug|response[_ -]?(?:body|headers?)|chain[_ -]?of[_ -]?thought|intermediate|cookie|jwt|oauth|https?://|[?&])",
    re.IGNORECASE,
)
_OPAQUE_ENDPOINT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROVIDER_FAILURES = {
    "provider_auth": (False, "Provider 인증을 확인해 주세요."),
    "provider_rate_limit": (True, "요청이 많아요. 잠시 후 다시 시도해 주세요."),
    "provider_unavailable": (True, "Provider가 일시적으로 응답하지 않아요. 잠시 후 다시 시도해 주세요."),
    "provider_transport": (True, "네트워크 오류로 답변을 받지 못했어요. 잠시 후 다시 시도해 주세요."),
    "provider_timeout": (True, "응답 시간이 초과됐어요. 다시 시도해 주세요."),
    "provider_cancelled": (True, "Provider 작업이 중단됐어요. 다시 시도해 주세요."),
    "provider_non_text": (False, "텍스트 답변을 받을 수 없어 새 대화에서 다시 시도해 주세요."),
    "provider_empty": (False, "Provider가 빈 답변을 반환했어요. 새 대화에서 다시 시도해 주세요."),
    "provider_incomplete": (True, "답변이 끝까지 생성되지 않았어요. 다시 시도해 주세요."),
    "provider_suspended": (True, "Provider 답변이 일시 중단됐어요. 다시 시도해 주세요."),
    "provider_interrupted": (True, "Provider 답변이 중단됐어요. 다시 시도해 주세요."),
    "provider_invalid_response": (False, "Provider 응답 형식을 확인할 수 없어요. 새 대화에서 다시 시도해 주세요."),
    "provider_content_filtered": (False, "Provider 정책으로 답변을 표시할 수 없어요. 새 대화에서 다시 시도해 주세요."),
    "provider_unknown": (False, "Provider 오류가 발생했어요. 새 대화를 시작해 주세요."),
    "capacity_exceeded": (False, "답변이 이 대화에 허용된 크기를 넘어 저장하지 못했어요. 새 대화를 시작해 주세요."),
}


# Every Code that may appear in a Telemetry Record's `error_class`, in one place.
# Three sources, none of them free text: Provider failure kinds, the Capacity Codes
# the boundary answers with, and the Readiness Codes. `unclassified` exists so an
# unknown Code is flattened rather than raising out of a request path -- the Record
# stays closed either way, which is the point.
CAPACITY_CODES = frozenset({
    "conversation_capacity_exceeded",
    "run_capacity_exceeded",
    "session_run_capacity_exceeded",
    "queue_capacity_exceeded",
    "provider_busy",
    "spend_limit_reached",
    "rate_limited",
    "stream_capacity_exceeded",
    "deployment_unconfigured",
})
# No "ready" here on purpose: health is not an Error Class. A healthy readiness
# transition is recorded with `error_class=None`, so filtering on that field yields
# faults and only faults.
READINESS_CODES = frozenset({
    "integrity_poisoned",
    "deployment_unconfigured",
    "provider_unbound",
    "policy_projection_invalid",
    "tokenizer_authority_invalid",
    "cancellation_unsupported",
    "provider_probe_failed",
})
TELEMETRY_ERROR_CLASSES = (
    frozenset(_PROVIDER_FAILURES)
    | CAPACITY_CODES
    | READINESS_CODES
    | {"provider_cancel_failed", "conversation_expired", "unclassified"}
)
# One Record per Capacity Code per window. A ceiling under sustained load refuses
# once per request; without this the 512-slot deque is filled with identical Records
# in seconds and every Run terminal in it is evicted.
CAPACITY_TELEMETRY_WINDOW_SECONDS = 60.0


def _telemetry_error_class(error_class: str | None) -> str | None:
    """An Error Class the Record will accept. Values arrive from outside this module
    -- a Capacity Code raised by the Store, a ProviderFailureKind -- and an unknown
    one is flattened to `unclassified` rather than raised: this runs inside request
    paths, and a refusal that crashed the refusal would be worse than one that is
    merely less specific."""
    if error_class is None or error_class in TELEMETRY_ERROR_CLASSES:
        return error_class
    return "unclassified"


class _ProviderOutputRejected(Exception):
    def __init__(self, kind: str) -> None:
        self.kind = kind


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _contains_unsafe_text(value: str) -> bool:
    """The contracts predicate, re-exported under a local name so every caller in
    this module reads the same rule as the Event validators and the Domain commits.
    No second implementation: a text path with its own copy is the failure mode the
    module-wide gate in tests/test_story_1_9.py exists to catch."""
    return has_unsafe_code_point(value)


def _question_digest(content: str) -> tuple[str, str]:
    normalized = _normalize_text(content)
    if _contains_unsafe_text(normalized):
        raise ValueError("질문에 유효하지 않은 문자가 포함되었습니다")
    digest = sha256(
        json.dumps(
            {"kind": "question", "content": normalized},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return normalized, digest


def _deadline(now: datetime) -> tuple[datetime, float]:
    """(wall-clock, monotonic) Deadline for a Run accepted at `now`. Enforcement
    always compares the monotonic value: an NTP step must not fire a live Run's
    Deadline early, or postpone it forever. The wall-clock one is kept for the
    record, and is derived from the same `now` the Run's created_at is."""
    return now + RUN_DEADLINE, time.monotonic() + RUN_DEADLINE.total_seconds()


def _retry_digest(retry_of_run_id: UUID) -> str:
    """The Idempotency digest of a Retry Command. A Retry has no content, so the
    target Run is the whole request -- the same key with a different target is the
    same idempotency_conflict a question with different text is."""
    return sha256(
        json.dumps(
            {"kind": "retry", "retry_of_run_id": str(retry_of_run_id)},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _provider_profile_digest(provider: object) -> str | None:
    """The bound Provider's `provider_profile_digest`, or None when it has none to
    give -- an UnboundProvider, or one whose property raises. None is never treated
    as a match: `_cached_success_locked` only compares it against a Digest a probe
    actually recorded, which is always a real one."""
    try:
        digest = provider.provider_profile_digest
    except Exception:
        return None
    return digest if isinstance(digest, str) and digest else None


def _provider_satisfies_context_budget(provider: object) -> bool:
    """Counter 부재·한도 미달 Gate: no count_input_tokens, provider_profile_digest,
    or tokenizer_authority -- all of which the request path requires -- a
    Tokenizer Authority budget below CONTEXT_TOKEN_BUDGET, or a tokenizer_authority
    whose own window disagrees with max_input_tokens (exactly the drift
    provider_profile_digest exists to catch), fails readiness closed."""
    try:
        max_tokens = provider.max_input_tokens
        profile_digest = provider.provider_profile_digest
        tokenizer_authority = provider.tokenizer_authority
    except AttributeError:
        return False
    if not (isinstance(tokenizer_authority, tuple) and len(tokenizer_authority) == 3):
        return False
    name, version, authority_window = tokenizer_authority
    return (
        callable(getattr(provider, "count_input_tokens", None))
        and isinstance(max_tokens, int)
        and not isinstance(max_tokens, bool)
        and max_tokens >= CONTEXT_TOKEN_BUDGET
        and isinstance(profile_digest, str)
        and bool(profile_digest)
        and isinstance(name, str)
        and bool(name)
        and isinstance(version, str)
        and bool(version)
        and isinstance(authority_window, int)
        and not isinstance(authority_window, bool)
        and authority_window == max_tokens
    )


def _provider_supports_cancellation(provider: object) -> bool:
    """Cancel/Close Gate: a Provider that hands out no cancel handle, does not
    confirm it closes its stream, or declares no usable close budget cannot honour a
    stop request -- so it fails readiness closed and questions are refused rather
    than becoming unstoppable. Zero is not a budget: `wait_for(close(), 0)` cancels
    the close before it can start and strands the Provider's response generators."""
    grace = getattr(provider, "close_grace_ms", None)
    if not (
        isinstance(provider, CancellableModelProviderPort)
        and isinstance(grace, int)
        and not isinstance(grace, bool)
        and grace > 0
    ):
        return False
    try:
        return provider.closes_stream() is True
    except Exception:
        return False


def _effective_budget(provider: "ModelProviderPort") -> int:
    """The budget is exactly CONTEXT_TOKEN_BUDGET (spec) even if a bound Provider
    advertises a larger window -- never smaller, via the Counter/limit Gate above."""
    return min(provider.max_input_tokens, CONTEXT_TOKEN_BUDGET)


def _provider_failure(kind: str) -> ProviderFailureV1:
    """The Correlation ID here is a placeholder and nothing more: every failure
    commit routes through `_commit_terminal`, which replaces it with the value the
    Run has carried since acceptance. Passing the Run's ID in would be a second way
    to be right about the same fact, and a lock taken to compute a value that is
    then discarded."""
    retryable, message = _PROVIDER_FAILURES[kind]
    return ProviderFailureV1(
        kind=kind,
        retryable=retryable,
        correlation_id=uuid4(),
        message=message,
    )


def _safe_public_text(value: object, *, max_length: int = MAX_POLICY_TEXT_LENGTH) -> bool:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > max_length:
        return False
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co", "Cn"} for char in value):
        return False
    return not _UNKNOWN_METADATA.search(value) and not _UNSAFE_METADATA.search(value)


class PolicyProjectionV1(BaseModel):
    """브라우저에 공개할 수 있는 닫힌 정책 Projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"]
    provider_label: StrictStr
    endpoint_disclosure: StrictStr
    model_revision: StrictStr
    transmitted_fields: tuple[
        Literal["system_instruction", "current_message", "selected_prior_messages"],
        ...,
    ]
    retention_summary: StrictStr
    deletion_summary: StrictStr
    training_use: Literal["not_used", "provider_policy"]
    processing_region: StrictStr
    subprocessors: tuple[StrictStr, ...]
    session_ttl_seconds: Literal[3_600]
    retrieval_status: Literal["disabled"]
    input_warning_categories: tuple[
        Literal["personal_information", "company_confidential_data", "credentials"],
        ...,
    ]

    @field_validator(
        "provider_label",
        "model_revision",
        "retention_summary",
        "deletion_summary",
        "processing_region",
        mode="before",
    )
    @classmethod
    def _validate_public_text(cls, value: object) -> object:
        if not _safe_public_text(value):
            raise ValueError("공개 정책 문자열은 비어 있지 않은 값이어야 합니다")
        return value

    @field_validator("endpoint_disclosure", mode="before")
    @classmethod
    def _validate_endpoint(cls, value: object) -> object:
        if not _safe_public_text(value, max_length=64) or not _OPAQUE_ENDPOINT.fullmatch(value):
            raise ValueError("Endpoint는 opaque 공개 label이어야 합니다")
        lowered = value.casefold()
        if (
            lowered in {"localhost", "127.0.0.1", "private", "internal"}
            or any(word in lowered for word in ("localhost", "private", "internal"))
            or lowered.endswith((".local", ".internal"))
            or bool(re.fullmatch(r"\d+(?:\.\d+){3}", lowered))
        ):
            raise ValueError("Endpoint 공개 label이 미확정입니다")
        return value

    @field_validator("transmitted_fields", mode="before")
    @classmethod
    def _canonical_transmitted_fields(cls, value: object) -> tuple[str, ...]:
        return _canonical_tuple(value, TRANSMITTED_FIELDS, "전송 필드")

    @field_validator("input_warning_categories", mode="before")
    @classmethod
    def _canonical_input_warning_categories(cls, value: object) -> tuple[str, ...]:
        return _canonical_tuple(value, INPUT_WARNING_CATEGORIES, "입력 금지 범주")

    @field_validator("subprocessors", mode="before")
    @classmethod
    def _canonical_subprocessors(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)) or len(value) > MAX_SUBPROCESSOR_COUNT:
            raise ValueError("Subprocessor는 label tuple이어야 합니다")
        return tuple(value)

    @field_validator("subprocessors")
    @classmethod
    def _validate_subprocessors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _safe_public_text(item, max_length=MAX_SUBPROCESSOR_LABEL_LENGTH) for item in value):
            raise ValueError("Subprocessor label이 비어 있거나 안전하지 않습니다")
        if len(set(value)) != len(value):
            raise ValueError("Subprocessor label은 중복될 수 없습니다")
        return value


def _canonical_tuple(value: object, expected: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label}의 canonical 값이 필요합니다")
    values = tuple(value)
    if (
        len(values) != len(expected)
        or any(item not in expected for item in values)
        or any(values.count(item) != 1 for item in expected)
    ):
        raise ValueError(f"{label}의 canonical 값이 필요합니다")
    return expected


@dataclass(frozen=True)
class ProviderPolicyMetadata:
    schema_version: str
    provider_label: str
    endpoint_disclosure: str
    model_revision: str
    transmitted_fields: tuple[str, ...]
    retention_summary: str
    deletion_summary: str
    training_use: str
    processing_region: str
    subprocessors: tuple[str, ...]
    session_ttl_seconds: int
    retrieval_status: str
    input_warning_categories: tuple[str, ...]


def build_policy_projection(metadata: object) -> PolicyProjectionV1:
    """완전한 공개 metadata만 닫힌 Projection으로 변환한다."""
    if isinstance(metadata, ProviderPolicyMetadata):
        values = asdict(metadata)
    elif isinstance(metadata, Mapping):
        values = dict(metadata)
    else:
        raise ValueError("Provider 공개 정책 metadata가 없습니다")

    if set(values) != _POLICY_FIELDS:
        raise ValueError("Provider 공개 정책 metadata가 불완전하거나 확장되었습니다")
    for value in values.values():
        _reject_unsafe_value(value)
    try:
        return PolicyProjectionV1.model_validate(values)
    except ValidationError as exc:
        raise ValueError("Provider 공개 정책 metadata를 검증할 수 없습니다") from exc


def _reject_unsafe_value(value: object) -> None:
    if isinstance(value, str):
        if value in TRANSMITTED_FIELDS + INPUT_WARNING_CATEGORIES:
            return
        if not _safe_public_text(value):
            raise ValueError("Provider 공개 정책에 비공개 detail이 포함되었습니다")
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_unsafe_value(item)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _reject_unsafe_value(key)
            _reject_unsafe_value(item)


@dataclass(frozen=True)
class Conversation:
    conversation_id: UUID
    created_at: datetime
    expires_at: datetime


class ConversationStorePort(Protocol):
    def create(self, conversation: Conversation, capability_hash: str) -> None: ...

    def authorize(self, conversation_id: UUID, capability_hash: str, now: datetime) -> None: ...

    def accept_question(
        self,
        conversation_id: UUID,
        capability_hash: str,
        idempotency_key: str,
        digest: str,
        content: str,
        now: datetime,
        deadline_at: datetime | None = None,
        deadline_monotonic: float | None = None,
        correlation_id: UUID | None = None,
    ) -> object: ...

    def accept_retry(
        self,
        conversation_id: UUID,
        capability_hash: str,
        idempotency_key: str,
        digest: str,
        retry_of_run_id: UUID,
        max_attempts: int,
        now: datetime,
        deadline_at: datetime | None = None,
        deadline_monotonic: float | None = None,
        correlation_id: UUID | None = None,
    ) -> object: ...

    def record_prepared_request(
        self,
        conversation_id: UUID,
        expected_active_run_id: UUID,
        request: PreparedModelRequestV1,
        dropped_turn_count: int,
    ) -> bool: ...

    def retry_target(
        self, conversation_id: UUID, capability_hash: str, retry_of_run_id: UUID, now: datetime
    ) -> tuple[PreparedModelRequestV1 | None, int, str, str | None, int]:
        """(Snapshot, dropped Turn count, question, the target's Binding Digest, the
        lineage's Attempt count) of the Run a Retry may re-run. Read-only and raising
        RunNotFound / RetryNotRetryable, so the caller can refuse before
        `accept_retry` consumes an Attempt. The Binding Digest is None for a Run that
        failed before it ever reached the Provider -- which is not a Binding change.
        The Attempt count is what `accept_retry` will compare against
        `max_attempts`, read here so a caller can order Exhaustion ahead of its own
        refusals."""
        ...

    def replay_question(
        self,
        conversation_id: UUID,
        capability_hash: str,
        idempotency_key: str,
        digest: str,
        now: datetime,
    ) -> RunProjectionV1 | None: ...

    def mark_running(
        self,
        conversation_id: UUID,
        run_id: UUID,
        now: datetime,
        monotonic_now: float,
        provider_binding_digest: str | None = None,
    ) -> bool: ...

    def get_context_snapshot(
        self, conversation_id: UUID, now: datetime
    ) -> tuple[tuple[PreparedMessageV1, PreparedMessageV1], ...] | None:
        """None distinguishes a missing/expired Conversation from a valid,
        merely-empty History -- callers must fail the Run on None, never treat
        it as 'no prior Turns'."""
        ...

    def commit_context_truncated(
        self, conversation_id: UUID, expected_active_run_id: UUID, dropped_turn_count: int, now: datetime
    ) -> bool: ...

    def commit_completed(
        self, conversation_id: UUID, expected_active_run_id: UUID, content: str, now: datetime
    ) -> bool: ...

    def commit_delta(
        self, conversation_id: UUID, expected_active_run_id: UUID, text: str, now: datetime
    ) -> bool: ...

    def commit_stream_completed(
        self,
        conversation_id: UUID,
        expected_active_run_id: UUID,
        content: str,
        mismatch_failure: ProviderFailureV1,
        now: datetime,
    ) -> bool: ...

    def commit_failed(
        self,
        conversation_id: UUID,
        expected_active_run_id: UUID,
        failure: ProviderFailureV1,
        now: datetime,
    ) -> bool: ...

    def commit_stream_failed(
        self,
        conversation_id: UUID,
        expected_active_run_id: UUID,
        failure: ProviderFailureV1,
        now: datetime,
    ) -> bool: ...

    def run_past_deadline(
        self, conversation_id: UUID, run_id: UUID, monotonic_now: float
    ) -> bool: ...

    def commit_timeout(
        self,
        conversation_id: UUID,
        expected_active_run_id: UUID,
        failure: ProviderFailureV1,
        now: datetime,
    ) -> bool: ...

    def commit_cancelled(self, run_id: UUID, capability_hash: str, now: datetime) -> bool:
        """Cancel is addressed by Run ID alone, so the store resolves the
        Conversation itself and raises ConversationNotFound / ConversationExpired
        exactly like get_run does. False means the Run was already terminal."""
        ...

    def get_run(self, run_id: UUID, capability_hash: str, now: datetime) -> RunProjectionV1: ...

    def get_run_snapshot(
        self, run_id: UUID, capability_hash: str, now: datetime
    ) -> tuple[RunProjectionV1, tuple[RunEventV1, ...]]: ...

    def get_run_slice(
        self, run_id: UUID, capability_hash: str, cursor: int, now: datetime
    ) -> tuple[RunProjectionV1, tuple[RunEventV1, ...]]: ...

    def expire(self, conversation_id: UUID, now: datetime) -> bool:
        """Fence, Purge, then cancel -- the Periodic Sweeper's entry point, and the
        same operation Access-time Expiry performs. The cancel comes last and
        outside the Conversation lock, because a `cancel()` that blocks must not
        stall every other request on that Conversation; the Fence has already made
        every in-flight Provider callback unable to write. False if it was not due,
        and True only for the caller that actually Purged."""
        ...

    def sweep(self, now: datetime) -> int: ...

    def run_deadline_monotonic(self, run_id: UUID, capability_hash: str) -> float | None: ...

    def apply_limits(self, limits: DeploymentLimits) -> None: ...

    def set_run_canceller(self, cancel_run: Callable[[UUID], None]) -> None: ...

    def set_purge_observer(
        self, observe: "Callable[[RunObservationV1 | None], None]"
    ) -> None:
        """How an expiry the Store Purged on its own Sweeper thread is reported.
        Without it the common production path -- nobody polls, the timer fires --
        ends Runs unobserved."""
        ...

    def observe_run(
        self, conversation_id: UUID, run_id: UUID
    ) -> "RunObservationV1 | None":
        """The Run's own Correlation ID, Stage, State, terminal Error kind and
        Duration, or None if it is gone. Capability-free: this is the Application
        reading its own Domain for Telemetry, and every alternative means
        re-deriving a Run's state above the Domain and getting it wrong."""
        ...

    def reserve_run(self, conversation_id: UUID) -> RunLease:
        """A Run slot, taken before any Domain state exists. Raises CapacityExceeded
        rather than returning a falsy lease, so a caller cannot forget to check."""
        ...

    def start_run(self, lease: RunLease) -> None: ...

    def release_run(self, lease: RunLease) -> None: ...

    def expired_stream_tail(
        self, run_id: UUID, capability_hash: str, cursor: int
    ) -> tuple[RunEventV1, ...] | None:
        """The expiry tail for a Purged Conversation's Run, or None if it was never
        expired. Readable by any holder of the Capability, however late and whoever
        won the Purge."""
        ...

    def reserve_stream(self) -> bool: ...

    def release_stream(self) -> None: ...

    def check_rate(self, key: object, kind: str, limit: int) -> bool: ...

    def close(self) -> None: ...


class ModelProviderPort(Protocol):
    def complete(self, request: PreparedModelRequestV1) -> str: ...

    def probe(self) -> bool: ...

    @property
    def policy_metadata(self) -> ProviderPolicyMetadata: ...

    @property
    def tokenizer_authority(self) -> tuple[str, str, int]:
        """(name, version, max_input_tokens). Only story 1.11's real Provider swap
        needs to change; everything else is written against this Port."""
        ...

    @property
    def max_input_tokens(self) -> int: ...

    @property
    def provider_profile_digest(self) -> str: ...

    @property
    def binding_digest(self) -> str:
        """SHA-256 of the ProviderBindingV1 snapshot, recorded on each Run."""
        ...

    def count_input_tokens(self, request: PreparedModelRequestV1) -> int: ...


class SupportsCancel(Protocol):
    """The only thing the Application ever asks of a Provider call handle."""

    def cancel(self) -> None: ...


@runtime_checkable
class CancellableModelProviderPort(Protocol):
    """Optional provider capability. `new_call_handle()` is called on the thread
    that is about to make the Provider call and returns a one-shot handle whose
    `cancel()` another thread may invoke; `closes_stream()` confirms the stream is
    always closed, and `close_grace_ms` is the budget it gets. All three are
    declared here because readiness enforces all three -- a Provider that satisfies
    the Protocol and still fails the Gate would leave an operator with nothing to
    look at."""

    @property
    def close_grace_ms(self) -> int: ...

    def new_call_handle(self) -> SupportsCancel: ...

    def closes_stream(self) -> bool:
        """True only if every call this Provider makes closes its stream context
        exactly once, within close_grace_ms."""
        ...


@runtime_checkable
class StreamingModelProviderPort(Protocol):
    """Optional provider capability, probed with isinstance before the streaming
    commit protocol is used. Declared separately from ModelProviderPort so a
    provider that only implements complete() stays a valid provider."""

    def stream(
        self, request: PreparedModelRequestV1, on_delta: Callable[[str], None]
    ) -> str: ...


@dataclass(frozen=True)
class _BootstrapBinding:
    provider: ModelProviderPort
    projection: PolicyProjectionV1 | None


@dataclass
class _Generation:
    """One in-flight generation: the worker thread, plus the Provider call handle
    it published so `cancel_run` -- running on a request thread -- can reach it."""

    conversation_id: UUID
    thread: Thread
    # The Run's Correlation ID -- the value ChatApplication minted at acceptance and
    # the Domain already holds, cached here, never minted here. Read on exactly one
    # path: a Provider cancel that fails after the Conversation was Purged, where
    # there is no Run left to read it from.
    correlation_id: UUID
    handle: "SupportsCancel | None" = None
    # Set once this Run has been cancelled: its terminal state is already
    # committed, so the next question must wait for the task itself to end.
    cancelled: bool = False
    # Set when that cancellation was the Deadline, not the user. Whatever the
    # Provider raises on the way out, this Run's terminal is `timeout`.
    timed_out: bool = False
    # The Deadline alarms for this Run, armed by the task itself. This is what
    # makes the Deadline hold with no client attached.
    timers: list["Timer"] = field(default_factory=list)


@dataclass
class _ProbeFlight:
    provider: object
    binding: _BootstrapBinding
    # The `provider_profile_digest` this flight is probing. AR27 keys the success
    # Cache by Provider Profile, not by object identity, so the value the Cache will
    # be stamped with is captured before the probe runs and re-checked after it: a
    # rebind mid-flight must not stamp the new Provider with the old one's result.
    digest: str | None
    deadline: float
    event: Event
    result: bool = False


# Omitting `limits` means an in-process unit test never had a deployment; passing
# None means Bootstrap tried and failed. Only the second is a reason to refuse
# readiness, so the two cannot be the same value -- and a frozen DeploymentLimits
# is a real one, unlike a bare sentinel object.
_UNCONFIGURED = DeploymentLimits()


class ChatApplication:
    PROBE_TIMEOUT_SECONDS = PROBE_TIMEOUT_SECONDS
    READINESS_CACHE_SECONDS = READINESS_CACHE_SECONDS

    def __setattr__(self, name: str, value: object) -> None:
        if name == "policy":
            raise AttributeError("policy_projection만 읽을 수 있습니다")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        store: ConversationStorePort,
        provider: ModelProviderPort,
        max_attempts_per_lineage: int,
        limits: DeploymentLimits | None = _UNCONFIGURED,
    ) -> None:
        # Required, with no code default: the limit is a deployment setting Bootstrap
        # owns, and a process that was never told one must not start rather than
        # discover it on a user's first 재시도.
        if not 1 <= max_attempts_per_lineage <= 8:
            raise ValueError("max_attempts_per_lineage는 1..8이어야 합니다")
        self._max_attempts_per_lineage = max_attempts_per_lineage
        # An explicit None means Bootstrap could not validate this deployment's
        # ceilings. The process still starts -- so /ready can say 503 instead of
        # vanishing -- but the Readiness Gate refuses it, which is what blocks
        # questions. Omitting the argument entirely is the in-process unit-test
        # case: inert defaults, and no deployment ever takes that path because
        # Bootstrap always passes the argument.
        self._limits = limits
        self.store = store
        store.apply_limits(self.limits)
        self._readiness_lock = Lock()
        self._provider_binding = provider
        self._policy_projection: PolicyProjectionV1 | None
        try:
            self._policy_projection = build_policy_projection(provider.policy_metadata)
        except Exception:
            self._policy_projection = None
        self._bootstrap_binding = _BootstrapBinding(provider, self._policy_projection)
        self._probe_inflight: _ProbeFlight | None = None
        self._last_success_at: float | None = None
        self._last_success_digest: str | None = None
        self._last_success_projection: PolicyProjectionV1 | None = None
        self._generation_lock = Lock()
        self._generation_tasks: dict[UUID, _Generation] = {}
        # The Allowlist Telemetry sink. Private, because it is appended from
        # generation threads, the Store's Sweeper thread and request threads at once
        # -- `telemetry_snapshot()` is the read path. Bounded and content-free.
        # Deliberately no endpoint and no export: every Record is also a log line,
        # an external backend is an Ask First, and a new public surface is not this
        # story's to add.
        self._telemetry: deque[RunTelemetryV1] = deque(maxlen=MAX_TELEMETRY_RECORDS)
        # Guards the sink AND the two dedupe tables below, all of which are touched
        # from those same threads.
        self._telemetry_lock = Lock()
        # Last reported readiness reason, so /ready being polled once a second by a
        # platform produces one Record per *change* rather than one per poll.
        self._last_readiness_reason: str | None = "unreported"
        # Last time each Capacity Code was reported, for the same reason.
        self._capacity_reported: dict[str, float] = {}

        # ponytail: sticky flag, no automatic recovery -- a digest mismatch means the
        # binding itself is corrupted, so readiness stays fail-closed until restart.
        self._integrity_poisoned = False
        # Last, deliberately: the Store may call this from its Sweeper thread the
        # moment it has it, and `_request_provider_cancel` reads `_generation_lock`
        # and `_generation_tasks`, which only exist now.
        store.set_run_canceller(self._request_provider_cancel)
        # Same reason, same moment: the Sweeper thread may Purge as soon as it can,
        # and an expiry it drives is the one that has no request behind it to notice.
        store.set_purge_observer(self._observe_purge)

    @property
    def provider(self) -> ModelProviderPort:
        return self._provider_binding

    @property
    def policy_projection(self) -> PolicyProjectionV1 | None:
        return self._policy_projection

    @property
    def limits(self) -> DeploymentLimits:
        """The configured ceilings, or -- when Bootstrap could not validate them --
        the fail-closed ones. Not the permissive in-process defaults: Conversation
        creation does not consult the Readiness Gate, so an unvalidated deployment
        would otherwise hand out Conversations under a ceiling nobody chose."""
        return UNCONFIGURED_LIMITS if self._limits is None else self._limits

    @property
    def is_configured(self) -> bool:
        """False when Bootstrap could not validate this deployment's ceilings. Every
        ceiling is then zero, so the process is fail-closed either way -- this exists
        so the refusal can name the real reason instead of `rate_limited`."""
        return self._limits is not None

    @property
    def trusted_proxy_hops(self) -> int:
        return self.limits.trusted_proxy_hops

    def allow_client_ip(self, client_ip: str) -> bool:
        """The Trusted-client-IP half of the Abuse Gate. An empty IP (no socket peer
        the boundary could trust) is counted under one shared key rather than waved
        through -- an unattributable caller must not be an unlimited one."""
        return self.store.check_rate(
            f"ip:{client_ip or 'unknown'}", "request", self.limits.max_requests_per_ip_per_minute
        )

    def allow_conversation_creation(self, client_ip: str) -> bool:
        """Creation needs a ceiling of its own. A Conversation holds a resident slot
        for the full hour, three orders of magnitude longer than the rate window, so
        the general per-IP budget -- which refills every minute -- cannot stop one
        caller pinning every slot in the process."""
        return self.store.check_rate(
            f"ip:{client_ip or 'unknown'}", "create", self.limits.create_rate_per_minute
        )

    def _ensure_session_rate(self, capability_hash: str, kind: str, limit: int) -> None:
        """Per-session Rate. Evaluated before any Domain call, so a refused Poll,
        Retry or Cancel changes no state at all."""
        if not self.store.check_rate(capability_hash, kind, limit):
            raise CapacityExceeded("rate_limited")

    def create_conversation(self) -> tuple[Conversation, str]:
        if self._limits is None:
            # Only a redeploy clears this, so it must not be advertised as something
            # worth waiting for. Every ceiling is zero on this path anyway; naming
            # the real reason is what lets an operator act on it.
            raise CapacityExceeded("deployment_unconfigured", retryable=False)
        created_at = datetime.now(UTC)
        conversation = Conversation(uuid4(), created_at, created_at + CONVERSATION_TTL)
        capability = token_urlsafe(32)
        # The Resident Conversation ceiling lives in `create`: counting and refusing
        # under one lock is the only way two simultaneous creations cannot both fit
        # into the last slot.
        self.store.create(conversation, sha256(capability.encode()).hexdigest())
        return conversation, capability

    def submit_question(
        self,
        conversation_id: UUID,
        capability: str,
        idempotency_key: str,
        content: str,
    ) -> RunProjectionV1:
        normalized, digest = _question_digest(content)
        self._ensure_within_budget(normalized)
        # A cancelled Run frees active_run_id the instant it commits, but its
        # generation task is still draining the Provider. Starting the next one now
        # would put two Provider calls on a single-worker deployment at once.
        if not self._await_previous_generation(conversation_id):
            raise ActiveRunConflict
        capability_hash = sha256(capability.encode()).hexdigest()
        now = datetime.now(UTC)
        # Authorization and the Idempotency receipt come first, and only then is any
        # budget spent -- neither a rate key nor a Run slot. Ahead of them, a stale,
        # foreign or guessed Capability would mint a rate-limiter key per request
        # and occupy a Run slot before being refused, and a duplicate key would be
        # answered 503 or 429 exactly when the ceilings are full and the client is
        # most likely to be retrying. A replay must always replay.
        replay = self.store.replay_question(
            conversation_id, capability_hash, idempotency_key, digest, now
        )
        if replay is not None:
            return replay
        # Charged after `_await_previous_generation`, exactly as retry_run does it:
        # a command that is refused `run_already_active` must not burn budget.
        self._ensure_session_rate(capability_hash, "submit", self.limits.submit_rate_per_minute)
        deadline_at, deadline_monotonic = _deadline(now)
        # Minted here, at Run acceptance, by the one component that owns it. Handed
        # to the Domain so the Run carries it to its Terminal Commit, and kept on the
        # generation so every Telemetry Record for this Run reports the same value.
        correlation_id = uuid4()
        # Reserved before accept_question, so a capacity refusal creates no Run, no
        # Message and no Provider work. Released by `_generate`'s own finally once
        # the generation owns it -- and here, on every path that never gets there.
        lease = self.store.reserve_run(conversation_id)
        try:
            accepted = self.store.accept_question(
                conversation_id,
                capability_hash,
                idempotency_key,
                digest,
                normalized,
                now,
                deadline_at,
                deadline_monotonic,
                correlation_id,
            )
            if accepted.replayed:
                self.store.release_run(lease)
                return accepted.run
        except BaseException:
            self.store.release_run(lease)
            raise
        return self._start_generation(
            accepted.run,
            normalized,
            deadline_monotonic,
            None,
            capability_hash,
            correlation_id,
            lease,
        )

    def replay_retry(
        self,
        conversation_id: UUID,
        capability: str,
        idempotency_key: str,
        command: RetryCommand,
    ) -> RunProjectionV1 | None:
        """`replay_question`'s sibling. A duplicate Retry must answer with the Run
        the first one created whether or not the Provider is healthy right now, so
        the boundary consults this before it consults readiness."""
        return self.store.replay_question(
            conversation_id,
            sha256(capability.encode()).hexdigest(),
            idempotency_key,
            _retry_digest(command.retry_of_run_id),
            datetime.now(UTC),
        )

    def retry_run(
        self,
        conversation_id: UUID,
        capability: str,
        idempotency_key: str,
        command: RetryCommand,
    ) -> RunProjectionV1:
        """`submit_question`'s sibling for a Run that already failed. No content, no
        new User Message, no fresh context selection: the Snapshot the target Run
        actually sent is what the new Run re-sends, so `context_digest` and the fact
        that History was truncated are the same as the first time.

        Every check that can refuse runs *before* `accept_retry`, exactly as
        `submit_question` gates before `accept_question`: a rejected Command must
        leave no Run, no Message and no consumed Attempt behind."""
        digest = _retry_digest(command.retry_of_run_id)
        capability_hash = sha256(capability.encode()).hexdigest()
        if not self._await_previous_generation(conversation_id):
            raise ActiveRunConflict
        now = datetime.now(UTC)
        # The receipt answers before anything is validated: a duplicate Command must
        # return the Run the first one created even when the lineage has moved on and
        # the target is no longer its Tail.
        replay = self.store.replay_question(
            conversation_id, capability_hash, idempotency_key, digest, now
        )
        if replay is not None:
            return replay
        # After the replay, like the submit path: a duplicate Command answers with
        # the Run the first one created rather than a 429, and only an authorized
        # session ever mints a rate-limiter key.
        self._ensure_session_rate(capability_hash, "retry", self.limits.retry_rate_per_minute)
        (
            request,
            dropped_turn_count,
            content,
            target_binding_digest,
            lineage_attempts,
        ) = self.store.retry_target(conversation_id, capability_hash, command.retry_of_run_id, now)
        if lineage_attempts >= self._max_attempts_per_lineage:
            # BEFORE the Binding gate below, because the two recoveries differ and
            # only one of them is available: an exhausted lineage cannot be re-asked
            # at all, so telling that user to resend the question -- which is what
            # `provider_profile_changed` says -- sends them into a second refusal.
            # `accept_retry` remains the authority; this only orders the answer.
            raise RetryExhausted
        if self._provider_profile_changed(request, target_binding_digest):
            # A rebind between the two Runs. Refused as its own fact rather than as
            # `retry_not_allowed`: the target WAS retryable, and the same question is
            # still askable, so the client must not fail this Conversation closed.
            raise ProviderProfileChanged
        if request is not None and not self._snapshot_still_sendable(request):
            # What is left after the Binding gate: a Snapshot whose own integrity
            # check fails. Every other way this returns False is a Provider change,
            # and the gate above already named that. A Snapshot that no longer
            # re-serializes to its own digests must never be re-sent -- re-selecting
            # History would answer a different question under the same
            # input_message_id -- so it is refused here, fail-closed.
            raise RetryNotRetryable
        if request is None:
            # No Snapshot to re-send, so this Retry re-selects History -- and must
            # pass the same System+Current Gate a typed question does.
            self._ensure_within_budget(content)
        deadline_at, deadline_monotonic = _deadline(now)
        correlation_id = uuid4()
        lease = self.store.reserve_run(conversation_id)
        try:
            accepted = self.store.accept_retry(
                conversation_id,
                capability_hash,
                idempotency_key,
                digest,
                command.retry_of_run_id,
                self._max_attempts_per_lineage,
                now,
                deadline_at,
                deadline_monotonic,
                correlation_id,
            )
            if accepted.replayed:
                self.store.release_run(lease)
                return accepted.run
        except BaseException:
            self.store.release_run(lease)
            raise
        snapshot = (
            None
            if accepted.prepared_request is None
            else (accepted.prepared_request, accepted.dropped_turn_count)
        )
        return self._start_generation(
            accepted.run,
            accepted.content,
            deadline_monotonic,
            snapshot,
            capability_hash,
            correlation_id,
            lease,
        )

    def _provider_profile_changed(
        self, request: PreparedModelRequestV1 | None, target_binding_digest: str | None
    ) -> bool:
        """Is the Provider bound now a different one from the Provider this Retry's
        target actually used?

        Two facts, because there are two records and they cover different ground.
        The Run's `binding_digest` is the whole ProviderBindingV1 -- it moves when the
        endpoint moves, which the Profile Digest alone does not. The Snapshot's
        `provider_profile_digest` covers a target that reached the Provider without
        a Binding Digest ever being recorded. `None` is not a mismatch: a Run that
        failed before it reached the Provider never had a Binding to differ from."""
        provider = self.provider
        try:
            if target_binding_digest is not None and target_binding_digest != provider.binding_digest:
                return True
            return (
                request is not None
                and request.provider_profile_digest != provider.provider_profile_digest
            )
        except Exception:
            # A Provider that cannot state its own Digest has not "changed" -- that is
            # a different fault, and naming it as a rebind would send the user to
            # re-ask a question this process cannot answer at all. With a Snapshot,
            # `_snapshot_still_sendable` refuses right after this; without one the
            # Retry proceeds and the Readiness Gate that runs before it (main._retry
            # checks `is_ready()`) is what keeps an unusable Provider out.
            return False

    def _snapshot_still_sendable(
        self, request: PreparedModelRequestV1, input_message_id: UUID | None = None
    ) -> bool:
        """Everything `_select_context_request` would have re-established, checked
        against the Provider bound *now*: the profile digest first (a mismatch makes
        count_input_tokens itself raise), then the budget, and -- when the caller
        knows which Run will send it -- AD-25's Current Input binding, the one thing
        that proves this Snapshot answers that Run's question."""
        provider = self.provider
        try:
            if input_message_id is not None and request.current_input_message_id != input_message_id:
                return False
            if request.provider_profile_digest != provider.provider_profile_digest:
                return False
            return provider.count_input_tokens(request) <= _effective_budget(provider)
        except Exception:
            return False

    def _start_generation(
        self,
        projection: RunProjectionV1,
        content: str,
        deadline_monotonic: float,
        snapshot: tuple[PreparedModelRequestV1, int] | None,
        capability_hash: str,
        correlation_id: UUID,
        lease: RunLease | None = None,
    ) -> RunProjectionV1:
        # Required, with no default: this is the ID the caller already minted and
        # handed to the Domain. A default here would be a second mint and a second
        # thing to get wrong.
        task = Thread(
            target=self._generate,
            args=(
                projection.conversation_id,
                projection.run_id,
                projection.input_message_id,
                content,
                deadline_monotonic,
                snapshot,
                lease,
            ),
            daemon=True,
            name=f"aidd-generation-{projection.run_id}",
        )
        try:
            with self._generation_lock:
                self._generation_tasks[projection.run_id] = _Generation(
                    projection.conversation_id, task, correlation_id
                )
                task.start()
        except Exception:
            with self._generation_lock:
                self._generation_tasks.pop(projection.run_id, None)
            if lease is not None:
                # The task that would have released it never started.
                self.store.release_run(lease)
            if self.store.commit_failed(
                projection.conversation_id,
                projection.run_id,
                _provider_failure("provider_unknown"),
                datetime.now(UTC),
            ):
                self._run_telemetry(projection.conversation_id, projection.run_id)
            return self.store.get_run(projection.run_id, capability_hash, datetime.now(UTC))
        return projection

    def replay_question(
        self,
        conversation_id: UUID,
        capability: str,
        idempotency_key: str,
        content: str,
    ) -> RunProjectionV1 | None:
        _, digest = _question_digest(content)
        return self.store.replay_question(
            conversation_id,
            sha256(capability.encode()).hexdigest(),
            idempotency_key,
            digest,
            datetime.now(UTC),
        )

    def get_run(self, run_id: UUID, capability: str, rate_limited: bool = True) -> RunProjectionV1:
        """`rate_limited=False` is for the boundary's own authorization reads -- the
        Cancel path looks the Run up before it cancels, and charging one user action
        against the Poll ceiling twice would refuse Cancels that never polled.

        The read comes first and the charge second, deliberately: the read is what
        authorizes, and a rate key minted from an unvalidated cookie would let an
        attacker fill the rate table with keys nobody owns. A read changes no Domain
        state, so charging after it still leaves a refused Poll with nothing done."""
        projection = self._observed(run_id, capability, self.store.get_run)[0]
        if rate_limited:
            self._ensure_session_rate(
                sha256(capability.encode()).hexdigest(), "poll", self.limits.poll_rate_per_minute
            )
        return projection

    def get_run_snapshot(
        self, run_id: UUID, capability: str
    ) -> tuple[RunProjectionV1, tuple[RunEventV1, ...]]:
        return self._observed(run_id, capability, self.store.get_run_snapshot)

    def get_run_slice(
        self, run_id: UUID, capability: str, cursor: int
    ) -> tuple[RunProjectionV1, tuple[RunEventV1, ...]]:
        return self._observed(run_id, capability, self.store.get_run_slice, cursor)

    def _observed(
        self, run_id: UUID, capability: str, read, *args
    ) -> tuple[RunProjectionV1, tuple[RunEventV1, ...]]:
        """Every read of a Run is also a Deadline enforcement point. The read is
        repeated whenever it saw a non-terminal Run -- not only when this call's own
        commit won -- because the alarm or another observer may have ended it in the
        same window, and a stale `running` projection would leave the client waiting
        on a Run that is already over."""
        capability_hash = sha256(capability.encode()).hexdigest()
        now = datetime.now(UTC)
        result = read(run_id, capability_hash, *args, now)
        projection = result[0] if isinstance(result, tuple) else result
        if projection.state in {"queued", "running"}:
            self._commit_timeout(projection.conversation_id, run_id, due_only=True)
            result = read(run_id, capability_hash, *args, now)
        return result if isinstance(result, tuple) else (result, ())

    def _commit_timeout(
        self, conversation_id: UUID, run_id: UUID, *, due_only: bool = False
    ) -> bool:
        """Structured Cancellation first, `commit_timeout` to confirm it. Unlike
        `cancel_run`, the reverse would be wrong here: the Provider must be told to
        stop before the Deadline is recorded as met, and the `timed_out` flag set
        with the cancel is what makes the generation task -- whose call is about to
        raise *because* of it -- commit `timeout` rather than a Provider failure. A
        late callback loses the CAS either way.

        `due_only` is asked first and separately, because a caller walking past an
        arbitrary Run must not cancel one whose Deadline has not passed."""
        if due_only and not self.store.run_past_deadline(
            conversation_id, run_id, time.monotonic()
        ):
            return False
        self._request_provider_cancel(run_id, timed_out=True)
        committed = self.store.commit_timeout(
            conversation_id,
            run_id,
            _provider_failure("provider_timeout"),
            datetime.now(UTC),
        )
        if committed:
            self._run_telemetry(conversation_id, run_id)
        if not committed:
            # The Run reached a terminal of its own inside the cancel-then-commit
            # window. Leaving `timed_out` set would re-route that Run's later
            # `fail()` to a `timeout` it never had.
            with self._generation_lock:
                generation = self._generation_tasks.get(run_id)
                if generation is not None:
                    generation.timed_out = False
        return committed

    def cancel_run(self, run_id: UUID, capability: str) -> CancelResultV1:
        """The order is the contract, not an implementation detail. The Terminal
        Commit lands first; only then is the Provider Cancel requested. Reversed,
        the Adapter's CancelledError would race back as a `provider_cancelled`
        *failure* commit and the user would see an error instead of a stop.

        One clock reading for both store calls: a TTL boundary crossed between them
        would otherwise turn an accepted Cancel into a 410 whose Provider was never
        told to stop."""
        capability_hash = sha256(capability.encode()).hexdigest()
        now = datetime.now(UTC)
        # Third observation point, and the Deadline binds here too: a Run already
        # past it must not become `cancelled` (no terminal_error) just because a
        # Cancel happened to look at it before a poll did. This read also authorizes,
        # which is why the Cancel rate is charged after it and not before -- an
        # unvalidated Capability must not be able to mint a rate-limiter key.
        conversation_id = self.store.get_run(run_id, capability_hash, now).conversation_id
        self._ensure_session_rate(capability_hash, "cancel", self.limits.cancel_rate_per_minute)
        self._commit_timeout(conversation_id, run_id, due_only=True)
        accepted = self.store.commit_cancelled(run_id, capability_hash, now)
        if accepted:
            self._run_telemetry(conversation_id, run_id)
            self._request_provider_cancel(run_id)
        # commit_cancelled refuses exactly the Runs that are already terminal, so the
        # outcome and the Run shipped with it cannot disagree -- `already_terminal`
        # beside a still-running Run would leave a client waiting forever.
        run = self.store.get_run(run_id, capability_hash, now)
        return CancelResultV1(
            cancel_outcome="accepted" if accepted else "already_terminal", run=run
        )

    def record_telemetry(
        self,
        correlation_id: UUID,
        stage: RunStage,
        *,
        duration_ms: int | None = None,
        terminal_state: TerminalRunState | None = None,
        error_class: str | None = None,
    ) -> RunTelemetryV1:
        """The one place a Telemetry Record is built, for every path. Everything it
        can say is a field of the closed Record, and the log line names those five
        fields and nothing else -- there is no message argument to smuggle anything
        into.

        `error_class` is the ONLY parameter typed wider than the Record holds, and it
        is the only one whose values come from outside this module (a Capacity Code
        raised by the Store, a ProviderFailureKind). An unknown one is flattened
        rather than raised: this runs inside request paths, and a refusal that
        crashed the refusal would be worse than one that is merely less specific.
        `stage` and `terminal_state` are supplied only by this module and by the
        Run's own fields, so they are typed exactly as the Record stores them and a
        bad value is a bug to be raised, not a value to be swallowed."""
        error_class = _telemetry_error_class(error_class)
        record = RunTelemetryV1(
            correlation_id=correlation_id,
            stage=stage,
            duration_ms=duration_ms,
            terminal_state=terminal_state,
            error_class=error_class,
        )
        with self._telemetry_lock:
            self._telemetry.append(record)
        _log.info(
            "run_telemetry correlation_id=%s stage=%s duration_ms=%s terminal_state=%s error_class=%s",
            record.correlation_id,
            record.stage,
            "-" if record.duration_ms is None else record.duration_ms,
            record.terminal_state or "-",
            record.error_class or "-",
        )
        return record

    def telemetry_snapshot(self) -> tuple[RunTelemetryV1, ...]:
        """The read path. A tuple, taken under the lock: the deque is appended from
        generation threads, the Sweeper thread and request threads, and iterating a
        deque while another thread appends to it raises RuntimeError."""
        with self._telemetry_lock:
            return tuple(self._telemetry)

    def _record_observation(
        self, observed: RunObservationV1, error_class: str | None = None
    ) -> RunTelemetryV1:
        """A Record built entirely from what the Run itself says. Stage, Terminal
        State, Correlation ID and Duration are read, never re-derived: a caller that
        reimplements the Domain's terminal rule reports `completed` for a Run the
        Domain just committed as `failed`."""
        terminal_state = observed.state if observed.state in TERMINAL_RUN_STATES else None
        return self.record_telemetry(
            observed.correlation_id,
            observed.stage,
            # Acceptance-to-terminal-commit, exactly as the frozen Design Note
            # defines it -- so only for a Run that actually reached a terminal. A
            # Purge or a failed Cancel reports a Run still in flight, and there is
            # no terminal commit to measure to.
            duration_ms=observed.duration_ms if terminal_state is not None else None,
            terminal_state=terminal_state,
            error_class=error_class or observed.error_kind,
        )

    def _run_telemetry(
        self,
        conversation_id: UUID,
        run_id: UUID,
        error_class: str | None = None,
        fallback_correlation_id: UUID | None = None,
    ) -> None:
        """Telemetry for a Run, read back from the Store after the commit that just
        ran. Nothing is looked up in `_generation_tasks`: a terminal committed by a
        poll or an SSE read after the generation task ended would otherwise be
        unobserved, which is exactly the terminal an operator most wants to see.

        None means the Conversation is gone -- Purged. The purge observer has already
        reported the Run's terminal, but a fact discovered AFTER the Purge has nothing
        left to read: the Store cancels the Provider only once the Conversation is
        emptied, so a cancel that fails there would vanish exactly when it matters
        most (that Run is about to burn its whole Deadline unattended).
        `fallback_correlation_id` is the value the Application already holds for that
        Run -- the same one the Domain has -- so the Record still correlates."""
        observed = self.store.observe_run(conversation_id, run_id)
        if observed is not None:
            self._record_observation(observed, error_class)
        elif fallback_correlation_id is not None:
            # `streaming`, not `terminal`: a cancel is only attempted on a Run that is
            # still in flight, which is precisely why this one matters. No duration --
            # there is no terminal commit to measure to.
            self.record_telemetry(
                fallback_correlation_id, "streaming", error_class=error_class
            )

    def _observe_purge(self, observed: "RunObservationV1 | None") -> None:
        """The Store Purged a Conversation, on its Sweeper thread or on a request's.
        A Conversation with a live Run reports that Run (non-terminal, so no Terminal
        State); one without gets its own ID, because there is no Run to borrow."""
        if observed is None:
            self.record_telemetry(uuid4(), "queued", error_class="conversation_expired")
        else:
            self._record_observation(observed, "conversation_expired")

    def record_capacity_refusal(self, reason: str) -> None:
        """A ceiling refused a request before any Run existed. There is no Run to
        borrow a Correlation ID or a Stage from, so this gets its own ID and the only
        Stage that describes "nothing started": `queued`.

        Coalesced per Code per window, unlike a Run terminal: a ceiling under
        sustained load refuses once per request, and 512 identical Records would
        evict every Run terminal in the deque within seconds.

        Keyed on the code that will actually be RECORDED, not on the raw reason:
        several unknown reasons all record as `unclassified`, and keying on the raw
        value would give each its own window while they were indistinguishable in
        the deque. That also bounds the table -- the key set is the closed
        vocabulary, so there is nothing to prune."""
        code = _telemetry_error_class(reason)
        now = time.monotonic()
        with self._telemetry_lock:
            last = self._capacity_reported.get(code)
            if last is not None and 0 <= now - last < CAPACITY_TELEMETRY_WINDOW_SECONDS:
                return
            self._capacity_reported[code] = now
        self.record_telemetry(uuid4(), "queued", error_class=code)

    def _request_provider_cancel(self, run_id: UUID, timed_out: bool = False) -> None:
        """Best-effort, and safe to enter concurrently: a poll, an SSE slice read,
        `cancel_run` and the task's own Deadline alarm can all arrive at once, and
        the Deadline path deliberately arrives *before* winning any CAS. Only the
        one-shot `ProviderCallHandle` makes that harmless -- at most one cancellation
        ever reaches the Provider however many callers ask. Nothing the Provider
        raises afterwards may disturb the Run."""
        with self._generation_lock:
            generation = self._generation_tasks.get(run_id)
            if generation is not None:
                generation.cancelled = True
                generation.timed_out = generation.timed_out or timed_out
        handle = None if generation is None else generation.handle
        if handle is None:
            return
        try:
            handle.cancel()
        except Exception:
            # Swallowed on purpose -- a Provider that cannot be told to stop must not
            # take the caller down -- but no longer silent: this is the one signal
            # that a Run is going to run to its Deadline rather than stop.
            #
            # Suppressed in turn, because this replaced a bare `pass`: `cancel_run`
            # and `_commit_timeout` both call in here and neither suppresses, so an
            # observation that raised would turn a best-effort cancel into a 500.
            with suppress(Exception):
                self._run_telemetry(
                    generation.conversation_id,
                    run_id,
                    error_class="provider_cancel_failed",
                    fallback_correlation_id=generation.correlation_id,
                )

    def _timed_out(self, run_id: UUID) -> bool:
        with self._generation_lock:
            generation = self._generation_tasks.get(run_id)
            return generation is not None and generation.timed_out

    def authorize_conversation(self, conversation_id: UUID, capability: str) -> None:
        self.store.authorize(
            conversation_id,
            sha256(capability.encode()).hexdigest(),
            datetime.now(UTC),
        )

    def _await_previous_generation(self, conversation_id: UUID, timeout: float = 2.0) -> bool:
        """Bounded wait for a *cancelled* generation task of this Conversation to be
        gone. Only cancelled ones: any other non-terminal Run is still the active
        one and accept_question refuses it on its own, while a Cancel frees
        active_run_id the instant it commits and leaves its task draining.

        A Deadline expiry is deliberately not waited on. Its Provider call has
        already had the whole 120 s and been told to stop; a call that ignores its
        handle would otherwise wedge the Conversation for every later question and
        Retry. ponytail: that thread still runs to completion -- bounding the thread
        itself needs the Story 1.8 capacity contract."""
        deadline = time.monotonic() + timeout
        while True:
            with self._generation_lock:
                threads = [
                    item.thread
                    for item in self._generation_tasks.values()
                    if item.conversation_id == conversation_id
                    and item.cancelled
                    and not item.timed_out
                ]
            if not threads:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            threads[0].join(remaining)

    def wait_for_generations(self, timeout: float | None = 2.0) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._generation_lock:
                tasks = tuple(item.thread for item in self._generation_tasks.values())
            if not tasks:
                return
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return
            tasks[0].join(remaining)

    def expired_stream_tail(
        self, run_id: UUID, capability: str, cursor: int
    ) -> "tuple[RunEventV1, ...] | None":
        return self.store.expired_stream_tail(
            run_id, sha256(capability.encode()).hexdigest(), cursor
        )

    def reserve_stream(self) -> bool:
        return self.store.reserve_stream()

    def release_stream(self) -> None:
        self.store.release_stream()

    def run_deadline_monotonic(self, run_id: UUID, capability: str) -> float | None:
        """This Run's own monotonic Deadline, for a boundary that has to bound how
        long it will hold a connection open for it. Monotonic, like every other
        Deadline in this process: a wall clock stepped by NTP would otherwise close
        every live stream at once, or let a stuck one hold its slot indefinitely."""
        return self.store.run_deadline_monotonic(
            run_id, sha256(capability.encode()).hexdigest()
        )

    def expire_due_conversations(self, now: datetime | None = None) -> int:
        """The Sweeper, driven on demand. The Store runs it on a timer as well; both
        call the same operation with the same Absolute Expiry, so which one gets
        there first changes nothing but when the memory comes back."""
        # No Record here: the Store reports every Conversation it Purges through
        # `set_purge_observer`, whether this call, a request, or its own timer thread
        # drove it -- and the timer is the path with no request behind it to notice.
        return self.store.sweep(now or datetime.now(UTC))

    def shutdown(self) -> None:
        # The Sweeper first: it must not start a Purge -- and a Provider cancel --
        # while the generations it would disturb are being waited on.
        self.store.close()
        self.wait_for_generations(None)

    def _ensure_within_budget(self, current_content: str) -> None:
        """System+Current Gate: computed before accept_question, so an oversized
        question never creates a Run or Message and never reaches the Provider.
        A digest mismatch here is the same fail-closed signal _generate() reacts
        to, just surfaced synchronously instead of inside a Run. Any other
        exception from the Provider's own count_input_tokens()/provider_profile_digest
        must not escape as a bare, envelope-free 500 either."""
        provider = self.provider
        current = PreparedMessageV1(uuid4(), "user", current_content)
        try:
            request = prepare_model_request(
                (current,), provider_profile_digest=provider.provider_profile_digest
            )
            count = provider.count_input_tokens(request)
        except ContextIntegrityError:
            self._integrity_poisoned = True
            raise
        except ContextTooLarge:
            raise
        except Exception as exc:
            raise ContextIntegrityError from exc
        if count > _effective_budget(provider):
            raise ContextTooLarge

    def _select_context_request(
        self,
        conversation_id: UUID,
        input_message_id: UUID,
        current_content: str,
        provider: ModelProviderPort,
    ) -> tuple[PreparedModelRequestV1, int] | None:
        """Newest-first greedy budget fit: keep the longest contiguous run of the
        most recent completed Turns that fits, drop the rest -- always a whole,
        oldest-first Turn at a time -- then restore chronological order. Returns
        None if the Conversation is gone or expired by the time History is read
        (the caller must fail the Run, not silently send an empty History)."""
        profile_digest = provider.provider_profile_digest
        current = PreparedMessageV1(input_message_id, "user", current_content)
        completed_turns = self.store.get_context_snapshot(conversation_id, datetime.now(UTC))
        if completed_turns is None:
            return None

        def build(turns: tuple[tuple[PreparedMessageV1, PreparedMessageV1], ...]) -> PreparedModelRequestV1:
            messages = tuple(message for pair in turns for message in pair) + (current,)
            return prepare_model_request(messages, provider_profile_digest=profile_digest)

        # ponytail: O(n^2) canonical-bytes rebuild across candidate turns -- fine for a
        # single 1-hour conversation's history; batch differently if turn counts grow large.
        accepted_newest_first: list[tuple[PreparedMessageV1, PreparedMessageV1]] = []
        for turn in reversed(completed_turns):
            candidate_newest_first = [*accepted_newest_first, turn]
            candidate = tuple(reversed(candidate_newest_first))
            if provider.count_input_tokens(build(candidate)) > _effective_budget(provider):
                break
            accepted_newest_first = candidate_newest_first
        selected = tuple(reversed(accepted_newest_first))
        dropped_turn_count = len(completed_turns) - len(selected)
        final_request = build(selected)
        # The loop only ever verifies *candidates that add a Turn*; a Provider
        # window that shrank (or a rebind) between the submit Gate and here means
        # even the zero-history baseline could now be over budget -- verify what
        # is actually about to be returned, not just what the loop happened to try.
        if provider.count_input_tokens(final_request) > _effective_budget(provider):
            raise ContextTooLarge
        return final_request, dropped_turn_count

    def _publish_call_handle(self, run_id: UUID, provider: object) -> "SupportsCancel | None":
        """Minted here, on the generation thread, because that is the thread that
        will make the Provider call, and returned so the call itself can take it.
        None means this Run cannot be stopped -- the Provider declares no
        cancellability, or declares it and fails to deliver -- and the caller
        refuses to start it either way. readiness makes that unreachable in
        production; this is the boundary that makes it true regardless."""
        if not isinstance(provider, CancellableModelProviderPort):
            return None
        try:
            handle = provider.new_call_handle()
        except Exception:
            return None
        with self._generation_lock:
            generation = self._generation_tasks.get(run_id)
            if generation is not None:
                generation.handle = handle
        return handle

    def _generate(
        self,
        conversation_id: UUID,
        run_id: UUID,
        input_message_id: UUID,
        content: str,
        deadline_monotonic: float,
        snapshot: tuple[PreparedModelRequestV1, int] | None = None,
        lease: RunLease | None = None,
    ) -> None:
        # The Provider is stopped a Close Grace *before* the Deadline, so the stream
        # close still lands inside the 120 s instead of claiming time of its own.
        # A Grace longer than what is left leaves no room for the call either, and
        # none is granted -- the Deadline is never extended to make one fit.
        # close_grace_ms, not close_grace_seconds: the former is what the Port
        # declares and what readiness validates. A provider that answers with
        # something non-numeric gets no Grace rather than taking the thread down.
        try:
            grace_ms = float(self.provider.close_grace_ms)
        except Exception:
            grace_ms = 0.0
        # Clamped, not subtracted: the Grace may only take what is left of the
        # Deadline. With less than a Grace remaining the call still happens and is
        # cancelled at once -- it is never refused to make room for a close.
        cancel_at = max(time.monotonic(), deadline_monotonic - max(0.0, grace_ms) / 1_000)

        def timed_out() -> bool:
            # One predicate for every classification in this task, so the same
            # moment cannot land as `timeout` down one path and a Provider failure
            # down another depending on whether an alarm happened to be armed.
            return self._timed_out(run_id) or time.monotonic() >= cancel_at

        def commit_timeout() -> bool:
            return self._commit_timeout(conversation_id, run_id)

        try:
            try:
                provider = self.provider
                # Published BEFORE mark_running. A Cancel landing in that window wins
                # the terminal CAS and mark_running then refuses, so no Provider call
                # starts for an already-cancelled Run; published after it, that same
                # Cancel would find no handle and the call would run uncancelled.
                handle = self._publish_call_handle(run_id, provider)
                started = self.store.mark_running(
                    conversation_id,
                    run_id,
                    datetime.now(UTC),
                    time.monotonic(),
                    provider.binding_digest,
                )
            except Exception:
                # Nothing above owns `fail()` yet, and an exception here would
                # otherwise leave the Run `queued` with no terminal and no stream.end.
                if self.store.commit_failed(
                    conversation_id,
                    run_id,
                    _provider_failure("provider_unknown"),
                    datetime.now(UTC),
                ):
                    self._run_telemetry(conversation_id, run_id)
                return
            if not started:
                # Either a Cancel won the terminal CAS (this commit then loses it and
                # changes nothing), or the Run was already past its Deadline before it
                # started -- in which case nothing else would ever end it.
                self._commit_timeout(conversation_id, run_id, due_only=True)
                return
            if lease is not None:
                # Out of the queue: the Queued ceiling frees up here, the Active one
                # only when this task ends. Inside the failure handler, because a
                # raise here would otherwise leave the Run `running` for ever with no
                # terminal, no run.error and no stream.end.
                try:
                    self.store.start_run(lease)
                except Exception:
                    if self.store.commit_failed(
                        conversation_id,
                        run_id,
                        _provider_failure("provider_unknown"),
                        datetime.now(UTC),
                    ):
                        self._run_telemetry(conversation_id, run_id)
                    return
            # The Deadline's own alarm, armed by the task that owns the call. This is
            # what makes the Deadline hold with no client attached: without it a Run
            # nobody polls could run past 120 s and block shutdown.
            self._arm_deadline(
                run_id,
                cancel_at - time.monotonic(),
                deadline_monotonic - time.monotonic(),
            )
            streamed = False

            def fail(kind: str) -> bool:
                # A Run that ran out of Deadline ends as `timeout` whatever the
                # Provider said on its way out -- including the provider_cancelled
                # our own Cancellation produced.
                if timed_out():
                    return commit_timeout()
                commit = self.store.commit_stream_failed if streamed else self.store.commit_failed
                committed = commit(
                    conversation_id,
                    run_id,
                    _provider_failure(kind),
                    datetime.now(UTC),
                )
                if committed:
                    self._run_telemetry(conversation_id, run_id)
                return committed

            if handle is None:
                # Readiness promised a Provider whose calls can be stopped. Starting
                # one we could never stop is worse than not starting it.
                fail("provider_unknown")
                return

            try:
                if snapshot is not None:
                    # Retry: re-send exactly what the original Run sent. Re-selecting
                    # History here would answer a different question under the same
                    # input_message_id and a different context_digest.
                    request, dropped_turn_count = snapshot
                else:
                    selection = self._select_context_request(
                        conversation_id, input_message_id, content, provider
                    )
                    if selection is None:
                        # Conversation gone or expired by the time History was read.
                        fail("provider_unknown")
                        return
                    request, dropped_turn_count = selection
                    if not self.store.record_prepared_request(
                        conversation_id, run_id, request, dropped_turn_count
                    ):
                        # This Run is no longer the active one. Proceeding would send
                        # a request no Retry could ever reproduce.
                        fail("provider_unknown")
                        return
                if time.monotonic() >= deadline_monotonic:
                    # Preparation alone consumed the Deadline; no Provider call starts.
                    commit_timeout()
                    return
                if dropped_turn_count and not self.store.commit_context_truncated(
                    conversation_id, run_id, dropped_turn_count, datetime.now(UTC)
                ):
                    # Conversation expired or the Run stopped being the active one
                    # between mark_running and here: the user must not silently
                    # receive a truncated answer with no record it happened.
                    fail("provider_unknown")
                    return
                streamed = isinstance(provider, StreamingModelProviderPort)
                if streamed:
                    def commit_delta(delta: str) -> None:
                        if timed_out():
                            # Deadline reached mid-stream: start Structured
                            # Cancellation, then leave. `fail` turns whatever the
                            # Provider raises next into the `timeout` terminal.
                            self._request_provider_cancel(run_id, timed_out=True)
                            raise _ProviderOutputRejected("provider_timeout")
                        if not isinstance(delta, str) or not delta or _contains_unsafe_text(delta):
                            raise _ProviderOutputRejected("provider_invalid_response")
                        if not self.store.commit_delta(
                            conversation_id, run_id, delta, datetime.now(UTC)
                        ):
                            raise _ProviderOutputRejected("provider_invalid_response")

                    output = provider.stream(request, commit_delta, handle)
                else:
                    output = provider.complete(request, handle)
                if not isinstance(output, str):
                    fail("provider_non_text")
                    return
                # Streamed output is committed raw: commit_stream_completed compares it
                # byte-for-byte against the concatenated deltas, so normalizing here
                # would turn every CRLF or non-NFC answer into a terminal failure.
                if not streamed:
                    output = _normalize_text(output)
                if _contains_unsafe_text(output):
                    fail("provider_invalid_response")
                    return
                if not has_visible_text(output):
                    fail("provider_empty")
                    return
                if timed_out():
                    # The answer arrived, but not inside the Deadline. Partial or
                    # whole, it does not become a completed Message.
                    commit_timeout()
                    return
                if streamed:
                    if not self.store.commit_stream_completed(
                        conversation_id,
                        run_id,
                        output,
                        _provider_failure("provider_invalid_response"),
                        datetime.now(UTC),
                    ):
                        fail("provider_invalid_response")
                    else:
                        self._run_telemetry(conversation_id, run_id)
                else:
                    if not self.store.commit_completed(
                        conversation_id, run_id, output, datetime.now(UTC)
                    ):
                        fail("provider_invalid_response")
                    else:
                        self._run_telemetry(conversation_id, run_id)
            except OutputLimitExceeded:
                # A deployment ceiling, not a Provider fault. Named explicitly rather
                # than left to the generic handler's getattr(exc, "kind") lookup,
                # which is one rename away from silently becoming provider_unknown.
                fail("capacity_exceeded")
            except asyncio.CancelledError:
                # Only a Provider that raises CancelledError raw reaches here; the
                # Direct adapter converts it to InvalidProviderOutput first and lands
                # on the generic handler below. Kept because CancelledError is a
                # BaseException -- without it the Run would never reach a terminal.
                fail("provider_cancelled")
            except Exception as exc:
                if isinstance(exc, ContextIntegrityError):
                    self._integrity_poisoned = True
                kind = getattr(exc, "kind", None)
                if not isinstance(kind, str) or kind not in _PROVIDER_FAILURES:
                    if isinstance(exc, (TimeoutError,)):
                        kind = "provider_timeout"
                    elif isinstance(exc, (ConnectionError, OSError)):
                        kind = "provider_transport"
                    else:
                        kind = "provider_unknown"
                fail(kind)
        finally:
            with self._generation_lock:
                generation = self._generation_tasks.pop(run_id, None)
            for timer in () if generation is None else generation.timers:
                timer.cancel()
            # The Run slot goes back the moment this task is over, however it ended.
            # Reserve without release and the ceiling only ever falls, until the
            # service refuses everyone.
            if lease is not None:
                self.store.release_run(lease)

    def _arm_deadline(self, run_id: UUID, cancel_in: float, commit_in: float) -> None:
        """Two one-shot alarms per Run, both retired by `_generate`'s own `finally`.
        Not a watchdog sweeping every Run: they belong to the call they bound.

        They are separate because the frozen contract separates them. Cancellation
        starts a Close Grace before the Deadline so the stream's close lands inside
        the 120 s; the `timeout` terminal is only confirmed once the Deadline itself
        has passed, which is also when `run_past_deadline` starts agreeing."""
        timers = [
            Timer(max(0.0, cancel_in), self._deadline_cancel, args=(run_id,)),
            Timer(max(0.0, commit_in), self._deadline_commit, args=(run_id,)),
        ]
        for timer in timers:
            timer.daemon = True
        with self._generation_lock:
            generation = self._generation_tasks.get(run_id)
            if generation is None:
                return
            generation.timers = timers
        for timer in timers:
            timer.start()

    def _deadline_cancel(self, run_id: UUID) -> None:
        self._request_provider_cancel(run_id, timed_out=True)

    def _deadline_commit(self, run_id: UUID) -> None:
        with self._generation_lock:
            generation = self._generation_tasks.get(run_id)
        if generation is not None:
            self._commit_timeout(generation.conversation_id, run_id, due_only=True)

    def is_ready(self) -> bool:
        """Readiness, then exactly one Record for it. The Record is written HERE and
        nowhere else, because "every Gate passed" and "this process is Ready" are
        different facts: the Gates are checked before the Provider probe runs, so
        reporting inside them announced `ready` for a process that then answered 503,
        and the probe's own refusal flipped it straight back -- two Records per poll
        for ever, with the dedupe defeated exactly when it mattered."""
        ready, reason = self._probe_readiness()
        with self._telemetry_lock:
            changed = reason != self._last_readiness_reason
            if changed:
                self._last_readiness_reason = reason
        if changed:
            # Outside every lock, like the Store's purge observer: an observer that
            # blocks must not hold the lock every other readiness check needs.
            # `error_class=None` when Ready -- health is not an Error Class.
            self.record_telemetry(uuid4(), "queued", error_class=reason)
        return ready

    def _probe_readiness(self) -> "tuple[bool, str | None]":
        """(ready, refusal Code). Both are derived under the SAME acquisition of
        `_readiness_lock` that produced the answer: computing the reason in a second
        acquisition lets a Gate change in between and yields a Record that disagrees
        with the answer this call returned."""
        with self._readiness_lock:
            refusal = self._readiness_refusal_locked()
            if not self._bootstrap_is_valid_locked():
                return False, refusal or "provider_probe_failed"
            now = time.monotonic()
            if self._cached_success_locked(now):
                return True, None
            flight = self._probe_inflight
            if flight is None:
                flight = _ProbeFlight(
                    self._provider_binding,
                    self._bootstrap_binding,
                    _provider_profile_digest(self._provider_binding),
                    now + self.PROBE_TIMEOUT_SECONDS,
                    Event(),
                )
                self._probe_inflight = flight
                self._clear_success_cache_locked()
                try:
                    Thread(
                        target=self._run_probe,
                        args=(flight,),
                        daemon=True,
                        name="aidd-provider-probe",
                    ).start()
                except Exception:
                    self._probe_inflight = None
                    return False, "provider_probe_failed"

        if not flight.event.wait(max(0.0, flight.deadline - time.monotonic())):
            return False, "provider_probe_failed"
        with self._readiness_lock:
            ready = flight.result and self._cached_success_locked(time.monotonic())
            if ready:
                return True, None
            return False, self._readiness_refusal_locked() or "provider_probe_failed"

    def _run_probe(self, flight: _ProbeFlight) -> None:
        result: object = False
        try:
            probe = getattr(flight.provider, "probe", None)
            if callable(probe):
                result = probe()
        except (Exception, asyncio.CancelledError):
            result = False

        with self._readiness_lock:
            now = time.monotonic()
            valid_binding = (
                self._probe_inflight is flight
                and flight.provider is self._provider_binding
                and flight.binding is self._bootstrap_binding
            )
            flight.result = (
                valid_binding
                and now < flight.deadline
                and type(result) is bool
                and result is True
                and self._bootstrap_is_valid_locked()
                # The Cache Key, re-read: a Provider whose Profile changed while its
                # own probe was in flight has not been probed at all.
                and flight.digest is not None
                and flight.digest == _provider_profile_digest(self._provider_binding)
            )
            if not valid_binding:
                self._policy_projection = None
            if flight.result:
                self._last_success_at = now
                self._last_success_digest = flight.digest
                self._last_success_projection = self._policy_projection
            else:
                self._clear_success_cache_locked()
            if self._probe_inflight is flight:
                self._probe_inflight = None
            flight.event.set()

    def _bootstrap_is_valid_locked(self) -> bool:
        reason = self._readiness_refusal_locked()
        if reason is not None:
            self._clear_success_cache_locked()
            if self._provider_binding is not self._bootstrap_binding.provider:
                self._policy_projection = None
        return reason is None

    def _readiness_refusal_locked(self) -> str | None:
        """Which Gate refuses this process, in the order they are evaluated. Story
        1.5.1 left an operator with one blank 503 for a missing credential, an
        unreachable endpoint and a broken Token invariant alike; the Code that comes
        back here is the difference, and it is a Code -- never an endpoint, a
        credential or a ceiling."""
        if self._integrity_poisoned:
            return "integrity_poisoned"
        # 상한 미설정 Gate: a deployment whose ceilings Bootstrap could not validate
        # is never Ready, so /ready answers 503 and every question is refused before
        # it can run under limits nobody chose.
        if self._limits is None:
            return "deployment_unconfigured"
        if self._provider_binding is not self._bootstrap_binding.provider:
            return "provider_unbound"
        if (
            self._policy_projection is not self._bootstrap_binding.projection
            or not self._projection_is_valid_locked()
        ):
            return "policy_projection_invalid"
        if not _provider_satisfies_context_budget(self._provider_binding):
            return "tokenizer_authority_invalid"
        if not _provider_supports_cancellation(self._provider_binding):
            return "cancellation_unsupported"
        return None

    def _projection_is_valid_locked(self) -> bool:
        projection = self._policy_projection
        if not isinstance(projection, PolicyProjectionV1) or projection.model_fields_set != _POLICY_FIELDS:
            return False
        try:
            return PolicyProjectionV1.model_validate(projection.model_dump()) == projection
        except Exception:
            return False

    def _clear_success_cache_locked(self) -> None:
        self._last_success_at = None
        self._last_success_digest = None
        self._last_success_projection = None

    def _cached_success_locked(self, now: float) -> bool:
        """AR27's Cache Key: a `provider_profile_digest`, for at most 30 seconds.

        Object identity was the wrong key in both directions -- rebuilding the same
        Provider with the same settings killed a still-valid Cache, and the "per
        Digest" rule the Architecture states was enforced nowhere. Keying on the
        Digest makes the invalidation condition "the Provider changed" rather than
        "the object changed", which is the fact AR27 is about."""
        if self._last_success_at is None:
            return False
        return (
            self._bootstrap_is_valid_locked()
            and self._last_success_digest is not None
            and self._last_success_digest == _provider_profile_digest(self._provider_binding)
            # By value, not by identity -- the same migration as the Digest above and
            # for the same reason. `_bootstrap_is_valid_locked` has already proven the
            # projection is the bound one and re-validates it; what is left to ask
            # here is whether the Policy this success was recorded under still holds,
            # and a rebuilt-but-identical Projection is the same Policy.
            and self._last_success_projection == self._policy_projection
            and 0 <= now - self._last_success_at < self.READINESS_CACHE_SECONDS
        )

    def get_policy_if_ready(self) -> PolicyProjectionV1 | None:
        if not self.is_ready():
            return None
        with self._readiness_lock:
            if self._cached_success_locked(time.monotonic()):
                return self._policy_projection
        return None

    def policy_readiness(self) -> tuple[PolicyProjectionV1 | None, bool]:
        projection = self.get_policy_if_ready()
        return projection, projection is not None
