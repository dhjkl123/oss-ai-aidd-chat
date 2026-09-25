import logging
from pathlib import Path
import re
import subprocess
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aidd_chat.adapters import FakeAgent, InMemoryConversationStore
from aidd_chat.adapters.pi_sidecar import PiSidecarAdapter, pi_policy_metadata
from aidd_chat.adapters.tokenizer import HfTokenizer
from aidd_chat.application import RUN_DEADLINE_SECONDS, ChatApplication
from aidd_chat.contracts import (
    AgentBindingV1,
    AgentLimitsV1,
    TokenizerAuthorityV1,
    format_endpoint_origin,
)
from aidd_chat.domain import DeploymentLimits

DEPLOYMENT_PROFILES = ("local_test",)

_log = logging.getLogger(__name__)


class ProviderSettings(BaseSettings):
    """Binding selection inputs only. The credential is a SecretStr so a stray
    repr(), log line or pydantic validation error cannot print it; it is unwrapped
    once, at the Outbound Adapter's client construction site."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Closed BY THE TYPE, so pydantic-settings itself refuses anything else -- the
    # same rule `AgentBindingV1.deployment_profile` carries, in the one place the
    # value enters the process. `public_demo` is retired (PRD C-7.3): naming it now
    # refuses startup like any other unknown profile.
    deployment_profile: Literal["local_test"] = "local_test"
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


class AgentSettings(BaseSettings):
    """Wiki agent (pi sidecar) inputs, only read when OLLAMA_BASE_URL is set and the
    deployment profile is local_test -- the only profile AgentBindingV1 supports."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    wiki_root: str = ""
    tokenizer_path: str = ""
    ollama_model: str = "qwen3.5:9b"
    model_context_window: int = 32_768
    # Not NODE_PATH: Node itself reads that one as its module search path.
    node_binary: str = "node"


def _node_version_ok(node: str) -> bool:
    try:
        completed = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\s*", completed.stdout)
    return bool(match) and tuple(int(part) for part in match.groups()) >= (22, 19, 0)


def build_agent_binding(origin: str, settings: AgentSettings, tokenizer: HfTokenizer) -> AgentBindingV1:
    wiki_root = Path(settings.wiki_root).expanduser().resolve()
    tokenizer_path = Path(settings.tokenizer_path).expanduser().resolve()
    limits = AgentLimitsV1(model_context_window=settings.model_context_window)
    return AgentBindingV1(
        provider_label="Ollama LAN proxy",
        endpoint_origin=origin,
        model_revision=settings.ollama_model,
        model_context_window=limits.model_context_window,
        max_output_tokens=limits.max_output_tokens,
        tokenizer_authority=TokenizerAuthorityV1(name="hf-tokenizers", sha256=tokenizer.sha256, path=str(tokenizer_path)),
        wiki_root=str(wiki_root),
        wiki_display_name=wiki_root.name,
        limits=limits,
    )


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
    """The configuration itself is wrong: an unknown profile, an unusable URL, a
    Close Grace that cannot fit inside the Run Deadline, or a missing/out-of-range deployment setting. Startup refuses --
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
    """The bound Provider for this process.

    Two ways out, and callers must handle both. It RAISES BindingConfigurationError
    for configuration that can never serve: an unknown Profile or an unusable URL. It
    RETURNS an `UnboundProvider` for a missing credential or Wiki agent input -- the
    process should still start so `/ready` can say 503 and name the reason."""
    if settings is None:
        try:
            settings = ProviderSettings()
        except ValidationError as exc:
            # DEPLOYMENT_PROFILE is a Literal, so pydantic-settings refuses an unknown
            # or blank value here rather than downstream. No values in the message.
            raise BindingConfigurationError(
                "Provider 설정이 올바르지 않습니다: DEPLOYMENT_PROFILE"
            ) from exc
    base_url = settings.ollama_base_url.strip()
    if not base_url:
        return FakeAgent()
    origin = endpoint_origin(base_url)
    api_key = settings.credential()
    if not api_key:
        # Never fall back to the deterministic stub: an operator who configured a
        # real endpoint would not notice the swap. Serve 503 instead: no digest, no
        # Tokenizer Authority, no policy, so /ready and /policy answer 503 and no
        # question is ever accepted.
        return UnboundProvider("OLLAMA_API_KEY가 설정되지 않았습니다")
    agent = AgentSettings()
    for name, value in (("WIKI_ROOT", agent.wiki_root), ("TOKENIZER_PATH", agent.tokenizer_path)):
        if not value.strip():
            return UnboundProvider(f"{name}가 설정되지 않았습니다")
    try:
        tokenizer = HfTokenizer(Path(agent.tokenizer_path).expanduser())
    except Exception:
        return UnboundProvider("TOKENIZER_PATH의 tokenizer.json을 읽을 수 없습니다")
    try:
        binding = build_agent_binding(origin, agent, tokenizer)
    except ValueError:
        return UnboundProvider("MODEL_CONTEXT_WINDOW가 AD-30 예산보다 작습니다")
    if not _node_version_ok(agent.node_binary):
        return UnboundProvider("Node 22.19.0 이상이 필요합니다 (NODE_BINARY)")
    return PiSidecarAdapter(binding, api_key, tokenizer, pi_policy_metadata(binding), node=agent.node_binary)


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
        # The message names the offending setting and no value -- which is
        # what makes an empty /ready 503 diagnosable at all.
        _log.error("%s", exc)
        provider = UnboundProvider("Provider Binding 설정이 올바르지 않습니다")
    except Exception as exc:
        _log.error("Provider 설정을 읽을 수 없습니다 (%s)", type(exc).__name__)
        provider = UnboundProvider("Provider 설정을 읽을 수 없습니다")
    if isinstance(provider, UnboundProvider):
        # Story 1.5.1 left `reason` written and never read: an operator saw the same
        # blank 503 for a missing credential, an unreachable endpoint and a bad
        # setting. It is a fixed sentence with no values, so it is safe to log.
        _log.error("%s", provider.reason)
    return ChatApplication(InMemoryConversationStore(), provider, attempts, limits)


__all__ = [
    "DEPLOYMENT_PROFILES",
    "AgentSettings",
    "BindingConfigurationError",
    "DeploymentLimits",
    "DeploymentSettingsV1",
    "ProviderSettings",
    "UnboundProvider",
    "build_agent_binding",
    "build_chat_application",
    "build_deployment_settings",
    "build_provider",
    "endpoint_origin",
]
