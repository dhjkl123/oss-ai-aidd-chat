"""Story 1.11 browser contract: what `provider_profile_changed` does on screen.

Every sibling refusal code has a running browser exercise; this one had only a
regex over `app.js` source, which proves the string is in a Set and nothing about
what a user sees. It is the *inverse* of the fail-closed exercises: the 재시도
button must go away (this Run can never be re-sent -- its target keeps carrying the
old Binding Digest), while the composer stays open, because the sentence tells the
user to send the same question again.

The harness is Story 1.7's, imported rather than re-created: a second copy of the
EventSource stub and the route table would drift from the one every other refusal
is proven against.
"""

import asyncio

from playwright.async_api import async_playwright, expect

from test_story_1_7_browser import (
    CORRELATION_ID,
    RUN_ID,
    TIMEOUT_LOG,
    install_event_source,
    projection,
    route_api,
    start_question,
)


# The server sentence, verbatim from main._ERROR_ENVELOPES. Transcribed here on
# purpose: this exercise is about what the USER reads, so a change to the sentence
# has to be made deliberately in both places.
CHANGED = "Provider 설정이 바뀌어 이 답변은 다시 시도할 수 없어요. 같은 질문을 새로 보내 주세요."


async def _exercise_provider_profile_changed(url: str) -> None:
    posts = []
    state = {"run": "running", "latest": 2}

    async def refuse(route):
        await route.fulfill(status=409, json={"error": {
            "code": "provider_profile_changed", "message": CHANGED, "retryable": False,
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

        # The server's own sentence, not a generic one: the code is in
        # RETRY_ERROR_CODES, so the client is allowed to repeat it.
        await expect(page.locator("#conversation-status")).to_have_text(CHANGED)
        # `retryable: false` -- re-sending THIS Run can never succeed.
        await expect(retry).to_be_disabled()
        await expect(page.locator("[data-retry-reason]")).to_have_text(CHANGED)
        assert await retry.get_attribute("aria-describedby") == "retry-reason"
        # ...and the inverse of a fail-closed refusal: the conversation lives on, so
        # the composer and 새 대화 both stay available. This is the whole reason the
        # code is NOT in RETRY_FAIL_CLOSED_CODES.
        await expect(page.locator("#prompt")).to_be_enabled()
        await expect(page.locator("[data-run-recovery] [data-new-conversation]")).to_be_enabled()
        # One question on screen, and the refused Retry created nothing.
        await expect(page.locator("article.user")).to_have_count(1)
        retries = [post for post in posts if post["body"].get("kind") == "retry"]
        assert len(retries) == 1
        assert retries[0]["body"] == {"kind": "retry", "retry_of_run_id": RUN_ID}

        # A further click cannot succeed and does not try.
        before = len(posts)
        await retry.dispatch_event("click")
        await expect(page.locator("[data-retry-reason]")).to_have_text(CHANGED)
        assert len(posts) == before

        # The composer is genuinely usable: the same question goes out as a new one.
        await page.locator("#prompt").fill("같은 질문을 새로 보냅니다")
        await page.locator("#send-question").click()
        await expect(page.locator("article.user")).to_have_count(2)
        questions = [post for post in posts if post["body"].get("kind") == "question"]
        assert len(questions) == 2


def test_story_1_11_browser_provider_profile_changed(server_url) -> None:
    asyncio.run(_exercise_provider_profile_changed(server_url))
