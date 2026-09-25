# tests/test_wiki_agent_adapter.py
import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from uuid import uuid4

import pytest

from aidd_chat.adapters.pi_sidecar import PiSidecarAdapter, map_failure
from aidd_chat.adapters.tokenizer import HfTokenizer
from aidd_chat.application import AgentRunError, ProviderPolicyMetadata
from aidd_chat.contracts import (
    AgentBindingV1,
    AgentLimitsV1,
    PreparedMessageV1,
    TokenizerAuthorityV1,
    prepare_model_request,
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
ROOT = Path(__file__).parents[1]
SCRIPTED = ROOT / "tests" / "scripted_sidecar.mjs"
TOKENIZER = ROOT / "tests" / "fixtures" / "tiny-tokenizer.json"


def make_adapter(api_key="", origin="http://127.0.0.1:9", **env):
    tokenizer = HfTokenizer(TOKENIZER)
    binding = AgentBindingV1(
        provider_label="Ollama LAN proxy", endpoint_origin=origin, model_revision="qwen3.5:9b",
        model_context_window=32768, max_output_tokens=2048,
        tokenizer_authority=TokenizerAuthorityV1(name="hf-tokenizers", sha256=tokenizer.sha256, path=str(TOKENIZER)),
        wiki_root=str(ROOT / "agent" / "test-fixtures" / "wiki"), wiki_display_name="wiki", limits=AgentLimitsV1(),
    )
    from aidd_chat.adapters.pi_sidecar import pi_policy_metadata
    adapter = PiSidecarAdapter(binding, api_key, tokenizer, pi_policy_metadata(binding), script=SCRIPTED)
    adapter.extra_env = env
    return adapter


def request_for(adapter, text):
    return prepare_model_request((PreparedMessageV1(uuid4(), "user", text),),
                                 provider_profile_digest=adapter.provider_profile_digest)


async def drive(adapter, text):
    steps, deltas = [], []
    async with adapter.run(uuid4(), uuid4(), request_for(adapter, text), steps.append, deltas.append) as result:
        return result, steps, deltas


def run(coro):
    return asyncio.run(coro)


async def started(adapter):
    await adapter.start()
    await asyncio.sleep(0.2)  # let the boot context_probe frame arrive
    return adapter


@pytest.mark.parametrize("cause,status,kind", [
    ("provider_http", 401, "provider_auth"), ("provider_http", 403, "provider_auth"),
    ("provider_http", 429, "provider_rate_limit"), ("provider_http", 408, "provider_timeout"),
    ("provider_http", 503, "provider_unavailable"), ("provider_http", 404, "provider_invalid_response"),
    ("provider_timeout", None, "provider_timeout"), ("provider_transport", None, "provider_transport"),
    ("wiki_unavailable", None, "wiki_unavailable"), ("protocol", None, "provider_invalid_response"),
    ("context_overflow", None, "provider_incomplete"), ("internal", None, "provider_unknown"),
    ("nonsense", None, "provider_unknown"), (["provider_http"], 401, "provider_unknown"),
    ({"cause": "internal"}, None, "provider_unknown"),
])
def test_failure_table(cause, status, kind):
    assert map_failure(cause, status) == kind


def test_grounded_run_relays_steps_deltas_and_result():
    async def go():
        adapter = await started(make_adapter())
        try:
            return await drive(adapter, "grounded")
        finally:
            await adapter.aclose()
    result, steps, deltas = run(go())
    assert [step.kind for step in steps] == ["wiki_read", "decide", "compose"]
    assert deltas == ["알파는 ", "개념이에요."]
    assert result.outcome == "grounded" and result.sources[0].contested is True


@pytest.mark.parametrize("question,kind", [
    ("error:provider_http:429", "provider_rate_limit"),
    ("error:wiki_unavailable", "wiki_unavailable"),
    ("badstep", "provider_invalid_response"),
    ("unknown", "provider_invalid_response"),
    ("badcause", "provider_invalid_response"),
])
def test_error_frames_map_to_kinds(question, kind):
    async def go():
        adapter = await started(make_adapter())
        try:
            with pytest.raises(AgentRunError) as caught:
                await drive(adapter, question)
            return caught.value.kind
        finally:
            await adapter.aclose()
    assert run(go()) == kind


def test_digest_mismatch_is_provider_unknown_and_not_ready():
    async def go():
        adapter = await started(make_adapter(SCRIPTED_WRONG_DIGEST="1"))
        try:
            probe = await adapter.probe()
            with pytest.raises(AgentRunError) as caught:
                await drive(adapter, "grounded")
            return probe, caught.value.kind
        finally:
            await adapter.aclose()
    probe, kind = run(go())
    assert not probe.ready and kind == "provider_unknown"


def test_probe_ready_and_context_probe_failure():
    async def go(**env):
        adapter = await started(make_adapter(**env))
        try:
            return await adapter.probe()
        finally:
            await adapter.aclose()
    assert run(go()).ready
    short = run(go(SCRIPTED_CONTEXT_OK="0"))
    assert not short.context_ok and not short.ready


def test_late_probe_reply_never_answers_the_next_probe(monkeypatch):
    import aidd_chat.adapters.pi_sidecar as module
    monkeypatch.setattr(module, "PROBE_TIMEOUT_SECONDS", 0.5)

    async def go():
        adapter = await started(make_adapter(SCRIPTED_STALE_PROBE="1"))
        try:
            first = await adapter.probe()  # its not-ok reply lands during the second probe
            second = await adapter.probe()
            return first, second
        finally:
            await adapter.aclose()
    first, second = run(go())
    assert not first.provider_ok and second.provider_ok


def test_context_ok_follows_a_retried_context_probe():
    adapter = make_adapter()
    frame = {"v": 1, "type": "context_probe", "run_id": None, "binding_digest": adapter.binding_digest}
    adapter._dispatch(json.dumps({**frame, "effective_context_ok": False}).encode())
    assert adapter._context_ok is False
    adapter._dispatch(json.dumps({**frame, "effective_context_ok": True}).encode())
    assert adapter._context_ok is True


def test_abort_sends_frame_and_awaits_aborted():
    async def go():
        adapter = await started(make_adapter())
        run_id = uuid4()
        try:
            async def body():
                async with adapter.run(run_id, uuid4(), request_for(adapter, "hang"), lambda _s: None, lambda _t: None):
                    pass
            task = asyncio.create_task(body())
            await asyncio.sleep(0.2)
            started_at = time.monotonic()
            await adapter.abort(run_id)
            elapsed = time.monotonic() - started_at
            with pytest.raises(AgentRunError) as caught:
                await task
            await adapter.abort(run_id)  # idempotent, unknown now
            return caught.value.kind, elapsed
        finally:
            await adapter.aclose()
    kind, elapsed = run(go())
    assert kind == "provider_incomplete" and elapsed < 0.25


def test_callback_rejection_aborts_the_sidecar_run():
    async def go():
        adapter = await started(make_adapter())
        try:
            def refuse(_text):
                raise RuntimeError("fenced")
            with pytest.raises(RuntimeError):
                async with adapter.run(uuid4(), uuid4(), request_for(adapter, "grounded"), lambda _s: None, refuse):
                    pass
            return await drive(adapter, "gap")
        finally:
            await adapter.aclose()
    result, _steps, _deltas = run(go())
    assert result.outcome == "wiki_gap"  # the sidecar is still healthy afterwards


def test_child_exit_fails_inflight_run_and_respawns(monkeypatch):
    import aidd_chat.adapters.pi_sidecar as module
    monkeypatch.setattr(module, "RESPAWN_INTERVAL_SECONDS", 0.3)

    async def go():
        adapter = await started(make_adapter())
        try:
            with pytest.raises(AgentRunError) as caught:
                await drive(adapter, "exit")
            immediately = await adapter.probe()
            await asyncio.sleep(0.5)
            later = await adapter.probe()
            result, _s, _d = await drive(adapter, "gap")
            return caught.value.kind, immediately, later, result
        finally:
            await adapter.aclose()
    kind, immediately, later, result = run(go())
    assert kind == "agent_runtime_unavailable"
    assert not immediately.ready and later.ready and result.outcome == "wiki_gap"


def test_aclose_reaps_the_child_and_fails_inflight_runs():
    async def go():
        adapter = await started(make_adapter())
        process = adapter._process
        task = asyncio.create_task(drive(adapter, "hang"))
        await asyncio.sleep(0.2)
        await adapter.aclose()
        with pytest.raises(AgentRunError) as caught:
            await task
        await adapter.abort(uuid4())  # unknown run after close: no-op, no hang
        return process.returncode, caught.value.kind
    returncode, kind = run(go())
    assert returncode is not None and kind == "agent_runtime_unavailable"


def test_context_ok_is_established_once_per_binding(monkeypatch):
    import aidd_chat.adapters.pi_sidecar as module
    monkeypatch.setattr(module, "RESPAWN_INTERVAL_SECONDS", 0.3)

    async def respawn_with(adapter, context_env):
        with pytest.raises(AgentRunError):
            await drive(adapter, "exit")
        adapter.extra_env = {"SCRIPTED_CONTEXT_OK": context_env}
        await asyncio.sleep(0.5)
        return await adapter.probe()

    async def go():
        adapter = await started(make_adapter())
        try:
            first = await adapter.probe()
            silent = await respawn_with(adapter, "none")  # new child never reports
            short = await respawn_with(adapter, "0")  # new child reports a shortfall
            return first, silent, short
        finally:
            await adapter.aclose()
    first, silent, short = run(go())
    assert first.ready and silent.ready and short.ready


def test_close_during_spawn_reaps_the_new_child(monkeypatch):
    real = asyncio.create_subprocess_exec
    spawned = []
    adapter = make_adapter()

    async def racing(*args, **kwargs):
        await adapter.aclose()  # the close lands while the spawn is in flight
        process = await real(*args, **kwargs)
        spawned.append(process)
        return process

    async def go():
        monkeypatch.setattr(asyncio, "create_subprocess_exec", racing)
        await adapter.start()
        probe = await adapter.probe()  # closed: must not spawn again
        with pytest.raises(AgentRunError) as caught:
            await drive(adapter, "gap")
        await adapter.aclose()
        return probe, caught.value.kind
    probe, kind = run(go())
    assert len(spawned) == 1 and spawned[0].returncode is not None
    assert not probe.ready and kind == "agent_runtime_unavailable"


def test_logs_are_content_free(caplog):
    caplog.set_level(logging.INFO)

    async def go():
        adapter = await started(make_adapter())
        try:
            await drive(adapter, "grounded")
        finally:
            await adapter.aclose()
    run(go())
    text = caplog.text
    for forbidden in ("grounded", "알파", "concepts/alpha.md", "Alpha 개념"):
        assert forbidden not in text


def test_key_and_endpoint_are_never_disclosed(caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    key, origin = "sk-test-SECRET", "http://secret-origin-7f3a.lan:11434"
    stdin_log = tmp_path / "stdin.log"

    async def go():
        adapter = await started(make_adapter(key, origin, SCRIPTED_STDIN_LOG=str(stdin_log)))
        try:
            probe = await adapter.probe()
            errors = []
            for question in ("error:provider_http:401", "error:provider_transport"):
                with pytest.raises(AgentRunError) as caught:
                    await drive(adapter, question)
                errors.append(caught.value)
            return probe, errors
        finally:
            await adapter.aclose()
    probe, errors = run(go())
    assert probe.ready and [error.kind for error in errors] == ["provider_auth", "provider_transport"]
    lines = stdin_log.read_text(encoding="utf-8").splitlines()
    assert lines[0] == f"ENV {key}"  # the key reached the child through env OLLAMA_API_KEY
    written = "\n".join(lines[1:])
    assert len(lines) == 4 and '"type":"probe"' in written  # probe + two runs, nothing else
    for secret in (key, "SECRET", origin, "secret-origin-7f3a"):
        assert secret not in written
        assert secret not in caplog.text
        for error in errors:
            assert secret not in str(error) and secret not in repr(error) and secret not in repr(error.args)


def test_unsafe_text_sets_agree():
    import re
    from aidd_chat.contracts import has_unsafe_code_point
    source = (ROOT / "agent" / "protocol.mjs").read_text(encoding="utf-8")
    assert "\\u202a-\\u202e" in source and "\\u{e0000}-\\u{e007f}" in source
    for probe in ("\u202e", "\u200b", "\ufeff", "\U000e0041", "\x07"):
        assert has_unsafe_code_point(probe)
