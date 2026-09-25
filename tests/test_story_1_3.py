import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import time
from pathlib import Path
from threading import Event, Lock, Thread, Timer
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import aidd_chat.application as application_module

from aidd_chat.adapters import (
    DETERMINISTIC_BINDING,
    ActiveRunConflict,
    IdempotencyConflict,
    InMemoryConversationStore,
)
from aidd_chat.application import AgentRunError, ChatApplication, _Generation
from aidd_chat.contracts import (
    PreparedMessageV1,
    PreparedModelRequestV1,
    ProviderFailureV1,
    QuestionCommand,
    RunProjectionV1,
    prepare_model_request,
)
from aidd_chat.domain import Message
from aidd_chat.main import _valid_idempotency_key, app, get_chat_application
from agent_fakes import GROUNDED_RESULT, LegacySyncAgent
from aidd_chat.adapters import FakeAgent


ROOT = Path(__file__).parents[1]
ORIGIN = {"Origin": "https://testserver"}


def _msg(role: str, text: str) -> PreparedMessageV1:
    return PreparedMessageV1(uuid4(), role, text)


def _request(*messages: PreparedMessageV1, provider: object = None) -> PreparedModelRequestV1:
    provider = provider or FakeAgent()
    return prepare_model_request(messages, provider_profile_digest=provider.provider_profile_digest)


class BlockingProvider(LegacySyncAgent):
    policy_metadata = FakeAgent().policy_metadata
    binding = DETERMINISTIC_BINDING

    def __init__(self, output: object = "완료 답변") -> None:
        self.output = output
        self.started = Event()
        self.release = Event()
        self.calls = 0

    def probe(self) -> bool:
        return True

    def complete(self, _prompt: str, handle=None) -> object:
        self.calls += 1
        self.started.set()
        self.release.wait(2)
        if isinstance(self.output, BaseException):
            raise self.output
        return self.output


@pytest.mark.release_suite
def test_concurrent_duplicate_accepts_one_user_message_and_one_run() -> None:
    provider = BlockingProvider()
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: application.submit_question(
                    conversation.conversation_id, capability, "same-key", "안녕하세요"
                ),
                range(8),
            )
        )

    state = store.states[conversation.conversation_id]
    assert len({result.run_id for result in results}) == 1
    assert len(state.runs) == 1
    assert len([message for message in state.messages if isinstance(message, Message)]) == 1
    assert len(state.receipts) == 1
    # `calls` is incremented on the generation thread, which submit_question does not
    # wait for -- assert it only once the winner has actually reached the Provider.
    assert provider.started.wait(5)
    assert provider.calls == 1
    provider.release.set()
    application.wait_for_generations()


def test_concurrent_replay_cannot_return_before_run_is_pollable() -> None:
    provider = BlockingProvider()
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()
    index_entered = Event()
    release_index = Event()
    replay_returned = Event()

    class IndexLock:
        def __init__(self) -> None:
            self.lock = Lock()

        def __enter__(self):
            self.lock.acquire()
            index_entered.set()
            release_index.wait(1)
            return self

        def __exit__(self, *_args):
            self.lock.release()

    store._index_lock = IndexLock()
    with ThreadPoolExecutor(max_workers=2) as pool:
        original = pool.submit(
            application.submit_question,
            conversation.conversation_id,
            capability,
            "visibility-key",
            "질문",
        )
        assert index_entered.wait(1)

        def replay():
            result = application.submit_question(
                conversation.conversation_id, capability, "visibility-key", "질문"
            )
            replay_returned.set()
            return result

        duplicate = pool.submit(replay)
        assert not replay_returned.wait(0.1)
        release_index.set()
        accepted = original.result(1)
        replayed = duplicate.result(1)

    assert replayed.run_id == accepted.run_id
    assert application.get_run(replayed.run_id, capability).run_id == accepted.run_id
    provider.release.set()
    application.wait_for_generations()


def test_idempotency_conflict_and_active_run_do_not_change_state() -> None:
    provider = BlockingProvider()
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()
    original = application.submit_question(conversation.conversation_id, capability, "key", "첫 질문")
    assert provider.started.wait(1)
    state = store.states[conversation.conversation_id]
    before = (len(state.messages), len(state.runs), len(state.receipts), state.active_run_id)

    with pytest.raises(IdempotencyConflict):
        application.submit_question(conversation.conversation_id, capability, "key", "다른 질문")
    with pytest.raises(ActiveRunConflict):
        application.submit_question(conversation.conversation_id, capability, "other", "두 번째 질문")

    assert (len(state.messages), len(state.runs), len(state.receipts), state.active_run_id) == before
    assert state.active_run_id == original.run_id
    provider.release.set()
    application.wait_for_generations()


def test_canonical_lf_digest_replays_original_run() -> None:
    provider = BlockingProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()
    first = application.submit_question(conversation.conversation_id, capability, "line-key", "가\r\n나")
    replay = application.submit_question(conversation.conversation_id, capability, "line-key", "가\n나")

    assert replay.run_id == first.run_id
    provider.release.set()
    application.wait_for_generations()
    composed = application.submit_question(conversation.conversation_id, capability, "nfc-key", "é")
    decomposed = application.submit_question(conversation.conversation_id, capability, "nfc-key", "e\u0301")
    assert decomposed.run_id == composed.run_id
    application.wait_for_generations()


def test_complete_cas_commits_one_assistant_and_full_equality_chain() -> None:
    provider = BlockingProvider("결정적 답변")
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(conversation.conversation_id, capability, "complete-key", "질문")
    provider.release.set()
    application.wait_for_generations()
    completed = application.get_run(accepted.run_id, capability)
    state = store.states[conversation.conversation_id]
    run = state.runs[accepted.run_id]
    assistant = [message for message in state.messages if isinstance(message, Message) and message.role == "assistant"]

    assert completed.state == "completed"
    assert len(assistant) == 1
    assert completed.output_message_id == completed.output_message.message_id == assistant[0].message_id
    assert completed.output_message_id == run.reserved_output_message_id
    assert completed.output_message.content == assistant[0].content == "결정적 답변"
    # One answer path now: a complete-only Provider's answer arrives as one delta,
    # and the sources event precedes the completion (AD-29).
    assert run.events[3].message_id == assistant[0].message_id
    assert run.events[3].text == assistant[0].content
    assert [event.type for event in run.events] == [
        "run.status",
        "message.delta",
        "message.sources",
        "message.completed",
        "run.status",
        "stream.end",
    ]
    assert store.commit_completed(conversation.conversation_id, accepted.run_id, "늦은 답변", GROUNDED_RESULT, completed.last_updated_at) is False
    assert len([message for message in state.messages if isinstance(message, Message) and message.role == "assistant"]) == 1


@pytest.mark.parametrize(
    ("output", "expected_kind", "retryable"),
    [
        ("", "provider_empty", False),
        ("   ", "provider_empty", False),
        # Story 1.9: a zero-width space is refused as hostile text before the
        # "nothing visible" rule is reached, so this is invalid, not empty.
        ("\u200b", "provider_invalid_response", False),
        ("\ud800", "provider_invalid_response", False),
        # Story 1.9 widened the Validator past lone surrogates. A complete-only
        # Provider's answer now arrives as one delta, so this text passes
        # `on_delta`/`commit_delta` before `commit_completed` -- and must fail the
        # Run as a typed Provider failure, not as a ValidationError escaping a
        # commit and landing on provider_unknown.
        ("답변\x00숨김", "provider_invalid_response", False),
        ("답변\u202e역순", "provider_invalid_response", False),
        ("답변\U000e0041태그", "provider_invalid_response", False),
        (object(), "provider_non_text", False),
        (AgentRunError("provider_incomplete"), "provider_incomplete", True),
        (asyncio.CancelledError(), "provider_cancelled", True),
    ],
)
def test_invalid_provider_output_fails_without_completed_message(
    output: object, expected_kind: str, retryable: bool
) -> None:
    provider = BlockingProvider(output)
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(conversation.conversation_id, capability, f"bad-{id(output)}", "질문")
    provider.release.set()
    application.wait_for_generations()
    failed = application.get_run(accepted.run_id, capability)

    assert failed.state == "failed"
    assert failed.output_message_id is None and failed.output_message is None
    assert failed.terminal_error is not None
    assert failed.terminal_error.kind == expected_kind
    assert failed.terminal_error.retryable is retryable
    assert failed.terminal_error.message
    run = store.states[conversation.conversation_id].runs[accepted.run_id]
    assert [event.type for event in run.events] == [
        "run.status",
        "message.discarded",
        "run.error",
        "run.status",
        "stream.end",
    ]
    assert run.events[2].error == failed.terminal_error
    assert run.events[-1].final_sequence == failed.latest_sequence == 5
    assert not any(isinstance(message, Message) and message.role == "assistant" for message in store.states[conversation.conversation_id].messages)


class StreamingBlockingProvider(BlockingProvider):
    """The same answers down the Delta path. Streaming and completed share one
    Validator, so the same text has to fail the Run the same typed way on both."""

    def stream(self, request, on_delta, handle=None) -> object:
        self.calls += 1
        self.started.set()
        self.release.wait(2)
        if isinstance(self.output, BaseException):
            raise self.output
        on_delta(self.output)
        return self.output


@pytest.mark.parametrize(
    "output",
    ["답변\x00숨김", "답변\u202e역순", "답변\U000e0041태그"],
)
def test_unsafe_model_text_fails_the_run_on_the_delta_path_too(output: str) -> None:
    provider = StreamingBlockingProvider(output)
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(
        conversation.conversation_id, capability, f"delta-{id(output)}", "질문"
    )
    provider.release.set()
    application.wait_for_generations()
    failed = application.get_run(accepted.run_id, capability)

    assert failed.state == "failed"
    assert failed.terminal_error.kind == "provider_invalid_response"
    assert failed.output_message_id is None and failed.output_message is None
    # Nothing hostile was committed on the way to the terminal.
    run = store.states[conversation.conversation_id].runs[accepted.run_id]
    assert not any(event.type == "message.delta" for event in run.events)


def test_expired_conversation_rejects_completed_and_failed_terminal_commits() -> None:
    provider = BlockingProvider()
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(conversation.conversation_id, capability, "expiry-key", "질문")
    assert provider.started.wait(1)
    state = store.states[conversation.conversation_id]
    state.expires_at = datetime.now(UTC) - timedelta(microseconds=1)
    # Story 1.8 enforces the Absolute Expiry on the monotonic clock, so a wall
    # step cannot suspend expiry or purge everything at once. Expire both.
    state.expires_monotonic = time.monotonic() - 1
    run = state.runs[accepted.run_id]
    before = (run.state, run.output_message_id, run.terminal_error, tuple(run.events), tuple(state.messages))
    failure = ProviderFailureV1(
        kind="provider_unknown",
        retryable=False,
        correlation_id=uuid4(),
        message="Provider 오류가 발생했어요. 새 대화를 시작해 주세요.",
    )

    assert store.commit_completed(conversation.conversation_id, accepted.run_id, "늦은 답변", GROUNDED_RESULT, datetime.now(UTC)) is False
    assert store.commit_failed(conversation.conversation_id, accepted.run_id, failure, datetime.now(UTC)) is False
    assert (run.state, run.output_message_id, run.terminal_error, tuple(run.events), tuple(state.messages)) == before

    provider.release.set()
    application.wait_for_generations()
    assert not any(isinstance(message, Message) and message.role == "assistant" for message in state.messages)


def test_domain_rejects_expired_start_and_invalid_completed_content() -> None:
    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)
    conversation, capability = application.create_conversation()
    capability_hash = sha256(capability.encode()).hexdigest()
    accepted = store.accept_question(
        conversation.conversation_id,
        capability_hash,
        "domain-key",
        "digest",
        "질문",
        datetime.now(UTC),
        correlation_id=uuid4(),
    ).run

    assert store.commit_completed(
        conversation.conversation_id,
        accepted.run_id,
        "\u200b",
        GROUNDED_RESULT,
        datetime.now(UTC),
    ) is False
    state = store.states[conversation.conversation_id]
    assert state.runs[accepted.run_id].state == "queued" and state.messages[-1].role == "user"

    state.expires_at = datetime.now(UTC) - timedelta(microseconds=1)
    # Story 1.8 enforces the Absolute Expiry on the monotonic clock, so a wall
    # step cannot suspend expiry or purge everything at once. Expire both.
    state.expires_monotonic = time.monotonic() - 1
    assert store.mark_running(conversation.conversation_id, accepted.run_id, datetime.now(UTC), time.monotonic()) is False
    assert state.runs[accepted.run_id].state == "queued"


def test_closed_contracts_reject_invisible_question_and_invalid_state_stage() -> None:
    with pytest.raises(ValueError):
        QuestionCommand.model_validate({"kind": "question", "content": "\u200b"})

    now = datetime.now(UTC)
    output_message_id = uuid4()
    with pytest.raises(ValueError):
        RunProjectionV1(
            run_id=uuid4(),
            conversation_id=uuid4(),
            input_message_id=uuid4(),
            state="completed",
            stage="queued",
            created_at=now,
            last_updated_at=now,
            output_message_id=output_message_id,
            output_message={"message_id": output_message_id, "content": "답변"},
        )


def test_cancelled_probe_clears_inflight_state() -> None:
    class CancelledProbeProvider(BlockingProvider):
        def probe(self) -> bool:
            raise asyncio.CancelledError

    application = ChatApplication(InMemoryConversationStore(), CancelledProbeProvider(), 3)

    assert application.is_ready() is False
    assert application._probe_inflight is None


def test_unhashable_provider_kind_maps_to_unknown_failure() -> None:
    class HostileFailure(Exception):
        kind = []

    provider = BlockingProvider(HostileFailure())
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(conversation.conversation_id, capability, "hostile-kind", "질문")
    provider.release.set()
    application.wait_for_generations()

    assert application.get_run(accepted.run_id, capability).terminal_error.kind == "provider_unknown"


def _is_generation(coro) -> bool:
    return getattr(getattr(coro, "cr_code", None), "co_name", None) == "_generate"


def test_generation_thread_registration_is_atomic(monkeypatch) -> None:
    provider = BlockingProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()
    original_submit = application.agent_loop.submit
    lock_states = []
    waiter_returned_early = []
    waited = Event()

    def waiter() -> None:
        application.wait_for_generations(5)
        waiter_returned_early.append(bool(application._generation_tasks))
        waited.set()

    def checked_submit(coro):
        # The generation is scheduled on the agent loop now, not started as a
        # thread -- inside `_generation_lock`, together with its registration, so
        # no waiter can see the entry without the Future it waits on. A waiter
        # started now must block on this generation, not skip it.
        if _is_generation(coro):
            lock_states.append(application._generation_lock.locked())
            Thread(target=waiter, daemon=True).start()
            waited.wait(0.3)
        return original_submit(coro)

    monkeypatch.setattr(application.agent_loop, "submit", checked_submit)
    application.submit_question(conversation.conversation_id, capability, "thread-key", "질문")
    provider.release.set()
    assert waited.wait(5)
    assert lock_states == [True]
    assert waiter_returned_early == [False]
    application.wait_for_generations()


def test_generation_thread_start_failure_returns_failed_projection(monkeypatch) -> None:
    application = ChatApplication(InMemoryConversationStore(), FakeAgent(), 3)
    conversation, capability = application.create_conversation()
    original_submit = application.agent_loop.submit

    def failing_submit(coro):
        if _is_generation(coro):
            coro.close()
            raise RuntimeError()
        return original_submit(coro)

    monkeypatch.setattr(application.agent_loop, "submit", failing_submit)

    projection = application.submit_question(conversation.conversation_id, capability, "start-fail", "질문")

    assert projection.state == "failed"
    assert projection.terminal_error.kind == "provider_unknown"
    assert application._generation_tasks == {}


def test_submit_poll_origin_capability_and_completed_output_contract() -> None:
    application = ChatApplication(InMemoryConversationStore(), FakeAgent(), 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            denied_origin = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers={"Origin": "https://other.test", "Idempotency-Key": "origin-key"},
                json={"kind": "question", "content": "질문"},
            )
            accepted = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers={"Origin": "https://testserver", "Idempotency-Key": "api-key"},
                json={"kind": "question", "content": "질문"},
            )
            application.wait_for_generations()
            polled = client.get(f"/api/v1/runs/{accepted.json()['run_id']}")
            client.post("/api/v1/conversations", headers=ORIGIN)
            unauthorized = client.get(f"/api/v1/runs/{accepted.json()['run_id']}")
            absent = client.get(f"/api/v1/runs/{uuid4()}")
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert denied_origin.status_code == 403
    assert accepted.status_code == 202
    assert accepted.headers["cache-control"] == "no-store"
    assert accepted.json()["state"] == "queued" and accepted.json()["output_message"] is None
    assert polled.status_code == 200 and polled.json()["state"] == "completed"
    assert polled.headers["cache-control"] == "no-store"
    assert polled.json()["output_message_id"] == polled.json()["output_message"]["message_id"]
    assert unauthorized.status_code == absent.status_code == 404
    assert unauthorized.content == absent.content == b""
    assert unauthorized.headers["cache-control"] == absent.headers["cache-control"] == "no-store"


def test_http_replay_bypasses_failed_readiness_but_new_command_does_not() -> None:
    class ToggleProvider(BlockingProvider):
        ready = True

        def probe(self) -> bool:
            return self.ready

    provider = ToggleProvider()
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            headers = {"Origin": "https://testserver", "Idempotency-Key": "replay-key"}
            first = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers=headers,
                json={"kind": "question", "content": "같은 질문"},
            )
            assert provider.started.wait(1)
            provider.ready = False
            with application._readiness_lock:
                application._clear_success_cache_locked()

            replay = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers=headers,
                json={"kind": "question", "content": "같은 질문"},
            )
            mismatch = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers=headers,
                json={"kind": "question", "content": "다른 질문"},
            )
            unavailable = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers={"Origin": "https://testserver", "Idempotency-Key": "new-key"},
                json={"kind": "question", "content": "새 질문"},
            )
            provider.release.set()
            application.wait_for_generations()
    finally:
        provider.release.set()
        application.wait_for_generations()
        app.dependency_overrides.pop(get_chat_application, None)

    state = store.states[UUID(conversation_id)]
    assert first.status_code == replay.status_code == 202
    assert first.json()["run_id"] == replay.json()["run_id"]
    assert replay.headers["cache-control"] == "no-store"
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "idempotency_conflict"
    assert unavailable.status_code == 409
    assert unavailable.json()["error"]["code"] == "run_already_active"
    assert unavailable.headers["cache-control"] == "no-store"
    assert len(state.runs) == len(state.receipts) == 1
    assert provider.calls == 1


def test_closed_422_surrogate_and_invalid_idempotency_leave_no_state() -> None:
    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            url = f"/api/v1/conversations/{conversation_id}/runs"
            origin = {"Origin": "https://testserver", "Idempotency-Key": "invalid-body"}
            closed = client.post(
                url,
                headers=origin,
                json={"kind": "question", "content": "질문", "extra": "금지"},
            )
            surrogate = client.post(
                url,
                headers={**origin, "Content-Type": "application/json"},
                content=b'{"kind":"question","content":"\\ud800"}',
            )
            invalid_key = client.post(
                url,
                headers={"Origin": "https://testserver"},
                json={"kind": "question", "content": "질문"},
            )
            malformed_keys = [
                client.post(
                    url,
                    headers={"Origin": "https://testserver", "Idempotency-Key": key},
                    json={"kind": "question", "content": "질문"},
                )
                for key in (" key ",)
            ]
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    for response in (closed, surrogate):
        assert response.status_code == 422
        assert response.headers["cache-control"] == "no-store"
        assert set(response.json()) == {"error"}
        assert set(response.json()["error"]) == {
            "code", "message", "retryable", "correlation_id", "field_errors"
        }
        assert response.json()["error"]["field_errors"] == {}
    assert invalid_key.status_code == 400
    assert invalid_key.headers["cache-control"] == "no-store"
    assert all(response.status_code == 400 for response in malformed_keys)
    assert _valid_idempotency_key("key\u200b") is False
    state = store.states[UUID(conversation_id)]
    assert state.messages == [] and state.runs == {} and state.receipts == {}


def test_create_origin_and_unauthorized_validation_boundary() -> None:
    application = ChatApplication(InMemoryConversationStore(), FakeAgent(), 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            missing_origin = client.post("/api/v1/conversations")
            wrong_origin = client.post(
                "/api/v1/conversations",
                headers={"Origin": "https://other.test"},
            )
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            client.cookies.clear()
            malformed = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers={"Origin": "https://testserver", "Idempotency-Key": "unauthorized"},
                json={"kind": "wrong", "extra": True},
            )
            malformed_id = client.get("/api/v1/runs/not-a-uuid")
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert missing_origin.status_code == wrong_origin.status_code == 403
    assert len(application.store.states) == 1
    assert malformed.status_code == malformed_id.status_code == 404
    assert malformed.content == malformed_id.content == b""


def test_expired_submit_and_poll_return_closed_410() -> None:
    application = ChatApplication(InMemoryConversationStore(), FakeAgent(), 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            conversation_id = created.json()["conversation_id"]
            accepted = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers={"Origin": "https://testserver", "Idempotency-Key": "expiry-http"},
                json={"kind": "question", "content": "질문"},
            )
            application.wait_for_generations()
            application.store.states[UUID(conversation_id)].expires_at = datetime.now(UTC) - timedelta(microseconds=1)
            # Story 1.8 enforces the Absolute Expiry on the monotonic clock, so a wall
            # step cannot suspend expiry or purge everything at once. Expire both.
            application.store.states[UUID(conversation_id)].expires_monotonic = time.monotonic() - 1
            expired_submit = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                headers={"Origin": "https://testserver", "Idempotency-Key": "after-expiry"},
                json={"kind": "question", "content": "다시"},
            )
            expired_poll = client.get(f"/api/v1/runs/{accepted.json()['run_id']}")

            # A second Conversation where the POLL is the first thing to notice:
            # both entry points have to be able to win that race, not just submit.
            second = client.post("/api/v1/conversations", headers=ORIGIN)
            second_run = client.post(
                f"/api/v1/conversations/{second.json()['conversation_id']}/runs",
                headers={"Origin": "https://testserver", "Idempotency-Key": "poll-first"},
                json={"kind": "question", "content": "질문"},
            )
            application.wait_for_generations()
            second_state = application.store.states[UUID(second.json()["conversation_id"])]
            second_state.expires_at = datetime.now(UTC) - timedelta(microseconds=1)
            second_state.expires_monotonic = time.monotonic() - 1
            expired_poll_first = client.get(f"/api/v1/runs/{second_run.json()['run_id']}")
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    # Story 1.8 made expiry Purge: the first valid Capability request to notice is
    # the one that empties the Conversation and the one that gets 410. Everything
    # after it -- this poll included -- is asking about a Resource that no longer
    # exists, and gets the same Minimal 404 an unknown Run always got.
    assert expired_poll_first.status_code == 410
    assert expired_poll_first.json()["error"]["code"] == "conversation_expired"
    assert expired_poll_first.headers["cache-control"] == "no-store"
    assert expired_submit.status_code == 410
    assert expired_submit.json()["error"]["code"] == "conversation_expired"
    assert expired_submit.headers["cache-control"] == "no-store"
    assert expired_poll.status_code == 404
    assert expired_poll.content == b""
    assert expired_poll.headers["cache-control"] == "no-store"


def test_lifespan_shutdown_joins_generation_threads() -> None:
    provider = BlockingProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    missing = object()
    original = getattr(app.state, "chat_application", missing)
    app.state.chat_application = application
    timer = Timer(0.05, provider.release.set)
    tasks = ()
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            submitted = client.post(
                f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                headers={"Origin": "https://testserver", "Idempotency-Key": "shutdown-key"},
                json={"kind": "question", "content": "종료 정리"},
            )
            assert submitted.status_code == 202
            assert provider.started.wait(1)
            with application._generation_lock:
                tasks = tuple(item.future for item in application._generation_tasks.values())
            timer.start()
    finally:
        provider.release.set()
        if timer.ident is not None:
            timer.join(1)
        if original is missing:
            del app.state.chat_application
        else:
            app.state.chat_application = original

    assert len(tasks) == 1
    assert tasks[0].done()
    assert application._generation_tasks == {}


def test_shutdown_waits_for_generations_within_the_close_grace_bound(monkeypatch) -> None:
    # Ruling (Task 4): shutdown waits at most close_grace + 2 s, then cancels what
    # is left -- an agent ignoring its abort must not hold the process open. The
    # story-era unbounded join (timeout None) is gone.
    application = ChatApplication(InMemoryConversationStore(), FakeAgent(), 3)
    seen = []
    real_wait = concurrent.futures.wait

    def recording_wait(futures, timeout=None, **kwargs):
        seen.append(timeout)
        with application._generation_lock:
            application._generation_tasks.clear()
        return real_wait(futures, 0, **kwargs)

    monkeypatch.setattr(application_module.concurrent.futures, "wait", recording_wait)
    application._generation_tasks[uuid4()] = _Generation(
        uuid4(), uuid4(), future=concurrent.futures.Future()
    )

    application.shutdown()

    assert len(seen) == 1
    assert seen[0] is not None and 0 < seen[0] <= FakeAgent().close_grace_ms / 1_000 + 2

