from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import ip_address
import json
import re
import socket
import unicodedata
from typing import Annotated, ClassVar, Literal, Union
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator


SYSTEM_INSTRUCTION = "사용자의 질문에 한국어로 명확하고 간결하게 답하세요."
# AD-25 Closed serializer identity: bumping this whenever the canonical request
# shape changes is what lets count_input_tokens()/stream()/complete() detect a
# stale caller and fail closed instead of silently mis-tokenizing.
SERIALIZER_ID = "pydanticai-direct-json-role-text-v1"
# AD-25 fixes both the id and the exact ASCII descriptor the digest is taken over.
SERIALIZER_DESCRIPTOR = (
    f"{SERIALIZER_ID}|UTF-8|NFC|LF|compact-json|fixed-field-order"
    "|metadata-excluded|one-textual-part"
)
SERIALIZER_DIGEST = sha256(SERIALIZER_DESCRIPTOR.encode("utf-8")).hexdigest()


def has_visible_text(value: str) -> bool:
    return any(char.isprintable() and not char.isspace() for char in value)


# Hostile-text code points. Model output is untrusted data, and so is a user's
# question -- both are rendered into the same transcript, so both pass this. These
# are the code points text has no everyday role for in a chat answer and that change
# what a reader sees, or hide from them entirely, rather than saying anything:
#   C0 / DEL / C1        controls, tab/LF/CR excepted: a streamed answer is
#                        committed raw, so CRLF has to stay admissible
#   U+00AD U+180E        soft hyphen, Mongolian vowel separator
#   U+061C U+200E U+200F bidi marks
#   U+200B               zero width space
#   U+202A-202E          bidi overrides and directional embeds
#   U+2060-2064          word joiner and the invisible operators
#   U+2066-2069          bidi isolates
#   U+2028 U+2029        line and paragraph separators
#   U+D800-DFFF          lone surrogates
#   U+FEFF               zero width no-break space / BOM
#   U+E0000-E007F        the Tags block: invisible, and the current fashion for
#                        hiding instructions inside otherwise innocent text
#
# Deliberately NOT here, because they are how ordinary text is composed rather than
# how it is smuggled, and refusing them would fail a whole answer for saying
# 좋아요 ❤️:
#   U+FE00-FE0F, U+E0100-E01EF  variation selectors -- VS16 is what makes ❤ render
#                               as ❤️, and models emit it constantly
#   U+200D                      zero width joiner -- the emoji composition
#                               mechanism (👨‍👩‍👧, 🏳️‍🌈) and meaningful in Indic
#                               and Persian scripts
#   U+200C                      zero width non-joiner -- same script reason
#
# ponytail: this is the fail-closed reading of the epic's "Model 출력은 Hostile
# Data" stance, and what is left has two known costs, both whole-answer rejections
# (provider_invalid_response) rather than partial edits: an ANSI colour sequence
# (ESC is C0), and a deliberate zero width space. Neither is something a model emits
# by accident. Rendering is textContent-only, so none of these bytes is executable
# in a browser either way.
_UNSAFE_TEXT = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
    "\u00ad\u180e"
    "\u061c\u200b\u200e\u200f"
    "\u202a-\u202e"
    "\u2060-\u2064\u2066-\u2069"
    "\u2028\u2029"
    "\ud800-\udfff"
    "\ufeff"
    "\U000e0000-\U000e007f]"
)


def has_unsafe_code_point(value: str) -> bool:
    """The one predicate. Every text this process shows a human -- Model answer,
    Delta, completed Message, the user's own question, a failure sentence -- is
    checked by THIS function and no local copy of it, so a new text path cannot
    quietly get a weaker rule (tests/test_story_1_9.py enforces that as a gate)."""
    return bool(_UNSAFE_TEXT.search(value))


def validate_model_text(value: object, *, require_visible: bool = False) -> str:
    """The single Validator every Model-produced Text passes through, streaming and
    completed alike. It REJECTS, it never rewrites -- the value it returns is the
    input unchanged, never a cleaned one: rendering is `textContent` only, so
    nothing is made safe by editing the answer, and silently changing what the Model
    said would break the 정적·축소·대체 답변 금지 contract. `<script>` is therefore
    admissible text: it is displayed, never executed."""
    if not isinstance(value, str):
        raise ValueError("Model 출력은 텍스트여야 합니다")
    if require_visible:
        if not has_visible_text(value):
            raise ValueError("완료 답변은 유효한 비어 있지 않은 텍스트여야 합니다")
    elif not value:
        raise ValueError("Delta는 유효한 비어 있지 않은 텍스트여야 합니다")
    if has_unsafe_code_point(value):
        raise ValueError("Model 출력에 허용되지 않는 문자가 포함되었습니다")
    return value


def is_valid_model_text(value: object, *, require_visible: bool = False) -> bool:
    """`validate_model_text` as a predicate, for the Domain commits whose contract is
    a bool rather than a raise. Same function, so a commit can never admit text the
    Event it is about to build would refuse -- and a non-str arrives as False rather
    than as a TypeError escaping a commit."""
    try:
        validate_model_text(value, require_visible=require_visible)
    except ValueError:
        return False
    return True


# The Run vocabularies, spelled once. Every Literal below and every tuple that
# enumerates them is derived from these, so a sixth Stage cannot be added to the
# Domain and missed by Telemetry.
RunStage = Literal["queued", "preparing", "streaming", "finalizing", "terminal"]
RunState = Literal["queued", "running", "completed", "failed", "timeout", "cancelled"]
TerminalRunState = Literal["completed", "failed", "timeout", "cancelled"]


def _uuid4(value: UUID) -> UUID:
    if value.version != 4:
        raise ValueError("UUIDv4가 필요합니다")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("UTC timestamp가 필요합니다")
    return value


def _canonical_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


class ContextIntegrityError(Exception):
    """A recomputed serializer/context/provider-profile digest did not match a
    PreparedModelRequestV1 about to be consumed. Fail-closed: never send this
    request to a Provider; the caller maps this to a provider_unknown Run failure."""

    kind = "provider_unknown"


@dataclass(frozen=True)
class PreparedMessageV1:
    message_id: UUID
    role: Literal["user", "assistant"]
    text: str


@dataclass(frozen=True)
class PreparedModelRequestV1:
    schema_version: Literal["1"]
    system_instruction: str
    messages: tuple[PreparedMessageV1, ...]
    canonical_bytes: bytes
    serializer_digest: str
    context_digest: str
    provider_profile_digest: str
    # AD-25's explicit Current Input binding. Excluded from canonical_request_bytes
    # (metadata, like every ID), so adding it leaves context_digest untouched -- it
    # exists so a Retry can prove which User Message a reused Snapshot answers.
    current_input_message_id: UUID | None = None


def canonical_request_bytes(
    system_instruction: str, messages: tuple[PreparedMessageV1, ...]
) -> bytes:
    """Compact UTF-8 JSON, field order schema_version/system_instruction/messages
    and role/text per message, NFC/LF text, IDs and digests excluded. Both
    count_input_tokens() and stream()/complete() must consume exactly these bytes."""
    payload = {
        "schema_version": "1",
        "system_instruction": _canonical_text(system_instruction),
        "messages": [
            {"role": message.role, "text": _canonical_text(message.text)} for message in messages
        ],
    }
    return json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def compute_provider_profile_digest(
    policy_metadata: Mapping[str, object], tokenizer_authority: tuple[str, str, int]
) -> str:
    """No new policy Projection field: the digest folds the existing public policy
    metadata together with the Provider's Tokenizer Authority (name, version,
    max_input_tokens) so a binding/tokenizer drift is detectable without one."""
    name, version, max_input_tokens = tokenizer_authority
    payload = {
        "policy_metadata": {key: policy_metadata[key] for key in sorted(policy_metadata)},
        "tokenizer_authority": {
            "name": name,
            "version": version,
            "max_input_tokens": max_input_tokens,
        },
    }
    payload_bytes = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return sha256(payload_bytes).hexdigest()


_CREDENTIAL_REFERENCE_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class ToolPolicyV1(BaseModel):
    """Closed zero-tool policy. Every field is single-valued, so `ToolPolicyV1.zero`
    is the only value that can exist -- a binding cannot quietly grow a tool,
    a native tool, or image output past the Closed Schema. Bindings are coerced to
    the singleton so `binding.tool_policy is ToolPolicyV1.zero` holds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    zero: ClassVar["ToolPolicyV1"]

    function_tool_count: Literal[0] = 0
    native_tool_count: Literal[0] = 0
    output_tool_count: Literal[0] = 0
    allow_text_output: Literal[True] = True
    allow_image_output: Literal[False] = False


ToolPolicyV1.zero = ToolPolicyV1()

_DNS_NAME = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))*$", re.IGNORECASE)
_PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa")


def _host_ip(host: str):
    """The host as an IP address, or None if it is a DNS name. `inet_aton` covers
    the legacy IPv4 spellings a browser and libc still resolve but `ip_address`
    rejects -- `2130706433` and `0x7f.1` are both 127.0.0.1."""
    try:
        return ip_address(host)
    except ValueError:
        pass
    try:
        return ip_address(socket.inet_ntoa(socket.inet_aton(host)))
    except (OSError, ValueError):
        return None


def split_endpoint_origin(value: str) -> tuple[str, str, int | None]:
    """(scheme, host, port) for an origin-only URL. The single trust boundary a
    Provider endpoint enters through: userinfo, path, query, fragment, junk hosts
    and non-http schemes are all rejected rather than silently carried into a
    binding digest."""
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("endpoint_origin은 http 또는 https여야 합니다")
    if parts.path or parts.query or parts.fragment:
        raise ValueError("endpoint_origin은 scheme://host[:port]만 담아야 합니다")
    if parts.username is not None or parts.password is not None:
        raise ValueError("endpoint_origin에 credential을 담을 수 없습니다")
    try:
        host, port = parts.hostname, parts.port
    except ValueError as exc:
        raise ValueError("endpoint_origin의 port가 올바르지 않습니다") from exc
    if not host:
        raise ValueError("endpoint_origin에 host가 필요합니다")
    if _host_ip(host) is None and not _DNS_NAME.fullmatch(host):
        raise ValueError("endpoint_origin의 host가 올바르지 않습니다")
    return parts.scheme, host, port


def format_endpoint_origin(value: str) -> str:
    """Validated, canonical scheme://host[:port]. IPv6 hosts are re-bracketed --
    urlsplit strips the brackets, so naive reassembly emits an unusable URL."""
    scheme, host, port = split_endpoint_origin(value)
    literal = f"[{host}]" if ":" in host else host
    return f"{scheme}://{literal}" + (f":{port}" if port else "")


def _is_private_origin(origin: str | None) -> bool:
    """Loopback/private/link-local addresses (in every IPv4 spelling) and
    non-public DNS suffixes. A missing or unparseable origin counts as private:
    `public_demo` may only bind what it can prove is publicly routable."""
    if not origin:
        return True
    try:
        _scheme, host, _port = split_endpoint_origin(origin)
    except ValueError:
        return True
    address = _host_ip(host)
    if address is None:
        lowered = host.casefold()
        return lowered == "localhost" or lowered.endswith(_PRIVATE_HOST_SUFFIXES)
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


class ProviderBindingV1(BaseModel):
    """Frozen, Closed snapshot of which Provider the process is bound to.

    Credential VALUES never live here -- only `credential_reference_names`, the
    environment variable names the Outbound Adapter reads them from. The binding
    is never projected to a client; only `binding_digest()` of it is recorded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    deployment_profile: Literal["local_test", "public_demo"]
    provider_extra: Literal["openai", "deterministic"]
    endpoint_origin: StrictStr | None = None
    provider_label: StrictStr
    endpoint_disclosure: StrictStr
    model_revision: StrictStr
    processing_region: StrictStr
    retention_summary: StrictStr
    deletion_summary: StrictStr
    credential_reference_names: tuple[StrictStr, ...] = ()
    tokenizer_authority_name: StrictStr
    tokenizer_authority_version: StrictStr
    max_input_tokens: int = Field(ge=1)
    tool_policy: ToolPolicyV1 = Field(default_factory=lambda: ToolPolicyV1.zero)
    max_retries: Literal[0] = 0
    transport_attempt_policy: Literal["single"] = "single"
    # gt=0, not ge=0: `wait_for(close(), 0)` cancels the close before it can
    # start. A binding that cannot be closed is refused here, where the field is
    # defined, rather than silently taking the whole app to /ready 503.
    close_grace_ms: int = Field(default=250, gt=0)

    @field_validator("credential_reference_names")
    @classmethod
    def validate_credential_reference_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # Shape only. An all-caps secret would also match, so this is not proof the
        # value is a name -- it is the cheap guard that keeps lowercase/base64/URL
        # shaped secrets and free text out of a field that is digested and logged.
        if any(not _CREDENTIAL_REFERENCE_NAME.fullmatch(name) for name in value):
            raise ValueError("Credential 참조는 환경변수 이름이어야 합니다")
        return value

    @field_validator("endpoint_origin")
    @classmethod
    def validate_endpoint_origin(cls, value: str | None) -> str | None:
        """Validated here, on the model, not in a caller docstring: this is the
        only place an endpoint enters the binding, so userinfo, paths and junk
        hosts cannot reach the digest no matter who constructs it."""
        return None if value is None else format_endpoint_origin(value)

    @field_validator("tool_policy")
    @classmethod
    def canonical_tool_policy(cls, value: ToolPolicyV1) -> ToolPolicyV1:
        # Single-valued schema: any instance that validated equals the singleton.
        return ToolPolicyV1.zero

    @model_validator(mode="after")
    def validate_profile_endpoint(self) -> "ProviderBindingV1":
        if (self.provider_extra == "deterministic") != (self.endpoint_origin is None):
            raise ValueError("endpoint_origin은 실제 Provider Binding에만 존재해야 합니다")
        if self.deployment_profile == "public_demo" and _is_private_origin(self.endpoint_origin):
            raise ValueError("public_demo는 Private/Loopback Endpoint를 Bind할 수 없습니다")
        return self


def binding_digest(binding: ProviderBindingV1) -> str:
    """SHA-256 over the canonical compact JSON of the whole binding. Credential
    values are structurally absent (only reference names are fields), so the
    digest is safe to record on a Run.

    Re-validated first: `model_copy(update=...)` deliberately skips validators, so a
    binding built that way can carry a public_demo profile with a private or missing
    endpoint that the model never checked. Nothing reaches the digest -- or, through
    it, a Run -- without passing the same rules a constructed binding passes."""
    binding = ProviderBindingV1.model_validate(binding.model_dump())
    payload = json.dumps(
        binding.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def validate_messages(messages: tuple[PreparedMessageV1, ...]) -> None:
    if not messages:
        raise ValueError("messages는 비어 있을 수 없습니다")
    if messages[-1].role != "user":
        raise ValueError("마지막 message는 현재 입력(user)이어야 합니다")
    if any(message.role not in ("user", "assistant") for message in messages):
        raise ValueError("message role은 user 또는 assistant여야 합니다")


def prepare_model_request(
    messages: tuple[PreparedMessageV1, ...], *, provider_profile_digest: str
) -> PreparedModelRequestV1:
    """Build the AD-25 Closed provider request. `messages` is the full completed
    context in chronological order with the current question last -- callers
    select which prior turns to include before calling this. Text is
    canonicalized (NFC/LF) here, once, so every consumer -- the canonical bytes
    below, the Tokenizer Authority, and the Provider adapter's own message
    mapping -- reads the exact same text a stored (possibly un-normalized,
    e.g. raw streamed) Message never guaranteed on its own."""
    validate_messages(messages)
    canonical_messages = tuple(
        PreparedMessageV1(message.message_id, message.role, _canonical_text(message.text))
        for message in messages
    )
    canonical_bytes = canonical_request_bytes(SYSTEM_INSTRUCTION, canonical_messages)
    return PreparedModelRequestV1(
        schema_version="1",
        system_instruction=SYSTEM_INSTRUCTION,
        messages=canonical_messages,
        canonical_bytes=canonical_bytes,
        serializer_digest=SERIALIZER_DIGEST,
        context_digest=sha256(canonical_bytes).hexdigest(),
        provider_profile_digest=provider_profile_digest,
        current_input_message_id=canonical_messages[-1].message_id,
    )


class QuestionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["question"]
    content: StrictStr

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        # Same predicate as a Model answer: a question is displayed in the same
        # transcript, by the same `textContent` path, and a bidi override typed by
        # the user reorders the line exactly as one emitted by a Model would. The
        # sentences differ because the audience does; the rule does not.
        if not has_visible_text(value):
            raise ValueError("질문을 입력해 주세요.")
        if has_unsafe_code_point(value):
            raise ValueError("질문에 유효하지 않은 문자가 포함되었습니다.")
        return value


class RetryCommand(BaseModel):
    """Re-run one prior Run, nothing else. No `content`: the question is whatever
    the target Run already asked, so a Retry can never smuggle a different one in
    under an old Run's `input_message_id`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["retry"]
    retry_of_run_id: UUID

    @field_validator("retry_of_run_id")
    @classmethod
    def validate_retry_of_run_id(cls, value: UUID) -> UUID:
        return _uuid4(value)


RunCommandV1 = Annotated[Union[QuestionCommand, RetryCommand], Field(discriminator="kind")]


class CompletedMessageV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: UUID
    content: StrictStr

    @field_validator("message_id")
    @classmethod
    def validate_message_id(cls, value: UUID) -> UUID:
        return _uuid4(value)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return validate_model_text(value, require_visible=True)


ProviderFailureKind = Literal[
    "provider_auth",
    "provider_rate_limit",
    "provider_unavailable",
    "provider_transport",
    "provider_timeout",
    "provider_cancelled",
    "provider_non_text",
    "provider_empty",
    "provider_incomplete",
    "provider_suspended",
    "provider_interrupted",
    "provider_invalid_response",
    "provider_content_filtered",
    "provider_unknown",
    # Not a Provider fault at all: a deployment ceiling ended this Run. It lives in
    # this enum because `RunProjectionV1.terminal_error` is the only terminal error
    # channel a Run has, and that projection is a closed Field set.
    "capacity_exceeded",
]


class ProviderFailureV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ProviderFailureKind
    retryable: bool
    correlation_id: UUID
    message: StrictStr

    @field_validator("correlation_id")
    @classmethod
    def validate_correlation_id(cls, value: UUID) -> UUID:
        return _uuid4(value)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        if not has_visible_text(value) or has_unsafe_code_point(value):
            raise ValueError("Provider failure message는 유효한 비어 있지 않은 텍스트여야 합니다")
        return value


class _RunEventBaseV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    run_id: UUID
    sequence: int = Field(ge=1)
    occurred_at: datetime

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: UUID) -> UUID:
        return _uuid4(value)

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        return _utc(value)


class RunStatusEventV1(_RunEventBaseV1):
    type: Literal["run.status"] = "run.status"
    state: Literal["running", "completed", "failed", "timeout", "cancelled"]
    stage: Literal["streaming", "terminal"]

    @model_validator(mode="after")
    def validate_state_stage(self) -> "RunStatusEventV1":
        if (self.state == "running") != (self.stage == "streaming"):
            raise ValueError("Run event state와 stage 조합이 올바르지 않습니다")
        return self


class MessageDeltaEventV1(_RunEventBaseV1):
    type: Literal["message.delta"] = "message.delta"
    message_id: UUID
    text: StrictStr

    @field_validator("message_id")
    @classmethod
    def validate_message_id(cls, value: UUID) -> UUID:
        return _uuid4(value)

    # Streaming half of the "same Validator" contract -- the completed half below
    # calls the identical function, so the two paths cannot drift apart.
    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return validate_model_text(value)


class MessageCompletedEventV1(_RunEventBaseV1):
    type: Literal["message.completed"] = "message.completed"
    message_id: UUID
    text: StrictStr

    @field_validator("message_id")
    @classmethod
    def validate_message_id(cls, value: UUID) -> UUID:
        return _uuid4(value)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return validate_model_text(value, require_visible=True)


class MessageDiscardedEventV1(_RunEventBaseV1):
    type: Literal["message.discarded"] = "message.discarded"
    message_id: UUID

    @field_validator("message_id")
    @classmethod
    def validate_message_id(cls, value: UUID) -> UUID:
        return _uuid4(value)


class RunErrorEventV1(_RunEventBaseV1):
    type: Literal["run.error"] = "run.error"
    error: ProviderFailureV1


class ContextTruncatedEventV1(_RunEventBaseV1):
    type: Literal["context.truncated"] = "context.truncated"
    dropped_turn_count: int = Field(ge=1)


class ConversationExpiredEventV1(_RunEventBaseV1):
    """The Conversation reached its absolute 3,600 s expiry while this Run's stream
    was still open. Carries nothing but the envelope: everything it could describe
    is being purged in the same Critical Section that emits it."""

    type: Literal["conversation.expired"] = "conversation.expired"


class StreamEndEventV1(_RunEventBaseV1):
    type: Literal["stream.end"] = "stream.end"
    # `expired` is not a Run state -- no Run ever reaches it. It is how a stream
    # says the Conversation underneath it ended, and it is always preceded by
    # conversation.expired.
    final_state: Literal["completed", "failed", "timeout", "cancelled", "expired"]
    final_sequence: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_final_sequence(self) -> "StreamEndEventV1":
        if self.final_sequence != self.sequence:
            raise ValueError("stream.end sequence가 일치하지 않습니다")
        return self


RunEventV1 = Annotated[
    Union[
        RunStatusEventV1,
        MessageDeltaEventV1,
        MessageCompletedEventV1,
        MessageDiscardedEventV1,
        RunErrorEventV1,
        ContextTruncatedEventV1,
        ConversationExpiredEventV1,
        StreamEndEventV1,
    ],
    Field(discriminator="type"),
]


class RunProjectionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    run_id: UUID
    conversation_id: UUID
    input_message_id: UUID
    retry_of_run_id: UUID | None = None
    output_message_id: UUID | None = None
    output_message: CompletedMessageV1 | None = None
    state: RunState
    stage: RunStage
    created_at: datetime
    last_updated_at: datetime
    latest_sequence: int = Field(default=0, ge=0)
    terminal_error: ProviderFailureV1 | None = None

    @field_validator(
        "run_id", "conversation_id", "input_message_id", "retry_of_run_id", "output_message_id"
    )
    @classmethod
    def validate_ids(cls, value: UUID | None) -> UUID | None:
        return None if value is None else _uuid4(value)

    @field_validator("created_at", "last_updated_at")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_terminal_fields(self) -> "RunProjectionV1":
        completed = self.state == "completed"
        valid_stages = {
            "queued": {"queued"},
            "running": {"preparing", "streaming", "finalizing"},
            "completed": {"terminal"},
            "failed": {"terminal"},
            "timeout": {"terminal"},
            "cancelled": {"terminal"},
        }
        if self.stage not in valid_stages[self.state]:
            raise ValueError("Run state와 stage 조합이 올바르지 않습니다")
        if (self.output_message_id is not None) != completed or (self.output_message is not None) != completed:
            raise ValueError("completed Run만 output_message를 가질 수 있습니다")
        if completed and self.output_message_id != self.output_message.message_id:
            raise ValueError("output message ID가 일치하지 않습니다")
        if (self.terminal_error is not None) != (self.state in {"failed", "timeout"}):
            raise ValueError("terminal_error 상태가 올바르지 않습니다")
        return self


class CancelResultV1(BaseModel):
    """Both Cancel outcomes are successes: `accepted` won the terminal CAS,
    `already_terminal` lost it to a Complete/Cancel that committed first. `run` is
    always the current Run either way, so a client never has to guess."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    cancel_outcome: Literal["accepted", "already_terminal"]
    run: RunProjectionV1


class ErrorBodyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: StrictStr
    message: StrictStr
    retryable: bool
    correlation_id: UUID
    field_errors: dict[str, str] = Field(default_factory=dict)


class ErrorEnvelopeV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ErrorBodyV1
