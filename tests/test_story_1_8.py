"""Story 1.8 -- 1시간 만료·용량 제한과 공개 Endpoint Abuse Gate.

Two contracts that only show themselves under pressure. Expiry is absolute: the
Sweeper and Access-time apply the same `expires_at`, and whichever notices first
Fences the Provider out and Purges everything the Conversation held. Capacity is a
reservation: counted and refused in one lock, before any Domain state or Provider
work exists, and returned in a `finally` -- because a ceiling that is only ever
taken stops the service by itself.

Every ceiling here is driven by lowering it deliberately, never by generating real
load: a `DeploymentLimits` with `max_global_active_runs=1` takes exactly the code
path a production 64 does.
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import re
from threading import Barrier, Event, Lock, Thread
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aidd_chat.adapters import (
    DETERMINISTIC_BINDING,
    CapacityExceeded,
    ConversationExpired,
    ConversationNotFound,
    DeploymentLimits,
    InMemoryConversationStore,
    TurnLimitReached,
)
from aidd_chat.application import ActiveRunConflict, ChatApplication
from aidd_chat.bootstrap import (
    BindingConfigurationError,
    DeploymentSettingsV1,
    build_chat_application,
    build_deployment_settings,
)
from aidd_chat.contracts import ConversationExpiredEventV1, StreamEndEventV1, verify_request_integrity
from aidd_chat.domain import (
    ConversationAggregate,
    DeploymentLimits as DomainDeploymentLimits,
    expiry_stream_tail,
)
import aidd_chat.main as main_module
from aidd_chat.main import WEB_ROOT, app, client_ip, get_chat_application
from agent_fakes import LegacySyncAgent
from aidd_chat.adapters import FakeAgent
from conftest import DEPLOYMENT_SETTINGS


ORIGIN = {"Origin": "https://testserver"}
QUESTION = "만료와 상한을 확인하는 질문"


def _hash(capability: str) -> str:
    return sha256(capability.encode()).hexdigest()


def _application(provider=None, **limits) -> ChatApplication:
    return ChatApplication(
        InMemoryConversationStore(),
        provider or FakeAgent(),
        3,
        DeploymentLimits(**limits),
    )


def _expire_now(application: ChatApplication, conversation_id: UUID) -> None:
    """Move the Conversation past its Absolute Expiry -- on both clocks, because
    enforcement reads the monotonic one and the wall value is the record."""
    state = application.store.states[conversation_id]
    state.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    state.expires_monotonic = time.monotonic() - 1


class BlockingProvider(LegacySyncAgent):
    """Streams one delta and then waits until it is cancelled -- the live Run an
    expiry or a ceiling has to be observed against."""

    policy_metadata = FakeAgent().policy_metadata
    binding = DETERMINISTIC_BINDING

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.cancel_requests = 0

    def probe(self) -> bool:
        return True

    def new_call_handle(self) -> object:
        provider = self

        class Handle:
            def cancel(self) -> None:
                provider.cancel_requests += 1
                provider.release.set()

        return Handle()

    def stream(self, request, on_delta, handle=None) -> str:
        verify_request_integrity(request, self)
        on_delta("부분 ")
        self.started.set()
        self.release.wait(5)
        return "부분 "


class LongAnswerProvider(LegacySyncAgent):
    """Streams a fixed number of deltas. Used to walk a Run into the Output-byte
    and Replay ceilings without any timing involved."""

    policy_metadata = FakeAgent().policy_metadata
    binding = DETERMINISTIC_BINDING

    def __init__(self, chunk: str = "답변", count: int = 8) -> None:
        self.chunk = chunk
        self.count = count
        self.emitted = 0

    def probe(self) -> bool:
        return True

    def stream(self, request, on_delta, handle=None) -> str:
        verify_request_integrity(request, self)
        for _index in range(self.count):
            on_delta(self.chunk)
            self.emitted += 1
        return self.chunk * self.count


class CompleteOnlyProvider(LegacySyncAgent):
    """No `stream`, so it is not a StreamingModelProviderPort and its answer never
    passes through commit_delta. This is the shape the real Ollama binding can take,
    and the only path on which commit_completed is where a size is ever checked."""

    policy_metadata = FakeAgent().policy_metadata
    binding = DETERMINISTIC_BINDING

    def __init__(self, answer: str) -> None:
        self.answer = answer

    def probe(self) -> bool:
        return True

    def complete(self, request, handle=None) -> str:
        verify_request_integrity(request, self)
        return self.answer


class PinnedLookupStore(InMemoryConversationStore):
    """Reproduces the one window the identity re-check exists for: two callers that
    both resolved the Aggregate and its Lock before either of them Purged. Pinning
    the lookup makes that race a deterministic sequence instead of a coin flip."""

    def __init__(self) -> None:
        super().__init__()
        self._pinned: dict[UUID, tuple] = {}

    def _state_and_lock(self, conversation_id):
        pair = self._pinned.get(conversation_id) or super()._state_and_lock(conversation_id)
        if pair[0] is not None:
            self._pinned[conversation_id] = pair
        return pair


class GatedStartStore(InMemoryConversationStore):
    """Holds every Run in the Queued population until released, so the Queue ceiling
    -- which only exists between `reserve_run` and `start_run` -- can be observed."""

    def __init__(self) -> None:
        super().__init__()
        self.reached = Event()
        self.release = Event()

    def start_run(self, lease) -> None:
        self.reached.set()
        self.release.wait(5)
        super().start_run(lease)


def _submit(application: ChatApplication, conversation, capability, key="k", content=QUESTION):
    return application.submit_question(conversation.conversation_id, capability, key, content)


# --------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------


def test_no_activity_extends_the_absolute_ttl() -> None:
    """TTL 불연장: `expires_at` is a function of `created_at` alone. Asking,
    reading, retrying and cancelling all leave it exactly where it was."""
    application = _application()
    conversation, capability = application.create_conversation()
    assert conversation.expires_at - conversation.created_at == timedelta(seconds=3_600)
    original = application.store.states[conversation.conversation_id].expires_at

    for index in range(3):
        run = _submit(application, conversation, capability, key=f"turn-{index}")
        application.wait_for_generations()
        application.get_run(run.run_id, capability)
        application.get_run_snapshot(run.run_id, capability)

    assert application.store.states[conversation.conversation_id].expires_at == original


def test_the_sweeper_purges_a_conversation_nobody_opened() -> None:
    """Sweeper 만료: the half of expiry that exists only for a Conversation no
    request will ever touch again. Same Absolute Expiry, no second concept."""
    application = _application()
    conversation, _capability = application.create_conversation()
    conversation_id = conversation.conversation_id
    assert application.expire_due_conversations() == 0

    _expire_now(application, conversation_id)
    assert application.expire_due_conversations() == 1
    # Actually reclaimed, not merely marked: the Aggregate itself is gone.
    assert conversation_id not in application.store.states
    assert application.store.capacity_snapshot()["conversations"] == 0
    # And it is not swept twice.
    assert application.expire_due_conversations() == 0


def test_expiry_is_enforced_on_the_monotonic_clock_not_the_wall_one() -> None:
    """A stepped wall clock must neither suspend expiry nor purge everything at
    once. Story 1.7 pairs a wall Deadline with a monotonic one for exactly this
    reason; the Absolute Expiry does the same."""
    application = _application()
    stepped_back, _capability = application.create_conversation()
    state = application.store.states[stepped_back.conversation_id]
    # The wall clock still says an hour to go -- the TTL has nonetheless elapsed.
    assert state.expires_at > datetime.now(UTC)
    state.expires_monotonic = time.monotonic() - 1
    assert application.expire_due_conversations() == 1

    # And the other direction: a wall clock jumped two hours forward must not purge
    # a Conversation that has really only been alive for a moment.
    stepped_forward, capability = application.create_conversation()
    live = application.store.states[stepped_forward.conversation_id]
    live.expires_at = datetime.now(UTC) - timedelta(hours=2)
    assert application.expire_due_conversations() == 0
    assert stepped_forward.conversation_id in application.store.states
    application.authorize_conversation(stepped_forward.conversation_id, capability)


def test_exactly_one_caller_wins_the_purge_even_when_both_resolved_it_first() -> None:
    """The 410-then-404 contract rests entirely on this: `expire` returns True only
    for the caller that actually Purged. Two callers that both got past the lookup
    before either Purged must not both claim to be the first detector."""
    store = PinnedLookupStore()
    application = ChatApplication(store, FakeAgent(), 3, DeploymentLimits())
    conversation, _capability = application.create_conversation()
    now = datetime.now(UTC)
    _expire_now(application, conversation.conversation_id)

    assert store.expire(conversation.conversation_id, now) is True
    # Same pinned (state, lock) pair the loser of the race would still be holding.
    assert store.expire(conversation.conversation_id, now) is False
    assert store.expire(conversation.conversation_id, now) is False


def test_expiry_purges_messages_receipts_capability_and_the_run_index() -> None:
    """The Purge list, checked item by item against the Aggregate the Store drops
    and against the reverse index that resolves a Run to its Conversation."""
    application = _application()
    conversation, capability = application.create_conversation()
    run = _submit(application, conversation, capability)
    application.wait_for_generations()
    state = application.store.states[conversation.conversation_id]
    assert state.messages and state.runs and state.receipts

    _expire_now(application, conversation.conversation_id)
    application.expire_due_conversations()

    assert state.messages == [] and state.runs == {} and state.receipts == {}
    assert state.capability_hash == "" and state.active_run_id is None
    assert application.store._run_conversations == {}
    with pytest.raises(ConversationNotFound):
        application.get_run(run.run_id, capability)


def test_the_first_detector_gets_410_and_everything_after_it_gets_404() -> None:
    """Access-time 만료 then Purge 뒤 접근, over HTTP: exactly one 410, and the
    same Minimal 404 an unknown Resource always got for every request after it."""
    application = _application()
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            accepted = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers={**ORIGIN, "Idempotency-Key": "before-expiry"},
                json={"kind": "question", "content": QUESTION},
            )
            application.wait_for_generations()
            _expire_now(application, UUID(conversation_id))

            first = client.get(f"/api/v1/runs/{accepted.json()['run_id']}")
            second = client.get(f"/api/v1/runs/{accepted.json()['run_id']}")
            third = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers={**ORIGIN, "Idempotency-Key": "after-expiry"},
                json={"kind": "question", "content": QUESTION},
            )
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert first.status_code == 410
    body = first.json()["error"]
    assert body["code"] == "conversation_expired"
    assert body["message"] == "대화 세션이 만료되었습니다."
    for response in (second, third):
        assert response.status_code == 404
        assert response.content == b""
        assert response.headers["cache-control"] == "no-store"


def test_an_expired_capability_cannot_start_new_work() -> None:
    """만료된 Capability로 새 작업을 만들지 않는다 -- and the refusal happens
    before any Provider call, not after one."""
    provider = FakeAgent()
    application = _application(provider)
    conversation, capability = application.create_conversation()
    _expire_now(application, conversation.conversation_id)

    with pytest.raises(ConversationExpired):
        _submit(application, conversation, capability)
    assert application.store.capacity_snapshot()["active_runs"] == 0
    # Purged by that same attempt, so the Capability is now simply unknown.
    with pytest.raises(ConversationNotFound):
        _submit(application, conversation, capability, key="second")


def test_expiry_cancels_a_live_run_rather_than_evicting_it_silently() -> None:
    """만료 시 Live Run: the Provider is told to stop -- the Fence alone would
    leave the call burning the rest of its Deadline with nowhere to write."""
    provider = BlockingProvider()
    application = _application(provider)
    conversation, capability = application.create_conversation()
    _submit(application, conversation, capability)
    assert provider.started.wait(5)

    _expire_now(application, conversation.conversation_id)
    assert application.expire_due_conversations() == 1
    assert provider.release.wait(5)
    assert provider.cancel_requests == 1
    application.wait_for_generations(5)
    # Capacity came back with the task, so the ceiling did not drift.
    assert application.store.capacity_snapshot()["active_runs"] == 0


def test_the_expiry_stream_tail_is_exactly_two_events_in_one_order() -> None:
    """The Domain's own view of it: `conversation.expired` -> `stream.end`
    (`expired`), continuing the Run's own sequence, and nothing else ever."""
    now = datetime.now(UTC)
    aggregate = ConversationAggregate(
        uuid4(), now, now + timedelta(seconds=3_600), _hash("cap")
    )
    run = aggregate.accept_question("key", "digest", QUESTION, now, correlation_id=uuid4())
    aggregate.mark_running(run.run.run_id, now, time.monotonic())
    tail = aggregate.expire(now)

    assert [event.type for event in tail] == ["conversation.expired", "stream.end"]
    assert isinstance(tail[0], ConversationExpiredEventV1)
    assert isinstance(tail[1], StreamEndEventV1)
    assert tail[1].final_state == "expired"
    assert tail[0].sequence == 2 and tail[1].sequence == 3
    # A second expiry has no live Run left to describe, so it produces nothing.
    assert aggregate.expire(now) == ()


@pytest.mark.release_suite
def test_an_open_stream_is_told_the_conversation_expired_and_then_stops() -> None:
    """만료 시 SSE 열림: the stream is still writable, so it writes the two Events
    the contract fixes -- and then produces nothing at all."""
    provider = BlockingProvider()
    application = _application(provider)
    app.dependency_overrides[get_chat_application] = lambda: application
    frames: list[str] = []
    ids: list[int] = []
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            accepted = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers={**ORIGIN, "Idempotency-Key": "sse-expiry"},
                json={"kind": "question", "content": QUESTION},
            )
            assert provider.started.wait(5)
            expiry = Thread(
                target=lambda: (time.sleep(0.2), _expire_now(application, UUID(conversation_id)))
            )
            expiry.start()
            with client.stream(
                "GET", f"/api/v1/runs/{accepted.json()['run_id']}/events"
            ) as response:
                assert response.status_code == 200
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        frames.append(line[len("event: ") :])
                    elif line.startswith("id: "):
                        ids.append(int(line[len("id: ") :]))
            expiry.join()
            application.wait_for_generations(5)
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert frames[-2:] == ["conversation.expired", "stream.end"]
    # Nothing after stream.end, and the expiry tail appears exactly once.
    assert frames.count("conversation.expired") == 1
    assert frames.count("stream.end") == 1
    # The Aggregate computes the tail's sequence from its own log length and the SSE
    # boundary computes it from the client's cursor. They agree only if the log the
    # client received is gap-free and the tail continues it -- assert that, rather
    # than asserting the helper in isolation and hoping.
    assert ids == list(range(1, len(ids) + 1))


def test_a_run_that_vanishes_for_another_reason_is_not_reported_as_expiry() -> None:
    """Claiming expiry on any disappearance wipes a still-valid transcript on the
    strength of a guess. Only the read that actually detects the expiry may say so;
    every other way a Run can go missing simply ends the stream."""
    provider = BlockingProvider()
    application = _application(provider)
    app.dependency_overrides[get_chat_application] = lambda: application
    frames: list[str] = []
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = UUID(created.json()["conversation_id"])
            accepted = client.post(
                f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                headers={**ORIGIN, "Idempotency-Key": "vanish"},
                json={"kind": "question", "content": QUESTION},
            )
            assert provider.started.wait(5)

            def drop() -> None:
                # Gone, but NOT expired: exactly the case that must not be dressed
                # up as an expiry.
                time.sleep(0.2)
                application.store.states.pop(conversation_id, None)

            dropper = Thread(target=drop, daemon=True)
            dropper.start()
            with client.stream(
                "GET", f"/api/v1/runs/{accepted.json()['run_id']}/events"
            ) as response:
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        frames.append(line[len("event: ") :])
            dropper.join()
            provider.release.set()
            application.wait_for_generations(5)
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert "conversation.expired" not in frames
    assert "stream.end" not in frames


def test_the_stream_tail_helper_is_the_single_definition_both_writers_use() -> None:
    """The Aggregate and the SSE boundary must not drift into two spellings of the
    same two Events."""
    run_id = uuid4()
    now = datetime.now(UTC)
    tail = expiry_stream_tail(run_id, 7, now)
    assert [event.sequence for event in tail] == [7, 8]
    assert tail[1].final_sequence == tail[1].sequence
    assert all(event.run_id == run_id for event in tail)


# --------------------------------------------------------------------------
# Capacity ceilings
# --------------------------------------------------------------------------


def test_the_resident_conversation_ceiling_refuses_without_disturbing_a_live_run() -> None:
    """Resident Conversation 초과: the new Conversation is refused, and the one
    already generating is not sacrificed to make room for it."""
    provider = BlockingProvider()
    application = _application(provider, max_resident_conversations=1)
    conversation, capability = application.create_conversation()
    run = _submit(application, conversation, capability)
    assert provider.started.wait(5)

    with pytest.raises(CapacityExceeded) as refusal:
        application.create_conversation()
    assert refusal.value.reason == "conversation_capacity_exceeded"
    assert application.get_run(run.run_id, capability).state == "running"

    provider.release.set()
    application.wait_for_generations(5)
    assert application.get_run(run.run_id, capability).state == "completed"


def test_the_global_active_run_ceiling_creates_no_run_message_or_provider_call() -> None:
    """Global Active Run 초과: refused before accept_question, so the second
    Conversation still has no Run, no Message and no Provider call."""
    provider = BlockingProvider()
    application = _application(provider, max_global_active_runs=1)
    first, first_capability = application.create_conversation()
    second, second_capability = application.create_conversation()
    _submit(application, first, first_capability)
    assert provider.started.wait(5)

    with pytest.raises(CapacityExceeded) as refusal:
        _submit(application, second, second_capability, key="second")
    assert refusal.value.reason == "run_capacity_exceeded"
    state = application.store.states[second.conversation_id]
    assert state.runs == {} and state.messages == [] and state.receipts == {}

    provider.release.set()
    application.wait_for_generations(5)
    # 회수: the slot is free the moment the task ends.
    assert application.store.capacity_snapshot()["active_runs"] == 0
    _submit(application, second, second_capability, key="second")
    application.wait_for_generations(5)


def test_queued_and_active_are_different_populations_with_their_own_ceilings() -> None:
    """Queued 초과. A reservation is Queued until `start_run` moves it to Active, so
    the two ceilings bound different things -- and the Queue one is reachable with
    the Global one nowhere near its limit."""
    provider = BlockingProvider()
    store = GatedStartStore()
    application = ChatApplication(
        store,
        provider,
        3,
        DeploymentLimits(
            max_queued_runs=1, max_global_active_runs=8, max_session_active_runs=8,
            max_provider_concurrency=8,
        ),
    )
    first, first_capability = application.create_conversation()
    second, second_capability = application.create_conversation()
    submitter = Thread(target=_submit, args=(application, first, first_capability), daemon=True)
    submitter.start()
    assert store.reached.wait(5)
    assert store.capacity_snapshot()["queued_runs"] == 1
    assert store.capacity_snapshot()["active_runs"] == 0

    with pytest.raises(CapacityExceeded) as refusal:
        _submit(application, second, second_capability, key="queued")
    assert refusal.value.reason == "queue_capacity_exceeded"
    assert store.states[second.conversation_id].runs == {}

    store.release.set()
    provider.release.set()
    application.wait_for_generations(5)
    snapshot = store.capacity_snapshot()
    assert snapshot["queued_runs"] == 0 and snapshot["active_runs"] == 0
    # Now that nothing is queued, the same session is admitted.
    _submit(application, second, second_capability, key="queued")
    application.wait_for_generations(5)


def test_the_global_ceiling_bounds_queued_and_active_together() -> None:
    """The Queue must not be a way around the Global ceiling: a burst of
    reservations that have not started yet still counts against it."""
    store = GatedStartStore()
    application = ChatApplication(
        store, BlockingProvider(), 3,
        DeploymentLimits(max_global_active_runs=1, max_queued_runs=8, max_session_active_runs=8),
    )
    conversation, capability = application.create_conversation()
    other, other_capability = application.create_conversation()
    Thread(target=_submit, args=(application, conversation, capability), daemon=True).start()
    assert store.reached.wait(5)

    with pytest.raises(CapacityExceeded) as refusal:
        _submit(application, other, other_capability, key="global")
    assert refusal.value.reason == "run_capacity_exceeded"
    store.release.set()


def test_the_session_ceiling_refuses_one_session_and_leaves_the_others_alone() -> None:
    """Session Active Run 초과: per-session isolation. The refused session is the
    only one that notices.

    Driven at the Store, because that is the only level where this ceiling is
    observable: session identity here IS the Conversation, and the Domain already
    permits one non-terminal Run per Conversation, so `submit_question` refuses a
    second one as `run_already_active` before capacity is ever consulted. Recorded
    as deferred -- a session spanning several Conversations needs an identity this
    story does not have."""
    provider = BlockingProvider()
    application = _application(provider, max_session_active_runs=1, max_global_active_runs=8)
    busy, busy_capability = application.create_conversation()
    other, other_capability = application.create_conversation()
    _submit(application, busy, busy_capability)
    assert provider.started.wait(5)

    with pytest.raises(CapacityExceeded) as refusal:
        application.store.reserve_run(busy.conversation_id)
    assert refusal.value.reason == "session_run_capacity_exceeded"
    # The other session's slot is its own: one session at its ceiling shuts nobody
    # else out, which is the whole point of a per-session ceiling.
    lease = application.store.reserve_run(other.conversation_id)
    application.store.release_run(lease)

    # And end to end: the untouched session still gets its answer.
    provider.release.set()
    run = _submit(application, other, other_capability, key="other")
    application.wait_for_generations(5)
    assert application.get_run(run.run_id, other_capability).state in {"completed", "running"}


def test_an_answer_over_the_output_ceiling_never_becomes_a_completed_message() -> None:
    """Output Byte 초과: the refusal is a Provider failure through the existing
    mapping, and no partial answer survives as a Message."""
    provider = LongAnswerProvider(chunk="가나다라", count=8)
    application = _application(provider, max_output_bytes=16)
    conversation, capability = application.create_conversation()
    run = _submit(application, conversation, capability)
    application.wait_for_generations(5)

    projection = application.get_run(run.run_id, capability)
    assert projection.state == "failed"
    assert projection.output_message_id is None and projection.output_message is None
    # Its own Code, deterministically: the Provider did nothing wrong and must not
    # be the thing the user is told about.
    assert projection.terminal_error.kind == "capacity_exceeded"
    assert projection.terminal_error.retryable is False
    state = application.store.states[conversation.conversation_id]
    assert [message.role for message in state.messages] == ["user"]


def test_a_complete_only_provider_answer_over_the_ceiling_is_refused_too() -> None:
    """The non-streaming path never passes through commit_delta, so commit_completed
    is the only place it is ever sized -- and that is the path the real Ollama
    binding can take."""
    provider = CompleteOnlyProvider("가" * 500)
    application = _application(provider, max_output_bytes=64)
    conversation, capability = application.create_conversation()
    run = _submit(application, conversation, capability)
    application.wait_for_generations(5)

    projection = application.get_run(run.run_id, capability)
    assert projection.state == "failed"
    assert projection.terminal_error.kind == "capacity_exceeded"
    assert projection.output_message_id is None
    state = application.store.states[conversation.conversation_id]
    assert [message.role for message in state.messages] == ["user"]

    # The same Provider under a ceiling the answer fits inside still completes.
    roomy = _application(CompleteOnlyProvider("짧은 답변"), max_output_bytes=4_096)
    fits, fits_capability = roomy.create_conversation()
    completed = _submit(roomy, fits, fits_capability)
    roomy.wait_for_generations(5)
    assert roomy.get_run(completed.run_id, fits_capability).state == "completed"


def test_a_run_that_reaches_the_replay_ceiling_still_ends_inside_it() -> None:
    """Replay Event 초과: the Run is ended within the ceiling rather than stranded
    non-terminal -- the headroom the Delta commit stops short of is exactly the
    terminal quadruple it then needs."""
    provider = LongAnswerProvider(chunk="답", count=40)
    application = _application(provider, max_replay_events=8)
    conversation, capability = application.create_conversation()
    run = _submit(application, conversation, capability)
    application.wait_for_generations(5)

    projection = application.get_run(run.run_id, capability)
    assert projection.state == "failed"
    events = application.store.states[conversation.conversation_id].runs[run.run_id].events
    assert len(events) <= 8
    assert events[-1].type == "stream.end"


def test_the_replay_byte_ceiling_measures_the_whole_log_and_holds() -> None:
    """`MAX_REPLAY_BYTES` bounds what the Replay Log actually occupies: every
    serialized Event, not the delta text inside a few of them."""
    provider = LongAnswerProvider(chunk="긴답변", count=200)
    application = _application(provider, max_replay_bytes=8_192)
    conversation, capability = application.create_conversation()
    run = _submit(application, conversation, capability)
    application.wait_for_generations(5)

    stored = application.store.states[conversation.conversation_id].runs[run.run_id]
    assert application.get_run(run.run_id, capability).state == "failed"
    # The accumulator is the log, envelopes and all -- not a fraction of it.
    from aidd_chat.domain import event_size

    assert stored.replay_bytes == sum(event_size(event) for event in stored.events)
    assert stored.replay_bytes > sum(
        len(event.text.encode()) for event in stored.events if event.type == "message.delta"
    )
    # And the Run ended inside the ceiling, terminal Events included.
    assert stored.replay_bytes <= 8_192
    assert stored.events[-1].type == "stream.end"


def test_the_turn_ceiling_refuses_a_further_question_in_the_same_conversation() -> None:
    application = _application(max_turns_per_conversation=1)
    conversation, capability = application.create_conversation()
    _submit(application, conversation, capability, key="first")
    application.wait_for_generations(5)

    with pytest.raises(TurnLimitReached):
        _submit(application, conversation, capability, key="second")
    state = application.store.states[conversation.conversation_id]
    assert sum(1 for message in state.messages if message.role == "user") == 1
    # And the reservation that question took was returned, not stranded.
    assert application.store.capacity_snapshot()["active_runs"] == 0


def test_the_provider_concurrency_ceiling_creates_no_provider_call() -> None:
    provider = BlockingProvider()
    application = _application(
        provider, max_provider_concurrency=1, max_global_active_runs=8, max_session_active_runs=8
    )
    first, first_capability = application.create_conversation()
    second, second_capability = application.create_conversation()
    _submit(application, first, first_capability)
    assert provider.started.wait(5)

    with pytest.raises(CapacityExceeded) as refusal:
        _submit(application, second, second_capability, key="second")
    assert refusal.value.reason == "provider_busy"
    assert application.store.states[second.conversation_id].runs == {}

    provider.release.set()
    application.wait_for_generations(5)
    assert application.store.capacity_snapshot()["provider_calls"] == 0


def test_the_spend_circuit_breaker_opens_and_starts_no_further_provider_work() -> None:
    """Spend 한도 초과: the window's budget is charged on reservation, so a local
    binding whose money cost is zero still proves the breaker path."""
    application = _application(spend_limit_units=1, spend_window_seconds=3_600)
    conversation, capability = application.create_conversation()
    _submit(application, conversation, capability, key="first")
    application.wait_for_generations(5)
    assert application.store.capacity_snapshot()["spend_units"] == 1

    with pytest.raises(CapacityExceeded) as refusal:
        _submit(application, conversation, capability, key="second")
    assert refusal.value.reason == "spend_limit_reached"
    # Open means open: releasing the Run capacity does not re-open it.
    assert application.store.capacity_snapshot()["active_runs"] == 0
    with pytest.raises(CapacityExceeded):
        _submit(application, conversation, capability, key="third")


def test_spend_is_charged_by_provider_work_not_by_arriving_requests() -> None:
    """A replayed question runs nothing, so it costs nothing. Charging at
    reservation would let a duplicate-key flood open the breaker without ever
    reaching the Provider."""
    application = _application(spend_limit_units=4)
    conversation, capability = application.create_conversation()
    _submit(application, conversation, capability, key="once")
    application.wait_for_generations(5)
    assert application.store.capacity_snapshot()["spend_units"] == 1

    for _attempt in range(3):
        _submit(application, conversation, capability, key="once")
    assert application.store.capacity_snapshot()["spend_units"] == 1
    # The Run slots those replays reserved came back too.
    assert application.store.capacity_snapshot()["active_runs"] == 0

    # A refused Conversation costs nothing either.
    with pytest.raises(ConversationNotFound):
        application.submit_question(uuid4(), capability, "unknown", QUESTION)
    assert application.store.capacity_snapshot()["spend_units"] == 1


def test_the_spend_breaker_does_not_overshoot_under_concurrent_reservations() -> None:
    """The charge lands at `start_run`, so a check that only looked at charged units
    would let a whole Active ceiling of requests through an open breaker before any
    of them paid. Reservations count too, and this is the only shape that shows it."""
    store = GatedStartStore()
    application = ChatApplication(
        store, BlockingProvider(), 3,
        DeploymentLimits(
            spend_limit_units=2, spend_window_seconds=3_600, max_global_active_runs=8,
            max_queued_runs=8, max_session_active_runs=8, max_provider_concurrency=8,
        ),
    )
    sessions = [application.create_conversation() for _ in range(6)]
    barrier = Barrier(len(sessions))
    guard = Lock()
    accepted: list[object] = []
    refused: list[CapacityExceeded] = []

    def submit(conversation, capability) -> None:
        barrier.wait(5)
        try:
            run = _submit(application, conversation, capability, key="spend")
            with guard:
                accepted.append(run)
        except CapacityExceeded as exc:
            with guard:
                refused.append(exc)

    threads = [Thread(target=submit, args=pair, daemon=True) for pair in sessions]
    for thread in threads:
        thread.start()
    # Every admitted reservation is now parked in the queue, uncharged. `reached` is
    # set by the FIRST thread to arrive, so wait for all six to have been judged --
    # otherwise a refusal that has not yet been appended reads as no refusal at all.
    assert store.reached.wait(5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with guard:
            if len(accepted) + len(refused) == len(sessions):
                break
        time.sleep(0.01)
    with guard:
        assert len(accepted) + len(refused) == len(sessions), "not every session was judged"
        assert len(accepted) <= 2, "more reservations passed the breaker than the window allows"
        assert refused and all(exc.reason == "spend_limit_reached" for exc in refused)

    store.release.set()
    for thread in threads:
        thread.join(10)
    application.wait_for_generations(10)
    # Whatever was admitted charged exactly once, and never past the limit.
    assert store.capacity_snapshot()["spend_units"] <= 2


def test_the_spend_window_rolls_over_and_closes_the_breaker() -> None:
    application = _application(spend_limit_units=1, spend_window_seconds=1)
    conversation, capability = application.create_conversation()
    _submit(application, conversation, capability, key="first")
    application.wait_for_generations(5)
    application.store._spend_window_start = time.monotonic() - 2
    _submit(application, conversation, capability, key="second")
    application.wait_for_generations(5)
    assert application.store.capacity_snapshot()["spend_units"] == 1


def test_the_sse_connection_ceiling_refuses_new_streams_and_keeps_the_old_ones() -> None:
    """SSE Connection 초과, and the reclamation that follows: a closed connection
    frees its slot immediately."""
    application = _application(max_sse_connections=1)
    assert application.reserve_stream() is True
    assert application.reserve_stream() is False
    application.release_stream()
    assert application.reserve_stream() is True
    application.release_stream()
    assert application.store.capacity_snapshot()["sse_connections"] == 0


def test_an_http_stream_over_the_ceiling_is_a_korean_envelope() -> None:
    provider = BlockingProvider()
    application = _application(provider, max_sse_connections=0)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            accepted = client.post(
                f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                headers={**ORIGIN, "Idempotency-Key": "stream-ceiling"},
                json={"kind": "question", "content": QUESTION},
            )
            assert provider.started.wait(5)
            refused = client.get(f"/api/v1/runs/{accepted.json()['run_id']}/events")
            provider.release.set()
            application.wait_for_generations(5)
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert refused.status_code == 503
    body = refused.json()["error"]
    assert body["code"] == "stream_capacity_exceeded"
    assert body["retryable"] is True
    assert "잠시 후" in body["message"]


def test_a_finished_stream_gives_its_slot_back_over_real_http() -> None:
    """The reservation is worthless without the release: with one slot, a stream
    that ran to `stream.end` must leave the next one able to open. Deleting the
    release makes the second stream a 503 and the ceiling never recovers."""
    application = _application(max_sse_connections=1)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            accepted = client.post(
                f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                headers={**ORIGIN, "Idempotency-Key": "slot"},
                json={"kind": "question", "content": QUESTION},
            )
            run_id = accepted.json()["run_id"]
            with client.stream("GET", f"/api/v1/runs/{run_id}/events") as response:
                assert response.status_code == 200
                frames = [line for line in response.iter_lines() if line.startswith("event: ")]
            assert frames[-1] == "event: stream.end"
            assert application.store.capacity_snapshot()["sse_connections"] == 0

            # The slot really came back: a second stream opens on the same ceiling.
            replay = client.get(
                f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "1"}
            )
            assert replay.status_code == 200
            assert application.store.capacity_snapshot()["sse_connections"] == 0
    finally:
        app.dependency_overrides.pop(get_chat_application, None)


class PastDeadlineStore(InMemoryConversationStore):
    """Reports every Run's monotonic Deadline as already gone, so the stream's own
    close-out is what ends the connection -- not the Run reaching a terminal, which
    is what a short real Deadline would test instead."""

    def run_deadline_monotonic(self, run_id, capability_hash):
        return time.monotonic() - 1


def test_a_stream_ends_on_its_own_when_the_runs_deadline_passes() -> None:
    """The bound is what is left of THIS Run's Deadline, read from the monotonic
    value the Run stores -- so a stuck Run cannot hold a connection, or its SSE
    slot, open indefinitely, and a client reconnecting with Last-Event-ID does not
    get a fresh window."""
    provider = BlockingProvider()
    application = ChatApplication(
        PastDeadlineStore(), provider, 3, DeploymentLimits(max_sse_connections=1)
    )
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            accepted = client.post(
                f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                headers={**ORIGIN, "Idempotency-Key": "bounded"},
                json={"kind": "question", "content": QUESTION},
            )
            assert provider.started.wait(5)
            run_id = accepted.json()["run_id"]
            started = time.monotonic()
            with client.stream("GET", f"/api/v1/runs/{run_id}/events") as response:
                frames = [line for line in response.iter_lines() if line.startswith("event: ")]
            elapsed = time.monotonic() - started
            # It closed itself while the Run was still live: no terminal, no
            # stream.end -- the connection simply stopped being held open.
            assert elapsed < 5
            assert "event: stream.end" not in frames

            assert application.store.capacity_snapshot()["sse_connections"] == 0

            # A reconnect gets what is left of the SAME Deadline, not a new one.
            reconnect = time.monotonic()
            with client.stream(
                "GET", f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "1"}
            ) as response:
                list(response.iter_lines())
            assert time.monotonic() - reconnect < 5
            provider.release.set()
            application.wait_for_generations(5)
    finally:
        app.dependency_overrides.pop(get_chat_application, None)


# --------------------------------------------------------------------------
# Rates and client IP
# --------------------------------------------------------------------------


def test_the_poll_rate_ceiling_changes_no_domain_state() -> None:
    application = _application(poll_rate_per_minute=2)
    conversation, capability = application.create_conversation()
    run = _submit(application, conversation, capability)
    application.wait_for_generations(5)
    before = application.store.states[conversation.conversation_id].runs[run.run_id].state

    application.get_run(run.run_id, capability)
    application.get_run(run.run_id, capability)
    with pytest.raises(CapacityExceeded) as refusal:
        application.get_run(run.run_id, capability)
    assert refusal.value.reason == "rate_limited"
    assert application.store.states[conversation.conversation_id].runs[run.run_id].state == before


def test_opening_a_stream_is_charged_against_the_poll_ceiling() -> None:
    """Opening a stream is a session read like any other, so it is charged -- and
    the endpoint has to answer the refusal with the same typed envelope a poll does,
    rather than letting it escape as a 500."""
    application = _application(poll_rate_per_minute=2, max_requests_per_ip_per_minute=1_000)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            accepted = client.post(
                f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                headers={**ORIGIN, "Idempotency-Key": "stream-rate"},
                json={"kind": "question", "content": QUESTION},
            )
            application.wait_for_generations(5)
            run_id = accepted.json()["run_id"]
            first = client.get(f"/api/v1/runs/{run_id}")
            second = client.get(f"/api/v1/runs/{run_id}")
            refused = client.get(f"/api/v1/runs/{run_id}/events")
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert first.status_code == second.status_code == 200
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "rate_limited"


def test_the_submit_rate_ceiling_refuses_before_anything_is_created() -> None:
    """Submit is the fourth per-session rate, beside poll, retry and cancel. Without
    it one session hammers cheap-to-refuse submits at the full per-IP budget."""
    application = _application(submit_rate_per_minute=1)
    conversation, capability = application.create_conversation()
    _submit(application, conversation, capability, key="first")
    application.wait_for_generations(5)

    with pytest.raises(CapacityExceeded) as refusal:
        _submit(application, conversation, capability, key="second")
    assert refusal.value.reason == "rate_limited"
    state = application.store.states[conversation.conversation_id]
    assert sum(1 for message in state.messages if message.role == "user") == 1
    assert application.store.capacity_snapshot()["active_runs"] == 0

    # A different session has its own budget.
    other, other_capability = application.create_conversation()
    _submit(application, other, other_capability, key="other")
    application.wait_for_generations(5)


def test_the_cancel_rate_ceiling_refuses_before_the_terminal_commit() -> None:
    provider = BlockingProvider()
    application = _application(provider, cancel_rate_per_minute=1)
    conversation, capability = application.create_conversation()
    run = _submit(application, conversation, capability)
    assert provider.started.wait(5)

    assert application.cancel_run(run.run_id, capability).cancel_outcome == "accepted"
    with pytest.raises(CapacityExceeded):
        application.cancel_run(run.run_id, capability)
    application.wait_for_generations(5)
    assert application.get_run(run.run_id, capability, rate_limited=False).state == "cancelled"


def test_the_retry_rate_ceiling_is_refused_before_an_attempt_is_consumed() -> None:
    from aidd_chat.contracts import RetryCommand

    provider = BlockingProvider()
    application = _application(provider, retry_rate_per_minute=1)
    conversation, capability = application.create_conversation()
    run = _submit(application, conversation, capability)
    assert provider.started.wait(5)
    application.cancel_run(run.run_id, capability)
    application.wait_for_generations(5)

    provider.release.clear()
    provider.started.clear()
    command = RetryCommand(kind="retry", retry_of_run_id=run.run_id)
    application.retry_run(conversation.conversation_id, capability, "retry-1", command)
    assert provider.started.wait(5)
    provider.release.set()
    application.wait_for_generations(5)

    lineage_before = len(application.store.states[conversation.conversation_id].runs)
    with pytest.raises(CapacityExceeded) as refusal:
        application.retry_run(conversation.conversation_id, capability, "retry-2", command)
    assert refusal.value.reason == "rate_limited"
    assert len(application.store.states[conversation.conversation_id].runs) == lineage_before


def test_a_foreign_capability_never_occupies_capacity() -> None:
    """Reserving before authorizing lets a stale, guessed or foreign Capability
    transiently hold a Run slot, a session slot and a Provider slot. With one slot
    in the whole process, a live Run proves it does not."""
    provider = BlockingProvider()
    application = _application(
        provider, max_global_active_runs=1, max_queued_runs=1, max_provider_concurrency=1
    )
    conversation, capability = application.create_conversation()
    victim = _submit(application, conversation, capability)
    assert provider.started.wait(5)

    for bogus in ("stale-capability", "another-one"):
        with pytest.raises(ConversationNotFound):
            application.submit_question(uuid4(), bogus, "bogus", QUESTION)
    with pytest.raises(ConversationNotFound):
        application.submit_question(conversation.conversation_id, "wrong-capability", "k", QUESTION)

    provider.release.set()
    application.wait_for_generations(5)
    assert application.get_run(victim.run_id, capability).state == "completed"


def test_a_duplicate_key_is_replayed_even_when_every_ceiling_is_full() -> None:
    """The moment a client is most likely to be retrying is exactly the moment the
    ceilings are full. A replay must replay, not 503: the Run already exists and
    answering it costs no capacity at all."""
    provider = BlockingProvider()
    application = _application(
        provider, max_global_active_runs=1, max_queued_runs=1, max_provider_concurrency=1
    )
    conversation, capability = application.create_conversation()
    first = _submit(application, conversation, capability, key="dup")
    assert provider.started.wait(5)
    # Nothing is free: a genuinely new question is refused right now.
    other, other_capability = application.create_conversation()
    with pytest.raises(CapacityExceeded):
        _submit(application, other, other_capability, key="new")

    replayed = _submit(application, conversation, capability, key="dup")
    assert replayed.run_id == first.run_id
    provider.release.set()
    application.wait_for_generations(5)


def test_x_forwarded_for_is_ignored_without_an_explicit_trusted_proxy_setting() -> None:
    """Trusted Proxy 미설정: the header is text the client wrote. Only the Socket
    Peer counts, so a spoofed header cannot mint a fresh rate-limit identity."""
    application = _application(max_requests_per_ip_per_minute=1, trusted_proxy_hops=0)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            first = client.post(
                "/api/v1/conversations", headers={**ORIGIN, "X-Forwarded-For": "203.0.113.1"}
            )
            second = client.post(
                "/api/v1/conversations", headers={**ORIGIN, "X-Forwarded-For": "203.0.113.2"}
            )
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"


def test_the_ip_gate_is_in_front_of_every_public_endpoint() -> None:
    """README claims every public endpoint is gated. Deleting the gate from any one
    of these leaves that endpoint unlimited, which is exactly how an attacker polls
    or streams for free."""
    application = _application(max_requests_per_ip_per_minute=8, poll_rate_per_minute=1_000)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            accepted = client.post(
                f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                headers={**ORIGIN, "Idempotency-Key": "gated"},
                json={"kind": "question", "content": QUESTION},
            )
            application.wait_for_generations(5)
            run_id = accepted.json()["run_id"]
            conversation_id = created.json()["conversation_id"]
            # Spend what is left of the budget, then check every endpoint agrees it
            # is gone. Draining through one endpoint is deliberate: the gate is
            # per-IP, so every other endpoint must already be out of budget too.
            for _attempt in range(16):
                if client.get("/api/v1/policy").status_code == 429:
                    break
            else:
                raise AssertionError("the per-IP gate never refused anything")
            # The budget is long gone; every public endpoint has to say so.
            refused = {
                "policy": client.get("/api/v1/policy"),
                "create": client.post("/api/v1/conversations", headers=ORIGIN),
                "submit": client.post(
                    f"/api/v1/conversations/{conversation_id}/runs",
                    headers={**ORIGIN, "Idempotency-Key": "gated-2"},
                    json={"kind": "question", "content": QUESTION},
                ),
                "poll": client.get(f"/api/v1/runs/{run_id}"),
                "cancel": client.post(f"/api/v1/runs/{run_id}/cancel", headers=ORIGIN),
                "stream": client.get(f"/api/v1/runs/{run_id}/events"),
            }
            # Health checks are deliberately NOT gated: reporting the platform down
            # under load is worse than an ungated probe.
            live, ready = client.get("/live"), client.get("/ready")
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    for name, response in refused.items():
        assert response.status_code == 429, name
        assert response.json()["error"]["code"] == "rate_limited", name
    assert live.status_code == 204
    assert ready.status_code in {204, 503}


def test_one_clients_ip_budget_does_not_touch_another_clients() -> None:
    application = _application(max_requests_per_ip_per_minute=1)
    assert application.allow_client_ip("203.0.113.1") is True
    assert application.allow_client_ip("203.0.113.1") is False
    # A different peer has its own budget, untouched by the first one's refusal.
    assert application.allow_client_ip("203.0.113.2") is True
    # An unattributable peer -- an ASGI server that reports no client -- shares one
    # key rather than being waved through. Asserted twice on purpose: a single True
    # is equally consistent with the gate being off for it entirely.
    assert application.allow_client_ip("") is True
    assert application.allow_client_ip("") is False


def test_a_declared_proxy_hop_count_strips_exactly_that_many_entries() -> None:
    class _Client:
        host = "10.0.0.9"

    class _Request:
        client = _Client()

        def __init__(self, forwarded: str | None) -> None:
            self.headers = {} if forwarded is None else {"x-forwarded-for": forwarded}

    trusting = _application(trusted_proxy_hops=1)
    untrusting = _application(trusted_proxy_hops=0)
    assert client_ip(_Request("203.0.113.5, 10.0.0.1"), trusting) == "10.0.0.1"
    assert client_ip(_Request("203.0.113.5"), trusting) == "203.0.113.5"
    # Fewer entries than declared hops means the header is unusable; fall back.
    assert client_ip(_Request(None), trusting) == "10.0.0.9"
    assert client_ip(_Request("203.0.113.5, 10.0.0.1"), untrusting) == "10.0.0.9"

    # With two or more declared hops the client owns that slot outright. Anything
    # that is not an address is refused rather than becoming a rate-limiter key of
    # the caller's own choosing and length.
    two_hops = _application(trusted_proxy_hops=2)
    assert client_ip(_Request("203.0.113.5, 10.0.0.2, 10.0.0.1"), two_hops) == "10.0.0.2"
    # The entries that count are the ones OUR proxies appended -- the rightmost. A
    # client that prepends junk must not be able to push them out of the parsed
    # window and hand itself the selected slot; every junk entry here is distinct,
    # so a leftmost slice would select one of them.
    flood = ", ".join(f"198.51.100.{index % 250}" for index in range(500))
    assert client_ip(_Request(f"{flood}, 10.0.0.2, 10.0.0.1"), two_hops) == "10.0.0.2"
    assert client_ip(_Request(f"{flood}, 10.0.0.2, 10.0.0.1"), trusting) == "10.0.0.1"
    assert client_ip(_Request(f"10.0.0.3, {'x' * 4_000}, 10.0.0.1"), two_hops) == "10.0.0.9"
    assert client_ip(_Request("not-an-ip"), trusting) == "10.0.0.9"
    # Normalized, so one address cannot be spelled several ways for several budgets.
    assert client_ip(_Request("0:0:0:0:0:0:0:1"), trusting) == "::1"
    # Bounded parse: a header of thousands of entries costs a fixed slice.
    assert client_ip(_Request(", ".join(["10.0.0.1"] * 5_000)), trusting) == "10.0.0.1"


# --------------------------------------------------------------------------
# Readiness gate and settings
# --------------------------------------------------------------------------


def test_every_ceiling_is_a_required_deployment_setting() -> None:
    """상한 미설정 detection happens in the settings model itself: a missing or
    out-of-range value fails validation and names the field, never its value."""
    # trusted_proxy_hops is the ONLY sanctioned default -- the Code Map names it,
    # and 0 is the safe direction. Everything else, spend_unit included, is a
    # number or a unit an operator has to choose.
    required = set(DeploymentSettingsV1.model_fields) - {"trusted_proxy_hops"}
    assert required == {
        "max_attempts_per_lineage",
        "max_resident_conversations",
        "max_global_active_runs",
        "max_session_active_runs",
        "max_queued_runs",
        "max_turns_per_conversation",
        "max_output_bytes",
        "max_replay_events",
        "max_replay_bytes",
        "max_sse_connections",
        "create_rate_per_minute",
        "submit_rate_per_minute",
        "poll_rate_per_minute",
        "retry_rate_per_minute",
        "cancel_rate_per_minute",
        "max_requests_per_ip_per_minute",
        "max_provider_concurrency",
        "spend_window_seconds",
        "spend_limit_units",
        "spend_unit",
    }
    for name in DeploymentSettingsV1.model_fields:
        if name == "trusted_proxy_hops":
            continue
        assert DeploymentSettingsV1.model_fields[name].is_required(), name


def test_the_settings_map_one_for_one_onto_the_limits_every_layer_reads() -> None:
    limits = build_deployment_settings().limits()
    assert isinstance(limits, DomainDeploymentLimits)
    assert set(DomainDeploymentLimits.__dataclass_fields__) <= set(DeploymentSettingsV1.model_fields)
    assert limits.trusted_proxy_hops == 0 and limits.spend_unit == "provider_call"


@pytest.mark.parametrize(
    "name, value",
    [
        ("MAX_SSE_CONNECTIONS", None),
        ("MAX_GLOBAL_ACTIVE_RUNS", "0"),
        ("SPEND_WINDOW_SECONDS", "0"),
        ("MAX_PROVIDER_CONCURRENCY", "999999"),
    ],
)
def test_a_missing_or_out_of_range_ceiling_refuses_the_settings(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    with pytest.raises(BindingConfigurationError) as failure:
        build_deployment_settings()
    # The field name, and never the value the operator supplied.
    message = str(failure.value)
    assert name in message
    if value:
        assert value not in message


def test_a_process_without_valid_limits_starts_but_serves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """상한 미설정: `/ready` says 503, questions are refused -- and so is creating a
    Conversation, which is the one path that never consults readiness. Falling back
    to the permissive in-process defaults there would hand out a million
    Conversations under numbers nobody chose. The process still starts, because an
    operator has to be able to ask it what is wrong."""
    monkeypatch.delenv("MAX_REPLAY_EVENTS", raising=False)
    application = build_chat_application()
    assert application.is_ready() is False
    # Every ceiling, not only the ones the Readiness Gate stands in front of: the
    # Gate does not cover creation, /api/v1/policy or the stream endpoint.
    for name in DomainDeploymentLimits.__dataclass_fields__:
        value = getattr(application.limits, name)
        if isinstance(value, int):
            assert value <= 1, name

    # Refused by name at the Application too, not only at the boundary: only a
    # redeploy clears this, so it must never be advertised as worth waiting for.
    with pytest.raises(CapacityExceeded) as refusal:
        application.create_conversation()
    assert refusal.value.reason == "deployment_unconfigured"
    assert refusal.value.retryable is False

    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            ready = client.get("/ready")
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            policy = client.get("/api/v1/policy")
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert ready.status_code == 503
    assert created.status_code == 503
    body = created.json()["error"]
    # Only a redeploy clears this, so it must not be advertised as worth waiting
    # for -- and it must not carry a Retry-After either.
    assert body["code"] == "deployment_unconfigured"
    assert body["retryable"] is False
    assert "retry-after" not in {key.lower() for key in created.headers}
    # The endpoints the Readiness Gate does not stand in front of are shut too, and
    # they name the same real reason rather than blaming a rate limit.
    assert policy.status_code == 503
    assert policy.json()["error"]["code"] == "deployment_unconfigured"


def test_the_missing_setting_is_named_in_the_startup_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`.env.example` and the spec's manual check both promise the operator the name
    of the offending setting. `build_deployment_settings` composes exactly that --
    names, no values -- so the startup path has to log the message, not its type."""
    monkeypatch.delenv("MAX_REPLAY_EVENTS", raising=False)
    monkeypatch.setenv("MAX_SSE_CONNECTIONS", "0")
    with caplog.at_level("ERROR"):
        build_chat_application()
    logged = " | ".join(record.getMessage() for record in caplog.records)
    assert "MAX_REPLAY_EVENTS" in logged and "MAX_SSE_CONNECTIONS" in logged
    # Names only: the ceilings themselves are not published to a log.
    assert not any(char.isdigit() for char in logged)


def test_settings_that_only_work_together_are_validated_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three rules no single field can express. Each was a sentence of advice in
    `.env.example`, and a deployment that ignored one looked healthy while silently
    losing a ceiling."""
    cases = {
        # A queue ceiling at or above the global one can never be reached.
        "MAX_QUEUED_RUNS": DEPLOYMENT_SETTINGS["MAX_GLOBAL_ACTIVE_RUNS"],
        # An IP budget at or below the poll rate rate-limits one ordinary browser.
        "MAX_REQUESTS_PER_IP_PER_MINUTE": DEPLOYMENT_SETTINGS["POLL_RATE_PER_MINUTE"],
        # A replay ceiling that cannot hold the answer twice bounds nothing useful.
        "MAX_REPLAY_BYTES": DEPLOYMENT_SETTINGS["MAX_OUTPUT_BYTES"],
    }
    for name, value in cases.items():
        monkeypatch.setenv(name, value)
        with pytest.raises(BindingConfigurationError):
            build_deployment_settings()
        monkeypatch.setenv(name, DEPLOYMENT_SETTINGS[name])
    # The pinned suite values satisfy all three, which is what makes the above a
    # test of the rules rather than of the fixture.
    assert build_deployment_settings().limits() is not None


def test_a_provider_that_cannot_be_built_also_starts_unready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator can easily have a bad ceiling AND a bad binding. Crashing on the
    second defeats the point of surviving the first."""
    monkeypatch.delenv("MAX_REPLAY_EVENTS", raising=False)
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "public_demo")
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    application = build_chat_application()
    assert application.is_ready() is False
    assert application.policy_projection is None


def test_valid_limits_are_what_make_a_ready_process_ready() -> None:
    provider = FakeAgent()
    store = InMemoryConversationStore()
    assert ChatApplication(store, provider, 3, DeploymentLimits()).is_ready() is True
    assert ChatApplication(InMemoryConversationStore(), provider, 3, None).is_ready() is False


def test_bootstrap_hands_the_store_the_configured_ceilings() -> None:
    application = build_chat_application()
    assert application.store.limits is application.limits
    # Every ceiling the Store enforces is the one the environment configured -- read
    # from the same pins the suite sets, not a number copied into this assertion.
    for name in DomainDeploymentLimits.__dataclass_fields__:
        expected = DEPLOYMENT_SETTINGS[name.upper()]
        actual = getattr(application.limits, name)
        assert str(actual) == expected, name


# --------------------------------------------------------------------------
# Cross-session isolation and the load-shed contract
# --------------------------------------------------------------------------


@pytest.mark.release_suite
def test_cross_session_read_submit_stream_and_cancel_are_all_minimal_404() -> None:
    application = _application()
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            mine = client.post("/api/v1/conversations", headers=ORIGIN)
            my_conversation = mine.json()["conversation_id"]
            accepted = client.post(
                f"/api/v1/conversations/{my_conversation}/runs",
                headers={**ORIGIN, "Idempotency-Key": "mine"},
                json={"kind": "question", "content": QUESTION},
            )
            run_id = accepted.json()["run_id"]
            application.wait_for_generations(5)
            # A second session: a fresh Capability cookie replaces the first.
            client.post("/api/v1/conversations", headers=ORIGIN)

            responses = {
                "read": client.get(f"/api/v1/runs/{run_id}"),
                "submit": client.post(
                    f"/api/v1/conversations/{my_conversation}/runs",
                    headers={**ORIGIN, "Idempotency-Key": "foreign"},
                    json={"kind": "question", "content": QUESTION},
                ),
                "stream": client.get(f"/api/v1/runs/{run_id}/events"),
                "cancel": client.post(f"/api/v1/runs/{run_id}/cancel", headers=ORIGIN),
            }
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    for name, response in responses.items():
        assert response.status_code == 404, name
        assert response.content == b"", name


def test_the_conversation_ceiling_over_http_is_a_korean_envelope() -> None:
    application = _application(max_resident_conversations=1)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            first = client.post("/api/v1/conversations", headers=ORIGIN)
            second = client.post("/api/v1/conversations", headers=ORIGIN)
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert first.status_code == 201
    assert second.status_code == 503
    body = second.json()["error"]
    assert body["code"] == "conversation_capacity_exceeded"
    assert body["retryable"] is True
    assert "잠시 후" in body["message"]


def test_the_rate_table_is_bounded_and_reclaimed() -> None:
    """Every other resource here has a ceiling; the rate table needs one too, or a
    caller that can mint distinct subjects mints unbounded memory."""
    application = _application(max_requests_per_ip_per_minute=5)
    store = application.store
    for index in range(50):
        application.allow_client_ip(f"203.0.113.{index}")
    assert store.capacity_snapshot()["rate_keys"] == 50

    # The Sweeper reclaims what the window has retired.
    store._rates = {key: (value[0] - 120, value[1]) for key, value in store._rates.items()}
    application.expire_due_conversations()
    assert store.capacity_snapshot()["rate_keys"] == 0

    # At the cap the OLDEST entries go and the newcomer is admitted. Shedding the
    # newcomer instead would refuse every arriving session while the entries already
    # in the table keep working -- a denial of service aimed at legitimate traffic,
    # and reachable in minutes at the shipped per-IP budget.
    from aidd_chat.adapters.memory import MAX_RATE_KEYS, RATE_EVICTION_BATCH

    store._rates = {
        (f"ip:filler-{index}", "request"): (time.monotonic(), 0)
        for index in range(MAX_RATE_KEYS)
    }
    oldest = ("ip:filler-0", "request")
    assert application.allow_client_ip("203.0.113.254") is True
    assert store.capacity_snapshot()["rate_keys"] < MAX_RATE_KEYS
    assert oldest not in store._rates
    # The eviction is a bounded batch, so it happens once per batch of new keys
    # rather than scanning the whole table on every request at the cap.
    assert store.capacity_snapshot()["rate_keys"] == MAX_RATE_KEYS - RATE_EVICTION_BATCH + 1


@pytest.mark.release_suite
def test_the_load_shed_contract_holds_under_concurrent_sessions() -> None:
    """The one thing no sequential test can show: many sessions arriving at the
    ceiling at once. The Global ceiling is never exceeded, every refusal is typed,
    and once the storm passes the capacity is all back."""
    provider = BlockingProvider()
    application = _application(
        provider,
        max_global_active_runs=2,
        max_session_active_runs=1,
        max_provider_concurrency=8,
        spend_limit_units=1_000,
    )
    sessions = [application.create_conversation() for _ in range(8)]
    barrier = Barrier(len(sessions))
    guard = Lock()
    accepted: list[object] = []
    refused: list[CapacityExceeded] = []
    peak = {"value": 0}

    def submit(conversation, capability) -> None:
        barrier.wait(5)
        try:
            run = _submit(application, conversation, capability, key="load")
            snapshot = application.store.capacity_snapshot()
            # Every shared list and counter is written under one lock: eight threads
            # read-modify-writing `peak` is exactly the race this test exists to
            # catch elsewhere, and it must not be one here.
            with guard:
                accepted.append(run)
                peak["value"] = max(peak["value"], snapshot["active_runs"] + snapshot["queued_runs"])
        except CapacityExceeded as exc:
            with guard:
                refused.append(exc)

    threads = [
        Thread(target=submit, args=(conversation, capability))
        for conversation, capability in sessions
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    # Both bounds. `<= 2` alone passes a gate that is broken closed and admits
    # nobody, which is indistinguishable from a correct one without a lower bound.
    assert 1 <= len(accepted) <= 2
    assert len(accepted) + len(refused) == len(sessions)
    assert refused, "the ceiling has to actually bite for this to prove anything"
    assert all(
        exc.reason in {"run_capacity_exceeded", "session_run_capacity_exceeded", "provider_busy"}
        for exc in refused
    )
    assert peak["value"] <= 2

    provider.release.set()
    application.wait_for_generations(10)
    snapshot = application.store.capacity_snapshot()
    assert snapshot["active_runs"] == 0 and snapshot["queued_runs"] == 0
    assert snapshot["provider_calls"] == 0


def test_every_capacity_refusal_has_a_korean_typed_envelope() -> None:
    """No refusal may reach a user as an untyped 500 or an English sentence."""
    chat = _application()
    for reason in main_module._CAPACITY_REJECTIONS:
        response = main_module.capacity_response(CapacityExceeded(reason), chat)
        body = json.loads(response.body)["error"]
        assert body["code"] == reason
        assert response.status_code in {429, 503}
        assert body["message"].endswith("주세요.")
        assert any("가" <= char <= "힣" for char in body["message"])
        # Never the ceiling itself, only which ceiling.
        assert not any(char.isdigit() for char in body["message"])
        UUID(body["correlation_id"])


def test_the_sweeper_can_win_the_purge_and_an_open_stream_still_learns_of_it() -> None:
    """The frozen AC makes the tail a property of the stream being writable, not of
    which caller detected the expiry. The Sweeper -- or any concurrent poll, submit
    or cancel -- may Purge first, and the stream still has to say what happened
    rather than ending silently and leaving the client to a 404."""
    provider = BlockingProvider()
    application = _application(provider)
    app.dependency_overrides[get_chat_application] = lambda: application
    frames: list[str] = []
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = UUID(created.json()["conversation_id"])
            accepted = client.post(
                f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                headers={**ORIGIN, "Idempotency-Key": "sweeper-wins"},
                json={"kind": "question", "content": QUESTION},
            )
            assert provider.started.wait(5)

            def sweep() -> None:
                time.sleep(0.2)
                _expire_now(application, conversation_id)
                # The Sweeper, not the stream, is the first detector here.
                assert application.expire_due_conversations() == 1

            sweeper = Thread(target=sweep, daemon=True)
            sweeper.start()
            with client.stream(
                "GET", f"/api/v1/runs/{accepted.json()['run_id']}/events"
            ) as response:
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        frames.append(line[len("event: ") :])
            sweeper.join()
            provider.release.set()
            application.wait_for_generations(5)
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert frames[-2:] == ["conversation.expired", "stream.end"]


def test_every_reader_of_an_expiry_gets_the_aggregates_own_sequence_numbers() -> None:
    """The tail is built once, by the Aggregate, from that Run's own log length --
    not from whatever cursor a particular client happened to be holding. Two clients
    on one Run, or one reconnecting with a stale Last-Event-ID, must not be handed
    different sequence numbers for the same Event."""
    provider = BlockingProvider()
    application = _application(provider)
    conversation, capability = application.create_conversation()
    run = _submit(application, conversation, capability)
    assert provider.started.wait(5)
    log_length = len(application.store.states[conversation.conversation_id].runs[run.run_id].events)

    _expire_now(application, conversation.conversation_id)
    assert application.expire_due_conversations() == 1
    provider.release.set()
    application.wait_for_generations(5)

    caught_up = application.expired_stream_tail(run.run_id, capability, log_length)
    stale_cursor = application.expired_stream_tail(run.run_id, capability, 0)
    # The reader that is behind and the reader that is caught up must be handed the
    # SAME numbers. Derived from a cursor instead, these two would disagree.
    assert [event.sequence for event in stale_cursor] == [event.sequence for event in caught_up]
    assert [event.type for event in caught_up] == ["conversation.expired", "stream.end"]
    # The reader behind by a few Events sees the same two Events with the same
    # numbers -- it simply also sees nothing it had already consumed filtered out.
    assert [event.sequence for event in caught_up] == [log_length + 1, log_length + 2]
    assert stale_cursor[-2:] == caught_up
    assert caught_up[1].final_sequence == caught_up[1].sequence
    # And the Capability still guards it: the tail is not readable by anyone else.
    assert application.expired_stream_tail(run.run_id, "another-capability", 0) is None
    # A Run that was never expired has no tail at all, so a disappearance for any
    # other reason can never be dressed up as one.
    assert application.expired_stream_tail(uuid4(), capability, 0) is None


def test_the_replay_ceilings_bind_a_complete_only_provider_too() -> None:
    """`commit_completed` appends the whole answer again as `message.completed`, so
    the Replay ceilings have to bind it -- not just `max_output_bytes`. A
    `complete`-only Provider is the shape the real Ollama binding takes."""
    provider = CompleteOnlyProvider("가" * 4_000)
    application = _application(provider, max_output_bytes=4_194_304, max_replay_bytes=4_096)
    conversation, capability = application.create_conversation()
    run = _submit(application, conversation, capability)
    application.wait_for_generations(5)

    projection = application.get_run(run.run_id, capability)
    assert projection.state == "failed"
    assert projection.terminal_error.kind == "capacity_exceeded"
    stored = application.store.states[conversation.conversation_id].runs[run.run_id]
    assert stored.replay_bytes <= 4_096
    assert [message.role for message in application.store.states[
        conversation.conversation_id
    ].messages] == ["user"]


def test_json_escaping_cannot_smuggle_an_answer_past_the_replay_ceiling() -> None:
    """The stored Events are JSON, so a ceiling measured on raw bytes is a ceiling an
    escape-heavy answer walks straight through. Story 1.9's Validator refuses control
    characters outright, so the escaping a Delta can still carry is the ordinary kind
    -- quotes and backslashes.

    Both texts are exactly 64 characters, deliberately: a comparison between a long
    escape-heavy string and a short plain one passes even for `event_size = len(text)`,
    which is the precise regression this test exists to catch."""
    from aidd_chat.domain import event_size
    from aidd_chat.contracts import MessageDeltaEventV1

    plain = MessageDeltaEventV1(
        run_id=uuid4(), sequence=1, occurred_at=datetime.now(UTC), message_id=uuid4(), text="a" * 64
    )
    escaped = MessageDeltaEventV1(
        run_id=uuid4(), sequence=1, occurred_at=datetime.now(UTC), message_id=uuid4(), text='\\"' * 32
    )
    assert event_size(escaped) > event_size(plain)


def test_an_unauthorized_capability_mints_no_rate_limiter_key() -> None:
    """Rate keys are session identities. Minting one from an unvalidated cookie lets
    an attacker fill the table with keys nobody owns -- and every eviction that
    causes is paid for by a legitimate session."""
    application = _application()
    store = application.store
    conversation, capability = application.create_conversation()
    run = _submit(application, conversation, capability)
    application.wait_for_generations(5)
    before = store.capacity_snapshot()["rate_keys"]

    for index in range(20):
        with pytest.raises(ConversationNotFound):
            application.get_run(run.run_id, f"forged-{index}")
        with pytest.raises(ConversationNotFound):
            application.submit_question(uuid4(), f"forged-{index}", "k", QUESTION)
        with pytest.raises(ConversationNotFound):
            application.cancel_run(uuid4(), f"forged-{index}")
    assert store.capacity_snapshot()["rate_keys"] == before

    # An authorized session does register one, which is what makes the above a
    # property of authorization rather than of the rate limiter being switched off.
    application.get_run(run.run_id, capability)
    assert store.capacity_snapshot()["rate_keys"] > before


def test_a_duplicate_key_is_replayed_even_when_the_session_rate_is_spent() -> None:
    """The comment says a replay must always replay, and the capacity path honours
    it. The rate path has to as well -- a duplicate arriving during a burst is
    exactly the retry the Idempotency-Key exists for."""
    application = _application(submit_rate_per_minute=1, retry_rate_per_minute=1)
    conversation, capability = application.create_conversation()
    first = _submit(application, conversation, capability, key="dup")
    application.wait_for_generations(5)
    # The budget is spent: a genuinely new question is refused.
    with pytest.raises(CapacityExceeded):
        _submit(application, conversation, capability, key="fresh")
    # The duplicate is not.
    assert _submit(application, conversation, capability, key="dup").run_id == first.run_id


def test_a_command_refused_as_already_active_burns_no_rate_budget() -> None:
    """submit and retry have to agree about the order, or one of them charges for a
    command the other refuses for free."""
    provider = BlockingProvider()
    application = _application(provider, submit_rate_per_minute=1, retry_rate_per_minute=1)
    conversation, capability = application.create_conversation()
    _submit(application, conversation, capability, key="live")
    assert provider.started.wait(5)

    # A second question while one is running is refused by the Run contract, not by
    # a ceiling -- and must not consume the session's single remaining submit.
    with pytest.raises(ActiveRunConflict):
        _submit(application, conversation, capability, key="blocked")
    provider.release.set()
    application.wait_for_generations(5)
    assert application.store.capacity_snapshot()["rate_keys"] >= 0


def test_a_generation_thread_that_never_starts_gives_its_capacity_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each leaked lease permanently costs a queue slot, a Provider slot, a session
    slot and an uncharged spend reservation. With the shipped ceilings a handful of
    thread-start failures leaves the process refusing everyone for ever."""
    application = _application(
        max_global_active_runs=2, max_queued_runs=1, max_provider_concurrency=1
    )
    conversation, capability = application.create_conversation()

    original_submit = application.agent_loop.submit

    def refuse_to_start(coro):
        # The generation is scheduled on the agent loop now, not started as a thread.
        if getattr(getattr(coro, "cr_code", None), "co_name", None) == "_generate":
            coro.close()
            raise RuntimeError("can't schedule the generation")
        return original_submit(coro)

    monkeypatch.setattr(application.agent_loop, "submit", refuse_to_start)
    for index in range(4):
        application.submit_question(
            conversation.conversation_id, capability, f"thread-{index}", QUESTION
        )
    monkeypatch.undo()

    snapshot = application.store.capacity_snapshot()
    assert snapshot["queued_runs"] == 0 and snapshot["active_runs"] == 0
    assert snapshot["provider_calls"] == 0 and snapshot["spend_units"] == 0
    # And the process still serves: nothing was permanently consumed.
    run = _submit(application, conversation, capability, key="after")
    application.wait_for_generations(5)
    assert application.get_run(run.run_id, capability).state == "completed"


def test_a_start_run_failure_still_ends_the_run() -> None:
    """`start_run` sits between mark_running and the Provider call. A raise there
    used to leave the Run `running` for ever with no terminal and no stream.end."""

    class BrokenStartStore(InMemoryConversationStore):
        def start_run(self, lease) -> None:
            raise RuntimeError("counter is broken")

    application = ChatApplication(
        BrokenStartStore(), FakeAgent(), 3, DeploymentLimits()
    )
    conversation, capability = application.create_conversation()
    run = _submit(application, conversation, capability)
    application.wait_for_generations(5)

    projection, events = application.get_run_snapshot(run.run_id, capability)
    assert projection.state == "failed"
    assert events[-1].type == "stream.end"


def test_provider_concurrency_counts_calls_in_flight_not_reservations() -> None:
    """A queued lease is not a Provider call. Counting it as one made the ceiling
    identically equal to queued+active, so the setting did nothing of its own."""
    store = GatedStartStore()
    provider = BlockingProvider()
    application = ChatApplication(
        store, provider, 3,
        DeploymentLimits(
            max_provider_concurrency=1, max_global_active_runs=8, max_queued_runs=8,
            max_session_active_runs=8,
        ),
    )
    conversation, capability = application.create_conversation()
    other, other_capability = application.create_conversation()
    submitter = Thread(target=_submit, args=(application, conversation, capability), daemon=True)
    submitter.start()
    assert store.reached.wait(5)
    # Reserved but not started: no Provider call exists yet, so the ceiling is free.
    assert store.capacity_snapshot()["provider_calls"] == 0
    second = store.reserve_run(other.conversation_id)
    store.release_run(second)

    store.release.set()
    provider.release.set()
    application.wait_for_generations(5)
    assert store.capacity_snapshot()["provider_calls"] == 0


def test_a_conversation_creation_rate_stops_one_ip_pinning_every_slot() -> None:
    """A resident slot is held for the full hour, three orders of magnitude longer
    than the per-minute request window, so the general budget cannot bound it."""
    application = _application(create_rate_per_minute=2, max_requests_per_ip_per_minute=1_000)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            statuses = [
                client.post("/api/v1/conversations", headers=ORIGIN).status_code
                for _attempt in range(4)
            ]
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert statuses[:2] == [201, 201]
    assert statuses[2:] == [503, 503]
    assert len(application.store.states) == 2


def test_a_cross_origin_request_costs_the_caller_no_ip_budget() -> None:
    """A cheap 403 has to stay cheap: otherwise a third-party page can burn a real
    user's budget from their own browser."""
    application = _application(max_requests_per_ip_per_minute=2)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            junk = [
                client.post("/api/v1/conversations", headers={"Origin": "https://evil.example"})
                for _attempt in range(5)
            ]
            allowed = client.post("/api/v1/conversations", headers=ORIGIN)
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert {response.status_code for response in junk} == {403}
    assert allowed.status_code == 201


def test_load_shed_responses_carry_a_retry_after() -> None:
    """Every ceiling has a known window. Without the header a client guesses, and
    guesses either too early or not at all."""
    chat = _application()
    for reason, (_status, _message, retry_after) in main_module._CAPACITY_REJECTIONS.items():
        response = main_module.capacity_response(CapacityExceeded(reason), chat)
        if retry_after is None:
            assert "retry-after" not in {key.lower() for key in response.headers}
            continue
        assert response.headers["Retry-After"] == str(retry_after)
        assert int(response.headers["Retry-After"]) > 0
    # A refusal only a redeploy can clear advertises no wait at all.
    permanent = main_module.capacity_response(
        CapacityExceeded("rate_limited", retryable=False), chat
    )
    assert "retry-after" not in {key.lower() for key in permanent.headers}


def test_the_server_and_the_client_agree_on_the_load_shed_codes() -> None:
    """Two hand-maintained copies of one list drift. This is the assertion that
    stops them."""
    source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    block = source.split("const CAPACITY_ERROR_CODES = new Set([", 1)[1].split("]);", 1)[0]
    client_codes = set(re.findall(r'"([a-z_]+)"', block))
    assert client_codes == set(main_module._CAPACITY_REJECTIONS)


def test_the_sweeper_thread_is_started_once_and_stopped_by_shutdown() -> None:
    application = _application()
    assert application.store._sweeper is None
    application.create_conversation()
    sweeper = application.store._sweeper
    assert sweeper is not None and sweeper.is_alive()
    application.create_conversation()
    assert application.store._sweeper is sweeper

    application.shutdown()
    sweeper.join(5)
    assert not sweeper.is_alive()
    # And nothing restarts it: a Sweeper started after shutdown is a daemon thread
    # nothing will ever join, which is the hazard the stop flag exists to prevent.
    application.create_conversation()
    assert application.store._sweeper is None
