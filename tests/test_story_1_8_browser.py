"""Story 1.8 browser contract: what expiry and load shedding look like to a user.

Two things have to be distinguishable on screen. An expiry ends the conversation:
the stream closes, what the Conversation held is gone from the transcript too, and
the only offer left is 새 대화. A capacity refusal ends nothing: the question comes
back to the composer, the server's own Korean sentence explains the wait, and the
next attempt is allowed.
"""

import asyncio
import json
import re

from playwright.async_api import async_playwright, expect


CONVERSATION_ID = "00000000-0000-4000-8000-000000000021"
RUN_ID = "00000000-0000-4000-8000-000000000022"
INPUT_ID = "00000000-0000-4000-8000-000000000023"
MESSAGE_ID = "00000000-0000-4000-8000-000000000024"
CORRELATION_ID = "00000000-0000-4000-8000-000000000025"
NOW = "2026-08-30T00:00:00Z"
QUESTION = "만료 전에 보낸 질문"
PARTIAL = "생성 중이던 문장"
BUSY = "지금은 답변을 시작할 수 없어요. 잠시 후 다시 시도해 주세요."
BUSY_CONVERSATION = "지금은 새 대화를 시작할 수 없어요. 잠시 후 다시 시도해 주세요."
EXPIRED = "대화 세션이 만료되었습니다. 이전 대화 내용은 더 이상 볼 수 없어요. 새 대화를 시작해 주세요."


def projection(state, latest):
    terminal = state not in {"queued", "running"}
    return {
        "schema_version": "1", "run_id": RUN_ID, "conversation_id": CONVERSATION_ID,
        "input_message_id": INPUT_ID, "retry_of_run_id": None, "output_message_id": None,
        "output_message": None, "state": state,
        "stage": "terminal" if terminal else ("streaming" if state == "running" else "queued"),
        "created_at": NOW, "last_updated_at": NOW, "latest_sequence": latest,
        "terminal_error": None,
    }


def event(sequence, kind, **values):
    return {"schema_version": "1", "run_id": RUN_ID, "sequence": sequence, "occurred_at": NOW, "type": kind, **values}


def envelope(code, message, retryable=True):
    return {
        "error": {
            "code": code, "message": message, "retryable": retryable,
            "correlation_id": CORRELATION_ID, "field_errors": {},
        }
    }


# run.status(running) -> one delta -> the Conversation expires underneath it.
EXPIRY_LOG = [
    event(1, "run.status", state="running", stage="streaming"),
    event(2, "message.delta", message_id=MESSAGE_ID, text=PARTIAL),
]
EXPIRY_TAIL = [
    event(3, "conversation.expired"),
    event(4, "stream.end", final_state="expired", final_sequence=4),
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


async def route_api(page, poll, runs, conversations=None):
    async def created(route):
        await route.fulfill(json={
            "conversation_id": CONVERSATION_ID, "created_at": NOW, "expires_at": "2026-08-30T01:00:00Z",
        })

    await page.route("**/api/v1/conversations", conversations or created)
    await page.route("**/api/v1/conversations/*/runs", runs)
    await page.route("**/api/v1/runs/*", poll)


async def start_question(page, url):
    await page.goto(url)
    await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
    await page.locator("[data-new-conversation]").first.click()
    await page.locator("#prompt").fill(QUESTION)
    await page.locator("#send-question").click()


async def _exercise_expiry_closes_the_stream_and_offers_a_new_conversation(url):
    polls = []

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, EXPIRY_LOG, EXPIRY_TAIL)

        async def poll(route):
            polls.append(route.request.url)
            await route.fulfill(json=projection("running", 2))

        async def runs(route):
            await route.fulfill(status=202, json=projection("queued", 0))

        await route_api(page, poll, runs)
        await start_question(page, url)
        await expect(page.locator("#transcript")).to_contain_text(PARTIAL)

        polls.clear()
        await page.evaluate("window.__emitTail()")
        await expect(page.locator("#conversation-status")).to_have_text(EXPIRED, timeout=10_000)

        # The stream is closed and nothing replaced it: no polling loop starts on a
        # Conversation that has been purged.
        assert await page.evaluate(
            "window.__eventSourceInstances.every((source) => source.closed)"
        )
        await page.wait_for_timeout(400)
        assert polls == []
        # 이전 대화 내용에 접근할 수 없다 -- the transcript is emptied with it.
        await expect(page.locator("#transcript")).to_be_empty()
        assert PARTIAL not in await page.locator("#transcript").inner_text()
        await expect(page.locator("[data-run-recovery]")).to_be_hidden()
        # The composer is closed and 새 대화 is the way on.
        await expect(page.locator("#prompt")).to_be_disabled()
        await expect(page.locator("[data-new-conversation]").first).to_be_enabled()


async def _exercise_a_capacity_refusal_keeps_the_conversation_usable(url):
    posts = []
    refuse = {"value": True}

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [], [], available=False)

        async def poll(route):
            await route.fulfill(json=projection("running", 1))

        async def runs(route):
            posts.append(route.request.headers.get("idempotency-key"))
            if refuse["value"]:
                await route.fulfill(status=503, json=envelope("run_capacity_exceeded", BUSY))
                return
            await route.fulfill(status=202, json=projection("queued", 0))

        await route_api(page, poll, runs)
        await start_question(page, url)

        # The server's own sentence, and the question back where the user can resend
        # it -- unlike an ambiguous failure, this conversation is still fine.
        await expect(page.locator("#conversation-status")).to_have_text(BUSY, timeout=10_000)
        await expect(page.locator("#prompt")).to_have_value(QUESTION)
        await expect(page.locator("#prompt")).to_be_enabled()
        await expect(page.locator("#send-question")).to_be_enabled()
        await expect(page.locator("article.user")).to_have_count(0)

        refuse["value"] = False
        await page.locator("#send-question").click()
        # queued or running: the polling fallback may already have advanced it.
        await expect(page.locator("[data-run-state]")).to_have_text(
            re.compile("답변 (대기|생성) 중"), timeout=10_000
        )
        # A fresh Idempotency-Key: the refused question created nothing to replay.
        assert len(posts) == 2 and posts[0] != posts[1]


async def _exercise_a_turn_ceiling_closes_the_conversation(url):
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [], [], available=False)
        message = "이 대화에서 더 질문할 수 없어요. 새 대화를 시작해 주세요."

        async def poll(route):
            await route.fulfill(json=projection("running", 1))

        async def runs(route):
            await route.fulfill(status=409, json=envelope("turn_limit_reached", message, False))

        await route_api(page, poll, runs)
        await start_question(page, url)

        # A ceiling this conversation can never get under closes the composer, so
        # the user is not invited to type the same question again.
        await expect(page.locator("#conversation-status")).to_have_text(message, timeout=10_000)
        await expect(page.locator("#prompt")).to_be_disabled()
        await expect(page.locator("[data-new-conversation]").first).to_be_enabled()


def test_story_1_8_browser_expiry_closes_the_stream(server_url) -> None:
    asyncio.run(_exercise_expiry_closes_the_stream_and_offers_a_new_conversation(server_url))


def test_story_1_8_browser_capacity_refusal_is_recoverable(server_url) -> None:
    asyncio.run(_exercise_a_capacity_refusal_keeps_the_conversation_usable(server_url))


def test_story_1_8_browser_turn_ceiling_closes_the_conversation(server_url) -> None:
    asyncio.run(_exercise_a_turn_ceiling_closes_the_conversation(server_url))


async def _exercise_the_polling_half_of_expiry(url):
    """No EventSource at all, so the client learns about the expiry the only other
    way it can: a 410 from the poll. Without that branch it shows the generic
    failure sentence with a purged conversation's transcript still on screen."""
    state = {"expired": False}

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [], [], available=False)

        async def poll(route):
            if state["expired"]:
                await route.fulfill(
                    status=410,
                    json=envelope("conversation_expired", "대화 세션이 만료되었습니다.", False),
                )
                return
            await route.fulfill(json=projection("running", 1))

        async def runs(route):
            await route.fulfill(status=202, json=projection("queued", 0))

        await route_api(page, poll, runs)
        await start_question(page, url)
        await expect(page.locator("[data-run-state]")).to_have_text("답변 생성 중", timeout=10_000)

        state["expired"] = True
        await expect(page.locator("#conversation-status")).to_have_text(EXPIRED, timeout=10_000)
        await expect(page.locator("#transcript")).to_be_empty()
        await expect(page.locator("#prompt")).to_be_disabled()
        await expect(page.locator("[data-new-conversation]").first).to_be_enabled()


async def _exercise_a_rate_limited_poll_is_not_a_broken_run(url):
    """429 is a ceiling, not a broken Run. Treating it as fatal tears down a
    conversation that is perfectly healthy and about to answer."""
    state = {"refuse": True, "run": "running"}

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [], [], available=False)

        async def poll(route):
            if state["refuse"]:
                state["refuse"] = False
                await route.fulfill(
                    status=429, json=envelope("rate_limited", "요청이 너무 잦아요. 잠시 후 다시 시도해 주세요.")
                )
                return
            await route.fulfill(json=projection(state["run"], 1))

        async def runs(route):
            await route.fulfill(status=202, json=projection("queued", 0))

        await route_api(page, poll, runs)
        await start_question(page, url)

        # It backed off and asked again instead of failing closed.
        await expect(page.locator("[data-run-state]")).to_have_text("답변 생성 중", timeout=10_000)
        assert "안전하게 확인할 수 없습니다" not in await page.locator("#conversation-status").inner_text()
        await expect(page.locator("[data-run-recovery]")).to_be_hidden()


async def _exercise_a_refused_new_conversation_shows_the_servers_sentence(url):
    """The refusal carries a typed envelope, so the user is told it is a ceiling and
    to wait -- not the generic "기존 대화를 유지합니다" sentence for a broken request."""
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_event_source(page, [], [], available=False)

        async def conversations(route):
            await route.fulfill(
                status=503, json=envelope("conversation_capacity_exceeded", BUSY_CONVERSATION)
            )

        async def poll(route):
            await route.fulfill(json=projection("running", 1))

        async def runs(route):
            await route.fulfill(status=202, json=projection("queued", 0))

        await route_api(page, poll, runs, conversations=conversations)
        await page.goto(url)
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
        await page.locator("[data-new-conversation]").first.click()

        await expect(page.locator("#conversation-status")).to_have_text(
            BUSY_CONVERSATION, timeout=10_000
        )
        # And 새 대화 is still offered: the ceiling is a wait, not a dead end.
        await expect(page.locator("[data-new-conversation]").first).to_be_enabled()


def test_story_1_8_browser_polling_half_of_expiry(server_url) -> None:
    asyncio.run(_exercise_the_polling_half_of_expiry(server_url))


def test_story_1_8_browser_rate_limited_poll_backs_off(server_url) -> None:
    asyncio.run(_exercise_a_rate_limited_poll_is_not_a_broken_run(server_url))


def test_story_1_8_browser_refused_new_conversation_shows_the_reason(server_url) -> None:
    asyncio.run(_exercise_a_refused_new_conversation_shows_the_servers_sentence(server_url))
