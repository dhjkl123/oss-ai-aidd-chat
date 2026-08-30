import logging
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aidd_chat.adapters import (
    DISABLED_RETRIEVAL,
    DeterministicProvider,
    InMemoryConversationStore,
    OllamaLanProxyAdapter,
    ollama_lan_proxy_binding,
)
from aidd_chat.application import (
    RUN_DEADLINE_SECONDS,
    ChatApplication,
    build_policy_projection,
)
from aidd_chat.contracts import (
    ProviderBindingV1,
    ToolPolicyV1,
    binding_digest,
    format_endpoint_origin,
)
from aidd_chat.domain import DeploymentLimits

DEPLOYMENT_PROFILES = ("local_test", "public_demo")

_log = logging.getLogger(__name__)


class ProviderSettings(BaseSettings):
    """Binding selection inputs only. The credential is a SecretStr so a stray
    repr(), log line or pydantic validation error cannot print it; it is unwrapped
    once, at the Outbound Adapter's client construction site."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Closed BY THE TYPE, so pydantic-settings itself refuses anything else -- the
    # same rule `ProviderBindingV1.deployment_profile` carries, in the one place the
    # value enters the process. There is no `.strip() or "local_test"` fallback
    # anywhere downstream: a blank or mistyped value must not silently select the
    # permissive profile, which is exactly what such a fallback did.
    deployment_profile: Literal["local_test", "public_demo"] = "local_test"
    ollama_base_url: str = ""
    # No default. `Secret에는 Default가 없다`: `None` means "no credential was
    # configured", which is a state this process can be in legitimately -- a
    # deterministic local_test binding needs none -- and which is distinct from any
    # value, including the empty string an operator may have typed. Every path that
    # needs a credential fails closed on absence rather than proceeding with "".
    ollama_api_key: SecretStr | None = None

    def credential(self) -> str:
        """The configured credential, or "" when there is none. The single unwrap
        point, and the only place `None` and a blank value are allowed to collapse:
        both mean this process cannot authenticate, and both fail closed."""
        if self.ollama_api_key is None:
            return ""
        return self.ollama_api_key.get_secret_value().strip()


class DeploymentSettingsV1(BaseSettings):
    """Deployment knobs that are not part of the Provider Binding: every capacity
    ceiling and rate this process enforces, plus the Attempt limit.

    None of them has a default. How many Conversations may live in memory, how many
    Runs may be in flight, how fast one session may poll -- these are operator
    decisions about a specific box, and a process that was never told them must not
    invent numbers and advertise itself as Ready. A missing or out-of-range value
    fails `build_deployment_settings`, which leaves the Application with no limits
    and therefore not Ready.

    `trusted_proxy_hops` is the one exception, and it defaults to 0 in the safe
    direction: X-Forwarded-For is client-controlled text until an operator states
    how many proxies actually sit in front of this process."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    max_attempts_per_lineage: int = Field(ge=1, le=8)
    max_resident_conversations: int = Field(ge=1, le=100_000)
    max_global_active_runs: int = Field(ge=1, le=1_000)
    max_session_active_runs: int = Field(ge=1, le=16)
    max_queued_runs: int = Field(ge=1, le=1_000)
    max_turns_per_conversation: int = Field(ge=1, le=1_000)
    max_output_bytes: int = Field(ge=1_024, le=4_194_304)
    max_replay_events: int = Field(ge=16, le=1_000_000)
    max_replay_bytes: int = Field(ge=4_096, le=16_777_216)
    max_sse_connections: int = Field(ge=1, le=1_000)
    create_rate_per_minute: int = Field(ge=1, le=6_000)
    submit_rate_per_minute: int = Field(ge=1, le=6_000)
    poll_rate_per_minute: int = Field(ge=1, le=60_000)
    retry_rate_per_minute: int = Field(ge=1, le=6_000)
    cancel_rate_per_minute: int = Field(ge=1, le=6_000)
    max_requests_per_ip_per_minute: int = Field(ge=1, le=60_000)
    max_provider_concurrency: int = Field(ge=1, le=64)
    spend_window_seconds: int = Field(ge=1, le=86_400)
    spend_limit_units: int = Field(ge=1, le=1_000_000)
    # Closed and single-valued: "one Provider call" is the only meter this process
    # can actually read. Story 1.11's billed Provider adds its unit beside it.
    # Required, with no default, like every other Spend setting: the AC asks for the
    # unit to be validated together with the window and the limit, and a value that
    # defaults is a value nobody chose.
    spend_unit: Literal["provider_call"]
    trusted_proxy_hops: int = Field(default=0, ge=0, le=8)

    @model_validator(mode="after")
    def validate_settings_that_only_work_together(self) -> "DeploymentSettingsV1":
        """Three rules that no single field can express. Each of them was a sentence
        of advice in `.env.example`; a deployment that ignored one looked healthy and
        silently lost a ceiling, so they refuse startup instead.

        No numbers are named in the messages -- an operator has the file, and a
        startup log is not the place to publish this box's ceilings."""
        if self.max_queued_runs >= self.max_global_active_runs:
            raise ValueError(
                "MAX_QUEUED_RUNS는 MAX_GLOBAL_ACTIVE_RUNS보다 작아야 합니다"
            )
        if self.max_requests_per_ip_per_minute <= self.poll_rate_per_minute:
            raise ValueError(
                "MAX_REQUESTS_PER_IP_PER_MINUTE는 POLL_RATE_PER_MINUTE보다 커야 합니다"
            )
        # The Replay Log holds the answer twice -- once as Deltas, once as the
        # message.completed echo -- plus the terminal Events.
        if self.max_replay_bytes < self.max_output_bytes * 2:
            raise ValueError(
                "MAX_REPLAY_BYTES는 MAX_OUTPUT_BYTES의 두 배 이상이어야 합니다"
            )
        return self

    def limits(self) -> DeploymentLimits:
        """The validated settings as the frozen value Domain, Store and Application
        share. Field names match one-for-one so a new ceiling cannot be validated
        here and then quietly not enforced."""
        return DeploymentLimits(
            **{
                name: getattr(self, name)
                for name in DeploymentLimits.__dataclass_fields__
            }
        )


class BindingConfigurationError(RuntimeError):
    """The configuration itself is wrong: an unusable URL, a public_demo profile
    pointed at a private endpoint, a Close Grace that cannot fit inside the Run
    Deadline, or a missing/out-of-range deployment setting. Startup refuses --
    there is no runtime state that could make any of them valid."""


class UnboundProvider:
    """No binding could be built (credential missing or blank), but the process
    still starts so `/ready` and `/api/v1/policy` can answer 503 rather than
    vanishing. It deliberately exposes no policy_metadata, no Tokenizer Authority
    and no digest, so every readiness gate fails closed and questions are refused."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def probe(self) -> bool:
        return False


def endpoint_origin(base_url: str) -> str:
    """scheme://host[:port] from a configured base URL. Path, query and fragment are
    dropped; userinfo is refused outright, the same rule the binding applies. The
    resulting origin is then validated by that same contract rule, so junk hosts
    cannot slip past either."""
    parts = urlsplit(base_url if "//" in base_url else f"//{base_url}", scheme="http")
    if parts.username is not None or parts.password is not None:
        # Rejected, not stripped: silently dropping it would hand the operator an
        # unauthenticated binding with no error at all.
        raise BindingConfigurationError(
            "OLLAMA_BASE_URL에 credential을 넣을 수 없습니다. OLLAMA_API_KEY를 사용하세요"
        )
    try:
        host, port = parts.hostname, parts.port
    except ValueError as exc:
        raise BindingConfigurationError("OLLAMA_BASE_URL이 올바르지 않습니다") from exc
    if not host:
        raise BindingConfigurationError("OLLAMA_BASE_URL이 올바르지 않습니다")
    literal = f"[{host}]" if ":" in host else host
    candidate = f"{parts.scheme}://{literal}" + (f":{port}" if port else "")
    try:
        return format_endpoint_origin(candidate)
    except ValueError as exc:
        raise BindingConfigurationError("OLLAMA_BASE_URL이 올바르지 않습니다") from exc


def build_provider(settings: ProviderSettings | None = None):
    """The bound Provider for this process, with the `public_demo` Guards already
    applied -- they live here, not in `build_chat_application`, because this function
    is exported and called directly.

    Two ways out, and callers must handle both. It RAISES BindingConfigurationError
    for configuration that can never serve: an unknown Profile, a missing Endpoint
    under `public_demo`, an unusable URL, or any refused `public_demo` Guard. It
    RETURNS an `UnboundProvider` for a missing credential -- the process should still
    start so `/ready` can say 503 and name the reason. Story 1.9 added the raising
    half; before it, a `public_demo` violation came back as a provider object."""
    if settings is None:
        try:
            settings = ProviderSettings()
        except ValidationError as exc:
            # DEPLOYMENT_PROFILE is a Literal, so pydantic-settings refuses an unknown
            # or blank value here rather than downstream. No values in the message.
            raise BindingConfigurationError(
                "Provider 설정이 올바르지 않습니다: DEPLOYMENT_PROFILE"
            ) from exc
    profile = settings.deployment_profile
    base_url = settings.ollama_base_url.strip()
    if not base_url:
        if profile != "local_test":
            # The deterministic stub is a Test/CI binding. Serving it under any other
            # profile would advertise "Deterministic local test provider" on a public
            # /policy page with a green /ready.
            raise BindingConfigurationError(
                f"{profile} profile에는 Provider Endpoint가 필요합니다"
            )
        return apply_public_demo_guards(DeterministicProvider(), profile)
    origin = endpoint_origin(base_url)
    api_key = settings.credential()
    if not api_key:
        # Never fall back to the deterministic stub: an operator who configured a
        # real endpoint would not notice the swap. Serve 503 instead.
        #
        # Returned UNGUARDED, deliberately: an UnboundProvider has no binding and no
        # policy metadata, so running it through the public_demo Guards would answer
        # one fault -- "no credential" -- with three Guard names and throw away the
        # `reason` that actually says which. It is already fail-closed: no digest, no
        # Tokenizer Authority, no policy, so /ready and /policy answer 503 and no
        # question is ever accepted.
        return UnboundProvider("OLLAMA_API_KEY가 설정되지 않았습니다")
    try:
        binding = ollama_lan_proxy_binding(origin, profile)
    except ValueError as exc:
        # public_demo + a private/loopback origin, or an unknown profile.
        raise BindingConfigurationError("Provider Binding을 구성할 수 없습니다") from exc
    # Guarded HERE, beside the profile check above, and not in build_chat_application:
    # this function is exported and called directly, and a Guard only one caller runs
    # is a Guard the next caller does not have.
    return apply_public_demo_guards(OllamaLanProxyAdapter(binding, api_key), profile)


def build_deployment_settings() -> DeploymentSettingsV1:
    try:
        return DeploymentSettingsV1()
    except ValidationError as exc:
        # Name the offending fields rather than hard-coding one: Story 1.8 adds more
        # settings beside max_attempts_per_lineage, and reporting the wrong one is
        # worse than reporting none.
        fields = ", ".join(
            sorted({str(error["loc"][0]).upper() for error in exc.errors() if error["loc"]})
        )
        raise BindingConfigurationError(
            f"배포 설정이 올바르지 않습니다: {fields or 'DEPLOYMENT_SETTINGS'}"
        ) from exc


def _validated_close_grace(provider):
    """A Close Grace at least as long as the Run Deadline leaves no Deadline for the
    Run: every call would be cancelled the moment it started. Refuse at startup
    rather than serve a binding that can only ever time out."""
    grace_ms = getattr(provider, "close_grace_ms", None)
    if isinstance(grace_ms, int) and not isinstance(grace_ms, bool):
        if grace_ms >= RUN_DEADLINE_SECONDS * 1_000:
            raise BindingConfigurationError(
                "close_grace_ms는 Run Deadline(120초)보다 짧아야 합니다"
            )
    return provider


# LOG_LEVEL is a level NAME here, not a number: a numeric value refuses, under the
# `quiet_logs` Guard, deliberately -- an operator writing 20 has almost certainly
# copied it from somewhere that meant something else.
_QUIET_LOG_LEVELS = frozenset({"info", "warning", "warn", "error", "critical", "fatal"})
# No "" here, deliberately: uvicorn's own default for the access log is ON, so a
# deployment that never mentions it has not declared it off -- exactly the argument
# WEB_CONCURRENCY/REPLICA_COUNT make, with the opposite default.
_ACCESS_LOG_OFF = frozenset({"0", "false", "off", "no"})


class RuntimeEnvironmentV1(BaseSettings):
    """How this process was LAUNCHED, as declared by its operator -- as opposed to
    what it enforces, which is why none of it belongs in DeploymentSettingsV1.

    Every field is a plain string with an empty default, deliberately: an int field
    defaulting to 1 would invent the very fact `public_demo` has to be told, and
    "not declared" has to stay distinguishable from "declared as one". Read the same
    way as every other setting (environment first, then .env), so an operator does
    not have to learn a second convention for four variables."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    web_concurrency: str = ""
    replica_count: str = ""
    log_level: str = ""
    uvicorn_access_log: str = ""

    def single_process(self) -> bool:
        """Both counts DECLARED as exactly one. Every ceiling this process enforces
        is in-memory, so a second worker or replica silently halves all of them.
        Not declared is a refusal, not a default of one.

        ponytail: a declaration is all this can check. `uvicorn --workers 4` beside
        `WEB_CONCURRENCY=1`, or a platform that scaled the deployment out after it
        started, both pass. Confirming the real topology needs a platform-specific
        probe this process does not have."""
        return self.web_concurrency.strip() == "1" and self.replica_count.strip() == "1"

    def quiet_logs(self) -> bool:
        """No Debug level ANYWHERE -- every configured logger, not just the root, so
        a single `logging.getLogger("aidd_chat").setLevel(DEBUG)` cannot run under
        `public_demo` -- and no Uvicorn Default Access Log. `main.lifespan` disables
        the access logger regardless; this refuses the deployment that asked for it,
        rather than quietly overriding an operator who meant it."""
        # Must be DECLARED off. Unset is a refusal: uvicorn's default is on.
        if self.uvicorn_access_log.strip().casefold() not in _ACCESS_LOG_OFF:
            return False
        names = ["", *logging.root.manager.loggerDict]
        if any(logging.getLogger(name).getEffectiveLevel() <= logging.DEBUG for name in names):
            return False
        return self.log_level.strip().casefold() in _QUIET_LOG_LEVELS


def _public_binding(provider: object, profile: str) -> "ProviderBindingV1 | None":
    """The Binding this provider is actually bound to, re-validated. `binding_digest`
    re-runs every model rule -- including `public_demo` may not bind a Private or
    Loopback Endpoint -- so a binding built with `model_copy(update=...)`, which skips
    validators, cannot get past this.

    A failure is logged by exception TYPE and never by value: an operator has to be
    able to tell "this binding is illegal for public_demo" from "reading it raised",
    and neither answer may carry an endpoint or a credential."""
    binding = getattr(provider, "binding", None)
    if binding is None:
        return None
    if getattr(binding, "deployment_profile", None) != profile:
        return None
    try:
        binding_digest(binding)
    except Exception as exc:
        _log.error("Provider Binding 재검증 실패 (%s)", type(exc).__name__)
        return None
    return binding


def _policy_metadata(provider: object) -> object | None:
    """The provider's public policy metadata, or None if it has none.

    A provider that simply does not expose the attribute is ABSENT, not broken, and
    says nothing to log -- conflating the two is how one fault comes back as several
    Guard names plus a spurious AttributeError line. Anything else that goes wrong
    reading it is a real fault and is logged by exception type, never by value."""
    if not hasattr(provider, "policy_metadata"):
        return None
    try:
        return provider.policy_metadata
    except Exception as exc:
        _log.error("Provider 공개 정책을 읽을 수 없습니다 (%s)", type(exc).__name__)
        return None


def _known_policy(metadata: object) -> bool:
    if metadata is None:
        return False
    try:
        build_policy_projection(metadata)
    except Exception:
        return False
    return True


def apply_public_demo_guards(
    provider: object,
    profile: str,
    environment: "RuntimeEnvironmentV1 | None" = None,
) -> object:
    """Every condition `public_demo` must refuse, in one place, fail-closed. A
    violation raises, `build_provider`'s caller catches it into `UnboundProvider`,
    and the process serves 503 on /ready and /policy instead of a public demo running
    on a test provider, a private endpoint, several workers or a debug log.

    Each Guard is INDEPENDENT: a missing binding must not also trip `test_provider`
    and `provider_tool`, or one fault comes back as three Guard names and none of
    them is the one that meant it. Every binding attribute is therefore read with a
    default rather than off a value that may not have it -- an AttributeError out of
    an exported function is not a refusal an operator can act on.

    The refusal names the Guards and nothing else -- no endpoint, no credential, no
    ceiling -- which is what an operator staring at a 503 actually needs."""
    if profile != "public_demo":
        return provider
    binding = _public_binding(provider, profile)
    metadata = _policy_metadata(provider)
    environment = environment or RuntimeEnvironmentV1()
    if binding is None and metadata is None:
        # Not bound at all -- no binding, no policy. That is ONE fault, and answering
        # it with `provider_binding`, `retrieval_disabled` and `known_policy` would
        # bury the cause under three names, none of which is it.
        raise BindingConfigurationError(
            "public_demo Guard가 거부했습니다: provider_unbound"
        )
    guards = {
        # A Deterministic/Fake Provider on a public demo would advertise a test
        # binding on /policy behind a green /ready.
        # ponytail: a class check plus the binding's own `provider_extra`. It proves
        # a class, where the AC states a property -- a fake that subclasses neither
        # and declares `provider_extra="openai"` passes. Adequate while
        # `build_provider` is the only thing that constructs providers; tighten to a
        # positive proof (a binding whose endpoint actually answered) if that stops
        # being true.
        "test_provider": not isinstance(provider, DeterministicProvider)
        and getattr(binding, "provider_extra", None) != "deterministic",
        # Covers Localhost/Private Endpoint and every other binding rule, re-checked.
        "provider_binding": binding is not None,
        # Redundant BY CONSTRUCTION, and kept as the AC's structural statement
        # rather than as a live check: `ToolPolicyV1` is single-valued, the binding
        # coerces any instance to the singleton, and `_public_binding` re-validates
        # -- so a binding carrying a tool is refused as `provider_binding` before
        # this is ever consulted. It can only ever be True. Delete it only together
        # with the AC it restates.
        "provider_tool": getattr(binding, "tool_policy", ToolPolicyV1.zero) == ToolPolicyV1.zero,
        "retrieval_disabled": getattr(metadata, "retrieval_status", None)
        == DISABLED_RETRIEVAL.status,
        "known_policy": _known_policy(metadata),
        "single_process": environment.single_process(),
        "quiet_logs": environment.quiet_logs(),
    }
    refused = sorted(name for name, satisfied in guards.items() if not satisfied)
    if refused:
        raise BindingConfigurationError(
            "public_demo Guard가 거부했습니다: " + ", ".join(refused)
        )
    return provider


def build_chat_application() -> ChatApplication:
    """The process starts even when the deployment configuration is unusable. It has
    to: an operator who left a ceiling out, or pointed the binding at a URL that
    cannot be parsed, needs `/ready` to answer 503 and a log line naming the setting
    -- not a container that exits before anyone can ask it anything.

    Both halves are caught, because an operator can easily have both problems at
    once and a crash from the second would defeat the whole point of surviving the
    first. Neither half can produce a serving process: no limits fails the Readiness
    Gate, and `UnboundProvider` exposes no policy metadata, digest or Tokenizer
    Authority, so every other readiness gate fails closed too."""
    try:
        settings = build_deployment_settings()
        attempts, limits = settings.max_attempts_per_lineage, settings.limits()
    except Exception as exc:
        # `build_deployment_settings` already composed the field names, and no
        # values, into this message -- which is the whole point of it, and what
        # `.env.example` and the manual check promise the operator.
        # Deliberately broader than BindingConfigurationError: a malformed .env can
        # fail in ways pydantic-settings does not wrap, and those must not crash a
        # process whose whole job right now is to explain itself on /ready.
        _log.error("%s", exc)
        attempts, limits = 1, None
    try:
        provider = _validated_close_grace(build_provider())
    except BindingConfigurationError as exc:
        # The message names the offending setting or Guard and no value -- which is
        # what makes an empty /ready 503 diagnosable at all.
        _log.error("%s", exc)
        provider = UnboundProvider("Provider Binding 설정이 올바르지 않습니다")
    except Exception as exc:
        _log.error("Provider 설정을 읽을 수 없습니다 (%s)", type(exc).__name__)
        provider = UnboundProvider("Provider 설정을 읽을 수 없습니다")
    if isinstance(provider, UnboundProvider):
        # Story 1.5.1 left `reason` written and never read: an operator saw the same
        # blank 503 for a missing credential, an unreachable endpoint and a refused
        # Guard. It is a fixed sentence with no values, so it is safe to log.
        _log.error("%s", provider.reason)
    return ChatApplication(InMemoryConversationStore(), provider, attempts, limits)


__all__ = [
    "DEPLOYMENT_PROFILES",
    "BindingConfigurationError",
    "DeploymentLimits",
    "DeploymentSettingsV1",
    "ProviderSettings",
    "RuntimeEnvironmentV1",
    "UnboundProvider",
    "apply_public_demo_guards",
    "build_chat_application",
    "build_deployment_settings",
    "build_provider",
    "endpoint_origin",
]
