from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aidd_chat.contracts import AgentResultV1, AgentStepV1, WikiSourceV1
from aidd_chat.domain import ConversationAggregate

NOW = datetime.now(UTC)
SOURCE = WikiSourceV1(path="concepts/a.md", title="A", confidence="low", contested=True)
GROUNDED = AgentResultV1(outcome="grounded", uncovered=None, sources=(SOURCE,),
                         search_truncated=False, finish="stop")


def running_run():
    aggregate = ConversationAggregate(uuid4(), NOW, NOW + timedelta(hours=1), "cap")
    accepted = aggregate.accept_question("key", "digest", "질문", NOW, correlation_id=uuid4())
    run_id = accepted.run.run_id
    assert aggregate.mark_running(run_id, NOW, 0.0)
    return aggregate, run_id


def step(index, kind="decide", label="근거 판단 중"):
    return AgentStepV1(step_index=index, kind=kind, label=label, doc_path=None)


def test_steps_are_recorded_in_order():
    aggregate, run_id = running_run()
    assert aggregate.commit_step(run_id, step(1, "wiki_index", "Wiki 목록 확인"), NOW)
    assert not aggregate.commit_step(run_id, step(3), NOW)  # gap refused
    assert aggregate.commit_step(run_id, step(2), NOW)
    types = [event.type for event in aggregate.runs[run_id].events]
    assert types == ["run.status", "agent.step", "agent.step"]


def test_completion_emits_sources_before_completed():
    aggregate, run_id = running_run()
    assert aggregate.commit_delta(run_id, "답", NOW)
    assert aggregate.commit_stream_completed(run_id, "답", GROUNDED, None, NOW)
    events = aggregate.runs[run_id].events
    assert [event.type for event in events][-4:] == [
        "message.sources", "message.completed", "run.status", "stream.end"]
    assert events[-4].sources == (SOURCE,)
    projection = aggregate.projection(aggregate.runs[run_id])
    assert projection.output_message.outcome == "grounded"
    assert projection.output_message.sources == (SOURCE,)


def test_failure_never_emits_sources():
    from aidd_chat.contracts import ProviderFailureV1
    aggregate, run_id = running_run()
    aggregate.commit_step(run_id, step(1), NOW)
    failure = ProviderFailureV1(kind="provider_unknown", retryable=False, correlation_id=uuid4(), message="x")
    assert aggregate.commit_failed(run_id, failure, NOW)
    assert "message.sources" not in [event.type for event in aggregate.runs[run_id].events]


def test_step_after_terminal_is_refused():
    aggregate, run_id = running_run()
    assert aggregate.commit_cancelled(run_id, NOW)
    assert not aggregate.commit_step(run_id, step(1), NOW)


# --- Task 5b: whitespace-only first delta must not fail a Run ----------------------


def test_whitespace_only_delta_is_accepted_and_run_still_completes():
    aggregate, run_id = running_run()
    assert aggregate.commit_delta(run_id, " ", NOW)
    assert aggregate.commit_delta(run_id, "답", NOW)
    assert aggregate.commit_stream_completed(run_id, " 답", GROUNDED, None, NOW)
    projection = aggregate.projection(aggregate.runs[run_id])
    assert projection.output_message.content == " 답"


def test_newline_only_delta_is_accepted():
    aggregate, run_id = running_run()
    assert aggregate.commit_delta(run_id, "\n", NOW)


def test_completed_echo_size_matches_a_validated_event():
    from aidd_chat.contracts import MessageCompletedEventV1
    from aidd_chat.domain import _completed_echo, event_size

    aggregate, run_id = running_run()
    run = aggregate.runs[run_id]
    echo = _completed_echo(run, "답", NOW)
    validated = MessageCompletedEventV1(
        run_id=run.run_id, sequence=echo.sequence, occurred_at=NOW,
        message_id=run.reserved_output_message_id, text="답",
    )
    assert event_size(echo) == event_size(validated)
