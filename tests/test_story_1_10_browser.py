"""Story 1.10 browser contract: responsive layout, input-method equivalence,
keyboard reachability and the WCAG 2.2 AA floor.

None of this can be proved from source text. Whether a page scrolls sideways at 320
CSS px, whether the folded navigation really made the covered composer unreachable,
whether Esc really returns focus to the trigger, whether the landmark set survives
the fold, and whether `prefers-reduced-motion` really stops both the animation and
the transition are all facts about a running engine, so they are asserted against
one.

Every test here drives the client through stubbed API routes only -- the Story 1.10
change is entirely client-side, and the server contract is owned by 1.3-1.9.
"""

import asyncio
import json

from playwright.async_api import async_playwright, expect


CONVERSATION_ID = "00000000-0000-4000-8000-000000000101"
RUN_ID = "00000000-0000-4000-8000-000000000102"
INPUT_ID = "00000000-0000-4000-8000-000000000103"
MESSAGE_ID = "00000000-0000-4000-8000-000000000104"
CORRELATION_ID = "00000000-0000-4000-8000-000000000105"
RETRY_RUN_ID = "00000000-0000-4000-8000-000000000106"
NOW = "2026-08-30T00:00:00Z"

FAILURE = {
    "kind": "provider_unavailable",
    "retryable": True,
    "correlation_id": CORRELATION_ID,
    "message": "답변을 완료하지 못했어요. 다시 시도할 수 있습니다.",
}
# The exact announcements, so a sixth field, a reordering or a dropped one fails.
# The Correlation ID is deliberately absent: it stays on screen in the recovery
# panel rather than being read aloud on every terminal.
FAILED_ANNOUNCEMENT = (
    f"{FAILURE['message']} · 상태 답변 생성 실패 · 영향 생성 중이던 내용은 답변으로 남지 않습니다."
    " · 오류 종류 provider_unavailable · 재시도 다시 시도할 수 있어요"
)
CANCELLED_ANNOUNCEMENT = (
    "생성을 중지했어요. 미완료 내용은 답변으로 남지 않습니다. · 상태 답변 취소됨"
    " · 영향 미완료 내용은 답변으로 남지 않습니다. · 오류 종류 사용자 중지"
    " · 재시도 다시 시도할 수 있어요"
)
WIKI_SOURCE = {"path": "concepts/alpha.md", "title": "Alpha 개념", "confidence": "low", "contested": True}

# The twelve Canonical Component IDs. tests/test_story_1_10.py imports this list and
# checks markup and stylesheet against it; the loop below checks a live DOM.
CANONICAL_IDS = [
    "UX-APP-CANVAS", "UX-NAV-SHEET", "UX-COMPOSER", "UX-PRIMARY-ACTION",
    "UX-USER-MESSAGE", "UX-ASSISTANT-RESPONSE", "UX-STATUS-ANNOUNCER",
    "UX-STOP-ACTION", "UX-RUN-RECOVERY", "UX-KNOWLEDGE-NOTICE", "UX-FOCUS-RING",
    "UX-SOURCE-LIST",
]

# 1440x1000 and 1024px are the desktop acceptance viewports; 1023px, 900px and 768px
# are the folded panel including both its boundaries; 320 CSS px is the floor; and
# 512x500 is 1024px rendered at 200% zoom, which is exactly a 512 CSS px viewport.
VIEWPORTS = [(1440, 1000), (1024, 800), (1023, 800), (900, 800), (768, 800), (512, 500), (320, 640)]


def projection(state="queued", latest=0, run_id=RUN_ID, retry_of=None, updated=NOW, outcome="meta", sources=None):
    completed = state == "completed"
    failed = state in {"failed", "timeout"}
    return {
        "schema_version": "1", "run_id": run_id, "conversation_id": CONVERSATION_ID,
        "input_message_id": INPUT_ID, "retry_of_run_id": retry_of,
        "output_message_id": MESSAGE_ID if completed else None,
        "output_message": {
            "message_id": MESSAGE_ID, "content": "완료된 답변", "outcome": outcome,
            "sources": sources if sources is not None else [], "search_truncated": False, "uncovered": None,
        } if completed else None,
        "state": state,
        "stage": "terminal" if state not in {"queued", "running"} else ("streaming" if state == "running" else "queued"),
        "created_at": NOW, "last_updated_at": updated, "latest_sequence": latest,
        "terminal_error": FAILURE if failed else None,
    }


def event(sequence, kind, **values):
    return {"schema_version": "1", "run_id": RUN_ID, "sequence": sequence,
            "occurred_at": NOW, "type": kind, **values}


async def stub_event_source(page, events=None):
    """With no events, the stream fails immediately and every test runs on the
    deterministic polling path. With events, it replays exactly those and then stops
    -- which is the only way to hold a Run in a state the poller would have moved on
    from."""
    if events is None:
        await page.add_init_script(
            "window.EventSource = class {"
            "  constructor() { setTimeout(() => this.onerror && this.onerror({}), 0); }"
            "  addEventListener() {}"
            "  close() {}"
            "};"
        )
        return
    await page.add_init_script(
        "const events = " + json.dumps(events, ensure_ascii=False) + ";\n"
        "window.EventSource = class {\n"
        "  constructor() { this.listeners = {}; this.closed = false; setTimeout(() => this.play(), 30); }\n"
        "  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }\n"
        "  play() {\n"
        "    events.forEach((value, index) => setTimeout(() => {\n"
        "      if (this.closed) return;\n"
        "      for (const handler of this.listeners[value.type] || [])\n"
        "        handler({lastEventId: String(value.sequence), data: JSON.stringify(value)});\n"
        "    }, index * 10));\n"
        "  }\n"
        "  close() { this.closed = true; }\n"
        "};"
    )


async def route_api(page, state, posts=None):
    async def conversations(route):
        state["creates"] = state.get("creates", 0) + 1
        if state.get("create_delay"):
            await asyncio.sleep(state["create_delay"])
        if state.get("create_status"):
            await route.fulfill(status=state["create_status"], body="")
            return
        await route.fulfill(json={
            "conversation_id": CONVERSATION_ID, "created_at": NOW,
            "expires_at": "2026-08-30T01:00:00Z",
        })

    async def runs(route):
        body = route.request.post_data_json or {}
        if posts is not None:
            posts.append(body)
        if state.get("submit_status"):
            await route.fulfill(status=state["submit_status"], body="")
            return
        if body.get("kind") == "retry":
            if state.get("bad_retry"):
                # A retry projection that does not name the Run it retries: the
                # client fails closed and re-attaches the detached answer bubble.
                await route.fulfill(status=202, json=projection("queued", 0, RETRY_RUN_ID))
                return
            state.update(run="queued", latest=0, run_id=RETRY_RUN_ID, retry_of=RUN_ID)
        await route.fulfill(status=202, json=projection(
            "queued", 0, state.get("run_id", RUN_ID), state.get("retry_of")))

    async def cancel(route):
        # A gate holds the cancel uncommitted -- the poller keeps seeing `running` --
        # until the test releases it, so "still in flight" is a fact, not a race.
        if state.get("cancel_gate"):
            await state["cancel_gate"].wait()
        state.update(run="cancelled", latest=5)
        await route.fulfill(json={
            "schema_version": "1", "cancel_outcome": "accepted",
            "run": projection("cancelled", 5, state.get("run_id", RUN_ID), state.get("retry_of")),
        })

    async def poll(route):
        await route.fulfill(json=projection(
            state["run"], state["latest"], state.get("run_id", RUN_ID), state.get("retry_of"),
            state.get("updated", NOW), state.get("outcome", "meta"), state.get("sources")))

    await page.route("**/api/v1/conversations", conversations)
    await page.route("**/api/v1/conversations/*/runs", runs)
    await page.route("**/api/v1/runs/*", poll)
    # Registered last so it wins over the generic run route above.
    await page.route("**/api/v1/runs/*/cancel", cancel)


async def open_page(browser, url, state, posts=None, policy_ready=True, events=None,
                    scripts=(), **context_options):
    context = await browser.new_context(**context_options)
    page = await context.new_page()
    for script in scripts:
        await page.add_init_script(script)
    await stub_event_source(page, events)
    if not policy_ready:
        await page.route("**/api/v1/policy", lambda route: route.fulfill(status=503, body=""))
    await route_api(page, state, posts)
    await page.goto(url)
    await expect(page.locator("#policy-status")).to_contain_text(
        "확인했습니다" if policy_ready else "확인할 수 없습니다")
    return page


async def overflowing(page):
    """Every element whose box reaches past either edge of the viewport. The
    stylesheet no longer clips with `overflow-x: hidden`, so an overflow is a real
    one and this is what fails on it."""
    return await page.evaluate(
        "[...document.querySelectorAll('body *')]"
        "  .filter((node) => { const box = node.getBoundingClientRect();"
        "    return box.right > window.innerWidth + 1 || box.left < -1; })"
        "  .map((node) => node.tagName + (node.id ? '#' + node.id : '') + '.' + node.className)"
    )


async def scrolls_sideways(page):
    """The page-level guarantee itself, which per-element boxes only approximate."""
    return await page.evaluate(
        "(() => {"
        "  const root = document.documentElement;"
        "  return Math.max(root.scrollWidth, document.body.scrollWidth) > window.innerWidth + 1"
        "    || root.scrollLeft !== 0;"
        "})()"
    )


async def tab_to(page, selector, limit=40):
    for _ in range(limit):
        await page.keyboard.press("Tab")
        if await page.evaluate("(s) => document.activeElement === document.querySelector(s)", selector):
            return True
    return False


async def sheet_state(page):
    return await page.evaluate(
        "(() => {"
        "  const sheet = document.querySelector('[data-ux=\"UX-NAV-SHEET\"]');"
        "  const box = sheet.getBoundingClientRect();"
        "  return {"
        "    open: sheet.open, mode: sheet.dataset.mode, modal: sheet.matches(':modal'),"
        "    role: sheet.getAttribute('role'), width: box.width,"
        "    expanded: document.querySelector('[data-sheet-trigger]').getAttribute('aria-expanded'),"
        "  };"
        "})()"
    )


async def focusable(page, selector):
    """Whether focus can actually land on the element -- the only honest test of an
    inert background, since modality is not exposed as the `inert` IDL attribute."""
    return await page.evaluate(
        "(s) => { const node = document.querySelector(s); node.focus();"
        "  return document.activeElement === node; }",
        selector,
    )


async def primary_actions(page):
    """The controls currently *emphasised* as the screen's one primary action."""
    return await page.locator("[data-ux='UX-PRIMARY-ACTION']:visible").evaluate_all(
        "(nodes) => nodes.map((node) => node.textContent.trim())")


async def unnamed_controls(page):
    """Every visible interactive control without an accessible name."""
    return await page.evaluate(
        "[...document.querySelectorAll('button, a[href], textarea, input, [tabindex]')]"
        "  .filter((node) => node.checkVisibility())"
        "  .filter((node) => !("
        "    node.getAttribute('aria-label')"
        "    || (node.labels && node.labels.length && node.labels[0].textContent.trim())"
        "    || node.textContent.trim() || node.title).trim())"
        "  .map((node) => node.tagName + (node.id ? '#' + node.id : ''))"
    )


async def _exercise_three_breakpoints_never_scroll_sideways(url):
    state = {"run": "queued", "latest": 0}
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await open_page(browser, url, state, viewport={"width": 1440, "height": 1000})

        # The desktop column is opened by app.js, and show() runs the dialog focusing
        # steps -- so the skip link has to still be the first Tab stop.
        await page.keyboard.press("Tab")
        assert await page.evaluate("document.activeElement.matches('a.skip-link')")

        sheet = page.locator("[data-ux='UX-NAV-SHEET']")
        trigger = page.locator("[data-sheet-trigger]")

        for width, height in VIEWPORTS:
            await page.set_viewport_size({"width": width, "height": height})
            # A resize is a media-query change; give the listener its tick.
            await page.wait_for_timeout(50)
            before = await sheet_state(page)
            # The landmark set does not flip with the viewport: a navigation
            # landmark exists at every width, and so does the banner or the column.
            assert await page.get_by_role("navigation").count() >= 1, width

            if width >= 1024:
                # 260px sidebar column in the flow, no menu trigger -- and
                # announced as persistent chrome, not as a dialog that never closes.
                await expect(sheet).to_be_visible()
                await expect(trigger).to_be_hidden()
                assert (before["mode"], before["modal"], before["role"]) == ("column", False, "complementary"), before
                assert abs(before["width"] - 260) <= 1, width
                assert await page.get_by_role("complementary").count() >= 1, width
            else:
                # Folded navigation: reachable only through the trigger, which lives
                # in the banner.
                await expect(sheet).to_be_hidden()
                await expect(trigger).to_be_visible()
                assert before["expanded"] == "false", width
                assert await page.get_by_role("banner").count() == 1, width

            await expect(page.locator("[data-ux='UX-COMPOSER']")).to_be_visible()
            await expect(page.locator("#prompt")).to_be_visible()
            assert await overflowing(page) == [], (width, await overflowing(page))
            assert not await scrolls_sideways(page), width
            assert await unnamed_controls(page) == [], width

            if width < 1024:
                await trigger.click()
                await expect(sheet).to_be_visible()
                opened = await sheet_state(page)
                assert opened["expanded"] == "true", width
                assert await overflowing(page) == [], (width, "sheet open")
                assert not await scrolls_sideways(page), (width, "sheet open")
                # Both folded modes are modal, so nothing they cover stays reachable.
                # `panel` is a side sheet; `sheet` takes the full width.
                assert opened["modal"] is True, (width, opened)
                assert opened["mode"] == ("sheet" if width < 768 else "panel"), width
                assert opened["role"] is None, width
                expected = width if width < 768 else min(round(0.88 * width), 320)
                assert abs(opened["width"] - expected) <= 1, (width, opened["width"])
                assert await focusable(page, "#prompt") is False, width
                await page.keyboard.press("Escape")
                await expect(sheet).to_be_hidden()
                # dialog.close() queues the `close` event rather than firing it
                # synchronously, so this has to be a polling assertion.
                await expect(trigger).to_have_attribute("aria-expanded", "false")

        # The upward leg: a Sheet opened while narrow must become the column again
        # when the window widens, with no surviving modality and no inert page.
        for start in (768, 375):
            await page.set_viewport_size({"width": start, "height": 800})
            await page.wait_for_timeout(50)
            await trigger.click()
            assert (await sheet_state(page))["modal"] is True, start
            await page.set_viewport_size({"width": 1440, "height": 1000})
            await page.wait_for_timeout(50)
            widened = await sheet_state(page)
            assert (widened["open"], widened["mode"], widened["modal"]) == (True, "column", False), widened
            assert widened["role"] == "complementary" and widened["expanded"] == "false", widened
            assert abs(widened["width"] - 260) <= 1
            await expect(trigger).to_be_hidden()
            assert await focusable(page, "#prompt")
            assert not await scrolls_sideways(page)

        # Crossing 768px with the Sheet open keeps the navigation rather than
        # dropping it mid-interaction.
        await page.set_viewport_size({"width": 900, "height": 800})
        await page.wait_for_timeout(50)
        await trigger.click()
        await page.set_viewport_size({"width": 375, "height": 700})
        await page.wait_for_timeout(50)
        crossed = await sheet_state(page)
        assert (crossed["open"], crossed["mode"], crossed["modal"]) == (True, "sheet", True), crossed
        assert abs(crossed["width"] - 375) <= 1

        await browser.close()


async def _exercise_sheet_is_inert_trapped_and_returns_focus(url):
    state = {"run": "queued", "latest": 0}
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await open_page(browser, url, state, viewport={"width": 375, "height": 700})

        sheet = page.locator("[data-ux='UX-NAV-SHEET']")
        assert await tab_to(page, "[data-sheet-trigger]")
        await page.keyboard.press("Enter")
        await expect(sheet).to_be_visible()

        # Native modality: the background is inert, so no amount of Tab can reach
        # anything behind the Sheet. (Focus passes through the document root as the
        # cycle wraps, which is the browser's own containment, not an escape.)
        visited = []
        for _ in range(15):
            await page.keyboard.press("Tab")
            visited.append(await page.evaluate(
                "(() => {"
                "  const sheet = document.querySelector('[data-ux=\"UX-NAV-SHEET\"]');"
                "  const active = document.activeElement;"
                "  if (active === document.body || active === document.documentElement) return 'root';"
                "  return sheet.contains(active) ? 'sheet' : 'ESCAPED:' + (active.id || active.tagName);"
                "})()"
            ))
        assert not [entry for entry in visited if entry.startswith("ESCAPED")], visited
        assert "sheet" in visited

        # Esc closes it and focus lands back on the trigger that opened it.
        await page.keyboard.press("Escape")
        await expect(sheet).to_be_hidden()
        assert await page.evaluate(
            "document.activeElement === document.querySelector('[data-sheet-trigger]')"
        )

        # The accessible close control does the same thing for pointer and touch.
        await page.keyboard.press("Enter")
        await expect(sheet).to_be_visible()
        await page.locator("[data-sheet-close]").click()
        await expect(sheet).to_be_hidden()
        assert await page.evaluate(
            "document.activeElement === document.querySelector('[data-sheet-trigger]')"
        )

        # A click on the scrim dismisses the side panel, the way a modal is expected
        # to. (Below 768px the Sheet is full width, so there is no scrim to click.)
        await page.set_viewport_size({"width": 900, "height": 800})
        await page.wait_for_timeout(50)
        await page.locator("[data-sheet-trigger]").click()
        await expect(sheet).to_be_visible()
        await page.mouse.click(800, 400)
        await expect(sheet).to_be_hidden()
        await page.set_viewport_size({"width": 375, "height": 700})
        await page.wait_for_timeout(50)

        # A malformed in-sheet fragment must not strand focus behind an open Sheet.
        await page.locator("[data-sheet-trigger]").click()
        await page.locator("[data-policy-nav]").evaluate("(node) => node.setAttribute('href', '#')")
        await page.locator("[data-policy-nav]").click()
        await expect(sheet).to_be_hidden()

        # Narrowing under an open Sheet must not drop focus on <body>: the trigger
        # is where the navigation went, so that is where the user goes.
        await page.set_viewport_size({"width": 1440, "height": 900})
        await page.wait_for_timeout(50)
        await page.locator(".brand").focus()
        await page.set_viewport_size({"width": 375, "height": 700})
        await page.wait_for_timeout(50)
        assert await page.evaluate(
            "document.activeElement === document.querySelector('[data-sheet-trigger]')"
        )

        # And in column mode the close control is not offered at all -- dismissing
        # permanent chrome would hide the navigation and its trigger together.
        await page.set_viewport_size({"width": 1440, "height": 900})
        await page.wait_for_timeout(50)
        await expect(page.locator("[data-sheet-close]")).to_be_hidden()
        await page.locator("[data-sheet-close]").evaluate("(node) => node.click()")
        await expect(sheet).to_be_visible()

        await browser.close()


async def _exercise_keyboard_only_reaches_every_core_action(url):
    """Tab, Enter and Esc only: 새 대화 → 질문 → 중지 → 재시도 → 후속 질문 →
    정보·정책, at the 320 CSS px floor where every one of them is behind the folded
    navigation."""
    state = {"run": "running", "latest": 2}
    posts = []
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await open_page(browser, url, state, posts, viewport={"width": 320, "height": 640})

        # One emphasised action per screen. Task 24 retired the 새 대화 시작 gate (the
        # first question creates the conversation), so on the empty canvas it is
        # already 질문 보내기.
        assert await primary_actions(page) == ["질문 보내기"]
        # 새 대화 is still a keyboard action: at this width it sits behind the folded
        # navigation.
        assert await tab_to(page, "[data-sheet-trigger]")
        await page.keyboard.press("Enter")
        assert await tab_to(page, "[data-ux='UX-NAV-SHEET'] [data-new-conversation]", limit=10)
        await page.keyboard.press("Enter")
        await expect(page.locator("#conversation-status")).to_contain_text("빈 새 대화를 만들었습니다")
        # ...and once a conversation exists it is still 질문 보내기, alone.
        await expect(page.locator("#send-question")).to_have_attribute("data-ux", "UX-PRIMARY-ACTION")
        assert await primary_actions(page) == ["질문 보내기"]

        assert await tab_to(page, "#prompt")
        await page.keyboard.type("키보드로 보내는 질문")
        await page.keyboard.press("Enter")
        assert [post["kind"] for post in posts] == ["question"]
        await expect(page.locator("[data-ux='UX-USER-MESSAGE']")).to_have_count(1)

        stop = page.locator("[data-ux='UX-STOP-ACTION']")
        await expect(stop).to_be_visible()
        # The composer disabled under the user's focus; the control that replaced it
        # takes over, rather than dropping focus on <body> or jumping to <main>.
        await expect(stop).to_be_focused()
        assert await overflowing(page) == [] and not await scrolls_sideways(page)
        # UX-FOCUS-RING, on a control the keyboard actually reached.
        ring = await stop.evaluate(
            "(node) => { const style = getComputedStyle(node);"
            "  return `${style.outlineWidth} ${style.outlineStyle}`; }"
        )
        assert ring == "2px solid", ring
        await page.keyboard.press("Enter")

        recovery = page.locator("[data-ux='UX-RUN-RECOVERY']")
        await expect(recovery).to_be_visible(timeout=10_000)
        # A cancel has no Provider failure behind it, and says so on both channels.
        await expect(page.locator("#run-announcer")).to_have_text(CANCELLED_ANNOUNCEMENT)
        # The recovery screen asks for 재시도, and only 재시도.
        assert await primary_actions(page) == ["재시도"]
        assert await overflowing(page) == [] and not await scrolls_sideways(page)

        retry = page.locator("[data-retry-run]")
        await expect(retry).to_be_enabled()
        assert await tab_to(page, "[data-retry-run]")
        await page.keyboard.press("Enter")
        await expect(page.locator("[data-run-state]")).to_have_text("답변 대기 중", timeout=10_000)
        assert [post["kind"] for post in posts] == ["question", "retry"]

        # 후속 질문: the same session carries a second question after the first
        # settles, by keyboard, with no extra ceremony. Grounded with a source, so
        # the canonical-ID sweep below finds UX-SOURCE-LIST too.
        state.update(run="completed", latest=9, outcome="grounded", sources=[WIKI_SOURCE])
        await expect(page.locator("#prompt")).to_be_enabled(timeout=10_000)
        assert await tab_to(page, "#prompt")
        await page.keyboard.type("같은 세션의 후속 질문")
        await page.keyboard.press("Enter")
        assert [post["kind"] for post in posts] == ["question", "retry", "question"]
        await expect(page.locator("[data-ux='UX-USER-MESSAGE']")).to_have_count(2)

        # 정책 확인 is only reachable through the Sheet at this width, by keyboard --
        # and choosing a destination has to leave focus on the destination, not back
        # on the menu button the user just used.
        assert await tab_to(page, "[data-sheet-trigger]")
        await page.keyboard.press("Enter")
        assert await tab_to(page, "[data-policy-nav]", limit=10)
        await page.keyboard.press("Enter")
        await expect(page.locator("[data-ux='UX-NAV-SHEET']")).to_be_hidden()
        assert await page.evaluate("document.activeElement.id") == "policy-panel"
        await expect(page.locator("[data-ux='UX-KNOWLEDGE-NOTICE']")).to_be_visible()

        # Every Canonical ID is on the page by now, under the one join key, and every
        # visible control has an accessible name.
        for name in CANONICAL_IDS:
            assert await page.locator(f"[data-ux='{name}']").count() >= 1, name
        assert await unnamed_controls(page) == []

        await browser.close()


async def _exercise_touch_reaches_the_same_core_actions(url):
    """Mouse, touch and keyboard must offer the same actions, and nothing may live
    on hover. Touch gets its own pass through the same flow."""
    state = {"run": "running", "latest": 2}
    posts = []
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await open_page(browser, url, state, posts, has_touch=True, is_mobile=True,
                               viewport={"width": 375, "height": 700})

        # No start gate (Task 24): the composer is live once the policy is, and the
        # first question creates the conversation.
        await expect(page.locator("#prompt")).to_be_enabled()
        await page.locator("#prompt").tap()
        await page.locator("#prompt").fill("손가락으로 보내는 질문")
        await page.locator("#send-question").tap()
        await expect(page.locator("[data-ux='UX-STOP-ACTION']")).to_be_visible()
        await page.locator("[data-ux='UX-STOP-ACTION']").tap()
        await expect(page.locator("[data-ux='UX-RUN-RECOVERY']")).to_be_visible(timeout=10_000)
        await page.locator("[data-retry-run]").tap()
        await expect(page.locator("[data-run-state]")).to_have_text("답변 대기 중", timeout=10_000)
        assert [post["kind"] for post in posts] == ["question", "retry"]

        # The navigation, its close control and the policy surface are all reachable
        # by tap alone -- nothing hides behind hover.
        await page.locator("[data-sheet-trigger]").tap()
        await expect(page.locator("[data-ux='UX-NAV-SHEET']")).to_be_visible()
        await page.locator("[data-policy-nav]").tap()
        await expect(page.locator("[data-ux='UX-NAV-SHEET']")).to_be_hidden()
        await expect(page.locator("[data-ux='UX-KNOWLEDGE-NOTICE']")).to_be_visible()
        assert not await scrolls_sideways(page)

        await browser.close()


async def _exercise_cancel_handoff_waits_for_the_sheet(url):
    """A Cancel hands focus from 생성 중지 back to the composer. If the Sheet is open
    the composer is behind an inert background, so the handoff has to wait -- and
    then happen immediately when the Sheet closes, not on the next state change."""
    state = {"run": "running", "latest": 2, "cancel_gate": asyncio.Event()}
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await open_page(browser, url, state, viewport={"width": 375, "height": 700})
        await page.locator("#prompt").fill("중지할 질문")
        await page.locator("#send-question").click()

        stop = page.locator("[data-ux='UX-STOP-ACTION']")
        await expect(stop).to_be_visible()
        await stop.focus()
        await stop.click()
        # The Sheet goes up while the cancel is still in flight.
        await page.locator("[data-sheet-trigger]").click()
        await expect(page.locator("[data-ux='UX-NAV-SHEET']")).to_be_visible()
        state["cancel_gate"].set()
        await expect(page.locator("[data-ux='UX-RUN-RECOVERY']")).to_be_visible(timeout=10_000)
        # The composer is inert; focus stayed inside the Sheet.
        assert await page.evaluate(
            "document.querySelector('[data-ux=\"UX-NAV-SHEET\"]').contains(document.activeElement)"
        )
        await page.keyboard.press("Escape")
        await expect(page.locator("#prompt")).to_be_focused()

        await browser.close()


async def _exercise_state_reaches_the_screen_reader_as_text(url):
    """Screen and announcer carry the same 상태 · 영향 · 종류 · retryability, once per
    transition, and none of it depends on colour."""
    state = {"run": "running", "latest": 2, "updated": "2026-08-30T00:11:22Z"}
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await open_page(browser, url, state)
        await page.evaluate(
            "window.__announcements = [];"
            "new MutationObserver(() => window.__announcements.push("
            "  document.querySelector('#run-announcer').textContent))"
            "  .observe(document.querySelector('#run-announcer'), {childList: true, subtree: true});"
        )
        await page.locator("#prompt").fill("실패하는 질문")
        await page.locator("#send-question").click()
        await expect(page.locator("[data-run-state]")).to_have_text("답변 생성 중", timeout=10_000)

        # UX-STATUS-ANNOUNCER is the visible surface, and it carries the 갱신 시각
        # both spine documents name -- as a time, not as a raw timestamp. Since Task 25
        # it is the thinking line: the time is one click away, under the steps.
        summary = page.locator("[data-ux='UX-STATUS-ANNOUNCER']")
        await expect(summary).to_be_visible()
        await summary.locator("[data-think]").click()
        await expect(page.locator("[data-run-updated]")).to_be_visible()
        updated = await page.locator("[data-run-updated]").text_content()
        expected = await page.evaluate(
            "new Date('2026-08-30T00:11:22Z').toLocaleTimeString('ko-KR')")
        assert updated == expected and updated != "확인되지 않음", updated

        state.update(run="failed", latest=6)
        await expect(page.locator("[data-run-state]")).to_have_text("답변 생성 실패", timeout=10_000)

        await expect(page.locator("#run-announcer")).to_have_text(FAILED_ANNOUNCEMENT)
        # Exactly one announcement carries the terminal -- a polite Live Region that
        # repeats is a Live Region users turn off.
        announcements = await page.evaluate("window.__announcements")
        assert len([text for text in announcements if "답변 생성 실패" in text]) == 1

        # The five facts are on screen as text, not as a colour -- including the
        # Correlation ID, which stays visible and copyable instead of being spoken.
        await expect(page.locator("[data-recovery-state]")).to_have_text("답변 생성 실패")
        await expect(page.locator("[data-recovery-kind]")).to_have_text(FAILURE["kind"])
        await expect(page.locator("[data-recovery-retryable]")).to_have_text("다시 시도할 수 있어요")
        await expect(page.locator("[data-recovery-correlation]")).to_have_text(CORRELATION_ID)
        assert CORRELATION_ID not in FAILED_ANNOUNCEMENT

        # Role and order, on every message, so a transcript can be navigated blind.
        labels = await page.locator("#transcript article").evaluate_all(
            "(nodes) => nodes.map((node) => node.getAttribute('aria-label'))"
        )
        assert labels == ["1번째 메시지, 내 질문", "2번째 메시지, Cite 답변"]

        # A timeout is the same builder with a different state label, and it must not
        # borrow the cancel fallback for the fields it does have.
        timeout_state = {"run": "running", "latest": 2}
        second = await open_page(browser, url, timeout_state)
        await second.locator("#prompt").fill("시간이 초과되는 질문")
        await second.locator("#send-question").click()
        timeout_state.update(run="timeout", latest=6)
        await expect(second.locator("[data-recovery-state]")).to_have_text("답변 시간 초과", timeout=10_000)
        announced = await second.locator("#run-announcer").text_content()
        assert announced == FAILED_ANNOUNCEMENT.replace("답변 생성 실패", "답변 시간 초과")

        await browser.close()


async def _exercise_fail_closed_relabels_the_reattached_answer(url):
    """The path where the client tells the user it cannot confirm the Run state is
    exactly the path where a stale position label would mislead: a cancel detached
    both bubbles, so the answer bubble re-enters an emptied transcript."""
    state = {"run": "running", "latest": 2, "bad_retry": True}
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await open_page(browser, url, state)
        await page.locator("#prompt").fill("중지했다가 재시도할 질문")
        await page.locator("#send-question").click()
        await page.locator("[data-ux='UX-STOP-ACTION']").click()
        await expect(page.locator("[data-ux='UX-RUN-RECOVERY']")).to_be_visible(timeout=10_000)
        await expect(page.locator("#transcript article")).to_have_count(0)

        await page.locator("[data-retry-run]").click()
        answer = page.locator("#transcript article")
        await expect(answer).to_have_count(1, timeout=10_000)
        await expect(answer).to_contain_text("실행 상태를 안전하게 확인할 수 없습니다")
        assert await answer.get_attribute("aria-label") == "1번째 메시지, Cite 답변"

        await browser.close()


async def _exercise_disabled_composer_says_why(url):
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        # The reason is a polite Live Region as well as a description, because a
        # disabled control is not focusable and its description is never spoken. It
        # is always rendered -- a region created at announcement time never speaks.
        unready = await open_page(browser, url, {"run": "queued", "latest": 0}, policy_ready=False)
        reason = unready.locator("#composer-reason")
        assert await reason.get_attribute("aria-live") == "polite"
        assert await reason.evaluate("(node) => getComputedStyle(node).display") != "none"
        await expect(unready.locator("#prompt")).to_be_disabled()
        await expect(reason).to_contain_text("서비스 준비 상태와 정책")

        # 새 대화를 만드는 중: the first question is creating its conversation.
        switching = await open_page(browser, url, {"run": "queued", "latest": 0, "create_delay": 1.0})
        await switching.locator("#prompt").fill("대화를 만드는 동안의 질문")
        await switching.locator("#send-question").click()
        await expect(switching.locator("#composer-reason")).to_contain_text("새 대화를 만드는 중")

        state = {"run": "running", "latest": 2}
        page = await open_page(browser, url, state)
        reason = page.locator("#composer-reason")
        assert "composer-reason" in (
            await page.locator("#prompt").get_attribute("aria-describedby")).split()
        assert "composer-reason" in (
            await page.locator("#send-question").get_attribute("aria-describedby")).split()

        # No conversation yet is no longer a reason to close the composer (Task 24):
        # it is open, and there is nothing to explain.
        await expect(page.locator("#prompt")).to_be_enabled()
        await expect(reason).to_have_text("")

        await page.locator("#prompt").fill("생성 중에는 다시 보낼 수 없는 질문")
        await page.locator("#send-question").click()
        await expect(page.locator("#prompt")).to_be_disabled()
        await expect(reason).to_contain_text("답변 생성이 끝나면")

        # A conversation the server closed under us is permanently unusable, and the
        # reason must say that rather than telling the user to wait for an answer.
        closed_state = {"run": "queued", "latest": 0, "submit_status": 400}
        closed = await open_page(browser, url, closed_state)
        await closed.locator("#prompt").fill("거부되는 질문")
        await closed.locator("#send-question").click()
        await expect(closed.locator("#composer-reason")).to_contain_text(
            "이 대화는 더 이상 사용할 수 없습니다", timeout=10_000)

        # A terminal Run whose committed log never arrived: the answer is on screen
        # and nothing is generating, so "wait for generation" would be a lie.
        settled = [
            event(1, "run.status", state="running", stage="streaming"),
            event(2, "message.delta", message_id=MESSAGE_ID, text="완료된 답변"),
            event(3, "message.sources", message_id=MESSAGE_ID, outcome="meta", sources=[],
                  search_truncated=False, uncovered=None),
            event(4, "message.completed", message_id=MESSAGE_ID, text="완료된 답변"),
            event(5, "run.status", state="completed", stage="terminal"),
        ]
        stalled = await open_page(browser, url, {"run": "running", "latest": 2}, events=settled)
        await stalled.locator("#prompt").fill("완료됐지만 확정 전인 질문")
        await stalled.locator("#send-question").click()
        await expect(stalled.locator("[data-run-state]")).to_have_text("답변 완료", timeout=10_000)
        await expect(stalled.locator("[data-ux='UX-ASSISTANT-RESPONSE']")).to_contain_text("완료된 답변")
        await expect(stalled.locator("#composer-reason")).to_contain_text("실행 결과를 확인하는 중")

        await browser.close()


async def _exercise_no_dialog_support_degrades_to_a_static_navigation(url):
    """One unsupported element must not take the client with it: without <dialog>
    the navigation is the block the markup shipped, the controls that would drive a
    Sheet are not offered, and every listener registered after the sheet wiring is
    still bound."""
    without_dialog = (
        "delete HTMLDialogElement.prototype.showModal;"
        "delete HTMLDialogElement.prototype.show;"
    )
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        errors = []
        page = await open_page(browser, url, {"run": "queued", "latest": 0},
                               scripts=[without_dialog], viewport={"width": 320, "height": 640})
        page.on("pageerror", lambda error: errors.append(str(error)))

        await expect(page.locator("[data-ux='UX-NAV-SHEET']")).to_be_visible()
        await expect(page.locator("[data-sheet-trigger]")).to_be_hidden()
        await expect(page.locator("[data-sheet-close]")).to_be_hidden()
        # The composer's own listeners are registered after the sheet wiring, so if
        # that had thrown at module scope none of this would work.
        await expect(page.locator("#prompt")).to_be_enabled()
        await page.locator("#prompt").fill("dialog 없이 보내는 질문")
        await expect(page.locator("#send-question")).to_be_enabled()
        assert errors == [], errors

        # And with no JavaScript at all: the markup ships the dialog open, so the
        # navigation is a block in the flow and the two dead controls never render.
        static = await browser.new_context(java_script_enabled=False,
                                           viewport={"width": 320, "height": 640})
        static_page = await static.new_page()
        await static_page.goto(url)
        await expect(static_page.locator("[data-ux='UX-NAV-SHEET']")).to_be_visible()
        await expect(static_page.locator("[data-current-conversation]")).to_be_visible()
        await expect(static_page.locator("[data-sheet-trigger]")).to_be_hidden()
        await expect(static_page.locator("[data-sheet-close]")).to_be_hidden()

        await browser.close()


async def _exercise_reduced_motion_replaces_movement_without_losing_it(url):
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        results = {}
        for setting in ("no-preference", "reduce"):
            state = {"run": "running", "latest": 2}
            page = await open_page(browser, url, state, reduced_motion=setting)
            await page.locator("#prompt").fill("생성 중 caret을 보는 질문")
            await page.locator("#send-question").click()
            note = page.locator("[data-ux='UX-ASSISTANT-RESPONSE'] small")
            await expect(note).to_be_visible(timeout=10_000)
            results[setting] = await note.evaluate(
                "(node) => {"
                "  const caret = getComputedStyle(node, '::after');"
                "  const action = document.querySelector('[data-ux=\"UX-PRIMARY-ACTION\"]');"
                "  return {"
                "    animation: caret.animationName,"
                "    opacity: caret.opacity,"
                "    width: caret.width,"
                "    content: caret.content,"
                "    label: node.textContent,"
                "    transition: getComputedStyle(action).transitionDuration,"
                "  };"
                "}"
            )

        # Motion for everyone else -- an animation and a transition...
        assert results["no-preference"]["animation"] == "ux-caret"
        assert float(results["no-preference"]["transition"].rstrip("s")) > 0.1
        # ...both replaced, not removed: the caret is still drawn and still opaque,
        # the control still changes state, and the fact the caret stood for is a
        # sentence rather than an animation.
        assert results["reduce"]["animation"] == "none"
        assert results["reduce"]["opacity"] == "1"
        assert float(results["reduce"]["transition"].rstrip("s")) < 0.01
        assert results["reduce"]["width"] == results["no-preference"]["width"] == "2px"
        # Drawn, never written: generated text content cannot be hidden from a
        # screen reader, so the caret contributes no characters to the label.
        assert results["reduce"]["content"] in {'""', "none"}
        assert results["reduce"]["label"] == results["no-preference"]["label"] == "생성 중인 답변입니다."

        await browser.close()


async def _exercise_first_screen_is_one_view_with_the_policy_statement(url):
    """Task 24 fix round: the empty screen after any reload is one screen, says which
    one it is in the navigation, and keeps its status a live region even while it is
    empty. Task 26 (owner decision): the input-safety fine print under the composer
    was a remnant of old epics.md and is gone; the policy statement now lives only on
    the 정보·정책 screen."""
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await open_page(browser, url, {"run": "queued", "latest": 0},
                               viewport={"width": 1440, "height": 900})
        # The skip link's fragment is not a screen: reloading there must still show
        # exactly one of the two, not both stacked.
        await page.goto(url + "#main-content")
        await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
        await expect(page.locator("#conversation-panel")).to_be_visible()
        await expect(page.locator("#policy-panel")).to_be_hidden()
        current = page.locator("[data-ux='UX-NAV-SHEET'] [aria-current='page']")
        await expect(current).to_have_count(1)
        await expect(current).to_have_text("새 대화")

        # Task 26: the composer carries no input-safety fine print any more.
        await expect(page.locator("[data-input-warning]")).to_have_count(0)
        await expect(page.locator("[data-ux='UX-USER-MESSAGE']")).to_have_count(0)

        # An empty live region stays in the accessibility tree.
        status = page.locator("#conversation-status")
        assert await status.text_content() == ""
        assert await status.evaluate("(node) => getComputedStyle(node).display") != "none"

        # 개인정보·회사 기밀·Credential 입력 금지 안내는 정보·정책 화면에만 있다.
        await page.locator("[data-policy-nav]").click()
        statement = page.locator("[data-policy-success] p").first
        await expect(statement).to_be_visible()
        for word in ("개인정보", "회사 기밀", "Credential"):
            await expect(statement).to_contain_text(word)

        # The current item keeps its white "active" surface under the pointer.
        policy_link = page.locator("[data-policy-nav]")
        await expect(policy_link).to_have_attribute("aria-current", "page")
        resting = await policy_link.evaluate("(node) => getComputedStyle(node).backgroundColor")
        await policy_link.hover()
        hovered = await policy_link.evaluate("(node) => getComputedStyle(node).backgroundColor")
        soft = await page.evaluate(
            "(() => { const probe = document.createElement('i');"
            "  probe.style.background = 'var(--bg)'; document.body.append(probe);"
            "  const value = getComputedStyle(probe).backgroundColor; probe.remove(); return value; })()")
        assert resting == hovered == soft, (resting, hovered, soft)

        await browser.close()


async def _exercise_first_submit_creates_the_conversation_once_and_fails_honestly(url):
    """The first question creates its conversation. A failed creation keeps the
    question and does not claim a conversation was kept; a slow one is created once,
    however often the user presses Enter."""
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        failing = {"run": "running", "latest": 2, "create_status": 503}
        posts = []
        page = await open_page(browser, url, failing, posts)
        await page.locator("#prompt").fill("대화를 만들지 못하는 첫 질문")
        await page.locator("#send-question").click()
        status = page.locator("#conversation-status")
        await expect(status).to_contain_text("대화를 시작하지 못해 질문을 보내지 못했습니다")
        assert "기존 대화를 유지" not in await status.text_content()
        await expect(page.locator("#prompt")).to_have_value("대화를 만들지 못하는 첫 질문")
        await expect(page.locator("#prompt")).to_be_enabled()
        await expect(page.locator("#send-question")).to_be_enabled()
        await expect(page.locator("[data-ux='UX-USER-MESSAGE']")).to_have_count(0)
        assert posts == []
        # ...and the same composer sends once creation works again.
        failing.pop("create_status")
        await page.locator("#send-question").click()
        await expect(page.locator("[data-ux='UX-USER-MESSAGE']")).to_have_count(1)
        assert [post["kind"] for post in posts] == ["question"]
        assert failing["creates"] == 2

        slow = {"run": "running", "latest": 2, "create_delay": 1.0}
        slow_posts = []
        second = await open_page(browser, url, slow, slow_posts)
        await second.locator("#prompt").fill("천천히 만들어지는 대화의 첫 질문")
        await second.locator("#prompt").press("Enter")
        await second.locator("#prompt").press("Enter")
        await second.locator("#send-question").evaluate("(node) => node.click()")
        await second.locator("#prompt").press("Enter")
        await expect(second.locator("[data-ux='UX-USER-MESSAGE']")).to_have_count(1, timeout=10_000)
        await second.wait_for_timeout(500)
        assert slow["creates"] == 1, slow
        assert [post["kind"] for post in slow_posts] == ["question"]
        await expect(second.locator("[data-ux='UX-USER-MESSAGE']")).to_have_count(1)

        await browser.close()


def test_story_1_10_first_screen_is_one_view_with_the_policy_statement(server_url):
    asyncio.run(_exercise_first_screen_is_one_view_with_the_policy_statement(server_url))


def test_story_1_10_first_submit_creates_the_conversation_once_and_fails_honestly(server_url):
    asyncio.run(_exercise_first_submit_creates_the_conversation_once_and_fails_honestly(server_url))


def test_story_1_10_three_breakpoints_never_scroll_sideways(server_url):
    asyncio.run(_exercise_three_breakpoints_never_scroll_sideways(server_url))


def test_story_1_10_sheet_is_inert_trapped_and_returns_focus(server_url):
    asyncio.run(_exercise_sheet_is_inert_trapped_and_returns_focus(server_url))


def test_story_1_10_keyboard_only_reaches_every_core_action(server_url):
    asyncio.run(_exercise_keyboard_only_reaches_every_core_action(server_url))


def test_story_1_10_touch_reaches_the_same_core_actions(server_url):
    asyncio.run(_exercise_touch_reaches_the_same_core_actions(server_url))


def test_story_1_10_cancel_handoff_waits_for_the_sheet(server_url):
    asyncio.run(_exercise_cancel_handoff_waits_for_the_sheet(server_url))


def test_story_1_10_state_reaches_the_screen_reader_as_text(server_url):
    asyncio.run(_exercise_state_reaches_the_screen_reader_as_text(server_url))


def test_story_1_10_fail_closed_relabels_the_reattached_answer(server_url):
    asyncio.run(_exercise_fail_closed_relabels_the_reattached_answer(server_url))


def test_story_1_10_disabled_composer_says_why(server_url):
    asyncio.run(_exercise_disabled_composer_says_why(server_url))


def test_story_1_10_no_dialog_support_degrades_to_a_static_navigation(server_url):
    asyncio.run(_exercise_no_dialog_support_degrades_to_a_static_navigation(server_url))


def test_story_1_10_reduced_motion_replaces_movement_without_losing_it(server_url):
    asyncio.run(_exercise_reduced_motion_replaces_movement_without_losing_it(server_url))
