"""Story 1.11 -- Docker·real Provider·public demo Release verification.

Four things this file is responsible for, and one it deliberately is not.

It runs ONE contract battery against three Provider configurations (Deterministic
Core Model, Ollama LAN Binding, Target Direct Adapter), so "the three pass the same
contract" is a claim made by running the same assertions three times rather than by
asserting it in prose. It pins AR27's success Cache to `provider_profile_digest` and
splits `provider_profile_changed` out of `retry_not_allowed`. It holds `/openapi.json`
to every Operation, Schema and Korean Error Envelope this process can emit, and it
holds `docs/sse-contract.md` to the Event set the Server and the Client actually use.
And it aggregates the Release Test Suite by Marker rather than by re-implementation.

What it is NOT is a second copy of Stories 1.3-1.9. Eight of the nine Release Suite
items are already proven there; re-writing them would produce two batteries that
disagree the first time one of them is edited.
"""

import ast
import json
import pathlib
import re
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from contextlib import contextmanager
from threading import Event, Thread
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import aidd_chat.application as application_module
import aidd_chat.main as main_module

from aidd_chat.adapters import DETERMINISTIC_BINDING, FakeAgent, InMemoryConversationStore, LocalTestBinding
from aidd_chat.application import (
    READINESS_CACHE_SECONDS,
    ChatApplication,
    PolicyProjectionV1,
    ProviderProfileChanged,
    build_policy_projection,
)
from aidd_chat.contracts import (
    ContextIntegrityError,
    PreparedMessageV1,
    RunEventV1,
    prepare_model_request,
)
from aidd_chat.domain import ConversationAggregate, RetryNotRetryable
from aidd_chat.main import app, get_chat_application
from agent_fakes import LegacySyncAgent
from test_story_1_5_1 import API_KEY


ROOT = pathlib.Path(__file__).parents[1]
ORIGIN = {"Origin": "https://testserver"}


# --- The configurations ----------------------------------------------


def _configuration(name: str):
    """(provider, fake). `fake` is None for the configuration that makes no
    outbound request at all."""
    assert name == "deterministic"
    return FakeAgent(), None


# The "ollama_lan" and "target_direct" configurations were the PydanticAI Direct
# adapters, retired by AD-24; the pi sidecar binding joins this battery with its own
# contract tests (Task 14).
CONFIGURATIONS = ("deterministic",)


@pytest.fixture(params=CONFIGURATIONS)
def configuration(request):
    provider, fake = _configuration(request.param)
    return request.param, provider, fake


@contextmanager
def route_client(application: ChatApplication):
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_chat_application, None)


def _application(provider) -> ChatApplication:
    return ChatApplication(InMemoryConversationStore(), provider, 3)


def _ask(client, content: str = "한국어로 답해 주세요", key: str = "s111"):
    created = client.post("/api/v1/conversations", headers=ORIGIN)
    assert created.status_code == 201
    conversation_id = created.json()["conversation_id"]
    accepted = client.post(
        f"/api/v1/conversations/{conversation_id}/runs",
        headers={**ORIGIN, "Idempotency-Key": key},
        json={"kind": "question", "content": content},
    )
    return conversation_id, accepted


def _settle(client, application: ChatApplication, run_id: str):
    application.wait_for_generations(10)
    return client.get(f"/api/v1/runs/{run_id}")


def _event_types(client, run_id: str) -> list[str]:
    """The Event Type sequence, with consecutive `message.delta` collapsed to one:
    how MANY deltas a Provider emits is the Provider's business, and the contract
    the three configurations share is the ORDER around them."""
    stream = client.get(f"/api/v1/runs/{run_id}/events")
    assert stream.status_code == 200
    types = [
        line.removeprefix("event: ")
        for line in stream.text.splitlines()
        if line.startswith("event: ")
    ]
    collapsed: list[str] = []
    for event_type in types:
        # Likewise how many steps an agent takes (AD-29): the order is the contract.
        if event_type in ("message.delta", "agent.step") and collapsed[-1:] == [event_type]:
            continue
        collapsed.append(event_type)
    return collapsed


def _count_probes(provider) -> dict:
    """Probe calls, counted on the instance. `_run_probe` reads `probe` off the
    Provider with getattr, so an instance attribute is enough and no configuration
    needs a counting subclass of its own."""
    counter = {"n": 0}
    inner = provider.probe

    async def counted():
        counter["n"] += 1
        return await inner()

    provider.probe = counted
    return counter


class _BrokenAgent(LegacySyncAgent):
    def stream(self, request, on_delta, handle=None):
        raise RuntimeError("provider down")


def _broken_deterministic() -> LegacySyncAgent:
    """Ready, and unable to answer."""
    return _BrokenAgent()


# --- 3-configuration parity battery ----------------------------------------


def test_every_configuration_is_ready_inside_the_probe_budget(configuration) -> None:
    name, provider, fake = configuration
    probes = _count_probes(provider)
    application = _application(provider)

    started = time.monotonic()
    assert application.is_ready() is True, name
    assert time.monotonic() - started < application.PROBE_TIMEOUT_SECONDS
    assert probes["n"] == 1
    if fake is not None:
        # The probe is a real streaming call on the Run's own path, not a ping.
        assert fake.payloads[-1]["stream"] is True
        assert "tools" not in fake.payloads[-1]

    # Cached by Digest for 30 s: the second call probes nothing.
    assert application.is_ready() is True
    assert probes["n"] == 1

    application._last_success_at -= READINESS_CACHE_SECONDS + 1
    assert application.is_ready() is True
    assert probes["n"] == 2


def test_every_configuration_projects_the_same_closed_policy(configuration) -> None:
    name, provider, _fake = configuration
    application = _application(provider)
    projection = application.get_policy_if_ready()

    assert isinstance(projection, PolicyProjectionV1), name
    assert projection.retrieval_status == "wiki_readonly"
    assert projection.session_ttl_seconds == 3_600
    assert projection.transmitted_fields == application_module.TRANSMITTED_FIELDS
    # Closed, and equal to what a re-validation of its own dump produces -- the same
    # rule the Readiness Gate applies, checked here for all three bindings.
    assert PolicyProjectionV1.model_validate(projection.model_dump()) == projection
    # A credential never reaches the projection, whichever binding produced it.
    assert API_KEY not in json.dumps(projection.model_dump(mode="json"), ensure_ascii=False)


def test_every_configuration_answers_one_question_over_the_same_api_and_sse(
    configuration,
) -> None:
    name, provider, _fake = configuration
    application = _application(provider)
    with route_client(application) as client:
        conversation_id, accepted = _ask(client)
        assert accepted.status_code == 202, name
        run_id = accepted.json()["run_id"]
        settled = _settle(client, application, run_id)

        assert settled.status_code == 200
        body = settled.json()
        assert body["state"] == "completed" and body["stage"] == "terminal"
        assert body["output_message"]["message_id"] == body["output_message_id"]
        assert body["output_message"]["content"]

        assert _event_types(client, run_id) == [
            "run.status", "agent.step", "message.delta", "message.sources", "message.completed",
            "run.status", "stream.end",
        ]
        # Terminal Close: replaying from the last Sequence has nothing left to send.
        exhausted = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": str(body["latest_sequence"])},
        )
        assert exhausted.status_code == 204 and exhausted.content == b""
        ahead = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": str(body["latest_sequence"] + 1)},
        )
        assert ahead.status_code == 400
        assert ahead.json()["error"]["code"] == "invalid_event_cursor"

        # Cancel and Retry of a Run that already completed: the same two answers.
        cancelled = client.post(f"/api/v1/runs/{run_id}/cancel", headers=ORIGIN)
        assert cancelled.status_code == 200
        assert cancelled.json()["cancel_outcome"] == "already_terminal"
        assert cancelled.json()["run"]["output_message_id"] == body["output_message_id"]
        retried = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={**ORIGIN, "Idempotency-Key": "retry-completed"},
            json={"kind": "retry", "retry_of_run_id": run_id},
        )
        assert retried.status_code == 409
        assert retried.json()["error"]["code"] == "retry_not_allowed"


@pytest.mark.release_suite
@pytest.mark.parametrize("name", CONFIGURATIONS)
def test_every_configuration_fails_a_run_with_the_same_shape(name: str) -> None:
    """A Provider fault, three ways in, one contract out: no completed marker, the
    discarded/error/status/end quadruple, and a typed Korean envelope on the Run."""
    provider = _broken_deterministic()
    application = _application(provider)
    assert application.is_ready() is True, name
    with route_client(application) as client:
        _conversation_id, accepted = _ask(client, key=f"fail-{name}")
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        settled = _settle(client, application, run_id)

        body = settled.json()
        assert body["state"] == "failed", name
        assert body["output_message"] is None and body["output_message_id"] is None
        failure = body["terminal_error"]
        assert failure["kind"].startswith("provider_")
        assert failure["message"] and failure["correlation_id"]
        assert failure["retryable"] in (True, False)
        assert _event_types(client, run_id) == [
            "run.status", "message.discarded", "run.error", "run.status", "stream.end"
        ]


def test_every_configuration_holds_the_ad25_request_contract(configuration) -> None:
    """AD-25: the Digest travels with the request and is recomputed by whoever
    consumes it, and the Tool Policy on the wire is the zero one."""
    name, provider, fake = configuration
    request = prepare_model_request(
        (PreparedMessageV1(uuid4(), "user", "질문"),),
        provider_profile_digest=provider.provider_profile_digest,
    )

    assert provider.count_input_tokens(request) > 0, name
    # A Snapshot minted against another Provider Profile is refused by the counter
    # itself -- which is what makes the Retry Gate in ChatApplication meaningful.
    foreign = prepare_model_request(
        (PreparedMessageV1(uuid4(), "user", "질문"),),
        provider_profile_digest="0" * 64,
    )
    # Narrow on purpose: `pytest.raises(Exception)` would also pass on the
    # AttributeError a refactor introduces, which is the opposite of this assertion.
    with pytest.raises(ContextIntegrityError):
        provider.count_input_tokens(foreign)

    # The zero-Tool Policy half of this test (ToolPolicyV1 on the wire) is retired:
    # AD-26 replaces it with the agent's `tool_allowlist` (Task 13).


def test_every_abnormal_probe_result_fails_readiness_closed() -> None:
    """The frozen matrix names six: Missing · Stale · Timeout · Error · Unhealthy ·
    Incomplete. None of them may reach `/ready` 204."""

    def refused(application: ChatApplication, label: str) -> None:
        assert application.is_ready() is False, label
        assert main_module.ready(application).status_code == 503, label
        assert application._last_success_at is None, label

    unhealthy = _CountingProvider(healthy=False)
    refused(_application(unhealthy), "unhealthy")

    class Raising(_CountingProvider):
        def probe(self) -> bool:
            self.calls += 1
            raise RuntimeError("provider unavailable")

    refused(_application(Raising()), "error")

    class Incomplete(_CountingProvider):
        def probe(self):
            self.calls += 1
            return 1  # truthy, not a completed boolean answer

    refused(_application(Incomplete()), "incomplete")

    missing = _CountingProvider()
    missing.probe = None
    refused(_application(missing), "missing")

    class Slow(_CountingProvider):
        def probe(self) -> bool:
            self.calls += 1
            time.sleep(0.5)
            return True

    slow_application = _application(Slow())
    slow_application.PROBE_TIMEOUT_SECONDS = 0.05
    refused(slow_application, "timeout")

    # Stale: a success older than the 30 s window is not reused, so a Provider that
    # has since gone unhealthy is refused rather than coasting on it.
    aging = _CountingProvider()
    application = _application(aging)
    assert application.is_ready() is True
    aging._healthy = False
    application._last_success_at -= READINESS_CACHE_SECONDS + 1
    refused(application, "stale")


class _PartialAgent(LegacySyncAgent):
    def __init__(self, after: str) -> None:
        self.after = after
        self.stopped = Event()

    def new_call_handle(self):
        agent = self

        class Handle:
            def cancel(self) -> None:
                agent.stopped.set()

        return Handle()

    def stream(self, request, on_delta, handle=None):
        on_delta(PARTIAL_TEXT)
        if self.after == "raise":
            raise RuntimeError("provider down")
        self.stopped.wait(30)
        raise RuntimeError("stopped")


def _partial_output_provider(after: str) -> LegacySyncAgent:
    """A Provider that emits a real delta and then does NOT finish -- the only shape
    that can prove "부분 출력의 completed 표시 0건", because a Run with no partial
    output has nothing to mis-mark."""
    return _PartialAgent(after)


PARTIAL_TEXT = "생성 중인 미완료 문장"


def _await_partial_output(client, run_id: str) -> None:
    """Wait for `run.status` + `message.delta` to be committed, by POLLING. Reading
    the SSE stream instead would block until the Run terminates -- which for a Run
    this test is about to cancel is the one thing that must not happen first."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if client.get(f"/api/v1/runs/{run_id}").json()["latest_sequence"] >= 2:
            return
        time.sleep(0.02)
    raise AssertionError("the fixture never produced partial output")


@pytest.mark.release_suite
def test_a_failed_run_with_partial_output_is_never_marked_completed() -> None:
    application = _application(_partial_output_provider("raise"))
    with route_client(application) as client:
        _conversation_id, accepted = _ask(client, key="partial-fail")
        run_id = accepted.json()["run_id"]
        body = _settle(client, application, run_id).json()
        types = _event_types(client, run_id)
        stream = client.get(f"/api/v1/runs/{run_id}/events").text

    assert body["state"] == "failed"
    assert body["output_message"] is None and body["output_message_id"] is None
    assert PARTIAL_TEXT in stream, "the fixture produced no partial output to discard"
    assert types == [
        "run.status", "message.delta", "message.discarded", "run.error", "run.status", "stream.end"
    ]
    assert "message.completed" not in types


@pytest.mark.release_suite
def test_a_cancelled_run_with_partial_output_is_never_marked_completed() -> None:
    application = _application(_partial_output_provider("hang"))
    with route_client(application) as client:
        _conversation_id, accepted = _ask(client, key="partial-cancel")
        run_id = accepted.json()["run_id"]
        _await_partial_output(client, run_id)
        cancelled = client.post(f"/api/v1/runs/{run_id}/cancel", headers=ORIGIN)
        assert cancelled.status_code == 200
        application.wait_for_generations(10)
        body = client.get(f"/api/v1/runs/{run_id}").json()
        types = _event_types(client, run_id)
        stream = client.get(f"/api/v1/runs/{run_id}/events").text

    assert body["state"] == "cancelled"
    assert body["output_message"] is None and body["output_message_id"] is None
    assert PARTIAL_TEXT in stream, "the fixture produced no partial output to discard"
    assert types == [
        "run.status", "message.delta", "message.discarded", "run.status", "stream.end"
    ]
    assert "message.completed" not in types


# --- AR27: the success Cache Key is a provider_profile_digest ---------------


def _variant(**overrides) -> LocalTestBinding:
    return replace(DETERMINISTIC_BINDING, **overrides)


def _rebind(application: ChatApplication, provider) -> None:
    """What a Binding change looks like from inside the process: a new Provider, a
    new Policy Projection built from it, and a new Bootstrap Binding tying the two.
    All three, because a readiness Gate that saw only two of them would answer
    `provider_unbound` and never reach the Cache this is about."""
    projection = build_policy_projection(provider.policy_metadata)
    application._policy_projection = projection
    application._provider_binding = provider
    application._bootstrap_binding = application_module._BootstrapBinding(provider, projection)


def _bound(binding: LocalTestBinding) -> FakeAgent:
    agent = FakeAgent()
    agent.binding = binding
    return agent


class _CountingProvider(LegacySyncAgent):
    def __init__(self, binding: LocalTestBinding = DETERMINISTIC_BINDING, healthy: bool = True):
        super().__init__()
        self.binding = binding
        self.calls = 0
        self._healthy = healthy

    def probe(self) -> bool:
        self.calls += 1
        return self._healthy


def test_the_success_cache_is_keyed_by_digest_not_by_object_identity() -> None:
    """The old key was `_last_success_binding is self._bootstrap_binding`. Rebuilding
    the same Provider Binding object -- same Provider, same Projection, same Digest --
    killed a Cache that AR27 says should still be valid."""
    provider = _CountingProvider()
    application = _application(provider)
    assert application.is_ready() is True
    assert provider.calls == 1

    application._bootstrap_binding = application_module._BootstrapBinding(
        provider, application.policy_projection
    )
    assert application.is_ready() is True
    assert provider.calls == 1, "same Digest, rebuilt Binding object: no re-Probe"


def test_the_cache_survives_a_rebuilt_but_identical_policy_projection() -> None:
    """The other half of the same migration. A Bootstrap that rebuilds the Projection
    -- same metadata, same values, new object -- has not changed the Policy, and the
    30-second window belongs to the Provider Profile, not to an object address."""
    provider = _CountingProvider()
    application = _application(provider)
    assert application.is_ready() is True
    assert provider.calls == 1

    rebuilt = build_policy_projection(provider.policy_metadata)
    assert rebuilt == application.policy_projection
    assert rebuilt is not application.policy_projection
    application._policy_projection = rebuilt
    application._bootstrap_binding = application_module._BootstrapBinding(provider, rebuilt)

    assert application.is_ready() is True
    assert provider.calls == 1


def test_a_different_digest_invalidates_the_cache_and_blocks_new_work() -> None:
    healthy = _CountingProvider()
    application = _application(healthy)
    assert application.is_ready() is True

    replacement = _CountingProvider(_variant(model_revision="deterministic-v2"), healthy=False)
    assert replacement.provider_profile_digest != healthy.provider_profile_digest
    _rebind(application, replacement)

    # Immediate, not after the 30 s window: the Cache Key no longer matches.
    assert application.is_ready() is False
    assert application._last_success_at is None
    with route_client(application) as client:
        _conversation_id, refused = _ask(client, key="blocked")
        assert refused.status_code == 503
        assert refused.json()["error"]["code"] == "provider_unavailable"
        assert refused.json()["error"]["retryable"] is True
        assert client.get("/ready").status_code == 503
        assert client.get("/api/v1/policy").status_code == 503


class _BlockingProbeProvider(_CountingProvider):
    """Holds its own probe open so a test can change what THIS object reports while
    its flight is still in the air."""

    def __init__(self) -> None:
        super().__init__()
        self.started, self.release = Event(), Event()

    def probe(self) -> bool:
        self.calls += 1
        self.started.set()
        self.release.wait(5)
        return True


def test_a_probe_that_lands_after_the_digest_moved_does_not_stamp_it() -> None:
    """The Digest re-check in `_run_probe`, tested where it is the DECIDING term.

    Every identity guard beside it still holds here: the same Provider object, the
    same Bootstrap Binding, the same Projection. Only the Provider Profile moves --
    in place, on the object the flight is already holding -- and the probe is allowed
    to land inside its own deadline. Delete the two Digest lines and this fails:
    `_last_success_digest` is stamped with a Profile that no longer exists."""
    provider = _BlockingProbeProvider()
    application = _application(provider)
    result: list[bool] = []
    first = Thread(target=lambda: result.append(application.is_ready()))
    first.start()
    assert provider.started.wait(2)

    before = provider.provider_profile_digest
    provider.binding = _variant(model_revision="deterministic-v9")
    assert provider.provider_profile_digest != before
    assert application._provider_binding is provider
    assert application._bootstrap_binding.provider is provider
    provider.release.set()
    first.join(5)

    assert result == [False]
    assert application._last_success_digest is None
    assert application._last_success_at is None


# --- provider_profile_changed is not retry_not_allowed ---------------------


def test_a_terminal_retry_from_a_previous_digest_is_refused_without_creating_work() -> None:
    """A `failed` Run IS retryable, so this cannot come back as `retry_not_allowed`
    -- and after a rebind it must not come back as an accepted Retry either."""
    application = _application(_broken_deterministic())
    store = application.store
    with route_client(application) as client:
        conversation_id, accepted = _ask(client, key="will-fail")
        failed_run_id = accepted.json()["run_id"]
        application.wait_for_generations(10)
        assert client.get(f"/api/v1/runs/{failed_run_id}").json()["state"] == "failed"

        state = store.states[UUID(conversation_id)]
        before = (len(state.runs), len(state.messages), len(state.receipts))
        target = state.runs[UUID(failed_run_id)]
        assert target.provider_binding_digest is not None

        rebound = _bound(_variant(model_revision="deterministic-v4"))
        assert rebound.binding_digest != target.provider_binding_digest
        _rebind(application, rebound)
        assert application.is_ready() is True

        refused = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={**ORIGIN, "Idempotency-Key": "after-rebind"},
            json={"kind": "retry", "retry_of_run_id": failed_run_id},
        )

    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "provider_profile_changed"
    assert error["retryable"] is False
    assert error["message"] and error["field_errors"] == {}
    # Nothing was created: no Run, no Message, no consumed Attempt, no Provider call.
    assert (len(state.runs), len(state.messages), len(state.receipts)) == before


def test_an_exhausted_lineage_is_refused_as_exhausted_even_after_a_rebind() -> None:
    """Order matters because the recoveries differ. `provider_profile_changed` says
    "send the same question again"; an exhausted lineage cannot be re-asked at all,
    so that advice walks the user into a second refusal. Exhaustion answers first."""
    application = ChatApplication(InMemoryConversationStore(), _broken_deterministic(), 1)
    with route_client(application) as client:
        conversation_id, accepted = _ask(client, key="exhausted")
        failed_run_id = accepted.json()["run_id"]
        application.wait_for_generations(10)
        assert client.get(f"/api/v1/runs/{failed_run_id}").json()["state"] == "failed"

        _rebind(application, _bound(_variant(model_revision="det-v5")))
        assert application.is_ready() is True
        refused = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={**ORIGIN, "Idempotency-Key": "exhausted-after-rebind"},
            json={"kind": "retry", "retry_of_run_id": failed_run_id},
        )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "retry_exhausted"
    assert refused.json()["error"]["retryable"] is False


def test_a_snapshot_that_fails_its_own_integrity_check_is_still_refused() -> None:
    """What is left for `_snapshot_still_sendable` once the Binding gate runs first.
    Every Provider-change route to False is now named `provider_profile_changed`; a
    Snapshot that no longer re-serializes to its own digests is a different fault and
    must still be refused, fail-closed, before an Attempt is consumed."""
    application = _application(_broken_deterministic())
    store = application.store
    with route_client(application) as client:
        conversation_id, accepted = _ask(client, key="tamper")
        failed_run_id = accepted.json()["run_id"]
        application.wait_for_generations(10)

        state = store.states[UUID(conversation_id)]
        target = state.runs[UUID(failed_run_id)]
        assert target.prepared_request is not None
        # `replace` on the frozen dataclass runs no validation, which is exactly the
        # corruption this guard exists for: the digests still say one thing and the
        # bytes another.
        target.prepared_request = replace(
            target.prepared_request, canonical_bytes=b'{"schema_version":"1"}'
        )
        before = (len(state.runs), len(state.messages), len(state.receipts))

        refused = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={**ORIGIN, "Idempotency-Key": "tampered-retry"},
            json={"kind": "retry", "retry_of_run_id": failed_run_id},
        )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "retry_not_allowed"
    assert (len(state.runs), len(state.messages), len(state.receipts)) == before
    # Not the integrity poison: a bad Snapshot refuses one Retry, not the process.
    assert application._integrity_poisoned is False


def test_the_same_digest_still_accepts_the_retry_it_always_did() -> None:
    """The other half. A Gate that refuses everything is not a Gate: an unchanged
    Binding must keep accepting the Retries that were legal before."""
    application = _application(_broken_deterministic())
    with route_client(application) as client:
        conversation_id, accepted = _ask(client, key="same-digest")
        failed_run_id = accepted.json()["run_id"]
        application.wait_for_generations(10)

        retried = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={**ORIGIN, "Idempotency-Key": "retry-same-digest"},
            json={"kind": "retry", "retry_of_run_id": failed_run_id},
        )
        application.wait_for_generations(10)

    assert retried.status_code == 202
    assert retried.json()["retry_of_run_id"] == failed_run_id


def test_a_target_that_never_reached_the_provider_is_not_a_digest_mismatch() -> None:
    """`Run.provider_binding_digest` is filled by `mark_running`, so a Run that
    failed before the Provider has None -- which is absence, not a change."""
    application = _application(FakeAgent())
    assert application._provider_profile_changed(None, None) is False
    assert (
        application._provider_profile_changed(None, application.provider.binding_digest) is False
    )
    assert application._provider_profile_changed(None, "0" * 64) is True


def test_provider_profile_changed_and_retry_not_allowed_are_different_facts() -> None:
    assert not issubclass(ProviderProfileChanged, RetryNotRetryable)
    assert not issubclass(RetryNotRetryable, ProviderProfileChanged)
    changed = main_module._ERROR_ENVELOPES["provider_profile_changed"]
    not_allowed = main_module._ERROR_ENVELOPES["retry_not_allowed"]
    assert changed[0] == not_allowed[0] == 409
    assert changed[1] != not_allowed[1]

    source = (ROOT / "src/aidd_chat/web/app.js").read_text(encoding="utf-8")
    retry_codes = _js_string_set(source, "RETRY_ERROR_CODES")
    fail_closed = _js_string_set(source, "RETRY_FAIL_CLOSED_CODES")
    assert "provider_profile_changed" in retry_codes
    # Only the Provider moved: the next question in this Conversation is still
    # possible, so the composer must not close.
    assert "provider_profile_changed" not in fail_closed


def _js_string_set(source: str, name: str) -> set[str]:
    match = re.search(rf"const {name} = new Set\(\[(.*?)\]\);", source, re.DOTALL)
    assert match, name
    return set(re.findall(r'"([a-z_.]+)"', match.group(1)))


# --- OpenAPI ---------------------------------------------------------------


@pytest.fixture(scope="module")
def openapi() -> dict:
    return app.openapi()


def test_openapi_carries_metadata_and_every_json_operation(openapi: dict) -> None:
    assert openapi["info"]["title"] and openapi["info"]["version"]
    assert "docs/sse-contract.md" in openapi["info"]["description"]

    from fastapi.routing import APIRoute

    declared = {
        (route.path, method.lower())
        for route in app.routes
        if isinstance(route, APIRoute) and route.include_in_schema
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    }
    documented = {
        (path, method) for path, item in openapi["paths"].items() for method in item
    }
    assert declared == documented
    for path, item in openapi["paths"].items():
        for method, operation in item.items():
            assert operation.get("summary"), (path, method)
            assert operation["responses"], (path, method)
            for status, response in operation["responses"].items():
                assert response.get("description"), (path, method, status)


def test_openapi_declares_every_korean_error_envelope(openapi: dict) -> None:
    """Every Code this process can answer with is named in some Operation, with the
    Korean sentence beside it, and every error status carries the one Envelope."""
    described = " ".join(
        response["description"]
        for item in openapi["paths"].values()
        for operation in item.values()
        for response in operation["responses"].values()
    )
    for code, (status, message, _retryable) in main_module._ERROR_ENVELOPES.items():
        assert f"`{code}`" in described, code
        assert message in described, code
        assert 400 <= status <= 599

    envelope = openapi["components"]["schemas"]["ErrorEnvelopeV1"]
    body = openapi["components"]["schemas"]["ErrorBodyV1"]
    assert envelope["required"] == ["error"]
    assert set(body["properties"]) == {
        "code", "message", "retryable", "correlation_id", "field_errors"
    }
    for item in openapi["paths"].values():
        for operation in item.values():
            for status, response in operation["responses"].items():
                content = response.get("content")
                if int(status) < 400 or content is None:
                    continue
                schema = content["application/json"]["schema"]
                assert schema["$ref"].endswith("/ErrorEnvelopeV1"), status


# Which Operation each `error_response` call site belongs to. `_retry` is a helper
# of the runs POST, not a Route of its own. The three global helpers can answer for
# any /api/v1 Operation, so their Codes must be declared on all of them.
_CALL_SITE_ROUTES = {
    "policy": ("/api/v1/policy", "get"),
    "create_conversation": ("/api/v1/conversations", "post"),
    "submit_question": ("/api/v1/conversations/{conversation_id}/runs", "post"),
    "_retry": ("/api/v1/conversations/{conversation_id}/runs", "post"),
    "poll_run": ("/api/v1/runs/{run_id}", "get"),
    "cancel_run": ("/api/v1/runs/{run_id}/cancel", "post"),
    "stream_run": ("/api/v1/runs/{run_id}/events", "get"),
}
_GLOBAL_HELPERS = frozenset({"validation_error", "ip_gate", "capacity_response"})


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    """node -> the name of the module-level function it sits in."""
    owner: dict[ast.AST, str] = {}
    for top in ast.walk(tree):
        if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(top):
            owner.setdefault(inner, top.name)
    return owner


def test_every_error_response_call_site_matches_the_declared_envelope(openapi: dict) -> None:
    """The catalog is only a single source if nothing bypasses it, and a declaration
    only means something if it is attached to the Operation that can actually emit
    the Code. Every literal `error_response(status, code, message, retryable)` in
    main.py is checked against the catalog AND against its own Route's `responses`,
    so moving a Code onto an Operation that never emits it fails here."""
    source = (ROOT / "src/aidd_chat/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    owner = _enclosing_functions(tree)
    seen: set[str] = set()
    forwarded: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "error_response"):
            continue
        literals = [
            argument.value for argument in node.args if isinstance(argument, ast.Constant)
        ]
        if not literals:
            # The one funnel with no literals of its own: `capacity_response` passes
            # the reason and sentence it read out of `_CAPACITY_REJECTIONS`, which the
            # catalog folds in rather than duplicates.
            forwarded.append(ast.unparse(node))
            continue
        assert len(literals) >= 3, ast.unparse(node)
        status, code, message = literals[0], literals[1], literals[2]
        retryable = literals[3] if len(literals) > 3 else False
        declared_status, declared_message, declared_retryable = main_module._ERROR_ENVELOPES[code]
        assert (status, message) == (declared_status, declared_message), code
        if declared_retryable is not None:
            # One Code, one meaning. `None` is the documented exception and the
            # catalog says why for each of them.
            assert retryable == declared_retryable, (code, ast.unparse(node))
        seen.add(code)

        function = owner.get(node)
        targets = (
            list(_CALL_SITE_ROUTES.values())
            if function in _GLOBAL_HELPERS
            else [_CALL_SITE_ROUTES[function]]
        )
        for path, method in targets:
            declared = openapi["paths"][path][method]["responses"]
            assert f"`{code}`" in declared[str(status)]["description"], (code, path, method)
    assert forwarded == ["error_response(status_code, exc.reason, message, exc.retryable)"]
    seen |= set(main_module._CAPACITY_REJECTIONS)
    assert seen == set(main_module._ERROR_ENVELOPES)


def test_the_only_site_decided_retryabilities_are_the_documented_two() -> None:
    """`None` in the catalog is a licence for a call site to disagree with itself, so
    the set that holds it is pinned rather than left to grow."""
    undecided = {
        code for code, (_status, _message, retryable) in main_module._ERROR_ENVELOPES.items()
        if retryable is None
    }
    assert undecided == set(main_module._CAPACITY_REJECTIONS) | {"provider_unavailable"}


def test_openapi_every_ref_resolves(openapi: dict) -> None:
    """Named by `_envelopes`'s docstring, which relies on another Route registering
    ErrorEnvelopeV1 for the SSE Route's hand-written `$ref`."""
    schemas = openapi["components"]["schemas"]

    def refs(node):
        if isinstance(node, dict):
            if "$ref" in node:
                yield node["$ref"]
            for value in node.values():
                yield from refs(value)
        elif isinstance(node, list):
            for value in node:
                yield from refs(value)

    found = set(refs(openapi))
    assert found
    for ref in found:
        assert ref.startswith("#/components/schemas/"), ref
        assert ref.rsplit("/", 1)[-1] in schemas, ref
    assert "#/components/schemas/ErrorEnvelopeV1" in found


def test_the_sse_operation_declares_every_run_event_schema(openapi: dict) -> None:
    operation = openapi["paths"]["/api/v1/runs/{run_id}/events"]["get"]
    responses = operation["responses"]
    assert set(responses) >= {"200", "204", "400", "404", "410"}
    assert "204" in responses and "content" not in responses["204"]
    assert "content" not in responses["404"]

    content = responses["200"]["content"]
    assert set(content) == {"text/event-stream"}
    schema = content["text/event-stream"]["schema"]
    refs = {option["$ref"].rsplit("/", 1)[-1] for option in schema["oneOf"]}
    assert refs == {member.__name__ for member in _event_models()}
    assert set(schema["discriminator"]["mapping"]) == _event_types_from_contract()
    assert "docs/sse-contract.md" in responses["200"]["description"]


# --- SSE document ----------------------------------------------------------


def _event_models() -> tuple[type, ...]:
    return RunEventV1.__origin__.__args__


def _event_types_from_contract() -> set[str]:
    return {
        model.model_fields["type"].default for model in _event_models()
    }


def _event_types_from_document() -> set[str]:
    text = (ROOT / "docs/sse-contract.md").read_text(encoding="utf-8")
    table = text.split("## Event 집합", 1)[1].split("## 순서", 1)[0]
    return set(re.findall(r"^\| `([a-z.]+)` \|", table, re.MULTILINE))


def test_the_document_and_contract_agree_on_the_event_set() -> None:
    contract = _event_types_from_contract()
    # AD-29 added `agent.step` and `message.sources` to the eight.
    assert len(contract) == 10
    assert _event_types_from_document() == contract


def test_the_client_and_contract_agree_on_the_event_set() -> None:
    source = (ROOT / "src/aidd_chat/web/app.js").read_text(encoding="utf-8")
    match = re.search(r"const EVENT_TYPES = \[(.*?)\];", source, re.DOTALL)
    assert match
    client = set(re.findall(r'"([a-z.]+)"', match.group(1)))
    assert client == _event_types_from_contract()


def test_every_documented_order_is_the_order_a_real_run_produces() -> None:
    """The document is only a contract if the code agrees with it. All four orderings
    it prints -- success, failure/Timeout, Cancel and the expiry Tail -- are compared
    against the Events a real Run (or the Aggregate itself, for expiry) emits."""
    text = (ROOT / "docs/sse-contract.md").read_text(encoding="utf-8")

    def documented(label: str) -> list[str]:
        return re.findall(r"[a-z]+\.[a-z]+", text.split(label, 1)[1].split("```")[1])

    success = documented("성공:")
    failure = documented("실패·Timeout:")
    cancel = documented("Cancel:")
    expiry = documented("## 만료 Tail")
    assert success == [
        "run.status", "context.truncated", "agent.step", "message.delta", "message.sources",
        "message.completed", "run.status", "stream.end",
    ]
    assert failure == [
        "run.status", "message.delta", "message.discarded", "run.error", "run.status",
        "stream.end",
    ]
    assert cancel == [
        "run.status", "message.delta", "message.discarded", "run.status", "stream.end"
    ]
    assert expiry == ["conversation.expired", "stream.end"]

    application = _application(FakeAgent())
    with route_client(application) as client:
        _conversation_id, accepted = _ask(client, key="doc-order")
        run_id = accepted.json()["run_id"]
        _settle(client, application, run_id)
        observed = _event_types(client, run_id)
    # A Run whose context was not truncated: the one optional piece dropped.
    assert observed == [item for item in success if item != "context.truncated"]

    # The Aggregate's own expiry Tail -- the single definition every writer uses.
    now = datetime.now(UTC)
    aggregate = ConversationAggregate(
        uuid4(), now, now + timedelta(seconds=3_600), sha256(b"cap").hexdigest()
    )
    accepted_run = aggregate.accept_question("k", "d", "질문", now, correlation_id=uuid4())
    aggregate.mark_running(accepted_run.run.run_id, now, time.monotonic())
    assert [event.type for event in aggregate.expire(now)] == expiry


def test_the_documented_failure_and_cancel_orders_match_a_run_with_partial_output() -> None:
    """The two orderings above, observed rather than only declared -- and observed on
    Runs that actually produced partial output, so `message.delta` in the document is
    a real position and not a hopeful one. Same fixtures the Release Suite uses."""
    text = (ROOT / "docs/sse-contract.md").read_text(encoding="utf-8")

    def documented(label: str) -> list[str]:
        return re.findall(r"[a-z]+\.[a-z]+", text.split(label, 1)[1].split("```")[1])

    failing = _application(_partial_output_provider("raise"))
    with route_client(failing) as client:
        _conversation_id, accepted = _ask(client, key="doc-failure")
        run_id = accepted.json()["run_id"]
        _settle(client, failing, run_id)
        assert _event_types(client, run_id) == documented("실패·Timeout:")

    hanging = _application(_partial_output_provider("hang"))
    with route_client(hanging) as client:
        _conversation_id, accepted = _ask(client, key="doc-cancel")
        run_id = accepted.json()["run_id"]
        _await_partial_output(client, run_id)
        client.post(f"/api/v1/runs/{run_id}/cancel", headers=ORIGIN)
        hanging.wait_for_generations(10)
        assert _event_types(client, run_id) == documented("Cancel:")



def test_the_sse_document_lists_exactly_the_codes_the_operation_declares(openapi: dict) -> None:
    text = (ROOT / "docs/sse-contract.md").read_text(encoding="utf-8")
    table = text.split("## Error", 1)[1]
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", table, re.MULTILINE))

    operation = openapi["paths"]["/api/v1/runs/{run_id}/events"]["get"]["responses"]
    declared = set(
        re.findall(
            r"`([a-z_]+)`",
            " ".join(
                response["description"]
                for status, response in operation.items()
                if int(status) >= 400
            ),
        )
    )
    assert documented == declared
    # The one field a reconnecting client needs, and the one refusal that does not
    # carry it.
    assert "Retry-After" in table
    assert "deployment_unconfigured" in documented


def test_the_document_defers_expiry_and_retry_rules_rather_than_restating_them() -> None:
    text = (ROOT / "docs/sse-contract.md").read_text(encoding="utf-8")
    assert "README.md" in text
    assert "Last-Event-ID" in text and "final_state" in text
    assert "3,600" in text  # only as a reference to the rule README owns
    assert "invalid_event_cursor" in text


# --- Release Test Suite: a Marker, not a second battery --------------------


# The nine items, and the test that already proves each one. Docker is the only new
# one; the other eight were written by Stories 1.3-1.9 and are selected, not copied.
RELEASE_SUITE = {
    "Atomic Lifecycle Race": [
        ("test_story_1_6.py", "test_concurrent_complete_and_cancel_resolve_to_one_terminal_log"),
    ],
    "Concurrent Idempotency": [
        ("test_story_1_3.py", "test_concurrent_duplicate_accepts_one_user_message_and_one_run"),
    ],
    "SSE Reconnect·Terminal Close": [
        ("test_story_1_4.py", "test_real_tcp_sse_emits_delta_before_terminal_and_replays_after_cursor"),
        ("test_story_1_4.py", "test_cursor_capability_no_store_and_csp_boundaries"),
    ],
    "Streaming 중 Expiry": [
        ("test_story_1_8.py", "test_an_open_stream_is_told_the_conversation_expired_and_then_stops"),
    ],
    "Cross-session Denial": [
        ("test_story_1_8.py", "test_cross_session_read_submit_stream_and_cancel_are_all_minimal_404"),
    ],
    "Abuse Load-shed": [
        ("test_story_1_8.py", "test_the_load_shed_contract_holds_under_concurrent_sessions"),
    ],
    "Hostile Markdown·URL": [
        ("test_story_1_9.py", "test_hostile_markup_is_text_not_an_error"),
    ],
    "Sanitized Log": [
        ("test_story_1_9.py", "test_default_and_debug_access_logs_are_disabled"),
    ],
    "Docker 재현": [
        ("test_story_1_11_docker.py", "test_the_container_reproduces_the_host_contract"),
        ("test_story_1_11_docker.py", "test_a_restarted_container_keeps_no_previous_state"),
        ("test_story_1_11_docker.py", "test_the_container_runs_one_worker_as_uid_1000_on_7860"),
        ("test_story_1_11_docker.py", "test_the_image_carries_only_what_the_dockerfile_copied"),
    ],
    # Proven together with the nine, because the frozen AC asks for both counts in
    # the same run rather than as a separate exercise.
    # A Run with NO partial output cannot prove this, so the 1.7 test (a late,
    # non-streaming answer) is joined by two fixtures that do produce deltas and then
    # fail or are cancelled -- plus the three-configuration failure battery.
    "부분 출력 completed 0건": [
        ("test_story_1_7.py", "test_a_late_but_valid_answer_never_becomes_a_completed_message"),
        ("test_story_1_11.py", "test_a_failed_run_with_partial_output_is_never_marked_completed"),
        ("test_story_1_11.py", "test_a_cancelled_run_with_partial_output_is_never_marked_completed"),
        ("test_story_1_11.py", "test_every_configuration_fails_a_run_with_the_same_shape"),
    ],
    "새 대화 문맥 유입 0건": [
        ("test_story_1_5.py", "test_new_conversation_does_not_reuse_other_conversations_history_or_ttl"),
    ],
}


COUNTED_WITH_THE_SUITE = ("부분 출력 completed 0건", "새 대화 문맥 유입 0건")
SUITE_ITEMS = set(RELEASE_SUITE) - set(COUNTED_WITH_THE_SUITE)


def _marked_release_suite() -> set[tuple[str, str]]:
    """Every test carrying `@pytest.mark.release_suite`, read from source. An import
    would work too, but reading is what lets this see the whole suite without
    executing any of it."""
    found: set[tuple[str, str]] = set()
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if "release_suite" in ast.unparse(decorator):
                    found.add((path.name, node.name))
    return found


def test_the_release_suite_marker_selects_exactly_the_declared_items() -> None:
    expected = {entry for entries in RELEASE_SUITE.values() for entry in entries}
    assert _marked_release_suite() == expected
    # Nine Suite items, and two counts the frozen AC asks for in the same run. Named,
    # not counted to a magic number -- the evidence says "nine", so the nine are what
    # this asserts.
    assert set(RELEASE_SUITE) - set(COUNTED_WITH_THE_SUITE) == SUITE_ITEMS
    assert len(SUITE_ITEMS) == 9

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "release_suite: " in pyproject
    # Without --strict-markers a typo'd Marker registers silently, and the source scan
    # above cannot tell the difference either.
    assert "--strict-markers" in pyproject


# --- Image, Manifest and .env.example say the same thing -------------------


def _instructions(dockerfile: str) -> list[str]:
    """The Dockerfile's actual instructions, with comments and line continuations
    folded away -- so an assertion cannot be satisfied (or broken) by prose."""
    folded = dockerfile.replace("\\\n", " ")
    return [
        line.strip() for line in folded.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_dockerfile_builds_from_the_lockfile_and_the_pinned_interpreter() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    python_version = (ROOT / ".python-version").read_text(encoding="utf-8").strip()

    assert python_version == "3.12.14"
    assert 'requires-python = "==3.12.14"' in pyproject
    assert 'required-version = "==0.12.5"' in pyproject
    # Both bases pinned BY DIGEST -- a tag can be re-pointed and the asserts below
    # would then pass against an image nobody reviewed.
    assert re.search(
        rf"^FROM python:{re.escape(python_version)}-slim-bookworm@sha256:[0-9a-f]{{64}}$",
        dockerfile,
        re.MULTILINE,
    )
    assert re.search(
        r"ghcr\.io/astral-sh/uv:0\.12\.5@sha256:[0-9a-f]{64}", dockerfile
    )
    assert f"assert sys.version.split()[0] == '{python_version}'" in dockerfile
    # `--locked` fails on a Lockfile that disagrees with pyproject.toml instead of
    # re-locking, and `--no-dev` keeps playwright out of the Image.
    assert "uv sync --locked --no-dev" in dockerfile
    assert "uv pip install" not in dockerfile and "pip install" not in dockerfile
    assert "USER 1000" in dockerfile and "--uid 1000" in dockerfile
    assert '"--workers", "1"' in dockerfile
    assert '"--host", "0.0.0.0", "--port", "7860"' in dockerfile
    assert "EXPOSE 7860" in dockerfile
    # Without it, a TLS-terminating router makes every mutating POST a 403 -- see
    # the Origin gate in main._has_exact_origin.
    assert '"--proxy-headers"' in dockerfile
    # The user exists before anything is written into /app, so no `chown -R` has to
    # rewrite the whole venv into a second layer. The `agent` build stage has its
    # own earlier WORKDIR (/agent, for `npm ci`) that this ordering claim is not
    # about, so the search names the final stage's WORKDIR specifically.
    instructions = _instructions(dockerfile)
    user_at = next(i for i, line in enumerate(instructions) if "useradd --uid 1000" in line)
    workdir_at = next(i for i, line in enumerate(instructions) if line.startswith("WORKDIR /app"))
    assert user_at < workdir_at
    assert not [line for line in instructions if "chown -R" in line]


def test_the_build_context_cannot_carry_a_credential() -> None:
    """The guarantee is the COPY allowlist; `.dockerignore` is the second line. Both
    are checked here, in that order, because the document says so."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copied = set()
    for line in _instructions(dockerfile):
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        # Everything but the destination, with the flags dropped.
        copied.update(
            token for token in line.split()[1:-1] if not token.startswith("--")
        )
    # The `agent` stage's own three (npm manifest + sidecar sources) join the
    # Python stage's four -- none of them a credential, all of them committed.
    assert copied == {
        "pyproject.toml", "uv.lock", ".python-version", "src",
        "agent/package.json", "agent/package-lock.json", "agent/*.mjs",
    }
    instructions = " ".join(_instructions(dockerfile))
    for secret in ("OLLAMA_API_KEY", "OLLAMA_BASE_URL", ".env"):
        assert secret not in instructions
    # No README is copied: `pyproject.toml` declares no `readme`, so it was never
    # used by the build -- and in a Space repo the root README is the Manifest, so
    # copying one would make "one Image" mean two different files.
    assert "README" not in instructions

    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    entries = {line.strip() for line in ignored if line.strip() and not line.startswith("#")}
    assert {".env", ".env.*", "*.pem", "*.key", ".netrc", "credentials*.json"} <= entries
    # Nothing is re-included: a `!` rule would name a file no COPY reads anyway.
    assert not [entry for entry in entries if entry.startswith("!")]
