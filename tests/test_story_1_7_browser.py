"""Story 1.7 browser contract: what a failed or timed-out Run tells the user.

UX-RUN-RECOVERY has to show 상태 · 영향 · 오류 종류 · retryability · Correlation ID
in that order and offer 재시도 · 새 대화 -- and 재시도 has to be an actual
RetryCommand for the Run that failed, not a fresh question. Three exercises,
because the client must reach the same place through the SSE log, through the
polling fallback (which is the only way it ever sees `timeout` without a Run
Event), and when the server refuses because the lineage is exhausted.
"""

import asyncio
import json

from playwright.async_api import async_playwright, expect


CONVERSATION_ID = "00000000-0000-4000-8000-000000000011"
RUN_ID = "00000000-0000-4000-8000-000000000012"
RETRY_RUN_ID = "00000000-0000-4000-8000-000000000013"
INPUT_ID = "00000000-0000-4000-8000-000000000014"
MESSAGE_ID = "00000000-0000-4000-8000-000000000015"
CORRELATION_ID = "00000000-0000-4000-8000-000000000016"
NOW = "2026-08-30T00:00:00Z"
QUESTION = "오래 걸리는 질문"
PARTIAL = "생성 중인 미완료 문장"
FAILURE = {
    "kind": "provider_timeout",
    "retryable": True,
    "correlation_id": CORRELATION_ID,
    "message": "응답 시간이 초과됐어요. 다시 시도해 주세요.",
}
EXHAUSTED = "재시도 가능 횟수를 모두 사용했어요. 새 대화를 시작해 주세요."
# The whole polite announcement for a Timeout. The Correlation ID is on screen in
# the recovery panel, not read out on every terminal.
ANNOUNCEMENT = (
    f"{FAILURE['message']} · 상태 답변 시간 초과"
    " · 영향 생성 중이던 내용은 답변으로 남지 않습니다."
    f" · 오류 종류 {FAILURE['kind']} · 재시도 다시 시도할 수 있어요"
)


def projection(state, latest, run_id=RUN_ID, retry_of=None):
    terminal = state not in {"queued", "running"}
    return {
        "schema_version": "1", "run_id": run_id, "conversation_id": CONVERSATION_ID,
        "input_message_id": INPUT_ID, "retry_of_run_id": retry_of, "output_message_id": None,
        "output_message": None, "state": state,
        "stage": "terminal" if terminal else ("streaming" if state == "running" else "queued"),
        "created_at": NOW, "last_updated_at": NOW, "latest_sequence": latest,
        "terminal_error": FAILURE if state in {"failed", "timeout"} else None,
    }


def event(sequence, kind, **values):
    return {"schema_version": "1", "run_id": RUN_ID, "sequence": sequence, "occurred_at": NOW, "type": kind, **values}


TIMEOUT_LOG = [
    event(1, "run.status", state="running", stage="streaming"),
    event(2, "message.delta", message_id=MESSAGE_ID, text=PARTIAL),
    event(3, "message.discarded", message_id=MESSAGE_ID),
    event(4, "run.error", error=FAILURE),
    event(5, "run.status", state="timeout", stage="terminal"),
    event(6, "stream.end", final_state="timeout", final_sequence=6),
]


async def install_event_source(page, head, tail, available=True):
    payload = json.dumps({"head": head, "tail": tail, "available": available}, ensure_ascii=False)
    await page.add_init_script(
        "const {head, tail, available} = " + payload + ";" + """
        (() => {
          window.__eventSourceInstances = [];
          window.__emitTail = () => {};
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
          window.__emitTail = () => live().play(tail);
        })()
        """
    )


async def route_api(page, poll, posts, retry_response=None):
    async def conversations(route):
        await route.fulfill(json={
            "conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-30T01:00:00Z",
        })

    async def runs(route):
        body = route.request.post_data_json or {}
        posts.append({"body": body, "key": route.request.headers.get("idempotency-key")})
        if body.get("kind") == "retry":
            if retry_response is not None:
                await retry_response(route)
                return
            await route.fulfill(status=202, json=projection(
                "queued", 0, run_id=RETRY_RUN_ID, retry_of=body["retry_of_run_id"]
            ))
            return
        await route.fulfill(status=202, json=projection("queued", 0))

    await page.route("**/api/v1/conversations", conversations)
    await page.route("**/api/v1/conversations/*/runs", runs)
    await page.route("**/api/v1/runs/*", poll)


async def start_question(page, url):
    await page.goto(url)
    await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
    await page.evaluate("""
      window.__statuses = [];
      new MutationObserver(() => window.__statuses.push(document.querySelector('#conversation-status').textContent))
        .observe(document.querySelector('#conversation-status'), {childList: true, subtree: true});
    """)
    await page.locator("[data-new-conversation]").first.click()
    await page.locator("#prompt").fill(QUESTION)
    await page.locator("#send-question").click()


async def assert_recovery_fields(page, state_label):
    """상태 · 영향 · 오류 종류 · retryability · Correlation ID, in that order, with
    both controls -- and no half-finished answer left on screen."""
    recovery = page.locator("[data-run-recovery]")
    await expect(recovery).to_be_visible()
    await expect(page.locator("[data-recovery-state]")).to_have_text(state_label)
    await expect(page.locator("[data-recovery-impact]")).to_contain_text("답변으로 남지 않습니다")
    await expect(page.locator("[data-recovery-kind]")).to_have_text(FAILURE["kind"])
    await expect(page.locator("[data-recovery-retryable]")).to_have_text("다시 시도할 수 있어요")
    await expect(page.locator("[data-recovery-correlation]")).to_have_text(CORRELATION_ID)
    await expect(page.locator("[data-run-recovery] [data-retry-run]")).to_be_enabled()
    await expect(page.locator("[data-run-recovery] [data-new-conversation]")).to_be_enabled()

    order = await recovery.locator("dt").all_text_contents()
    assert order == ["상태", "영향", "오류 종류", "재시도 가능 여부", "Correlation ID"]
    assert PARTIAL not in await page.locator("#transcript").inner_text()
    assert not any("안전하게 확인할 수 없습니다" in text for text in await page.evaluate("window.__statuses"))


async def _exercise_sse_timeout_then_retry(url):
    posts = []
    state = {"run": "running", "latest": 2}

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, TIMEOUT_LOG, [])

        async def poll(route):
            await route.fulfill(json=projection(state["run"], state["latest"]))

        await route_api(page, poll, posts)
        await start_question(page, url)

        state.update(run="timeout", latest=6)
        await expect(page.locator("[data-run-state]")).to_have_text("답변 시간 초과", timeout=10_000)
        await assert_recovery_fields(page, "답변 시간 초과")
        await expect(page.locator("#run-announcer")).to_have_text(ANNOUNCEMENT)
        # The ID a user would copy is on screen, and is not in the spoken burst.
        assert CORRELATION_ID not in ANNOUNCEMENT

        # 재시도 is a RetryCommand for the Run that timed out, not a new question.
        # Two clicks in one tick: the second must find the flight already taken.
        retry = page.locator("[data-retry-run]")
        await retry.dispatch_event("click")
        await retry.dispatch_event("click")
        await expect(page.locator("[data-run-state]")).to_have_text("답변 대기 중")
        retries = [post for post in posts if post["body"].get("kind") == "retry"]
        assert len(retries) == 1
        assert retries[0]["body"] == {"kind": "retry", "retry_of_run_id": RUN_ID}
        # The turn is the same one, so the question is not shown twice.
        await expect(page.locator("article.user")).to_have_count(1)
        await expect(page.locator("[data-run-recovery]")).to_be_hidden()


async def _exercise_polling_only_timeout(url):
    """The projection transition map has to admit `timeout`: without it the client
    fails closed on a Run it can perfectly well describe."""
    posts = []
    state = {"run": "running", "latest": 2}

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [], [], available=False)

        async def poll(route):
            await route.fulfill(json=projection(state["run"], state["latest"]))

        await route_api(page, poll, posts)
        await start_question(page, url)

        await expect(page.locator("[data-run-state]")).to_have_text("답변 생성 중", timeout=10_000)
        state.update(run="timeout", latest=6)

        await expect(page.locator("[data-run-state]")).to_have_text("답변 시간 초과", timeout=10_000)
        # Polling never saw a run.error Event, so the panel's error fields came from
        # the projection's terminal_error alone.
        await assert_recovery_fields(page, "답변 시간 초과")


async def _timed_out_page(page, url, posts, retry_response, poll=None):
    """A page sitting on a timed-out Run with its recovery panel open."""
    state = {"run": "timeout", "latest": 6}

    async def default_poll(route):
        # Answer for whichever Run is being polled, so a Run adopted by a Retry is
        # reconciled instead of read as somebody else's projection.
        run_id = RETRY_RUN_ID if RETRY_RUN_ID in route.request.url else RUN_ID
        if run_id == RETRY_RUN_ID:
            await route.fulfill(json=projection("queued", 0, run_id=run_id, retry_of=RUN_ID))
            return
        await route.fulfill(json=projection(state["run"], state["latest"]))

    await install_event_source(page, TIMEOUT_LOG, [])
    await route_api(page, poll or default_poll, posts, retry_response=retry_response)
    await start_question(page, url)
    await expect(page.locator("[data-run-state]")).to_have_text("답변 시간 초과", timeout=10_000)


async def _exercise_a_lost_retry_response_is_reconciled(url):
    """A retry POST whose response is lost may already have reached the server. The
    client resends the same key once -- an idempotent replay, not a second Attempt
    out of the lineage -- rather than showing a terminal Run beside a live one."""
    posts = []
    attempts = {"count": 0}

    async def flaky(route):
        attempts["count"] += 1
        if attempts["count"] == 1:
            await route.abort()
            return
        await route.fulfill(status=202, json=projection(
            "queued", 0, run_id=RETRY_RUN_ID, retry_of=RUN_ID
        ))

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await _timed_out_page(page, url, posts, flaky)

        # One press, two requests: the client reconciled without asking the user.
        await page.locator("[data-retry-run]").click()
        await expect(page.locator("[data-run-state]")).to_have_text("답변 대기 중", timeout=10_000)

        keys = [post["key"] for post in posts if post["body"].get("kind") == "retry"]
        assert len(keys) == 2 and keys[0] == keys[1]


async def _exercise_a_retry_that_never_answers_closes_the_conversation(url):
    """Both attempts lost. The client cannot tell whether the server is generating,
    so it stops guessing and closes the composer the way an ambiguous submit does."""
    posts = []

    async def always_lost(route):
        await route.abort()

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await _timed_out_page(page, url, posts, always_lost)

        await page.locator("[data-retry-run]").click()
        await expect(page.locator("#conversation-status")).to_have_text(
            "다시 시도 여부를 확인할 수 없습니다. 새 대화를 시작해 주세요.", timeout=10_000
        )
        await expect(page.locator("#prompt")).to_be_disabled()
        await expect(page.locator("[data-retry-run]")).to_be_disabled()
        await expect(page.locator("[data-run-recovery] [data-new-conversation]")).to_be_enabled()


async def _exercise_a_transient_refusal_leaves_retry_available(url):
    """409 run_already_active is retryable and says so. Treating it as permanent
    would lock 재시도 off for the rest of the Run with 새 대화 the only exit."""
    posts = []
    attempts = {"count": 0}

    async def busy_then_accepted(route):
        attempts["count"] += 1
        if attempts["count"] == 1:
            await route.fulfill(status=409, json={"error": {
                "code": "run_already_active",
                "message": "답변을 생성하고 있어요. 완료 후 다시 질문해 주세요.",
                "retryable": True, "correlation_id": CORRELATION_ID, "field_errors": {},
            }})
            return
        await route.fulfill(status=202, json=projection(
            "queued", 0, run_id=RETRY_RUN_ID, retry_of=RUN_ID
        ))

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await _timed_out_page(page, url, posts, busy_then_accepted)

        retry = page.locator("[data-retry-run]")
        await retry.click()
        await expect(page.locator("#conversation-status")).to_have_text(
            "답변을 생성하고 있어요. 완료 후 다시 질문해 주세요."
        )
        await expect(retry).to_be_enabled()
        await expect(page.locator("[data-retry-reason]")).to_be_empty()

        await retry.click()
        await expect(page.locator("[data-run-state]")).to_have_text("답변 대기 중", timeout=10_000)
        assert len([post for post in posts if post["body"].get("kind") == "retry"]) == 2


async def _exercise_retry_exhausted_is_not_offered_again(url):
    posts = []
    state = {"run": "running", "latest": 2}

    async def refuse(route):
        await route.fulfill(status=409, json={"error": {
            "code": "retry_exhausted", "message": EXHAUSTED, "retryable": False,
            "correlation_id": CORRELATION_ID, "field_errors": {},
        }})

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, TIMEOUT_LOG, [])

        async def poll(route):
            await route.fulfill(json=projection(state["run"], state["latest"]))

        await route_api(page, poll, posts, retry_response=refuse)
        await start_question(page, url)

        state.update(run="timeout", latest=6)
        await expect(page.locator("[data-run-state]")).to_have_text("답변 시간 초과", timeout=10_000)

        retry = page.locator("[data-retry-run]")
        await retry.click()
        await expect(page.locator("#conversation-status")).to_have_text(EXHAUSTED)
        # Pressing 재시도 again cannot succeed, so it stays disabled; 새 대화 does not.
        await expect(retry).to_be_disabled()
        # And the disabled control says why, where a screen reader will find it.
        await expect(page.locator("[data-retry-reason]")).to_have_text(EXHAUSTED)
        assert await retry.get_attribute("aria-describedby") == "retry-reason"
        await expect(page.locator("[data-run-recovery] [data-new-conversation]")).to_be_enabled()
        await expect(page.locator("article.user")).to_have_count(1)
        # Exhaustion ends this lineage, not the conversation: a new question is
        # still allowed, unlike the refusals that close the composer.
        await expect(page.locator("#prompt")).to_be_enabled()
        # The lockout is the Run's, not the button's: a further click changes nothing.
        before = len(posts)
        await retry.dispatch_event("click")
        await expect(page.locator("[data-retry-reason]")).to_have_text(EXHAUSTED)
        assert len(posts) == before


def test_story_1_7_browser_sse_timeout_and_retry(server_url) -> None:
    asyncio.run(_exercise_sse_timeout_then_retry(server_url))


def test_story_1_7_browser_polling_only_timeout(server_url) -> None:
    asyncio.run(_exercise_polling_only_timeout(server_url))


def test_story_1_7_browser_lost_retry_response_is_reconciled(server_url) -> None:
    asyncio.run(_exercise_a_lost_retry_response_is_reconciled(server_url))


def test_story_1_7_browser_unanswered_retry_closes_the_conversation(server_url) -> None:
    asyncio.run(_exercise_a_retry_that_never_answers_closes_the_conversation(server_url))


def test_story_1_7_browser_transient_refusal_keeps_retry(server_url) -> None:
    asyncio.run(_exercise_a_transient_refusal_leaves_retry_available(server_url))


def test_story_1_7_browser_retry_exhausted(server_url) -> None:
    asyncio.run(_exercise_retry_exhausted_is_not_offered_again(server_url))
