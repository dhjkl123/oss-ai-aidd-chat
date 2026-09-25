from base64 import urlsafe_b64decode
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from importlib.resources import files
from pathlib import Path
import re
from subprocess import run
from tempfile import TemporaryDirectory
import tomllib
from uuid import UUID
from zipfile import ZipFile

from fastapi.testclient import TestClient

from aidd_chat.main import app


ORIGIN = {"Origin": "https://testserver"}


ROOT = Path(__file__).parents[1]


def capability_from(response) -> str:
    return response.cookies["conversation_capability"]


class ShellParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.elements = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append(
            (
                tag,
                attributes,
                any(item.get("data-ux") == "UX-COMPOSER" for _, item in self.stack),
            )
        )
        if tag not in {"meta", "link"}:
            self.stack.append((tag, attributes))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def test_live_shell_and_routes_share_lifespan_singleton():
    with TestClient(app, base_url="https://testserver") as client:
        application = app.state.chat_application
        assert client.get("/live").status_code == 204
        assert client.get("/live").content == b""
        assert client.get("/").status_code == 200
        assert client.get("/static/styles.css").status_code == 200
        assert client.post("/api/v1/conversations", headers=ORIGIN).status_code == 201
        store, provider = application.store, application.provider

    with TestClient(app, base_url="https://testserver"):
        assert app.state.chat_application is application
        assert app.state.chat_application.store is store
        assert app.state.chat_application.provider is provider


def test_shell_and_static_assets_are_revalidated_not_heuristically_cached():
    # A heuristically cached app.js next to a fresh index.html is a dead composer.
    with TestClient(app, base_url="https://testserver") as client:
        for path in ("/", "/static/app.js", "/static/markdown.js", "/static/styles.css", "/static/fonts/PretendardVariable.woff2"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers["cache-control"] == "no-cache", path
            assert response.headers.get("etag"), path
        assert client.get("/static/fonts/PretendardVariable.woff2").headers["content-type"] == "font/woff2"
        # Only the shell: API responses keep their own no-store.
        assert client.get("/live").headers.get("cache-control") != "no-cache"


def test_creation_returns_uuid_timestamps_and_cookie_but_stores_only_hash():
    with TestClient(app, base_url="https://testserver") as client:
        requested_at = datetime.now(UTC)
        response = client.post("/api/v1/conversations", headers=ORIGIN)
        responded_at = datetime.now(UTC)
        body = response.json()
        conversation_id = UUID(body["conversation_id"])
        created_at = datetime.fromisoformat(body["created_at"])
        expires_at = datetime.fromisoformat(body["expires_at"])
        capability = capability_from(response)

        assert response.status_code == 201
        assert conversation_id.version == 4
        assert requested_at <= created_at <= responded_at
        assert (expires_at - created_at).total_seconds() == 3_600
        assert created_at.utcoffset().total_seconds() == 0
        assert "capability" not in body
        assert len(urlsafe_b64decode(capability + "==")) == 32
        cookie_header = response.headers["set-cookie"]
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie["conversation_capability"]
        assert morsel["max-age"] == "3600"
        assert morsel["path"] == "/api/v1"
        assert parsedate_to_datetime(morsel["expires"]) == expires_at.replace(microsecond=0)
        assert all(flag in cookie_header.lower() for flag in ("httponly", "secure", "samesite=strict"))

        state = app.state.chat_application.store.states[conversation_id]
        assert state.capability_hash == sha256(capability.encode()).hexdigest()
        assert capability not in repr(state)


def test_new_conversation_is_empty_and_does_not_change_previous_ttl():
    with TestClient(app, base_url="https://testserver") as client:
        first = client.post("/api/v1/conversations", headers=ORIGIN)
        first_id = UUID(first.json()["conversation_id"])
        first_state = app.state.chat_application.store.states[first_id]
        first_state.messages.append("기존 문맥")
        original_expiry = first_state.expires_at

        second = client.post("/api/v1/conversations", headers=ORIGIN)
        second_id = UUID(second.json()["conversation_id"])
        second_state = app.state.chat_application.store.states[second_id]

        assert second_id != first_id
        assert capability_from(second) != capability_from(first)
        assert first_state.expires_at == original_expiry
        assert first_state.messages == ["기존 문맥"]
        assert second_state.messages == []


def test_packaged_shell_is_accessible_responsive_and_truthful():
    web = files("aidd_chat").joinpath("web")
    html = web.joinpath("index.html").read_text(encoding="utf-8")
    css = web.joinpath("styles.css").read_text(encoding="utf-8")
    script = web.joinpath("app.js").read_text(encoding="utf-8")

    assert all(web.joinpath(name).is_file() for name in ("index.html", "styles.css", "app.js"))
    parser = ShellParser()
    parser.feed(html)
    skip = next(attrs for tag, attrs, _ in parser.elements if tag == "a" and "skip-link" in attrs.get("class", "").split())
    target = next((tag, attrs) for tag, attrs, _ in parser.elements if attrs.get("id") == skip["href"].removeprefix("#"))
    textarea = next(attrs for tag, attrs, inside in parser.elements if tag == "textarea" and inside)
    send_button = next(attrs for tag, attrs, inside in parser.elements if tag == "button" and inside)
    new_conversation_controls = [attrs for _, attrs, _ in parser.elements if "data-new-conversation" in attrs]

    assert skip["href"] == "#main-content"
    assert target[1].get("tabindex") == "-1"
    assert ":focus-visible" in css
    assert "min-width: 320px" in css
    # Story 1.10 dropped `overflow-x: hidden`: clipping hides an overflowing
    # layout instead of failing it. The three breakpoints are the guarantee now.
    assert "overflow-x: hidden" not in css
    assert "@media (max-width: 767px)" in css and "@media (min-width: 1024px)" in css
    assert "Enter로 전송하고 Shift+Enter로 줄을 바꿉니다." in html
    assert "disabled" in textarea and "disabled" in send_button
    # UX-NAV-SHEET and the cancelled-run recovery panel (UX-RUN-RECOVERY). Task 24
    # retired the hero's 새 대화 시작 gate: the first question creates the conversation.
    assert len(new_conversation_controls) == 2

    assert 'const controls = [...document.querySelectorAll("[data-new-conversation]")];' in script
    assert 'control.addEventListener("click", createConversation)' in script
    assert 'fetch("/api/v1/conversations"' in script
    assert "빈 새 대화를 만들었습니다" in script and "새 대화를 만들지 못했습니다" in script


def test_built_wheel_contains_shell_assets_and_imports_as_clean_artifact():
    with TemporaryDirectory() as temporary_directory:
        output_directory = Path(temporary_directory)
        build = run(
            ["uv", "build", "--wheel", "--out-dir", str(output_directory)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert build.returncode == 0, build.stdout + build.stderr
        wheels = list(output_directory.glob("*.whl"))
        assert len(wheels) == 1

        wheel = wheels[0]
        with ZipFile(wheel) as archive:
            names = set(archive.namelist())
        assert {
            "aidd_chat/web/index.html",
            "aidd_chat/web/styles.css",
            "aidd_chat/web/app.js",
            "aidd_chat/web/markdown.js",
            "aidd_chat/web/fonts/PretendardVariable.woff2",
            "aidd_chat/web/fonts/OFL.txt",
        } <= names

        imported = run(
            [
                "uv",
                "run",
                "--isolated",
                "--no-project",
                "--with",
                str(wheel),
                "python",
                "-c",
                (
                    "from importlib.resources import files; "
                    "from aidd_chat.main import app; "
                    "assert app; "
                    "assert files('aidd_chat').joinpath('web/index.html').is_file()"
                ),
            ],
            cwd=output_directory,
            capture_output=True,
            text=True,
            check=False,
        )
        assert imported.returncode == 0, imported.stdout + imported.stderr


def test_framework_boundary_and_forbidden_dependencies():
    application = (ROOT / "src/aidd_chat/application/__init__.py").read_text(encoding="utf-8").lower()
    manifest_path = ROOT / "pyproject.toml"
    manifest = manifest_path.read_text(encoding="utf-8").lower()
    manifest_data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    assert "fastapi" not in application
    assert all(name not in manifest for name in ("langgraph", "neo4j", "graphrag", "vector", "embedding", "ragas"))
    assert manifest_data["tool"]["uv"]["required-version"] == "==0.12.5"
    assert all("playwright" not in item for item in manifest_data["project"]["dependencies"])
    assert "playwright==1.62.0" in manifest_data["dependency-groups"]["dev"]
