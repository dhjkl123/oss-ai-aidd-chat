import asyncio
import json

from playwright.async_api import async_playwright, expect


CONVERSATION_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "00000000-0000-4000-8000-000000000002"
INPUT_ID = "00000000-0000-4000-8000-000000000003"
MESSAGE_ID = "00000000-0000-4000-8000-000000000004"
CORRELATION_ID = "00000000-0000-4000-8000-000000000005"
NOW = "2026-08-29T00:00:00Z"


def projection(state="queued", latest=0, content=None):
    completed = state == "completed"
    return {
        "schema_version": "1", "run_id": RUN_ID, "conversation_id": CONVERSATION_ID,
        "input_message_id": INPUT_ID, "retry_of_run_id": None,
        "output_message_id": MESSAGE_ID if completed else None,
        "output_message": {
            "message_id": MESSAGE_ID, "content": content, "outcome": "meta",
            "sources": [], "search_truncated": False, "uncovered": None,
        } if completed else None,
        "state": state, "stage": "terminal" if state in {"completed", "failed"} else ("streaming" if state == "running" else "queued"),
        "created_at": NOW, "last_updated_at": NOW, "latest_sequence": latest,
        "terminal_error": None,
    }


def event(sequence, kind, **values):
    return {"schema_version": "1", "run_id": RUN_ID, "sequence": sequence, "occurred_at": NOW, "type": kind, **values}


async def install_event_source(page, batches):
    """`batches` is a list of event-lists, one per EventSource construction (i.e.
    one per question submitted in the page's lifetime)."""
    payload = json.dumps({"batches": batches}, ensure_ascii=False)
    script = (
        "const {batches} = " + payload + ";\n"
        "(() => {\n"
        "  window.__eventSourceInstances = [];\n"
        "  class FakeEventSource {\n"
        "    constructor() {\n"
        "      this.listeners = {};\n"
        "      this.closed = false;\n"
        "      this.events = batches[window.__eventSourceInstances.length] || [];\n"
        "      window.__eventSourceInstances.push(this);\n"
        "      setTimeout(() => this.play(), 50);\n"
        "    }\n"
        "    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }\n"
        "    play() {\n"
        "      this.events.forEach((value, index) => setTimeout(() => {\n"
        "        if (this.closed) return;\n"
        "        for (const handler of this.listeners[value.type] || [])\n"
        "          handler({lastEventId: String(value.sequence), data: JSON.stringify(value)});\n"
        "      }, index * 10));\n"
        "    }\n"
        "    close() { this.closed = true; }\n"
        "  }\n"
        "  window.EventSource = FakeEventSource;\n"
        "})()\n"
    )
    await page.add_init_script(script)


async def _watch_context_notice_reveals(page):
    """Counts how many times #context-notice actually becomes visible -- the
    proxy for "announced" a `role=status` polite Live Region uses, since a
    hidden->visible attribute flip is what triggers assistive tech to read it."""
    await page.evaluate(
        "window.__contextNoticeReveals = 0;"
        "new MutationObserver((mutations) => {"
        "  for (const m of mutations) {"
        "    if (m.type === 'attributes' && m.attributeName === 'hidden'"
        "      && !document.querySelector('#context-notice').hidden) {"
        "      window.__contextNoticeReveals += 1;"
        "    }"
        "  }"
        "}).observe(document.querySelector('#context-notice'), {attributes: true, attributeFilter: ['hidden']});"
    )


async def _exercise_truncated_notice_is_a_single_polite_announcement_and_resets(url):
    truncated_run = [
        event(1, "run.status", state="running", stage="streaming"),
        event(2, "context.truncated", dropped_turn_count=2),
        event(3, "message.delta", message_id=MESSAGE_ID, text="답변1"),
        event(4, "message.sources", message_id=MESSAGE_ID, outcome="meta", sources=[],
              search_truncated=False, uncovered=None),
        event(5, "message.completed", message_id=MESSAGE_ID, text="답변1"),
        event(6, "run.status", state="completed", stage="terminal"),
        event(7, "stream.end", final_state="completed", final_sequence=7),
    ]
    truncated_run_2 = [
        event(1, "run.status", state="running", stage="streaming"),
        event(2, "context.truncated", dropped_turn_count=1),
        event(3, "message.delta", message_id=MESSAGE_ID, text="답변2"),
        event(4, "message.sources", message_id=MESSAGE_ID, outcome="meta", sources=[],
              search_truncated=False, uncovered=None),
        event(5, "message.completed", message_id=MESSAGE_ID, text="답변2"),
        event(6, "run.status", state="completed", stage="terminal"),
        event(7, "stream.end", final_state="completed", final_sequence=7),
    ]
    plain_run = [
        event(1, "run.status", state="running", stage="streaming"),
        event(2, "message.delta", message_id=MESSAGE_ID, text="답변3"),
        event(3, "message.sources", message_id=MESSAGE_ID, outcome="meta", sources=[],
              search_truncated=False, uncovered=None),
        event(4, "message.completed", message_id=MESSAGE_ID, text="답변3"),
        event(5, "run.status", state="completed", stage="terminal"),
        event(6, "stream.end", final_state="completed", final_sequence=6),
    ]
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [truncated_run, truncated_run_2, plain_run])
        submissions = 0

        async def api(route):
            nonlocal submissions
            request = route.request
            if request.url.endswith("/api/v1/conversations"):
                await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z"})
            elif request.method == "POST":
                submissions += 1
                await route.fulfill(status=202, json=projection())
            else:
                content = {1: "답변1", 2: "답변2", 3: "답변3"}[submissions]
                latest = {1: 7, 2: 7, 3: 6}[submissions]
                await route.fulfill(json=projection("completed", latest, content))

        await page.route("**/api/v1/conversations", api)
        await page.route("**/api/v1/conversations/*/runs", api)
        await page.route("**/api/v1/runs/*", api)
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")

        notice = page.locator("#context-notice")
        assert await notice.is_hidden()
        # Restored per the second review round: a screen-reader user must still
        # be told context was dropped -- a single polite announcement per run,
        # not a repeated one, satisfies "not an error, not a repeated Live Region".
        assert await notice.get_attribute("role") == "status"
        assert await notice.get_attribute("aria-live") == "polite"
        assert await notice.get_attribute("aria-atomic") == "true"
        await _watch_context_notice_reveals(page)

        # Question 1: truncated. The notice appears while it's genuinely new.
        await page.locator("[data-new-conversation]").first.click()
        await page.locator("#prompt").fill("첫 질문")
        await page.locator("#send-question").click()
        await expect(page.locator("article.assistant")).to_contain_text("답변1")
        await expect(notice).to_be_visible()
        assert "제외했어요" in (await notice.text_content() or "")
        assert "warning" not in (await notice.get_attribute("class") or "")
        assert "제외했어요" not in (await page.locator("#run-announcer").text_content() or "")
        await expect(page.locator("#prompt")).to_be_enabled()

        # A new conversation clears the notice while it is still visible --
        # unlike testing this after a plain run already hid it, this actually
        # exercises createConversation()'s own reset.
        await page.locator("[data-new-conversation]").first.click()
        await expect(notice).to_be_hidden()

        # Question 2 (new conversation): truncated again, notice reappears.
        await page.locator("#prompt").fill("두번째 질문")
        await page.locator("#send-question").click()
        await expect(page.locator("article.assistant")).to_contain_text("답변2")
        await expect(notice).to_be_visible()

        # Question 3, SAME conversation, no context.truncated this time: this is
        # startRun()'s own per-run reset, isolated from the new-conversation click.
        await page.locator("#prompt").fill("세번째 질문")
        await page.locator("#send-question").click()
        await expect(page.locator("article.assistant").last).to_contain_text("답변3")
        await expect(notice).to_be_hidden()

        assert await page.evaluate("window.__contextNoticeReveals") == 2


async def _exercise_context_too_large_keeps_conversation_usable(url):
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [])

        async def api(route):
            request = route.request
            if request.url.endswith("/api/v1/conversations"):
                await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z"})
            elif request.method == "POST":
                await route.fulfill(
                    status=413,
                    json={
                        "error": {
                            "code": "context_too_large", "retryable": False,
                            "correlation_id": CORRELATION_ID,
                            "message": "질문이 너무 길어 처리할 수 없습니다. 내용을 줄여 다시 시도해 주세요.",
                            "field_errors": {},
                        }
                    },
                )
            else:
                await route.fulfill(json=projection())

        await page.route("**/api/v1/conversations", api)
        await page.route("**/api/v1/conversations/*/runs", api)
        await page.route("**/api/v1/runs/*", api)
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")

        await page.locator("[data-new-conversation]").first.click()
        await page.locator("#prompt").fill("너무 긴 질문")
        await page.locator("#send-question").click()

        await expect(page.locator("#conversation-status")).to_contain_text("내용을 줄여 다시 시도해 주세요")
        # Unlike a fail-closed rejection, the conversation stays usable: no
        # optimistic user bubble left behind, and the question text comes back.
        assert await page.locator("article.user").count() == 0
        assert await page.locator("#prompt").input_value() == "너무 긴 질문"
        await expect(page.locator("#prompt")).to_be_enabled()
        await expect(page.locator("#send-question")).to_be_enabled()


async def _exercise_context_too_large_with_unrecognized_body_fails_closed(url):
    """A 413 whose body doesn't validate as the closed context_too_large envelope
    (e.g. a proxy body-size limit's own error page) must not be treated as
    recoverable -- it follows the ordinary fail-closed 4xx path instead."""
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [])

        async def api(route):
            request = route.request
            if request.url.endswith("/api/v1/conversations"):
                await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z"})
            elif request.method == "POST":
                await route.fulfill(status=413, content_type="text/plain", body="Request Entity Too Large")
            else:
                await route.fulfill(json=projection())

        await page.route("**/api/v1/conversations", api)
        await page.route("**/api/v1/conversations/*/runs", api)
        await page.route("**/api/v1/runs/*", api)
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")

        await page.locator("[data-new-conversation]").first.click()
        await page.locator("#prompt").fill("질문")
        await page.locator("#send-question").click()

        await expect(page.locator("#conversation-status")).to_contain_text("질문을 접수하지 못했습니다")
        assert await page.locator("article.user").count() == 1
        assert await page.locator("#prompt").is_disabled()


async def _exercise_malformed_context_truncated_falls_back_to_polling(url):
    events = [
        event(1, "run.status", state="running", stage="streaming"),
        event(2, "context.truncated", dropped_turn_count=0),  # malformed: below the >=1 floor
    ]
    polls = 0

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [events])

        async def api(route):
            nonlocal polls
            request = route.request
            if request.url.endswith("/api/v1/conversations"):
                await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z"})
            elif request.method == "POST":
                await route.fulfill(status=202, json=projection("running", 1))
            else:
                polls += 1
                await route.fulfill(json=projection("completed", 6, "poll 완료"))

        await page.route("**/api/v1/conversations", api)
        await page.route("**/api/v1/conversations/*/runs", api)
        await page.route("**/api/v1/runs/*", api)
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
        await page.locator("[data-new-conversation]").first.click()
        await page.locator("#prompt").fill("질문")
        await page.locator("#send-question").click()

        await expect(page.locator("article.assistant")).to_contain_text("poll 완료", timeout=5_000)
        assert polls >= 1
        assert await page.evaluate("window.__eventSourceInstances[0].closed")
        # The malformed context.truncated was never a valid reveal -- the client
        # discarded the whole stream rather than partially trusting it.
        assert await page.locator("#context-notice").is_hidden()


def test_story_1_5_browser_context_truncated_notice(server_url):
    asyncio.run(_exercise_truncated_notice_is_a_single_polite_announcement_and_resets(server_url))


def test_story_1_5_browser_context_too_large_recoverable(server_url):
    asyncio.run(_exercise_context_too_large_keeps_conversation_usable(server_url))


def test_story_1_5_browser_context_too_large_unrecognized_body_fails_closed(server_url):
    asyncio.run(_exercise_context_too_large_with_unrecognized_body_fails_closed(server_url))


def test_story_1_5_browser_malformed_context_truncated_falls_back_to_polling(server_url):
    asyncio.run(_exercise_malformed_context_truncated_falls_back_to_polling(server_url))
