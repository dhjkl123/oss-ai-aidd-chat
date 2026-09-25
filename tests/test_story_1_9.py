"""Story 1.9 -- 비신뢰 Model 출력, Content-free 관측, Deployment Profile 경계.

Three contracts that all say "prove the absence of something".

Model output is hostile data. The Validator's job is to REFUSE dangerous code
points, not to rewrite an answer: `<script>` and `javascript:` are ordinary text
here, and the proof that they stay text lives in the browser suite. What this file
fixes is that Streaming and completed go through the *same function*, so the two
paths cannot drift apart -- and that the Domain commits share it, so a Run refuses
hostile text as `provider_invalid_response` rather than as an escaping ValidationError.

Telemetry is a closed Record. The tests for it are not "the right things appear" but
"nothing else can", and -- the part that is easy to get wrong -- that the wiring is
actually connected: a Record asserted by calling the recorder directly says nothing
about whether the route, the Sweeper or `lifespan` reaches it.

`local_test` is the only Deployment Profile: `public_demo` is retired (PRD C-7.3) and
refused like any unknown value.
"""

import ast
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta
import inspect
import logging
from pathlib import Path
from threading import Event
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aidd_chat.adapters import DETERMINISTIC_BINDING, DeploymentLimits, InMemoryConversationStore
from aidd_chat.application import (
    MAX_TELEMETRY_RECORDS,
    READINESS_CODES,
    AgentRunError,
    ChatApplication,
    RunTelemetryV1,
)
from aidd_chat.bootstrap import (
    BindingConfigurationError,
    ProviderSettings,
    UnboundProvider,
    build_chat_application,
    build_provider,
)
from aidd_chat.contracts import (
    MessageCompletedEventV1,
    MessageDeltaEventV1,
    RunProjectionV1,
    validate_model_text,
    verify_request_integrity,
)
from pydantic import SecretStr, ValidationError
from aidd_chat.domain import ConversationAggregate
import aidd_chat.main as main_module
from aidd_chat.main import WEB_ROOT, app, get_chat_application
from agent_fakes import GROUNDED_RESULT, LegacySyncAgent
from aidd_chat.adapters import FakeAgent


ORIGIN = {"Origin": "https://testserver"}
QUESTION = "적대적 출력을 확인하는 질문"
HOSTILE = '<script>alert(1)</script><img src=x onerror=alert(2)> [클릭](javascript:alert(3)) data:text/html,<b>'
SRC_ROOT = Path(__file__).parents[1] / "src" / "aidd_chat"


def _now() -> datetime:
    return datetime.now(UTC)


def _application(provider=None, **limits) -> ChatApplication:
    return ChatApplication(
        InMemoryConversationStore(),
        provider or FakeAgent(),
        3,
        DeploymentLimits(**limits),
    )


def _run(application: ChatApplication, content: str = QUESTION):
    conversation, capability = application.create_conversation()
    run = application.submit_question(conversation.conversation_id, capability, "k", content)
    application.wait_for_generations(5)
    return application.get_run(run.run_id, capability), capability


def _terminals(application: ChatApplication) -> list[RunTelemetryV1]:
    return [r for r in application.telemetry_snapshot() if r.terminal_state is not None]


class HostileStreamingProvider(LegacySyncAgent):
    """Streams an adversarial answer one hostile fragment at a time, so the Deltas
    and the completed echo carry the same text through two different code paths."""

    policy_metadata = FakeAgent().policy_metadata
    binding = DETERMINISTIC_BINDING

    def __init__(self, answer: str = HOSTILE, chunks: int = 4) -> None:
        self.answer = answer
        self.chunks = chunks

    def probe(self) -> bool:
        return True

    def stream(self, request, on_delta, handle=None) -> str:
        verify_request_integrity(request, self)
        size = max(1, len(self.answer) // self.chunks)
        for start in range(0, len(self.answer), size):
            on_delta(self.answer[start : start + size])
        return self.answer


class MismatchedStreamProvider(LegacySyncAgent):
    """Streams one answer and returns a different one. `commit_stream_completed`
    answers True for this -- it commits the mismatch FAILURE -- which is why the
    terminal has to be read back rather than inferred from what was streamed."""

    policy_metadata = FakeAgent().policy_metadata
    binding = DETERMINISTIC_BINDING

    def probe(self) -> bool:
        return True

    def stream(self, request, on_delta, handle=None) -> str:
        verify_request_integrity(request, self)
        on_delta("스트리밍한 답변")
        return "스트리밍한 답변과 다른 답변"


class HostileCompleteOnlyProvider(LegacySyncAgent):
    """No `stream`: the whole answer comes back from `complete()` and the shim hands
    it to the Application as one delta, so the Application's refusal is what is
    tested, not the fake's."""

    policy_metadata = FakeAgent().policy_metadata
    binding = DETERMINISTIC_BINDING

    def __init__(self, answer: str) -> None:
        self.answer = answer

    def probe(self) -> bool:
        return True

    def complete(self, request, handle=None) -> str:
        verify_request_integrity(request, self)
        return self.answer


class ToolCallProvider(LegacySyncAgent):
    """Answers with something that is not Text. The adapter's own ladder maps a real
    Tool Call to `provider_non_text`; this raises the same typed failure so the
    Run-level contract can be asserted without a live Provider."""

    policy_metadata = FakeAgent().policy_metadata
    binding = DETERMINISTIC_BINDING

    def probe(self) -> bool:
        return True

    def stream(self, request, on_delta, handle=None) -> str:
        verify_request_integrity(request, self)
        raise AgentRunError("provider_non_text")


class BlockingProvider(LegacySyncAgent):
    """Streams one delta, then hangs until cancelled -- the live Run a Cancel and a
    Deadline both have to be observed against. `cancel_raises` is the Story 1.6
    silent path: a Provider whose stop request itself fails."""

    policy_metadata = FakeAgent().policy_metadata
    binding = DETERMINISTIC_BINDING

    def __init__(self, cancel_raises: bool = False) -> None:
        self.started = Event()
        self.release = Event()
        self.cancel_raises = cancel_raises

    def probe(self) -> bool:
        return True

    def new_call_handle(self):
        provider = self

        class Handle:
            def cancel(self) -> None:
                # Released first either way: a handle that raises before letting the
                # call end would wedge the test rather than exercise the path.
                provider.release.set()
                if provider.cancel_raises:
                    raise RuntimeError("cancel failed")

        return Handle()

    def stream(self, request, on_delta, handle=None) -> str:
        verify_request_integrity(request, self)
        on_delta("부분 ")
        self.started.set()
        self.release.wait(5)
        return "부분 "


# --------------------------------------------------------------------------
# Untrusted Model output
# --------------------------------------------------------------------------


def test_streaming_and_completed_share_one_validator() -> None:
    """The enforcement point for "두 경로가 같은 결과를 낸다": both Events call the
    same function, so there is no second implementation that could drift."""
    delta = inspect.getsource(MessageDeltaEventV1.validate_text.__func__)
    completed = inspect.getsource(MessageCompletedEventV1.validate_text.__func__)
    assert "validate_model_text(" in delta and "validate_model_text(" in completed


def test_no_module_has_its_own_copy_of_the_hostile_text_rule() -> None:
    """The gate that protects "one Validator" has to be module-wide, not a reading of
    the two Event validators: a THIRD text path added anywhere -- a new adapter, a new
    command -- is exactly what a two-validator check cannot see.

    A hand-rolled surrogate scan is the specific shape that keeps reappearing, so it
    is banned by source; `has_unsafe_code_point` is where the rule lives."""
    for path in SRC_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if path.name == "__init__.py" and path.parent.name == "contracts":
            continue  # the one definition
        for banned in ("0xD800", "0xDFFF", "surrogatepass", "isprintable()"):
            assert banned not in source, f"{path.name}: {banned}"


HOSTILE_TEXTS = [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    "javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD4=",
    "vbscript:msgbox(1)",
    "</p><iframe src=//evil></iframe>",
]

# Every class the widened Validator refuses. Written as escapes on purpose: three of
# them are invisible in a source file, which is the whole reason they are here.
# Text that must be ACCEPTED, and the reason the set is not wider. Every one of
# these failed the whole Run as `provider_invalid_response` while variation
# selectors and the zero width joiner were refused -- ❤️ and ⚠️ are everyday
# content, and ZWJ is not a smuggling character, it is how family and flag emoji are
# COMPOSED. A validator that refuses a model for saying 좋아요 ❤️ is worse than the
# threat it was added for.
ACCEPTED_TEXTS = [
    "좋아요 \u2764\ufe0f",                                  # heart + VS16
    "주의 \u26a0\ufe0f",                                    # warning + VS16
    "가족 \U0001f468\u200d\U0001f469\u200d\U0001f467",      # family, ZWJ-composed
    "깃발 \U0001f3f3\ufe0f\u200d\U0001f308",                # rainbow flag, VS16 + ZWJ
    "\u0928\u092e\u0938\u094d\u200d\u0924\u0947",          # Devanagari, ZWJ is meaning
    "\u0645\u06cc\u200c\u0631\u0648\u0645",                # Persian, ZWNJ is meaning
]

UNSAFE_TEXTS = [
    "정상\x00숨김",            # NUL
    "정상\x1b[31m색",          # ESC / ANSI
    "정상\x7f",                # DEL
    "정상\x85다음줄",           # C1
    "정상\u00ad하이픈",         # soft hyphen
    "정상\u180e몽골",           # Mongolian vowel separator
    "정상\u061c아랍",           # arabic letter mark
    "정상\u200b숨김",           # zero width space
    "정상\u200e좌우",           # LRM
    "정상\u200f우좌",           # RLM
    "정상\u202e역순",           # bidi override
    "정상\u2060결합",           # word joiner
    "정상\u2064보이지않는",      # invisible plus
    "정상\u2066고립",           # bidi isolate
    "정상\u2028줄",             # line separator
    "정상\u2029문단",           # paragraph separator
    "정상\ufeff바이트순서",      # BOM / zero width no-break space
    "정상\U000e0041숨김",       # Tags block
    "정상\ud800",              # lone surrogate
]


@pytest.mark.parametrize("text", HOSTILE_TEXTS)
@pytest.mark.release_suite
def test_hostile_markup_is_text_not_an_error(text: str) -> None:
    """Validator는 내용을 바꾸지 않는다: rendering is `textContent`, so the danger is
    execution, not the letters `<script>`. Refusing or rewriting these would be the
    정적·축소·대체 답변 금지 violation, so both paths accept them unchanged."""
    assert validate_model_text(text) == text
    assert validate_model_text(text, require_visible=True) == text
    assert MessageDeltaEventV1(
        run_id=uuid4(), sequence=1, occurred_at=_now(), message_id=uuid4(), text=text
    ).text == text
    assert MessageCompletedEventV1(
        run_id=uuid4(), sequence=1, occurred_at=_now(), message_id=uuid4(), text=text
    ).text == text


@pytest.mark.parametrize("text", ACCEPTED_TEXTS)
def test_composed_emoji_and_script_joiners_are_ordinary_text(text: str) -> None:
    """The predicate half. Variation selectors and ZWJ/ZWNJ are how ordinary text is
    composed, not how it is smuggled -- refusing them fails a whole answer for
    saying 좋아요 ❤️, which is a worse outcome than the threat they were added for."""
    assert validate_model_text(text, require_visible=True) == text
    assert MessageDeltaEventV1(
        run_id=uuid4(), sequence=1, occurred_at=_now(), message_id=uuid4(), text=text
    ).text == text
    assert MessageCompletedEventV1(
        run_id=uuid4(), sequence=1, occurred_at=_now(), message_id=uuid4(), text=text
    ).text == text
    # The user's own question is held to the same rule, so it must accept them too.
    from aidd_chat.contracts import QuestionCommand

    assert QuestionCommand(kind="question", content=text).content == text


@pytest.mark.parametrize("text", ACCEPTED_TEXTS)
def test_composed_emoji_survive_a_real_run_on_both_paths(text: str) -> None:
    """The half that matters: a whole Run, streamed and complete-only, ending
    `completed` with the answer stored byte-for-byte. The predicate agreeing is not
    the same fact as a user getting their answer."""
    for provider in (HostileStreamingProvider(text), HostileCompleteOnlyProvider(text)):
        application = _application(provider)
        try:
            run, _capability = _run(application)
            assert run.state == "completed", (type(provider).__name__, run.terminal_error)
            assert run.output_message.content == text
        finally:
            application.shutdown()


@pytest.mark.parametrize("text", UNSAFE_TEXTS)
def test_dangerous_code_points_are_refused_identically_on_both_paths(text: str) -> None:
    with pytest.raises(ValueError):
        validate_model_text(text)
    with pytest.raises(ValueError):
        validate_model_text(text, require_visible=True)
    with pytest.raises(Exception):
        MessageDeltaEventV1(
            run_id=uuid4(), sequence=1, occurred_at=_now(), message_id=uuid4(), text=text
        )
    with pytest.raises(Exception):
        MessageCompletedEventV1(
            run_id=uuid4(), sequence=1, occurred_at=_now(), message_id=uuid4(), text=text
        )


@pytest.mark.parametrize("text", UNSAFE_TEXTS)
def test_a_user_question_is_held_to_the_same_rule(text: str) -> None:
    """A question is rendered into the same transcript by the same `textContent`
    path, so a bidi override typed by the user reorders the line exactly as one
    emitted by a Model would. Same predicate; only the sentence differs."""
    from aidd_chat.contracts import QuestionCommand

    with pytest.raises(Exception):
        QuestionCommand(kind="question", content=text)
    # And an ordinary question is still a question.
    assert QuestionCommand(kind="question", content=QUESTION).content == QUESTION


def test_the_run_vocabularies_are_spelled_once() -> None:
    """Stage and Terminal State exist in the Domain, in the projection and in the
    Record. Derived from one Literal, so a sixth Stage cannot be added to one and
    missed by the others."""
    from typing import get_args

    from aidd_chat.application import RUN_STAGES, TERMINAL_RUN_STATES
    from aidd_chat.contracts import RunStage, TerminalRunState

    assert RUN_STAGES == get_args(RunStage)
    assert TERMINAL_RUN_STATES == get_args(TerminalRunState)
    # The Domain and the public projection carry the same alias, not a copy.
    assert get_args(RunProjectionV1.model_fields["stage"].annotation) == RUN_STAGES


def test_a_non_string_is_refused_rather_than_raising_a_type_error() -> None:
    """The Domain commits answer False, not raise. A TypeError escaping one of them
    would leave a Run non-terminal with no run.error and no stream.end."""
    from aidd_chat.contracts import is_valid_model_text

    for value in (None, 5, b"bytes", object()):
        assert is_valid_model_text(value) is False


def test_tab_newline_and_carriage_return_stay_admissible() -> None:
    """A streamed answer is committed raw, so a CRLF answer must not become a
    terminal failure just because the Validator got stricter."""
    assert validate_model_text("첫 줄\r\n둘째\t줄", require_visible=True)


def test_a_hostile_answer_completes_and_is_stored_verbatim() -> None:
    """End to end: adversarial text streams, commits and comes back byte-identical.
    The Validator refused nothing, because nothing here is executable as text."""
    application = _application(HostileStreamingProvider())
    try:
        run, _capability = _run(application)
        assert run.state == "completed"
        assert run.output_message.content == HOSTILE
    finally:
        application.shutdown()


def test_a_tool_call_fails_the_run_as_provider_non_text() -> None:
    application = _application(ToolCallProvider())
    try:
        run, _capability = _run(application)
        assert run.state == "failed"
        assert run.terminal_error.kind == "provider_non_text"
    finally:
        application.shutdown()


def test_the_web_client_never_uses_an_html_sink() -> None:
    """Source Gate, deliberately: `textContent` everywhere is what makes the CSP the
    second line of defence rather than the only one, and one `innerHTML =` added
    later would silently undo the whole contract."""
    # markdown.js (Task 25) turns model text into an AST the page builds with
    # textContent, so it is held to the same rule as app.js.
    script = "".join((WEB_ROOT / name).read_text(encoding="utf-8") for name in ("app.js", "markdown.js"))
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script, sink
    # No Link is ever constructed from Model output, so a `javascript:` URL in an
    # answer has nothing to become.
    assert 'createElement("a")' not in script and "createElement('a')" not in script


def test_the_csp_blocks_inline_and_foreign_script() -> None:
    with TestClient(app, base_url="https://testserver") as client:
        policy = client.get("/").headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "'unsafe-inline'" not in policy and "'unsafe-eval'" not in policy
    assert "object-src 'none'" in policy and "base-uri 'none'" in policy


# --------------------------------------------------------------------------
# Content-free Telemetry
# --------------------------------------------------------------------------


def test_the_telemetry_record_is_closed_to_exactly_the_allowlist() -> None:
    """Free-form Message를 받는 순간 Allowlist는 무너진다: the Record is a frozen
    dataclass whose field set IS the Allowlist -- and whose vocabularies are checked
    at runtime, because a Literal annotation is not."""
    assert is_dataclass(RunTelemetryV1)
    assert [field.name for field in fields(RunTelemetryV1)] == [
        "correlation_id",
        "stage",
        "duration_ms",
        "terminal_state",
        "error_class",
    ]
    with pytest.raises(Exception):
        RunTelemetryV1(uuid4(), "terminal", 1, "completed", None).stage = "queued"
    # A typo, a fabricated Stage, or a caller handing over Provider text.
    for bad in (
        {"stage": "runnning"},
        {"stage": "ready"},
        {"terminal_state": "expired"},
        {"error_class": "Provider가 응답하지 않았습니다"},
        {"correlation_id": "not-a-uuid"},
        {"duration_ms": "12"},
    ):
        values = {
            "correlation_id": uuid4(),
            "stage": "terminal",
            "duration_ms": 1,
            "terminal_state": "completed",
            "error_class": None,
            **bad,
        }
        with pytest.raises(ValueError):
            RunTelemetryV1(**values)


def test_an_unknown_error_class_is_flattened_not_raised() -> None:
    """`record_telemetry` runs inside request paths. A refusal that crashed the
    refusal would be worse than one that is merely less specific."""
    application = _application()
    try:
        record = application.record_telemetry(uuid4(), "queued", error_class="누가 봐도 자유 텍스트")
        assert record.error_class == "unclassified"
    finally:
        application.shutdown()


def test_a_completed_run_records_only_the_allowlisted_fields() -> None:
    application = _application()
    try:
        run, _capability = _run(application)
        assert run.state == "completed"
        record = _terminals(application)[-1]
        assert record.stage == "terminal"
        assert record.terminal_state == "completed"
        assert record.error_class is None
        # Acceptance-to-terminal-commit, and this Run reached a terminal.
        assert isinstance(record.duration_ms, int) and record.duration_ms >= 0
    finally:
        application.shutdown()


def test_a_failed_run_records_the_error_class_and_no_provider_text() -> None:
    application = _application(ToolCallProvider())
    try:
        run, _capability = _run(application)
        record = _terminals(application)[-1]
        assert record.terminal_state == "failed"
        assert record.error_class == "provider_non_text"
        # The Korean sentence the user sees is not the Error Class, and the
        # Provider's own words are nowhere at all.
        assert record.error_class != run.terminal_error.message
    finally:
        application.shutdown()


def test_the_stage_and_terminal_are_read_from_the_run_not_re_derived() -> None:
    """`commit_stream_completed` answers True for BOTH outcomes it can reach. A
    caller that infers `completed` from what was streamed reports the opposite of
    what the Domain committed."""
    application = _application(MismatchedStreamProvider())
    try:
        run, _capability = _run(application)
        assert run.state == "failed"
        assert run.terminal_error.kind == "provider_invalid_response"
        record = _terminals(application)[-1]
        assert record.terminal_state == "failed"
        assert record.error_class == "provider_invalid_response"
        assert record.stage == "terminal"
    finally:
        application.shutdown()


def test_a_terminal_committed_after_the_generation_ended_is_still_observed() -> None:
    """`_commit_timeout` is reachable from a poll and from an SSE read, both of which
    can land after the generation task is gone. Reading the Run rather than a
    generation entry is what keeps that terminal observable."""
    provider = BlockingProvider()
    application = _application(provider)
    try:
        conversation, capability = application.create_conversation()
        run = application.submit_question(conversation.conversation_id, capability, "k", QUESTION)
        assert provider.started.wait(5)
        # The exact state the old lookup could not see: no generation entry left.
        with application._generation_lock:
            application._generation_tasks.clear()
        before = len(_terminals(application))

        assert application._commit_timeout(conversation.conversation_id, run.run_id) is True

        record = _terminals(application)[-1]
        assert len(_terminals(application)) == before + 1
        assert record.terminal_state == "timeout"
        assert record.error_class == "provider_timeout"
    finally:
        provider.release.set()
        application.shutdown()


@pytest.mark.parametrize(
    ("provider_factory", "terminal_state", "error_class"),
    [
        (lambda: HostileStreamingProvider(), "completed", None),
        (lambda: ToolCallProvider(), "failed", "provider_non_text"),
        (lambda: MismatchedStreamProvider(), "failed", "provider_invalid_response"),
    ],
)
def test_every_terminal_path_produces_exactly_one_record(
    provider_factory, terminal_state: str, error_class: str | None
) -> None:
    application = _application(provider_factory())
    try:
        _run(application)
        terminals = _terminals(application)
        assert len(terminals) == 1
        assert terminals[0].terminal_state == terminal_state
        assert terminals[0].error_class == error_class
    finally:
        application.shutdown()


def test_a_cancelled_run_is_reported_as_cancelled() -> None:
    provider = BlockingProvider()
    application = _application(provider)
    try:
        conversation, capability = application.create_conversation()
        run = application.submit_question(conversation.conversation_id, capability, "k", QUESTION)
        assert provider.started.wait(5)
        result = application.cancel_run(run.run_id, capability)
        application.wait_for_generations(5)

        assert result.cancel_outcome == "accepted"
        cancelled = [r for r in application.telemetry_snapshot() if r.terminal_state == "cancelled"]
        assert len(cancelled) == 1
        assert cancelled[0].error_class is None
        assert cancelled[0].stage == "terminal"
        state = application.store.states[conversation.conversation_id]
        assert cancelled[0].correlation_id == state.runs[run.run_id].correlation_id
    finally:
        provider.release.set()
        application.shutdown()


def test_a_provider_that_cannot_be_stopped_is_reported() -> None:
    """Story 1.6's silent path. The exception stays swallowed -- a Provider that
    cannot be told to stop must not take the caller down -- but the Cancel is still
    accepted and the fact that this Run will burn its whole Deadline is now visible."""
    provider = BlockingProvider(cancel_raises=True)
    application = _application(provider)
    try:
        conversation, capability = application.create_conversation()
        run = application.submit_question(conversation.conversation_id, capability, "k", QUESTION)
        assert provider.started.wait(5)
        result = application.cancel_run(run.run_id, capability)
        application.wait_for_generations(5)

        assert result.cancel_outcome == "accepted"
        failures = [
            r for r in application.telemetry_snapshot() if r.error_class == "provider_cancel_failed"
        ]
        assert len(failures) == 1
        state = application.store.states[conversation.conversation_id]
        assert failures[0].correlation_id == state.runs[run.run_id].correlation_id
    finally:
        provider.release.set()
        application.shutdown()


def test_one_correlation_id_spans_acceptance_to_terminal_commit() -> None:
    """Correlation ID 동일성: the value the client is shown on the failure IS the
    value Telemetry recorded, and the Run has carried it since it was accepted."""
    application = _application(ToolCallProvider())
    try:
        run, _capability = _run(application)
        state = application.store.states[run.conversation_id]
        domain_run = state.runs[run.run_id]

        assert run.terminal_error.correlation_id == domain_run.correlation_id
        assert _terminals(application)[-1].correlation_id == domain_run.correlation_id
    finally:
        application.shutdown()


def test_a_retried_run_has_its_own_single_correlation_id() -> None:
    """The Retry path accepts a Run of its own, so it mints and threads its own ID.
    Without this the Retry's error envelope and its Record silently disagree."""
    from aidd_chat.contracts import RetryCommand

    application = _application(ToolCallProvider())
    try:
        conversation, capability = application.create_conversation()
        first = application.submit_question(
            conversation.conversation_id, capability, "k1", QUESTION
        )
        application.wait_for_generations(5)
        assert application.get_run(first.run_id, capability).state == "failed"

        retried = application.retry_run(
            conversation.conversation_id,
            capability,
            "k2",
            RetryCommand(kind="retry", retry_of_run_id=first.run_id),
        )
        application.wait_for_generations(5)
        run = application.get_run(retried.run_id, capability)

        state = application.store.states[conversation.conversation_id]
        first_id = state.runs[first.run_id].correlation_id
        retry_id = state.runs[retried.run_id].correlation_id
        assert first_id != retry_id
        assert run.terminal_error.correlation_id == retry_id
        assert _terminals(application)[-1].correlation_id == retry_id
        assert {record.correlation_id for record in _terminals(application)} == {
            first_id,
            retry_id,
        }
    finally:
        application.shutdown()


def test_the_aggregate_refuses_to_mint_a_correlation_id_of_its_own() -> None:
    """ChatApplication이 유일하게 소유한다. An Aggregate that defaulted here would be
    a second owner, and the client's ID and the Record's would diverge."""
    now = _now()
    aggregate = ConversationAggregate(uuid4(), now, now + timedelta(seconds=3_600), "hash")
    for bad in (None, "not-a-uuid", UUID(int=0, version=1)):
        # v4 specifically: this exact value is injected into
        # `ProviderFailureV1.correlation_id`, which requires v4 -- and it is injected
        # by `model_validate`, so a non-v4 would be caught THERE, inside a terminal
        # commit, rather than here where it can still be refused cleanly.
        with pytest.raises(ValueError):
            aggregate.accept_question("key", "digest", QUESTION, now, correlation_id=bad)


@pytest.mark.parametrize(
    ("provider_factory", "cancel"),
    [
        (lambda: HostileStreamingProvider(), False),
        (lambda: ToolCallProvider(), False),
        (lambda: BlockingProvider(), True),
    ],
)
def test_no_forbidden_value_can_reach_telemetry(
    caplog: pytest.LogCaptureFixture, provider_factory, cancel: bool
) -> None:
    """금지 필드 기록 시도, on the completed, failed and cancelled paths alike -- plus
    a reconnect-shaped replay read. Prompt, answer, Secret, Run ID, Conversation ID
    and Model Revision are all present in this process while the Run happens; none of
    them appears in a Record or in the line the Record logs."""
    provider = provider_factory()
    application = _application(provider)
    try:
        with caplog.at_level(logging.DEBUG):
            conversation, capability = application.create_conversation()
            run = application.submit_question(
                conversation.conversation_id, capability, "k", QUESTION
            )
            if cancel:
                assert provider.started.wait(5)
                application.cancel_run(run.run_id, capability)
            application.wait_for_generations(5)
            # The reconnect path: a client replaying the Run's committed log.
            application.get_run_slice(run.run_id, capability, 0)
            application.get_run(run.run_id, capability)

        logged = "\n".join(record.getMessage() for record in caplog.records)
        forbidden = (
            QUESTION,
            HOSTILE,
            capability,
            str(run.run_id),
            str(conversation.conversation_id),
            DETERMINISTIC_BINDING.model_revision,
            application.provider.binding_digest,
        )
        for value in forbidden:
            assert value not in logged, value
        for record in application.telemetry_snapshot():
            rendered = repr(record)
            for value in forbidden:
                assert value not in rendered, value
        # And it did record something, so the assertions above are not vacuous.
        assert "run_telemetry" in logged
        assert str(application.telemetry_snapshot()[-1].correlation_id) in logged
    finally:
        if cancel:
            provider.release.set()
        application.shutdown()


def test_the_telemetry_deque_evicts_rather_than_growing() -> None:
    application = _application()
    try:
        for _ in range(MAX_TELEMETRY_RECORDS + 64):
            application.record_telemetry(uuid4(), "queued")
        assert len(application.telemetry_snapshot()) == MAX_TELEMETRY_RECORDS
    finally:
        application.shutdown()


def test_the_capacity_envelope_cannot_be_built_without_observing_it() -> None:
    """Structural, because behaviour cannot see it: with every call site passing
    `chat`, a default would change nothing today and everything the moment someone
    adds the eleventh call site. The same argument that moved the public_demo Guards
    inside `build_provider`."""
    parameter = inspect.signature(main_module.capacity_response).parameters["chat"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation is ChatApplication

    # And every call site in main.py actually supplies it.
    source = ast.parse(Path(main_module.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(source)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "capacity_response"
    ]
    assert len(calls) >= 9
    assert all(len(call.args) + len(call.keywords) == 2 for call in calls)


def test_a_ceiling_refused_through_the_api_is_recorded() -> None:
    """The wiring, not the recorder: the Record has to be produced by an actual
    refusal travelling out through `capacity_response`, or Story 1.8's silent
    ceilings are silent again."""
    application = _application(poll_rate_per_minute=1)
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post("/api/v1/conversations", headers=ORIGIN)
            submitted = client.post(
                f"/api/v1/conversations/{created.json()['conversation_id']}/runs",
                headers={**ORIGIN, "Idempotency-Key": "ceiling"},
                json={"kind": "question", "content": QUESTION},
            )
            assert submitted.status_code == 202
            run_id = submitted.json()["run_id"]
            application.wait_for_generations(5)
            first = client.get(f"/api/v1/runs/{run_id}")
            refused = client.get(f"/api/v1/runs/{run_id}")
    finally:
        app.dependency_overrides.pop(get_chat_application, None)
        application.shutdown()

    assert first.status_code == 200
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "rate_limited"
    refusals = [r for r in application.telemetry_snapshot() if r.error_class == "rate_limited"]
    assert len(refusals) == 1
    assert refusals[0].stage == "queued" and refusals[0].terminal_state is None
    # Nothing was measured: this is not a Run, so there is no
    # acceptance-to-terminal-commit to report and `0` would be a fiction.
    assert refusals[0].duration_ms is None


def test_repeated_ceiling_refusals_are_coalesced_per_window() -> None:
    """A ceiling under load refuses once per request. 512 identical Records would
    evict every Run terminal in the deque within seconds."""
    application = _application()
    try:
        for _ in range(50):
            application.record_capacity_refusal("rate_limited")
        assert len([r for r in application.telemetry_snapshot() if r.error_class == "rate_limited"]) == 1
        # A different Code is a different fact and is reported on its own.
        application.record_capacity_refusal("queue_capacity_exceeded")
        assert application.telemetry_snapshot()[-1].error_class == "queue_capacity_exceeded"
    finally:
        application.shutdown()


def test_unknown_capacity_codes_coalesce_as_one() -> None:
    """The dedupe key has to be the code that is actually RECORDED. Keying on the raw
    reason gives several unknown codes their own window while they are
    indistinguishable in the deque -- and leaves the table unbounded."""
    application = _application()
    try:
        for reason in ("존재하지 않는 코드 A", "존재하지 않는 코드 B", "또 다른 코드"):
            application.record_capacity_refusal(reason)
        unclassified = [
            r for r in application.telemetry_snapshot() if r.error_class == "unclassified"
        ]
        assert len(unclassified) == 1
        # Bounded by the closed vocabulary, so there is nothing to prune.
        assert set(application._capacity_reported) <= {"unclassified"}
    finally:
        application.shutdown()


def test_the_sweeper_reports_every_purge_it_performs() -> None:
    """The Store's own timer thread calls `sweep` directly, so the Record has to come
    from the Store's purge observer -- not from the Application's sweep entry point,
    which production never calls."""
    application = _application()
    try:
        conversation, _capability = application.create_conversation()
        state = application.store.states[conversation.conversation_id]
        state.expires_at = _now() - timedelta(seconds=1)
        state.expires_monotonic = time.monotonic() - 1
        before = len(application.telemetry_snapshot())

        # Straight at the Store, exactly as its Sweeper thread does it.
        assert application.store.sweep(_now()) == 1

        assert len(application.telemetry_snapshot()) == before + 1
        assert application.telemetry_snapshot()[-1].error_class == "conversation_expired"
    finally:
        application.shutdown()


def test_a_purge_reports_the_live_run_it_ended_with_that_run_s_id() -> None:
    """The one Run-ending path with no request behind it. The observation has to be
    read BEFORE the Purge -- `expire` clears `runs`, and afterwards the Correlation
    ID is gone -- or this Record carries an ID belonging to nothing."""
    provider = BlockingProvider()
    application = _application(provider)
    try:
        conversation, capability = application.create_conversation()
        run = application.submit_question(conversation.conversation_id, capability, "k", QUESTION)
        assert provider.started.wait(5)
        state = application.store.states[conversation.conversation_id]
        correlation_id = state.runs[run.run_id].correlation_id
        state.expires_at = _now() - timedelta(seconds=1)
        state.expires_monotonic = time.monotonic() - 1
        before = len(application.telemetry_snapshot())

        assert application.store.sweep(_now()) == 1

        records = application.telemetry_snapshot()[before:]
        expired = [r for r in records if r.error_class == "conversation_expired"]
        assert len(expired) == 1
        assert expired[0].correlation_id == correlation_id
        # Still in flight when the Purge ended it: no terminal, so no duration.
        assert expired[0].terminal_state is None
        assert expired[0].duration_ms is None
        assert expired[0].stage == "streaming"
    finally:
        provider.release.set()
        application.shutdown()


def test_a_cancel_that_fails_after_the_purge_is_still_reported() -> None:
    """The Store cancels the Provider only once the Conversation is emptied, so a
    cancel that fails there has no Run left to read. It is also the moment that
    matters most: that Run is about to burn its whole Deadline unattended."""
    provider = BlockingProvider(cancel_raises=True)
    application = _application(provider)
    try:
        conversation, capability = application.create_conversation()
        run = application.submit_question(conversation.conversation_id, capability, "k", QUESTION)
        assert provider.started.wait(5)
        state = application.store.states[conversation.conversation_id]
        correlation_id = state.runs[run.run_id].correlation_id
        state.expires_at = _now() - timedelta(seconds=1)
        state.expires_monotonic = time.monotonic() - 1

        assert application.store.sweep(_now()) == 1

        def failures():
            return [
                r for r in application.telemetry_snapshot()
                if r.error_class == "provider_cancel_failed"
            ]

        # The abort runs on the agent loop now, after the Purge returns.
        deadline = time.monotonic() + 2
        while not failures() and time.monotonic() < deadline:
            time.sleep(0.01)
        failures = failures()
        assert len(failures) == 1
        assert failures[0].correlation_id == correlation_id
    finally:
        provider.release.set()
        application.shutdown()


def test_a_run_that_dies_before_the_provider_is_still_observed() -> None:
    """The three pre-Provider commit_failed paths end a Run terminal without ever
    reaching a Provider. Unobserved, they are the silent failure most likely to be
    happening in bulk exactly when someone goes looking at the telemetry."""

    class BrokenStartStore(InMemoryConversationStore):
        def start_run(self, lease) -> None:
            raise RuntimeError("counter is broken")

    application = ChatApplication(
        BrokenStartStore(), FakeAgent(), 3, DeploymentLimits()
    )
    try:
        conversation, capability = application.create_conversation()
        run = application.submit_question(conversation.conversation_id, capability, "k", QUESTION)
        application.wait_for_generations(5)

        assert application.get_run(run.run_id, capability).state == "failed"
        terminals = _terminals(application)
        assert len(terminals) == 1
        assert terminals[0].terminal_state == "failed"
        assert terminals[0].error_class == "provider_unknown"
        state = application.store.states[conversation.conversation_id]
        assert terminals[0].correlation_id == state.runs[run.run_id].correlation_id
    finally:
        application.shutdown()


def test_readiness_refusals_name_the_gate_that_refused() -> None:
    """Story 1.5.1 left `/ready` 503 undiagnosable: a missing credential, an
    unreachable endpoint and a broken Token invariant were the same blank body."""
    unconfigured = ChatApplication(InMemoryConversationStore(), FakeAgent(), 3, None)
    try:
        assert unconfigured.is_ready() is False
        assert unconfigured.telemetry_snapshot()[-1].error_class == "deployment_unconfigured"
    finally:
        unconfigured.shutdown()

    healthy = _application()
    try:
        assert healthy.is_ready() is True
        # Health is NOT an Error Class: an operator filtering `error_class is not
        # None` must see faults and only faults.
        assert healthy.telemetry_snapshot()[-1].error_class is None
        assert "ready" not in {code for code in READINESS_CODES}
        before = len(healthy.telemetry_snapshot())
        for _ in range(5):
            healthy.is_ready()
        assert len(healthy.telemetry_snapshot()) == before
    finally:
        healthy.shutdown()


def test_a_never_ready_process_reports_once_and_does_not_flap() -> None:
    """The Gates are checked BEFORE the probe runs, so a reason reported from inside
    them announces `ready` for a process that then answers 503 -- and the probe's own
    refusal flips it straight back, two Records per poll for ever."""

    class DeadProvider(LegacySyncAgent):
        def probe(self) -> bool:
            return False

    application = _application(DeadProvider())
    try:
        for _ in range(3):
            assert application.is_ready() is False
        reasons = [r.error_class for r in application.telemetry_snapshot()]
        assert reasons == ["provider_probe_failed"]
    finally:
        application.shutdown()


@pytest.mark.release_suite
def test_default_and_debug_access_logs_are_disabled() -> None:
    """Uvicorn's own access log and every HTTP client logger write request lines
    with URLs. `disabled` is irreversible for the process, so the state is captured
    and restored -- otherwise this test silences those loggers for the whole session
    and every caplog assertion after it becomes vacuous."""
    names = ("uvicorn.access", "httpx", "httpcore", "urllib3")
    saved = {
        name: (
            logging.getLogger(name).disabled,
            logging.getLogger(name).propagate,
            list(logging.getLogger(name).handlers),
        )
        for name in (*names, "uvicorn.error")
    }
    try:
        main_module.silence_untrusted_loggers()
        for name in names:
            logger = logging.getLogger(name)
            assert logger.disabled and not logger.propagate and not logger.handlers, name
        # The startup/error channel is left alone: it carries no request content and
        # it is what reports a refused Guard.
        assert not logging.getLogger("uvicorn.error").disabled
    finally:
        for name, (disabled, propagate, handlers) in saved.items():
            logger = logging.getLogger(name)
            logger.disabled, logger.propagate = disabled, propagate
            logger.handlers[:] = handlers


def test_startup_silences_the_untrusted_loggers() -> None:
    """The wiring, not the function: `lifespan` has to call it, or a deployment
    constructs its HTTP clients with httpx logging live."""
    logger = logging.getLogger("httpx")
    saved = (logger.disabled, logger.propagate, list(logger.handlers))
    try:
        logger.disabled = False
        logger.propagate = True
        with TestClient(app, base_url="https://testserver"):
            assert logging.getLogger("httpx").disabled is True
    finally:
        logger.disabled, logger.propagate = saved[0], saved[1]
        logger.handlers[:] = saved[2]


def test_no_free_form_logging_call_exists() -> None:
    """Every logging call in `src/` takes a constant format string. Parsed, not
    grepped: an f-string, a `.format()` or a `%`-concatenation is how a Prompt or an
    answer reaches a log by accident, and the logger does not have to be called
    `_log` for that to happen."""
    methods = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
    seen = 0
    for path in SRC_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "logging.basicConfig" not in source, path.name
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in methods or not node.args:
                continue
            target = getattr(node.func.value, "id", getattr(node.func.value, "attr", ""))
            if "log" not in str(target).casefold():
                continue
            seen += 1
            message = node.args[1] if node.func.attr == "log" else node.args[0]
            assert isinstance(message, ast.Constant) and isinstance(message.value, str), (
                f"{path.name}:{node.lineno} logging message must be a constant format string"
            )
    assert seen, "no logging at all would make this gate vacuous"


# --------------------------------------------------------------------------
# No retrieval path
# --------------------------------------------------------------------------


def test_no_runtime_or_readiness_path_calls_retrieval() -> None:
    """0건, counted where it can actually be counted: in the source. No name that
    could be a Retrieval call exists on any path."""
    for path in SRC_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for banned in (
            ".retrieve(",
            "retriever",
            "embedding",
            "vector_store",
            "vectorstore",
            "similarity_search",
            "knowledge_base",
        ):
            assert banned not in source, f"{path.name}: {banned}"


# --------------------------------------------------------------------------
# Deployment profile
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["public_demo", "staging", "", "   ", "LOCAL_TEST", "public-demo"])
def test_an_unknown_or_blank_deployment_profile_is_refused(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """DeploymentProfile은 local_test만 허용한다 (public_demo is retired), and
    pydantic-settings is where that is enforced -- the AC scopes the check to it. A
    blank value is the dangerous case: an imperative `.strip() or "local_test"`
    fallback made DEPLOYMENT_PROFILE="   " select a profile in silence."""
    monkeypatch.setenv("DEPLOYMENT_PROFILE", value)
    with pytest.raises(ValidationError):
        ProviderSettings()
    # And the exported builder turns that into the refusal its callers handle.
    with pytest.raises(BindingConfigurationError):
        build_provider()


def test_the_credential_has_no_default_and_absence_is_representable() -> None:
    """`Secret에는 Default가 없다`. Absence is None -- a state a deterministic
    local_test deployment is legitimately in -- and it is distinct from any value,
    including the empty string. Both fail closed wherever a credential is needed."""
    assert ProviderSettings.model_fields["ollama_api_key"].default is None

    absent = ProviderSettings(
        deployment_profile="local_test", ollama_base_url="", ollama_api_key=None
    )
    assert absent.ollama_api_key is None
    assert absent.credential() == ""

    # Distinct from absence at the type level, identical in effect: both fail closed.
    blank = ProviderSettings(ollama_base_url="", ollama_api_key=SecretStr(""))
    assert blank.ollama_api_key is not None and blank.credential() == ""

    # A real endpoint with no credential serves 503 rather than silently falling back
    # to the test provider, and the reason survives to the operator.
    unbound = build_provider(
        ProviderSettings(
            deployment_profile="local_test",
            ollama_base_url="https://demo.example.com",
            ollama_api_key=None,
        )
    )
    assert isinstance(unbound, UnboundProvider)
    assert "OLLAMA_API_KEY" in unbound.reason
    assert unbound.probe() is False


def test_a_refused_public_demo_deployment_is_never_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed하여 Ready가 되지 않는다: the retired profile leaves the process
    serving 503 rather than a public demo on any provider."""
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "public_demo")
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://demo.example.com")
    monkeypatch.setenv("OLLAMA_API_KEY", "demo-key")

    application = build_chat_application()
    try:
        assert isinstance(application.provider, UnboundProvider)
        assert application.is_ready() is False
        assert application.policy_projection is None
    finally:
        application.shutdown()


# --------------------------------------------------------------------------
# Public health surface
# --------------------------------------------------------------------------


def test_live_and_ready_return_status_only() -> None:
    """Status만, 본문 없음: no Provider health detail, no internal limit, no endpoint
    and no model revision, whether the process is Ready or not."""
    with TestClient(app, base_url="https://testserver") as client:
        live = client.get("/live")
        ready = client.get("/ready")

    for response in (live, ready):
        assert response.status_code in (204, 503)
        assert response.content == b""
        headers = " ".join(f"{key}:{value}" for key, value in response.headers.items()).lower()
        for leak in ("ollama", "qwen", "localhost", "127.0.0.1", "deterministic", "token"):
            assert leak not in headers, leak
    assert ready.headers["cache-control"] == "no-store"


def test_ready_leaks_nothing_when_the_provider_is_unbound() -> None:
    application = _application()
    application.__setattr__("_provider_binding", object())
    app.dependency_overrides[get_chat_application] = lambda: application
    try:
        with TestClient(app, base_url="https://testserver") as client:
            ready = client.get("/ready")
    finally:
        app.dependency_overrides.pop(get_chat_application, None)
        application.shutdown()

    assert ready.status_code == 503
    assert ready.content == b""


@pytest.mark.parametrize("text", UNSAFE_TEXTS)
def test_the_domain_refuses_hostile_text_rather_than_raising(text: str) -> None:
    """The invariant that lets `uvicorn.error` stay enabled: every commit PRE-CHECKS
    with the predicate and answers False, so hostile text never reaches an Event
    constructor. If it did, pydantic's ValidationError message embeds
    `input_value=...` and an escaping ASGI traceback would log the Model's answer
    verbatim on the one logger this process does not silence.

    Asserted at the Domain, not through a Run: the Application also refuses this text
    a step earlier, so a Run-level test would still pass with this guard removed."""
    now = _now()
    aggregate = ConversationAggregate(uuid4(), now, now + timedelta(seconds=3_600), "hash")
    accepted = aggregate.accept_question(
        "key", "digest", QUESTION, now, correlation_id=uuid4()
    )
    assert aggregate.mark_running(accepted.run.run_id, now, time.monotonic())
    events_before = len(aggregate.runs[accepted.run.run_id].events)

    assert aggregate.commit_delta(accepted.run.run_id, text, now) is False
    assert aggregate.commit_completed(accepted.run.run_id, text, GROUNDED_RESULT, now) is False
    # No Event was built, so nothing could have raised on the way to one.
    assert len(aggregate.runs[accepted.run.run_id].events) == events_before


def test_a_hostile_answer_that_reaches_the_completed_path_fails_typed() -> None:
    """A `complete`-only Provider's answer reaches the Application as one delta on
    the agent port, and the Application's own hostile-text refusal must end the Run
    typed. Without it a control character escapes as a ValidationError and the Run
    ends `provider_unknown`."""
    application = _application(HostileCompleteOnlyProvider("답변\x00숨김"))
    try:
        run, _capability = _run(application)
        assert run.state == "failed"
        assert run.terminal_error.kind == "provider_invalid_response"
    finally:
        application.shutdown()
