"""AD-27 production AgentRuntimePort: one `node agent/sidecar.mjs` child, JSONL over
stdio, Runs multiplexed by run_id. This adapter is the sole owner of the cause ->
ProviderFailureV1 mapping (AD-25); the sidecar only reports causes."""

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
import json
import logging
import os
from pathlib import Path
import shutil
import time
from uuid import UUID, uuid4

from pydantic import ValidationError

from aidd_chat.application import (
    SESSION_TTL_SECONDS,
    TRANSMITTED_FIELDS,
    AgentRunError,
    ProviderPolicyMetadata,
)
from aidd_chat.contracts import (
    HISTORY_TOKEN_BUDGET,
    AgentBindingV1,
    AgentProbeV1,
    AgentResultV1,
    AgentStepV1,
    PreparedModelRequestV1,
    agent_binding_digest,
    agent_binding_json,
    compute_provider_profile_digest,
    verify_request_integrity,
)

from .tokenizer import HfTokenizer

AGENT_SCRIPT = Path(__file__).resolve().parents[3] / "agent" / "sidecar.mjs"
RESPAWN_INTERVAL_SECONDS = 5.0
PROBE_TIMEOUT_SECONDS = 1.8
_LOG_FIELDS = ("correlation_id", "run_stage", "duration_ms", "terminal_state", "error_class",
               "step_count", "tool_name")
_CAUSES = frozenset({"provider_http", "provider_timeout", "provider_transport", "wiki_unavailable",
                     "context_overflow", "protocol", "internal"})
_FRAME_KEYS = {
    "step": {"v", "type", "run_id", "step"},
    "delta": {"v", "type", "run_id", "text"},
    "final": {"v", "type", "run_id", "binding_digest", "outcome", "uncovered", "sources",
              "search_truncated", "finish"},
    "error": {"v", "type", "run_id", "binding_digest", "cause", "http_status"},
    "aborted": {"v", "type", "run_id"},
}
_ENV_PASSTHROUGH = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")

_log = logging.getLogger(__name__)


def map_failure(cause: object, http_status: object) -> str:
    """AD-25 table, first matching row wins."""
    if not isinstance(cause, str):
        return "provider_unknown"
    if cause == "provider_http" and isinstance(http_status, int) and not isinstance(http_status, bool):
        if http_status in (401, 403):
            return "provider_auth"
        if http_status == 429:
            return "provider_rate_limit"
        if http_status == 408:
            return "provider_timeout"
        if 500 <= http_status <= 599:
            return "provider_unavailable"
        if 400 <= http_status <= 499:
            return "provider_invalid_response"
        return "provider_unknown"
    return {
        "provider_timeout": "provider_timeout",
        "provider_transport": "provider_transport",
        "wiki_unavailable": "wiki_unavailable",
        "protocol": "provider_invalid_response",
        "context_overflow": "provider_incomplete",
    }.get(cause, "provider_unknown")


def pi_policy_metadata(binding: AgentBindingV1) -> ProviderPolicyMetadata:
    return ProviderPolicyMetadata(
        schema_version="1",
        provider_label=binding.provider_label,
        model_revision=binding.model_revision,
        transmitted_fields=TRANSMITTED_FIELDS,
        session_ttl_seconds=SESSION_TTL_SECONDS,
        retrieval_status="wiki_readonly",
        wiki_display_name=binding.wiki_display_name,
    )


@dataclass
class _Channel:
    queue: "asyncio.Queue[tuple[str, dict | None]]" = field(default_factory=asyncio.Queue)
    aborted: asyncio.Event = field(default_factory=asyncio.Event)
    abort_sent: bool = False


class PiSidecarAdapter:
    def __init__(
        self,
        binding: AgentBindingV1,
        api_key: str,
        tokenizer: HfTokenizer,
        policy: ProviderPolicyMetadata,
        *,
        node: str = "node",
        script: Path = AGENT_SCRIPT,
    ) -> None:
        self.binding = binding
        self._binding_json = agent_binding_json(binding)
        self._digest = agent_binding_digest(binding)
        self._api_key = api_key
        self._tokenizer = tokenizer
        self._policy = policy
        self._node = node
        self._script = script
        self.extra_env: dict[str, str] = {}
        self._process: asyncio.subprocess.Process | None = None  # owned until reaped
        self._live: asyncio.subprocess.Process | None = None  # cleared the moment stdout ends
        self._tasks: list[asyncio.Task] = []
        self._runs: dict[UUID, _Channel] = {}
        # Keyed by probe_id: the sidecar echoes it, so a late reply to a timed-out probe
        # finds no waiter instead of answering the next probe.
        self._probe_waiters: dict[str, asyncio.Future] = {}
        # Effective-context result (AD-30), exposed as AgentProbeV1.context_ok. Established once
        # per binding: kept across respawns, and once True a later child cannot revoke it. A
        # False may still turn True: the sidecar re-runs a context probe that hit a Provider error.
        self._context_ok: bool | None = None
        self._closed = False
        self._last_change = float("-inf")  # last spawn or child exit; throttles respawn
        self._write_lock: asyncio.Lock | None = None

    # -- metadata the Application reads --------------------------------------
    @property
    def policy_metadata(self) -> ProviderPolicyMetadata:
        return self._policy

    @property
    def close_grace_ms(self) -> int:
        return self.binding.close_grace_ms

    @property
    def max_input_tokens(self) -> int:
        return HISTORY_TOKEN_BUDGET

    @property
    def tokenizer_authority(self) -> tuple[str, str, int]:
        return ("hf-tokenizers", self._tokenizer.sha256, HISTORY_TOKEN_BUDGET)

    @property
    def provider_profile_digest(self) -> str:
        return compute_provider_profile_digest(asdict(self._policy), self.tokenizer_authority)

    @property
    def binding_digest(self) -> str:
        return self._digest

    def count_input_tokens(self, request: PreparedModelRequestV1) -> int:
        verify_request_integrity(request, self)
        return self._tokenizer.count(request.canonical_bytes.decode("utf-8"))

    # -- process lifecycle ----------------------------------------------------
    async def start(self) -> None:
        self._closed = False
        await self._spawn()

    async def _spawn(self) -> None:
        self._last_change = time.monotonic()
        self._write_lock = self._write_lock or asyncio.Lock()
        env = {name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ}
        env["AIDD_AGENT_BINDING"] = self._binding_json
        if self._api_key:
            env["OLLAMA_API_KEY"] = self._api_key  # the only way the key reaches the child
        env.update(self.extra_env)
        process = await asyncio.create_subprocess_exec(
            shutil.which(self._node) or self._node, str(self._script),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, limit=4 * 1024 * 1024,
        )
        if self._closed:  # aclose() ran while we were spawning: reap, never adopt
            process.kill()
            process.stdin.close()
            await process.wait()
            return
        self._process = self._live = process
        self._tasks = [asyncio.create_task(self._read(process)), asyncio.create_task(self._drain_stderr(process))]

    async def aclose(self) -> None:
        self._closed = True
        await self._reap()

    async def _reap(self) -> None:
        process = self._process
        self._process = self._live = None
        if process is not None:
            if process.returncode is None:
                with suppress(Exception):
                    process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            # The child is gone, so both readers hit EOF on their own; cancel only stragglers.
            with suppress(Exception):
                await asyncio.wait(self._tasks, timeout=1)
        for task in self._tasks:
            task.cancel()
            with suppress(BaseException):
                await task
        self._fail_inflight()

    def _fail_inflight(self) -> None:
        for channel in self._runs.values():
            channel.queue.put_nowait(("exit", None))
        for waiter in self._probe_waiters.values():
            if not waiter.done():
                waiter.set_result(None)

    async def _ensure_alive(self) -> bool:
        if self._live is not None:
            return True
        if self._closed or time.monotonic() - self._last_change < RESPAWN_INTERVAL_SECONDS:
            return False
        self._last_change = time.monotonic()  # before any await: one respawn per interval
        if self._process is not None:  # dead child from before: reap it first
            await self._reap()
        try:
            await self._spawn()
        except Exception as exc:
            _log.error("agent_sidecar error_class=agent_runtime_unavailable (%s)", type(exc).__name__)
            return False
        return self._live is not None

    async def _read(self, process: asyncio.subprocess.Process) -> None:
        try:
            async for raw in process.stdout:
                self._dispatch(raw)
        finally:
            # Mark dead synchronously, before any await, so no Run can register against it unseen.
            if self._live is process:
                self._live = None
                self._last_change = time.monotonic()
                _log.error("agent_sidecar error_class=agent_runtime_unavailable")
                self._fail_inflight()
            with suppress(Exception):
                process.stdin.close()  # closes the pipe transport (no ResourceWarning)
            with suppress(Exception):
                await process.wait()

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        async for raw in process.stderr:
            try:
                fields = json.loads(raw)
            except ValueError:
                continue
            if isinstance(fields, dict):
                kept = " ".join(f"{key}={fields[key]}" for key in _LOG_FIELDS if key in fields)
                if kept:
                    _log.info("agent_sidecar %s", kept)

    def _dispatch(self, raw: bytes) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            return
        if not isinstance(frame, dict) or frame.get("v") != 1:
            return
        kind = frame.get("type")
        run_id = frame.get("run_id")
        if run_id is None:
            if kind == "probe_ok":
                waiter = self._probe_waiters.get(frame.get("probe_id"))
                if waiter is not None and not waiter.done():
                    waiter.set_result(frame)
            elif kind == "context_probe" and self._context_ok is not True:
                self._context_ok = (frame.get("binding_digest") == self._digest
                                    and frame.get("effective_context_ok") is True)
            return
        try:
            key = UUID(str(run_id))
        except ValueError:
            return
        channel = self._runs.get(key)
        if channel is not None:
            channel.queue.put_nowait((str(kind), frame))

    async def _send(self, frame: dict) -> None:
        process = self._live
        if process is None or process.returncode is not None or process.stdin is None:
            raise AgentRunError("agent_runtime_unavailable")
        data = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            async with self._write_lock:
                process.stdin.write(data)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            raise AgentRunError("agent_runtime_unavailable") from exc

    # -- port -------------------------------------------------------------------
    async def probe(self) -> AgentProbeV1:
        down = AgentProbeV1(provider_ok=False, wiki_ok=False, context_ok=False)
        if not await self._ensure_alive():
            return down
        probe_id = str(uuid4())
        waiter = self._probe_waiters[probe_id] = asyncio.get_running_loop().create_future()
        try:
            await self._send({"v": 1, "type": "probe", "run_id": None, "probe_id": probe_id})
            frame = await asyncio.wait_for(waiter, PROBE_TIMEOUT_SECONDS)
        except (AgentRunError, TimeoutError):
            return down
        finally:
            del self._probe_waiters[probe_id]
        if not isinstance(frame, dict) or frame.get("binding_digest") != self._digest:
            return down
        return AgentProbeV1(provider_ok=frame.get("provider_ok") is True,
                            wiki_ok=frame.get("wiki_ok") is True, context_ok=self._context_ok is True)

    @asynccontextmanager
    async def run(self, run_id, correlation_id, prepared, on_step, on_delta):
        verify_request_integrity(prepared, self)
        if not await self._ensure_alive():
            raise AgentRunError("agent_runtime_unavailable")
        channel = _Channel()
        self._runs[run_id] = channel
        try:
            await self._send({
                "v": 1, "type": "run", "run_id": str(run_id), "correlation_id": str(correlation_id),
                "system_instruction": prepared.system_instruction,
                "messages": [{"role": message.role, "text": message.text} for message in prepared.messages],
            })
            result = await self._consume(run_id, channel, on_step, on_delta)
        finally:
            self._runs.pop(run_id, None)
        yield result

    async def _consume(self, run_id, channel, on_step, on_delta) -> AgentResultV1:
        while True:
            kind, frame = await channel.queue.get()
            if kind == "exit":
                raise AgentRunError("agent_runtime_unavailable")
            if kind not in _FRAME_KEYS or set(frame) != _FRAME_KEYS[kind]:
                await self._abort_channel(run_id, channel)
                raise AgentRunError("provider_invalid_response")
            if kind == "aborted":
                channel.aborted.set()
                raise AgentRunError("provider_incomplete")
            if kind in ("final", "error") and frame["binding_digest"] != self._digest:
                raise AgentRunError("provider_unknown")
            if kind == "error":
                cause, status = frame["cause"], frame["http_status"]
                if not isinstance(cause, str) or cause not in _CAUSES or (cause == "provider_http") != isinstance(status, int):
                    raise AgentRunError("provider_invalid_response")
                raise AgentRunError(map_failure(cause, status))
            try:
                if kind == "final":
                    return AgentResultV1.model_validate(
                        {key: frame[key] for key in ("outcome", "uncovered", "sources", "search_truncated", "finish")})
                payload = AgentStepV1.model_validate(frame["step"]) if kind == "step" else frame["text"]
                if kind == "delta" and not isinstance(payload, str):
                    raise TypeError
            except (ValidationError, TypeError):
                await self._abort_channel(run_id, channel)
                raise AgentRunError("provider_invalid_response")
            try:
                (on_step if kind == "step" else on_delta)(payload)
            except BaseException:
                await self._abort_channel(run_id, channel)
                raise

    async def _abort_channel(self, run_id: UUID, channel: _Channel) -> None:
        if not channel.abort_sent:
            channel.abort_sent = True
            with suppress(AgentRunError):
                await self._send({"v": 1, "type": "abort", "run_id": str(run_id)})
        # Drain until the sidecar confirms, bounded by the close grace (AD-25).
        deadline = time.monotonic() + self.close_grace_ms / 1_000
        while not channel.aborted.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                kind, _frame = await asyncio.wait_for(channel.queue.get(), remaining)
            except TimeoutError:
                return
            if kind in ("aborted", "final", "error", "exit"):
                channel.aborted.set()

    async def abort(self, run_id: UUID) -> None:
        channel = self._runs.get(run_id)
        if channel is None or channel.abort_sent:
            return
        channel.abort_sent = True
        with suppress(AgentRunError):
            await self._send({"v": 1, "type": "abort", "run_id": str(run_id)})
        with suppress(TimeoutError):
            await asyncio.wait_for(channel.aborted.wait(), self.close_grace_ms / 1_000)
