"""Story 1.5.1 -- real Provider binding selection and credential containment.

The PydanticAI Ollama adapter this story introduced is retired (Task 22); what is
left here is the bootstrap half -- which binding a configuration selects, and that
the credential never surfaces -- now proved against the wiki agent binding.
"""

from contextlib import contextmanager
from dataclasses import replace
import pathlib

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError


from aidd_chat.adapters import DETERMINISTIC_BINDING, InMemoryConversationStore
from aidd_chat.adapters.fake_agent import local_test_binding_digest
from aidd_chat.adapters.pi_sidecar import PiSidecarAdapter
from aidd_chat.application import ChatApplication
from aidd_chat.bootstrap import (
    BindingConfigurationError,
    ProviderSettings,
    UnboundProvider,
    build_chat_application,
    build_provider,
    endpoint_origin,
)
from aidd_chat.contracts import format_endpoint_origin
from aidd_chat.main import app, get_chat_application
from aidd_chat.adapters import FakeAgent


# A credential-shaped value that must never surface anywhere. Not a real key.
API_KEY = "sk-story151-must-never-appear"
# A private-range origin -- a documentation address, not any deployment's. Nothing
# here connects to it.
BASE_URL = "http://192.168.0.10:11435"
ORIGIN = {"Origin": "https://testserver"}
# local_test with a real endpoint binds PiSidecarAdapter -- these fixtures let the
# bootstrap tests below build a full AgentBindingV1.
WIKI = pathlib.Path(__file__).parents[1] / "agent" / "test-fixtures" / "wiki"
TOKENIZER = pathlib.Path(__file__).parents[1] / "tests" / "fixtures" / "tiny-tokenizer.json"


@contextmanager
def route_client(application: ChatApplication):
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_chat_application, None)


def _run_one_question(application: ChatApplication, content: str = "한국어로 답해 주세요"):
    conversation, capability = application.create_conversation()
    accepted = application.submit_question(
        conversation.conversation_id, capability, "story-1-5-1", content
    )
    application.wait_for_generations(5)
    return application.get_run_snapshot(accepted.run_id, capability) + (capability,)


# --- Endpoint origin --------------------------------------------------------


@pytest.mark.parametrize(
    "junk",
    [
        "http://user:secret@192.168.0.10:11435",
        "http://192.168.0.10:11435/v1",
        "http://192.168.0.10:11435?q=1",
        "http://a..b:11435",
        "http://host.:11435",
        "http://a_b:11435",
        "http://-lead:11435",
        "file://192.168.0.10",
        "http://",
    ],
)
def test_binding_rejects_an_endpoint_that_is_not_a_bare_origin(junk) -> None:
    with pytest.raises(ValueError):
        format_endpoint_origin(junk)


def test_binding_canonicalizes_ipv6() -> None:
    assert format_endpoint_origin("http://[FD00::1]:11435") == "http://[fd00::1]:11435"


# --- Happy path ------------------------------------------------------------


def test_deterministic_binding_still_records_its_own_digest() -> None:
    store = InMemoryConversationStore()
    application = ChatApplication(store, FakeAgent(), 3)

    projection, _events, _capability = _run_one_question(application)

    run = store.states[projection.conversation_id].runs[projection.run_id]
    assert run.provider_binding_digest == local_test_binding_digest(DETERMINISTIC_BINDING)


# --- Failure matrix --------------------------------------------------------


# --- Readiness -------------------------------------------------------------


# --- Bootstrap -------------------------------------------------------------


def test_bootstrap_binds_deterministic_without_a_configured_endpoint() -> None:
    provider = build_provider(ProviderSettings(ollama_base_url="", ollama_api_key=SecretStr("")))

    assert isinstance(provider, FakeAgent)
    assert provider.binding == DETERMINISTIC_BINDING


def test_only_local_test_is_a_profile() -> None:
    # public_demo is retired (PRD C-7.3): like any unknown profile it is refused
    # where the value enters the process, so it can never reach build_provider and
    # serve the deterministic stub behind a green /ready.
    for retired in ("public_demo", "staging"):
        with pytest.raises(ValidationError):
            ProviderSettings(deployment_profile=retired)


def test_missing_credential_still_serves_503_instead_of_refusing_to_start() -> None:
    # Frozen matrix row "Credential 부재": /ready 503 and /policy 503, which a
    # startup exception could never produce because nothing would be listening.
    for blank in ("", "   "):
        provider = build_provider(
            ProviderSettings(ollama_base_url=BASE_URL, ollama_api_key=SecretStr(blank))
        )
        assert isinstance(provider, UnboundProvider)

    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    assert application.is_ready() is False
    with route_client(application) as client:
        assert client.get("/ready").status_code == 503
        created = client.post("/api/v1/conversations", headers=ORIGIN)
        submitted = client.post(
            f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
            headers={**ORIGIN, "Idempotency-Key": "unbound"},
            json={"kind": "question", "content": "질문"},
        )
        policy = client.get("/api/v1/policy")

    assert policy.status_code == 503 and policy.content == b""
    assert submitted.status_code == 503


def test_bootstrap_binds_pi_sidecar_when_endpoint_and_credential_are_present(monkeypatch) -> None:
    # local_test with a real endpoint binds the wiki agent (PiSidecarAdapter); the
    # credential/endpoint non-disclosure shape this story proved applies to it.
    monkeypatch.setenv("WIKI_ROOT", str(WIKI))
    monkeypatch.setenv("TOKENIZER_PATH", str(TOKENIZER))
    provider = build_provider(
        ProviderSettings(ollama_base_url=f"{BASE_URL}/v1", ollama_api_key=SecretStr(API_KEY))
    )

    assert isinstance(provider, PiSidecarAdapter)
    assert provider.binding.endpoint_origin == BASE_URL
    assert provider._api_key == API_KEY
    assert API_KEY not in provider._binding_json


def test_environment_variables_actually_select_the_binding(monkeypatch) -> None:
    # Every other bootstrap test passes kwargs, which would keep passing even if the
    # env var names drifted -- and a real deployment would then silently serve the
    # deterministic stub with a green /ready.
    monkeypatch.setenv("OLLAMA_BASE_URL", f"{BASE_URL}/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", API_KEY)
    monkeypatch.setenv("WIKI_ROOT", str(WIKI))
    monkeypatch.setenv("TOKENIZER_PATH", str(TOKENIZER))

    provider = build_provider()
    assert isinstance(provider, PiSidecarAdapter)
    assert provider.binding.endpoint_origin == BASE_URL
    assert provider._api_key == API_KEY
    assert isinstance(build_chat_application().provider, PiSidecarAdapter)


def test_environment_with_only_a_base_url_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", BASE_URL)
    monkeypatch.setenv("OLLAMA_API_KEY", "")

    assert isinstance(build_provider(), UnboundProvider)
    assert build_chat_application().is_ready() is False


def test_credential_is_a_secret_that_does_not_print(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", BASE_URL)
    monkeypatch.setenv("OLLAMA_API_KEY", API_KEY)
    settings = ProviderSettings()

    assert API_KEY not in repr(settings)
    assert API_KEY not in str(settings.ollama_api_key)
    assert settings.ollama_api_key.get_secret_value() == API_KEY


def test_endpoint_origin_rejects_userinfo_and_junk_but_drops_the_path() -> None:
    # Refused, not stripped: silently dropping the credential would hand the operator
    # an unauthenticated binding with no error at all.
    with pytest.raises(BindingConfigurationError) as caught:
        endpoint_origin(f"http://user:{API_KEY}@192.168.0.10:11435/v1")
    assert "OLLAMA_API_KEY" in str(caught.value)
    assert API_KEY not in str(caught.value)
    assert endpoint_origin("192.168.0.10:11435") == BASE_URL
    assert endpoint_origin("https://ollama.example.com/v1") == "https://ollama.example.com"
    assert endpoint_origin("http://[fd00::1]:11435/v1") == "http://[fd00::1]:11435"
    for junk in ("not a url", "file:///etc/passwd", "http://a..b/v1", "http://a_b", "http://host./v1", ""):
        with pytest.raises(BindingConfigurationError):
            endpoint_origin(junk)


# --- Tokenizer authority ---------------------------------------------------


def test_tokenizer_authority_always_matches_the_recorded_binding() -> None:
    provider = FakeAgent()
    assert provider.tokenizer_authority == (
        provider.binding.tokenizer_authority_name,
        provider.binding.tokenizer_authority_version,
        provider.binding.max_input_tokens,
    )
    assert provider.max_input_tokens == provider.binding.max_input_tokens
    assert provider.binding_digest == local_test_binding_digest(provider.binding)

    # A binding swap must move the Authority with it, or the digest recorded on a
    # Run would describe a window the process is not using.
    provider.binding = replace(provider.binding, max_input_tokens=50_000)
    assert provider.tokenizer_authority[2] == 50_000
    assert provider.max_input_tokens == 50_000


# --- Binding parity --------------------------------------------------------


# The sidecar binding's own contract tests cover the pi adapter (Task 14).
def test_the_binding_passes_the_api_sse_policy_and_readiness_contract() -> None:
    provider = FakeAgent()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)

    with route_client(application) as client:
        assert client.get("/ready").status_code == 204
        policy = client.get("/api/v1/policy")
        created = client.post("/api/v1/conversations", headers=ORIGIN)
        assert created.status_code == 201
        submitted = client.post(
            f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
            headers={**ORIGIN, "Idempotency-Key": "parity"},
            json={"kind": "question", "content": "질문"},
        )
        assert submitted.status_code == 202
        application.wait_for_generations(5)
        with client.stream(
            "GET",
            f"/api/v1/runs/{submitted.json()['run_id']}/events",
            headers={"Last-Event-ID": "0"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            types = [line[len("event: ") :] for line in response.iter_lines() if line.startswith("event: ")]
        polled = client.get(f"/api/v1/runs/{submitted.json()['run_id']}")

    assert policy.status_code == 200
    assert set(policy.json()) == {
        "schema_version",
        "provider_label",
        "model_revision",
        "transmitted_fields",
        "session_ttl_seconds",
        "retrieval_status",
        "wiki_display_name",
    }
    assert types[0] == "run.status"
    assert types[-4:] == ["message.sources", "message.completed", "run.status", "stream.end"]
    assert polled.json()["state"] == "completed"
    assert set(polled.json()) == {
        "schema_version",
        "run_id",
        "conversation_id",
        "input_message_id",
        "retry_of_run_id",
        "output_message_id",
        "output_message",
        "state",
        "stage",
        "created_at",
        "last_updated_at",
        "latest_sequence",
        "terminal_error",
    }
