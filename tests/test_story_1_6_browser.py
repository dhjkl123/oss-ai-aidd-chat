"""Story 1.6 browser contract: a stop the user can see worked.

The streaming bubble, its unanswered question and every uncommitted character go
away; the acceptance criteria's own sentence is announced exactly once and not as
an error; 재시도 and 새 대화 are both offered as controls; a double click cannot
send a second Cancel; and Esc is not a stop shortcut.

Five exercises, because the client has to reach the same end state through the
SSE log, the polling fallback, a queued Run whose log has no run.status(running),
and a Cancel whose own request fails or never settles.
"""

import asyncio
from contextlib import suppress
import json

from playwright.async_api import async_playwright, expect


CONVERSATION_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "00000000-0000-4000-8000-000000000002"
INPUT_ID = "00000000-0000-4000-8000-000000000003"
MESSAGE_ID = "00000000-0000-4000-8000-000000000004"
NOW = "2026-08-29T00:00:00Z"
QUESTION = "아주 긴 답변이 필요한 질문"
PARTIAL = "생성 중인 미완료 문장"
# The exact sentence Story 1.6's acceptance criteria require.
NOTICE = "생성을 중지했어요. 미완료 내용은 답변으로 남지 않습니다."
# The whole polite announcement for a Cancel: message, then 상태 · 영향 · 종류 ·
# retryability. The Correlation ID stays in the panel rather than being read out.
ANNOUNCEMENT = (
    f"{NOTICE} · 상태 답변 취소됨 · 영향 미완료 내용은 답변으로 남지 않습니다."
    " · 오류 종류 사용자 중지 · 재시도 다시 시도할 수 있어요"
)


def projection(state, latest):
    return {
        "schema_version": "1", "run_id": RUN_ID, "conversation_id": CONVERSATION_ID,
        "input_message_id": INPUT_ID, "retry_of_run_id": None, "output_message_id": None,
        "output_message": None, "state": state,
        "stage": "terminal" if state == "cancelled" else ("streaming" if state == "running" else "queued"),
        "created_at": NOW, "last_updated_at": NOW, "latest_sequence": latest, "terminal_error": None,
    }


def completed_projection(latest):
    return {
        **projection("completed", latest), "state": "completed", "stage": "terminal",
        "output_message_id": MESSAGE_ID,
        "output_message": {
            "message_id": MESSAGE_ID, "content": "완료된 답변", "outcome": "meta",
            "sources": [], "search_truncated": False, "uncovered": None,
        },
    }


def event(sequence, kind, **values):
    return {"schema_version": "1", "run_id": RUN_ID, "sequence": sequence, "occurred_at": NOW, "type": kind, **values}


RUNNING_HEAD = [
    event(1, "run.status", state="running", stage="streaming"),
    event(2, "message.delta", message_id=MESSAGE_ID, text=PARTIAL),
]
RUNNING_TAIL = [
    event(3, "message.discarded", message_id=MESSAGE_ID),
    event(4, "run.status", state="cancelled", stage="terminal"),
    event(5, "stream.end", final_state="cancelled", final_sequence=5),
]
# A Run cancelled before mark_running: the triplet opens the log at sequence 1,
# with no run.status(running) ahead of it.
QUEUED_TAIL = [
    event(1, "message.discarded", message_id=MESSAGE_ID),
    event(2, "run.status", state="cancelled", stage="terminal"),
    event(3, "stream.end", final_state="cancelled", final_sequence=3),
]


async def install_event_source(page, head, tail, available=True):
    payload = json.dumps({"head": head, "tail": tail, "available": available}, ensure_ascii=False)
    await page.add_init_script(
        "const {head, tail, available} = " + payload + ";" + """
        (() => {
          window.__eventSourceInstances = [];
          window.__emitCancelTail = () => {};
          window.__emit = () => {};
          if (!available) { window.EventSource = undefined; return; }
          class FakeEventSource {
            constructor() {
              this.listeners = {};
              this.closed = false;
              window.__eventSourceInstances.push(this);
              setTimeout(() => this.play(head), 50);
            }
            addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
            play(list) {
              list.forEach((value, index) => setTimeout(() => {
                if (this.closed) return;
                for (const handler of this.listeners[value.type] || [])
                  handler({lastEventId: String(value.sequence), data: JSON.stringify(value)});
              }, index * 10));
            }
            close() { this.closed = true; }
          }
          window.EventSource = FakeEventSource;
          const live = () => window.__eventSourceInstances[window.__eventSourceInstances.length - 1];
          window.__emitCancelTail = () => live().play(tail);
          window.__emit = (value) => { live().closed = false; live().play([value]); };
        })()
        """
    )


async def route_api(page, poll, cancel, submits=None):
    async def conversations(route):
        await route.fulfill(json={
            "conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-29T01:00:00Z",
        })

    async def runs(route):
        if submits is not None:
            submits.append(route.request.url)
        # Story 1.7 turned 재시도 into a RetryCommand on this same route: the accepted
        # Run then carries retry_of_run_id, which the client checks before adopting it.
        body = route.request.post_data_json or {}
        accepted = projection("queued", 0)
        if body.get("kind") == "retry":
            accepted = {**accepted, "retry_of_run_id": body["retry_of_run_id"]}
        await route.fulfill(status=202, json=accepted)

    await page.route("**/api/v1/conversations", conversations)
    await page.route("**/api/v1/conversations/*/runs", runs)
    await page.route("**/api/v1/runs/*", poll)
    # Registered last so it wins over the generic run route above.
    await page.route("**/api/v1/runs/*/cancel", cancel)


async def start_question(page, url):
    await page.goto(url)
    await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
    await page.evaluate("""
      window.__announcements = [];
      new MutationObserver(() => window.__announcements.push(document.querySelector('#run-announcer').textContent))
        .observe(document.querySelector('#run-announcer'), {childList: true, subtree: true});
      // The status line is overwritten as the flow proceeds, so a transient wrong
      // message is only visible in its history.
      window.__statuses = [];
      new MutationObserver(() => window.__statuses.push(document.querySelector('#conversation-status').textContent))
        .observe(document.querySelector('#conversation-status'), {childList: true, subtree: true});
    """)
    await page.locator("[data-new-conversation]").first.click()
    await page.locator("#prompt").fill(QUESTION)
    await page.locator("#send-question").click()


async def assert_stopped_cleanly(page):
    """The end state every path must reach, however the client learned about it."""
    await expect(page.locator("#run-announcer")).to_have_text(ANNOUNCEMENT)
    # Both bubbles go: the partial answer, and the question it never answered.
    await expect(page.locator("#transcript article")).to_have_count(0)
    assert PARTIAL not in await page.locator("#transcript").inner_text()
    await expect(page.locator("[data-stop-action]")).to_be_hidden()
    # UX-RUN-RECOVERY: both actions are controls, not prose.
    await expect(page.locator("[data-run-recovery]")).to_be_visible()
    # Story 1.7's detail rows, on the branch that has no Provider failure behind it.
    await expect(page.locator("[data-recovery-state]")).to_have_text("답변 취소됨")
    await expect(page.locator("[data-recovery-impact]")).to_contain_text("답변으로 남지 않습니다")
    await expect(page.locator("[data-recovery-kind]")).to_have_text("사용자 중지")
    await expect(page.locator("[data-recovery-retryable]")).to_have_text("다시 시도할 수 있어요")
    await expect(page.locator("[data-recovery-correlation]")).to_have_text("없음")
    await expect(page.locator("[data-recovery-message]")).to_have_text(NOTICE)
    await expect(page.locator("[data-retry-run]")).to_be_enabled()
    await expect(page.locator("[data-run-recovery] [data-new-conversation]")).to_be_enabled()
    await expect(page.locator("#prompt")).to_have_value(QUESTION)
    await expect(page.locator("#send-question")).to_be_enabled()
    assert await page.locator("[data-run-state]").inner_text() == "답변 취소됨"
    # Cancel is not an error: no failure copy, no warning styling.
    assert await page.locator("#conversation-status.warning").count() == 0
    announcements = await page.evaluate("window.__announcements")
    assert len([text for text in announcements if NOTICE in text]) == 1


async def _exercise_sse_stop_then_fail_closed(url):
    cancels = []
    state = {"run": "running", "poll": None}

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, RUNNING_HEAD, RUNNING_TAIL)

        async def poll(route):
            if state["poll"] is not None:
                await route.fulfill(json=state["poll"])
                return
            await route.fulfill(json=projection(state["run"], 5 if state["run"] == "cancelled" else 2))

        async def cancel(route):
            cancels.append(route.request.url)
            state["run"] = "cancelled"
            await route.fulfill(json={
                "schema_version": "1", "cancel_outcome": "accepted", "run": projection("cancelled", 5),
            })

        await route_api(page, poll, cancel)
        await start_question(page, url)

        stop = page.locator("[data-stop-action]")
        await expect(page.locator("article.assistant")).to_contain_text(PARTIAL)
        await expect(stop).to_be_visible()
        await expect(stop).to_be_enabled()

        # Esc is never the stop shortcut.
        await page.locator("#prompt").press("Escape")
        await page.keyboard.press("Escape")
        assert cancels == []

        before_statuses = await page.evaluate("window.__statuses.length")
        await stop.focus()
        await stop.click()
        # A second click cannot produce a second Cancel: the control is disabled the
        # moment the first request is in flight.
        await expect(stop).to_be_disabled()
        with suppress(Exception):
            await stop.click(timeout=300, force=True)
        await page.evaluate("window.__emitCancelTail()")

        await assert_stopped_cleanly(page)
        # Focus followed the stop button to the composer instead of falling to body.
        assert await page.evaluate("document.activeElement.id") == "prompt"
        # Exactly one live region fired: #conversation-status is aria-live too, so a
        # second message there would announce the same transition twice.
        assert await page.evaluate("window.__statuses.length") == before_statuses
        assert len(cancels) == 1
        assert await page.evaluate("window.__eventSourceInstances.every(item => item.closed)")

        # A projection that contradicts the cancel must fail closed *visibly*: the
        # bubble renderCancelled detached has to come back to carry the message.
        state["poll"] = completed_projection(7)
        await page.evaluate(
            "window.__emit(" + json.dumps(event(6, "message.delta", message_id=MESSAGE_ID, text="늦은"))
            + ")"
        )
        await expect(page.locator("article.assistant")).to_contain_text("실행 상태를 안전하게 확인할 수 없습니다")


async def _exercise_polling_fallback_and_aborted_cancel(url):
    """No EventSource at all -- a blocked SSE proxy, or any malformedStream fallback --
    and a Cancel whose response arrives after the client stopped waiting."""
    state = {"run": "running"}

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [], [], available=False)

        async def poll(route):
            await route.fulfill(json=projection(state["run"], 5 if state["run"] == "cancelled" else 2))

        async def cancel(route):
            # Past POST_TIMEOUT_MS: the client aborts before this resolves.
            await asyncio.sleep(2.5)
            state["run"] = "cancelled"
            await route.fulfill(json={
                "schema_version": "1", "cancel_outcome": "accepted", "run": projection("cancelled", 5),
            })

        await route_api(page, poll, cancel)
        await start_question(page, url)

        stop = page.locator("[data-stop-action]")
        await expect(stop).to_be_visible()
        await stop.click()

        await expect(page.locator("#run-announcer")).to_have_text(ANNOUNCEMENT, timeout=10_000)
        await assert_stopped_cleanly(page)
        statuses = await page.evaluate("window.__statuses")
        # An abort is not a failed stop: the committed log is the authority, so the
        # client reconciles instead of telling the user the request failed.
        assert any("결과를 확인하는 중" in text for text in statuses)
        assert not any("보내지 못했습니다" in text for text in statuses)
        assert not any("안전하게 확인할 수 없습니다" in text for text in statuses)


async def _exercise_queued_cancel_retryable_failure_and_retry(url):
    """The frozen matrix's first row: a Run stopped while still queued, whose log has
    no run.status(running). The first Cancel request fails with a 5xx, which must
    read as retryable -- and 재시도 has to actually resend."""
    attempts = []
    submits = []
    state = {"run": "queued"}

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [], QUEUED_TAIL)

        async def poll(route):
            await route.fulfill(json=projection(state["run"], 3 if state["run"] == "cancelled" else 0))

        async def cancel(route):
            attempts.append(route.request.url)
            if len(attempts) == 1:
                await route.fulfill(status=500, body="")
                return
            state["run"] = "cancelled"
            await route.fulfill(json={
                "schema_version": "1", "cancel_outcome": "accepted", "run": projection("cancelled", 3),
            })

        await route_api(page, poll, cancel, submits)
        await start_question(page, url)

        stop = page.locator("[data-stop-action]")
        await expect(stop).to_be_visible()

        await stop.click()
        await expect(page.locator("#conversation-status")).to_contain_text("다시 시도해 주세요")
        await expect(stop).to_be_enabled()

        await stop.click()
        await page.evaluate("window.__emitCancelTail()")

        await assert_stopped_cleanly(page)
        # The queued triplet is accepted as-is; degrading to polling would have
        # flashed the malformed-stream notice on the way through.
        statuses = await page.evaluate("window.__statuses")
        assert not any("실시간 응답을 확인할 수 없어" in text for text in statuses)
        assert len(attempts) == 2

        # 재시도 sends a RetryCommand for the stopped Run and adopts the new Run.
        submitted = len(submits)
        await page.locator("[data-retry-run]").click()
        await expect(page.locator("article.user")).to_have_count(1)
        assert len(submits) == submitted + 1
        # Wait for the resent Run to be live before asserting the recovery panel is
        # gone -- otherwise this races startRun rather than testing it.
        await expect(page.locator("[data-run-state]")).to_have_text("답변 대기 중")
        await expect(page.locator("[data-run-recovery]")).to_be_hidden()


async def _exercise_permanent_cancel_failure_does_not_invite_a_retry(url):
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, RUNNING_HEAD, RUNNING_TAIL)

        async def poll(route):
            await route.fulfill(json=projection("running", 2))

        async def cancel(route):
            await route.fulfill(status=404, body="")

        await route_api(page, poll, cancel)
        await start_question(page, url)

        stop = page.locator("[data-stop-action]")
        await expect(stop).to_be_visible()
        await stop.click()

        await expect(page.locator("#conversation-status")).to_contain_text("중지할 수 없습니다")
        # Pressing stop again can never succeed, so the control must not invite it.
        await expect(stop).to_be_disabled()
        await expect(page.locator("[data-new-conversation]").first).to_be_enabled()


async def _exercise_an_accepted_cancel_whose_terminal_never_arrives(url):
    """The settle path: without it the composer and the stop control stay disabled
    forever and 새 대화 is the only way out."""
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, RUNNING_HEAD, [])

        async def poll(route):
            await route.fulfill(json=projection("running", 2))

        async def cancel(route):
            await route.fulfill(json={
                "schema_version": "1", "cancel_outcome": "accepted", "run": projection("cancelled", 5),
            })

        await route_api(page, poll, cancel)
        await start_question(page, url)

        stop = page.locator("[data-stop-action]")
        await expect(stop).to_be_visible()
        await stop.click()
        await expect(stop).to_be_disabled()

        await expect(page.locator("#conversation-status")).to_contain_text(
            "중지 상태를 아직 확인하지 못했습니다", timeout=15_000
        )
        await expect(stop).to_be_enabled()
        await expect(page.locator("[data-new-conversation]").first).to_be_enabled()


def test_story_1_6_browser_sse_stop_and_fail_closed(server_url):
    asyncio.run(_exercise_sse_stop_then_fail_closed(server_url))


def test_story_1_6_browser_polling_and_aborted_cancel(server_url):
    asyncio.run(_exercise_polling_fallback_and_aborted_cancel(server_url))


def test_story_1_6_browser_queued_stop_and_retry(server_url):
    asyncio.run(_exercise_queued_cancel_retryable_failure_and_retry(server_url))


def test_story_1_6_browser_permanent_cancel_failure(server_url):
    asyncio.run(_exercise_permanent_cancel_failure_does_not_invite_a_retry(server_url))


def test_story_1_6_browser_stalled_cancel_settles(server_url):
    asyncio.run(_exercise_an_accepted_cancel_whose_terminal_never_arrives(server_url))
