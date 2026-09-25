"""Deterministic local_test agent. Same port, same events, no model and no Wiki:
the Run lifecycle, SSE and Web behave exactly as with the pi sidecar."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from uuid import UUID

from aidd_chat.application import (
    SESSION_TTL_SECONDS,
    TRANSMITTED_FIELDS,
    AgentRunError,
    ProviderPolicyMetadata,
)
from aidd_chat.contracts import (
    STEP_LABELS,
    AgentProbeV1,
    AgentResultV1,
    AgentStepV1,
    PreparedModelRequestV1,
    WikiSourceV1,
    compute_provider_profile_digest,
    verify_request_integrity,
)


@dataclass(frozen=True)
class LocalTestBinding:
    """What the deterministic local_test agent is bound to. Tests vary it with
    `dataclasses.replace`; its digest moves with every field."""

    provider_label: str = "Deterministic local test provider"
    model_revision: str = "deterministic-v1"
    tokenizer_authority_name: str = "local-test-codepoint-v1"
    tokenizer_authority_version: str = "1"
    max_input_tokens: int = 8_192
    close_grace_ms: int = 250

    def __post_init__(self) -> None:
        # gt=0, not ge=0: `wait_for(close(), 0)` cancels the close before it can start.
        if self.max_input_tokens < 1 or self.close_grace_ms < 1:
            raise ValueError("max_input_tokens와 close_grace_ms는 양수여야 합니다")


DETERMINISTIC_BINDING = LocalTestBinding()


def local_test_binding_digest(binding: LocalTestBinding) -> str:
    payload = json.dumps(asdict(binding), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(payload.encode("utf-8")).hexdigest()


class LocalTestTokenizerMixin:
    """Tokenizer Authority for the deterministic binding: counts Unicode code points
    in the Canonical Request bytes (`local-test-codepoint-v1`). Identity and window
    come from `self.binding`, never from module constants, so the digest recorded on
    a Run always describes the Authority and window the process actually used."""

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
        return local_test_binding_digest(self.binding)

    @property
    def provider_profile_digest(self) -> str:
        return compute_provider_profile_digest(asdict(self.policy_metadata), self.tokenizer_authority)

    def count_input_tokens(self, request: PreparedModelRequestV1) -> int:
        verify_request_integrity(request, self)
        return len(request.canonical_bytes.decode("utf-8"))


FAKE_SOURCE = WikiSourceV1(
    path="concepts/local-test.md", title="로컬 테스트 문서", confidence=None, contested=False
)


class FakeAgent(LocalTestTokenizerMixin):
    binding = DETERMINISTIC_BINDING

    def __init__(
        self,
        outcome: str = "grounded",
        steps: tuple[AgentStepV1, ...] | None = None,
        answer: str | None = None,
        sources: tuple[WikiSourceV1, ...] | None = None,
        uncovered: str | None = None,
        search_truncated: bool = False,
    ) -> None:
        self.outcome = outcome
        self._steps = steps
        self._answer = answer
        self._sources = sources
        self._uncovered = uncovered if uncovered is not None or outcome != "partial" else "로컬 테스트 범위"
        self._search_truncated = search_truncated
        self._aborts: dict[UUID, asyncio.Event] = {}

    @property
    def close_grace_ms(self) -> int:
        return self.binding.close_grace_ms

    @property
    def policy_metadata(self) -> ProviderPolicyMetadata:
        return ProviderPolicyMetadata(
            schema_version="1",
            provider_label=self.binding.provider_label,
            model_revision=self.binding.model_revision,
            transmitted_fields=TRANSMITTED_FIELDS,
            session_ttl_seconds=SESSION_TTL_SECONDS,
            retrieval_status="wiki_readonly",
            wiki_display_name="local-test-wiki",
        )

    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def probe(self) -> AgentProbeV1:
        return AgentProbeV1(provider_ok=True, wiki_ok=True, context_ok=True)

    def steps_for(self, prepared: PreparedModelRequestV1) -> tuple[AgentStepV1, ...]:
        if self._steps is not None:
            return self._steps
        kinds = ["wiki_index"]
        if self.outcome in ("grounded", "partial"):
            kinds.append("wiki_read")
        kinds.append("decide")
        if self.outcome in ("grounded", "partial"):
            kinds.append("compose")
        steps = []
        for index, kind in enumerate(kinds, start=1):
            if kind == "wiki_read":
                source = self.result_for(prepared).sources[0]
                steps.append(AgentStepV1(step_index=index, kind=kind,
                                         label=STEP_LABELS[kind] + source.title, doc_path=source.path))
            else:
                steps.append(AgentStepV1(step_index=index, kind=kind, label=STEP_LABELS[kind], doc_path=None))
        return tuple(steps)

    def chunks_for(self, prepared: PreparedModelRequestV1) -> tuple[str, ...]:
        return (self._answer if self._answer is not None else f"테스트 응답: {prepared.messages[-1].text}",)

    def result_for(self, prepared: PreparedModelRequestV1) -> AgentResultV1:
        sourced = self.outcome in ("grounded", "partial")
        return AgentResultV1(
            outcome=self.outcome,
            uncovered=self._uncovered if self.outcome == "partial" else None,
            sources=(self._sources or (FAKE_SOURCE,)) if sourced else (),
            search_truncated=self._search_truncated,
            finish="stop",
        )

    @asynccontextmanager
    async def run(self, run_id, correlation_id, prepared, on_step, on_delta):
        verify_request_integrity(prepared, self)
        aborted = self._aborts.setdefault(run_id, asyncio.Event())
        try:
            for step in self.steps_for(prepared):
                if aborted.is_set():
                    raise AgentRunError("provider_incomplete")
                on_step(step)
                await asyncio.sleep(0)
            result = self.result_for(prepared)
            if result.outcome in ("grounded", "partial"):
                for chunk in self.chunks_for(prepared):
                    if aborted.is_set():
                        raise AgentRunError("provider_incomplete")
                    on_delta(chunk)
                    await asyncio.sleep(0)
            if aborted.is_set():
                raise AgentRunError("provider_incomplete")
        finally:
            self._aborts.pop(run_id, None)
        yield result

    async def abort(self, run_id: UUID) -> None:
        event = self._aborts.get(run_id)
        if event is not None:
            event.set()
