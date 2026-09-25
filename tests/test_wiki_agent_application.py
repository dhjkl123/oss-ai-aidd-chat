import asyncio
from uuid import uuid4

import pytest

from aidd_chat.adapters.fake_agent import FAKE_SOURCE, FakeAgent
from aidd_chat.application import AgentRunError
from aidd_chat.contracts import PreparedMessageV1, prepare_model_request


def prepared(agent, text="질문"):
    return prepare_model_request((PreparedMessageV1(uuid4(), "user", text),),
                                 provider_profile_digest=agent.provider_profile_digest)


def collect(agent, request):
    steps, deltas = [], []

    async def go():
        async with agent.run(uuid4(), uuid4(), request, steps.append, deltas.append) as result:
            return result

    return asyncio.run(go()), steps, deltas


def test_fake_agent_grounded_script():
    agent = FakeAgent()
    result, steps, deltas = collect(agent, prepared(agent))
    assert result.outcome == "grounded" and result.sources == (FAKE_SOURCE,)
    assert [step.kind for step in steps] == ["wiki_index", "wiki_read", "decide", "compose"]
    assert deltas == ["테스트 응답: 질문"]


@pytest.mark.parametrize("outcome", ["wiki_gap", "out_of_scope", "meta"])
def test_fake_agent_unsourced_outcomes_send_no_deltas(outcome):
    agent = FakeAgent(outcome=outcome)
    result, steps, deltas = collect(agent, prepared(agent))
    assert result.outcome == outcome and result.sources == () and deltas == []
    assert "compose" not in [step.kind for step in steps]


def test_fake_agent_abort_ends_with_incomplete():
    agent = FakeAgent()
    request = prepared(agent)
    run_id = uuid4()

    async def go():
        def first_step(step):
            asyncio.get_running_loop().create_task(agent.abort(run_id))

        async with agent.run(run_id, uuid4(), request, first_step, lambda _t: None):
            pass

    with pytest.raises(AgentRunError) as caught:
        asyncio.run(go())
    assert caught.value.kind == "provider_incomplete"


def test_fake_agent_probe_is_ready():
    assert asyncio.run(FakeAgent().probe()).ready


# --- Task 4: async generation core -------------------------------------------------

import time
from contextlib import asynccontextmanager

from aidd_chat.adapters import InMemoryConversationStore
from aidd_chat.application import ChatApplication
from aidd_chat.contracts import AgentProbeV1, AgentResultV1


def app_with(agent):
    return ChatApplication(InMemoryConversationStore(), agent, 3)


def ask(chat, text="질문"):
    conversation, capability = chat.create_conversation()
    run = chat.submit_question(conversation.conversation_id, capability, str(uuid4()), text)
    return capability, run


def wait_terminal(chat, capability, run_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        projection = chat.get_run(run_id, capability, rate_limited=False)
        if projection.state not in ("queued", "running"):
            return projection
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def event_types(chat, capability, run_id):
    return [event.type for event in chat.get_run_snapshot(run_id, capability)[1]]


def test_grounded_run_streams_steps_then_sources():
    chat = app_with(FakeAgent())
    try:
        capability, run = ask(chat)
        done = wait_terminal(chat, capability, run.run_id)
        assert done.state == "completed"
        assert done.output_message.outcome == "grounded"
        assert event_types(chat, capability, run.run_id) == [
            "run.status", "agent.step", "agent.step", "agent.step", "agent.step",
            "message.delta", "message.sources", "message.completed", "run.status", "stream.end"]
    finally:
        chat.shutdown()


@pytest.mark.parametrize("outcome", ["wiki_gap", "out_of_scope", "meta"])
def test_unsourced_outcome_publishes_fixed_reply(outcome):
    from aidd_chat.contracts import FIXED_REPLIES
    chat = app_with(FakeAgent(outcome=outcome))
    try:
        capability, run = ask(chat)
        done = wait_terminal(chat, capability, run.run_id)
        assert done.output_message.content == FIXED_REPLIES[outcome]
        assert done.output_message.sources == ()
    finally:
        chat.shutdown()


def test_model_text_on_unsourced_outcome_is_invalid():
    class Chatty(FakeAgent):
        """Streams model text, then reports an outcome that must never carry any.
        FakeAgent only streams for sourced outcomes, so the delta is sent here."""

        def result_for(self, prepared):
            return AgentResultV1(outcome="wiki_gap", uncovered=None, sources=(),
                                 search_truncated=False, finish="stop")

        @asynccontextmanager
        async def run(self, run_id, correlation_id, prepared, on_step, on_delta):
            on_delta("모델 텍스트")
            async with super().run(run_id, correlation_id, prepared, on_step, on_delta) as result:
                yield result

    chat = app_with(Chatty(steps=()))
    try:
        capability, run = ask(chat)
        done = wait_terminal(chat, capability, run.run_id)
        assert done.state == "failed" and done.terminal_error.kind == "provider_invalid_response"
    finally:
        chat.shutdown()


def test_agent_run_error_kind_becomes_terminal_error():
    class Broken(FakeAgent):
        def chunks_for(self, prepared):
            raise AgentRunError("wiki_unavailable")

    chat = app_with(Broken())
    try:
        capability, run = ask(chat)
        done = wait_terminal(chat, capability, run.run_id)
        assert done.state == "failed" and done.terminal_error.kind == "wiki_unavailable"
        assert done.terminal_error.retryable is False
        assert "message.sources" not in event_types(chat, capability, run.run_id)
    finally:
        chat.shutdown()


class BlockingAgent(FakeAgent):
    """Emits one research step, then waits until aborted -- a Run stuck in research."""

    def __init__(self):
        super().__init__()
        self.aborted = []

    @asynccontextmanager
    async def run(self, run_id, correlation_id, prepared, on_step, on_delta):
        on_step(self.steps_for(prepared)[0])
        gate = self._aborts.setdefault(run_id, asyncio.Event())
        await gate.wait()
        raise AgentRunError("provider_incomplete")
        yield  # pragma: no cover -- makes this an async generator

    async def abort(self, run_id):
        self.aborted.append(run_id)
        await super().abort(run_id)


def test_cancel_during_research_emits_no_sources():
    agent = BlockingAgent()
    chat = app_with(agent)
    try:
        capability, run = ask(chat)
        deadline = time.monotonic() + 5
        while "agent.step" not in event_types(chat, capability, run.run_id):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        result = chat.cancel_run(run.run_id, capability)
        assert result.cancel_outcome == "accepted"
        done = wait_terminal(chat, capability, run.run_id)
        assert done.state == "cancelled"
        assert "message.sources" not in event_types(chat, capability, run.run_id)
        chat.wait_for_generations(2)
        assert agent.aborted == [run.run_id]
    finally:
        chat.shutdown()


def test_deadline_times_out_a_stuck_agent(monkeypatch):
    import aidd_chat.application as application
    monkeypatch.setattr(application, "RUN_DEADLINE", application.timedelta(seconds=1.0))
    chat = app_with(BlockingAgent())
    try:
        capability, run = ask(chat)
        done = wait_terminal(chat, capability, run.run_id, timeout=5)
        assert done.state == "timeout" and done.terminal_error.kind == "provider_timeout"
    finally:
        chat.shutdown()


def test_readiness_uses_async_probe():
    class Down(FakeAgent):
        async def probe(self):
            return AgentProbeV1(provider_ok=True, wiki_ok=False, context_ok=True)

    chat = app_with(FakeAgent())
    down = app_with(Down())
    try:
        assert chat.is_ready()
        assert not down.is_ready()
    finally:
        chat.shutdown()
        down.shutdown()


# --- Task 4 fix round 1: AD-6 order, one abort, stuck tasks end ----------------------

import threading
from datetime import UTC, datetime
from hashlib import sha256

from aidd_chat.domain import DeploymentLimits


class StuckAgent(FakeAgent):
    """One research step, then waits on a gate. With `honour_abort=False` nothing
    ever opens it -- an agent that ignores its abort. `abort` records the Run's
    state at the moment it arrives, to prove the terminal was committed first."""

    def __init__(self, honour_abort=True):
        super().__init__()
        self.honour_abort = honour_abort
        self.aborted = []
        self.state_at_abort = []
        self.abort_seen = threading.Event()
        self.stepped = threading.Event()
        self.chat = None
        self.capability = None

    @asynccontextmanager
    async def run(self, run_id, correlation_id, prepared, on_step, on_delta):
        on_step(self.steps_for(prepared)[0])
        self.stepped.set()
        gate = asyncio.Event()
        if self.honour_abort:
            self._aborts[run_id] = gate
        await gate.wait()
        raise AgentRunError("provider_incomplete")
        yield  # pragma: no cover -- makes this an async generator

    async def abort(self, run_id):
        if self.chat is not None:
            self.state_at_abort.append(
                self.chat.store.get_run(run_id, sha256(self.capability.encode()).hexdigest(),
                                        datetime.now(UTC)).state)
        self.aborted.append(run_id)
        await super().abort(run_id)
        self.abort_seen.set()


def short_deadline(monkeypatch, seconds=1.0):
    import aidd_chat.application as application
    monkeypatch.setattr(application, "RUN_DEADLINE", application.timedelta(seconds=seconds))


def wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def test_deadline_alarm_fences_then_aborts_once_with_no_polling(monkeypatch):
    short_deadline(monkeypatch)
    agent = StuckAgent()
    chat = app_with(agent)
    try:
        started = time.monotonic()
        capability, run = ask(chat)
        agent.chat, agent.capability = chat, capability
        # No poll: only the loop's own alarm can end this Run.
        assert agent.abort_seen.wait(3)
        assert time.monotonic() - started >= 1.0
        assert agent.state_at_abort == ["timeout"]
        chat.wait_for_generations(2)
        assert chat._generation_tasks == {}
        done = chat.get_run(run.run_id, capability, rate_limited=False)
        assert done.state == "timeout" and done.terminal_error.kind == "provider_timeout"
        assert agent.aborted == [run.run_id]
    finally:
        chat.shutdown()


def test_agent_ignoring_abort_is_cut_off_at_close_grace(monkeypatch):
    short_deadline(monkeypatch)
    agent = StuckAgent(honour_abort=False)
    chat = ChatApplication(InMemoryConversationStore(), agent, 3,
                           limits=DeploymentLimits(max_global_active_runs=1))
    try:
        capability, run = ask(chat)
        assert agent.abort_seen.wait(3)
        grace = agent.close_grace_ms / 1000
        assert wait_until(lambda: not chat._generation_tasks, grace + 1.0)
        done = chat.get_run(run.run_id, capability, rate_limited=False)
        assert done.state == "timeout" and done.terminal_error.kind == "provider_timeout"
        assert agent.aborted == [run.run_id]
        # The Run slot came back: with a global ceiling of one, this is accepted.
        _, second = ask(chat)
        assert second.state in ("queued", "running")
    finally:
        started = time.monotonic()
        chat.shutdown()
        assert time.monotonic() - started < 3


def test_length_finish_is_incomplete():
    class Truncated(FakeAgent):
        def result_for(self, prepared):
            return super().result_for(prepared).model_copy(update={"finish": "length"})

    chat = app_with(Truncated())
    try:
        capability, run = ask(chat)
        done = wait_terminal(chat, capability, run.run_id)
        assert done.state == "failed" and done.terminal_error.kind == "provider_incomplete"
    finally:
        chat.shutdown()


def test_cancel_commits_before_the_single_abort():
    agent = StuckAgent()
    chat = app_with(agent)
    try:
        capability, run = ask(chat)
        agent.chat, agent.capability = chat, capability
        assert agent.stepped.wait(3)
        assert chat.cancel_run(run.run_id, capability).cancel_outcome == "accepted"
        # A second stop request for the same Run only updates flags.
        chat._request_provider_cancel(run.run_id)
        assert agent.abort_seen.wait(3)
        chat.wait_for_generations(2)
        assert agent.state_at_abort == ["cancelled"]
        assert agent.aborted == [run.run_id]
    finally:
        chat.shutdown()


# --- Task 5: story-suite shim -------------------------------------------------------


def test_legacy_sync_stream_shape_still_drives_a_run():
    from agent_fakes import LegacySyncAgent

    class Streaming(LegacySyncAgent):
        def stream(self, request, on_delta, handle=None):
            on_delta("가")
            on_delta("나")
            return "가나"

    chat = app_with(Streaming())
    capability, run = ask(chat)
    done = wait_terminal(chat, capability, run.run_id)
    assert done.state == "completed" and done.output_message.content == "가나"
    chat.shutdown()


def test_legacy_mismatched_return_is_invalid_response():
    from agent_fakes import LegacySyncAgent

    class Lying(LegacySyncAgent):
        def stream(self, request, on_delta, handle=None):
            on_delta("가")
            return "다른 답"

    chat = app_with(Lying())
    capability, run = ask(chat)
    done = wait_terminal(chat, capability, run.run_id)
    assert done.terminal_error.kind == "provider_invalid_response"
    chat.shutdown()


def test_legacy_delta_refusal_after_cancel_propagates_from_the_worker_thread():
    """A Domain refusal raised inside on_delta (a delta arriving after the Run was
    cancelled) crosses the run_coroutine_threadsafe hop back into the fake's
    stream() call, exactly as it would reach a real synchronous Provider."""
    from agent_fakes import LegacySyncAgent

    class SlowStreaming(LegacySyncAgent):
        def stream(self, request, on_delta, handle=None):
            on_delta("가")
            while not handle.cancelled:
                time.sleep(0.01)
            on_delta("나")  # refused: the Run is no longer accepting deltas
            return "가나"

    chat = app_with(SlowStreaming())
    try:
        capability, run = ask(chat)
        deadline = time.monotonic() + 5
        while "message.delta" not in event_types(chat, capability, run.run_id):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        result = chat.cancel_run(run.run_id, capability)
        assert result.cancel_outcome == "accepted"
        done = wait_terminal(chat, capability, run.run_id)
        assert done.state == "cancelled"
        assert "message.sources" not in event_types(chat, capability, run.run_id)
    finally:
        chat.shutdown()


def test_legacy_bare_complete_drives_a_run_to_completed():
    """A bare LegacySyncAgent (no stream override, default complete()) must not
    deadlock the agent loop: complete()'s result is relayed from the loop thread
    directly, never through relay()'s run_coroutine_threadsafe(...).result() hop."""
    from agent_fakes import LegacySyncAgent

    chat = app_with(LegacySyncAgent())
    try:
        capability, run = ask(chat)
        done = wait_terminal(chat, capability, run.run_id)
        assert done.state == "completed" and done.output_message.content == "테스트 응답: 질문"
    finally:
        chat.shutdown()


def test_whitespace_only_first_delta_still_completes():
    class LeadingWhitespace(FakeAgent):
        def chunks_for(self, prepared):
            return ("\n", "알파는 개념이에요.")

    chat = app_with(LeadingWhitespace())
    try:
        capability, run = ask(chat)
        done = wait_terminal(chat, capability, run.run_id)
        assert done.state == "completed"
        assert done.output_message.content == "\n알파는 개념이에요."
    finally:
        chat.shutdown()


def test_legacy_bare_complete_blank_output_is_provider_empty():
    from agent_fakes import LegacySyncAgent

    class Blank(LegacySyncAgent):
        def complete(self, request, handle=None):
            return "   "

    chat = app_with(Blank())
    try:
        capability, run = ask(chat)
        done = wait_terminal(chat, capability, run.run_id)
        assert done.state == "failed" and done.terminal_error.kind == "provider_empty"
    finally:
        chat.shutdown()


# --- Task 6b: a generation is awaitable from the moment it is registered -----------

import concurrent.futures
import dataclasses
import warnings

import aidd_chat.application as application_module


def hold_submit(monkeypatch, chat, during):
    """Run `during(original_submit, coro)` in place of the generation's
    `agent_loop.submit`: the window between its registration and its Future."""
    original = chat.agent_loop.submit

    def submit(coro):
        if getattr(getattr(coro, "cr_code", None), "co_name", None) == "_generate":
            return during(original, coro)
        return original(coro)

    monkeypatch.setattr(chat.agent_loop, "submit", submit)


def test_generation_finishing_before_submit_returns_is_awaited_and_cleaned(monkeypatch):
    chat = app_with(FakeAgent())

    def finish_first(original, coro):
        future = original(coro)
        # Every chance for the Task to run to its end before submit returns.
        concurrent.futures.wait([future], 0.5)
        return future

    hold_submit(monkeypatch, chat, finish_first)
    try:
        capability, run = ask(chat)
        chat.wait_for_generations(2)
        assert chat._generation_tasks == {}
        assert chat.get_run(run.run_id, capability, rate_limited=False).state == "completed"
    finally:
        chat.shutdown()


def test_await_previous_generation_waits_for_a_cancelled_one_without_a_future_yet(monkeypatch):
    chat = app_with(FakeAgent())
    make = application_module._Generation
    # Cancelled from birth: the waiter must wait for exactly this generation.
    monkeypatch.setattr(
        application_module, "_Generation",
        lambda *args: dataclasses.replace(make(*args), cancelled=True),
    )
    conversation, capability = chat.create_conversation()
    still_registered = []
    waited = threading.Event()

    def waiter():
        chat._await_previous_generation(conversation.conversation_id, timeout=5)
        still_registered.append(bool(chat._generation_tasks))
        waited.set()

    def race(original, coro):
        threading.Thread(target=waiter, daemon=True).start()
        # The generation is registered and has no Future yet: a waiter that looks
        # now must not conclude there is nothing to wait for.
        waited.wait(0.3)
        return original(coro)

    hold_submit(monkeypatch, chat, race)
    try:
        chat.submit_question(conversation.conversation_id, capability, "k", "질문")
        assert waited.wait(5)
        assert still_registered == [False]
    finally:
        chat.shutdown()


def test_shutdown_twice_and_on_a_closed_loop_leaves_no_unawaited_coroutine():
    chat = app_with(FakeAgent())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        chat.shutdown()
        chat.shutdown()
    assert [str(w.message) for w in caught] == []


# --- Task 16 fix round 1: readiness refusals are distinguishable -------------------


def _last_readiness_error_class(chat):
    return chat.telemetry_snapshot()[-1].error_class


def test_readiness_provider_probe_failed_names_the_provider():
    class ProviderDown(FakeAgent):
        async def probe(self):
            return AgentProbeV1(provider_ok=False, wiki_ok=True, context_ok=True)

    chat = app_with(ProviderDown())
    try:
        assert not chat.is_ready()
        assert _last_readiness_error_class(chat) == "provider_probe_failed"
    finally:
        chat.shutdown()


def test_readiness_wiki_probe_failed_names_the_wiki():
    class WikiDown(FakeAgent):
        async def probe(self):
            return AgentProbeV1(provider_ok=True, wiki_ok=False, context_ok=True)

    chat = app_with(WikiDown())
    try:
        assert not chat.is_ready()
        assert _last_readiness_error_class(chat) == "wiki_probe_failed"
    finally:
        chat.shutdown()


def test_readiness_context_probe_failed_names_the_context():
    class ContextDown(FakeAgent):
        async def probe(self):
            return AgentProbeV1(provider_ok=True, wiki_ok=True, context_ok=False)

    chat = app_with(ContextDown())
    try:
        assert not chat.is_ready()
        assert _last_readiness_error_class(chat) == "context_probe_failed"
    finally:
        chat.shutdown()


def test_readiness_probe_timeout_is_distinct_from_a_failed_probe():
    """A Probe already in flight, past its own deadline and never resolved -- the
    2 s Readiness Probe Budget running out on a Probe that has not answered yet,
    which is a different fact from one that answered and named a specific
    failure. Set up directly: a live "budget expires while genuinely in
    flight" race is not reproducible deterministically."""
    chat = app_with(FakeAgent())
    try:
        chat._probe_inflight = application_module._ProbeFlight(
            chat._provider_binding, chat._bootstrap_binding,
            application_module._provider_profile_digest(chat._provider_binding),
            time.monotonic() - 1, threading.Event(),
        )
        assert not chat.is_ready()
        assert _last_readiness_error_class(chat) == "probe_timeout"
    finally:
        chat.shutdown()


def test_readiness_digest_mismatch_is_distinct_from_a_failed_probe():
    """A Probe that answered Ready, but for a Provider Profile the binding has
    since moved past -- `_run_probe`'s own re-check. Exercised directly for the
    same reason as the Probe Timeout case above: a live rebind mid-flight is a
    race, not something a test can force to land."""
    chat = app_with(FakeAgent())
    try:
        flight = application_module._ProbeFlight(
            chat._provider_binding, chat._bootstrap_binding,
            "a-digest-this-binding-never-had", time.monotonic() + 10, threading.Event(),
        )
        chat._probe_inflight = flight
        future: concurrent.futures.Future = concurrent.futures.Future()
        future.set_result(AgentProbeV1(provider_ok=True, wiki_ok=True, context_ok=True))
        chat._run_probe(flight, future)
        assert flight.result is False and flight.reason == "digest_mismatch"
        # `_run_probe` already released `_probe_inflight` (it resolved this Flight);
        # re-attach it so the public path reads the same resolved Flight rather than
        # starting a fresh Probe of its own.
        chat._probe_inflight = flight
        assert not chat.is_ready()
        assert _last_readiness_error_class(chat) == "digest_mismatch"
    finally:
        chat.shutdown()


def test_fake_agent_keeps_nothing_of_a_finished_run():
    # FakeAgent is the runtime the app binds for local_test: a Run's prepared request
    # (the owner's preview history) must not outlive the Run and its Session TTL.
    agent = FakeAgent()
    request = prepare_model_request((PreparedMessageV1(uuid4(), "user", "질문"),),
                                    provider_profile_digest=agent.provider_profile_digest)

    async def go():
        async with agent.run(uuid4(), uuid4(), request, lambda step: None, lambda text: None):
            pass
    asyncio.run(go())
    assert not hasattr(agent, "requests") and not hasattr(agent, "_handles")
    assert agent._aborts == {}
