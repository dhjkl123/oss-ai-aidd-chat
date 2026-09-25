"""Story 1.6 -- 생성 중지(Cancel)와 Complete/Cancel 경합.

The whole story is a race, so almost everything here is about *ordering*: the
terminal commit before the Provider cancel, exactly one winner between Complete
and Cancel, and nothing a late Provider does afterwards being able to move the
Run again.
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Barrier, Event, Thread
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aidd_chat.adapters import (
    DETERMINISTIC_BINDING,
    InMemoryConversationStore,
)
from aidd_chat.application import ChatApplication, _provider_supports_cancellation
from aidd_chat.contracts import (
    CancelResultV1,
    ProviderFailureV1,
    RunStatusEventV1,
    StreamEndEventV1,
)
from aidd_chat.main import app, get_chat_application
from agent_fakes import GROUNDED_RESULT, LegacySyncAgent
from aidd_chat.adapters import FakeAgent


ORIGIN = {"Origin": "https://testserver"}
CANCEL_SEQUENCE = ("message.discarded", "run.status", "stream.end")


def _hash(capability: str) -> str:
    return sha256(capability.encode()).hexdigest()


def _failure() -> ProviderFailureV1:
    return ProviderFailureV1(
        kind="provider_invalid_response",
        retryable=False,
        correlation_id=uuid4(),
        message="응답이 올바르지 않습니다.",
    )


class StoppableProvider(LegacySyncAgent):
    """Streams one delta, then blocks until the Run is cancelled or released, then
    emits one more delta -- the Provider output that arrives *after* the fence."""

    policy_metadata = FakeAgent().policy_metadata
    binding = DETERMINISTIC_BINDING

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.cancel_requests = 0
        self.abort_requests = 0
        self.observed: list[object] = []
        self.observer = None
        self.late_delta_kind: str | None = None

    def probe(self) -> bool:
        return True

    def new_call_handle(self) -> object:
        provider = self

        class Handle:
            def cancel(self) -> None:
                provider.cancel_requests += 1
                if provider.observer is not None:
                    provider.observed.append(provider.observer())
                provider.release.set()

        return Handle()

    async def abort(self, run_id) -> None:
        self.abort_requests += 1
        await super().abort(run_id)

    def stream(self, _request, on_delta, handle=None) -> str:
        on_delta("첫 ")
        self.started.set()
        self.release.wait(3)
        try:
            on_delta("늦은")
        except Exception as exc:
            self.late_delta_kind = getattr(exc, "kind", type(exc).__name__)
            raise
        return "첫 늦은"


class NoCancelProvider(LegacySyncAgent):
    """Passes every other readiness Gate but cannot be aborted."""

    abort = None

    def complete(self, _request, handle=None) -> str:
        return "답변"


class NoCloseBudgetProvider(NoCancelProvider):
    """Abortable, but declares no close budget."""

    abort = LegacySyncAgent.abort
    close_grace_ms = None


class ZeroGraceProvider(NoCloseBudgetProvider):
    """`wait_for(close(), 0)` cancels the close before it can start."""

    close_grace_ms = 0


class BrokenHandleProvider(StoppableProvider):
    """Declares cancellability and then fails to deliver it."""

    def new_call_handle(self) -> object:
        raise RuntimeError('handle 생성 실패')
def _started_run(store: InMemoryConversationStore, application: ChatApplication):
    conversation, capability = application.create_conversation()
    run = store.accept_question(
        conversation.conversation_id,
        _hash(capability),
        "key",
        "digest",
        "질문",
        datetime.now(UTC),
        correlation_id=uuid4(),
    ).run
    assert store.mark_running(conversation.conversation_id, run.run_id, datetime.now(UTC), time.monotonic())
    return conversation, capability, run


# --- Contracts -------------------------------------------------------------


def test_cancel_result_and_events_are_closed_and_admit_cancelled() -> None:
    now = datetime.now(UTC)
    run_id = uuid4()
    assert RunStatusEventV1(
        run_id=run_id, sequence=2, occurred_at=now, state="cancelled", stage="terminal"
    ).state == "cancelled"
    assert StreamEndEventV1(
        run_id=run_id, sequence=3, occurred_at=now, final_state="cancelled", final_sequence=3
    ).final_state == "cancelled"
    # cancelled is terminal, never streaming -- the running<->streaming rule stands.
    with pytest.raises(ValueError):
        RunStatusEventV1(
            run_id=run_id, sequence=2, occurred_at=now, state="cancelled", stage="streaming"
        )

    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)
    conversation, capability, run = _started_run(store, application)
    assert store.commit_cancelled(run.run_id, _hash(capability), datetime.now(UTC)) is True
    current = store.get_run(run.run_id, _hash(capability), datetime.now(UTC))

    result = CancelResultV1(cancel_outcome="accepted", run=current)
    assert result.schema_version == "1"
    assert set(result.model_dump()) == {"schema_version", "cancel_outcome", "run"}
    with pytest.raises(ValueError):
        CancelResultV1(cancel_outcome="accepted", run=current, extra=1)
    with pytest.raises(ValueError):
        CancelResultV1(cancel_outcome="rejected", run=current)
    assert conversation.conversation_id == current.conversation_id


# --- Domain / store CAS ----------------------------------------------------


def test_commit_cancelled_discards_the_buffer_and_emits_the_triplet_once() -> None:
    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)
    conversation, capability, run = _started_run(store, application)
    assert store.commit_delta(conversation.conversation_id, run.run_id, "부분", datetime.now(UTC))

    assert store.commit_cancelled(run.run_id, _hash(capability), datetime.now(UTC)) is True
    projection, events = application.get_run_snapshot(run.run_id, capability)

    assert (projection.state, projection.stage) == ("cancelled", "terminal")
    # A cancelled Run is not a failure and never carries a completed answer.
    assert projection.terminal_error is None
    assert projection.output_message is None and projection.output_message_id is None
    assert [event.type for event in events] == [
        "run.status", "message.delta", *CANCEL_SEQUENCE
    ]
    assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
    assert events[-2].state == "cancelled" and events[-1].final_state == "cancelled"
    assert sum(event.type == "run.error" for event in events) == 0
    assert store.states[conversation.conversation_id].runs[run.run_id].raw_buffer == ""
    assert store.states[conversation.conversation_id].active_run_id is None
    # The uncommitted streaming text never becomes a Message.
    assert [message.role for message in store.states[conversation.conversation_id].messages] == ["user"]


@pytest.mark.parametrize("cancel_first", [True, False])
def test_complete_and_cancel_keep_only_the_first_committed_terminal(cancel_first: bool) -> None:
    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)
    conversation, capability, run = _started_run(store, application)

    def cancel() -> bool:
        return store.commit_cancelled(run.run_id, _hash(capability), datetime.now(UTC))

    def complete() -> bool:
        return store.commit_completed(
            conversation.conversation_id, run.run_id, "완료 답변", GROUNDED_RESULT, datetime.now(UTC)
        )

    first, second = (cancel, complete) if cancel_first else (complete, cancel)
    assert first() is True
    assert second() is False

    projection, events = application.get_run_snapshot(run.run_id, capability)
    assert projection.state == ("cancelled" if cancel_first else "completed")
    assert sum(event.type == "stream.end" for event in events) == 1
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert (projection.output_message is None) is cancel_first


@pytest.mark.release_suite
def test_concurrent_complete_and_cancel_resolve_to_one_terminal_log() -> None:
    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)
    conversation, capability, run = _started_run(store, application)
    barrier = Barrier(2)
    outcomes: list[tuple[str, bool]] = []

    def cancel() -> None:
        barrier.wait()
        outcomes.append(("cancel", store.commit_cancelled(run.run_id, _hash(capability), datetime.now(UTC))))

    def complete() -> None:
        barrier.wait()
        outcomes.append((
            "complete",
            store.commit_completed(conversation.conversation_id, run.run_id, "완료 답변", GROUNDED_RESULT, datetime.now(UTC)),
        ))

    threads = [Thread(target=cancel), Thread(target=complete)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)

    projection, events = application.get_run_snapshot(run.run_id, capability)
    assert sorted(outcome for _name, outcome in outcomes) == [False, True]
    winner = next(name for name, outcome in outcomes if outcome)
    assert projection.state == ("cancelled" if winner == "cancel" else "completed")
    assert sum(event.type == "stream.end" for event in events) == 1
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))


def test_a_cancelled_run_refuses_every_later_commit() -> None:
    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)
    conversation, capability, run = _started_run(store, application)
    assert store.commit_cancelled(run.run_id, _hash(capability), datetime.now(UTC)) is True
    before = application.get_run_snapshot(run.run_id, capability)

    # This is the Terminal Fence -- no new flag, just the CAS the siblings already had.
    assert store.commit_delta(conversation.conversation_id, run.run_id, "늦음", datetime.now(UTC)) is False
    assert store.commit_completed(conversation.conversation_id, run.run_id, "늦은 완료", GROUNDED_RESULT, datetime.now(UTC)) is False
    assert store.commit_failed(conversation.conversation_id, run.run_id, _failure(), datetime.now(UTC)) is False
    assert store.commit_stream_failed(conversation.conversation_id, run.run_id, _failure(), datetime.now(UTC)) is False
    assert store.commit_stream_completed(
        conversation.conversation_id, run.run_id, "늦음", GROUNDED_RESULT, _failure(), datetime.now(UTC)
    ) is False
    assert store.commit_context_truncated(conversation.conversation_id, run.run_id, 1, datetime.now(UTC)) is False
    assert store.commit_cancelled(run.run_id, _hash(capability), datetime.now(UTC)) is False

    assert application.get_run_snapshot(run.run_id, capability) == before


def test_commit_cancelled_hides_unknown_runs_and_foreign_capabilities() -> None:
    from aidd_chat.adapters import ConversationExpired, ConversationNotFound

    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)
    _conversation, capability, run = _started_run(store, application)
    _other, other_capability, _other_run = _started_run(store, application)

    with pytest.raises(ConversationNotFound):
        store.commit_cancelled(uuid4(), _hash(capability), datetime.now(UTC))
    with pytest.raises(ConversationNotFound):
        store.commit_cancelled(run.run_id, _hash(other_capability), datetime.now(UTC))
    assert application.get_run(run.run_id, capability).state == "running"

    store.states[_conversation.conversation_id].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    # Story 1.8 enforces the Absolute Expiry on the monotonic clock, so a wall
    # step cannot suspend expiry or purge everything at once. Expire both.
    store.states[_conversation.conversation_id].expires_monotonic = time.monotonic() - 1
    with pytest.raises(ConversationExpired):
        store.commit_cancelled(run.run_id, _hash(capability), datetime.now(UTC))


# --- Application: order is the contract -------------------------------------


def test_cancel_commits_before_it_asks_the_provider_and_asks_exactly_once() -> None:
    provider = StoppableProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(conversation.conversation_id, capability, "stop", "질문")
    assert provider.started.wait(2)
    provider.observer = lambda: application.get_run(accepted.run_id, capability).state

    first = application.cancel_run(accepted.run_id, capability)
    second = application.cancel_run(accepted.run_id, capability)
    application.wait_for_generations()

    assert first.cancel_outcome == "accepted" and first.run.state == "cancelled"
    assert second.cancel_outcome == "already_terminal" and second.run.state == "cancelled"
    # The Provider only ever hears about a Run that is *already* terminal, and only
    # from the caller that won the CAS -- otherwise the user sees an error, not a stop.
    assert provider.observed == ["cancelled"]
    assert provider.cancel_requests == 1


def test_provider_output_after_the_fence_changes_nothing() -> None:
    provider = StoppableProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(conversation.conversation_id, capability, "late", "질문")
    assert provider.started.wait(2)

    assert application.cancel_run(accepted.run_id, capability).cancel_outcome == "accepted"
    frozen = application.get_run_snapshot(accepted.run_id, capability)
    application.wait_for_generations()

    projection, events = application.get_run_snapshot(accepted.run_id, capability)
    assert provider.late_delta_kind == "provider_invalid_response"
    assert projection.state == "cancelled" and projection.terminal_error is None
    assert (projection, events) == frozen
    assert [event.type for event in events] == ["run.status", "message.delta", *CANCEL_SEQUENCE]
    assert application._generation_tasks == {}


def test_cancelling_a_queued_run_never_reaches_the_provider() -> None:
    store = InMemoryConversationStore()
    provider = StoppableProvider()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()
    run = store.accept_question(
        conversation.conversation_id, _hash(capability), "queued", "digest", "질문",
        datetime.now(UTC), correlation_id=uuid4(),
    ).run

    result = application.cancel_run(run.run_id, capability)

    assert result.cancel_outcome == "accepted"
    assert (result.run.state, result.run.stage) == ("cancelled", "terminal")
    assert provider.cancel_requests == 0


# --- Adapter: cancellation and the close budget -----------------------------


# --- Readiness --------------------------------------------------------------


def test_a_provider_that_cannot_be_cancelled_is_not_ready_and_refuses_questions() -> None:
    assert ChatApplication(InMemoryConversationStore(), FakeAgent(), 3).is_ready() is True

    for provider in (NoCancelProvider(), NoCloseBudgetProvider(), ZeroGraceProvider()):
        application = ChatApplication(InMemoryConversationStore(), provider, 3)
        assert application.is_ready() is False

        app.dependency_overrides[get_chat_application] = lambda: application
        try:
            with TestClient(app, base_url="https://testserver") as client:
                created = client.post("/api/v1/conversations", headers=ORIGIN)
                ready = client.get("/ready")
                submitted = client.post(
                    f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                    headers={**ORIGIN, "Idempotency-Key": "no-cancel"},
                    json={"kind": "question", "content": "질문"},
                )
        finally:
            app.dependency_overrides.pop(get_chat_application, None)

        assert ready.status_code == 503
        assert submitted.status_code == 503
        assert submitted.json()["error"]["code"] == "provider_unavailable"


# --- HTTP -------------------------------------------------------------------


def test_cancel_endpoint_returns_200_for_accepted_and_already_terminal() -> None:
    provider = StoppableProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            accepted = client.post(
                f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                headers={**ORIGIN, "Idempotency-Key": "cancel-http"},
                json={"kind": "question", "content": "질문"},
            )
            run_id = accepted.json()["run_id"]
            assert provider.started.wait(2)
            first = client.post(f"/api/v1/runs/{run_id}/cancel", headers=ORIGIN)
            second = client.post(f"/api/v1/runs/{run_id}/cancel", headers=ORIGIN)
            application.wait_for_generations()
            polled = client.get(f"/api/v1/runs/{run_id}")
            events = client.get(f"/api/v1/runs/{run_id}/events")
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.headers["cache-control"] == "no-store"
    assert second.headers["cache-control"] == "no-store"
    assert first.json()["cancel_outcome"] == "accepted"
    assert second.json()["cancel_outcome"] == "already_terminal"
    # Both bodies carry the current Run, so a loser never has to guess.
    for payload in (first.json(), second.json()):
        assert payload["schema_version"] == "1"
        assert payload["run"]["state"] == "cancelled" and payload["run"]["stage"] == "terminal"
        assert payload["run"]["output_message"] is None
        assert payload["run"]["terminal_error"] is None

    # Polling and SSE report the same committed state and stop at stream.end.
    assert polled.json()["state"] == "cancelled"
    body = events.text
    streamed = [line.removeprefix("event: ") for line in body.splitlines() if line.startswith("event: ")]
    assert streamed == ["run.status", "message.delta", *CANCEL_SEQUENCE]
    assert '"final_state":"cancelled"' in body
    # Nothing is produced after stream.end.
    assert body.rstrip().endswith("}")
    assert provider.cancel_requests == 1


def test_cancel_hides_unknown_foreign_and_uncredentialed_runs_behind_404() -> None:
    provider = StoppableProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            accepted = client.post(
                f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                headers={**ORIGIN, "Idempotency-Key": "hidden"},
                json={"kind": "question", "content": "질문"},
            )
            run_id = accepted.json()["run_id"]
            assert provider.started.wait(2)
            unknown = client.post(f"/api/v1/runs/{uuid4()}/cancel", headers=ORIGIN)
            malformed = client.post("/api/v1/runs/not-a-uuid/cancel", headers=ORIGIN)
            # A second Conversation's Capability must not reach the first one's Run.
            client.cookies.clear()
            client.post("/api/v1/conversations", headers=ORIGIN)
            foreign = client.post(f"/api/v1/runs/{run_id}/cancel", headers=ORIGIN)
            client.cookies.clear()
            anonymous = client.post(f"/api/v1/runs/{run_id}/cancel", headers=ORIGIN)
            still_running = application.get_run(
                UUID(run_id), created.cookies["conversation_capability"]
            )
            provider.release.set()
            application.wait_for_generations()
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    for response in (unknown, malformed, foreign, anonymous):
        assert response.status_code == 404
        assert response.content == b""
        assert response.headers["cache-control"] == "no-store"
    # No Domain state and no Provider work came out of any of them.
    assert still_running.state == "running"
    assert provider.cancel_requests == 0


def test_cancel_of_an_expired_conversation_is_a_korean_410() -> None:
    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = UUID(created.json()["conversation_id"])
            accepted = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers={**ORIGIN, "Idempotency-Key": "expired"},
                json={"kind": "question", "content": "질문"},
            )
            application.wait_for_generations()
            store.states[conversation_id].expires_at = datetime.now(UTC) - timedelta(seconds=1)
            # Story 1.8 enforces the Absolute Expiry on the monotonic clock, so a wall
            # step cannot suspend expiry or purge everything at once. Expire both.
            store.states[conversation_id].expires_monotonic = time.monotonic() - 1
            expired = client.post(f"/api/v1/runs/{accepted.json()['run_id']}/cancel", headers=ORIGIN)
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert expired.status_code == 410
    assert expired.json()["error"]["code"] == "conversation_expired"
    assert expired.json()["error"]["message"] == "대화 세션이 만료되었습니다."


def test_cancelling_a_completed_run_keeps_the_answer() -> None:
    application = ChatApplication(InMemoryConversationStore(), FakeAgent(), 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            accepted = client.post(
                f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                headers={**ORIGIN, "Idempotency-Key": "late-cancel"},
                json={"kind": "question", "content": "질문"},
            )
            application.wait_for_generations()
            run_id = accepted.json()["run_id"]
            completed = client.get(f"/api/v1/runs/{run_id}").json()
            cancelled = client.post(f"/api/v1/runs/{run_id}/cancel", headers=ORIGIN)
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert completed["state"] == "completed"
    assert cancelled.status_code == 200
    assert cancelled.json()["cancel_outcome"] == "already_terminal"
    assert cancelled.json()["run"] == completed


def test_cancel_requires_the_same_origin_gate_as_every_other_mutating_post() -> None:
    provider = StoppableProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            accepted = client.post(
                f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                headers={**ORIGIN, "Idempotency-Key": "origin"},
                json={"kind": "question", "content": "질문"},
            )
            run_id = accepted.json()["run_id"]
            assert provider.started.wait(2)
            missing = client.post(f"/api/v1/runs/{run_id}/cancel")
            foreign = client.post(
                f"/api/v1/runs/{run_id}/cancel", headers={"Origin": "https://evil.example"}
            )
            still_running = application.get_run(
                UUID(run_id), created.cookies["conversation_capability"]
            )
            provider.release.set()
            application.wait_for_generations()
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    for response in (missing, foreign):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "invalid_origin"
    assert still_running.state == "running"
    assert provider.cancel_requests == 0


# --- The publish window ------------------------------------------------------


class GatedHandleProvider(StoppableProvider):
    """Blocks inside new_call_handle, so a Cancel can land before the handle is
    published -- the window a late publish leaves open."""

    def __init__(self) -> None:
        super().__init__()
        self.minting = Event()
        self.may_mint = Event()

    def new_call_handle(self) -> object:
        self.minting.set()
        self.may_mint.wait(3)
        return super().new_call_handle()


class GatedMarkRunningStore(InMemoryConversationStore):
    """Blocks inside mark_running, i.e. after the handle has been published."""

    def __init__(self) -> None:
        super().__init__()
        self.marking = Event()
        self.may_mark = Event()

    def mark_running(self, *args, **kwargs) -> bool:
        self.marking.set()
        self.may_mark.wait(3)
        return super().mark_running(*args, **kwargs)


def test_cancel_before_the_handle_is_published_still_stops_the_provider_call() -> None:
    provider = GatedHandleProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(conversation.conversation_id, capability, "early", "질문")

    assert provider.minting.wait(2)
    result = application.cancel_run(accepted.run_id, capability)
    provider.may_mint.set()
    application.wait_for_generations()

    assert result.cancel_outcome == "accepted"
    # mark_running refuses a Run that is already cancelled, so the Provider call
    # never starts. Otherwise the user sees "cancelled" while the model keeps going.
    assert provider.started.is_set() is False
    assert application.get_run(accepted.run_id, capability).state == "cancelled"
    assert application._generation_tasks == {}


def test_cancel_between_publish_and_mark_running_still_stops_the_provider_call() -> None:
    store = GatedMarkRunningStore()
    provider = StoppableProvider()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(conversation.conversation_id, capability, "window", "질문")

    assert store.marking.wait(2)
    result = application.cancel_run(accepted.run_id, capability)
    store.may_mark.set()
    application.wait_for_generations()

    assert result.cancel_outcome == "accepted"
    # The Cancel reaches the agent as abort(run_id) -- there is no pre-published
    # call handle on the agent port any more (AD-25). The abort is scheduled on the
    # agent loop, which mark_running's gate was blocking, so it may land just after
    # the generation Task ends.
    deadline = time.monotonic() + 2
    while provider.abort_requests == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert provider.abort_requests == 1
    assert provider.started.is_set() is False
    assert application.get_run(accepted.run_id, capability).state == "cancelled"


def test_a_provider_that_cannot_mint_a_handle_fails_the_run_instead_of_running_it() -> None:
    provider = BrokenHandleProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(conversation.conversation_id, capability, "broken", "질문")
    application.wait_for_generations()

    projection, events = application.get_run_snapshot(accepted.run_id, capability)
    # Readiness promised a stoppable Provider; an unstoppable Run must not start.
    assert provider.started.is_set() is False
    assert projection.state == "failed"
    assert projection.terminal_error.kind == "provider_unknown"
    assert [event.type for event in events][0] == "run.status"


# --- The real adapter cancel seam -------------------------------------------


# --- The CAS the frozen Intent names ----------------------------------------


def test_commit_cancelled_keeps_the_expected_active_run_cas() -> None:
    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)
    conversation, _capability, run = _started_run(store, application)
    state = store.states[conversation.conversation_id]

    # Only a terminal commit ever clears active_run_id, so this state is not
    # reachable in practice -- it is here to prove the guard is really applied.
    state.active_run_id = None
    assert state.commit_cancelled(run.run_id, datetime.now(UTC)) is False
    state.active_run_id = run.run_id
    assert state.commit_cancelled(run.run_id, datetime.now(UTC)) is True


def test_cancel_outcome_and_the_run_it_returns_never_disagree() -> None:
    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)
    conversation, capability, run = _started_run(store, application)

    first = application.cancel_run(run.run_id, capability)
    second = application.cancel_run(run.run_id, capability)

    # active_run_id is set at accept and cleared only by a terminal commit, so the
    # CAS refuses exactly the terminal Runs: `already_terminal` beside a Run that is
    # still going -- which would leave a client waiting forever -- cannot happen.
    for result in (first, second):
        assert result.run.state not in {"queued", "running"}
    assert first.cancel_outcome == "accepted"
    assert second.cancel_outcome == "already_terminal"
    assert store.states[conversation.conversation_id].active_run_id is None


# --- Readiness verifies Close as well as Cancel ------------------------------


def test_readiness_refuses_a_provider_that_does_not_confirm_it_closes_its_stream() -> None:
    assert _provider_supports_cancellation(FakeAgent()) is True
    for provider in (
        NoCancelProvider(),
        NoCloseBudgetProvider(),
        ZeroGraceProvider(),
    ):
        # Checked directly, not only through /ready: a None or 0 budget must never
        # reach asyncio.wait_for, where it would cancel the close before it starts.
        assert _provider_supports_cancellation(provider) is False
        assert ChatApplication(InMemoryConversationStore(), provider, 3).is_ready() is False


# --- The Adapter Task is not reused before it ends ---------------------------


class LingeringProvider(StoppableProvider):
    """Its Cancel does not end the call, so the generation task keeps draining after
    the terminal state is committed -- the window a resubmit must not slip through."""

    def new_call_handle(self) -> object:
        provider = self

        class Handle:
            def cancel(self) -> None:
                provider.cancel_requests += 1

        return Handle()


def test_a_cancelled_generation_task_is_not_reused_before_it_ends() -> None:
    provider = LingeringProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()
    first = application.submit_question(conversation.conversation_id, capability, "one", "질문")
    assert provider.started.wait(2)
    assert application.cancel_run(first.run_id, capability).cancel_outcome == "accepted"

    first_task = application._generation_tasks[first.run_id].future

    # active_run_id is already free, but the generation Task is still draining. The
    # next question waits for it; an agent ignoring its abort has its Task cancelled
    # once the Close Grace is up (Task 4 ruling), so the next Run starts only after
    # the first Task has ended -- never two at once.
    second = application.submit_question(conversation.conversation_id, capability, "two", "질문")
    assert first_task.done()

    provider.release.set()
    application.wait_for_generations()
    application.wait_for_generations()

    assert second.run_id != first.run_id
    assert application.get_run(second.run_id, capability).state == "completed"


# --- The handle is an argument, so an unhandled call is visibly uncancellable -


def test_an_exception_before_the_failure_handler_still_resolves_the_run() -> None:
    """mark_running and the handle publish run before `fail()` exists; without a
    guard of their own an exception there leaves the Run queued forever, with no
    terminal commit and no stream.end for any client to end on."""

    class BrokenStore(InMemoryConversationStore):
        def mark_running(self, *args, **kwargs) -> bool:
            raise RuntimeError("store 고장")

    application = ChatApplication(BrokenStore(), StoppableProvider(), 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(conversation.conversation_id, capability, "broken", "질문")
    application.wait_for_generations()

    projection = application.get_run(accepted.run_id, capability)
    assert projection.state == "failed"
    assert projection.terminal_error.kind == "provider_unknown"
    assert application._generation_tasks == {}
