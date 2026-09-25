import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect

from aidd_chat.adapters.pi_sidecar import pi_policy_metadata
from aidd_chat.adapters.tokenizer import HfTokenizer
from aidd_chat.application import build_policy_projection
from aidd_chat.contracts import AgentBindingV1, AgentLimitsV1, TokenizerAuthorityV1

from wiki_agent_browser_helpers import (
    CONVERSATION_ID, MESSAGE_ID, NOW, SOURCE, event, install_event_source, projection, stub_api,
)

ROOT = Path(__file__).parents[1]
WIKI_ROOT = ROOT / "agent" / "test-fixtures" / "wiki"
TOKENIZER_PATH = ROOT / "tests" / "fixtures" / "tiny-tokenizer.json"


async def _policy(url):
    async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
        page = await browser.new_page()
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
        await expect(page.locator("[data-policy-field='wiki_display_name']")).to_have_text("local-test-wiki")
        await expect(page.locator("[data-policy-field='transmitted_fields']")).to_contain_text("읽은 Wiki 발췌")
        await expect(page.locator("[data-policy-field='retrieval_status']")).to_have_text("Wiki 읽기 전용")
        notice = page.locator("#policy-panel [data-ux='UX-KNOWLEDGE-NOTICE']")
        await expect(notice).to_contain_text("Wiki")
        await expect(notice).not_to_contain_text("연결되지 않습니다")
        # Task 25: the start screen names the Wiki in the composer ("llm-wiki에 물어보세요").
        await expect(page.locator("#prompt")).to_have_attribute("placeholder", "llm-wiki에 물어보세요")


def test_policy_panel_discloses_wiki(server_url):
    asyncio.run(_policy(server_url))


def step(sequence, index, kind, label, doc_path=None):
    return event(sequence, "agent.step", step={"step_index": index, "kind": kind, "label": label, "doc_path": doc_path})


GROUNDED_EVENTS = [
    event(1, "run.status", state="running", stage="streaming"),
    step(2, 1, "wiki_index", "Wiki 목록 확인"),
    step(3, 2, "wiki_read", "문서 읽는 중: Alpha 개념", "concepts/alpha.md"),
    step(4, 3, "wiki_read", "문서 읽는 중: Alpha 개념", "concepts/alpha.md"),
    step(5, 4, "decide", "근거 판단 중"),
    step(6, 5, "compose", "답변 작성 중"),
    event(7, "message.delta", message_id=MESSAGE_ID, text="알파는 개념이에요."),
    event(8, "message.sources", message_id=MESSAGE_ID, outcome="grounded", sources=[SOURCE],
          search_truncated=False, uncovered=None),
    event(9, "message.completed", message_id=MESSAGE_ID, text="알파는 개념이에요."),
    event(10, "run.status", state="completed", stage="terminal"),
    event(11, "stream.end", final_state="completed", final_sequence=11),
]


async def _steps(url):
    async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
        page = await browser.new_page()
        await install_event_source(page, GROUNDED_EVENTS)
        await stub_api(page, projection("completed", 11, "알파는 개념이에요.", outcome="grounded", sources=[SOURCE]))
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
        await page.evaluate("""
          window.__announcements = [];
          new MutationObserver(() => window.__announcements.push(document.querySelector('#run-announcer').textContent))
            .observe(document.querySelector('#run-announcer'), {childList: true, subtree: true});
        """)
        await page.locator("[data-new-conversation]").first.click()
        await page.locator("#prompt").fill("alpha?")
        await page.locator("#send-question").click()
        steps = page.locator("[data-agent-steps] li")
        await expect(page.locator("#run-announcer")).to_have_text("답변 완료")
        # Task 25: the settled thinking line reads "Wiki N단계 확인" and keeps every step,
        # all done, behind it (it used to collapse the list to one "단계 N개" item).
        think = page.locator("[data-think]")
        await expect(think).to_have_text("Wiki 5단계 확인")
        await expect(think).to_have_attribute("aria-expanded", "false")
        await expect(steps).to_have_count(5)
        await expect(steps.first).to_have_text("완료 Wiki 목록 확인")
        await expect(steps.last).to_have_text("완료 답변 작성 중")
        await expect(steps.first).to_be_hidden()
        await think.click()
        await expect(steps.first).to_be_visible()
        announcements = await page.evaluate("window.__announcements")
        assert announcements.count("문서 읽는 중: Alpha 개념") == 1   # repeated label announced once
        assert "Wiki 목록 확인" in announcements and "답변 작성 중" in announcements
        assert not any("알파는" in item for item in announcements)   # tokens never announced
        assert await page.locator("[data-agent-steps]").evaluate("n => !!n.closest('[data-ux=UX-STATUS-ANNOUNCER]')")


async def _steps_live(url):
    events = GROUNDED_EVENTS[:4]
    async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
        page = await browser.new_page()
        await install_event_source(page, events)
        await stub_api(page, projection("running", 4))
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
        await page.locator("[data-new-conversation]").first.click()
        await page.locator("#prompt").fill("alpha?")
        await page.locator("#send-question").click()
        steps = page.locator("[data-agent-steps] li")
        await expect(steps).to_have_count(3)
        await expect(steps.nth(0)).to_have_text("완료 Wiki 목록 확인")
        await expect(steps.nth(2)).to_have_text("진행 중 문서 읽는 중: Alpha 개념")
        # The thinking line shows the current step while the Run is active.
        await expect(page.locator("[data-think]")).to_have_text("문서 읽는 중: Alpha 개념")
        await expect(page.locator("[data-think]")).to_have_class("think live")
        assert await page.locator("article.assistant .md").text_content() == ""   # no answer text during research


def test_step_list_announces_each_new_label_once_and_collapses(server_url):
    asyncio.run(_steps(server_url))


def test_step_list_shows_done_and_current_while_running(server_url):
    asyncio.run(_steps_live(server_url))


# An async context manager, not a plain function returning `page`: every caller
# keeps using `page` for its own assertions afterwards, and a plain `async with
# ... as browser:` block around a `return` closes the browser (TargetClosedError)
# the instant the function returned, before the caller ever sees the page.
@asynccontextmanager
async def _run_with(url, events, final):
    async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await install_event_source(page, events)
        await stub_api(page, final)
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
        # No 새 대화 click: since Task 24 the first question creates the conversation,
        # at any viewport width (the nav's 새 대화 is folded shut at 320px).
        await page.locator("#prompt").fill("질문")
        await page.locator("#send-question").click()
        await expect(page.locator("#run-announcer")).to_have_text("답변 완료")
        overflow = await page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
        html = await page.locator("#transcript").inner_html()
        yield page, overflow, html


def completion(outcome, text, sources, uncovered=None, truncated=False, steps=()):
    events = [event(1, "run.status", state="running", stage="streaming")]
    for index, (kind, label) in enumerate(steps, start=1):
        events.append(step(len(events) + 1, index, kind, label))
    events.append(event(len(events) + 1, "message.delta", message_id=MESSAGE_ID, text=text))
    events.append(event(len(events) + 1, "message.sources", message_id=MESSAGE_ID, outcome=outcome, sources=sources,
                        search_truncated=truncated, uncovered=uncovered))
    events.append(event(len(events) + 1, "message.completed", message_id=MESSAGE_ID, text=text))
    events.append(event(len(events) + 1, "run.status", state="completed", stage="terminal"))
    events.append(event(len(events) + 1, "stream.end", final_state="completed", final_sequence=len(events) + 1))
    final = projection("completed", len(events), text, outcome=outcome, sources=sources,
                       search_truncated=truncated, uncovered=uncovered)
    return events, final


def test_grounded_answer_lists_sources_with_text_badges(server_url):
    hostile = {"path": "queries/x.md", "title": "<img src=x onerror=window.__owned=1>", "confidence": None, "contested": False}
    events, final = completion("grounded", "답", [SOURCE, hostile])

    async def go():
        async with _run_with(server_url, events, final) as (page, overflow, _html):
            sources = page.locator("[data-ux='UX-SOURCE-LIST']")
            await expect(sources).to_have_count(1)
            await expect(sources.locator("h3")).to_have_text("근거 문서")
            await expect(sources.locator("li")).to_have_count(2)
            first = sources.locator("li").first
            await expect(first).to_contain_text("Alpha 개념")
            await expect(first).to_contain_text("concepts/alpha.md")
            await expect(first).to_contain_text("논쟁 중")
            await expect(first).to_contain_text("신뢰도 낮음")
            assert await sources.locator("img, a, button").count() == 0
            assert await page.evaluate("window.__owned") is None
            assert await sources.get_attribute("tabindex") == "0"
            assert not overflow
            # AD-29's message.sources now rides ahead of message.completed on every
            # real run (EVENT_TYPES), so a well-formed stream must never trip the
            # malformed -> polling fallback -- this is the cheap check that it does not.
            assert "확인할 수 없어" not in (await page.locator("#conversation-status").text_content() or "")
    asyncio.run(go())


def test_partial_shows_uncovered_and_truncation_notices(server_url):
    events, final = completion("partial", "일부 답", [SOURCE], uncovered="가격 정보", truncated=True)

    async def go():
        async with _run_with(server_url, events, final) as (page, overflow, _html):
            notices = page.locator("#transcript .outcome-notice")
            await expect(notices).to_have_count(2)
            await expect(notices.nth(0)).to_have_text("Wiki에 없는 부분: 가격 정보")
            await expect(notices.nth(1)).to_have_text("Wiki 검색이 한도에서 끝났어요.")
            await expect(page.locator("[data-ux='UX-SOURCE-LIST']")).to_have_count(1)
            assert not overflow
    asyncio.run(go())


@pytest.mark.parametrize("outcome,text,label", [
    ("wiki_gap", "Wiki에서 근거를 찾지 못했어요. Wiki 보충 대상이에요.", "Wiki 보충 대상"),
    ("out_of_scope", "이 질문은 Wiki가 다루는 범위 밖이라 답할 수 없어요.", "Wiki 범위 밖"),
    ("meta", "안녕하세요. llm-wiki에 정리된 내용을 근거로 답해요. AIDD 도구·워크플로·방법론을 물어보세요. 답변 아래에 근거 문서가 표시돼요.", None),
])
def test_unsourced_outcomes_render_fixed_reply_and_label(server_url, outcome, text, label):
    events, final = completion(outcome, text, [])

    async def go():
        async with _run_with(server_url, events, final) as (page, _overflow, _html):
            await expect(page.locator("[data-ux='UX-SOURCE-LIST']")).to_have_count(0)
            labels = page.locator("#transcript .outcome-label")
            if label is None:
                await expect(labels).to_have_count(0)
            else:
                await expect(labels).to_have_text(label)
    asyncio.run(go())


def test_sources_after_completed_is_malformed(server_url):
    events, final = completion("grounded", "답", [SOURCE])
    events[2], events[3] = events[3], events[2]          # completed before sources
    events[2]["sequence"], events[3]["sequence"] = 3, 4

    async def go():
        async with _run_with(server_url, events, final) as (page, _overflow, _html):
            # falls back to polling, which still renders the committed sources
            await expect(page.locator("[data-ux='UX-SOURCE-LIST'] li")).to_have_count(1)
    asyncio.run(go())


def test_mismatched_final_projection_after_completed_strips_rendered_outcome(server_url):
    """C-10.4: a source list, its notices and its label are confirmed only once
    the Run's terminal state is confirmed. A grounded/partial stream renders them
    the moment SSE commits to message.completed -- so if the poll that always
    follows stream.end comes back with a final projection that no longer matches
    the committed log (here: a wrong latest_sequence), the Run fails closed and
    none of it may survive on screen."""
    events, final = completion("partial", "일부 답", [SOURCE], uncovered="가격 정보", truncated=True)
    final["latest_sequence"] += 1

    async def go():
        async with _run_with(server_url, events, final) as (page, _overflow, _html):
            await expect(page.locator("#conversation-status")).to_contain_text("안전하게 확인할 수 없습니다")
            await expect(page.locator("[data-ux='UX-SOURCE-LIST']")).to_have_count(0)
            await expect(page.locator("#transcript .outcome-notice")).to_have_count(0)
            await expect(page.locator("#transcript .outcome-label")).to_have_count(0)
    asyncio.run(go())


def test_outcome_mismatch_between_stream_and_final_projection_is_refused(server_url):
    """The same C-10.4 guarantee, forced from the other side: the final projection
    is shape-valid on its own (a well-formed `partial` CompletedMessageV1), but its
    outcome disagrees with what message.sources already committed to over SSE
    (`grounded`) -- refused, and the grounded source list it had rendered does not
    survive the refusal."""
    events, final = completion("grounded", "답", [SOURCE])
    final["output_message"]["outcome"] = "partial"
    final["output_message"]["uncovered"] = "가격 정보"

    async def go():
        async with _run_with(server_url, events, final) as (page, _overflow, _html):
            await expect(page.locator("#conversation-status")).to_contain_text("안전하게 확인할 수 없습니다")
            await expect(page.locator("[data-ux='UX-SOURCE-LIST']")).to_have_count(0)
    asyncio.run(go())


async def _expect_fail_closed(url, events, final):
    async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
        page = await browser.new_page()
        await install_event_source(page, events)
        await stub_api(page, final)
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
        await page.locator("#prompt").fill("질문")
        await page.locator("#send-question").click()
        await expect(page.locator("#conversation-status")).to_contain_text("안전하게 확인할 수 없습니다")
        await expect(page.locator("[data-ux='UX-SOURCE-LIST']")).to_have_count(0)


def test_source_with_invalid_path_is_refused(server_url):
    # Outside the Wiki root: validSource's path rule (mirrors validate_wiki_path
    # server-side) refuses it at the message.sources Event, and the (equally
    # invalid) final projection fails the same shape check on the polling path.
    bad = {"path": "/etc/passwd", "title": "탈출 시도", "confidence": None, "contested": False}
    events, final = completion("grounded", "답", [bad])
    asyncio.run(_expect_fail_closed(server_url, events, final))


def test_duplicate_source_paths_are_refused(server_url):
    events, final = completion("grounded", "답", [SOURCE, dict(SOURCE)])
    asyncio.run(_expect_fail_closed(server_url, events, final))


def test_failed_run_shows_no_sources(server_url):
    failure = {"kind": "wiki_unavailable", "retryable": False, "correlation_id": "00000000-0000-4000-8000-000000000009",
               "message": "Wiki를 읽을 수 없어요. Wiki 경로 설정을 확인해 주세요."}
    events = [
        event(1, "run.status", state="running", stage="streaming"),
        step(2, 1, "wiki_index", "Wiki 목록 확인"),
        event(3, "message.discarded", message_id=MESSAGE_ID),
        event(4, "run.error", error=failure),
        event(5, "run.status", state="failed", stage="terminal"),
        event(6, "stream.end", final_state="failed", final_sequence=6),
    ]

    async def go():
        async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
            page = await browser.new_page()
            await install_event_source(page, events)
            await stub_api(page, projection("failed", 6, failure=failure))
            await page.goto(server_url)
            await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
            await page.locator("[data-new-conversation]").first.click()
            await page.locator("#prompt").fill("질문")
            await page.locator("#send-question").click()
            await expect(page.locator("[data-recovery-kind]")).to_have_text("wiki_unavailable")
            await expect(page.locator("[data-ux='UX-SOURCE-LIST']")).to_have_count(0)
    asyncio.run(go())


@pytest.mark.parametrize("viewport", [(1440, 1000), (1024, 900), (768, 900), (320, 800)])
def test_grounded_flow_fits_every_acceptance_viewport(server_url, viewport):
    events, final = completion("partial", "일부 답 " * 40, [SOURCE], uncovered="가격 정보 " * 10, truncated=True,
                               steps=[("wiki_index", "Wiki 목록 확인"), ("compose", "답변 작성 중")])

    async def go():
        async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
            page = await browser.new_page(viewport={"width": viewport[0], "height": viewport[1]},
                                          reduced_motion="reduce")
            await install_event_source(page, events)
            await stub_api(page, final)
            await page.goto(server_url)
            await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
            # The first question creates the conversation (Task 24), so this works at
            # every width, including below the 768px fold where the nav is closed.
            await page.locator("#prompt").fill("질문")
            await page.keyboard.press("Enter")
            await expect(page.locator("#run-announcer")).to_have_text("답변 완료")
            assert not await page.evaluate(
                "document.documentElement.scrollWidth > document.documentElement.clientWidth")
            # Task 25: the source card opens from the "근거 문서 N" pill under the answer.
            await page.locator(".sources-btn").click()
            await page.locator("[data-ux='UX-SOURCE-LIST']").focus()
            assert await page.evaluate("document.activeElement.dataset.ux") == "UX-SOURCE-LIST"
    asyncio.run(go())


def test_grounded_flow_at_200_percent_zoom(server_url):
    events, final = completion("grounded", "답", [SOURCE])

    async def go():
        async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
            page = await browser.new_page(viewport={"width": 640, "height": 800}, device_scale_factor=2)
            await page.add_init_script("document.addEventListener('DOMContentLoaded', () => { document.documentElement.style.zoom = '2'; })")
            await install_event_source(page, events)
            await stub_api(page, final)
            await page.goto(server_url)
            await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
            await page.locator("#prompt").fill("질문")
            await page.keyboard.press("Enter")
            await expect(page.locator("[data-ux='UX-SOURCE-LIST'] li")).to_have_count(1)
            assert not await page.evaluate(
                "document.documentElement.scrollWidth > document.documentElement.clientWidth")
    asyncio.run(go())


# Carried from Task 17 review: the pi binding's policy projection was no longer
# checked against app.js's isExactPolicy predicate once the old Ollama-adapter test
# (`test_ollama_policy_projection_satisfies_the_browser_predicate`) went with the
# PydanticAI path in Task 22. This is the pi-binding replacement -- and unlike
# that old test (which transcribed isExactPolicy's rules into Python), this one runs the
# real app.js in a real browser, so a JS-side rule change breaks this instead of a stale copy.
async def _pi_policy_projection_satisfies_browser_predicate(url):
    tokenizer = HfTokenizer(TOKENIZER_PATH)
    binding = AgentBindingV1(
        provider_label="Wiki agent (pi-agent-core)", endpoint_origin="http://127.0.0.1:9",
        model_revision="qwen3.5:9b", model_context_window=32768, max_output_tokens=2048,
        tokenizer_authority=TokenizerAuthorityV1(name="hf-tokenizers", sha256=tokenizer.sha256, path=str(TOKENIZER_PATH)),
        wiki_root=str(WIKI_ROOT), wiki_display_name="llm-wiki", limits=AgentLimitsV1(),
    )
    policy = build_policy_projection(pi_policy_metadata(binding)).model_dump()

    async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
        page = await browser.new_page()
        await page.goto(url)
        assert await page.evaluate("(policy) => isExactPolicy(policy)", policy)


def test_pi_binding_policy_projection_satisfies_the_browser_predicate(server_url):
    asyncio.run(_pi_policy_projection_satisfies_browser_predicate(server_url))
