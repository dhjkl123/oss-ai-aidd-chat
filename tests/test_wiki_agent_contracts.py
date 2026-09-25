from uuid import uuid4
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aidd_chat.contracts import (
    FIXED_REPLIES,
    STEP_LABELS,
    AgentProbeV1,
    AgentResultV1,
    AgentStepEventV1,
    AgentStepV1,
    CompletedMessageV1,
    MessageSourcesEventV1,
    ProviderFailureV1,
    SERIALIZER_ID,
    SYSTEM_INSTRUCTION,
    WikiSourceV1,
    validate_wiki_path,
)

SOURCE = WikiSourceV1(path="comparisons/bmad-vs-superpowers.md", title="BMAD vs Superpowers",
                      confidence="high", contested=False)


def test_fixed_copy_is_verbatim():
    assert FIXED_REPLIES == {
        "wiki_gap": "Wiki에서 근거를 찾지 못했어요. Wiki 보충 대상이에요.",
        "out_of_scope": "이 질문은 Wiki가 다루는 범위 밖이라 답할 수 없어요.",
        "meta": "안녕하세요. llm-wiki에 정리된 내용을 근거로 답해요. AIDD 도구·워크플로·방법론을 물어보세요. 답변 아래에 근거 문서가 표시돼요.",
    }
    assert STEP_LABELS == {
        "wiki_index": "Wiki 목록 확인", "wiki_search": "Wiki 검색 중", "wiki_read": "문서 읽는 중: ",
        "decide": "근거 판단 중", "compose": "답변 작성 중", "limit_reached": "검색 한도 도달",
    }
    assert SERIALIZER_ID == "pi-sidecar-json-role-text-v1"
    assert "decide" in SYSTEM_INSTRUCTION and "wiki_index" in SYSTEM_INSTRUCTION


@pytest.mark.parametrize("path", [
    "comparisons/a.md", "queries/한글 문서.md", "raw/x/y.md", "index.md",
])
def test_wiki_path_accepts_relative_posix_markdown(path):
    assert validate_wiki_path(path) == path


@pytest.mark.parametrize("path", [
    "", "/etc/passwd.md", "../x.md", "a/../b.md", "./a.md", "a\\b.md", "a.txt",
    "inbox/a.md", "docs/a.md", ".obsidian/a.md", ".git/a.md", ".ua/a.md",
    "Inbox/a.md", "DOCS/a.md", ".Obsidian/a.md",
    "a\u202eb.md", "x" * 600 + ".md", "C:/x.md",
])
def test_wiki_path_rejects_everything_else(path):
    with pytest.raises(ValueError):
        validate_wiki_path(path)


def test_wiki_path_requires_nfc():
    decomposed = "queries/\u1100\u1161.md"  # NFD 가
    with pytest.raises(ValueError):
        validate_wiki_path(decomposed)


def test_step_labels_are_the_fixed_templates():
    AgentStepV1(step_index=1, kind="wiki_index", label="Wiki 목록 확인", doc_path=None)
    AgentStepV1(step_index=2, kind="wiki_read", label="문서 읽는 중: BMAD vs Superpowers",
                doc_path="comparisons/bmad-vs-superpowers.md")
    with pytest.raises(ValidationError):
        AgentStepV1(step_index=1, kind="wiki_search", label="Wiki 검색 중: bmad", doc_path=None)
    with pytest.raises(ValidationError):
        AgentStepV1(step_index=1, kind="wiki_read", label="문서 읽는 중: x", doc_path=None)
    with pytest.raises(ValidationError):
        AgentStepV1(step_index=0, kind="decide", label="근거 판단 중", doc_path=None)


def test_step_label_rejects_unsafe_title():
    with pytest.raises(ValidationError):
        AgentStepV1(step_index=1, kind="wiki_read", label="문서 읽는 중: a\u202eb", doc_path="a.md")
    with pytest.raises(ValidationError):
        AgentStepV1(step_index=1, kind="wiki_read", label="문서 읽는 중: " + "가" * 121, doc_path="a.md")
    # Markup is inert text, not a refusal: the Web renders textContent only.
    AgentStepV1(step_index=1, kind="wiki_read", label="문서 읽는 중: <script>", doc_path="a.md")


def test_result_sources_match_outcome():
    AgentResultV1(outcome="grounded", uncovered=None, sources=(SOURCE,), search_truncated=False, finish="stop")
    AgentResultV1(outcome="partial", uncovered="가격 정보", sources=(SOURCE,), search_truncated=True, finish="stop")
    AgentResultV1(outcome="wiki_gap", uncovered=None, sources=(), search_truncated=False, finish="stop")
    for bad in (
        dict(outcome="grounded", uncovered=None, sources=()),
        dict(outcome="wiki_gap", uncovered=None, sources=(SOURCE,)),
        dict(outcome="partial", uncovered=None, sources=(SOURCE,)),
        dict(outcome="grounded", uncovered="x", sources=(SOURCE,)),
        dict(outcome="grounded", uncovered=None, sources=(SOURCE, SOURCE)),
    ):
        with pytest.raises(ValidationError):
            AgentResultV1(search_truncated=False, finish="stop", **bad)


def test_completed_message_carries_outcome_fields():
    message = CompletedMessageV1(message_id=uuid4(), content="답", outcome="grounded",
                                 sources=(SOURCE,), search_truncated=False, uncovered=None)
    assert message.sources[0].path == SOURCE.path
    with pytest.raises(ValidationError):
        CompletedMessageV1(message_id=uuid4(), content="답")


def test_new_events_validate():
    now = datetime.now(UTC)
    step = AgentStepV1(step_index=1, kind="decide", label="근거 판단 중", doc_path=None)
    event = AgentStepEventV1(run_id=uuid4(), sequence=2, occurred_at=now, step=step)
    assert event.type == "agent.step"
    sources = MessageSourcesEventV1(run_id=uuid4(), sequence=3, occurred_at=now, message_id=uuid4(),
                                    outcome="partial", sources=(SOURCE,), search_truncated=False,
                                    uncovered="가격")
    assert sources.type == "message.sources"


def test_new_failure_kinds_exist():
    for kind in ("agent_runtime_unavailable", "wiki_unavailable"):
        ProviderFailureV1(kind=kind, retryable=True, correlation_id=uuid4(), message="x")


def test_probe_ready_only_when_all_ok():
    assert AgentProbeV1(provider_ok=True, wiki_ok=True, context_ok=True).ready
    assert not AgentProbeV1(provider_ok=True, wiki_ok=False, context_ok=True).ready
    assert not AgentProbeV1(provider_ok=True, wiki_ok=True, context_ok=False).ready
