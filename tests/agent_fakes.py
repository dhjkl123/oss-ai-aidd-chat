"""Story-era Provider shapes on the AgentRuntimePort.

The story suites (1.2-1.11) define fakes with a synchronous
`stream(request, on_delta, handle)` or `complete(request, handle)` and a
`new_call_handle()` whose `cancel()` stops them. This shim runs that method on a
worker thread and hops each delta back onto the agent loop, so a Domain refusal
raised inside `on_delta` reaches the fake exactly as it did before, and `abort`
reaches the fake's handle exactly as `cancel()` did. Behavioural assertions in
those suites stay as written."""

import asyncio
from contextlib import asynccontextmanager, suppress
import inspect
from threading import Event

from aidd_chat.adapters.fake_agent import FAKE_SOURCE, FakeAgent
from aidd_chat.application import AgentRunError
from aidd_chat.contracts import AgentProbeV1, AgentResultV1, has_unsafe_code_point, has_visible_text

GROUNDED_RESULT = AgentResultV1(
    outcome="grounded", uncovered=None, sources=(FAKE_SOURCE,), search_truncated=False, finish="stop"
)


class ProviderCallHandle:
    """The story-era cancel handle: `cancel()` from any thread, `cancelled` read by
    the fake's own synchronous loop."""

    def __init__(self) -> None:
        self._event = Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()


def _legacy_probe(probe):
    """A story-era synchronous `probe() -> bool`, on the async port. It runs on a
    worker thread like the story-era probe thread did, and -- also like that thread
    -- it does not stop when the Application's probe timeout cancels it: its result
    arrives late and is discarded, not cut short. A bool becomes the AgentProbeV1
    it stood for; anything else (a truthy non-bool) is handed over unchanged so the
    Application must refuse it."""

    async def run(self):
        waiter = asyncio.ensure_future(asyncio.to_thread(probe, self))
        while not waiter.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(waiter)
        result = waiter.result()
        if isinstance(result, bool):
            return AgentProbeV1(provider_ok=result, wiki_ok=True, context_ok=True)
        return result

    return run


class LegacySyncAgent(FakeAgent):
    stream = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        probe = cls.__dict__.get("probe")
        if probe is not None and not inspect.iscoroutinefunction(probe):
            cls.probe = _legacy_probe(probe)

    @property
    def _handles(self) -> dict:
        # Story-era fakes define their own __init__ without calling super().
        return self.__dict__.setdefault("_legacy_handles", {})

    def new_call_handle(self):
        return ProviderCallHandle()

    def complete(self, request, handle=None):
        return f"테스트 응답: {request.messages[-1].text}"

    @asynccontextmanager
    async def run(self, run_id, correlation_id, prepared, on_step, on_delta):
        loop = asyncio.get_running_loop()
        handle = self.new_call_handle()
        self._handles[run_id] = handle
        sent: list[str] = []

        def relay(text):
            async def deliver():
                on_delta(text)

            asyncio.run_coroutine_threadsafe(deliver(), loop).result()
            sent.append(text)

        try:
            if self.stream is not None:
                output = await asyncio.to_thread(self.stream, prepared, relay, handle)
            else:
                output = await asyncio.to_thread(self.complete, prepared, handle)
                # to_thread's await resumes back on the agent loop -- the same
                # thread relay() would try to run_coroutine_threadsafe(...).result()
                # onto, which is a deadlock. Call on_delta directly instead.
                if isinstance(output, str) and output:
                    # A blank (whitespace-only) delta would otherwise reach the
                    # Domain's commit_delta, which raises a raw ValidationError
                    # while building the completed-message echo -- pre-check it
                    # instead so this ends provider_empty, not an unclassified provider_unknown.
                    # Only a blank answer that is not ALSO hostile stops here. Hostile
                    # text (a zero-width answer included) goes on to on_delta, so the
                    # Application's own refusal is what these suites prove.
                    if not has_visible_text(output) and not has_unsafe_code_point(output):
                        raise AgentRunError("provider_empty")
                    on_delta(output)
                    sent.append(output)
        finally:
            self._handles.pop(run_id, None)
        if not isinstance(output, str):
            raise AgentRunError("provider_non_text")
        if "".join(sent) != output:
            raise AgentRunError("provider_invalid_response")
        yield GROUNDED_RESULT

    async def abort(self, run_id):
        handle = self._handles.get(run_id)
        if handle is not None:
            await asyncio.to_thread(handle.cancel)
