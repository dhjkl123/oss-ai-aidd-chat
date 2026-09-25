import json
import os
from hashlib import sha256

import pytest
from pydantic import ValidationError

from aidd_chat.contracts import (
    AgentBindingV1,
    AgentLimitsV1,
    TokenizerAuthorityV1,
    agent_binding_digest,
    agent_binding_json,
)

# Read at import time, not inside isolate_provider_env's per-test monkeypatch
# window, so a developer's own TEST_* value (or the neutral default) is what this
# module's tests use either way. Neutral defaults -- never the owner's real Wiki.
TEST_WIKI_ROOT = os.environ.get("TEST_WIKI_ROOT", "C:/w/llm-wiki-ro")
TEST_WIKI_NAME = os.environ.get("TEST_WIKI_NAME", "llm-wiki-ro")


def binding(**over):
    values = dict(
        provider_label="Ollama LAN proxy", endpoint_origin="http://192.168.0.10:11434",
        model_revision="qwen3.5:9b", model_context_window=32768, max_output_tokens=2048,
        tokenizer_authority=TokenizerAuthorityV1(name="hf-tokenizers", sha256="a" * 64, path="C:/t/tokenizer.json"),
        wiki_root=TEST_WIKI_ROOT, wiki_display_name=TEST_WIKI_NAME, limits=AgentLimitsV1(),
    )
    values.update(over)
    return AgentBindingV1(**values)


def test_defaults_match_the_spine():
    limits = AgentLimitsV1()
    assert (limits.max_tool_calls, limits.max_read_tokens, limits.max_tool_output_tokens,
            limits.max_output_tokens, limits.fixed_overhead_tokens, limits.model_context_window) == (
        8, 3000, 12000, 2048, 1500, 32768)
    item = binding()
    assert item.tool_allowlist == ("wiki_index", "wiki_search", "wiki_read", "decide")
    assert item.thinking == "off" and item.close_grace_ms == 250


def test_budget_sum_must_fit_the_window():
    with pytest.raises(ValidationError):
        AgentLimitsV1(model_context_window=16384)


def test_window_fields_must_agree_with_limits():
    with pytest.raises(ValidationError):
        binding(model_context_window=65536)


def test_tool_allowlist_is_closed():
    with pytest.raises(ValidationError):
        binding(tool_allowlist=("wiki_index", "wiki_search", "wiki_read", "decide", "shell"))


def test_endpoint_rejects_credentials_and_paths():
    for origin in ("http://user:pw@host:1", "http://host:1/v1", "file:///x"):
        with pytest.raises(ValidationError):
            binding(endpoint_origin=origin)


def test_digest_is_sha256_of_the_exact_json_the_sidecar_gets():
    item = binding()
    text = agent_binding_json(item)
    assert json.loads(text)["wiki_root"] == TEST_WIKI_ROOT
    assert list(json.loads(text))[0] == "schema_version"
    assert agent_binding_digest(item) == sha256(text.encode("utf-8")).hexdigest()
    assert "OLLAMA_API_KEY" in text and "secret" not in text
