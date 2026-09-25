"""Story 1.11 -- Docker reproduction.

The Container is the ninth Release Suite item and the only one that cannot be
proven in-process: "the same app runs from the Lockfile in an Image" is a claim
about a build, a user, a port and a process tree.

The whole file skips -- as a Skip, reported as a Skip -- when there is no Docker to
build with, and that includes a Docker that is present but not answering: a hung
daemon must not error the module, because the file's own contract is that a missing
Docker is a Skip. Passing here without Docker would be the failure mode the story
forbids.

Its control group is `server_url`: the same code, the same deterministic binding,
the same deployment settings, run by uvicorn on the host. Every observation is made
twice and compared, so "the Container passes the same contract" is a comparison
rather than a second set of assertions that could quietly be weaker.

**What is NOT exercised here, and why.** The Container carries the deterministic
binding, whose answers are instant, so there is no way to stop a Run that is still
running without racing it -- and a racing assertion proves nothing on the run where
it loses. There is likewise no failing Provider inside the Image, so no Retry is ever
accepted here. What IS exercised is 질문 → 완료 → (terminal) 중지 → (거부된) 복구:
`POST …/cancel` on the completed Run, which must answer `already_terminal` and leave
the answer intact, and a Retry of it, which must be refused `retry_not_allowed`. The
accepted-Cancel and accepted-Retry paths are proven in-process, against Providers
that can be made to hang and fail -- see `tests/test_story_1_11.py` and Stories
1.6/1.7. Slowing the Image down to make them reachable would mean shipping test-only
behaviour in the artefact this story exists to verify.
"""

from contextlib import suppress
import json
import shutil
import socket
import subprocess
import time
from uuid import uuid4

import httpx
import pytest

from conftest import DEPLOYMENT_SETTINGS


IMAGE = "aidd-chat:test-1-11"
# One Container per test, on its own name and port. A shared one would make the
# restart test's side effect part of every later test's setup, and the file would
# then work only for as long as that test happens to run last.
CONTAINER_PREFIX = "aidd-chat-test-1-11"
# The Container carries the deterministic binding and nothing else. No .env, no
# credential, no endpoint: a Provider secret must never enter an Image or a test.
CONTAINER_ENV = {
    "DEPLOYMENT_PROFILE": "local_test",
    "OLLAMA_BASE_URL": "",
    "OLLAMA_API_KEY": "",
    **DEPLOYMENT_SETTINGS,
}


class _DockerUnavailable(Exception):
    """The daemon did not answer in time. A Skip, never an error -- see the module
    docstring."""


def _docker(*arguments: str, timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["docker", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise _DockerUnavailable(f"docker {arguments[0]} timed out") from exc


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def image(pytestconfig) -> str:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI가 없어 Container 재현을 실행하지 않았습니다")
    try:
        if _docker("info", timeout=60).returncode != 0:
            pytest.skip("docker daemon이 응답하지 않아 Container 재현을 실행하지 않았습니다")
        built = _docker("build", "--pull", "-t", IMAGE, str(pytestconfig.rootpath))
    except _DockerUnavailable as exc:
        pytest.skip(f"docker가 응답하지 않아 Container 재현을 실행하지 않았습니다: {exc}")
    assert built.returncode == 0, built.stderr[-4000:]
    return IMAGE


@pytest.fixture()
def container(image: str, request):
    name = f"{CONTAINER_PREFIX}-{request.node.name[:40]}"
    port = _free_port()
    try:
        _docker("rm", "-f", name, timeout=60)
        arguments = ["run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:7860"]
        for key, value in CONTAINER_ENV.items():
            arguments += ["-e", f"{key}={value}"]
        started = _docker(*arguments, image, timeout=120)
        assert started.returncode == 0, started.stderr
        try:
            _await_live(f"http://127.0.0.1:{port}")
            yield name, f"http://127.0.0.1:{port}"
        finally:
            logs = _docker("logs", name, timeout=60)
            print(logs.stdout[-4000:], logs.stderr[-4000:])
            _docker("rm", "-f", name, timeout=60)
    except _DockerUnavailable as exc:
        pytest.skip(f"docker가 응답하지 않아 Container 재현을 실행하지 않았습니다: {exc}")


def _await_live(base_url: str, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with suppress(httpx.HTTPError):
            if httpx.get(f"{base_url}/live", timeout=1).status_code == 204:
                return
        time.sleep(0.2)
    raise AssertionError(f"{base_url}/live never answered 204")


def _capability(response: httpx.Response) -> str:
    """Read from the header rather than a cookie jar: the cookie is `Secure`, and a
    jar over plain http would drop it -- which would test the jar, not the app."""
    header = response.headers["set-cookie"]
    return header.split("conversation_capability=", 1)[1].split(";", 1)[0]


def _json(response: httpx.Response, *expected: int) -> dict:
    """Body, only after the status says there is one. A bare `.json()` on an
    unexpected status reports a JSONDecodeError and hides the status that caused it."""
    assert response.status_code in expected, (
        response.status_code, response.text[:400], str(response.request.url)
    )
    return response.json()


def _observe(base_url: str) -> dict:
    """Everything about this deployment a client can see, with the values that are
    unique per Run (IDs, timestamps, Correlation IDs) left out. Two deployments of
    the same Image and the same binding must produce the identical dict."""
    origin = {"Origin": base_url}
    seen: dict = {}
    with httpx.Client(base_url=base_url, timeout=10) as client:
        shell = client.get("/")
        seen["live"] = client.get("/live").status_code
        seen["ready"] = client.get("/ready").status_code
        seen["shell"] = (shell.status_code, shell.headers["content-type"].split(";")[0])
        seen["csp"] = shell.headers["content-security-policy"]
        seen["nosniff"] = shell.headers["x-content-type-options"]
        seen["referrer"] = shell.headers["referrer-policy"]

        document = _json(client.get("/openapi.json"), 200)
        seen["openapi_info"] = (document["info"]["title"], document["info"]["version"])
        seen["openapi_paths"] = sorted(document["paths"])
        seen["openapi_schemas"] = sorted(document["components"]["schemas"])
        seen["openapi_operations"] = sorted(
            f"{method} {path} {sorted(operation['responses'])}"
            for path, item in document["paths"].items()
            for method, operation in item.items()
        )

        policy = client.get("/api/v1/policy")
        body = _json(policy, 200)
        seen["policy"] = (
            policy.status_code,
            body["provider_label"],
            body["model_revision"],
            body["retrieval_status"],
            body["session_ttl_seconds"],
        )

        created = client.post("/api/v1/conversations", headers=origin)
        conversation_id = _json(created, 201)["conversation_id"]
        capability = _capability(created)
        cookie = {"Cookie": f"conversation_capability={capability}"}
        seen["create"] = created.status_code
        seen["cookie_flags"] = tuple(
            flag
            for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/api/v1")
            if flag.casefold() in created.headers["set-cookie"].casefold()
        )
        # A create without an Origin is refused before anything exists.
        no_origin = client.post("/api/v1/conversations")
        seen["no_origin"] = (no_origin.status_code, _json(no_origin, 403)["error"]["code"])

        accepted = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={**origin, **cookie, "Idempotency-Key": "docker"},
            json={"kind": "question", "content": "컨테이너에서 한국어로 답해 주세요"},
        )
        run_id = _json(accepted, 202)["run_id"]
        seen["submit"] = accepted.status_code

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            polled = client.get(f"/api/v1/runs/{run_id}", headers=cookie)
            body = _json(polled, 200)
            if body["state"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        seen["run"] = (
            polled.status_code,
            body["state"],
            body["stage"],
            body["output_message"] is not None,
            body["latest_sequence"],
        )
        # The deterministic binding echoes the question, so the answer itself is a
        # parity signal rather than just its presence.
        seen["answer"] = body["output_message"]["content"]
        seen["poll_no_store"] = polled.headers["cache-control"]

        stream = client.get(f"/api/v1/runs/{run_id}/events", headers=cookie)
        assert stream.status_code == 200, stream.text[:400]
        seen["sse_status"] = (stream.status_code, stream.headers["content-type"].split(";")[0])
        types = [
            line.removeprefix("event: ")
            for line in stream.text.splitlines()
            if line.startswith("event: ")
        ]
        # Collapsed exactly as the host's `_event_types` (test_story_1_11.py) does:
        # how many Deltas or Steps a Provider/Agent emits is its own business, the
        # ORDER around them is the shared contract, and the two sides must compare
        # the same shape or a Provider-specific chunk count would fail this parity
        # check for a reason that has nothing to do with the Container.
        collapsed: list[str] = []
        for event_type in types:
            if event_type in ("message.delta", "agent.step") and collapsed[-1:] == [event_type]:
                continue
            collapsed.append(event_type)
        seen["sse_events"] = collapsed
        exhausted = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={**cookie, "Last-Event-ID": str(body["latest_sequence"])},
        )
        seen["sse_terminal_close"] = (exhausted.status_code, exhausted.content)
        bad_cursor = client.get(
            f"/api/v1/runs/{run_id}/events", headers={**cookie, "Last-Event-ID": "nope"}
        )
        seen["sse_bad_cursor"] = (
            bad_cursor.status_code, _json(bad_cursor, 400)["error"]["code"]
        )

        # 중지, on a Run that has already reached its terminal: the only Cancel this
        # Image can be made to take deterministically (see the module docstring).
        cancelled = client.post(
            f"/api/v1/runs/{run_id}/cancel", headers={**origin, **cookie}
        )
        cancel_body = _json(cancelled, 200)
        seen["cancel_terminal"] = (cancelled.status_code, cancel_body["cancel_outcome"])
        seen["cancel_keeps_the_answer"] = (
            cancel_body["run"]["output_message"]["content"] == seen["answer"]
        )
        # 복구, refused: a completed Run is not a Retry target, whatever the binding.
        retried = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={**origin, **cookie, "Idempotency-Key": "docker-retry"},
            json={"kind": "retry", "retry_of_run_id": run_id},
        )
        retry_error = _json(retried, 409)["error"]
        seen["retry_completed"] = (retried.status_code, retry_error["code"], retry_error["retryable"])

        unknown = client.get(f"/api/v1/runs/{uuid4()}", headers=cookie)
        seen["unknown_run"] = (unknown.status_code, unknown.content)
        uncredentialed = client.get(f"/api/v1/runs/{run_id}")
        seen["no_capability"] = (uncredentialed.status_code, uncredentialed.content)
    return seen


@pytest.mark.release_suite
def test_the_container_reproduces_the_host_contract(container, server_url: str) -> None:
    """질문 → 완료 → (terminal) 중지 → (거부된) 복구, twice: once in the Container,
    once against the host uvicorn the rest of the suite uses. Compared as one dict, so
    a contract that differs anywhere fails here with the differing key named."""
    _name, base_url = container
    from_container = _observe(base_url)
    from_host = _observe(server_url)

    assert from_container.keys() == from_host.keys()
    differences = {
        key: (from_container[key], from_host[key])
        for key in from_host
        if from_container[key] != from_host[key]
    }
    assert differences == {}, json.dumps(differences, ensure_ascii=False, default=str)

    # Not only "equal to the host" -- the values themselves are the contract.
    assert from_container["live"] == 204 and from_container["ready"] == 204
    assert from_container["run"][1:4] == ("completed", "terminal", True)
    assert from_container["sse_events"] == [
        "run.status", "agent.step", "message.delta", "message.sources", "message.completed",
        "run.status", "stream.end",
    ]
    assert from_container["sse_terminal_close"] == (204, b"")
    assert from_container["cancel_terminal"] == (200, "already_terminal")
    assert from_container["cancel_keeps_the_answer"] is True
    assert from_container["retry_completed"] == (409, "retry_not_allowed", False)
    assert from_container["unknown_run"] == (404, b"")
    assert "HttpOnly" in from_container["cookie_flags"]
    assert "Secure" in from_container["cookie_flags"]


@pytest.mark.release_suite
def test_the_container_runs_one_worker_as_uid_1000_on_7860(container) -> None:
    """Worker 1, Process 1 (PID 1), UID 1000, 0.0.0.0:7860, Python 3.12.14 -- read
    from inside the Container rather than from the Dockerfile that asked for them."""
    name, _base_url = container
    probe = _docker(
        "exec",
        name,
        "python",
        "-c",
        "import json,os,sys\n"
        "procs={}\n"
        "for p in os.listdir('/proc'):\n"
        "    if not p.isdigit(): continue\n"
        "    try: procs[int(p)]=open('/proc/'+p+'/cmdline').read().replace(chr(0),' ').strip()\n"
        "    except OSError: pass\n"
        "print(json.dumps({'python': sys.version.split()[0], 'uid': os.getuid(),"
        " 'uvicorn': {k: v for k, v in procs.items() if v.startswith('python -m uvicorn')}}))",
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    observed = json.loads(probe.stdout.strip().splitlines()[-1])

    assert observed["python"] == "3.12.14"
    assert observed["uid"] == 1000
    # Exactly one uvicorn command line, and it is PID 1 -- one Application Process,
    # not a master and a pool, and no init shim between it and the platform's SIGTERM.
    assert list(observed["uvicorn"]) == ["1"], observed["uvicorn"]
    command = observed["uvicorn"]["1"]
    assert "--workers 1" in command
    assert "--host 0.0.0.0" in command and "--port 7860" in command
    assert "--proxy-headers" in command
    # No Volume is required, and no credential was baked into the Image.
    inspected = json.loads(_docker("inspect", name, timeout=60).stdout)[0]
    assert inspected["Mounts"] == []
    image_env = json.loads(_docker("inspect", IMAGE, timeout=60).stdout)[0]["Config"]["Env"]
    assert not [entry for entry in image_env if entry.startswith(("OLLAMA_", "DEPLOYMENT_"))]


@pytest.mark.release_suite
def test_the_image_carries_only_what_the_dockerfile_copied(image: str) -> None:
    """The credential claim, checked against the artefact instead of restated. The
    COPY allowlist names five paths -- the fifth, `agent`, is the pi sidecar's
    pruned Node tree copied in from the `agent` build stage -- and `/app` holds
    those five and the venv built from them, and nothing else -- no `.env`, no
    `.git`, no README, no tests."""
    listed = _docker("run", "--rm", "--entrypoint", "python", image, "-c",
                     "import os,json;print(json.dumps(sorted(os.listdir('/app'))))", timeout=120)
    assert listed.returncode == 0, listed.stderr
    entries = json.loads(listed.stdout.strip().splitlines()[-1])

    assert entries == [".python-version", ".venv", "agent", "pyproject.toml", "src", "uv.lock"]
    # And nothing secret anywhere else the build could have put it.
    found = _docker(
        "run", "--rm", "--entrypoint", "python", image, "-c",
        "import os,json\n"
        "hits=[]\n"
        "for root, dirs, files in os.walk('/app'):\n"
        "    dirs[:] = [d for d in dirs if d != '.venv']\n"
        "    hits += [os.path.join(root, f) for f in files\n"
        "             if f.startswith('.env') or f.endswith(('.pem', '.key')) or f == '.netrc']\n"
        "print(json.dumps(hits))",
        timeout=120,
    )
    assert found.returncode == 0, found.stderr
    assert json.loads(found.stdout.strip().splitlines()[-1]) == []


@pytest.mark.release_suite
def test_a_restarted_container_keeps_no_previous_state(container) -> None:
    """Ephemeral Disk: a restart is a new process with a new memory, so the previous
    Conversation and Run are simply gone -- Minimal 404, not an error page."""
    name, base_url = container
    origin = {"Origin": base_url}
    with httpx.Client(base_url=base_url, timeout=10) as client:
        created = client.post("/api/v1/conversations", headers=origin)
        conversation_id = _json(created, 201)["conversation_id"]
        capability = _capability(created)
        cookie = {"Cookie": f"conversation_capability={capability}"}
        accepted = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={**origin, **cookie, "Idempotency-Key": "before-restart"},
            json={"kind": "question", "content": "재시작 전 질문"},
        )
        run_id = _json(accepted, 202)["run_id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _json(client.get(f"/api/v1/runs/{run_id}", headers=cookie), 200)["state"] == "completed":
                break
            time.sleep(0.05)
        assert client.get(f"/api/v1/runs/{run_id}", headers=cookie).status_code == 200

    restarted = _docker("restart", name, timeout=120)
    assert restarted.returncode == 0, restarted.stderr
    _await_live(base_url)

    with httpx.Client(base_url=base_url, timeout=10) as client:
        gone_run = client.get(f"/api/v1/runs/{run_id}", headers=cookie)
        gone_submit = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={**origin, **cookie, "Idempotency-Key": "after-restart"},
            json={"kind": "question", "content": "재시작 후 질문"},
        )
        assert gone_run.status_code == 404 and gone_run.content == b""
        assert gone_submit.status_code == 404 and gone_submit.content == b""
        # And the restarted process is fully serviceable, not merely empty.
        assert client.get("/ready").status_code == 204
        fresh = client.post("/api/v1/conversations", headers=origin)
        assert _json(fresh, 201)["conversation_id"] != conversation_id
