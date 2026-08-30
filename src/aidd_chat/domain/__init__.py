from dataclasses import dataclass, field
from datetime import datetime
import json
import time
from typing import Literal
from uuid import UUID, uuid4

from aidd_chat.contracts import (
    CompletedMessageV1,
    ContextTruncatedEventV1,
    ConversationExpiredEventV1,
    MessageCompletedEventV1,
    MessageDeltaEventV1,
    MessageDiscardedEventV1,
    PreparedMessageV1,
    PreparedModelRequestV1,
    ProviderFailureV1,
    RunErrorEventV1,
    RunEventV1,
    RunProjectionV1,
    RunStage,
    RunState,
    RunStatusEventV1,
    StreamEndEventV1,
    is_valid_model_text,
)


class ConversationExpired(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


class ActiveRunConflict(Exception):
    pass


class RunNotFound(Exception):
    """The Retry target is not a Run of this Conversation. Indistinguishable from
    a foreign Run on purpose -- both leave the boundary as the same Minimal 404."""


class RetryNotRetryable(Exception):
    """The target exists but is not in a state a Retry may re-run (`completed`, or
    still non-terminal)."""


class RetryExhausted(Exception):
    """The lineage already used max_attempts_per_lineage Attempts."""


class CapacityExceeded(Exception):
    """A deployment ceiling refused this request, before any Domain state or
    Provider work was created. `reason` is the Error Code the boundary answers
    with; `retryable` says whether waiting can change the answer."""

    def __init__(self, reason: str, retryable: bool = True) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class TurnLimitReached(Exception):
    """This Conversation already holds max_turns_per_conversation questions. Not an
    error the user can retry away -- the boundary answers 새 대화."""


class OutputLimitExceeded(Exception):
    """The answer, or the Replay Log describing it, hit a deployment ceiling. Not a
    Provider fault, so it is not reported as one. `kind` names the Code its terminal
    carries; the Application also catches this class by name, because a contract
    that only works by duck-typed attribute lookup is one refactor from silence."""

    kind = "capacity_exceeded"


# The terminal Event quadruple (message.discarded, run.error, run.status,
# stream.end) every failing Run still has to be able to write. Delta commits stop
# this far short of the Replay ceiling so a Run that hits it can still *end*
# inside the ceiling instead of being stranded non-terminal.
TERMINAL_EVENT_HEADROOM = 4
# The same headroom in bytes. A terminal quadruple with a Korean failure message
# and four UUIDs serializes to well under this.
TERMINAL_EVENT_BYTE_HEADROOM = 1_024


@dataclass
class RunLease:
    """One reserved Run slot. Reserved before any Domain state exists and returned
    exactly once, in the reserving caller's `finally`. `started` is what separates
    the Queued ceiling from the Active one: a reservation is queued until the Run
    actually runs, and active from then until it is released."""

    conversation_id: UUID
    started: bool = False
    released: bool = False


@dataclass(frozen=True)
class DeploymentLimits:
    """Every capacity ceiling this deployment enforces, in one frozen value so the
    Domain, the Store and the Application all read the same numbers.

    The defaults are deliberately far above anything a deployment would configure:
    they exist so an in-process unit test can build an Aggregate or a Store with no
    deployment behind it. A real process never reaches them -- Bootstrap owns the
    numbers (`DeploymentSettingsV1`), and a process whose settings Bootstrap could
    not validate never becomes Ready, so it refuses questions instead of serving
    them under these."""

    max_resident_conversations: int = 1_000_000
    max_global_active_runs: int = 1_000_000
    max_session_active_runs: int = 1_000_000
    max_queued_runs: int = 1_000_000
    max_turns_per_conversation: int = 1_000_000
    max_output_bytes: int = 1 << 30
    max_replay_events: int = 1_000_000
    max_replay_bytes: int = 1 << 30
    max_sse_connections: int = 1_000_000
    create_rate_per_minute: int = 1_000_000
    submit_rate_per_minute: int = 1_000_000
    poll_rate_per_minute: int = 1_000_000
    retry_rate_per_minute: int = 1_000_000
    cancel_rate_per_minute: int = 1_000_000
    max_requests_per_ip_per_minute: int = 1_000_000
    max_provider_concurrency: int = 1_000_000
    spend_window_seconds: int = 3_600
    spend_limit_units: int = 1_000_000
    # Closed, single-valued for now: the only meter this deployment can actually
    # read is "one Provider call". Story 1.11's billed Provider adds its own unit
    # beside it rather than pretending this one counts money.
    spend_unit: Literal["provider_call"] = "provider_call"
    # Off unless a deployment explicitly says how many proxies sit in front of it.
    # X-Forwarded-For is client-controlled text until then.
    trusted_proxy_hops: int = 0


# Every Event carries the same envelope -- schema_version, a UUID run id, a
# sequence, an ISO timestamp, a type, and for most of them a second UUID. Measured
# once, generously, so the constant can never under-count a real one.
EVENT_ENVELOPE_BYTES = 256


def event_size(event: RunEventV1) -> int:
    """What one Event costs the Replay Log: the fixed envelope plus the exactly
    serialized size of the only field that varies by orders of magnitude.

    Deliberately not `model_dump_json()`: this runs on the per-token delta path, and
    an exact total would re-serialize every Event a second time purely to measure
    it. The text IS serialized exactly, because JSON escaping is what makes a
    ceiling measured on raw bytes overrun -- `\u0001` is one character and six
    bytes. `run.status`, `run.error`, `stream.end` and the `message.completed` echo
    of the whole answer are all counted, which is the part that was missing."""
    text = getattr(event, "text", None)
    if not isinstance(text, str):
        return EVENT_ENVELOPE_BYTES
    return EVENT_ENVELOPE_BYTES + len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def _completed_echo(
    run: "Run", content: str, now: datetime, sequence: int | None = None
) -> RunEventV1:
    """The `message.completed` this Run would write if it finished with `content`.
    Built only to be measured -- the Replay ceiling has to account for the answer
    appearing in the log a second time, escaped."""
    return MessageCompletedEventV1(
        run_id=run.run_id,
        sequence=len(run.events) + 1 if sequence is None else sequence,
        occurred_at=now,
        message_id=run.reserved_output_message_id,
        text=content,
    )


def _record(run: "Run", *events: RunEventV1) -> None:
    """The only way an Event enters a Run's log. Appending anywhere else would let
    the Replay ceiling drift away from what the log really holds."""
    run.events.extend(events)
    run.replay_bytes += sum(event_size(event) for event in events)


# What a process runs under when Bootstrap could not validate its ceilings. Every
# ceiling is zero, not just the ones the Readiness Gate already covers: the Gate
# does not stand in front of Conversation creation, `/api/v1/policy` or the stream
# endpoint, and a misconfigured box is exactly the one that should not be serving
# any of them under a million-wide default nobody chose. `spend_window_seconds` is
# 1 rather than 0 only so the window arithmetic stays well-defined.
UNCONFIGURED_LIMITS = DeploymentLimits(
    **{
        name: (1 if name == "spend_window_seconds" else 0)
        for name, field_ in DeploymentLimits.__dataclass_fields__.items()
        if field_.type is int or field_.type == "int"
    }
)


def expiry_stream_tail(
    run_id: UUID, first_sequence: int, now: datetime
) -> tuple[RunEventV1, RunEventV1]:
    """The two, and only two, Events an expiry may put on a stream, in the one order
    the contract allows.

    Built once, by the Aggregate, at the moment it commits the expiry -- so the
    sequence numbers continue that Run's own log rather than whatever cursor a
    particular client happened to be holding. The Store keeps the pair on a
    tombstone and every reader, however late and whoever won the Purge, is handed
    the identical two Events."""
    return (
        ConversationExpiredEventV1(run_id=run_id, sequence=first_sequence, occurred_at=now),
        StreamEndEventV1(
            run_id=run_id,
            sequence=first_sequence + 1,
            occurred_at=now,
            final_state="expired",
            final_sequence=first_sequence + 1,
        ),
    )


@dataclass(frozen=True)
class Message:
    message_id: UUID
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    state: Literal["completed"] = "completed"


@dataclass
class Run:
    run_id: UUID
    conversation_id: UUID
    input_message_id: UUID
    reserved_output_message_id: UUID
    created_at: datetime
    last_updated_at: datetime
    # Minted once, by ChatApplication, at the moment this Run is accepted, and used
    # unchanged until its Terminal Commit -- including by every ProviderFailureV1
    # this Run reports (see _commit_terminal). Required, with no default: a Run that
    # could mint its own would be a second owner, and a client would then be shown
    # one ID while Telemetry recorded another.
    correlation_id: UUID
    state: RunState = "queued"
    stage: RunStage = "queued"
    retry_of_run_id: UUID | None = None
    output_message_id: UUID | None = None
    terminal_error: ProviderFailureV1 | None = None
    events: list[RunEventV1] = field(default_factory=list)
    raw_buffer: str = ""
    # SHA-256 of the ProviderBindingV1 snapshot this Run was actually sent to.
    # Internal traceability only -- deliberately absent from RunProjectionV1.
    provider_binding_digest: str | None = None
    # The Deadline, twice. `deadline_at` is the wall-clock value, kept for the
    # record; `deadline_monotonic` is the one every enforcement path compares
    # against, because a wall clock can be stepped by NTP and this budget must not
    # fire early or never. None only for a Run built directly in a domain test.
    deadline_at: datetime | None = None
    deadline_monotonic: float | None = None
    # 1 for the original question, +1 per accepted Retry in the same lineage.
    attempt: int = 1
    # The exact request this Run sent, kept so a Retry re-sends it rather than
    # re-selecting History and quietly answering a different question.
    prepared_request: PreparedModelRequestV1 | None = None
    dropped_turn_count: int = 0
    # Serialized size of every Event in `events`, accumulated as they are appended
    # so the ceiling costs one addition per Event rather than a re-walk of the log.
    # Counted over the whole envelope, because that is what actually occupies memory.
    replay_bytes: int = 0

    def past_deadline(self, monotonic_now: float) -> bool:
        """The clock arrives as an argument, like every `now` in this module: the
        one predicate that decides every Run's terminal must be drivable."""
        return self.deadline_monotonic is not None and monotonic_now >= self.deadline_monotonic


def _owned_correlation_id(correlation_id: object) -> None:
    """Refused, not defaulted. `ChatApplication`은 Correlation ID를 유일하게 소유한다:
    an Aggregate that minted its own on a missing argument would be a second owner,
    and the client would then be shown one ID while Telemetry logged another."""
    # v4 specifically: `ProviderFailureV1.correlation_id` requires it, and
    # `_commit_terminal` puts this exact value into that field. Accepting any UUID
    # here would let a v1 through to the public failure envelope.
    if not isinstance(correlation_id, UUID) or correlation_id.version != 4:
        raise ValueError("Run Correlation ID는 ChatApplication이 정한 UUIDv4여야 합니다")


@dataclass(frozen=True)
class RunObservationV1:
    """Everything Telemetry is allowed to know about a Run, read from the Run's own
    fields rather than re-derived by a caller. `duration_ms` is
    acceptance-to-last-commit: `created_at` is set at accept and `last_updated_at` by
    the commit that just ran, so at a terminal it is exactly the frozen contract's
    Acceptance-to-terminal-commit. Wall clock, unlike every Deadline in this process
    -- an NTP step misreports a duration, where it would misfire a Deadline."""

    correlation_id: UUID
    stage: str
    state: str
    error_kind: str | None
    duration_ms: int


@dataclass(frozen=True)
class IdempotencyReceipt:
    digest: str
    run_id: UUID


@dataclass(frozen=True)
class AcceptQuestionResult:
    run: RunProjectionV1
    replayed: bool
    # Only a Retry carries these: the Snapshot the new Run must re-send, and the
    # original question text -- the fallback for a target that failed before it
    # ever built a Snapshot, so the Retry re-selects instead of sending nothing.
    prepared_request: PreparedModelRequestV1 | None = None
    dropped_turn_count: int = 0
    content: str = ""


@dataclass
class ConversationAggregate:
    conversation_id: UUID
    created_at: datetime
    expires_at: datetime
    capability_hash: str
    messages: list[Message] = field(default_factory=list)
    runs: dict[UUID, Run] = field(default_factory=dict)
    receipts: dict[str, IdempotencyReceipt] = field(default_factory=dict)
    active_run_id: UUID | None = None
    limits: DeploymentLimits = field(default_factory=DeploymentLimits)
    # The Absolute Expiry, twice, exactly as Story 1.7 keeps a Run's Deadline:
    # `expires_at` is the wall-clock value kept for the record and shown to the
    # client, `expires_monotonic` is the one every enforcement path compares
    # against. A wall clock can be stepped by NTP or a DST rule, and neither
    # suspending all expiry nor purging every live Conversation at once is an
    # acceptable answer to that. None only for an Aggregate built in a domain test.
    expires_monotonic: float | None = None

    def is_expired(self) -> bool:
        """The one predicate every expiry path -- Access-time, Sweeper, and every
        commit's own fence -- reads, so they cannot disagree about one Conversation.

        No clock argument, unlike `Run.past_deadline`: there is only one clock here
        to pass. A Store-created Aggregate always carries `expires_monotonic`, so a
        caller-supplied wall time could only ever be ignored, and an argument that
        is silently ignored is worse than one that was never offered."""
        if self.expires_monotonic is not None:
            return time.monotonic() >= self.expires_monotonic
        return datetime.now(self.expires_at.tzinfo) >= self.expires_at

    def ensure_current(self, now: datetime) -> None:
        if self.is_expired():
            raise ConversationExpired

    def expire(self, now: datetime) -> tuple[RunEventV1, ...]:
        """The absolute 3,600 s expiry, committed. One Critical Section, in the one
        order that is safe: Fence, then Purge.

        Clearing `active_run_id` first is the Fence -- it is the same CAS every
        terminal commit checks, so every Provider callback still in flight loses it
        and cannot write into a Conversation that is being emptied. Only then are
        Messages, Buffers, Replay Events, Receipts and the Capability dropped.

        Returns the two-Event tail (`conversation.expired` -> `stream.end`) for the
        Run that was still live, because an open stream has to be able to say what
        happened *after* its own log is gone. Nothing else is ever appended: the
        Events are handed out, not stored."""
        events: tuple[RunEventV1, ...] = ()
        run = None if self.active_run_id is None else self.runs.get(self.active_run_id)
        if run is not None:
            events = expiry_stream_tail(run.run_id, len(run.events) + 1, now)
        self.active_run_id = None
        self.messages.clear()
        self.runs.clear()
        self.receipts.clear()
        # Not a valid SHA-256 hex digest, so `compare_digest` can never match it --
        # the Capability is gone even for a caller holding a reference to this
        # Aggregate after the Store dropped it.
        self.capability_hash = ""
        return events

    def accept_question(
        self,
        idempotency_key: str,
        digest: str,
        content: str,
        now: datetime,
        deadline_at: datetime | None = None,
        deadline_monotonic: float | None = None,
        *,
        correlation_id: UUID,
    ) -> AcceptQuestionResult:
        _owned_correlation_id(correlation_id)
        self.ensure_current(now)
        receipt = self.receipts.get(idempotency_key)
        if receipt is not None:
            if receipt.digest != digest:
                raise IdempotencyConflict
            return AcceptQuestionResult(self.projection(self.runs[receipt.run_id]), True)
        self.ensure_no_active_run()
        # Counted here, inside the same Critical Section that creates the Message,
        # so two questions racing into one Conversation cannot both see room for
        # the last Turn. A Retry deliberately does not count: it re-asks a question
        # this Conversation already holds.
        if sum(1 for message in self.messages if message.role == "user") >= self.limits.max_turns_per_conversation:
            raise TurnLimitReached

        input_message = Message(uuid4(), "user", content, now)
        run = Run(
            uuid4(),
            self.conversation_id,
            input_message.message_id,
            uuid4(),
            now,
            now,
            correlation_id=correlation_id,
            deadline_at=deadline_at,
            deadline_monotonic=deadline_monotonic,
        )
        self.messages.append(input_message)
        self.runs[run.run_id] = run
        self.receipts[idempotency_key] = IdempotencyReceipt(digest, run.run_id)
        self.active_run_id = run.run_id
        return AcceptQuestionResult(self.projection(run), False)

    def accept_retry(
        self,
        idempotency_key: str,
        digest: str,
        retry_of_run_id: UUID,
        max_attempts: int,
        now: datetime,
        deadline_at: datetime | None = None,
        deadline_monotonic: float | None = None,
        *,
        correlation_id: UUID,
    ) -> AcceptQuestionResult:
        """`accept_question`'s sibling, through the same receipts/active-run gate so
        a Retry and a new question racing into one Conversation resolve in the same
        Critical Section -- exactly one of them creates a Run.

        No new User Message: the lineage is every Run sharing one `input_message_id`,
        which is also what bounds it. A duplicate or rejected Command returns or
        raises before anything is created, so neither consumes an Attempt."""
        _owned_correlation_id(correlation_id)
        self.ensure_current(now)
        receipt = self.receipts.get(idempotency_key)
        if receipt is not None:
            if receipt.digest != digest:
                raise IdempotencyConflict
            return AcceptQuestionResult(self.projection(self.runs[receipt.run_id]), True)
        # Active-run first, exactly as accept_question does: while a Run is active
        # every command is refused the same way, so neither a Run's existence nor
        # its terminal state is probeable through a Retry.
        self.ensure_no_active_run()
        target, original = self.retry_target(retry_of_run_id)
        lineage = self.lineage_of(target)
        if len(lineage) >= max_attempts:
            raise RetryExhausted

        run = Run(
            uuid4(),
            self.conversation_id,
            target.input_message_id,
            uuid4(),
            now,
            now,
            correlation_id=correlation_id,
            retry_of_run_id=target.run_id,
            deadline_at=deadline_at,
            deadline_monotonic=deadline_monotonic,
            # Reads the lineage's own recorded ordinals rather than counting rows,
            # so the number stays right even beside a Run the lineage lost.
            attempt=max((run.attempt for run in lineage), default=0) + 1,
            prepared_request=target.prepared_request,
            dropped_turn_count=target.dropped_turn_count,
        )
        self.runs[run.run_id] = run
        self.receipts[idempotency_key] = IdempotencyReceipt(digest, run.run_id)
        self.active_run_id = run.run_id
        return AcceptQuestionResult(
            self.projection(run),
            False,
            run.prepared_request,
            run.dropped_turn_count,
            original.content,
        )

    def observe_run(self, run_id: UUID) -> RunObservationV1 | None:
        """Read-only, capability-free, and the only way the Application learns a
        Run's Stage, Terminal State and Correlation ID. Reimplementing any of those
        above the Domain is how a Record ends up disagreeing with the Run it
        describes."""
        run = self.runs.get(run_id)
        if run is None:
            return None
        return RunObservationV1(
            correlation_id=run.correlation_id,
            stage=run.stage,
            state=run.state,
            error_kind=None if run.terminal_error is None else run.terminal_error.kind,
            duration_ms=max(
                0, int((run.last_updated_at - run.created_at).total_seconds() * 1_000)
            ),
        )

    def lineage_of(self, target: Run) -> list[Run]:
        return [run for run in self.runs.values() if run.input_message_id == target.input_message_id]

    def retry_target(self, retry_of_run_id: UUID) -> tuple[Run, Message]:
        """The Run a Retry may re-run, and the question it asked. Read-only, so the
        caller can gate on the Snapshot *before* accepting anything -- a rejected
        Command must not consume an Attempt.

        The target must be the lineage's Tail, the immediately previous Terminal
        Run. Any earlier member is refused: it is not what the user is looking at,
        its Snapshot has been superseded, and branching a lineage would make
        `retry_of_run_id` ambiguous."""
        target = self.runs.get(retry_of_run_id)
        if target is None:
            raise RunNotFound
        original = next(
            (message for message in self.messages if message.message_id == target.input_message_id),
            None,
        )
        if original is None:
            # Without the question there is nothing to re-ask. Indistinguishable
            # from an unknown Run, which is what it effectively is.
            raise RunNotFound
        if target.state not in {"failed", "timeout", "cancelled"}:
            raise RetryNotRetryable
        if target.attempt != max(run.attempt for run in self.lineage_of(target)):
            raise RetryNotRetryable
        return target, original

    def run_past_deadline(self, run_id: UUID, monotonic_now: float) -> bool:
        """Whether a still-live Run has run out of Deadline, asked of the Run's own
        stored `deadline_monotonic` -- the single source of truth. An observer needs
        this *before* it acts, because the Deadline's Cancellation precedes the
        Terminal Commit that confirms it."""
        run = self.runs.get(run_id)
        return (
            run is not None
            and self.active_run_id == run_id
            and run.state in {"queued", "running"}
            and run.past_deadline(monotonic_now)
        )

    def record_prepared_request(
        self,
        expected_active_run_id: UUID,
        request: PreparedModelRequestV1,
        dropped_turn_count: int,
    ) -> bool:
        """Keep what this Run actually sent, so a Retry re-sends the same bytes."""
        run = self.runs.get(expected_active_run_id)
        if run is None or self.active_run_id != expected_active_run_id:
            return False
        run.prepared_request = request
        run.dropped_turn_count = dropped_turn_count
        return True

    def replay_question(self, idempotency_key: str, digest: str, now: datetime) -> RunProjectionV1 | None:
        self.ensure_current(now)
        receipt = self.receipts.get(idempotency_key)
        if receipt is None:
            self.ensure_no_active_run()
            return None
        if receipt.digest != digest:
            raise IdempotencyConflict
        return self.projection(self.runs[receipt.run_id])

    def ensure_no_active_run(self) -> None:
        if self.active_run_id is not None and self.runs[self.active_run_id].state in {"queued", "running"}:
            raise ActiveRunConflict

    def completed_turns(
        self, now: datetime
    ) -> tuple[tuple[PreparedMessageV1, PreparedMessageV1], ...] | None:
        """Completed User+Assistant Pairs only, oldest first. A Run only reaches
        `completed` once both its input and output Messages exist, so this
        naturally excludes the in-flight current Run and any discarded/unmatched
        (failed) Turn without needing to inspect adjacency in `self.messages`.
        None (not an empty tuple) signals expiry, like every sibling commit's
        `now >= self.expires_at` guard -- a valid, merely-empty History must stay
        distinguishable from a Conversation the caller must fail instead."""
        if self.is_expired():
            return None
        by_id = {message.message_id: message for message in self.messages}
        # Position is the question's own place in `self.messages`, not a timestamp:
        # two Runs accepted inside one clock tick would otherwise sort arbitrarily.
        order = {message_id: index for index, message_id in enumerate(by_id)}
        # One Turn per `input_message_id`, not per completed Run: a Retry shares the
        # original's input Message, so both completing would put the same question in
        # History twice. The most recently completed Run wins the answer.
        lineages: dict[UUID, tuple[int, tuple[datetime, int], PreparedMessageV1, PreparedMessageV1]] = {}
        for run in self.runs.values():
            if run.state != "completed" or run.output_message_id is None:
                continue
            user = by_id.get(run.input_message_id)
            assistant = by_id.get(run.output_message_id)
            if user is None or assistant is None:
                continue
            previous = lineages.get(run.input_message_id)
            # Positioned by the question, never by a Run: the Run that first asked it
            # may have failed, and a Retry created later must not move the Turn after
            # questions the user asked in between.
            position = order[run.input_message_id]
            # (completed_at, attempt): two Runs of one lineage can complete inside
            # a single clock tick, and on a tie the later Attempt is the answer the
            # user asked for.
            if previous is not None and previous[1] >= (run.last_updated_at, run.attempt):
                continue
            lineages[run.input_message_id] = (
                position,
                (run.last_updated_at, run.attempt),
                PreparedMessageV1(user.message_id, "user", user.content),
                PreparedMessageV1(assistant.message_id, "assistant", assistant.content),
            )
        entries = sorted(lineages.values(), key=lambda entry: entry[0])
        return tuple((user, assistant) for _position, _completed_at, user, assistant in entries)

    def mark_running(
        self,
        run_id: UUID,
        now: datetime,
        monotonic_now: float,
        provider_binding_digest: str | None = None,
    ) -> bool:
        if self.is_expired():
            return False
        run = self.runs.get(run_id)
        if run is None or self.active_run_id != run_id or run.state != "queued":
            return False
        if run.past_deadline(monotonic_now):
            # Past its Deadline before it ever started: no Provider call may begin.
            # The caller reacts to False by committing `timeout` (a Cancel that won
            # the CAS first lands on the same call and simply loses it).
            return False
        if provider_binding_digest is not None:
            run.provider_binding_digest = provider_binding_digest
        run.state = "running"
        run.stage = "streaming"
        run.last_updated_at = now
        _record(
            run,
            RunStatusEventV1(
                run_id=run.run_id,
                sequence=1,
                occurred_at=now,
                state="running",
                stage="streaming",
            ),
        )
        return True

    def commit_context_truncated(
        self, expected_active_run_id: UUID, dropped_turn_count: int, now: datetime
    ) -> bool:
        """Fires at most once per Run, after mark_running and before the first
        message.delta -- the caller enforces that ordering by calling this right
        after context selection and before invoking the Provider."""
        if self.is_expired():
            return False
        run = self.runs.get(expected_active_run_id)
        if run is None or self.active_run_id != expected_active_run_id or run.state != "running":
            return False
        if any(event.type == "context.truncated" for event in run.events):
            return False
        run.last_updated_at = now
        _record(
            run,
            ContextTruncatedEventV1(
                run_id=run.run_id,
                sequence=len(run.events) + 1,
                occurred_at=now,
                dropped_turn_count=dropped_turn_count,
            ),
        )
        return True

    def commit_delta(self, expected_active_run_id: UUID, text: str, now: datetime) -> bool:
        if self.is_expired() or not is_valid_model_text(text):
            return False
        run = self.runs.get(expected_active_run_id)
        if run is None or self.active_run_id != expected_active_run_id or run.state != "running":
            return False
        event = MessageDeltaEventV1(
            run_id=run.run_id,
            sequence=len(run.events) + 1,
            occurred_at=now,
            message_id=run.reserved_output_message_id,
            text=text,
        )
        # Built first, then measured: the ceiling is over the serialized Event, and
        # guessing its size from the text alone is how a ceiling ends up bounding a
        # fraction of what it was meant to bound. `echoed` is what the eventual
        # message.completed will add -- the whole answer, escaped, a second time.
        self._ensure_within_output_and_replay_limits(
            run, text, event, event_size(_completed_echo(run, run.raw_buffer + text, now))
        )
        run.raw_buffer += text
        run.last_updated_at = now
        _record(run, event)
        return True

    def _ensure_within_output_and_replay_limits(
        self, run: Run, text: str, event: RunEventV1, echoed: int
    ) -> None:
        """Refused, never truncated: a partial answer must not become a completed
        Message. Raising rather than returning False is what lets the boundary name
        the ceiling instead of blaming the Provider for something it did not do.

        Both headrooms are what make `Run을 상한 안에서 종료` true: whatever is
        admitted here must still leave room for the terminal quadruple, and for the
        `message.completed` echo that repeats the whole answer a second time --
        measured serialized, because that echo is JSON-escaped in the log."""
        if len(run.raw_buffer.encode("utf-8")) + len(text.encode("utf-8")) > self.limits.max_output_bytes:
            raise OutputLimitExceeded
        if len(run.events) + 1 + TERMINAL_EVENT_HEADROOM > self.limits.max_replay_events:
            raise OutputLimitExceeded
        projected = (
            run.replay_bytes + event_size(event) + echoed + TERMINAL_EVENT_BYTE_HEADROOM
        )
        if projected > self.limits.max_replay_bytes:
            raise OutputLimitExceeded

    def commit_completed(self, expected_active_run_id: UUID, content: str, now: datetime) -> bool:
        if self.is_expired() or not is_valid_model_text(content, require_visible=True):
            return False
        run = self.runs.get(expected_active_run_id)
        if (
            run is None
            or self.active_run_id != expected_active_run_id
            or run.state not in {"queued", "running"}
        ):
            return False
        # The non-streaming path never passed through commit_delta, so this is the
        # only place a `complete`-only Provider's answer is ever sized -- and that is
        # the path the real Ollama binding can take. All three ceilings, not just the
        # output one: this Run is about to append four Events, one of which repeats
        # the entire answer.
        completed_sequence = len(run.events) + 1
        echo = _completed_echo(run, content, now, sequence=completed_sequence)
        if len(content.encode("utf-8")) > self.limits.max_output_bytes:
            raise OutputLimitExceeded
        if completed_sequence + 2 > self.limits.max_replay_events:
            raise OutputLimitExceeded
        if (
            run.replay_bytes + event_size(echo) + TERMINAL_EVENT_BYTE_HEADROOM
            > self.limits.max_replay_bytes
        ):
            raise OutputLimitExceeded
        assistant = Message(run.reserved_output_message_id, "assistant", content, now)
        self.messages.append(assistant)
        run.state = "completed"
        run.stage = "terminal"
        run.output_message_id = assistant.message_id
        run.last_updated_at = now
        _record(
            run,
            MessageCompletedEventV1(
                run_id=run.run_id,
                sequence=completed_sequence,
                occurred_at=now,
                message_id=assistant.message_id,
                text=content,
            ),
            RunStatusEventV1(
                run_id=run.run_id,
                sequence=completed_sequence + 1,
                occurred_at=now,
                state="completed",
                stage="terminal",
            ),
            StreamEndEventV1(
                run_id=run.run_id,
                sequence=completed_sequence + 2,
                occurred_at=now,
                final_state="completed",
                final_sequence=completed_sequence + 2,
            ),
        )
        self.active_run_id = None
        return True

    def commit_stream_completed(
        self,
        expected_active_run_id: UUID,
        content: str,
        mismatch_failure: ProviderFailureV1,
        now: datetime,
    ) -> bool:
        if self.is_expired():
            return False
        run = self.runs.get(expected_active_run_id)
        if (
            run is None
            or self.active_run_id != expected_active_run_id
            or run.state != "running"
        ):
            return False
        if (
            not run.raw_buffer
            or run.raw_buffer != content
            or not is_valid_model_text(content, require_visible=True)
        ):
            return self._commit_failed(run, mismatch_failure, now)
        return self.commit_completed(expected_active_run_id, content, now)

    def commit_failed(
        self,
        expected_active_run_id: UUID,
        failure: ProviderFailureV1,
        now: datetime,
    ) -> bool:
        if self.is_expired():
            return False
        run = self.runs.get(expected_active_run_id)
        if (
            run is None
            or self.active_run_id != expected_active_run_id
            or run.state not in {"queued", "running"}
        ):
            return False
        return self._commit_failed(run, failure, now)

    def commit_stream_failed(
        self, expected_active_run_id: UUID, failure: ProviderFailureV1, now: datetime
    ) -> bool:
        run = self.runs.get(expected_active_run_id)
        if run is None or self.active_run_id != expected_active_run_id or run.state != "running":
            return False
        return self._commit_failed(run, failure, now)

    def commit_cancelled(self, expected_active_run_id: UUID, now: datetime) -> bool:
        """`_commit_failed` without the failure: no `run.error` Event and no
        `terminal_error`, because a stop the user asked for is not an error.
        Whichever of Complete/Cancel reaches the state CAS first wins, and clearing
        `active_run_id` is what fences every later Provider Delta and terminal
        commit out.

        The `expected_active_run_id` CAS is the sibling commits' own: `active_run_id`
        is set at accept and cleared only by a terminal commit, so a non-terminal Run
        is always the active one and False still means exactly `already terminal` --
        which is what stops `cancel_run` answering `already_terminal` beside a Run
        that is still going."""
        if self.is_expired():
            return False
        run = self.runs.get(expected_active_run_id)
        if (
            run is None
            or self.active_run_id != expected_active_run_id
            or run.state not in {"queued", "running"}
        ):
            return False
        run.state = "cancelled"
        run.stage = "terminal"
        run.raw_buffer = ""
        run.last_updated_at = now
        first = len(run.events) + 1
        _record(
            run,
            MessageDiscardedEventV1(
                run_id=run.run_id,
                sequence=first,
                occurred_at=now,
                message_id=run.reserved_output_message_id,
            ),
            RunStatusEventV1(
                run_id=run.run_id,
                sequence=first + 1,
                occurred_at=now,
                state="cancelled",
                stage="terminal",
            ),
            StreamEndEventV1(
                run_id=run.run_id,
                sequence=first + 2,
                occurred_at=now,
                final_state="cancelled",
                final_sequence=first + 2,
            ),
        )
        self.active_run_id = None
        return True

    def commit_timeout(
        self, expected_active_run_id: UUID, failure: ProviderFailureV1, now: datetime
    ) -> bool:
        """`commit_failed` with `state="timeout"`: same Event quadruple, same CAS,
        same discard of the uncommitted output. `queued` is accepted too -- a Run
        that never started still has to reach a terminal when its Deadline passes."""
        if self.is_expired():
            return False
        run = self.runs.get(expected_active_run_id)
        if (
            run is None
            or self.active_run_id != expected_active_run_id
            or run.state not in {"queued", "running"}
        ):
            return False
        return self._commit_terminal(run, failure, "timeout", now)

    def _commit_failed(self, run: Run, failure: ProviderFailureV1, now: datetime) -> bool:
        return self._commit_terminal(run, failure, "failed", now)

    def _commit_terminal(
        self,
        run: Run,
        failure: ProviderFailureV1,
        state: Literal["failed", "timeout"],
        now: datetime,
    ) -> bool:
        if self.is_expired():
            return False
        # The Run owns the Correlation ID from acceptance to here. Every caller that
        # builds a ProviderFailureV1 -- the generation task, a poll's Deadline
        # enforcement, the Sweeper's canceller -- mints a placeholder, and this is the
        # one place all of them pass through, so the value a client is shown is always
        # the value this Run has carried since it was accepted.
        # model_validate, not model_copy(update=...): the latter SKIPS validators,
        # which is how a non-v4 UUID would reach the public failure envelope
        # unchecked -- the same bypass `binding_digest` re-validates against.
        failure = ProviderFailureV1.model_validate(
            {**failure.model_dump(), "correlation_id": run.correlation_id}
        )
        run.state = state
        run.stage = "terminal"
        run.terminal_error = failure
        run.raw_buffer = ""
        run.last_updated_at = now
        first = len(run.events) + 1
        _record(
            run,
            MessageDiscardedEventV1(
                run_id=run.run_id,
                sequence=first,
                occurred_at=now,
                message_id=run.reserved_output_message_id,
            ),
            RunErrorEventV1(
                run_id=run.run_id,
                sequence=first + 1,
                occurred_at=now,
                error=failure,
            ),
            RunStatusEventV1(
                run_id=run.run_id,
                sequence=first + 2,
                occurred_at=now,
                state=state,
                stage="terminal",
            ),
            StreamEndEventV1(
                run_id=run.run_id,
                sequence=first + 3,
                occurred_at=now,
                final_state=state,
                final_sequence=first + 3,
            ),
        )
        self.active_run_id = None
        return True

    def projection(self, run: Run) -> RunProjectionV1:
        output = None
        if run.output_message_id is not None:
            message = next(item for item in self.messages if item.message_id == run.output_message_id)
            output = CompletedMessageV1(message_id=message.message_id, content=message.content)
        return RunProjectionV1(
            run_id=run.run_id,
            conversation_id=run.conversation_id,
            input_message_id=run.input_message_id,
            retry_of_run_id=run.retry_of_run_id,
            output_message_id=run.output_message_id,
            output_message=output,
            state=run.state,
            stage=run.stage,
            created_at=run.created_at,
            last_updated_at=run.last_updated_at,
            latest_sequence=len(run.events),
            terminal_error=run.terminal_error,
        )
