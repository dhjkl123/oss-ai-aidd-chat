import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import socket
from threading import Thread
import time
from uuid import uuid1, uuid4

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from aidd_chat.adapters import DETERMINISTIC_BINDING, InMemoryConversationStore, LocalTestBinding
from aidd_chat.application import (
    CONTEXT_TOKEN_BUDGET,
    RUN_DEADLINE,
    ChatApplication,
    ContextTooLarge,
    _Generation,
)
from aidd_chat.contracts import (
    SERIALIZER_DIGEST,
    SYSTEM_INSTRUCTION,
    ContextIntegrityError,
    ContextTruncatedEventV1,
    PreparedMessageV1,
    PreparedModelRequestV1,
    canonical_request_bytes,
    prepare_model_request,
    verify_request_integrity,
)
from aidd_chat.main import app, get_chat_application
from agent_fakes import LegacySyncAgent
from aidd_chat.adapters import FakeAgent


ORIGIN = {"Origin": "https://testserver"}


def _msg(role: str, text: str) -> PreparedMessageV1:
    return PreparedMessageV1(uuid4(), role, text)


def _window(max_input_tokens: int) -> LocalTestBinding:
    """A deterministic binding with a different advertised window."""
    return replace(DETERMINISTIC_BINDING, max_input_tokens=max_input_tokens)


def _request(*messages: PreparedMessageV1, provider: object = None) -> PreparedModelRequestV1:
    provider = provider or FakeAgent()
    return prepare_model_request(messages, provider_profile_digest=provider.provider_profile_digest)


class RecordingProvider(LegacySyncAgent):
    """A local_test-style Provider with a controllable max_input_tokens, so the
    History budget algorithm can be exercised without giant strings. Always
    streams, recording every PreparedModelRequestV1 it actually consumed."""

    policy_metadata = FakeAgent().policy_metadata

    binding = DETERMINISTIC_BINDING

    def __init__(self, max_input_tokens: int, output: str = "완료 답변") -> None:
        self.binding = _window(max_input_tokens)
        self.output = output
        self.requests: list[PreparedModelRequestV1] = []
        self.fail_next = False

    def probe(self) -> bool:
        return True

    def stream(self, request: PreparedModelRequestV1, on_delta, handle=None) -> str:
        verify_request_integrity(request, self)
        self.requests.append(request)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("provider down")
        on_delta(self.output)
        return self.output


class DriftingProvider(LegacySyncAgent):
    """provider_profile_digest changes between when a request is built and when
    complete() is asked to consume it, simulating a corrupted binding -- but only
    after the Gate has already accepted the question (Run generation path)."""

    policy_metadata = FakeAgent().policy_metadata

    binding = DETERMINISTIC_BINDING

    def __init__(self) -> None:
        self.digest = "stable-digest"

    @property
    def provider_profile_digest(self) -> str:
        return self.digest

    def probe(self) -> bool:
        return True

    def complete(self, request: PreparedModelRequestV1, handle=None) -> str:
        self.digest = "drifted-digest"
        verify_request_integrity(request, self)
        return "안 옴"


class AlwaysDriftingProvider(LegacySyncAgent):
    """provider_profile_digest changes on every single read, so even the
    System+Current Gate (before any Run exists) sees a mismatch between what it
    embedded in the request and what count_input_tokens() recomputes."""

    policy_metadata = FakeAgent().policy_metadata

    binding = DETERMINISTIC_BINDING

    def __init__(self) -> None:
        self._reads = 0

    @property
    def provider_profile_digest(self) -> str:
        self._reads += 1
        return f"digest-{self._reads}"

    def probe(self) -> bool:
        return True

    def complete(self, request: PreparedModelRequestV1, handle=None) -> str:
        return "도달 불가"


class NoCounterProvider:
    """Satisfies everything except the Tokenizer Authority's count_input_tokens."""

    policy_metadata = FakeAgent().policy_metadata

    binding = DETERMINISTIC_BINDING
    tokenizer_authority = ("no-counter", "0", CONTEXT_TOKEN_BUDGET)
    provider_profile_digest = "fixed-digest"

    def probe(self) -> bool:
        return True

    def complete(self, _request: PreparedModelRequestV1, handle=None) -> str:
        return "무시됨"


class NoProfileDigestProvider:
    """Satisfies everything except provider_profile_digest -- required by the
    request path (verify_request_integrity), so readiness must not pass without it."""

    policy_metadata = FakeAgent().policy_metadata

    binding = DETERMINISTIC_BINDING
    tokenizer_authority = ("no-digest", "1", CONTEXT_TOKEN_BUDGET)

    def probe(self) -> bool:
        return True

    def count_input_tokens(self, _request: PreparedModelRequestV1) -> int:
        return 0

    def complete(self, _request: PreparedModelRequestV1, handle=None) -> str:
        return "무시됨"


class NoTokenizerAuthorityProvider:
    """Satisfies everything except tokenizer_authority."""

    policy_metadata = FakeAgent().policy_metadata

    binding = DETERMINISTIC_BINDING
    provider_profile_digest = "fixed-digest"

    def probe(self) -> bool:
        return True

    def count_input_tokens(self, _request: PreparedModelRequestV1) -> int:
        return 0

    def complete(self, _request: PreparedModelRequestV1, handle=None) -> str:
        return "무시됨"


class UnderBudgetProvider(LegacySyncAgent):
    policy_metadata = FakeAgent().policy_metadata
    binding = _window(CONTEXT_TOKEN_BUDGET - 1)

    def probe(self) -> bool:
        return True

    def complete(self, _request: PreparedModelRequestV1, handle=None) -> str:
        return "무시됨"


class LargeWindowProvider(LegacySyncAgent):
    """Advertises a token window far larger than the story's fixed budget -- the
    kind of Provider Story 1.11 might bind. The effective budget must still be
    capped at CONTEXT_TOKEN_BUDGET, never raised by a roomier Provider."""

    policy_metadata = FakeAgent().policy_metadata

    binding = _window(50_000)

    def probe(self) -> bool:
        return True

    def complete(self, _request: PreparedModelRequestV1, handle=None) -> str:
        return "무시됨"


class DroppingTruncationStore(InMemoryConversationStore):
    """commit_context_truncated() always reports failure (as it would for an
    expired Conversation or a Run that stopped being active mid-selection)."""

    def commit_context_truncated(self, *_args, **_kwargs) -> bool:
        return False


class MissingHistoryStore(InMemoryConversationStore):
    """get_context_snapshot() always reports the Conversation gone/expired,
    simulating the race between mark_running succeeding and History being read."""

    def get_context_snapshot(self, _conversation_id, _now):
        return None


class InconsistentAuthorityProvider(LegacySyncAgent):
    """tokenizer_authority's own window disagrees with max_input_tokens -- exactly
    the drift provider_profile_digest exists to detect; readiness must reject it
    before any request is ever built."""

    policy_metadata = FakeAgent().policy_metadata

    binding = DETERMINISTIC_BINDING

    @property
    def tokenizer_authority(self) -> tuple[str, str, int]:
        return ("inconsistent", "1", CONTEXT_TOKEN_BUDGET + 1)

    def probe(self) -> bool:
        return True

    def complete(self, _request: PreparedModelRequestV1, handle=None) -> str:
        return "무시됨"


class BrokenCounterProvider(LegacySyncAgent):
    """count_input_tokens() raises something other than ContextIntegrityError --
    the System+Current Gate must still map this to a closed envelope, not a
    bare, envelope-free 500."""

    policy_metadata = FakeAgent().policy_metadata

    binding = DETERMINISTIC_BINDING

    def probe(self) -> bool:
        return True

    def count_input_tokens(self, _request: PreparedModelRequestV1) -> int:
        raise RuntimeError("boom")

    def complete(self, _request: PreparedModelRequestV1, handle=None) -> str:
        return "도달 불가"


class DriftingAuthorityProvider(LegacySyncAgent):
    """tokenizer_authority's window changes between when a request is built
    (embedding provider_profile_digest) and when complete() verifies it --
    proving compute_provider_profile_digest() actually folds tokenizer_authority
    into the digest. Deleting that key from the payload would let this drift
    through unnoticed."""

    policy_metadata = FakeAgent().policy_metadata

    binding = DETERMINISTIC_BINDING

    def __init__(self) -> None:
        self.binding = DETERMINISTIC_BINDING

    def probe(self) -> bool:
        return True

    def complete(self, request: PreparedModelRequestV1, handle=None) -> str:
        self.binding = _window(CONTEXT_TOKEN_BUDGET + 1)
        verify_request_integrity(request, self)
        return "도달 불가"


def _overhead_codepoints() -> int:
    provider = FakeAgent()
    request = _request(_msg("user", ""), provider=provider)
    return provider.count_input_tokens(request)


def _korean_of_length(total_tokens: int) -> str:
    return "가" * (total_tokens - _overhead_codepoints())


def _content_with_escapes_at_token_count(total_tokens: int) -> str:
    """`total_tokens` code points of *actual* usage, guaranteed to contain a
    literal `"`, `\\`, and newline -- each of which escapes to two JSON
    characters, so a naive raw-character-count estimate would undercount this."""
    provider = FakeAgent()
    overhead = _overhead_codepoints()
    suffix = '"\\\n'
    suffix_cost = provider.count_input_tokens(_request(_msg("user", suffix), provider=provider)) - overhead
    filler_count = total_tokens - overhead - suffix_cost
    assert filler_count >= 0
    return "가" * filler_count + suffix


def test_first_question_has_no_history_and_no_truncated_event() -> None:
    provider = RecordingProvider(CONTEXT_TOKEN_BUDGET)
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()

    accepted = application.submit_question(conversation.conversation_id, capability, "first", "첫질문")
    application.wait_for_generations()

    _final, events = application.get_run_snapshot(accepted.run_id, capability)
    assert "context.truncated" not in [event.type for event in events]
    assert [(message.role, message.text) for message in provider.requests[-1].messages] == [
        ("user", "첫질문")
    ]


def test_followup_within_budget_includes_all_completed_pairs_in_order() -> None:
    provider = RecordingProvider(CONTEXT_TOKEN_BUDGET)
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()

    for index in range(1, 3):
        provider.output = f"답{index}"
        application.submit_question(conversation.conversation_id, capability, f"turn-{index}", f"질문{index}")
        application.wait_for_generations()

    accepted = application.submit_question(conversation.conversation_id, capability, "turn-3", "질문3")
    application.wait_for_generations()

    _final, events = application.get_run_snapshot(accepted.run_id, capability)
    assert "context.truncated" not in [event.type for event in events]
    assert [(message.role, message.text) for message in provider.requests[-1].messages] == [
        ("user", "질문1"),
        ("assistant", "답1"),
        ("user", "질문2"),
        ("assistant", "답2"),
        ("user", "질문3"),
    ]


def test_completed_turns_excludes_failed_and_unmatched_turns() -> None:
    provider = RecordingProvider(CONTEXT_TOKEN_BUDGET)
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()

    provider.fail_next = True
    application.submit_question(conversation.conversation_id, capability, "fails", "실패할질문")
    application.wait_for_generations()

    provider.output = "완료 답변"
    application.submit_question(conversation.conversation_id, capability, "ok", "성공질문")
    application.wait_for_generations()

    turns = store.states[conversation.conversation_id].completed_turns(datetime.now(UTC))
    assert [(user.text, assistant.text) for user, assistant in turns] == [("성공질문", "완료 답변")]


def test_history_over_budget_keeps_newest_contiguous_turns_and_truncates_once() -> None:
    provider = RecordingProvider(CONTEXT_TOKEN_BUDGET)
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()

    for index in range(1, 4):
        provider.output = f"답{index}"
        application.submit_question(conversation.conversation_id, capability, f"turn-{index}", f"질문{index}")
        application.wait_for_generations()

    turn2 = (_msg("user", "질문2"), _msg("assistant", "답2"))
    turn3 = (_msg("user", "질문3"), _msg("assistant", "답3"))
    current = _msg("user", "질문4")

    def count(*turns: tuple[PreparedMessageV1, PreparedMessageV1]) -> int:
        messages = tuple(message for pair in turns for message in pair) + (current,)
        request = prepare_model_request(
            messages, provider_profile_digest=provider.provider_profile_digest
        )
        return provider.count_input_tokens(request)

    budget = count(turn3)
    assert count(turn2, turn3) > budget
    provider.binding = _window(budget)

    accepted = application.submit_question(conversation.conversation_id, capability, "turn-4", "질문4")
    application.wait_for_generations()

    final, events = application.get_run_snapshot(accepted.run_id, capability)
    assert final.state == "completed"
    assert [event.type for event in events] == [
        "run.status", "context.truncated", "message.delta", "message.sources", "message.completed",
        "run.status", "stream.end",
    ]
    truncated = events[1]
    assert isinstance(truncated, ContextTruncatedEventV1)
    assert truncated.dropped_turn_count == 2
    assert [(message.role, message.text) for message in provider.requests[-1].messages] == [
        ("user", "질문3"),
        ("assistant", "답3"),
        ("user", "질문4"),
    ]


def test_history_selection_stops_at_first_gap_not_skip_over_a_large_middle_turn() -> None:
    """A buggy `continue` (skip the Turn that doesn't fit, keep trying older ones)
    would happily include an old, small Turn behind a large one it skipped -- the
    correct `break` keeps only a contiguous run of the newest Turns."""
    provider = RecordingProvider(CONTEXT_TOKEN_BUDGET)
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()

    huge_question = "질문2" + "가" * 5_000
    for index, question in enumerate(("질문1", huge_question, "질문3"), start=1):
        provider.output = f"답{index}"
        application.submit_question(conversation.conversation_id, capability, f"gap-turn-{index}", question)
        application.wait_for_generations()

    turn1 = (_msg("user", "질문1"), _msg("assistant", "답1"))
    turn2 = (_msg("user", huge_question), _msg("assistant", "답2"))
    turn3 = (_msg("user", "질문3"), _msg("assistant", "답3"))
    current = _msg("user", "질문4")

    def count(*turns: tuple[PreparedMessageV1, PreparedMessageV1]) -> int:
        messages = tuple(message for pair in turns for message in pair) + (current,)
        request = prepare_model_request(
            messages, provider_profile_digest=provider.provider_profile_digest
        )
        return provider.count_input_tokens(request)

    # turn1 + turn3 fits; the huge turn2 alone does not -- a `continue` bug would
    # skip turn2 and still add turn1, since it individually fits this budget.
    budget = count(turn1, turn3)
    assert count(turn2, turn3) > budget
    provider.binding = _window(budget)

    accepted = application.submit_question(conversation.conversation_id, capability, "gap-turn-4", "질문4")
    application.wait_for_generations()

    final, events = application.get_run_snapshot(accepted.run_id, capability)
    assert final.state == "completed"
    truncated = [event for event in events if event.type == "context.truncated"]
    assert len(truncated) == 1 and truncated[0].dropped_turn_count == 2
    assert [(message.role, message.text) for message in provider.requests[-1].messages] == [
        ("user", "질문3"),
        ("assistant", "답3"),
        ("user", "질문4"),
    ]


def test_current_message_uses_the_runs_real_input_message_id() -> None:
    """_ensure_within_budget() runs before the Run exists and keeps a synthetic
    id, but by generation time the real input Message exists -- traceability
    should point at it, not a throwaway uuid4()."""
    provider = RecordingProvider(CONTEXT_TOKEN_BUDGET)
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()

    accepted = application.submit_question(conversation.conversation_id, capability, "traceable", "질문")
    application.wait_for_generations()

    assert provider.requests[-1].messages[-1].message_id == accepted.input_message_id


def test_provider_advertising_larger_window_still_caps_history_at_context_token_budget() -> None:
    provider = RecordingProvider(max_input_tokens=50_000)
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()

    filler = "가" * 3_000
    for index in (1, 2):
        provider.output = f"답{index}"
        application.submit_question(
            conversation.conversation_id, capability, f"turn-{index}", f"{filler}{index}"
        )
        application.wait_for_generations()

    turn1 = (_msg("user", f"{filler}1"), _msg("assistant", "답1"))
    turn2 = (_msg("user", f"{filler}2"), _msg("assistant", "답2"))
    current = _msg("user", f"{filler}3")

    def count(*turns: tuple[PreparedMessageV1, PreparedMessageV1]) -> int:
        messages = tuple(message for pair in turns for message in pair) + (current,)
        request = prepare_model_request(
            messages, provider_profile_digest=provider.provider_profile_digest
        )
        return provider.count_input_tokens(request)

    # This Provider's own max_input_tokens (50,000) would happily fit every turn --
    # the fixture straddles the fixed 8,192 budget on purpose.
    assert count(turn2) <= CONTEXT_TOKEN_BUDGET < count(turn1, turn2)

    accepted = application.submit_question(conversation.conversation_id, capability, "turn-3", f"{filler}3")
    application.wait_for_generations()

    final, events = application.get_run_snapshot(accepted.run_id, capability)
    assert final.state == "completed"
    truncated = [event for event in events if event.type == "context.truncated"]
    assert len(truncated) == 1 and truncated[0].dropped_turn_count == 1
    assert [(message.role, message.text) for message in provider.requests[-1].messages] == [
        ("user", f"{filler}2"),
        ("assistant", "답2"),
        ("user", f"{filler}3"),
    ]


def test_provider_advertising_larger_window_still_rejects_gate_at_8193() -> None:
    application = ChatApplication(InMemoryConversationStore(), LargeWindowProvider(), 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            for index, total in enumerate((CONTEXT_TOKEN_BUDGET, CONTEXT_TOKEN_BUDGET + 1)):
                response = client.post(
                    f"/api/v1/conversations/{conversation_id}/runs",
                    headers={**ORIGIN, "Idempotency-Key": f"large-window-{index}"},
                    json={"kind": "question", "content": _korean_of_length(total)},
                )
                application.wait_for_generations()
                if total <= CONTEXT_TOKEN_BUDGET:
                    assert response.status_code == 202, total
                else:
                    assert response.status_code == 413, total
                    assert response.json()["error"]["code"] == "context_too_large"
    finally:
        app.dependency_overrides.pop(get_chat_application, None)


def test_context_truncated_commit_failure_fails_run_without_calling_provider() -> None:
    provider = RecordingProvider(CONTEXT_TOKEN_BUDGET)
    store = DroppingTruncationStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()

    for index in (1, 2):
        provider.output = f"답{index}"
        application.submit_question(conversation.conversation_id, capability, f"turn-{index}", f"질문{index}")
        application.wait_for_generations()

    turn2 = (_msg("user", "질문2"), _msg("assistant", "답2"))
    current = _msg("user", "질문3")
    request = prepare_model_request(
        (*turn2, current), provider_profile_digest=provider.provider_profile_digest
    )
    provider.binding = _window(provider.count_input_tokens(request))  # forces >=1 dropped Turn

    calls_before = len(provider.requests)
    accepted = application.submit_question(conversation.conversation_id, capability, "turn-3", "질문3")
    application.wait_for_generations()

    failed = application.get_run(accepted.run_id, capability)
    assert failed.state == "failed"
    assert failed.terminal_error.kind == "provider_unknown"
    assert len(provider.requests) == calls_before


def test_select_context_request_verifies_the_returned_request_fits_budget() -> None:
    """If a Provider's window is smaller than even "system + current alone" by
    the time _generate() selects context (a rebind, or a window that shrank
    after the submit Gate passed), the Run must fail rather than send an
    over-budget request -- the greedy loop only ever verifies *candidates that
    add a Turn*, never the returned baseline itself."""
    provider = RecordingProvider(max_input_tokens=1)
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()
    capability_hash = sha256(capability.encode()).hexdigest()
    accepted = store.accept_question(
        conversation.conversation_id, capability_hash, "shrunk", "digest", "질문",
        datetime.now(UTC), correlation_id=uuid4(),
    ).run
    # _generate reads the Run's Correlation ID from its registered entry.
    application._generation_tasks[accepted.run_id] = _Generation(conversation.conversation_id, uuid4())

    application.agent_loop.submit(application._generate(
        conversation.conversation_id,
        accepted.run_id,
        accepted.input_message_id,
        "질문",
        time.monotonic() + RUN_DEADLINE.total_seconds(),
    )).result(5)

    failed = application.get_run(accepted.run_id, capability)
    assert failed.state == "failed"
    assert failed.terminal_error.kind == "provider_unknown"
    assert provider.requests == []


def test_get_context_snapshot_distinguishes_missing_and_expired_from_empty_history() -> None:
    store = InMemoryConversationStore()
    assert store.get_context_snapshot(uuid4(), datetime.now(UTC)) is None  # missing entirely

    application = ChatApplication(store, FakeAgent(), 3)
    conversation, _capability = application.create_conversation()
    assert store.get_context_snapshot(conversation.conversation_id, datetime.now(UTC)) == ()  # valid, empty

    # Story 1.8 enforces the Absolute Expiry on the monotonic clock, so a stepped
    # wall clock can neither suspend expiry nor purge everything at once. There is
    # no wall time to pass any more -- the Conversation is expired or it is not.
    store.states[conversation.conversation_id].expires_monotonic = time.monotonic() - 1
    assert store.get_context_snapshot(conversation.conversation_id, datetime.now(UTC)) is None


def test_generate_fails_run_when_context_snapshot_reports_conversation_gone() -> None:
    provider = RecordingProvider(CONTEXT_TOKEN_BUDGET)
    store = MissingHistoryStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()
    capability_hash = sha256(capability.encode()).hexdigest()
    accepted = store.accept_question(
        conversation.conversation_id, capability_hash, "gone-mid-gen", "digest", "질문",
        datetime.now(UTC), correlation_id=uuid4(),
    ).run
    # _generate reads the Run's Correlation ID from its registered entry.
    application._generation_tasks[accepted.run_id] = _Generation(conversation.conversation_id, uuid4())

    application.agent_loop.submit(application._generate(
        conversation.conversation_id,
        accepted.run_id,
        accepted.input_message_id,
        "질문",
        time.monotonic() + RUN_DEADLINE.total_seconds(),
    )).result(5)

    failed = application.get_run(accepted.run_id, capability)
    assert failed.state == "failed"
    assert failed.terminal_error.kind == "provider_unknown"
    assert provider.requests == []


def test_commit_context_truncated_rejects_after_expiry() -> None:
    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)
    conversation, capability = application.create_conversation()
    capability_hash = sha256(capability.encode()).hexdigest()
    accepted = store.accept_question(
        conversation.conversation_id, capability_hash, "expiry-fence", "digest", "질문",
        datetime.now(UTC), correlation_id=uuid4(),
    ).run
    assert store.mark_running(conversation.conversation_id, accepted.run_id, datetime.now(UTC), time.monotonic())

    state = store.states[conversation.conversation_id]
    state.expires_at = datetime.now(UTC) - timedelta(microseconds=1)
    # Story 1.8 enforces the Absolute Expiry on the monotonic clock, so a wall
    # step cannot suspend expiry or purge everything at once. Expire both.
    state.expires_monotonic = time.monotonic() - 1
    run = state.runs[accepted.run_id]
    before = tuple(run.events)

    assert store.commit_context_truncated(conversation.conversation_id, accepted.run_id, 2, datetime.now(UTC)) is False
    assert tuple(run.events) == before


def test_provider_with_inconsistent_tokenizer_authority_window_fails_readiness() -> None:
    application = ChatApplication(InMemoryConversationStore(), InconsistentAuthorityProvider(), 3)
    assert application.is_ready() is False


def test_tokenizer_authority_drift_between_build_and_consume_is_detected() -> None:
    provider = DriftingAuthorityProvider()
    request = _request(_msg("user", "질문"), provider=provider)
    with pytest.raises(ContextIntegrityError):
        provider.complete(request)


def test_gate_arbitrary_exception_maps_to_503_not_bare_500() -> None:
    application = ChatApplication(InMemoryConversationStore(), BrokenCounterProvider(), 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            response = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers={**ORIGIN, "Idempotency-Key": "broken-counter"},
                json={"kind": "question", "content": "질문"},
            )
    finally:
        app.dependency_overrides.pop(get_chat_application, None)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"
    assert response.json()["error"]["retryable"] is False


@pytest.mark.release_suite
def test_new_conversation_does_not_reuse_other_conversations_history_or_ttl() -> None:
    provider = RecordingProvider(CONTEXT_TOKEN_BUDGET)
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)

    first, first_capability = application.create_conversation()
    application.submit_question(first.conversation_id, first_capability, "a-1", "첫대화질문")
    application.wait_for_generations()
    first_expires_before = store.states[first.conversation_id].expires_at

    second, second_capability = application.create_conversation()
    application.submit_question(second.conversation_id, second_capability, "b-1", "새대화질문")
    application.wait_for_generations()

    assert [(message.role, message.text) for message in provider.requests[-1].messages] == [
        ("user", "새대화질문")
    ]
    assert store.states[first.conversation_id].expires_at == first_expires_before == first.expires_at


def test_system_current_over_budget_rejects_without_run_or_message() -> None:
    application = ChatApplication(InMemoryConversationStore(), FakeAgent(), 3)
    conversation, capability = application.create_conversation()

    with pytest.raises(ContextTooLarge):
        application.submit_question(conversation.conversation_id, capability, "huge", "가" * 20_000)

    state = application.store.states[conversation.conversation_id]
    assert state.messages == [] and state.runs == {} and state.receipts == {}


def test_korean_boundary_vector_8191_8192_accepted_8193_rejected() -> None:
    application = ChatApplication(InMemoryConversationStore(), FakeAgent(), 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            for index, total in enumerate((8_191, 8_192, 8_193)):
                response = client.post(
                    f"/api/v1/conversations/{conversation_id}/runs",
                    headers={**ORIGIN, "Idempotency-Key": f"boundary-{index}"},
                    json={"kind": "question", "content": _korean_of_length(total)},
                )
                application.wait_for_generations()
                if total <= CONTEXT_TOKEN_BUDGET:
                    assert response.status_code == 202, total
                else:
                    assert response.status_code == 413, total
                    assert response.json()["error"]["code"] == "context_too_large"
                    assert response.headers["cache-control"] == "no-store"
    finally:
        app.dependency_overrides.pop(get_chat_application, None)


def test_json_escaped_characters_boundary_diverges_from_naive_character_count() -> None:
    application = ChatApplication(InMemoryConversationStore(), FakeAgent(), 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            for index, total in enumerate((CONTEXT_TOKEN_BUDGET, CONTEXT_TOKEN_BUDGET + 1)):
                content = _content_with_escapes_at_token_count(total)
                assert '"' in content and "\\" in content and "\n" in content
                response = client.post(
                    f"/api/v1/conversations/{conversation_id}/runs",
                    headers={**ORIGIN, "Idempotency-Key": f"escaped-boundary-{index}"},
                    json={"kind": "question", "content": content},
                )
                application.wait_for_generations()
                if total <= CONTEXT_TOKEN_BUDGET:
                    assert response.status_code == 202, total
                else:
                    assert response.status_code == 413, total
                    assert response.json()["error"]["code"] == "context_too_large"
    finally:
        app.dependency_overrides.pop(get_chat_application, None)


def test_gate_digest_mismatch_poisons_readiness_before_any_run_exists() -> None:
    application = ChatApplication(InMemoryConversationStore(), AlwaysDriftingProvider(), 3)
    conversation, capability = application.create_conversation()

    with pytest.raises(ContextIntegrityError):
        application.submit_question(conversation.conversation_id, capability, "gate-drift", "질문")

    assert application.is_ready() is False
    state = application.store.states[conversation.conversation_id]
    assert state.messages == [] and state.runs == {}


def test_gate_digest_mismatch_returns_503_provider_unavailable_over_http() -> None:
    application = ChatApplication(InMemoryConversationStore(), AlwaysDriftingProvider(), 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            response = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers={**ORIGIN, "Idempotency-Key": "gate-drift-http"},
                json={"kind": "question", "content": "질문"},
            )
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"
    assert response.headers["cache-control"] == "no-store"


def test_digest_mismatch_fails_run_provider_unknown_and_poisons_readiness() -> None:
    provider = DriftingProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()
    assert application.is_ready() is True

    accepted = application.submit_question(conversation.conversation_id, capability, "drift", "질문")
    application.wait_for_generations()

    failed = application.get_run(accepted.run_id, capability)
    assert failed.state == "failed"
    assert failed.terminal_error.kind == "provider_unknown"
    assert application.is_ready() is False


def test_provider_without_counter_fails_readiness() -> None:
    application = ChatApplication(InMemoryConversationStore(), NoCounterProvider(), 3)
    assert application.is_ready() is False


@pytest.mark.parametrize("provider_cls", [NoProfileDigestProvider, NoTokenizerAuthorityProvider])
def test_provider_missing_profile_digest_or_tokenizer_authority_fails_readiness(provider_cls) -> None:
    application = ChatApplication(InMemoryConversationStore(), provider_cls(), 3)
    assert application.is_ready() is False


def test_provider_below_token_budget_fails_readiness_and_blocks_http_questions() -> None:
    application = ChatApplication(InMemoryConversationStore(), UnderBudgetProvider(), 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            ready = client.get("/ready")
            blocked = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers={**ORIGIN, "Idempotency-Key": "under-budget"},
                json={"kind": "question", "content": "질문"},
            )
    finally:
        app.dependency_overrides.pop(get_chat_application, None)
    assert ready.status_code == 503
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "provider_unavailable"


def _run_agent(agent, request, on_delta) -> None:
    async def drive() -> None:
        async with agent.run(uuid4(), uuid4(), request, lambda _step: None, on_delta):
            pass

    asyncio.run(drive())


def test_count_input_tokens_and_stream_consume_identical_canonical_bytes() -> None:
    adapter = FakeAgent()
    request = _request(
        _msg("user", "이전 질문"), _msg("assistant", "이전 답변"), _msg("user", "질문"), provider=adapter
    )
    count = adapter.count_input_tokens(request)
    assert count == len(request.canonical_bytes.decode("utf-8"))
    seen = []
    _run_agent(adapter, request, seen.append)
    assert seen == ["테스트 응답: 질문"]


def test_history_turn_text_is_normalized_once_for_counting_and_provider_send() -> None:
    class RawAssistantProvider(LegacySyncAgent):
        """Turn 1 answers with un-normalized (NFD + CRLF) text, exactly as Story 1.4
        commits a streamed answer raw. Turn 2's request is what we inspect."""

        policy_metadata = FakeAgent().policy_metadata

        binding = DETERMINISTIC_BINDING

        def __init__(self) -> None:
            self.requests: list[PreparedModelRequestV1] = []
            self.turn = 0

        def probe(self) -> bool:
            return True

        def stream(self, request: PreparedModelRequestV1, on_delta, handle=None) -> str:
            self.requests.append(request)
            self.turn += 1
            if self.turn == 1:
                raw = "가\r\n나"  # NFD 가 + CRLF + NFC 나
                on_delta(raw)
                return raw
            on_delta("두번째 답")
            return "두번째 답"

    provider = RawAssistantProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()

    application.submit_question(conversation.conversation_id, capability, "raw-1", "질문1")
    application.wait_for_generations()
    accepted = application.submit_question(conversation.conversation_id, capability, "raw-2", "질문2")
    application.wait_for_generations()
    assert application.get_run(accepted.run_id, capability).state == "completed"

    sent = provider.requests[-1]
    history_assistant_text = sent.messages[1].text
    assert history_assistant_text == "가\n나"
    assert sent.canonical_bytes == canonical_request_bytes(sent.system_instruction, sent.messages)


def _swap_messages_stale_canonical_bytes(
    base: PreparedModelRequestV1, other: PreparedModelRequestV1
) -> PreparedModelRequestV1:
    # messages + context_digest are swapped in from `other` (self-consistent for
    # `other`'s content); canonical_bytes is left as `base`'s stale bytes. Only the
    # canonical_bytes recompute-and-compare clause catches this.
    return replace(base, messages=other.messages, context_digest=other.context_digest)


def _self_consistent_request(
    messages: tuple[PreparedMessageV1, ...], provider: object
) -> PreparedModelRequestV1:
    """A fully self-consistent PreparedModelRequestV1 for a `messages` shape that
    prepare_model_request() itself would refuse -- built by hand so only
    verify_request_integrity()'s own re-validation can catch it."""
    canonical_bytes = canonical_request_bytes(SYSTEM_INSTRUCTION, messages)
    return PreparedModelRequestV1(
        schema_version="1",
        system_instruction=SYSTEM_INSTRUCTION,
        messages=messages,
        canonical_bytes=canonical_bytes,
        serializer_digest=SERIALIZER_DIGEST,
        context_digest=sha256(canonical_bytes).hexdigest(),
        provider_profile_digest=provider.provider_profile_digest,
    )


_TAMPER_CASES = {
    "context_digest_only": lambda base, _other: replace(base, context_digest="0" * 64),
    "canonical_bytes_stale_for_swapped_messages": _swap_messages_stale_canonical_bytes,
    "wrong_serializer_digest": lambda base, _other: replace(base, serializer_digest="0" * 64),
    "swapped_system_instruction": lambda base, _other: replace(base, system_instruction="다른 지시문입니다"),
    "tampered_schema_version": lambda base, _other: replace(base, schema_version="2"),
    "empty_messages": lambda base, _other: _self_consistent_request((), FakeAgent()),
    "last_message_not_user": lambda base, _other: _self_consistent_request(
        (_msg("user", "질문"), _msg("assistant", "마지막")), FakeAgent()
    ),
}


@pytest.mark.parametrize("build_tampered", _TAMPER_CASES.values(), ids=_TAMPER_CASES.keys())
def test_tampered_request_variants_fail_closed_for_tokenizer_and_provider_calls(build_tampered) -> None:
    adapter = FakeAgent()
    base = _request(_msg("user", "질문"), provider=adapter)
    other = _request(
        _msg("user", "다른 질문"), _msg("assistant", "다른 답"), _msg("user", "또 다른 질문"), provider=adapter
    )
    tampered = build_tampered(base, other)

    with pytest.raises(ContextIntegrityError):
        adapter.count_input_tokens(tampered)
    # One agent call path now (run), where complete() and stream() were two.
    with pytest.raises(ContextIntegrityError):
        _run_agent(adapter, tampered, lambda _delta: None)


def test_prepare_model_request_rejects_empty_or_badly_shaped_messages() -> None:
    with pytest.raises(ValueError):
        prepare_model_request((), provider_profile_digest="digest")
    with pytest.raises(ValueError):
        # last message must be the current (user) input
        prepare_model_request(
            (_msg("user", "질문"), _msg("assistant", "마지막이 아님")), provider_profile_digest="digest"
        )
    with pytest.raises(ValueError):
        # any role outside user/assistant is rejected, even mid-history
        prepare_model_request(
            (PreparedMessageV1(uuid4(), "system", "금지"), _msg("user", "질문")),
            provider_profile_digest="digest",
        )


def test_canonical_request_bytes_pinned_byte_shape() -> None:
    messages = (PreparedMessageV1(uuid4(), "user", "안녕"),)
    expected = (
        '{"schema_version":"1","system_instruction":"지시문",'
        '"messages":[{"role":"user","text":"안녕"}]}'
    ).encode("utf-8")
    assert canonical_request_bytes("지시문", messages) == expected


def test_overhead_codepoints_matches_hardcoded_canonical_shape() -> None:
    # Independent of canonical_request_bytes()/count_input_tokens(): the exact JSON
    # shape is hand-built here (field order, punctuation) so a broken serializer or
    # counter implementation can't rubber-stamp itself via a shared formula.
    expected_json = (
        '{"schema_version":"1","system_instruction":"' + SYSTEM_INSTRUCTION + '",'
        '"messages":[{"role":"user","text":""}]}'
    )
    assert _overhead_codepoints() == len(expected_json)


def test_commit_context_truncated_fires_at_most_once_per_run() -> None:
    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)
    conversation, capability = application.create_conversation()
    capability_hash = sha256(capability.encode()).hexdigest()
    accepted = store.accept_question(
        conversation.conversation_id, capability_hash, "fence", "digest", "질문",
        datetime.now(UTC), correlation_id=uuid4(),
    ).run
    assert store.mark_running(conversation.conversation_id, accepted.run_id, datetime.now(UTC), time.monotonic())

    assert store.commit_context_truncated(conversation.conversation_id, accepted.run_id, 2, datetime.now(UTC)) is True
    assert store.commit_context_truncated(conversation.conversation_id, accepted.run_id, 5, datetime.now(UTC)) is False

    run = store.states[conversation.conversation_id].runs[accepted.run_id]
    assert [event.type for event in run.events] == ["run.status", "context.truncated"]
    assert run.events[1].dropped_turn_count == 2


def test_context_truncated_event_closed_contract() -> None:
    now = datetime.now(UTC)
    event = ContextTruncatedEventV1(run_id=uuid4(), sequence=2, occurred_at=now, dropped_turn_count=3)
    assert event.type == "context.truncated"
    with pytest.raises(ValueError):
        ContextTruncatedEventV1(run_id=uuid4(), sequence=2, occurred_at=now, dropped_turn_count=0)
    with pytest.raises(ValueError):
        ContextTruncatedEventV1(run_id=uuid1(), sequence=1, occurred_at=now, dropped_turn_count=1)



@contextmanager
def _tcp_server(application: ChatApplication):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    missing = object()
    previous = getattr(app.state, "chat_application", missing)
    app.state.chat_application = application
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(5)
        if previous is missing:
            if hasattr(app.state, "chat_application"):
                del app.state.chat_application
        else:
            app.state.chat_application = previous


def test_real_tcp_sse_emits_context_truncated_before_first_delta() -> None:
    # max_input_tokens stays at the real CONTEXT_TOKEN_BUDGET throughout (unlike
    # the synthetic-budget unit tests above) -- shrinking it would fail the
    # readiness floor and this exercises the real fixed budget end to end.
    provider = RecordingProvider(CONTEXT_TOKEN_BUDGET)
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    filler = "가" * 3_000
    with _tcp_server(application) as base_url, httpx.Client(base_url=base_url, timeout=3) as client:
        created = client.post("/api/v1/conversations", headers={"Origin": base_url})
        capability = created.headers["set-cookie"].split("conversation_capability=", 1)[1].split(";", 1)[0]
        conversation_id = created.json()["conversation_id"]
        post_headers = {"Origin": base_url, "Cookie": f"conversation_capability={capability}"}
        get_headers = {"Cookie": f"conversation_capability={capability}"}

        for index in (1, 2):
            provider.output = f"답{index}"
            client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers={**post_headers, "Idempotency-Key": f"tcp-turn-{index}"},
                json={"kind": "question", "content": f"{filler}{index}"},
            )
            application.wait_for_generations()

        turn1 = (_msg("user", f"{filler}1"), _msg("assistant", "답1"))
        turn2 = (_msg("user", f"{filler}2"), _msg("assistant", "답2"))
        current = _msg("user", f"{filler}3")

        def count(*turns: tuple[PreparedMessageV1, PreparedMessageV1]) -> int:
            messages = tuple(message for pair in turns for message in pair) + (current,)
            request = prepare_model_request(
                messages, provider_profile_digest=provider.provider_profile_digest
            )
            return provider.count_input_tokens(request)

        # These filler sizes straddle the real fixed budget: the newest Turn
        # alone fits, both prior Turns together do not.
        assert count(turn2) <= CONTEXT_TOKEN_BUDGET < count(turn1, turn2)

        accepted = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={**post_headers, "Idempotency-Key": "tcp-turn-3"},
            json={"kind": "question", "content": f"{filler}3"},
        )
        run_id = accepted.json()["run_id"]

        with client.stream("GET", f"/api/v1/runs/{run_id}/events", headers=get_headers) as response:
            lines = []
            for line in response.iter_lines():
                lines.append(line)
                if "stream.end" in line:
                    break
        application.wait_for_generations()

    body = '\n'.join(lines)
    assert "event: context.truncated" in body
    assert body.index("event: context.truncated") < body.index("event: message.delta")
    assert '"dropped_turn_count":1' in body
