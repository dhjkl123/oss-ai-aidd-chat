"""Task 25 browser contract: the owner-approved Cite shell.

What the new design adds on top of the stories' guarantees, asserted against a
running engine: Markdown answers (one block per nested fence, exact copy, no markup
from model text), the thinking line, the 근거 문서 pill, the collapsible sidebar and
the 현재 대화 item. Every test drives the client through scripted SSE Events and
stubbed API routes (wiki_agent_browser_helpers), like the wiki-agent suite.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from playwright.async_api import async_playwright, expect

from wiki_agent_browser_helpers import (
    CONVERSATION_ID, MESSAGE_ID, NOW, SOURCE, event, install_event_source, projection, stub_api,
)

F3, F4 = "```", "````"
NESTED = (
    "예시:\n\n"
    f"{F4}markdown\n### Story 3.2\n\n{F3}bash\nbmad build-auto --story 3.2\n{F3}\n{F4}\n\n"
    "끝 문단 **굵게** `code`\n\n"
    "<img src=x onerror=window.__owned=1><script>window.__owned=2</script> [링크](javascript:window.__owned=3)"
)
NESTED_CODE = f"### Story 3.2\n\n{F3}bash\nbmad build-auto --story 3.2\n{F3}"
STEPS = [("wiki_index", "Wiki 목록 확인"), ("wiki_search", "Wiki 검색 중"), ("compose", "답변 작성 중")]
OTHER = {"path": "queries/beta.md", "title": "Beta 질의", "confidence": "high", "contested": False}


def step(sequence, index, kind, label):
    return event(sequence, "agent.step", step={"step_index": index, "kind": kind, "label": label, "doc_path": None})


def completed_run(text, sources, steps=STEPS, outcome="grounded"):
    events = [event(1, "run.status", state="running", stage="streaming")]
    for index, (kind, label) in enumerate(steps, start=1):
        events.append(step(len(events) + 1, index, kind, label))
    events += [
        event(len(events) + 1, "message.delta", message_id=MESSAGE_ID, text=text),
        event(len(events) + 2, "message.sources", message_id=MESSAGE_ID, outcome=outcome, sources=sources,
              search_truncated=False, uncovered=None),
        event(len(events) + 3, "message.completed", message_id=MESSAGE_ID, text=text),
        event(len(events) + 4, "run.status", state="completed", stage="terminal"),
    ]
    events.append(event(len(events) + 1, "stream.end", final_state="completed", final_sequence=len(events) + 1))
    return events, projection("completed", len(events), text, outcome=outcome, sources=sources)


@asynccontextmanager
async def asked(url, events, final, question="질문", **context):
    async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, **context)
        page = await ctx.new_page()
        await install_event_source(page, events)
        await stub_api(page, final)
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
        await page.locator("#prompt").fill(question)
        await page.keyboard.press("Enter")
        yield page


def test_markdown_answer_keeps_nested_fence_as_one_block_copies_exact_text_and_builds_no_markup(server_url):
    events, final = completed_run(NESTED, [SOURCE])

    async def go():
        async with asked(server_url, events, final,
                         permissions=["clipboard-read", "clipboard-write"]) as page:
            await expect(page.locator("#run-announcer")).to_have_text("답변 완료")
            # The exact string handed to the clipboard, before the OS touches line endings.
            await page.evaluate("""() => {
              window.__copied = [];
              const write = navigator.clipboard.writeText.bind(navigator.clipboard);
              navigator.clipboard.writeText = (text) => { window.__copied.push(text); return write(text); };
            }""")
            answer = page.locator("article.assistant .md")
            # 예시 / one code block / 끝 문단 / the hostile paragraph -- the inner ```bash
            # fence stays text inside the outer ````markdown block.
            assert await answer.evaluate_all("(nodes) => [...nodes[0].children].map((n) => n.className || n.tagName)") \
                == ["P", "codeblock", "P", "P"]
            block = answer.locator(".codeblock")
            await expect(block).to_have_count(1)
            await expect(block.locator(".code-head span").first).to_have_text("markdown")
            assert await block.locator("pre").text_content() == NESTED_CODE
            await expect(answer.locator("strong")).to_have_text("굵게")
            await expect(answer.locator("p code")).to_have_text("code")

            # Model text is text: no element, attribute or link came out of it.
            assert await page.locator("#transcript img, #transcript script, #transcript a").count() == 0
            await expect(answer).to_contain_text("<img src=x onerror=window.__owned=1><script>")
            assert await page.evaluate("window.__owned") is None

            # The code block's copy button copies exactly the block, then says so.
            copy = block.locator(".code-head button")
            await expect(copy).to_have_text("복사")
            await copy.click()
            await expect(copy).to_have_text("복사됨")
            assert await page.evaluate("window.__copied.at(-1)") == NESTED_CODE
            # ...and it reached the system clipboard (Windows stores CRLF).
            pasted = await page.evaluate("navigator.clipboard.readText()")
            assert pasted.replace("\r\n", "\n") == NESTED_CODE
            await expect(copy).to_have_text("복사", timeout=3_000)

            # The action row's copy button copies the whole answer's Markdown source.
            await page.locator(".actions .icon-btn").click()
            assert await page.evaluate("window.__copied.at(-1)") == NESTED
    asyncio.run(go())


def test_open_fence_while_streaming_is_a_code_block_without_copy(server_url):
    events = [
        event(1, "run.status", state="running", stage="streaming"),
        event(2, "message.delta", message_id=MESSAGE_ID, text="설명\n\n```python\nprint(1)\n"),
        event(3, "message.delta", message_id=MESSAGE_ID, text="print(2"),
    ]

    async def go():
        async with asked(server_url, events, projection("running", 3)) as page:
            block = page.locator("article.assistant .codeblock")
            await expect(block.locator("pre")).to_have_text("print(1)\nprint(2")
            await expect(block.locator(".code-head span")).to_have_text("python")
            assert await block.locator("button").count() == 0
            await expect(page.locator("article.assistant small")).to_have_text("생성 중인 답변입니다.")
    asyncio.run(go())


def test_thinking_line_is_live_then_collapses_to_the_step_count(server_url):
    events, final = completed_run("답", [SOURCE])

    async def go():
        async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
            page = await browser.new_page()
            # Hold the stream after the second step, so the live line is observable.
            await install_event_source(page, events[:3])
            await stub_api(page, projection("running", 3))
            await page.goto(server_url)
            await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
            await page.locator("#prompt").fill("질문")
            await page.keyboard.press("Enter")
            think = page.locator("[data-think]")
            await expect(think).to_have_text("Wiki 검색 중")
            await expect(think).to_have_class("think live")
            # It sits in the answer it describes, above the answer text.
            assert await think.evaluate(
                "(node) => node.closest('article.assistant') !== null"
                " && !!(node.compareDocumentPosition(node.closest('article').querySelector('.md'))"
                " & Node.DOCUMENT_POSITION_FOLLOWING)")
            await page.close()

        async with asked(server_url, events, final) as page:
            await expect(page.locator("#run-announcer")).to_have_text("답변 완료")
            think = page.locator("[data-think]")
            await expect(think).to_have_text("Wiki 3단계 확인")
            await expect(think).to_have_class("think")
            steps = page.locator("[data-agent-steps] li")
            await expect(steps.first).to_be_hidden()
            await think.click()
            await expect(think).to_have_attribute("aria-expanded", "true")
            await expect(steps).to_have_text(["완료 Wiki 목록 확인", "완료 Wiki 검색 중", "완료 답변 작성 중"])
            await expect(page.locator("[data-run-state]")).to_have_text("답변 완료")
            await think.click()
            await expect(steps.first).to_be_hidden()
    asyncio.run(go())


def test_thinking_line_is_absent_for_a_settled_run_without_steps(server_url):
    events, final = completed_run("안녕하세요.", [], steps=(), outcome="meta")

    async def go():
        async with asked(server_url, events, final) as page:
            await expect(page.locator("#run-announcer")).to_have_text("답변 완료")
            await expect(page.locator("[data-think]")).to_be_hidden()
            await expect(page.locator(".sources-btn")).to_have_count(0)
            await expect(page.locator(".actions .icon-btn")).to_have_count(1)
    asyncio.run(go())


def test_sources_pill_expands_the_source_card(server_url):
    events, final = completed_run("답", [SOURCE, OTHER])

    async def go():
        async with asked(server_url, events, final) as page:
            await expect(page.locator("#run-announcer")).to_have_text("답변 완료")
            pill = page.locator(".sources-btn")
            card = page.locator("[data-ux='UX-SOURCE-LIST']")
            await expect(pill).to_have_text("W근거 문서 2")
            await expect(pill).to_have_attribute("aria-expanded", "false")
            card_id = await card.get_attribute("id")
            assert card_id and await page.locator(f"[id='{card_id}']").count() == 1
            await expect(pill).to_have_attribute("aria-controls", card_id)
            await expect(card).to_be_hidden()
            await pill.click()
            await expect(card).to_be_visible()
            await expect(pill).to_have_attribute("aria-expanded", "true")
            first = card.locator("li").first
            await expect(first.locator(".source-title")).to_have_text("Alpha 개념")
            await expect(first.locator(".source-path")).to_have_text("concepts/alpha.md")
            await expect(first.locator(".badge")).to_have_text(["논쟁 중", "신뢰도 낮음"])
            await expect(card.locator("li").nth(1).locator(".badge")).to_have_count(0)
            await pill.click()
            await expect(card).to_be_hidden()
    asyncio.run(go())


def test_sidebar_collapses_to_a_rail_and_remembers_it(server_url):
    async def go():
        async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
            ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
            page = await ctx.new_page()
            await page.goto(server_url)
            await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
            sheet = page.locator("[data-ux='UX-NAV-SHEET']")
            toggle = page.locator("[data-side-toggle]")

            async def width():
                return await sheet.evaluate("(node) => node.getBoundingClientRect().width")

            assert abs(await width() - 260) <= 1
            await expect(toggle).to_have_attribute("aria-expanded", "true")
            await expect(toggle).to_have_attribute("aria-label", "사이드바 닫기")
            await toggle.click()
            await expect(toggle).to_have_attribute("aria-expanded", "false")
            await expect(toggle).to_have_attribute("aria-label", "사이드바 열기")
            await expect(toggle).to_have_attribute("title", "사이드바 열기")
            await page.wait_for_timeout(50)
            assert abs(await width() - 52) <= 1
            await expect(page.locator(".brand")).to_be_hidden()
            # The rail keeps every destination reachable, by name.
            await expect(page.locator("[data-policy-nav]")).to_be_visible()
            await expect(page.locator("[data-policy-nav]")).to_have_attribute("title", "정보·정책")

            # Remembered across a reload.
            await page.reload()
            await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
            await expect(toggle).to_have_attribute("aria-expanded", "false")
            assert abs(await width() - 52) <= 1

            # Ctrl+Shift+S toggles it back, and that is remembered too.
            await page.locator("#prompt").focus()
            await page.keyboard.press("Control+Shift+S")
            await expect(toggle).to_have_attribute("aria-expanded", "true")
            await page.wait_for_timeout(50)
            assert abs(await width() - 260) <= 1
            await page.reload()
            await expect(toggle).to_have_attribute("aria-expanded", "true")

            # Below 1024px the Sheet owns the navigation: no toggle, no rail.
            await toggle.click()
            await page.set_viewport_size({"width": 900, "height": 800})
            await page.wait_for_timeout(50)
            await expect(toggle).to_be_hidden()
            await page.locator("[data-sheet-trigger]").click()
            await expect(sheet).to_be_visible()
            assert abs(await width() - 320) <= 1
    asyncio.run(go())


def test_current_conversation_item_shows_title_and_remaining_time(server_url):
    events, final = completed_run("답", [SOURCE])
    question = "BMAD v6.11.0에서 Build와 Build Auto는 어떻게 달라요? " * 3

    async def go():
        async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            await install_event_source(page, events)
            await stub_api(page, final)
            expires = (datetime.now(UTC) + timedelta(minutes=52, seconds=-30)).strftime("%Y-%m-%dT%H:%M:%SZ")

            async def conversations(route):
                await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": expires})

            await page.route("**/api/v1/conversations", conversations)
            await page.goto(server_url)
            await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
            item = page.locator("[data-current-conversation]")
            # No conversation yet: plain 현재 대화, no time.
            await expect(item.locator("[data-conv-title]")).to_have_text("현재 대화")
            await expect(item.locator("[data-conv-ttl]")).to_have_text("")

            await page.locator("#prompt").fill(question)
            await page.keyboard.press("Enter")
            await expect(page.locator("#run-announcer")).to_have_text("답변 완료")
            await expect(item.locator("[data-conv-title]")).to_have_text(question.strip())
            await expect(item.locator("[data-conv-ttl]")).to_have_text("52분 남음")
            await expect(item).to_have_attribute("aria-current", "page")
            await expect(item).to_have_attribute("title", question.strip())
            # Truncated to the sidebar, never widening it.
            assert await item.evaluate("(node) => node.getBoundingClientRect().right") <= 260

            # 새 대화 resets it.
            await page.locator("[data-ux='UX-NAV-SHEET'] [data-new-conversation]").click()
            await expect(page.locator("#conversation-status")).to_contain_text("빈 새 대화를 만들었습니다")
            await expect(item.locator("[data-conv-title]")).to_have_text("현재 대화")
            await expect(item.locator("[data-conv-ttl]")).to_have_text("52분 남음")
    asyncio.run(go())


def test_a_failed_answer_stays_visible_after_the_next_question(server_url):
    """Fix round 1: while the error card sits under a failed answer the card stands
    for it; once the next question moves the card away, the first turn must still
    visibly carry its failure -- a question may never look unanswered."""
    failure = {"kind": "agent_runtime_unavailable", "retryable": True,
               "correlation_id": "00000000-0000-4000-8000-000000000009",
               "message": "답변 엔진을 다시 시작하고 있어요. 잠시 후 다시 시도해 주세요."}
    failed_events = [
        event(1, "run.status", state="running", stage="streaming"),
        event(2, "message.discarded", message_id=MESSAGE_ID),
        event(3, "run.error", error=failure),
        event(4, "run.status", state="failed", stage="terminal"),
        event(5, "stream.end", final_state="failed", final_sequence=5),
    ]
    ok_events, ok_final = completed_run("두 번째 답", [SOURCE])
    finals = [projection("failed", 5, failure=failure), ok_final]
    streams = json.dumps([failed_events, ok_events], ensure_ascii=False)

    async def go():
        async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            # One scripted stream per EventSource: the failed Run, then the next one.
            await page.add_init_script("const streams = " + streams + """;
              window.EventSource = class {
                constructor() {
                  this.listeners = {}; this.closed = false;
                  const events = streams[(window.__streams = (window.__streams || 0) + 1) - 1] || [];
                  setTimeout(() => events.forEach((value, index) => setTimeout(() => {
                    if (this.closed) return;
                    for (const handler of this.listeners[value.type] || [])
                      handler({lastEventId: String(value.sequence), data: JSON.stringify(value)});
                  }, index * 10)), 50);
                }
                addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
                close() { this.closed = true; }
              };""")
            await stub_api(page, finals[0])
            state = {"final": finals[0]}

            async def poll(route):
                await route.fulfill(json=state["final"])

            await page.route("**/api/v1/runs/*", poll)
            await page.goto(server_url)
            await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
            await page.locator("#prompt").fill("첫 질문")
            await page.keyboard.press("Enter")
            recovery = page.locator("[data-ux='UX-RUN-RECOVERY']")
            await expect(recovery).to_be_visible()
            first = page.locator("article.assistant").first
            # The card stands for the answer while it is there...
            await expect(first).to_contain_text(failure["message"])
            assert await first.evaluate("(node) => node.getBoundingClientRect().height") <= 1

            await expect(page.locator("#prompt")).to_be_enabled()
            state["final"] = finals[1]
            await page.locator("#prompt").fill("다음 질문")
            await page.keyboard.press("Enter")
            await expect(page.locator("#run-announcer")).to_have_text("답변 완료")
            await expect(recovery).to_be_hidden()
            # ...and afterwards the first turn shows its failure, visibly.
            text = first.locator(".md")
            await expect(text).to_be_visible()
            await expect(text).to_have_text(failure["message"])
            assert await text.evaluate("(node) => node.getBoundingClientRect().height") > 10
            await expect(page.locator("article.assistant").nth(1).locator(".md")).to_have_text("두 번째 답")
    asyncio.run(go())


def test_the_pill_draws_the_focus_ring_while_its_textarea_has_keyboard_focus(server_url):
    """Replaces the retired 3:1 composer-border check: the composer's boundary is its
    2px #0071e3 ring whenever the keyboard is in it."""
    async def go():
        async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            await page.goto(server_url)
            await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
            pill = page.locator("[data-pill]")
            ring = ("(node) => { const s = getComputedStyle(node);"
                    " return [s.outlineStyle, s.outlineWidth, s.outlineColor]; }")
            assert (await pill.evaluate(ring))[0] == "none"
            for _ in range(40):
                await page.keyboard.press("Tab")
                if await page.evaluate("document.activeElement.id") == "prompt":
                    break
            assert await page.evaluate("document.activeElement.id") == "prompt"
            assert await pill.evaluate(ring) == ["solid", "2px", "rgb(0, 113, 227)"]
    asyncio.run(go())


def test_closed_code_blocks_stay_put_while_the_answer_streams(server_url):
    """Fix round 1: a closed code block is not rebuilt by later deltas, so its copy
    button keeps its 복사됨 state and focus; message.completed still settles the text."""
    text = "앞 문단\n\n```bash\necho 1\n```\n\n뒤 문단"
    parts = ["앞 문단\n\n```bash\necho 1\n```\n\n뒤", " 문", "단"]
    events = [event(1, "run.status", state="running", stage="streaming")]
    events += [event(index + 2, "message.delta", message_id=MESSAGE_ID, text=part) for index, part in enumerate(parts)]
    events += [
        event(5, "message.sources", message_id=MESSAGE_ID, outcome="meta", sources=[],
              search_truncated=False, uncovered=None),
        event(6, "message.completed", message_id=MESSAGE_ID, text=text),
        event(7, "run.status", state="completed", stage="terminal"),
        event(8, "stream.end", final_state="completed", final_sequence=8),
    ]

    async def go():
        async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
            page = await browser.new_page()
            await install_event_source(page, events)
            await stub_api(page, projection("completed", 8, text, outcome="meta", sources=[]))
            await page.goto(server_url)
            await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
            # Record the first code block and copy button that ever reach the page.
            await page.evaluate("""() => new MutationObserver(() => {
                window.__block ||= document.querySelector('#transcript .codeblock');
                window.__copy ||= document.querySelector('#transcript .codeblock button');
              }).observe(document.body, {childList: true, subtree: true})""")
            await page.locator("#prompt").fill("질문")
            await page.keyboard.press("Enter")
            await expect(page.locator("#run-announcer")).to_have_text("답변 완료")
            assert await page.evaluate(
                "!!window.__block && document.querySelector('#transcript .codeblock') === window.__block"
                " && document.querySelector('#transcript .codeblock button') === window.__copy")
            assert await page.locator("article.assistant .md > *").evaluate_all(
                "(nodes) => nodes.map((node) => node.textContent)") == ["앞 문단", "bash복사echo 1", "뒤 문단"]
    asyncio.run(go())
