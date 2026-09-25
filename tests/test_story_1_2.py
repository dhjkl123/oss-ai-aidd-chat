from contextlib import contextmanager, suppress
from dataclasses import asdict
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
from threading import Event, Thread
import time
import re
import sys
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from playwright.async_api import Error as PlaywrightError, async_playwright, expect

from aidd_chat.adapters import FakeAgent, InMemoryConversationStore
from aidd_chat.application import (
    ChatApplication,
    PolicyProjectionV1,
    READINESS_CACHE_SECONDS,
    build_policy_projection,
)
from aidd_chat.contracts import AgentProbeV1
from aidd_chat.main import app, get_chat_application


ROOT = Path(__file__).parents[1]


def as_probe(result: object) -> object:
    """A bool becomes the AgentProbeV1 it stands for; anything else (a truthy
    non-bool, say) is returned as-is so the Application must refuse it."""
    if isinstance(result, bool):
        return AgentProbeV1(provider_ok=result, wiki_ok=True, context_ok=True)
    return result


class CountingProvider(FakeAgent):
    def __init__(self, result: object = True) -> None:
        super().__init__()
        self.calls = 0
        self.result = result

    async def probe(self) -> object:
        self.calls += 1
        return as_probe(self.result)


class BlockingProvider(CountingProvider):
    def __init__(self) -> None:
        super().__init__(True)
        self.started = Event()
        self.release = Event()

    async def probe(self) -> object:
        self.calls += 1
        self.started.set()
        # A hung probe, as the story-era thread was: it runs off the agent loop and
        # does not stop when the Application's probe timeout cancels it, so its
        # result arrives late -- after `release` -- exactly like before.
        waiter = asyncio.ensure_future(asyncio.to_thread(self.release.wait))
        while not waiter.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(waiter)
        return as_probe(True)


@contextmanager
def route_client(application: ChatApplication):
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_chat_application, None)


def wait_for_probe_idle(application: ChatApplication) -> None:
    deadline = time.monotonic() + 1
    while application._probe_inflight is not None and time.monotonic() < deadline:
        time.sleep(0.001)
    assert application._probe_inflight is None


def test_projection_is_frozen_closed_and_canonical() -> None:
    policy = build_policy_projection(FakeAgent().policy_metadata)

    assert isinstance(policy, PolicyProjectionV1)
    assert policy.model_config["extra"] == "forbid"
    assert policy.model_config["frozen"] is True
    assert isinstance(policy.transmitted_fields, tuple)
    assert policy.session_ttl_seconds == 3_600
    assert policy.retrieval_status == "wiki_readonly"
    with pytest.raises((ValidationError, TypeError)):
        policy.provider_label = "변경"
    with pytest.raises(ValidationError):
        PolicyProjectionV1.model_validate({**policy.model_dump(), "extra": "금지"})


def test_projection_rejects_unsafe_or_incomplete_metadata() -> None:
    metadata = asdict(FakeAgent().policy_metadata)
    metadata["model_revision"] = "https://user:secret@example.test?api_key=x"
    with pytest.raises(ValueError):
        build_policy_projection(metadata)
    # A retired public-demo field is an unknown key now, and the contract is closed.
    for retired in ("endpoint_disclosure", "retention_summary", "subprocessors"):
        metadata = asdict(FakeAgent().policy_metadata)
        metadata[retired] = "local-test"
        with pytest.raises(ValueError):
            build_policy_projection(metadata)
    with pytest.raises(ValueError):
        build_policy_projection({"provider_label": "없는 나머지 필드"})

    for unsafe in ("unknown provider", "trace id", "raw response", "access token", "bad\x00value", "bad\u202evalue"):
        metadata = asdict(FakeAgent().policy_metadata)
        metadata["provider_label"] = unsafe
        with pytest.raises(ValueError):
            build_policy_projection(metadata)

    metadata = asdict(FakeAgent().policy_metadata)
    metadata["model_revision"] = "x" * 257
    with pytest.raises(ValueError):
        build_policy_projection(metadata)


def test_probe_uses_strict_two_second_timeout_and_success_cache() -> None:
    assert ChatApplication.PROBE_TIMEOUT_SECONDS == 2.0
    assert READINESS_CACHE_SECONDS == 30.0
    provider = CountingProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)

    assert application.is_ready() is True
    assert application.is_ready() is True
    assert provider.calls == 1

    application._last_success_at -= 31
    assert application.is_ready() is True
    assert provider.calls == 2


def test_readiness_reuses_at_29_999_seconds_but_reprobes_at_30(monkeypatch) -> None:
    import aidd_chat.application as application_module

    class Clock:
        now = 100.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    provider = CountingProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    monkeypatch.setattr(application_module.time, "monotonic", clock)
    assert application.is_ready() is True
    clock.now = 129.999
    assert application.is_ready() is True
    assert provider.calls == 1
    clock.now = 130.0
    assert application.is_ready() is True
    assert provider.calls == 2


def test_invalid_projection_clears_previous_success_cache() -> None:
    application = ChatApplication(InMemoryConversationStore(), CountingProvider(), 3)
    assert application.is_ready() is True
    application._policy_projection = PolicyProjectionV1.model_construct(schema_version="1")

    assert application.is_ready() is False
    assert application._last_success_at is None


def test_failure_is_not_cached_and_truthy_non_bool_is_unhealthy() -> None:
    provider = CountingProvider(False)
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    assert application.is_ready() is False
    assert application.is_ready() is False
    assert provider.calls == 2

    provider = CountingProvider(1)
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    assert application.is_ready() is False


def test_probe_exception_returns_503_clears_inflight_and_retries() -> None:
    class RaisingThenHealthyProvider(CountingProvider):
        async def probe(self) -> object:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider unavailable")
            return as_probe(True)

    provider = RaisingThenHealthyProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            first = client.get("/ready")
            assert application._probe_inflight is None
            second = client.get("/ready")
    finally:
        app.dependency_overrides.pop(get_chat_application, None)

    assert first.status_code == 503 and first.content == b""
    assert first.headers["cache-control"] == "no-store"
    assert second.status_code == 204 and second.content == b""
    assert provider.calls == 2


def test_timeout_has_one_inflight_probe_and_late_result_is_not_cached() -> None:
    provider = BlockingProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    application.PROBE_TIMEOUT_SECONDS = 0.05
    first_result: list[bool] = []

    first = Thread(target=lambda: first_result.append(application.is_ready()))
    first.start()
    assert provider.started.wait(1)
    second_result: list[bool] = []
    second = Thread(target=lambda: second_result.append(application.is_ready()))
    second.start()
    second.join(1)
    assert second_result == [False]
    first.join(1)
    assert first_result == [False]
    assert provider.calls == 1
    first.join(1)
    assert first_result == [False]
    assert provider.calls == 1

    time.sleep(0.06)
    provider.release.set()
    first.join(1)
    deadline = time.monotonic() + 1
    while application._probe_inflight is not None and time.monotonic() < deadline:
        time.sleep(0.001)
    assert application.is_ready() is True
    assert provider.calls == 2


def test_provider_replacement_during_probe_invalidates_bootstrap_projection() -> None:
    original = BlockingProvider()
    application = ChatApplication(InMemoryConversationStore(), original, 3)
    application.PROBE_TIMEOUT_SECONDS = 0.05
    first_result: list[bool] = []

    first = Thread(target=lambda: first_result.append(application.is_ready()))
    first.start()
    assert original.started.wait(1)
    replacement = CountingProvider()
    with pytest.raises(AttributeError):
        application.provider = replacement
    time.sleep(0.06)
    application._provider_binding = replacement
    original.release.set()
    first.join(1)
    deadline = time.monotonic() + 1
    while application._probe_inflight is not None and time.monotonic() < deadline:
        time.sleep(0.001)

    assert first_result == [False]
    assert application.provider is replacement
    assert application.policy_projection is None
    assert replacement.calls == 0


def test_missing_projection_fails_closed_without_a_partial_response() -> None:
    application = ChatApplication(InMemoryConversationStore(), FakeAgent(), 3)
    with pytest.raises(AttributeError):
        application.policy = None
    application._policy_projection = None

    assert application.is_ready() is False
    assert application.get_policy_if_ready() is None


def test_routes_are_status_only_and_policy_is_no_store() -> None:
    application = ChatApplication(InMemoryConversationStore(), FakeAgent(), 3)
    with route_client(application) as client:
        ready = client.get("/ready")
        policy = client.get("/api/v1/policy")

    assert ready.status_code == 204
    assert ready.content == b""
    assert ready.headers["cache-control"] == "no-store"
    assert policy.status_code == 200
    assert policy.headers["cache-control"] == "no-store"
    assert policy.json()["retrieval_status"] == "wiki_readonly"
    assert policy.json()["session_ttl_seconds"] == 3_600
    # Pinned values, not just shape: the deterministic binding is what CI and the
    # browser suite exercise, so a binding edit must not silently retarget it.
    assert policy.json()["provider_label"] == "Deterministic local test provider"
    assert policy.json()["model_revision"] == "deterministic-v1"


def test_policy_route_is_empty_503_when_projection_becomes_stale() -> None:
    application = ChatApplication(InMemoryConversationStore(), FakeAgent(), 3)
    previous = application._policy_projection
    with route_client(application) as client:
        application._policy_projection = None
        try:
            ready = client.get("/ready")
            policy = client.get("/api/v1/policy")
        finally:
            application._policy_projection = previous

    assert ready.status_code == 503 and ready.content == b""
    assert policy.status_code == 503 and policy.content == b""
    assert ready.headers["cache-control"] == "no-store"
    assert policy.headers["cache-control"] == "no-store"


def assert_empty_policy_503(response) -> None:
    assert response.status_code == 503
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"


def test_policy_route_fails_closed_for_unhealthy_provider() -> None:
    application = ChatApplication(InMemoryConversationStore(), CountingProvider(False), 3)
    with route_client(application) as client:
        response = client.get("/api/v1/policy")
    assert_empty_policy_503(response)


def test_policy_route_fails_closed_for_provider_error() -> None:
    class ErrorProvider(CountingProvider):
        async def probe(self) -> object:
            self.calls += 1
            raise RuntimeError("provider error")

    application = ChatApplication(InMemoryConversationStore(), ErrorProvider(), 3)
    with route_client(application) as client:
        response = client.get("/api/v1/policy")
    assert_empty_policy_503(response)


def test_policy_route_fails_closed_for_provider_timeout() -> None:
    provider = BlockingProvider()
    application = ChatApplication(InMemoryConversationStore(), provider, 3)
    application.PROBE_TIMEOUT_SECONDS = 0.05
    with route_client(application) as client:
        response = client.get("/api/v1/policy")
        assert_empty_policy_503(response)
    provider.release.set()
    wait_for_probe_idle(application)


def test_policy_ui_has_runtime_loading_retry_and_knowledge_boundary_contract() -> None:
    from pathlib import Path

    root = Path(__file__).parents[1] / "src" / "aidd_chat" / "web"
    html = (root / "index.html").read_text(encoding="utf-8")
    script = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")

    assert "정보·정책" in html
    assert "현재 대화" in html and "새 대화" in html
    assert "다시 확인" in html
    assert "<noscript>" in html and "JavaScript를 사용할 수 없어" in html
    assert 'data-policy-impact>' in html
    assert "개인정보" in html and "회사 기밀" in html and "Credential" in html
    assert "영구 저장되지 않습니다" in html
    assert "연결된 Wiki" in html and "Wiki 보충 대상" in html
    # Exactly the PolicyProjectionV1 fields (AD-11): a row per field, and no row for
    # a retired public-demo field.
    assert sorted(re.findall(r'data-policy-field="([a-z_]+)"', html)) == sorted((
        "schema_version", "provider_label", "model_revision", "transmitted_fields",
        "session_ttl_seconds", "retrieval_status", "wiki_display_name",
    ))
    assert "미확정" not in html
    assert "AbortController" in script and "requestGeneration" in script
    assert "프로세스 내 처리" not in script
    assert "textContent" in script and "innerHTML" not in script
    assert "/ready" in script and "/api/v1/policy" in script
    assert "ready.status !== 204" in script
    assert 'policyRetry.addEventListener("click", loadPolicy);' in script
    assert re.search(
        r"policyReady = false;.*?clearPolicyFields\(\);.*?updateComposer\(\);.*?"
        r"policyImpact\.hidden = false;.*?질문 기능을 사용할 수 없습니다",
        script,
        re.DOTALL,
    )
    assert re.search(
        r"const policy = await response\.json\(\);.*?"
        r"!isExactPolicy\(policy\).*?catch \{.*?showPolicyUnavailable\(\)",
        script,
        re.DOTALL,
    )
    assert re.search(
        r"const currentGeneration = \+\+requestGeneration;.*?"
        r"fetchPolicyResource\(\"/ready\", currentGeneration\).*?"
        r"currentGeneration !== requestGeneration",
        script,
        re.DOTALL,
    )
    assert "min-width: 320px" in css and ":focus-visible" in css


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_server(url: str, process: subprocess.Popen[bytes]) -> None:
    for _ in range(100):
        if process.poll() is not None:
            raise AssertionError("uvicorn이 시작되기 전에 종료되었습니다")
        try:
            with urlopen(f"{url}/live", timeout=0.2) as response:
                if response.status == 204:
                    return
        except OSError:
            pass
    raise AssertionError("uvicorn startup timeout")


@pytest.fixture()
def policy_server_url():
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "aidd_chat.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "error",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(url, process)
        yield url
    finally:
        process.terminate()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


async def _exercise_policy_browser(url: str) -> None:
    valid_policy = {
        "schema_version": "1",
        "provider_label": "Deterministic local test provider",
        "model_revision": "deterministic-v1",
        "transmitted_fields": [
            "system_instruction", "current_message", "selected_prior_messages", "wiki_excerpts",
        ],
        "session_ttl_seconds": 3_600,
        "retrieval_status": "wiki_readonly",
        "wiki_display_name": "llm-wiki",
    }
    mode = "loading"
    ready_requests = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 320, "height": 800})
        page = await context.new_page()

        async def ready_route(route):
            nonlocal ready_requests
            ready_requests += 1
            if mode == "loading" and ready_requests == 1:
                await asyncio.sleep(0.3)
                with suppress(PlaywrightError):
                    await route.fulfill(status=503, body="")
                return
            if mode == "stale" and ready_requests == 1:
                await asyncio.sleep(0.5)
                with suppress(PlaywrightError):
                    await route.fulfill(status=204, body="")
                return
            await route.fulfill(status=204, body="")

        async def policy_route(route):
            body = valid_policy
            if mode == "invalid":
                # A retired public-demo key: the browser's exact key set refuses it.
                body = {**valid_policy, "endpoint_disclosure": "local-test"}
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(body, ensure_ascii=False),
            )

        await page.route("**/ready", ready_route)
        await page.route("**/api/v1/policy", policy_route)
        await page.goto(url, wait_until="domcontentloaded")

        status = page.locator("#policy-status")
        impact = page.locator("[data-policy-impact]")
        retry = page.locator("[data-policy-retry]")
        success = page.locator("[data-policy-success]")
        prompt = page.locator("#prompt")

        await expect(status).to_contain_text("확인하는 중입니다", timeout=1000)
        assert await prompt.is_enabled() is False
        assert await impact.is_visible()
        assert await page.evaluate(
            "Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) <= window.innerWidth"
        )

        await expect(status).to_contain_text("확인할 수 없습니다", timeout=2000)
        assert await retry.is_visible()
        assert await success.is_visible() is False

        mode = "success"
        await retry.click()
        # Task 24: the policy detail is its own screen (EXPERIENCE IA), reached from the
        # navigation; the readiness status, impact and 다시 확인 above stay on both.
        await page.evaluate("location.hash = 'policy-panel'")
        await expect(success).to_be_visible(timeout=2000)
        assert await retry.is_visible() is False
        assert await page.locator('[data-policy-field="provider_label"]').inner_text() == "Deterministic local test provider"
        assert await page.locator('[data-policy-field="wiki_display_name"]').inner_text() == "llm-wiki"
        assert await impact.is_visible() is False

        mode = "invalid"
        await page.evaluate("void loadPolicy()")
        await expect(status).to_contain_text("확인할 수 없습니다", timeout=2000)
        assert await success.is_visible() is False
        assert await page.locator('[data-policy-field="provider_label"]').inner_text() == ""
        assert await retry.is_visible()

        mode = "stale"
        ready_requests = 0
        await page.evaluate("void loadPolicy()")
        await page.evaluate("void loadPolicy()")
        await expect(success).to_be_visible(timeout=2000)
        await page.wait_for_timeout(600)
        assert await status.text_content() == "서비스 준비 상태와 공개 정책을 확인했습니다."
        assert await page.locator('[data-policy-field="model_revision"]').inner_text() == "deterministic-v1"

        await browser.close()


def test_policy_browser_state_machine(policy_server_url):
    asyncio.run(_exercise_policy_browser(policy_server_url))
