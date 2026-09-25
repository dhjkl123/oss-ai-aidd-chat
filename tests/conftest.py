from collections.abc import Iterator
from contextlib import suppress
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryFile
import time
from urllib.request import urlopen

import pytest

from aidd_chat.main import app


# Every DeploymentSettingsV1 field, pinned. Adding a ceiling to that model without
# adding it here makes the whole suite refuse to become Ready, which is the point.
DEPLOYMENT_SETTINGS = {
    "MAX_ATTEMPTS_PER_LINEAGE": "3",
    "MAX_RESIDENT_CONVERSATIONS": "500",
    "MAX_GLOBAL_ACTIVE_RUNS": "64",
    "MAX_SESSION_ACTIVE_RUNS": "2",
    "MAX_QUEUED_RUNS": "32",
    "MAX_TURNS_PER_CONVERSATION": "100",
    "MAX_OUTPUT_BYTES": "262144",
    "MAX_REPLAY_EVENTS": "20000",
    "MAX_REPLAY_BYTES": "1048576",
    "MAX_SSE_CONNECTIONS": "32",
    "CREATE_RATE_PER_MINUTE": "600",
    "SUBMIT_RATE_PER_MINUTE": "600",
    "POLL_RATE_PER_MINUTE": "6000",
    "RETRY_RATE_PER_MINUTE": "600",
    "CANCEL_RATE_PER_MINUTE": "600",
    "MAX_REQUESTS_PER_IP_PER_MINUTE": "20000",
    "MAX_PROVIDER_CONCURRENCY": "32",
    "SPEND_WINDOW_SECONDS": "3600",
    "SPEND_LIMIT_UNITS": "100000",
    "SPEND_UNIT": "provider_call",
    "TRUSTED_PROXY_HOPS": "0",
}


def _clear_global_store() -> None:
    """Reset the module-global application's Store completely. Clearing `states`
    alone leaves rate windows, SSE counts, run counters, spend and expiry tombstones
    to leak across tests, which makes any test that lowers a ceiling against the
    global app depend on what ran before it."""
    chat_application = getattr(app.state, "chat_application", None)
    loop = getattr(getattr(chat_application, "agent_loop", None), "loop", None)
    if loop is not None and loop.is_closed():
        # A TestClient's lifespan exit shut this application down, agent loop and
        # all; a process serves one lifespan, so the next one needs a fresh build.
        del app.state.chat_application
        return
    store = getattr(chat_application, "store", None)
    if store is None:
        return
    close = getattr(store, "close", None)
    if callable(close):
        close()
    for name in ("states", "_locks", "_run_conversations", "_rates", "_expired_runs", "_active_runs"):
        container = getattr(store, name, None)
        if hasattr(container, "clear"):
            container.clear()
    for name in ("_active_total", "_queued_total", "_reserved_units", "_sse_connections",
                 "_provider_calls", "_spend_units"):
        if hasattr(store, name):
            setattr(store, name, 0)
    if hasattr(store, "_spend_window_start"):
        store._spend_window_start = time.monotonic()


@pytest.fixture(autouse=True)
def isolate_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every ProviderSettings input so the suite binds the deterministic provider
    regardless of a developer's .env. An env var beats a dotenv entry in
    pydantic-settings, so explicit values here make CI and local runs identical and
    keep the tests off the real LAN endpoint. monkeypatch undoes them after every
    test; a test that wants a real binding sets its own values, which run after this
    fixture."""
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "local_test")
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    monkeypatch.setenv("OLLAMA_API_KEY", "")
    # Wiki agent (Task 15). Pinned empty so a developer's own .env WIKI_ROOT/
    # TOKENIZER_PATH cannot leak into the suite and bind a real PiSidecarAdapter
    # by accident. OLLAMA_MODEL and MODEL_CONTEXT_WINDOW are left unpinned: both
    # have working AgentSettings defaults, and MODEL_CONTEXT_WINDOW is an int
    # field -- an empty string would fail pydantic-settings parsing outright.
    monkeypatch.setenv("WIKI_ROOT", "")
    monkeypatch.setenv("TOKENIZER_PATH", "")
    monkeypatch.setenv("NODE_BINARY", "node")
    # DeploymentSettingsV1 has no default for any of these on purpose, so the suite
    # has to pin them exactly like the Provider inputs above. Generous values: the
    # ceilings themselves are proven in tests that lower them deliberately, and a
    # suite-wide ceiling that bites by accident would look like a flaky test.
    for name, value in DEPLOYMENT_SETTINGS.items():
        monkeypatch.setenv(name, value)
    # A test that DELETES one of those variables to prove it is required would
    # otherwise read the developer's .env instead and see it set.
    from aidd_chat import bootstrap

    for settings in (
        bootstrap.ProviderSettings,
        bootstrap.DeploymentSettingsV1,
        bootstrap.AgentSettings,
    ):
        monkeypatch.setitem(settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def isolate_silenced_loggers() -> Iterator[None]:
    """`main.silence_untrusted_loggers` sets `disabled = True` and clears handlers,
    permanently and by design -- there is no operator switch to turn request logging
    back on, because the switch would be the leak. Every test that enters
    `TestClient(app)` runs it, so without this the first such test in a session
    silences httpx/asyncio for the whole run and every later caplog assertion
    on them is vacuously true."""
    import logging

    from aidd_chat.main import _SILENCED_LOGGERS

    saved = {
        name: (
            logging.getLogger(name).disabled,
            logging.getLogger(name).propagate,
            list(logging.getLogger(name).handlers),
        )
        for name in _SILENCED_LOGGERS
    }
    try:
        yield
    finally:
        for name, (disabled, propagate, handlers) in saved.items():
            logger = logging.getLogger(name)
            logger.disabled, logger.propagate = disabled, propagate
            logger.handlers[:] = handlers


@pytest.fixture(autouse=True)
def isolate_global_store() -> Iterator[None]:
    _clear_global_store()
    yield
    _clear_global_store()


@pytest.fixture(scope="session")
def server_url():
    """One real uvicorn for the whole Playwright suite. Every browser test stubs its
    own API routes, so the process carries no per-test state and booting it once is
    the difference between one startup and a dozen. Its output goes to a temp file
    rather than DEVNULL so a server that dies at import says why instead of costing
    the full timeout and reporting nothing."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = Path(__file__).parents[1]
    with TemporaryFile() as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "aidd_chat.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"],
            cwd=root,
            # Pinned here as well as in isolate_provider_env: this fixture is
            # session-scoped, so it is set up before that function-scoped one ever
            # runs and would otherwise inherit a developer's .env.
            env={
                **os.environ,
                "PYTHONPATH": str(root / "src"),
                "DEPLOYMENT_PROFILE": "local_test",
                "OLLAMA_BASE_URL": "",
                "OLLAMA_API_KEY": "",
                **DEPLOYMENT_SETTINGS,
            },
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}"

        def output() -> str:
            log.seek(0)
            return log.read().decode("utf-8", "replace").strip()

        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(f"uvicorn exited {process.returncode}: {output()}")
                try:
                    with urlopen(f"{url}/live", timeout=0.2) as response:
                        if response.status == 204:
                            break
                except OSError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError(f"uvicorn startup timeout: {output()}")
            yield url
        finally:
            process.terminate()
            with suppress(subprocess.TimeoutExpired):
                process.wait(5)
            if process.poll() is None:
                process.kill()
                process.wait(5)
