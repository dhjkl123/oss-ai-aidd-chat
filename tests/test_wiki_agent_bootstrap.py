from pathlib import Path

import pytest

from aidd_chat.adapters import FakeAgent
from aidd_chat.adapters.pi_sidecar import PiSidecarAdapter
from aidd_chat.bootstrap import UnboundProvider, build_provider

ROOT = Path(__file__).parents[1]
WIKI = ROOT / "agent" / "test-fixtures" / "wiki"
TOKENIZER = ROOT / "tests" / "fixtures" / "tiny-tokenizer.json"


def real_env(monkeypatch, **over):
    # ponytail: the brief's literal fixture uses OLLAMA_API_KEY="k" -- a single
    # letter that trivially collides with ordinary binding JSON text (e.g. the
    # "k" in "tokenizers"), making test_endpoint_binds_pi_sidecar's non-disclosure
    # assertion unsatisfiable by any implementation. Using a distinctive value
    # (the same convention as tests/test_story_1_5_1.py's API_KEY) keeps the
    # assertion meaningful without weakening it.
    values = {"OLLAMA_BASE_URL": "http://192.168.0.10:11434",
              "OLLAMA_API_KEY": "sk-wiki-agent-must-never-appear",
              "WIKI_ROOT": str(WIKI), "TOKENIZER_PATH": str(TOKENIZER)}
    values.update(over)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_no_endpoint_binds_fake_agent():
    assert isinstance(build_provider(), FakeAgent)


def test_endpoint_binds_pi_sidecar(monkeypatch):
    real_env(monkeypatch)
    provider = build_provider()
    assert isinstance(provider, PiSidecarAdapter)
    assert provider.binding.wiki_display_name == "wiki"
    assert provider.binding.tokenizer_authority.path == str(TOKENIZER.resolve())
    assert "sk-wiki-agent-must-never-appear" not in provider._binding_json


def test_node_binary_names_the_node_executable(monkeypatch):
    real_env(monkeypatch, NODE_BINARY="no-such-node-binary", NODE_PATH="node")
    provider = build_provider()
    assert isinstance(provider, UnboundProvider) and "NODE_BINARY" in provider.reason


@pytest.mark.parametrize("name", ["WIKI_ROOT", "TOKENIZER_PATH"])
def test_missing_agent_setting_is_unbound(monkeypatch, name):
    real_env(monkeypatch, **{name: ""})
    provider = build_provider()
    assert isinstance(provider, UnboundProvider) and name in provider.reason


def test_unreadable_tokenizer_is_unbound(monkeypatch, tmp_path):
    real_env(monkeypatch, TOKENIZER_PATH=str(tmp_path / "missing.json"))
    assert isinstance(build_provider(), UnboundProvider)


def test_small_window_is_refused(monkeypatch):
    real_env(monkeypatch, MODEL_CONTEXT_WINDOW="16384")
    provider = build_provider()
    assert isinstance(provider, UnboundProvider) and "MODEL_CONTEXT_WINDOW" in provider.reason


def test_missing_wiki_directory_still_binds_and_fails_readiness_later(monkeypatch, tmp_path):
    real_env(monkeypatch, WIKI_ROOT=str(tmp_path / "nowhere"))
    assert isinstance(build_provider(), PiSidecarAdapter)
