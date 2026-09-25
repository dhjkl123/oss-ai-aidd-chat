import asyncio
from contextlib import suppress
import json

from playwright.async_api import async_playwright, expect


CONVERSATION_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "00000000-0000-4000-8000-000000000002"
INPUT_ID = "00000000-0000-4000-8000-000000000003"
MESSAGE_ID = "00000000-0000-4000-8000-000000000004"
CORRELATION_ID = "00000000-0000-4000-8000-000000000005"
NOW = "2026-08-29T00:00:00Z"


def projection(state="queued", latest=0, content=None, failure=None):
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
        "terminal_error": failure,
    }


def event(sequence, kind, **values):
    return {"schema_version": "1", "run_id": RUN_ID, "sequence": sequence, "occurred_at": NOW, "type": kind, **values}


async def install_event_source(page, events, fail_constructor=False):
    payload = json.dumps({"events": events, "failConstructor": fail_constructor}, ensure_ascii=False)
    await page.add_init_script(
        "const {events, failConstructor} = " + payload + ";" + """
        (() => {
          window.__eventSourceInstances = [];
          class FakeEventSource {
            constructor() {
              if (failConstructor) throw new Error('constructor failed');
              this.listeners = {};
              this.closed = false;
              window.__eventSourceInstances.push(this);
              setTimeout(() => this.play(), 50);
            }
            addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
            play() {
              events.forEach((value, index) => setTimeout(() => {
                if (this.closed) return;
                for (const handler of this.listeners[value.type] || [])
                  handler({lastEventId: String(value.sequence), data: JSON.stringify(value)});
              }, index * 10));
            }
            close() { this.closed = true; }
          }
          window.EventSource = FakeEventSource;
        })()
        """
    )


async def _exercise_streaming_same_article_plain_text_and_terminal_reconcile(url):
    hostile = '<img src=x onerror="window.__owned=1">안전'
    events = [
        event(1, "run.status", state="running", stage="streaming"),
        event(2, "message.delta", message_id=MESSAGE_ID, text=" "),
        event(3, "message.delta", message_id=MESSAGE_ID, text=hostile),
        event(4, "message.delta", message_id=MESSAGE_ID, text=" 답변"),
        event(5, "message.sources", message_id=MESSAGE_ID, outcome="meta", sources=[],
              search_truncated=False, uncovered=None),
        event(6, "message.completed", message_id=MESSAGE_ID, text=f" {hostile} 답변"),
        event(7, "run.status", state="completed", stage="terminal"),
        event(8, "stream.end", final_state="completed", final_sequence=8),
    ]
    # Browser is an AsyncContextManager: closing it here survives a failed assert, where a
    # trailing close() call at the end of the body leaks a Chromium process for the session.
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, events)

        async def api(route):
            request = route.request
            if request.url.endswith("/api/v1/conversations"):
                await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z"})
            elif request.method == "POST":
                await route.fulfill(status=202, json=projection())
            else:
                await route.fulfill(json=projection("completed", 8, f" {hostile} 답변"))

        await page.route("**/api/v1/conversations", api)
        await page.route("**/api/v1/conversations/*/runs", api)
        await page.route("**/api/v1/runs/*", api)
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
        await page.evaluate("""
          window.__announcements = [];
          new MutationObserver(() => window.__announcements.push(document.querySelector('#run-announcer').textContent))
            .observe(document.querySelector('#run-announcer'), {childList: true, subtree: true});
        """)
        await page.locator("[data-new-conversation]").first.click()
        await page.locator("#prompt").fill("질문")
        await page.locator("#send-question").click()
        assistant = page.locator("article.assistant")
        handle = await assistant.element_handle()
        await handle.evaluate("node => { node.dataset.identity = 'original'; }")
        await expect(assistant).to_contain_text(hostile)
        await expect(assistant).to_contain_text("답변")
        assert await assistant.get_attribute("data-identity") == "original"
        assert await assistant.locator("img").count() == 0
        assert await page.evaluate("window.__owned") is None
        # Wait for the terminal announcement before asserting the incomplete-answer
        # note is gone: a delta alone satisfies to_contain_text() above, so checking
        # the note first races the message.completed that removes it.
        await expect(page.locator("#run-announcer")).to_have_text("답변 완료")
        assert await assistant.locator("small").count() == 0
        assert await page.locator("article.assistant").count() == 1
        announcements = await page.evaluate("window.__announcements")
        assert announcements.count("답변 대기 중") == 1
        assert announcements.count("답변 생성 중") == 1
        assert announcements.count("답변 완료") == 1
        # run.status(completed) (which settles the announcer above) and stream.end
        # (which closes the source) are two separate Events a beat apart -- poll
        # instead of asserting immediately after the announcer, which would race it.
        for _ in range(100):
            if await page.evaluate("window.__eventSourceInstances.every(item => item.closed)"):
                break
            await asyncio.sleep(0.01)
        assert await page.evaluate("window.__eventSourceInstances.every(item => item.closed)")


async def _exercise_constructor_fallback_and_failed_projection_discards_partial(url):
    failure = {
        "kind": "provider_invalid_response", "retryable": False,
        "correlation_id": CORRELATION_ID, "message": "응답을 표시할 수 없습니다.",
    }
    # Browser is an AsyncContextManager: closing it here survives a failed assert, where a
    # trailing close() call at the end of the body leaks a Chromium process for the session.
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [], fail_constructor=True)

        async def api(route):
            request = route.request
            if request.url.endswith("/api/v1/conversations"):
                await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z"})
            elif request.method == "POST":
                await route.fulfill(status=202, json=projection("running", 2))
            else:
                await route.fulfill(json=projection("failed", 6, failure=failure))

        await page.route("**/api/v1/conversations", api)
        await page.route("**/api/v1/conversations/*/runs", api)
        await page.route("**/api/v1/runs/*", api)
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
        await page.locator("[data-new-conversation]").first.click()
        await page.locator("#prompt").fill("질문")
        await page.locator("#send-question").click()
        await expect(page.locator("article.assistant")).to_contain_text(failure["message"])
        assert "부분" not in (await page.locator("article.assistant").text_content())
        assert await page.locator("#completed-answer").evaluate(
            "node => document.activeElement !== node && node.tabIndex === 0"
        )


async def _exercise_submit_timeout_replays_same_command_and_preserves_conversation(url):
    # Browser is an AsyncContextManager: closing it here survives a failed assert, where a
    # trailing close() call at the end of the body leaks a Chromium process for the session.
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [], fail_constructor=True)
        conversation_calls = 0
        submissions = []

        async def conversations(route):
            nonlocal conversation_calls
            conversation_calls += 1
            if conversation_calls == 1:
                await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z"})
            else:
                await route.fulfill(status=503, body="")

        async def runs(route):
            submissions.append((route.request.headers.get("idempotency-key"), route.request.post_data))
            if len(submissions) == 1:
                await asyncio.sleep(2.1)
                with suppress(Exception):
                    await route.fulfill(status=202, json=projection("completed", 4, "완료"))
            else:
                await route.fulfill(status=202, json=projection("completed", 4, "완료"))

        async def poll(route):
            await route.fulfill(json=projection("completed", 4, "완료"))

        await page.route("**/api/v1/conversations", conversations)
        await page.route("**/api/v1/conversations/*/runs", runs)
        await page.route("**/api/v1/runs/*", poll)
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
        await page.locator("[data-new-conversation]").first.click()
        await page.locator("#prompt").fill("유실 질문")
        await page.locator("#send-question").click()
        await expect(page.locator("article.assistant")).to_contain_text("완료", timeout=5_000)
        assert len(submissions) == 2
        assert submissions[0] == submissions[1]
        before = await page.locator("#transcript").text_content()
        await page.locator("[data-new-conversation]").first.click()
        await expect(page.locator("#conversation-status")).to_contain_text("기존 대화를 유지합니다")
        assert await page.locator("#transcript").text_content() == before


async def _exercise_established_onerror_and_terminal_mismatch_fail_closed(url):
    # Browser is an AsyncContextManager: closing it here survives a failed assert, where a
    # trailing close() call at the end of the body leaks a Chromium process for the session.
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [])
        polls = 0

        async def api(route):
            nonlocal polls
            request = route.request
            if request.url.endswith("/api/v1/conversations"):
                await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z"})
            elif request.method == "POST":
                await route.fulfill(status=202, json=projection("running", 1))
            else:
                polls += 1
                await route.fulfill(json=projection("completed", 4, "poll 완료"))

        await page.route("**/api/v1/conversations", api)
        await page.route("**/api/v1/conversations/*/runs", api)
        await page.route("**/api/v1/runs/*", api)
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
        await page.locator("[data-new-conversation]").first.click()
        await page.locator("#prompt").fill("질문")
        await page.locator("#send-question").click()
        for _ in range(100):
            if await page.evaluate("window.__eventSourceInstances.length === 1"): break
            await asyncio.sleep(0.01)
        assert await page.evaluate("window.__eventSourceInstances.length") == 1
        await page.evaluate("window.__eventSourceInstances[0].onerror()")
        await expect(page.locator("article.assistant")).to_contain_text("poll 완료")
        assert polls == 1
        assert await page.evaluate("window.__eventSourceInstances[0].closed")

        events = [
            event(1, "run.status", state="running", stage="streaming"),
            event(2, "message.delta", message_id=MESSAGE_ID, text="답"),
            event(3, "message.sources", message_id=MESSAGE_ID, outcome="meta", sources=[],
                  search_truncated=False, uncovered=None),
            event(4, "message.completed", message_id=MESSAGE_ID, text="답"),
            event(5, "message.delta", message_id=MESSAGE_ID, text="늦은 delta"),
            event(6, "run.status", state="completed", stage="terminal"),
            event(7, "stream.end", final_state="completed", final_sequence=7),
        ]
        page2 = await browser.new_page()
        await install_event_source(page2, events)

        async def mismatch(route):
            request = route.request
            if request.url.endswith("/api/v1/conversations"):
                await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z"})
            elif request.method == "POST":
                await route.fulfill(status=202, json=projection())
            else:
                await route.fulfill(json=projection("completed", 6, "다른 답"))

        await page2.route("**/api/v1/conversations", mismatch)
        await page2.route("**/api/v1/conversations/*/runs", mismatch)
        await page2.route("**/api/v1/runs/*", mismatch)
        await page2.goto(url)
        await expect(page2.locator("#policy-status")).to_contain_text("확인했습니다")
        await page2.locator("[data-new-conversation]").first.click()
        await page2.locator("#prompt").fill("질문")
        await page2.locator("#send-question").click()
        await expect(page2.locator("#conversation-status")).to_contain_text("안전하게 확인할 수 없습니다")
        assert await page2.locator("#prompt").is_disabled()

        failure = {
            "kind": "provider_invalid_response", "retryable": False,
            "correlation_id": CORRELATION_ID, "message": "응답을 폐기했습니다.",
        }
        failed_events = [
            event(1, "run.status", state="running", stage="streaming"),
            event(2, "message.delta", message_id=MESSAGE_ID, text="부분"),
            event(3, "message.discarded", message_id=MESSAGE_ID),
            event(4, "run.error", error=failure),
            event(5, "run.status", state="failed", stage="terminal"),
            event(6, "message.delta", message_id=MESSAGE_ID, text="늦음"),
        ]
        page3 = await browser.new_page()
        await install_event_source(page3, failed_events)

        async def failed(route):
            request = route.request
            if request.url.endswith("/api/v1/conversations"):
                await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z"})
            elif request.method == "POST":
                await route.fulfill(status=202, json=projection())
            else:
                await route.fulfill(json=projection("failed", 6, failure=failure))

        await page3.route("**/api/v1/conversations", failed)
        await page3.route("**/api/v1/conversations/*/runs", failed)
        await page3.route("**/api/v1/runs/*", failed)
        await page3.goto(url)
        await expect(page3.locator("#policy-status")).to_contain_text("확인했습니다")
        await page3.locator("[data-new-conversation]").first.click()
        await page3.locator("#prompt").fill("질문")
        await page3.locator("#send-question").click()
        await expect(page3.locator("article.assistant")).to_contain_text(failure["message"])
        failed_text = await page3.locator("article.assistant").text_content()
        assert "부분" not in failed_text and "늦음" not in failed_text
        assert await page3.locator("article.assistant small").count() == 0


async def _exercise_accepted_terminal_and_malformed_post(url):
    # Browser is an AsyncContextManager: closing it here survives a failed assert, where a
    # trailing close() call at the end of the body leaks a Chromium process for the session.
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        terminal = await browser.new_page()
        await install_event_source(terminal, [])

        async def terminal_api(route):
            if route.request.url.endswith("/api/v1/conversations"):
                await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z"})
            else:
                await route.fulfill(status=202, json=projection("completed", 4, "즉시 완료"))

        await terminal.route("**/api/v1/conversations", terminal_api)
        await terminal.route("**/api/v1/conversations/*/runs", terminal_api)
        await terminal.goto(url)
        await expect(terminal.locator("#policy-status")).to_contain_text("확인했습니다")
        await terminal.locator("[data-new-conversation]").first.click()
        await terminal.locator("#prompt").fill("질문")
        await terminal.locator("#send-question").click()
        await expect(terminal.locator("article.assistant")).to_contain_text("즉시 완료")
        assert await terminal.evaluate("window.__eventSourceInstances.length") == 0
        assert not await terminal.locator("#prompt").is_disabled()

        malformed = await browser.new_page()
        await install_event_source(malformed, [])
        posts = 0

        async def malformed_api(route):
            nonlocal posts
            if route.request.url.endswith("/api/v1/conversations"):
                await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z"})
            else:
                posts += 1
                invalid = projection("failed", 4, failure={
                    "kind": "provider_future_kind", "retryable": False,
                    "correlation_id": CORRELATION_ID, "message": "실패",
                })
                await route.fulfill(status=202, json=invalid)

        await malformed.route("**/api/v1/conversations", malformed_api)
        await malformed.route("**/api/v1/conversations/*/runs", malformed_api)
        await malformed.goto(url)
        await expect(malformed.locator("#policy-status")).to_contain_text("확인했습니다")
        await malformed.locator("[data-new-conversation]").first.click()
        await malformed.locator("#prompt").fill("질문")
        await malformed.locator("#send-question").click()
        await expect(malformed.locator("#conversation-status")).to_contain_text("응답을 확인할 수 없습니다")
        assert await malformed.locator("#prompt").is_disabled()
        assert posts == 1

        rejected = await browser.new_page()
        await install_event_source(rejected, [])
        rejected_posts = 0

        async def rejected_api(route):
            nonlocal rejected_posts
            if route.request.url.endswith("/api/v1/conversations"):
                await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z"})
            else:
                rejected_posts += 1
                await route.fulfill(status=422, json={"error": {}})

        await rejected.route("**/api/v1/conversations", rejected_api)
        await rejected.route("**/api/v1/conversations/*/runs", rejected_api)
        await rejected.goto(url)
        await expect(rejected.locator("#policy-status")).to_contain_text("확인했습니다")
        await rejected.locator("[data-new-conversation]").first.click()
        await rejected.locator("#prompt").fill("질문")
        await rejected.locator("#send-question").click()
        await expect(rejected.locator("#conversation-status")).to_contain_text("질문을 접수하지 못했습니다")
        assert rejected_posts == 1


async def _exercise_pagehide_recovery_closes_and_resumes_once(url):
    # Browser is an AsyncContextManager: closing it here survives a failed assert, where a
    # trailing close() call at the end of the body leaks a Chromium process for the session.
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [])
        submissions = []
        polls = 0

        async def conversations(route):
            await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z"})

        async def runs(route):
            submissions.append((route.request.headers.get("idempotency-key"), route.request.post_data))
            if len(submissions) == 1:
                await asyncio.sleep(10)
            else:
                await route.fulfill(status=202, json=projection("running", 1))

        async def poll(route):
            nonlocal polls
            polls += 1
            await route.fulfill(json=projection("completed", 4, "복구 완료"))

        await page.route("**/api/v1/conversations", conversations)
        await page.route("**/api/v1/conversations/*/runs", runs)
        await page.route("**/api/v1/runs/*", poll)
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
        await page.locator("[data-new-conversation]").first.click()
        await page.locator("#prompt").fill("복구 질문")
        await page.locator("#send-question").click()
        for _ in range(50):
            if submissions: break
            await asyncio.sleep(0.01)
        assert len(submissions) == 1
        await page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
        await page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow'))")
        for _ in range(100):
            if await page.evaluate("window.__eventSourceInstances.length === 1"): break
            await asyncio.sleep(0.01)
        assert await page.evaluate("window.__eventSourceInstances.length") == 1
        await page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
        await page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow'))")
        await expect(page.locator("article.assistant")).to_contain_text("복구 완료", timeout=5_000)
        assert submissions[0] == submissions[1]
        assert len(submissions) == 2 and polls == 1
        assert await page.evaluate("window.__eventSourceInstances.length === 1 && window.__eventSourceInstances[0].closed")


def test_story_1_4_browser_streaming(server_url):
    asyncio.run(_exercise_streaming_same_article_plain_text_and_terminal_reconcile(server_url))


def test_story_1_4_browser_fallback(server_url):
    asyncio.run(_exercise_constructor_fallback_and_failed_projection_discards_partial(server_url))


def test_story_1_4_browser_submit_recovery(server_url):
    asyncio.run(_exercise_submit_timeout_replays_same_command_and_preserves_conversation(server_url))


def test_story_1_4_browser_onerror_and_mismatch(server_url):
    asyncio.run(_exercise_established_onerror_and_terminal_mismatch_fail_closed(server_url))


def test_story_1_4_browser_accepted_projection(server_url):
    asyncio.run(_exercise_accepted_terminal_and_malformed_post(server_url))


def test_story_1_4_browser_pagehide_recovery(server_url):
    asyncio.run(_exercise_pagehide_recovery_closes_and_resumes_once(server_url))
