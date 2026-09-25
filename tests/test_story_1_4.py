from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import socket
from threading import Barrier, Event, Thread
import time
from uuid import UUID, uuid1, uuid4

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from aidd_chat.adapters import (
    DETERMINISTIC_BINDING,
    InMemoryConversationStore,
)
from aidd_chat.application import ChatApplication
from aidd_chat.contracts import (
    MessageCompletedEventV1,
    PreparedMessageV1,
    PreparedModelRequestV1,
    ProviderFailureV1,
    RunStatusEventV1,
    prepare_model_request,
)
import aidd_chat.main as main_module
from aidd_chat.main import app, get_chat_application
from agent_fakes import GROUNDED_RESULT, LegacySyncAgent
from aidd_chat.adapters import FakeAgent


ORIGIN = {"Origin": "https://testserver"}


def _msg(role: str, text: str) -> PreparedMessageV1:
    return PreparedMessageV1(uuid4(), role, text)


def _request(*messages: PreparedMessageV1, provider: object = None) -> PreparedModelRequestV1:
    provider = provider or FakeAgent()
    return prepare_model_request(messages, provider_profile_digest=provider.provider_profile_digest)


class StreamingProvider(LegacySyncAgent):
    policy_metadata = FakeAgent().policy_metadata
    binding = DETERMINISTIC_BINDING

    def __init__(self) -> None:
        self.first = Event()
        self.release = Event()

    def probe(self) -> bool:
        return True

    def stream(self, _request, on_delta, handle=None) -> str:
        on_delta("첫 ")
        self.first.set()
        self.release.wait(3)
        on_delta("둘")
        return "첫 둘"


def test_closed_event_contracts_reject_non_v4_ids_and_non_utc_timestamps() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        RunStatusEventV1(run_id=uuid1(), sequence=1, occurred_at=now, state="running", stage="streaming")
    with pytest.raises(ValueError):
        RunStatusEventV1(
            run_id=uuid4(), sequence=1, occurred_at=datetime.now(), state="running", stage="streaming"
        )
    with pytest.raises(ValueError):
        MessageCompletedEventV1(
            run_id=uuid4(), sequence=1, occurred_at=now, message_id=uuid1(), text="답변"
        )


def test_closed_event_contracts_reject_invalid_pairs_text_and_failure() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        RunStatusEventV1(run_id=uuid4(), sequence=1, occurred_at=now, state="completed", stage="streaming")
    with pytest.raises(ValueError):
        MessageCompletedEventV1(
            run_id=uuid4(), sequence=1, occurred_at=now, message_id=uuid4(), text=" \u200b "
        )
    with pytest.raises(ValueError):
        ProviderFailureV1(
            kind="provider_unknown", retryable=False, correlation_id=uuid4(), message="\ud800"
        )
    with pytest.raises(ValueError):
        ProviderFailureV1(
            kind="provider_future_kind", retryable=False, correlation_id=uuid4(), message="실패"
        )


def test_streamed_run_uses_one_contiguous_log_and_projection() -> None:
    provider = StreamingProvider()
    store = InMemoryConversationStore()
    application = ChatApplication(store, provider, 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(conversation.conversation_id, capability, "stream", "질문")
    assert provider.first.wait(1)

    active, active_events = application.get_run_snapshot(accepted.run_id, capability)
    assert active.state == "running"
    assert [event.sequence for event in active_events] == [1, 2]
    assert [event.type for event in active_events] == ["run.status", "message.delta"]
    assert active_events[1].text == "첫 "

    provider.release.set()
    application.wait_for_generations()
    final, events = application.get_run_snapshot(accepted.run_id, capability)
    assert final.state == "completed"
    assert final.output_message.content == "첫 둘"
    assert final.latest_sequence == len(events) == 7
    assert [event.sequence for event in events] == list(range(1, 8))
    assert [event.type for event in events] == [
        "run.status", "message.delta", "message.delta", "message.sources", "message.completed",
        "run.status", "stream.end",
    ]
    assert events[-1].final_sequence == events[-1].sequence == final.latest_sequence


def test_stream_mismatch_and_zero_delta_commit_typed_failure() -> None:
    class BadProvider(StreamingProvider):
        def __init__(self, delta: str | None) -> None:
            self.delta = delta

        def stream(self, _request, on_delta, handle=None) -> str:
            if self.delta is not None:
                on_delta(self.delta)
            return "다른 final"

    for delta in ("부분", None):
        application = ChatApplication(InMemoryConversationStore(), BadProvider(delta), 3)
        conversation, capability = application.create_conversation()
        accepted = application.submit_question(conversation.conversation_id, capability, f"bad-{delta}", "질문")
        application.wait_for_generations()
        failed, events = application.get_run_snapshot(accepted.run_id, capability)
        assert failed.state == "failed"
        assert failed.terminal_error.kind == "provider_invalid_response"
        assert failed.output_message is None
        assert events[-1].type == "stream.end"


def test_delta_terminal_race_has_one_fence_and_rejects_late_mutation() -> None:
    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)
    conversation, capability = application.create_conversation()
    capability_hash = __import__("hashlib").sha256(capability.encode()).hexdigest()
    accepted = store.accept_question(
        conversation.conversation_id, capability_hash, "race", "digest", "질문",
        datetime.now(UTC), correlation_id=uuid4(),
    ).run
    assert store.mark_running(conversation.conversation_id, accepted.run_id, datetime.now(UTC), time.monotonic())
    barrier = Barrier(2)
    failure = ProviderFailureV1(
        kind="provider_invalid_response", retryable=False, correlation_id=uuid4(), message="응답이 올바르지 않습니다."
    )

    def delta():
        barrier.wait()
        store.commit_delta(conversation.conversation_id, accepted.run_id, "답", datetime.now(UTC))

    def terminal():
        barrier.wait()
        store.commit_stream_completed(
            conversation.conversation_id, accepted.run_id, "답", GROUNDED_RESULT, failure, datetime.now(UTC)
        )

    threads = [Thread(target=delta), Thread(target=terminal)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    projection, events = application.get_run_snapshot(accepted.run_id, capability)
    assert projection.state in {"completed", "failed"}
    assert sum(event.type == "stream.end" for event in events) == 1
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    before = tuple(events)
    assert store.commit_delta(conversation.conversation_id, accepted.run_id, "늦음", datetime.now(UTC)) is False
    assert store.commit_completed(conversation.conversation_id, accepted.run_id, "늦은 완료", GROUNDED_RESULT, datetime.now(UTC)) is False
    assert store.commit_failed(conversation.conversation_id, accepted.run_id, failure, datetime.now(UTC)) is False
    assert application.get_run_snapshot(accepted.run_id, capability)[1] == before


@contextmanager
def tcp_server(application: ChatApplication):
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


@pytest.mark.release_suite
def test_real_tcp_sse_emits_delta_before_terminal_and_replays_after_cursor() -> None:
    provider = StreamingProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    with tcp_server(application) as base_url, httpx.Client(base_url=base_url, timeout=3) as client:
        created = client.post("/api/v1/conversations", headers={"Origin": base_url})
        capability = created.headers["set-cookie"].split("conversation_capability=", 1)[1].split(";", 1)[0]
        conversation_id = created.json()["conversation_id"]
        accepted = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={"Origin": base_url, "Idempotency-Key": "tcp", "Cookie": f"conversation_capability={capability}"},
            json={"kind": "question", "content": "질문"},
        )
        run_id = accepted.json()["run_id"]
        assert provider.first.wait(1)
        assert not provider.release.is_set()
        headers = {"Cookie": f"conversation_capability={capability}"}
        with client.stream("GET", f"/api/v1/runs/{run_id}/events", headers=headers) as response:
            lines = []
            for line in response.iter_lines():
                lines.append(line)
                if "message.delta" in line:
                    break
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-accel-buffering"] == "no"
            assert any(line == "id: 2" for line in lines)
            assert not any("stream.end" in line for line in lines)
        provider.release.set()
        application.wait_for_generations()
        with client.stream(
            "GET", f"/api/v1/runs/{run_id}/events", headers={**headers, "Last-Event-ID": "2"}
        ) as replay:
            body = "\n".join(replay.iter_lines())
        assert "id: 3" in body and "event: stream.end" in body
        assert "id: 1" not in body and "id: 2" not in body


def test_active_ahead_cursor_exits_when_terminal_stays_below_cursor() -> None:
    provider = StreamingProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(conversation.conversation_id, capability, "ahead", "질문")
    assert provider.first.wait(1)
    with tcp_server(application) as base_url, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            httpx.get,
            f"{base_url}/api/v1/runs/{accepted.run_id}/events",
            headers={"Cookie": f"conversation_capability={capability}", "Last-Event-ID": "999"},
            timeout=3,
        )
        # Released only once the stream is open on a still-running Run: a request that
        # arrives after the Run ended is refused as an ahead cursor instead (400).
        deadline = time.monotonic() + 2
        while application.store._sse_connections == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert application.store._sse_connections == 1
        assert not future.done()
        provider.release.set()
        response = future.result(3)
    application.wait_for_generations()
    assert response.status_code == 200
    assert response.content == b""


@pytest.mark.release_suite
def test_cursor_capability_no_store_and_csp_boundaries() -> None:
    application = ChatApplication(InMemoryConversationStore(), FakeAgent(), 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            accepted = client.post(
                f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                headers={**ORIGIN, "Idempotency-Key": "cursor"},
                json={"kind": "question", "content": "질문"},
            )
            application.wait_for_generations()
            run_id = accepted.json()["run_id"]
            final = application.get_run(UUID(run_id), client.cookies["conversation_capability"]).latest_sequence
            malformed = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "-1"})
            too_long = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "1" * 21})
            ahead = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": str(final + 1)})
            exhausted = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": str(final)})
            client.cookies.clear()
            hidden = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "bad"})
            shell = client.get("/")
    finally:
        app.dependency_overrides.pop(get_chat_application, None)
    for response in (malformed, too_long, ahead):
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_event_cursor"
        assert response.headers["cache-control"] == "no-store"
    assert exhausted.status_code == 204 and exhausted.headers["cache-control"] == "no-store"
    assert hidden.status_code == 404 and hidden.content == b""
    assert shell.headers["content-security-policy"] == (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    )
    for response in (shell, exhausted, hidden):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "content-security-policy" in response.headers


class RawStreamingProvider(LegacySyncAgent):
    """Emits deltas that CRLF+NFC normalization would rewrite."""

    policy_metadata = FakeAgent().policy_metadata

    binding = DETERMINISTIC_BINDING

    def __init__(self, chunks: tuple[str, ...]) -> None:
        self.chunks = chunks

    def probe(self) -> bool:
        return True

    def stream(self, _request, on_delta, handle=None) -> str:
        for chunk in self.chunks:
            on_delta(chunk)
        return "".join(self.chunks)


@pytest.mark.parametrize(
    "chunks",
    [
        ("\uac00\r\n", "\ub098"),
        ("\u1100\u1161", "\ub098"),
    ],
)
def test_streamed_output_is_committed_without_normalization(chunks: tuple[str, ...]) -> None:
    provider = RawStreamingProvider(chunks)
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(conversation.conversation_id, capability, "raw", "\uc9c8\ubb38")
    application.wait_for_generations()
    final, events = application.get_run_snapshot(accepted.run_id, capability)
    assert final.state == "completed"
    assert final.output_message.content == "".join(chunks)
    assert "".join(event.text for event in events if event.type == "message.delta") == "".join(chunks)


def test_sse_emits_one_keepalive_per_interval_while_the_run_is_active(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "SSE_KEEPALIVE_SECONDS", 0.05)
    provider = StreamingProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(conversation.conversation_id, capability, "keepalive", "\uc9c8\ubb38")
    assert provider.first.wait(1)
    with tcp_server(application) as base_url, httpx.Client(base_url=base_url, timeout=3) as client:
        headers = {"Cookie": f"conversation_capability={capability}"}
        with client.stream("GET", f"/api/v1/runs/{accepted.run_id}/events", headers=headers) as response:
            comments = 0
            for line in response.iter_lines():
                if line.startswith(": keepalive"):
                    comments += 1
                    if comments == 2:
                        break
        provider.release.set()
    application.wait_for_generations()
    assert comments == 2
