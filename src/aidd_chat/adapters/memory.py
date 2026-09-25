from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from itertools import islice
import logging
from secrets import compare_digest
from threading import Event, Lock, Thread
import time
from uuid import UUID

from aidd_chat.application import Conversation
from aidd_chat.contracts import (
    AgentResultV1,
    AgentStepV1,
    PreparedMessageV1,
    PreparedModelRequestV1,
    ProviderFailureV1,
    RunEventV1,
    RunProjectionV1,
)
from aidd_chat.domain import (
    AcceptQuestionResult,
    ActiveRunConflict,
    CapacityExceeded,
    ConversationAggregate,
    ConversationExpired,
    DeploymentLimits,
    IdempotencyConflict,
    RetryExhausted,
    RunLease,
    RetryNotRetryable,
    RunNotFound,
    RunObservationV1,
    TurnLimitReached,
)


class ConversationNotFound(Exception):
    pass


ConversationState = ConversationAggregate

# How often the Sweeper looks for Conversations nobody is opening. Not a second
# expiry concept: it only decides how long an already-expired Conversation may sit
# in memory before it is reclaimed, never when it expires.
_log = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 30.0
RATE_WINDOW_SECONDS = 60.0
# How long an expired Run's two-Event tail is kept so a stream that was open at the
# moment of the Purge can still be told what happened. Comfortably longer than the
# 20 ms the SSE loop takes to come back, and far shorter than anything it bounds.
EXPIRY_TOMBSTONE_SECONDS = 120.0
# Rate-limiter keys held at once. Every other resource here has a ceiling; without
# one, a caller that can mint distinct subjects mints unbounded memory. At the cap
# the oldest tenth is evicted -- never the newcomer, because shedding new keys is a
# denial of service against exactly the legitimate sessions arriving next.
MAX_RATE_KEYS = 10_000
RATE_EVICTION_BATCH = MAX_RATE_KEYS // 10


class InMemoryConversationStore:
    """The single owner of this process's capacity. Every ceiling is counted and
    enforced inside `_index_lock`, so the count that refuses a request is the same
    count the winning request incremented -- and every reservation has exactly one
    release, because a ceiling that only ever goes up stops the service on its own."""

    def __init__(self) -> None:
        self.states: dict[UUID, ConversationAggregate] = {}
        self._locks: dict[UUID, Lock] = {}
        self._run_conversations: dict[UUID, UUID] = {}
        self._index_lock = Lock()
        # Replaced by `apply_limits` before the first Conversation exists. Inert
        # until then: the Application that owns the numbers always pushes them in.
        self.limits = DeploymentLimits()
        self._active_runs: Counter[UUID] = Counter()
        self._active_total = 0
        self._queued_total = 0
        self._reserved_units = 0
        self._sse_connections = 0
        self._provider_calls = 0
        self._spend_window_start = time.monotonic()
        self._spend_units = 0
        self._rates: dict[tuple[object, str], tuple[float, int]] = {}
        # run_id -> (reaped_at, capability_hash, the Aggregate's own expiry tail).
        # An open stream may read this after its Conversation is gone, whoever won
        # the Purge, which is what makes the tail a property of the stream being
        # writable rather than of who noticed the expiry.
        self._expired_runs: dict[UUID, tuple[float, str, tuple[RunEventV1, ...]]] = {}
        self._cancel_run: Callable[[UUID], None] | None = None
        self._purge_observer: Callable[[object], None] | None = None
        self._stop = Event()
        self._sweeper: Thread | None = None

    # ---- wiring -----------------------------------------------------------

    def apply_limits(self, limits: DeploymentLimits) -> None:
        """Bootstrap's numbers, pushed in by the Application that owns them. Called
        before the first Conversation exists, so no Aggregate is ever built against
        one set of limits and enforced against another."""
        self.limits = limits

    def set_run_canceller(self, cancel_run: Callable[[UUID], None]) -> None:
        """How the Store stops a live Run's Provider work when it Purges. The Store
        can Fence a callback out (that is `active_run_id`); only the Application can
        tell the Provider to stop."""
        self._cancel_run = cancel_run

    def set_purge_observer(self, observe: Callable[[object], None]) -> None:
        """How the Store reports an expiry it Purged. Registered the same way as the
        canceller and for the same reason: the Sweeper runs on the Store's own timer
        thread, so an expiry the Application never drove would otherwise be the one
        path in this process that ends Runs unobserved. Receives the live Run's
        RunObservationV1, or None when the Conversation held no live Run."""
        self._purge_observer = observe

    def close(self) -> None:
        """Stop the Sweeper, permanently. `_stop` is never cleared again: a `create`
        arriving during or after shutdown would otherwise start a fresh daemon
        Sweeper that nothing will ever join, which is the exact hazard
        `_start_sweeper_locked` reads `_stop` to prevent. A Store is closed once,
        when its process is going away."""
        with self._index_lock:
            sweeper, self._sweeper = self._sweeper, None
            self._stop.set()
        if sweeper is not None:
            sweeper.join(5)

    # ---- capacity ---------------------------------------------------------

    def reserve_run(self, conversation_id: UUID) -> RunLease:
        """Every ceiling a new Run has to fit through, counted and decided in one
        lock: the Spend Circuit Breaker first (open means nothing new starts at
        all), then the Run ceilings, then Provider Concurrency.

        Queued and Active are genuinely different populations. A reservation is
        Queued until `start_run` moves it; the Global ceiling bounds Queued+Active
        together so it stays hard, while the Queue ceiling bounds how much work may
        be waiting to start. Provider Concurrency counts calls actually in flight --
        a queued lease is not one -- so it is checked here and incremented at
        `start_run`, where the call really begins."""
        monotonic_now = time.monotonic()
        with self._index_lock:
            self._roll_spend_window_locked(monotonic_now)
            # Reservations that have not charged yet count against the breaker too.
            # Without them a full Active ceiling of requests passes an open breaker
            # before any of them charges, and the window overshoots by that much.
            if self._spend_units + self._reserved_units >= self.limits.spend_limit_units:
                raise CapacityExceeded("spend_limit_reached")
            if self._active_total + self._queued_total >= self.limits.max_global_active_runs:
                raise CapacityExceeded("run_capacity_exceeded")
            if self._active_runs[conversation_id] >= self.limits.max_session_active_runs:
                raise CapacityExceeded("session_run_capacity_exceeded")
            if self._queued_total >= self.limits.max_queued_runs:
                raise CapacityExceeded("queue_capacity_exceeded")
            if self._provider_calls >= self.limits.max_provider_concurrency:
                raise CapacityExceeded("provider_busy")
            self._queued_total += 1
            self._reserved_units += 1
            self._active_runs[conversation_id] += 1
            return RunLease(conversation_id)

    def _roll_spend_window_locked(self, monotonic_now: float) -> None:
        if monotonic_now - self._spend_window_start >= self.limits.spend_window_seconds:
            self._spend_window_start = monotonic_now
            self._spend_units = 0

    def start_run(self, lease: RunLease) -> None:
        """Queued -> Active, and where Spend is charged. Charged here rather than at
        reservation because this is the point a Provider call actually happens: a
        request refused, replayed or cancelled before it ever ran must not burn the
        window's budget. Charged before the call rather than after, so a call that
        fails still cost what it cost."""
        with self._index_lock:
            if lease.started or lease.released:
                return
            lease.started = True
            self._queued_total -= 1
            self._active_total += 1
            self._reserved_units -= 1
            # The Provider call starts here, so this is where it is counted.
            self._provider_calls += 1
            # One Provider call is one unit, which is what `spend_unit` declares.
            self._spend_units += 1

    def release_run(self, lease: RunLease) -> None:
        with self._index_lock:
            if lease.released:
                return
            lease.released = True
            if lease.started:
                self._active_total -= 1
                self._provider_calls -= 1
            else:
                self._queued_total -= 1
                # Never started, so it never became a Provider call and never
                # charged; the budget it held open goes back.
                self._reserved_units -= 1
            self._active_runs[lease.conversation_id] -= 1
            if self._active_runs[lease.conversation_id] <= 0:
                del self._active_runs[lease.conversation_id]

    def reserve_stream(self) -> bool:
        with self._index_lock:
            if self._sse_connections >= self.limits.max_sse_connections:
                return False
            self._sse_connections += 1
            return True

    def release_stream(self) -> None:
        with self._index_lock:
            self._sse_connections = max(0, self._sse_connections - 1)

    def check_rate(self, key: object, kind: str, limit: int) -> bool:
        """Fixed 60 s window per (subject, action). ponytail: fixed window, not
        sliding -- a burst can straddle two windows; swap in a sliding window if the
        edge ever matters more than the 3 lines it costs."""
        now = time.monotonic()
        with self._index_lock:
            entry = self._rates.get((key, kind))
            if entry is None and len(self._rates) >= MAX_RATE_KEYS:
                # Evict the oldest tenth, by insertion order, and admit the newcomer.
                # Shedding the newcomer instead would refuse every arriving session
                # while the entries already in the table keep working -- a denial of
                # service aimed at exactly the legitimate traffic.
                # ponytail: amortized O(batch) once per batch of new keys, not a
                # full-table scan per request; a true LRU needs an access order this
                # fixed-window counter does not otherwise keep.
                for stale in list(islice(self._rates, RATE_EVICTION_BATCH)):
                    del self._rates[stale]
            window_start, count = entry or (now, 0)
            if now - window_start >= RATE_WINDOW_SECONDS:
                window_start, count = now, 0
            if count >= limit:
                self._rates[(key, kind)] = (window_start, count)
                return False
            self._rates[(key, kind)] = (window_start, count + 1)
            return True

    def capacity_snapshot(self) -> dict[str, int]:
        """Counts only, for tests and for a load-shed assertion. Deliberately not
        projected to any client: internal ceilings are not public state."""
        with self._index_lock:
            return {
                "conversations": len(self.states),
                "active_runs": self._active_total,
                "queued_runs": self._queued_total,
                "sse_connections": self._sse_connections,
                "provider_calls": self._provider_calls,
                "spend_units": self._spend_units,
                "rate_keys": len(self._rates),
                "expiry_tombstones": len(self._expired_runs),
            }

    # ---- lifecycle --------------------------------------------------------

    def create(self, conversation: Conversation, capability_hash: str) -> None:
        # The Absolute Expiry, translated once into the monotonic clock every
        # enforcement path actually reads. Taken here rather than from `created_at`
        # so an NTP step between accept and now cannot move it.
        expires_monotonic = time.monotonic() + max(
            0.0, (conversation.expires_at - datetime.now(UTC)).total_seconds()
        )
        with self._index_lock:
            if len(self.states) >= self.limits.max_resident_conversations:
                # Refused before anything exists, so no live Run and no completed
                # Conversation is disturbed by a new one being turned away.
                raise CapacityExceeded("conversation_capacity_exceeded")
            self.states[conversation.conversation_id] = ConversationAggregate(
                conversation.conversation_id,
                conversation.created_at,
                conversation.expires_at,
                capability_hash,
                limits=self.limits,
                expires_monotonic=expires_monotonic,
            )
            self._locks[conversation.conversation_id] = Lock()
            self._start_sweeper_locked()

    def _start_sweeper_locked(self) -> None:
        """One Sweeper per Store, started under the same lock that reads `_stop`, so
        a `create` racing a `close` cannot leave a daemon thread running after
        shutdown joined. Started inside the lock too: assigning the field and then
        starting outside it is what lets `close` join a thread that never began."""
        if self._sweeper is not None or self._stop.is_set():
            return
        sweeper = Thread(target=self._sweep_loop, daemon=True, name="aidd-expiry-sweeper")
        try:
            sweeper.start()
        except Exception:
            # Out of threads. Access-time Expiry still holds; leaving the field None
            # means the next `create` tries again rather than the Store silently
            # never sweeping for the rest of its life.
            return
        self._sweeper = sweeper

    def _sweep_loop(self) -> None:
        while not self._stop.wait(SWEEP_INTERVAL_SECONDS):
            try:
                self.sweep(datetime.now(UTC))
            except Exception:
                # Keep sweeping -- one bad Conversation must not stop expiry for the
                # rest -- but never silently: a persistent fault here means nothing
                # is being reclaimed, and that is invisible from the outside.
                _log.exception("expiry sweep failed")

    def sweep(self, now: datetime) -> int:
        """The Periodic half of expiry. Same Absolute Expiry as the Access-time half
        -- it exists only so a Conversation nobody ever opens again is still
        reclaimed. Returns how many it Purged."""
        monotonic_now = time.monotonic()
        with self._index_lock:
            due = [
                conversation_id
                for conversation_id, state in self.states.items()
                if state.is_expired()
            ]
            for key in [
                key
                for key, (window_start, _count) in self._rates.items()
                if monotonic_now - window_start >= RATE_WINDOW_SECONDS
            ]:
                del self._rates[key]
            for run_id in [
                run_id
                for run_id, (reaped_at, _hash, _tail) in self._expired_runs.items()
                if monotonic_now >= reaped_at
            ]:
                del self._expired_runs[run_id]
        return sum(1 for conversation_id in due if self.expire(conversation_id, now))

    def expire(self, conversation_id: UUID, now: datetime) -> bool:
        """Fence, Purge, then cancel -- the Fence and the Purge inside the
        Conversation's own Critical Section, which is what makes them indivisible
        against every commit racing them, and the Provider cancel outside it,
        because a `cancel()` that blocks must not stall every other request on this
        Conversation.

        True only for the caller that actually performed the Purge. That is what
        makes exactly one request the first detector: every other one gets False and
        answers Minimal 404."""
        state, lock = self._state_and_lock(conversation_id)
        if state is None or lock is None:
            return False
        with lock:
            # Re-checked under the lock against the live index, not just the
            # reference we captured: a thread that blocked here while another
            # Purged must not Purge the same Conversation a second time and claim
            # to be the first detector too.
            if self.states.get(conversation_id) is not state or not state.is_expired():
                return False
            capability_hash = state.capability_hash
            # Read BEFORE the Purge: `expire` clears `runs`, so afterwards there is
            # nothing left to observe and the Correlation ID is gone with it.
            observed = (
                None if state.active_run_id is None else state.observe_run(state.active_run_id)
            )
            tail = state.expire(now)
            self._purge_indices(conversation_id, capability_hash, tail)
        if self._purge_observer is not None:
            # Outside the lock, like the cancel below: an observer that blocks must
            # not stall every other request on this Conversation.
            with suppress(Exception):
                self._purge_observer(observed)
        if tail and self._cancel_run is not None:
            # The Provider call is already unable to write anything; this only stops
            # it burning the rest of its Deadline.
            with suppress(Exception):
                self._cancel_run(tail[0].run_id)
        return True

    def _purge_indices(
        self,
        conversation_id: UUID,
        capability_hash: str = "",
        tail: tuple[RunEventV1, ...] = (),
    ) -> None:
        with self._index_lock:
            self.states.pop(conversation_id, None)
            self._locks.pop(conversation_id, None)
            for run_id in [
                run_id
                for run_id, owner in self._run_conversations.items()
                if owner == conversation_id
            ]:
                del self._run_conversations[run_id]
            if tail:
                # Everything else about this Conversation is gone; what stays for a
                # couple of minutes is the two Events an open stream still has to be
                # able to write, under the Capability that stream authenticated with.
                self._expired_runs[tail[0].run_id] = (
                    time.monotonic() + EXPIRY_TOMBSTONE_SECONDS,
                    capability_hash,
                    tail,
                )
        # Rate-limiter entries are keyed by Capability hash and client IP, not by
        # Conversation, and the Sweeper drops them once their window is over.
        #
        # Run capacity is deliberately NOT reclaimed here: the lease belongs to the
        # request that took it and is released in its own `finally`. Releasing it
        # twice would let the ceiling drift upward.

    @contextmanager
    def _conversation(
        self, conversation_id: UUID, capability_hash: str, now: datetime
    ) -> Iterator[ConversationAggregate]:
        """Authorize, enforce Access-time Expiry, then hold the Conversation lock --
        the one entry every Capability-bearing operation goes through.

        Expiry is settled BEFORE the lock is taken, by `expire`, which Purges under
        the lock and cancels the Provider outside it. Exactly one caller wins that
        Purge and raises 410; everyone else raises the Minimal-404
        ConversationNotFound, which is the frozen contract.

        The identity re-check inside the lock is the tombstone: a caller that got
        its Lock object a moment before a Purge dropped it would otherwise wake up
        and operate on an emptied Aggregate that no index points at any more."""
        state = self._authorized_state(conversation_id, capability_hash)
        if state.is_expired():
            raise ConversationExpired if self.expire(conversation_id, now) else ConversationNotFound
        with self._lock(conversation_id):
            if self.states.get(conversation_id) is not state:
                raise ConversationNotFound
            yield state

    def authorize(self, conversation_id: UUID, capability_hash: str, now: datetime) -> None:
        with self._conversation(conversation_id, capability_hash, now):
            return

    def accept_question(
        self,
        conversation_id: UUID,
        capability_hash: str,
        idempotency_key: str,
        digest: str,
        content: str,
        now: datetime,
        deadline_at: datetime | None = None,
        deadline_monotonic: float | None = None,
        correlation_id: UUID | None = None,
    ) -> AcceptQuestionResult:
        # Not defaulted away: the Domain refuses None, which is what keeps
        # ChatApplication the only component that can mint one.
        with self._conversation(conversation_id, capability_hash, now) as state:
            result = state.accept_question(
                idempotency_key,
                digest,
                content,
                now,
                deadline_at,
                deadline_monotonic,
                correlation_id=correlation_id,
            )
            if not result.replayed:
                with self._index_lock:
                    self._run_conversations[result.run.run_id] = conversation_id
            return result

    def accept_retry(
        self,
        conversation_id: UUID,
        capability_hash: str,
        idempotency_key: str,
        digest: str,
        retry_of_run_id: UUID,
        max_attempts: int,
        now: datetime,
        deadline_at: datetime | None = None,
        deadline_monotonic: float | None = None,
        correlation_id: UUID | None = None,
    ) -> AcceptQuestionResult:
        with self._conversation(conversation_id, capability_hash, now) as state:
            result = state.accept_retry(
                idempotency_key,
                digest,
                retry_of_run_id,
                max_attempts,
                now,
                deadline_at,
                deadline_monotonic,
                correlation_id=correlation_id,
            )
            if not result.replayed:
                with self._index_lock:
                    self._run_conversations[result.run.run_id] = conversation_id
            return result

    def record_prepared_request(
        self,
        conversation_id: UUID,
        expected_active_run_id: UUID,
        request: PreparedModelRequestV1,
        dropped_turn_count: int,
    ) -> bool:
        with self._live(conversation_id) as state:
            if state is None:
                return False
            return state.record_prepared_request(
                expected_active_run_id, request, dropped_turn_count
            )

    def replay_question(
        self,
        conversation_id: UUID,
        capability_hash: str,
        idempotency_key: str,
        digest: str,
        now: datetime,
    ) -> RunProjectionV1 | None:
        with self._conversation(conversation_id, capability_hash, now) as state:
            return state.replay_question(idempotency_key, digest, now)

    def mark_running(
        self,
        conversation_id: UUID,
        run_id: UUID,
        now: datetime,
        monotonic_now: float,
        provider_binding_digest: str | None = None,
    ) -> bool:
        with self._live(conversation_id) as state:
            if state is None:
                return False
            return state.mark_running(run_id, now, monotonic_now, provider_binding_digest)

    def get_context_snapshot(
        self, conversation_id: UUID, now: datetime
    ) -> tuple[tuple[PreparedMessageV1, PreparedMessageV1], ...] | None:
        with self._live(conversation_id) as state:
            if state is None:
                return None
            return state.completed_turns(now)

    def commit_context_truncated(
        self, conversation_id: UUID, expected_active_run_id: UUID, dropped_turn_count: int, now: datetime
    ) -> bool:
        with self._live(conversation_id) as state:
            if state is None:
                return False
            return state.commit_context_truncated(expected_active_run_id, dropped_turn_count, now)

    def commit_completed(
        self,
        conversation_id: UUID,
        expected_active_run_id: UUID,
        content: str,
        result: AgentResultV1,
        now: datetime,
    ) -> bool:
        with self._live(conversation_id) as state:
            if state is None:
                return False
            return state.commit_completed(expected_active_run_id, content, result, now)

    def commit_step(
        self, conversation_id: UUID, expected_active_run_id: UUID, step: AgentStepV1, now: datetime
    ) -> bool:
        with self._live(conversation_id) as state:
            if state is None:
                return False
            return state.commit_step(expected_active_run_id, step, now)

    def commit_delta(
        self, conversation_id: UUID, expected_active_run_id: UUID, text: str, now: datetime
    ) -> bool:
        with self._live(conversation_id) as state:
            if state is None:
                return False
            return state.commit_delta(expected_active_run_id, text, now)

    def commit_stream_completed(
        self,
        conversation_id: UUID,
        expected_active_run_id: UUID,
        content: str,
        result: AgentResultV1,
        mismatch_failure: ProviderFailureV1,
        now: datetime,
    ) -> bool:
        with self._live(conversation_id) as state:
            if state is None:
                return False
            return state.commit_stream_completed(
                expected_active_run_id, content, result, mismatch_failure, now
            )

    def commit_failed(
        self,
        conversation_id: UUID,
        expected_active_run_id: UUID,
        failure: ProviderFailureV1,
        now: datetime,
    ) -> bool:
        with self._live(conversation_id) as state:
            if state is None:
                return False
            return state.commit_failed(expected_active_run_id, failure, now)

    def commit_stream_failed(
        self,
        conversation_id: UUID,
        expected_active_run_id: UUID,
        failure: ProviderFailureV1,
        now: datetime,
    ) -> bool:
        with self._live(conversation_id) as state:
            if state is None:
                return False
            return state.commit_stream_failed(expected_active_run_id, failure, now)

    def run_past_deadline(self, conversation_id: UUID, run_id: UUID, monotonic_now: float) -> bool:
        with self._live(conversation_id) as state:
            if state is None:
                return False
            return state.run_past_deadline(run_id, monotonic_now)

    def retry_target(
        self, conversation_id: UUID, capability_hash: str, retry_of_run_id: UUID, now: datetime
    ) -> tuple[PreparedModelRequestV1 | None, int, str, str | None, int]:
        """The Snapshot and question a Retry would re-send, the Binding Digest the
        target Run was actually sent to, and how many Attempts its lineage has
        already used -- all read before anything is accepted so the caller can refuse
        without consuming an Attempt. The lineage count is the same one
        `accept_retry` compares against `max_attempts`; it is read here so the
        caller can order Exhaustion ahead of its own refusals, not so it can
        enforce it."""
        with self._conversation(conversation_id, capability_hash, now) as state:
            target, original = state.retry_target(retry_of_run_id)
            return (
                target.prepared_request,
                target.dropped_turn_count,
                original.content,
                target.provider_binding_digest,
                len(state.lineage_of(target)),
            )

    def commit_timeout(
        self,
        conversation_id: UUID,
        expected_active_run_id: UUID,
        failure: ProviderFailureV1,
        now: datetime,
    ) -> bool:
        with self._live(conversation_id) as state:
            if state is None:
                return False
            return state.commit_timeout(expected_active_run_id, failure, now)

    def commit_cancelled(self, run_id: UUID, capability_hash: str, now: datetime) -> bool:
        """Cancel arrives with a Run ID only, so the reverse index resolves the
        Conversation and the commit runs under that Conversation's own lock -- the
        same Critical Section every sibling terminal commit uses, which is what
        makes the Complete/Cancel race resolve to exactly one winner."""
        with self._index_lock:
            conversation_id = self._run_conversations.get(run_id)
        if conversation_id is None:
            raise ConversationNotFound
        with self._conversation(conversation_id, capability_hash, now) as state:
            return state.commit_cancelled(run_id, now)

    def get_run(self, run_id: UUID, capability_hash: str, now: datetime) -> RunProjectionV1:
        projection, _events = self.get_run_snapshot(run_id, capability_hash, now)
        return projection

    def get_run_snapshot(
        self, run_id: UUID, capability_hash: str, now: datetime
    ) -> tuple[RunProjectionV1, tuple[RunEventV1, ...]]:
        return self.get_run_slice(run_id, capability_hash, 0, now)

    def run_deadline_monotonic(self, run_id: UUID, capability_hash: str) -> float | None:
        """This Run's own monotonic Deadline. None if the Run is unknown, not this
        Capability's, or was built without one."""
        with self._index_lock:
            conversation_id = self._run_conversations.get(run_id)
        if conversation_id is None:
            return None
        with self._live(conversation_id) as state:
            if state is None or not compare_digest(state.capability_hash, capability_hash):
                return None
            run = state.runs.get(run_id)
            return None if run is None else run.deadline_monotonic

    def expired_stream_tail(
        self, run_id: UUID, capability_hash: str, cursor: int
    ) -> "tuple[RunEventV1, ...] | None":
        """The `conversation.expired` -> `stream.end` pair for a Run whose
        Conversation has been Purged, or None if it was never expired (or the
        tombstone is gone).

        The Aggregate built this pair, so its sequence numbers continue that Run's
        own log -- two clients on one Run, or one reconnecting with a stale cursor,
        see the identical two Events. And any reader may collect it, not only the
        one that happened to win the Purge: the frozen contract makes the tail a
        property of the stream being writable, not of who noticed."""
        with self._index_lock:
            entry = self._expired_runs.get(run_id)
        if entry is None or not compare_digest(entry[1], capability_hash):
            return None
        return tuple(event for event in entry[2] if event.sequence > cursor)

    def get_run_slice(
        self, run_id: UUID, capability_hash: str, cursor: int, now: datetime
    ) -> tuple[RunProjectionV1, tuple[RunEventV1, ...]]:
        with self._index_lock:
            conversation_id = self._run_conversations.get(run_id)
        if conversation_id is None:
            raise ConversationNotFound
        with self._conversation(conversation_id, capability_hash, now) as state:
            run = state.runs.get(run_id)
            if run is None:
                raise ConversationNotFound
            return state.projection(run), tuple(run.events[cursor:])

    def _authorized_state(self, conversation_id: UUID, capability_hash: str) -> ConversationAggregate:
        state = self.states.get(conversation_id)
        if state is None or not compare_digest(state.capability_hash, capability_hash):
            raise ConversationNotFound
        return state

    def _lock(self, conversation_id: UUID) -> "Lock":
        """Never a KeyError on a Purged Conversation: a caller that passed
        `_authorized_state` may still be racing the Purge that drops both entries."""
        lock = self._locks.get(conversation_id)
        if lock is None:
            raise ConversationNotFound
        return lock

    def _state_and_lock(
        self, conversation_id: UUID
    ) -> "tuple[ConversationAggregate | None, Lock | None]":
        """Both halves under one lock, so an internal commit racing a Purge sees
        either a usable pair or nothing -- never a live state with a vanished lock."""
        with self._index_lock:
            return self.states.get(conversation_id), self._locks.get(conversation_id)

    def observe_run(self, conversation_id: UUID, run_id: UUID) -> "RunObservationV1 | None":
        """The Run's own Stage, State, Correlation ID and Duration. Capability-free
        because it is an internal read for Telemetry, and read-only, so it cannot
        change what it reports."""
        with self._live(conversation_id) as state:
            return None if state is None else state.observe_run(run_id)

    @contextmanager
    def _live(self, conversation_id: UUID) -> "Iterator[ConversationAggregate | None]":
        """The capability-free half: an internal commit holding the Conversation
        lock still has to confirm the Aggregate is the live one, because a Purge may
        have dropped it while this thread waited. Yields None when it did, and every
        caller answers False to that -- the same answer a lost CAS gets."""
        state, lock = self._state_and_lock(conversation_id)
        if state is None or lock is None:
            yield None
            return
        with lock:
            yield state if self.states.get(conversation_id) is state else None


__all__ = [
    "AcceptQuestionResult",
    "ActiveRunConflict",
    "CapacityExceeded",
    "ConversationExpired",
    "ConversationNotFound",
    "ConversationState",
    "DeploymentLimits",
    "IdempotencyConflict",
    "InMemoryConversationStore",
    "RetryExhausted",
    "RetryNotRetryable",
    "RunLease",
    "RunNotFound",
    "RunObservationV1",
    "TurnLimitReached",
]
