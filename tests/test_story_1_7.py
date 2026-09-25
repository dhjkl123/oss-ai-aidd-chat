"""Story 1.7 -- 실패·120초 Timeout과 안전한 재시도.

Two contracts, both of which only exist under contention: a Wall-clock Deadline
that starts at accept and ends every Run inside it, and an explicit RetryCommand
that re-sends the *same* request under a bounded number of Attempts.

The Deadline is exercised by shrinking `application.RUN_DEADLINE` rather than by
waiting: every consumer reads the module global at call time, so a 0.5 s Deadline
takes the same code path a 120 s one does.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from threading import Barrier, Event, Thread
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aidd_chat.adapters import (
    DETERMINISTIC_BINDING,
    ConversationExpired,
    LocalTestBinding,
    ConversationNotFound,
    IdempotencyConflict,
    InMemoryConversationStore,
    RetryExhausted,
    RetryNotRetryable,
    RunNotFound,
)
import aidd_chat.application as application_module
from aidd_chat.application import (
    RUN_DEADLINE,
    RUN_DEADLINE_SECONDS,
    ActiveRunConflict,
    ChatApplication,
    ContextTooLarge,
    ProviderProfileChanged,
)
from aidd_chat.bootstrap import BindingConfigurationError, build_chat_application
from aidd_chat.contracts import (
    verify_request_integrity,
    ContextIntegrityError,
    PreparedMessageV1,
    RetryCommand,
    RunProjectionV1,
    RunStatusEventV1,
    StreamEndEventV1,
    canonical_request_bytes,
    has_visible_text,
    prepare_model_request,
)
from aidd_chat.domain import Message, Run
import aidd_chat.main as main_module
from aidd_chat.main import app, get_chat_application
from agent_fakes import LegacySyncAgent
from aidd_chat.adapters import FakeAgent


ORIGIN = {"Origin": "https://testserver"}
TIMEOUT_SEQUENCE = ("message.discarded", "run.error", "run.status", "stream.end")
QUESTION = "왜 실패했나요"


def _hash(capability: str) -> str:
    return sha256(capability.encode()).hexdigest()


def _binding(**overrides) -> LocalTestBinding:
    """Built through the constructor, so it passes the same rules a real binding does."""
    return replace(DETERMINISTIC_BINDING, **overrides)


@pytest.fixture
def short_deadline(monkeypatch: pytest.MonkeyPatch):
    def apply(seconds: float) -> None:
        monkeypatch.setattr(application_module, "RUN_DEADLINE", timedelta(seconds=seconds))

    return apply


def _accept_directly(store, conversation, capability, key="queued", question="질문"):
    """A Run accepted with no generation task behind it: the shape only the
    observation path can end."""
    now = datetime.now(UTC)
    return store.accept_question(
        conversation.conversation_id, _hash(capability), key, "digest", question, now,
        now, time.monotonic(), correlation_id=uuid4(),
    ).run


class ScriptedProvider(LegacySyncAgent):
    """Streams one answer per call, or raises, following a script. Records every
    request it actually consumed so a Retry's re-sent bytes can be compared."""

    policy_metadata = FakeAgent().policy_metadata
    binding = DETERMINISTIC_BINDING

    def __init__(self, *outcomes: str, delay: float = 0.0) -> None:
        self.outcomes = list(outcomes)
        self.delay = delay
        self.requests: list[object] = []

    def probe(self) -> bool:
        return True

    def stream(self, request, on_delta, handle=None) -> str:
        verify_request_integrity(request, self)
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else "완료 답변"
        if self.delay:
            time.sleep(self.delay)
        if outcome == "fail":
            raise RuntimeError("provider down")
        on_delta(outcome)
        return outcome


class BlockingProvider(LegacySyncAgent):
    """Streams one delta and then hangs until cancelled -- the Run that has to be
    stopped by the Deadline rather than by finishing. `observer` is read at the
    instant cancel() lands, which is how the Cancellation-before-commit ordering is
    proven rather than assumed."""

    policy_metadata = FakeAgent().policy_metadata

    def __init__(self, close_grace_ms: int = 10) -> None:
        self.binding = _binding(close_grace_ms=close_grace_ms)
        self.started = Event()
        self.release = Event()
        self.calls = 0
        self.cancel_requests = 0
        self.observer = None
        self.observed: list[object] = []
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

    def stream(self, request, on_delta, handle=None) -> str:
        self.calls += 1
        verify_request_integrity(request, self)
        on_delta("부분 ")
        self.started.set()
        self.release.wait(5)
        try:
            on_delta("늦은")
        except Exception as exc:
            self.late_delta_kind = getattr(exc, "kind", type(exc).__name__)
            raise
        return "부분 늦은"


class DrippingProvider(LegacySyncAgent):
    """Emits one delta and then keeps emitting on its own schedule until cancelled.
    Nothing polls it, so only the task's own Deadline alarm can end it."""

    policy_metadata = FakeAgent().policy_metadata

    def __init__(self, interval: float = 0.05, close_grace_ms: int = 10) -> None:
        self.binding = _binding(close_grace_ms=close_grace_ms)
        self.interval = interval
        self.cancel_requests = 0
        self.deltas = 0
        self.handle_cancelled = Event()

    def probe(self) -> bool:
        return True

    def new_call_handle(self) -> object:
        provider = self

        class Handle:
            def cancel(self) -> None:
                provider.cancel_requests += 1
                provider.handle_cancelled.set()

        return Handle()

    def stream(self, request, on_delta, handle=None) -> str:
        verify_request_integrity(request, self)
        buffer = ""
        limit = time.monotonic() + 10
        while time.monotonic() < limit:
            if self.handle_cancelled.is_set():
                raise RuntimeError("cancelled by the deadline")
            self.deltas += 1
            on_delta("조각")
            buffer += "조각"
            time.sleep(self.interval)
        return buffer


class SlowCompleteProvider(LegacySyncAgent):
    """Non-streaming, and answers *after* the Deadline. The answer is valid; it is
    simply late, and a late answer must not become a completed Message."""

    policy_metadata = FakeAgent().policy_metadata

    def __init__(self, seconds: float, close_grace_ms: int = 10) -> None:
        self.binding = _binding(close_grace_ms=close_grace_ms)
        self.seconds = seconds
        self.cancel_requests = 0

    def probe(self) -> bool:
        return True

    def new_call_handle(self) -> object:
        provider = self

        class Handle:
            def cancel(self) -> None:
                provider.cancel_requests += 1

        return Handle()

    def complete(self, request, handle=None) -> str:
        verify_request_integrity(request, self)
        time.sleep(self.seconds)
        return "늦게 도착한 완전한 답변"


def _application(provider, max_attempts: int = 3):
    store = InMemoryConversationStore()
    return store, ChatApplication(store, provider, max_attempts)


def _fail_one_run(application, conversation, capability, key: str = "q1"):
    projection = application.submit_question(
        conversation.conversation_id, capability, key, QUESTION
    )
    application.wait_for_generations(5)
    return application.get_run(projection.run_id, capability)


def _retry(application, conversation, capability, key, target_run_id):
    return application.retry_run(
        conversation.conversation_id,
        capability,
        key,
        RetryCommand(kind="retry", retry_of_run_id=target_run_id),
    )


def _await_state(application, run_id, capability, state, seconds=5.0):
    """Polling is itself the Deadline's enforcement point, so the loop that waits
    for `timeout` is also the thing that produces it."""
    limit = time.monotonic() + seconds
    current = application.get_run(run_id, capability)
    while current.state != state and time.monotonic() < limit:
        current = application.get_run(run_id, capability)
    return current


# --- Contracts --------------------------------------------------------------


def test_contracts_admit_the_timeout_terminal_and_a_content_free_retry_command() -> None:
    now = datetime.now(UTC)
    run_id = uuid4()
    assert RunStatusEventV1(
        run_id=run_id, sequence=2, occurred_at=now, state="timeout", stage="terminal"
    ).state == "timeout"
    assert StreamEndEventV1(
        run_id=run_id, sequence=3, occurred_at=now, final_state="timeout", final_sequence=3
    ).final_state == "timeout"
    # timeout is terminal, never streaming -- the running<->streaming rule stands.
    with pytest.raises(ValueError):
        RunStatusEventV1(
            run_id=run_id, sequence=2, occurred_at=now, state="timeout", stage="streaming"
        )

    command = RetryCommand(kind="retry", retry_of_run_id=run_id)
    assert command.retry_of_run_id == run_id
    # No content, nothing extra, frozen, and a UUIDv4 target.
    with pytest.raises(ValueError):
        RetryCommand(kind="retry", retry_of_run_id=run_id, content="새 질문")
    with pytest.raises(ValueError):
        RetryCommand(kind="retry", retry_of_run_id=UUID(int=1))
    with pytest.raises(ValueError):
        command.retry_of_run_id = run_id


def test_current_input_message_id_is_metadata_and_leaves_the_digest_alone() -> None:
    messages = (
        PreparedMessageV1(uuid4(), "user", "이전"),
        PreparedMessageV1(uuid4(), "assistant", "답"),
        PreparedMessageV1(uuid4(), "user", "현재"),
    )
    request = prepare_model_request(messages, provider_profile_digest="digest")

    assert request.current_input_message_id == messages[-1].message_id
    # Canonical bytes exclude every ID, so the new field cannot move a digest.
    assert request.canonical_bytes == canonical_request_bytes(request.system_instruction, messages)
    assert request.context_digest == sha256(request.canonical_bytes).hexdigest()


def test_run_projection_requires_a_terminal_error_for_timeout() -> None:
    with pytest.raises(ValueError):
        RunProjectionV1(
            run_id=uuid4(),
            conversation_id=uuid4(),
            input_message_id=uuid4(),
            state="timeout",
            stage="terminal",
            created_at=datetime.now(UTC),
            last_updated_at=datetime.now(UTC),
        )


# --- Deadline ---------------------------------------------------------------


def test_the_deadline_is_120_wall_clock_seconds_from_accept() -> None:
    assert RUN_DEADLINE_SECONDS == 120
    assert RUN_DEADLINE == timedelta(seconds=120)

    store, application = _application(ScriptedProvider("답변"))
    conversation, capability = application.create_conversation()
    projection = application.submit_question(conversation.conversation_id, capability, "k", "질문")
    application.wait_for_generations(5)

    run = store.states[conversation.conversation_id].runs[projection.run_id]
    # Measured from accept, not from the Provider call.
    assert run.deadline_at == run.created_at + RUN_DEADLINE


def test_a_queued_run_past_its_deadline_is_committed_timeout_when_observed(short_deadline) -> None:
    """No watchdog thread exists, so a `queued` Run ends the moment Polling or SSE
    looks at it -- which is exactly when someone is waiting on it."""
    short_deadline(0)
    store, application = _application(ScriptedProvider())
    conversation, capability = application.create_conversation()
    # Accepted through the store directly: no generation task, so nothing but the
    # observation path can end this Run.
    accepted = _accept_directly(store, conversation, capability)
    assert accepted.state == "queued"

    projection, events = application.get_run_snapshot(accepted.run_id, capability)

    assert (projection.state, projection.stage) == ("timeout", "terminal")
    assert projection.terminal_error.kind == "provider_timeout"
    assert projection.output_message_id is None
    assert tuple(event.type for event in events) == TIMEOUT_SEQUENCE
    assert events[2].state == "timeout"
    assert events[-1].final_state == "timeout"
    # Polling and SSE read the same committed log.
    assert application.get_run(accepted.run_id, capability) == projection


def test_a_running_run_past_its_deadline_is_committed_then_cancelled(short_deadline) -> None:
    short_deadline(0.5)
    provider = BlockingProvider()
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    projection = application.submit_question(conversation.conversation_id, capability, "k", "긴 질문")
    # Read at the instant cancel() lands. AD-6 (Task 4 ruling): the `timeout`
    # terminal is committed FIRST and only that commit asks the agent to stop, so the
    # Run is already `timeout` when the cancel arrives.
    provider.observer = lambda: store.states[conversation.conversation_id].runs[
        projection.run_id
    ].state
    assert provider.started.wait(3)

    current = _await_state(application, projection.run_id, capability, "timeout")
    application.wait_for_generations(5)

    assert current.state == "timeout"
    assert current.terminal_error.kind == "provider_timeout"
    assert provider.cancel_requests >= 1
    assert provider.observed and provider.observed[0] == "timeout"
    # 부분 출력은 완료 답변이 되지 않는다.
    assert current.output_message_id is None and current.output_message is None
    state = store.states[conversation.conversation_id]
    assert state.runs[projection.run_id].raw_buffer == ""
    assert [message.role for message in state.messages] == ["user"]


def test_a_late_provider_callback_cannot_move_a_timed_out_run(short_deadline) -> None:
    short_deadline(0.5)
    provider = BlockingProvider()
    _store, application = _application(provider)
    conversation, capability = application.create_conversation()
    projection = application.submit_question(conversation.conversation_id, capability, "k", "긴 질문")
    assert provider.started.wait(3)

    _await_state(application, projection.run_id, capability, "timeout")
    settled, events = application.get_run_snapshot(projection.run_id, capability)
    application.wait_for_generations(5)

    after, after_events = application.get_run_snapshot(projection.run_id, capability)
    assert settled.state == "timeout"
    assert (after.state, after.latest_sequence) == (settled.state, settled.latest_sequence)
    assert [event.model_dump() for event in after_events] == [event.model_dump() for event in events]


def test_a_close_grace_longer_than_the_remaining_deadline_is_clamped(short_deadline) -> None:
    """`남은 Deadline이 Grace보다 짧다 → Grace를 남은 Deadline으로 clamp`. The Grace
    may only take what is left; it never takes time of its own, and it never costs
    the Run its Provider call."""
    short_deadline(0.5)
    provider = BlockingProvider(close_grace_ms=5_000)
    _store, application = _application(provider)
    conversation, capability = application.create_conversation()
    projection = application.submit_question(conversation.conversation_id, capability, "k", "질문")
    application.wait_for_generations(5)

    settled, events = application.get_run_snapshot(projection.run_id, capability)
    assert settled.state == "timeout"
    # The call was made and cancelled at once -- not refused to make room for a close.
    assert provider.calls == 1
    assert events[0].type == "run.status" and events[0].state == "running"


def test_the_alarm_neither_commits_nor_cancels_before_the_deadline(short_deadline) -> None:
    """AD-6 (Task 4 ruling): ONE alarm, at the Deadline. Fired early -- asyncio may
    run a handle a clock tick early -- it re-arms instead of committing, and there
    is no earlier cancel alarm: the agent is stopped only after `timeout` is
    committed, which is when `run_past_deadline`, the single source of truth,
    starts agreeing."""
    short_deadline(0.5)
    provider = BlockingProvider()
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    projection = application.submit_question(conversation.conversation_id, capability, "k", QUESTION)
    assert provider.started.wait(3)
    deadline = application.run_deadline_monotonic(projection.run_id, capability)

    async def fire() -> None:
        application._deadline_commit(projection.run_id, deadline)

    # The alarm, fired while the Deadline is still ahead.
    application.agent_loop.submit(fire()).result(2)
    assert application.get_run(projection.run_id, capability).state == "running"
    assert store.states[conversation.conversation_id].runs[projection.run_id].terminal_error is None
    assert provider.cancel_requests == 0

    # Its re-armed self ends the Run at the Deadline: commit, then the one abort.
    application.wait_for_generations(5)
    assert provider.cancel_requests == 1
    assert application.get_run(projection.run_id, capability).state == "timeout"


def test_the_deadline_ends_a_stalled_run_with_nobody_observing(short_deadline) -> None:
    """The case only the task's own alarm can reach: a Provider that stopped
    emitting and has not returned, with no poll and no SSE attached. Timeliness is
    the assertion -- this Provider would otherwise sit for five seconds, and
    `shutdown()` would sit with it."""
    short_deadline(0.4)
    provider = BlockingProvider()
    _store, application = _application(provider)
    conversation, capability = application.create_conversation()
    projection = application.submit_question(conversation.conversation_id, capability, "k", "질문")
    assert provider.started.wait(3)

    started = time.monotonic()
    application.wait_for_generations(2.5)
    elapsed = time.monotonic() - started

    assert application._generation_tasks == {}
    assert elapsed < 2.5
    assert provider.cancel_requests >= 1
    settled = application.get_run(projection.run_id, capability)
    assert settled.state == "timeout"
    assert settled.output_message_id is None


def test_the_deadline_ends_a_streaming_run_with_nobody_observing(short_deadline) -> None:
    """No poll, no SSE: `wait_for_generations` runs before the first `get_run`, so
    only the task's own alarm can have ended this Run."""
    short_deadline(0.4)
    provider = DrippingProvider()
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    projection = application.submit_question(conversation.conversation_id, capability, "k", "질문")

    application.wait_for_generations(10)
    settled, events = application.get_run_snapshot(projection.run_id, capability)

    assert settled.state == "timeout"
    assert provider.cancel_requests >= 1
    assert settled.output_message_id is None
    # Every delta after the Deadline was refused, so the log stops where the
    # Deadline did rather than growing for the length of the Provider's patience.
    assert [event.type for event in events].count("message.delta") >= 1
    assert [event.type for event in events][-4:] == list(TIMEOUT_SEQUENCE)


@pytest.mark.release_suite
def test_a_late_but_valid_answer_never_becomes_a_completed_message(short_deadline) -> None:
    short_deadline(0.3)
    provider = SlowCompleteProvider(1.0)
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    projection = application.submit_question(conversation.conversation_id, capability, "k", "질문")

    application.wait_for_generations(10)
    settled = application.get_run(projection.run_id, capability)

    assert settled.state == "timeout"
    assert settled.output_message_id is None and settled.output_message is None
    assert [message.role for message in store.states[conversation.conversation_id].messages] == ["user"]


def test_mark_running_refuses_a_past_deadline_run_and_its_caller_commits_a_terminal() -> None:
    """Both directions of the guard: the store refuses to start it, and `_generate`
    reacts to that False with a terminal rather than a silent return that would
    leave the Run `queued` with no stream.end."""
    store, application = _application(ScriptedProvider())
    conversation, capability = application.create_conversation()
    accepted = _accept_directly(store, conversation, capability)
    state = store.states[conversation.conversation_id]

    assert state.mark_running(accepted.run_id, datetime.now(UTC), time.monotonic()) is False

    application.agent_loop.submit(application._generate(
        conversation.conversation_id,
        accepted.run_id,
        accepted.input_message_id,
        "질문",
        time.monotonic(),
    )).result(10)
    settled, events = application.get_run_snapshot(accepted.run_id, capability)
    assert settled.state == "timeout"
    assert tuple(event.type for event in events) == TIMEOUT_SEQUENCE


def _unalarmed_run(store, application, conversation, capability):
    """A Run whose stored Deadline is far away, driven by calling `_generate`
    directly. There is no `_generation_tasks` entry, so no alarm is armed and no
    observer can enforce anything -- only the task's own in-flight checks can end
    it. That is what makes those checks testable on their own.

    `_generate` reads the Run's Correlation ID from its `_generation_tasks` entry,
    so one is registered; the alarm that entry would arm is switched off instead."""
    now = datetime.now(UTC)
    correlation_id = uuid4()
    run = store.accept_question(
        conversation.conversation_id, _hash(capability), "k", "digest", QUESTION, now,
        now + timedelta(seconds=120), time.monotonic() + 120, correlation_id=correlation_id,
    ).run
    application._generation_tasks[run.run_id] = application_module._Generation(
        conversation.conversation_id, correlation_id
    )
    application._arm_deadline = lambda *_args: None
    return run


def test_a_late_answer_is_refused_by_the_task_itself_with_no_alarm_armed() -> None:
    provider = SlowCompleteProvider(0.5)
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    accepted = _unalarmed_run(store, application, conversation, capability)

    application.agent_loop.submit(application._generate(
        conversation.conversation_id, accepted.run_id, accepted.input_message_id,
        QUESTION, time.monotonic() + 0.2,
    )).result(10)

    settled = application.get_run(accepted.run_id, capability)
    assert settled.state == "timeout"
    assert settled.output_message_id is None and settled.output_message is None


def test_a_stream_running_past_the_deadline_is_stopped_by_the_task_itself() -> None:
    provider = DrippingProvider()
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    accepted = _unalarmed_run(store, application, conversation, capability)

    started = time.monotonic()
    application.agent_loop.submit(application._generate(
        conversation.conversation_id, accepted.run_id, accepted.input_message_id,
        QUESTION, time.monotonic() + 0.2,
    )).result(10)
    elapsed = time.monotonic() - started

    # This Provider streams for ten seconds unless something stops it.
    assert elapsed < 3
    settled, events = application.get_run_snapshot(accepted.run_id, capability)
    assert settled.state == "timeout"
    assert tuple(event.type for event in events)[-4:] == TIMEOUT_SEQUENCE


def test_a_run_accepted_past_its_deadline_never_reaches_the_provider(short_deadline) -> None:
    """The `mark_running` guard and its caller, together: no Provider call, and a
    terminal without waiting for an observer."""
    short_deadline(0)
    provider = ScriptedProvider("완료 답변")
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    projection = application.submit_question(conversation.conversation_id, capability, "k", "질문")

    application.wait_for_generations(5)
    settled = application.get_run(projection.run_id, capability)

    assert settled.state == "timeout"
    assert provider.requests == []


def test_the_deadline_is_reached_through_an_sse_slice_read(short_deadline) -> None:
    """`get_run_slice`, the SSE path, enforces the Deadline on a *running* Run
    exactly as `get_run` does -- the client streaming an answer is often the only
    thing looking at it."""
    short_deadline(0.5)
    provider = BlockingProvider()
    _store, application = _application(provider)
    conversation, capability = application.create_conversation()
    projection = application.submit_question(conversation.conversation_id, capability, "k", "긴 질문")
    assert provider.started.wait(3)

    limit = time.monotonic() + 5
    current, events = application.get_run_slice(projection.run_id, capability, 0)
    while current.state != "timeout" and time.monotonic() < limit:
        current, events = application.get_run_slice(projection.run_id, capability, 0)
    application.wait_for_generations(5)

    assert current.state == "timeout"
    assert tuple(event.type for event in events)[-4:] == TIMEOUT_SEQUENCE


def test_a_cancel_on_a_run_past_its_deadline_still_ends_as_timeout(short_deadline) -> None:
    short_deadline(0)
    store, application = _application(ScriptedProvider())
    conversation, capability = application.create_conversation()
    accepted = _accept_directly(store, conversation, capability)

    result = application.cancel_run(accepted.run_id, capability)

    assert result.cancel_outcome == "already_terminal"
    assert result.run.state == "timeout"
    assert result.run.terminal_error.kind == "provider_timeout"


def test_polling_and_sse_return_the_same_timeout_terminal(short_deadline) -> None:
    short_deadline(0)
    store, application = _application(ScriptedProvider())
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = UUID(created.json()["conversation_id"])
            capability = client.cookies.get("conversation_capability")
            now = datetime.now(UTC)
            accepted = store.accept_question(
                conversation_id, _hash(capability), "queued", "digest", "질문", now,
                now, time.monotonic(), correlation_id=uuid4(),
            ).run

            polled = client.get(f"/api/v1/runs/{accepted.run_id}")
            streamed = client.get(f"/api/v1/runs/{accepted.run_id}/events")
    finally:
        app.dependency_overrides.clear()

    assert polled.status_code == 200
    assert polled.json()["state"] == "timeout"
    assert polled.json()["terminal_error"]["kind"] == "provider_timeout"
    # The terminal was committed by the poll above, so the stream replays the same
    # committed log and closes on it.
    assert streamed.status_code == 200
    assert '"state":"timeout"' in streamed.text
    assert '"final_state":"timeout"' in streamed.text


# --- Retry ------------------------------------------------------------------


def test_retry_reuses_the_input_message_and_the_request_snapshot() -> None:
    provider = ScriptedProvider("fail", "완료 답변")
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    failed = _fail_one_run(application, conversation, capability)
    assert failed.state == "failed"

    retried = _retry(application, conversation, capability, "r1", failed.run_id)
    application.wait_for_generations(5)
    settled = application.get_run(retried.run_id, capability)

    assert retried.run_id != failed.run_id
    assert retried.retry_of_run_id == failed.run_id
    assert retried.input_message_id == failed.input_message_id
    # 새 User Message는 만들어지지 않는다.
    state = store.states[conversation.conversation_id]
    assert [message.role for message in state.messages] == ["user", "assistant"]
    # 원본 Snapshot 재사용: the same bytes and digest, not a fresh selection.
    assert len(provider.requests) == 2
    assert provider.requests[0].canonical_bytes == provider.requests[1].canonical_bytes
    assert provider.requests[0].context_digest == provider.requests[1].context_digest
    assert provider.requests[1].current_input_message_id == failed.input_message_id
    assert provider.requests[1] is provider.requests[0]
    # 원래 실패 Run은 Terminal 기록으로 보존된다.
    assert application.get_run(failed.run_id, capability).state == "failed"
    assert settled.state == "completed"
    assert settled.output_message_id is not None


def test_retry_re_sends_the_snapshot_even_after_history_moved_on() -> None:
    """A completed Turn lands between the failure and the Retry. A fresh selection
    would include it; re-sending the Snapshot cannot."""
    provider = ScriptedProvider("fail", "사이 답변", "재시도 답변")
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    failed = _fail_one_run(application, conversation, capability)
    original = provider.requests[0]

    application.submit_question(conversation.conversation_id, capability, "q2", "사이 질문")
    application.wait_for_generations(5)

    retried = _retry(application, conversation, capability, "r1", failed.run_id)
    application.wait_for_generations(5)

    assert application.get_run(retried.run_id, capability).state == "completed"
    resent = provider.requests[-1]
    assert resent.canonical_bytes == original.canonical_bytes
    assert resent.context_digest == original.context_digest
    # The intervening Turn is absent -- proof this was not a re-selection.
    assert "사이 질문" not in resent.canonical_bytes.decode("utf-8")


def test_a_retry_whose_snapshot_no_longer_matches_the_provider_is_refused() -> None:
    """A rebind between the two Runs. The Retry is refused before acceptance -- no
    Run, no consumed Attempt, and never a ContextIntegrityError into the
    process-wide integrity poison.

    Story 1.11 gave this refusal its own name: a rebind is `provider_profile_changed`,
    not `retry_not_allowed`. The target WAS retryable and the same question is still
    askable, which is a different thing to tell the user."""
    provider = ScriptedProvider("fail", "완료 답변")
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    failed = _fail_one_run(application, conversation, capability)
    state = store.states[conversation.conversation_id]
    before = (len(state.runs), len(state.messages), len(state.receipts))

    provider.binding = _binding(max_input_tokens=DETERMINISTIC_BINDING.max_input_tokens + 1)
    with pytest.raises(ProviderProfileChanged):
        _retry(application, conversation, capability, "r1", failed.run_id)

    assert (len(state.runs), len(state.messages), len(state.receipts)) == before
    assert application._integrity_poisoned is False
    assert len(provider.requests) == 1


def test_a_snapshot_bound_to_another_question_is_never_re_sent() -> None:
    """AD-25's Current Input binding, enforced rather than merely recorded: a
    Snapshot may only be re-sent by the Run whose question it answers."""
    provider = ScriptedProvider()
    _store, application = _application(provider)
    request = prepare_model_request(
        (PreparedMessageV1(uuid4(), "user", QUESTION),),
        provider_profile_digest=provider.provider_profile_digest,
    )

    assert application._snapshot_still_sendable(request, request.current_input_message_id) is True
    assert application._snapshot_still_sendable(request, uuid4()) is False


def test_retry_of_a_truncated_context_re_emits_context_truncated_exactly_once() -> None:
    provider = ScriptedProvider("완료 답변", "fail", "완료 답변")
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    application.submit_question(conversation.conversation_id, capability, "k1", "첫 질문")
    application.wait_for_generations(5)

    # Shrink the window to exactly "system + the next question", so the completed
    # Turn no longer fits and the next Run has to drop it.
    baseline = prepare_model_request(
        (PreparedMessageV1(uuid4(), "user", QUESTION),),
        provider_profile_digest=provider.provider_profile_digest,
    )
    provider.binding = _binding(max_input_tokens=provider.count_input_tokens(baseline))

    failed = _fail_one_run(application, conversation, capability, key="k2")
    _, first_events = application.get_run_snapshot(failed.run_id, capability)
    assert [event.type for event in first_events].count("context.truncated") == 1

    retried = _retry(application, conversation, capability, "r1", failed.run_id)
    application.wait_for_generations(5)
    _, retry_events = application.get_run_snapshot(retried.run_id, capability)

    truncated = [event for event in retry_events if event.type == "context.truncated"]
    assert len(truncated) == 1
    assert truncated[0].dropped_turn_count == 1


def test_a_retry_racing_a_question_leaves_exactly_one_winner() -> None:
    provider = ScriptedProvider("fail", delay=0.2)
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    failed = _fail_one_run(application, conversation, capability)

    gate = Barrier(2)
    outcomes: list[object] = []

    def run(action) -> None:
        gate.wait(3)
        try:
            outcomes.append(action())
        except Exception as exc:  # noqa: BLE001 -- which exception is the assertion
            outcomes.append(exc)

    threads = [
        Thread(target=run, args=(lambda: _retry(application, conversation, capability, "r1", failed.run_id),)),
        Thread(target=run, args=(lambda: application.submit_question(
            conversation.conversation_id, capability, "q2", "다른 질문"
        ),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    application.wait_for_generations(10)

    accepted = [value for value in outcomes if isinstance(value, RunProjectionV1)]
    rejected = [value for value in outcomes if isinstance(value, Exception)]
    assert len(accepted) == 1 and len(rejected) == 1
    assert isinstance(rejected[0], ActiveRunConflict)
    # The loser created nothing: the original Run plus exactly one new one.
    assert len(store.states[conversation.conversation_id].runs) == 2


def test_lineage_exhaustion_refuses_without_creating_a_run_message_or_call() -> None:
    provider = ScriptedProvider("fail", "fail")
    store, application = _application(provider, max_attempts=2)
    conversation, capability = application.create_conversation()
    failed = _fail_one_run(application, conversation, capability)

    retried = _retry(application, conversation, capability, "r1", failed.run_id)
    application.wait_for_generations(5)
    assert application.get_run(retried.run_id, capability).state == "failed"

    state = store.states[conversation.conversation_id]
    before = (len(state.runs), len(state.messages), len(provider.requests))
    with pytest.raises(RetryExhausted):
        _retry(application, conversation, capability, "r2", retried.run_id)
    assert (len(state.runs), len(state.messages), len(provider.requests)) == before


def test_only_the_lineage_tail_may_be_retried() -> None:
    """`retry_of_run_id`는 즉시 이전 Terminal Run을 가리킨다. With Attempts still
    left, an older member of the lineage is refused by the Tail rule itself -- not
    by the cap, and not by falling back to a re-selection it has no Snapshot for."""
    provider = ScriptedProvider("fail", "fail", "완료 답변")
    store, application = _application(provider, max_attempts=8)
    conversation, capability = application.create_conversation()
    first = _fail_one_run(application, conversation, capability)

    second = _retry(application, conversation, capability, "r1", first.run_id)
    application.wait_for_generations(5)
    assert application.get_run(second.run_id, capability).state == "failed"

    before = len(store.states[conversation.conversation_id].runs)
    with pytest.raises(RetryNotRetryable):
        _retry(application, conversation, capability, "r2", first.run_id)
    assert len(store.states[conversation.conversation_id].runs) == before

    # The Tail itself is still retryable, and the Attempt cap was never involved.
    third = _retry(application, conversation, capability, "r3", second.run_id)
    application.wait_for_generations(5)
    assert third.retry_of_run_id == second.run_id
    assert application.get_run(third.run_id, capability).state == "completed"


def test_duplicate_and_conflicting_retry_commands_do_not_consume_an_attempt() -> None:
    provider = ScriptedProvider("fail", "fail")
    store, application = _application(provider, max_attempts=2)
    conversation, capability = application.create_conversation()
    failed = _fail_one_run(application, conversation, capability)

    first = _retry(application, conversation, capability, "r1", failed.run_id)
    application.wait_for_generations(5)
    replayed = _retry(application, conversation, capability, "r1", failed.run_id)

    assert replayed.run_id == first.run_id
    state = store.states[conversation.conversation_id]
    assert len(state.runs) == 2
    assert state.runs[first.run_id].attempt == 2

    # Same key, different target -> conflict, and still no new Run.
    with pytest.raises(IdempotencyConflict):
        _retry(application, conversation, capability, "r1", first.run_id)
    assert len(state.runs) == 2


def test_a_retry_of_a_run_that_never_built_a_snapshot_reselects_the_question() -> None:
    """A Run can fail before it ever produced a request Snapshot. The Retry then
    has nothing to re-send, so it re-selects -- with the original question, never
    with an empty one."""
    provider = ScriptedProvider("완료 답변")
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    now = datetime.now(UTC)
    accepted = store.accept_question(
        conversation.conversation_id, _hash(capability), "q1", "digest", QUESTION, now,
        now + timedelta(seconds=120), time.monotonic() + 120, correlation_id=uuid4(),
    ).run
    assert store.commit_failed(
        conversation.conversation_id,
        accepted.run_id,
        application_module._provider_failure("provider_transport"),
        datetime.now(UTC),
    )

    retried = _retry(application, conversation, capability, "r1", accepted.run_id)
    application.wait_for_generations(5)

    assert application.get_run(retried.run_id, capability).state == "completed"
    assert provider.requests[-1].messages[-1].text == QUESTION
    assert provider.requests[-1].messages[-1].message_id == accepted.input_message_id


def test_a_retried_turn_keeps_the_question_s_place_in_history() -> None:
    """Q1 fails, Q2 is asked and answered, then Q1 is retried and answers. The
    lineage's place in History is the *question's*, not the Retry Run's."""
    provider = ScriptedProvider("fail", "둘째 답변", "첫째 답변", "셋째 답변")
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    first = application.submit_question(conversation.conversation_id, capability, "q1", "첫째 질문")
    application.wait_for_generations(5)
    assert application.get_run(first.run_id, capability).state == "failed"

    application.submit_question(conversation.conversation_id, capability, "q2", "둘째 질문")
    application.wait_for_generations(5)
    retried = _retry(application, conversation, capability, "r1", first.run_id)
    application.wait_for_generations(5)
    assert application.get_run(retried.run_id, capability).state == "completed"

    state = store.states[conversation.conversation_id]
    turns = state.completed_turns(datetime.now(UTC))
    assert [user.text for user, _assistant in turns] == ["첫째 질문", "둘째 질문"]

    # And that is the order the next question's context is actually built from.
    application.submit_question(conversation.conversation_id, capability, "q3", "셋째 질문")
    application.wait_for_generations(5)
    assert [message.text for message in provider.requests[-1].messages] == [
        "첫째 질문", "첫째 답변", "둘째 질문", "둘째 답변", "셋째 질문",
    ]


def test_a_cancelled_run_is_a_valid_retry_target_with_its_own_deadline() -> None:
    provider = BlockingProvider()
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    projection = application.submit_question(conversation.conversation_id, capability, "k", QUESTION)
    assert provider.started.wait(3)
    assert application.cancel_run(projection.run_id, capability).cancel_outcome == "accepted"
    application.wait_for_generations(5)

    retried = _retry(application, conversation, capability, "r1", projection.run_id)

    assert retried.retry_of_run_id == projection.run_id
    assert retried.input_message_id == projection.input_message_id
    state = store.states[conversation.conversation_id]
    original, new_run = state.runs[projection.run_id], state.runs[retried.run_id]
    # The Deadline restarts at accept_retry: a Retry does not inherit what its
    # target already spent. >=, not >, on both clocks -- either tick can be
    # coarser than two accepts in a test, and the wall clock is the coarser one
    # on Windows. What actually proves the restart is the last assertion: the
    # Retry's Deadline is computed from its own acceptance, not the target's.
    assert new_run.deadline_at >= original.deadline_at
    assert new_run.deadline_monotonic >= original.deadline_monotonic
    assert new_run.deadline_at == new_run.created_at + RUN_DEADLINE
    application.wait_for_generations(5)


def test_a_completed_or_unknown_run_cannot_be_retried() -> None:
    provider = ScriptedProvider("완료 답변")
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    projection = application.submit_question(conversation.conversation_id, capability, "k", "질문")
    application.wait_for_generations(5)
    assert application.get_run(projection.run_id, capability).state == "completed"

    state = store.states[conversation.conversation_id]
    before = (len(state.runs), len(state.messages), len(provider.requests))
    with pytest.raises(RetryNotRetryable):
        _retry(application, conversation, capability, "r1", projection.run_id)
    with pytest.raises(RunNotFound):
        _retry(application, conversation, capability, "r2", uuid4())
    assert (len(state.runs), len(state.messages), len(provider.requests)) == before


def test_the_attempt_limit_is_required_and_bounded_and_bootstrap_supplies_it() -> None:
    """No code default anywhere: the constructor demands the limit, and the wiring
    that hands it over is what the shipped deployment depends on."""
    provider = ScriptedProvider()
    with pytest.raises(TypeError):
        ChatApplication(InMemoryConversationStore(), provider)
    for invalid in (0, 9):
        with pytest.raises(ValueError):
            ChatApplication(InMemoryConversationStore(), provider, invalid)

    # conftest pins MAX_ATTEMPTS_PER_LINEAGE=3 for the suite.
    assert build_chat_application()._max_attempts_per_lineage == 3


def test_a_close_grace_that_cannot_fit_the_deadline_refuses_to_boot() -> None:
    class WideGraceProvider(ScriptedProvider):
        binding = _binding(close_grace_ms=RUN_DEADLINE_SECONDS * 1_000)

    from aidd_chat.bootstrap import _validated_close_grace

    assert _validated_close_grace(ScriptedProvider()) is not None
    with pytest.raises(BindingConfigurationError):
        _validated_close_grace(WideGraceProvider())


def test_one_input_message_contributes_exactly_one_turn_to_the_context() -> None:
    """A Retry shares the original's input Message. If the original completes late
    and the Retry completed too, the question must still appear once."""
    provider = ScriptedProvider("fail", "재시도 답변", "다음 답변")
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    failed = _fail_one_run(application, conversation, capability)
    retried = _retry(application, conversation, capability, "r1", failed.run_id)
    application.wait_for_generations(5)
    assert application.get_run(retried.run_id, capability).state == "completed"

    # The original Run completes late, out of band: two completed Runs, one
    # input_message_id -- the exact shape the dedup exists for.
    state = store.states[conversation.conversation_id]
    late = state.runs[failed.run_id]
    late.state, late.stage, late.terminal_error = "completed", "terminal", None
    late.output_message_id = late.reserved_output_message_id
    late.last_updated_at = datetime.now(UTC) + timedelta(seconds=1)
    state.messages.append(
        Message(late.reserved_output_message_id, "assistant", "늦은 답변", late.last_updated_at)
    )

    turns = state.completed_turns(datetime.now(UTC))
    assert len(turns) == 1
    assert turns[0][0].text == QUESTION
    # 가장 최신 완료 Run의 Pair가 답변을 가져간다.
    assert turns[0][1].text == "늦은 답변"

    application.submit_question(conversation.conversation_id, capability, "q9", "다음 질문")
    application.wait_for_generations(5)
    sent = provider.requests[-1]
    assert [message.text for message in sent.messages].count(QUESTION) == 1


# --- HTTP boundary ----------------------------------------------------------


def test_http_retry_envelopes_and_minimal_404() -> None:
    provider = ScriptedProvider("fail", "fail")
    store, application = _application(provider, max_attempts=2)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            first = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                json={"kind": "question", "content": "질문"},
                headers={**ORIGIN, "Idempotency-Key": "q1"},
            )
            application.wait_for_generations(5)
            failed_id = first.json()["run_id"]

            retried = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                json={"kind": "retry", "retry_of_run_id": failed_id},
                headers={**ORIGIN, "Idempotency-Key": "r1"},
            )
            application.wait_for_generations(5)

            exhausted = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                json={"kind": "retry", "retry_of_run_id": retried.json()["run_id"]},
                headers={**ORIGIN, "Idempotency-Key": "r2"},
            )
            unknown = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                json={"kind": "retry", "retry_of_run_id": str(uuid4())},
                headers={**ORIGIN, "Idempotency-Key": "r3"},
            )
            malformed = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                json={"kind": "retry", "retry_of_run_id": failed_id, "content": "몰래"},
                headers={**ORIGIN, "Idempotency-Key": "r4"},
            )
            # A duplicate Retry is replayed even while the Provider is unhealthy:
            # readiness must never turn an idempotent replay into a 503.
            application._integrity_poisoned = True
            replayed = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                json={"kind": "retry", "retry_of_run_id": failed_id},
                headers={**ORIGIN, "Idempotency-Key": "r1"},
            )
            application._integrity_poisoned = False
    finally:
        app.dependency_overrides.clear()

    assert retried.status_code == 202
    assert retried.json()["retry_of_run_id"] == failed_id
    assert retried.json()["input_message_id"] == first.json()["input_message_id"]

    assert exhausted.status_code == 409
    body = exhausted.json()["error"]
    assert body["code"] == "retry_exhausted"
    assert body["retryable"] is False
    assert body["field_errors"] == {}
    assert body["message"] == "재시도 가능 횟수를 모두 사용했어요. 새 대화를 시작해 주세요."

    assert replayed.status_code == 202
    assert replayed.json()["run_id"] == retried.json()["run_id"]

    # 미지의 Run은 Minimal 404: 본문도 code도 없다.
    assert unknown.status_code == 404 and unknown.content == b""
    assert malformed.status_code == 422

    # No rejection created a Run.
    assert len(next(iter(store.states.values())).runs) == 2


def test_http_retry_of_a_completed_run_is_a_korean_409_envelope() -> None:
    """A stale tab retrying a Run that completed in the meantime. Without its own
    branch this escapes as a 500 with no envelope at all."""
    provider = ScriptedProvider("완료 답변")
    _store, application = _application(provider)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            first = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                json={"kind": "question", "content": "질문"},
                headers={**ORIGIN, "Idempotency-Key": "q1"},
            )
            application.wait_for_generations(5)
            refused = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                json={"kind": "retry", "retry_of_run_id": first.json()["run_id"]},
                headers={**ORIGIN, "Idempotency-Key": "r1"},
            )
    finally:
        app.dependency_overrides.clear()

    assert refused.status_code == 409
    body = refused.json()["error"]
    assert body["code"] == "retry_not_allowed"
    assert body["field_errors"] == {}
    assert "다시 시도할 수 없어요" in body["message"]


def test_retrying_a_timeout_run_is_accepted_like_any_other_terminal(short_deadline) -> None:
    """`timeout` is the terminal this story introduces, and the frozen matrix names
    it a valid Retry target beside failed and cancelled."""
    short_deadline(0)
    provider = ScriptedProvider("완료 답변")
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    accepted = _accept_directly(store, conversation, capability, question=QUESTION)
    timed_out = application.get_run(accepted.run_id, capability)
    assert timed_out.state == "timeout"

    short_deadline(120)
    retried = _retry(application, conversation, capability, "r1", timed_out.run_id)
    application.wait_for_generations(5)

    assert retried.retry_of_run_id == timed_out.run_id
    assert retried.input_message_id == timed_out.input_message_id
    assert application.get_run(retried.run_id, capability).state == "completed"
    assert [message.role for message in store.states[conversation.conversation_id].messages] == [
        "user", "assistant",
    ]


def test_an_oversized_snapshotless_retry_is_refused_before_anything_is_created() -> None:
    """No Snapshot to re-send and the question no longer fits: refused like an
    oversized question, before a Run, a Message or an Attempt is spent."""
    provider = ScriptedProvider("완료 답변")
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    now = datetime.now(UTC)
    accepted = store.accept_question(
        conversation.conversation_id, _hash(capability), "q1", "digest", QUESTION, now,
        now + timedelta(seconds=120), time.monotonic() + 120, correlation_id=uuid4(),
    ).run
    assert store.commit_failed(
        conversation.conversation_id,
        accepted.run_id,
        application_module._provider_failure("provider_transport"),
        datetime.now(UTC),
    )
    state = store.states[conversation.conversation_id]
    before = (len(state.runs), len(state.messages), len(state.receipts))

    provider.binding = _binding(max_input_tokens=1)
    with pytest.raises(ContextTooLarge):
        _retry(application, conversation, capability, "r1", accepted.run_id)

    assert (len(state.runs), len(state.messages), len(state.receipts)) == before
    assert provider.requests == []


def test_a_snapshotless_retry_that_truncates_announces_it_once() -> None:
    """The `snapshot is None` fallback re-selects History, so it can truncate on its
    own -- and must say so exactly once, like any other Run that does."""
    provider = ScriptedProvider("첫 답변", "재시도 답변")
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    application.submit_question(conversation.conversation_id, capability, "k1", "첫 질문")
    application.wait_for_generations(5)

    # A second Run accepted straight through the store: it never built a Snapshot.
    now = datetime.now(UTC)
    accepted = store.accept_question(
        conversation.conversation_id, _hash(capability), "q2", "digest", QUESTION, now,
        now + timedelta(seconds=120), time.monotonic() + 120, correlation_id=uuid4(),
    ).run
    assert store.commit_failed(
        conversation.conversation_id,
        accepted.run_id,
        application_module._provider_failure("provider_transport"),
        datetime.now(UTC),
    )
    baseline = prepare_model_request(
        (PreparedMessageV1(uuid4(), "user", QUESTION),),
        provider_profile_digest=provider.provider_profile_digest,
    )
    provider.binding = _binding(max_input_tokens=provider.count_input_tokens(baseline))

    retried = _retry(application, conversation, capability, "r1", accepted.run_id)
    application.wait_for_generations(5)
    _, events = application.get_run_snapshot(retried.run_id, capability)

    truncated = [event for event in events if event.type == "context.truncated"]
    assert len(truncated) == 1 and truncated[0].dropped_turn_count == 1
    assert application.get_run(retried.run_id, capability).state == "completed"


def test_the_later_attempt_wins_a_lineage_that_completed_in_one_clock_tick() -> None:
    """Two Runs of one lineage can complete inside a single clock tick, so the
    winner is decided by Attempt, not by a timestamp comparison."""
    store, application = _application(ScriptedProvider())
    conversation, capability = application.create_conversation()
    state = store.states[conversation.conversation_id]
    moment = datetime.now(UTC)
    question = Message(uuid4(), "user", QUESTION, moment)
    state.messages.append(question)
    for attempt, answer in ((1, "첫 시도 답변"), (2, "재시도 답변")):
        reply = Message(uuid4(), "assistant", answer, moment)
        state.messages.append(reply)
        state.runs[uuid4()] = Run(
            uuid4(), conversation.conversation_id, question.message_id, reply.message_id,
            moment, moment, uuid4(), state="completed", stage="terminal",
            output_message_id=reply.message_id, attempt=attempt,
        )

    turns = state.completed_turns(datetime.now(UTC))

    assert len(turns) == 1
    assert turns[0][1].text == "재시도 답변"


def test_a_lost_timeout_cas_does_not_retro_classify_a_finished_run() -> None:
    """The cancel-then-commit window is inherent to the frozen ordering. What must
    not survive it is `timed_out` on a Run that finished on its own -- it would
    re-route that Run's later failure into a `timeout` it never had."""
    provider = ScriptedProvider("완료 답변")
    store, application = _application(provider)
    conversation, capability = application.create_conversation()
    projection = application.submit_question(conversation.conversation_id, capability, "k", QUESTION)
    application.wait_for_generations(5)
    assert application.get_run(projection.run_id, capability).state == "completed"

    # A Deadline alarm arriving after the Run already finished.
    generation = application_module._Generation(conversation.conversation_id, uuid4())
    application._generation_tasks[projection.run_id] = generation
    try:
        assert application._commit_timeout(conversation.conversation_id, projection.run_id) is False
        assert generation.timed_out is False
    finally:
        application._generation_tasks.pop(projection.run_id, None)


def test_an_observation_that_loses_the_timeout_cas_still_reports_the_terminal(short_deadline) -> None:
    """Another observer, or the alarm, may end the Run in the same window. The read
    must repeat whenever it saw a non-terminal Run, not only when its own commit
    won -- a stale `running` would leave the client waiting on a finished Run."""

    class LosingTimeoutStore(InMemoryConversationStore):
        def commit_timeout(self, *args, **kwargs) -> bool:
            super().commit_timeout(*args, **kwargs)
            return False

    short_deadline(0)
    store = LosingTimeoutStore()
    application = ChatApplication(store, ScriptedProvider(), 3)
    conversation, capability = application.create_conversation()
    accepted = _accept_directly(store, conversation, capability)

    assert application.get_run(accepted.run_id, capability).state == "timeout"


def test_a_run_that_lost_active_status_never_reaches_the_provider() -> None:
    """`record_prepared_request` returning False means this Run is no longer the
    active one. Sending anyway would produce a request no Retry could reproduce."""

    class LosingStore(InMemoryConversationStore):
        def record_prepared_request(self, *args, **kwargs) -> bool:
            return False

    provider = ScriptedProvider("완료 답변")
    store = LosingStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()
    projection = application.submit_question(conversation.conversation_id, capability, "k", QUESTION)
    application.wait_for_generations(5)

    settled = application.get_run(projection.run_id, capability)
    assert settled.state == "failed"
    assert settled.terminal_error.kind == "provider_unknown"
    assert provider.requests == []


@pytest.mark.parametrize(
    ("error", "status", "code", "retryable"),
    [
        (ConversationNotFound(), 404, None, None),
        (RunNotFound(), 404, None, None),
        (ConversationExpired(), 410, "conversation_expired", False),
        (IdempotencyConflict(), 409, "idempotency_conflict", False),
        (ActiveRunConflict(), 409, "run_already_active", True),
        (RetryExhausted(), 409, "retry_exhausted", False),
        (RetryNotRetryable(), 409, "retry_not_allowed", False),
        (ProviderProfileChanged(), 409, "provider_profile_changed", False),
        (ContextTooLarge(), 413, "context_too_large", False),
        (ContextIntegrityError(), 503, "provider_unavailable", False),
    ],
)
def test_every_retry_refusal_leaves_the_boundary_as_a_closed_envelope(
    error, status, code, retryable
) -> None:
    """The client keys its permanent/transient decision on exactly these codes and
    on `retryable`, so drift on either side degrades it to a generic sentence."""

    class RaisingChat:
        def replay_retry(self, *_args) -> None:
            return None

        def is_ready(self) -> bool:
            return True

        def retry_run(self, *_args):
            raise error

    response = main_module._retry(
        uuid4(), "capability", "key", RetryCommand(kind="retry", retry_of_run_id=uuid4()), RaisingChat()
    )

    assert response.status_code == status
    if code is None:
        assert response.body == b""
        return
    body = json.loads(response.body)["error"]
    assert body["code"] == code
    assert body["retryable"] is retryable
    assert body["field_errors"] == {}
    assert has_visible_text(body["message"])


def test_the_bootstrap_built_application_serves_retries_end_to_end() -> None:
    """No dependency override and no stubbed route: the Run, the Retry refusal and
    the Attempt limit all come from the application `build_chat_application()` wired
    together at startup."""
    with TestClient(app, base_url="https://testserver") as client:
        created = client.post("/api/v1/conversations", headers=ORIGIN)
        conversation_id = created.json()["conversation_id"]
        first = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            json={"kind": "question", "content": "질문"},
            headers={**ORIGIN, "Idempotency-Key": "q1"},
        )
        app.state.chat_application.wait_for_generations(5)
        completed = client.get(f"/api/v1/runs/{first.json()['run_id']}")
        refused = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            json={"kind": "retry", "retry_of_run_id": first.json()["run_id"]},
            headers={**ORIGIN, "Idempotency-Key": "r1"},
        )

    assert completed.json()["state"] == "completed"
    # 409, not the 500 an unwired limit or an unmapped exception would produce.
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "retry_not_allowed"


def test_bootstrap_requires_an_explicit_max_attempts_per_lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    from aidd_chat.bootstrap import BindingConfigurationError, build_deployment_settings

    monkeypatch.setenv("MAX_ATTEMPTS_PER_LINEAGE", "8")
    assert build_deployment_settings().max_attempts_per_lineage == 8
    for invalid in ("0", "9", "", "많이"):
        monkeypatch.setenv("MAX_ATTEMPTS_PER_LINEAGE", invalid)
        with pytest.raises(BindingConfigurationError):
            build_deployment_settings()
