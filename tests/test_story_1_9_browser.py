"""Story 1.9 browser contract: an adversarial answer is text, and only text.

This is the half of the Sanitizer contract that cannot be proved anywhere else. In
Python you can show that `<script>alert(1)</script>` survives validation unchanged;
only a real browser can show that surviving unchanged means being *displayed* rather
than *executed*, that no `<a href="javascript:">` was ever constructed for a user to
click, and that the Streaming path and the completed path put the identical string
on screen.

Every hostile construct here reports itself if it runs: a global counter the page
increments, a patched `alert`, and a dialog handler that fails the test outright. An
assertion that only checks "the text is visible" would pass even while the payload
fired.
"""

import asyncio
import json

from playwright.async_api import async_playwright, expect


CONVERSATION_ID = "00000000-0000-4000-8000-000000000031"
RUN_ID = "00000000-0000-4000-8000-000000000032"
INPUT_ID = "00000000-0000-4000-8000-000000000033"
MESSAGE_ID = "00000000-0000-4000-8000-000000000034"
NOW = "2026-08-30T00:00:00Z"
QUESTION = "적대적 출력을 확인하는 질문"

# One answer carrying every construct the frozen matrix names: script, an event
# handler, an iframe, and both unsafe URL schemes.
HOSTILE = (
    "<script>window.__xss.push('script')</script>"
    '<img src=x onerror="window.__xss.push(\'onerror\')">'
    "<iframe src=\"javascript:window.__xss.push('iframe')\"></iframe>"
    "javascript:alert(1) data:text/html,<b>bold</b> "
    "<a href=\"javascript:window.__xss.push('href')\">클릭</a>"
)
# Split at boundaries that cut tags in half: a client that reassembled Deltas into
# markup would have to see the halves as markup at some point.
DELTAS = [
    "<script>window.__xss.push('script')</script>",
    '<img src=x onerror="window.__xss.push(\'onerror\')">',
    "<iframe src=\"javascript:window.__xss.push('iframe')\"></ifr",
    "ame>javascript:alert(1) data:text/html,<b>bold</b> ",
    "<a href=\"javascript:window.__xss.push('href')\">클릭</a>",
]
assert "".join(DELTAS) == HOSTILE


def projection(state, latest, completed=False):
    terminal = state not in {"queued", "running"}
    return {
        "schema_version": "1", "run_id": RUN_ID, "conversation_id": CONVERSATION_ID,
        "input_message_id": INPUT_ID, "retry_of_run_id": None,
        "output_message_id": MESSAGE_ID if completed else None,
        "output_message": (
            {
                "message_id": MESSAGE_ID, "content": HOSTILE, "outcome": "meta",
                "sources": [], "search_truncated": False, "uncovered": None,
            }
            if completed
            else None
        ),
        "state": state,
        "stage": "terminal" if terminal else ("streaming" if state == "running" else "queued"),
        "created_at": NOW, "last_updated_at": NOW, "latest_sequence": latest,
        "terminal_error": None,
    }


def event(sequence, kind, **values):
    return {
        "schema_version": "1", "run_id": RUN_ID, "sequence": sequence,
        "occurred_at": NOW, "type": kind, **values,
    }


STREAM_LOG = (
    [event(1, "run.status", state="running", stage="streaming")]
    + [
        event(index + 2, "message.delta", message_id=MESSAGE_ID, text=delta)
        for index, delta in enumerate(DELTAS)
    ]
    + [
        event(len(DELTAS) + 2, "message.sources", message_id=MESSAGE_ID, outcome="meta",
              sources=[], search_truncated=False, uncovered=None),
        event(len(DELTAS) + 3, "message.completed", message_id=MESSAGE_ID, text=HOSTILE),
        event(len(DELTAS) + 4, "run.status", state="completed", stage="terminal"),
        event(
            len(DELTAS) + 5, "stream.end",
            final_state="completed", final_sequence=len(DELTAS) + 5,
        ),
    ]
)


async def install_probes(page, log, available=True):
    """The XSS tripwires, plus a fake EventSource when the Streaming path is under
    test. `window.__xss` is what every payload in HOSTILE writes to, so an empty
    array at the end is a positive statement, not an absence of evidence."""
    payload = json.dumps({"log": log, "available": available}, ensure_ascii=False)
    await page.add_init_script(
        "const {log, available} = " + payload + ";" + """
        (() => {
          window.__xss = [];
          window.__streamed = 0;
          const alerted = window.alert;
          window.alert = (...args) => { window.__xss.push('alert'); };
          void alerted;
          if (!available) { window.EventSource = undefined; return; }
          class FakeEventSource {
            constructor() {
              this.listeners = {};
              this.closed = false;
              setTimeout(() => {
                log.forEach((value, index) => setTimeout(() => {
                  if (this.closed) return;
                  window.__streamed += 1;
                  for (const handler of this.listeners[value.type] || [])
                    handler({lastEventId: String(value.sequence), data: JSON.stringify(value)});
                }, index * 10));
              }, 20);
            }
            addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
            close() { this.closed = true; }
          }
          window.EventSource = FakeEventSource;
        })()
        """
    )


async def route_api(page, poll, runs):
    async def created(route):
        await route.fulfill(json={
            "conversation_id": CONVERSATION_ID,
            "created_at": NOW,
            "expires_at": "2026-08-30T01:00:00Z",
        })

    await page.route("**/api/v1/conversations", created)
    await page.route("**/api/v1/conversations/*/runs", runs)
    await page.route("**/api/v1/runs/*", poll)


async def start_question(page, url):
    await page.goto(url)
    await expect(page.locator("#policy-status")).to_contain_text("확인했습니다")
    await page.locator("[data-new-conversation]").first.click()
    await page.locator("#prompt").fill(QUESTION)
    await page.locator("#send-question").click()


async def assert_nothing_executed(page):
    """DOM 실행 0건 and 실행 가능한 Link 0건, asserted structurally rather than by
    looking for the absence of a symptom: the answer's characters are in the
    document, but no element, attribute or URL was ever built out of them."""
    assert await page.evaluate("window.__xss") == []
    counts = await page.evaluate(
        """() => {
          const transcript = document.querySelector('#transcript');
          return {
            scripts: transcript.querySelectorAll('script, iframe, object, embed').length,
            images: transcript.querySelectorAll('img').length,
            links: transcript.querySelectorAll('a, area, [href], [src]').length,
            handlers: transcript.querySelectorAll('[onerror], [onload], [onclick]').length,
            elements: [...transcript.querySelectorAll('*')].map((node) => node.tagName).sort(),
          };
        }"""
    )
    assert counts["scripts"] == 0
    assert counts["images"] == 0
    assert counts["links"] == 0
    assert counts["handlers"] == 0
    # Only the elements the client builds itself: the article, its label, the
    # Markdown paragraph it renders the answer into (Task 25: div.md > p), the
    # thinking line (button, step list and state rows) and the action row's copy
    # button with its icon. Nothing the Model said became a node -- in particular
    # none of the tags HOSTILE spells out (script, img, iframe, a, b).
    client_built = {"ARTICLE", "STRONG", "P", "SMALL", "DIV", "SPAN", "BUTTON", "OL", "LI",
                    "DL", "DT", "DD", "svg", "path", "rect"}
    assert set(counts["elements"]) <= client_built, set(counts["elements"]) - client_built
    assert await page.locator("#transcript article.assistant .md > *").evaluate_all(
        "(nodes) => nodes.map((node) => node.tagName)") == ["P"]


async def rendered_answer(page):
    return await page.evaluate(
        "document.querySelector('#transcript article.assistant p').textContent"
    )


async def _exercise_hostile_output_is_text_on_both_paths(url):
    results = {}

    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        for path, log, available in (("streaming", STREAM_LOG, True), ("completed", [], False)):
            page = await browser.new_page()
            # A payload that got as far as opening a real dialog would hang the page
            # rather than fail quietly.
            page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.dismiss()))
            errors: list = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await install_probes(page, log, available=available)

            async def poll(route, available=available):
                # Streaming: the client only polls as a fallback, then once more to
                # reconcile after a clean stream.end -- latest_sequence must still
                # match STREAM_LOG's own tail there. Completed: this is the whole
                # path -- the answer arrives inside the projection.
                await route.fulfill(json=projection("completed", len(STREAM_LOG), completed=True))

            async def runs(route):
                await route.fulfill(status=202, json=projection("queued", 0))

            await route_api(page, poll, runs)
            await start_question(page, url)

            await expect(page.locator("#transcript article.assistant")).to_have_count(
                1, timeout=10_000
            )
            await expect(page.locator("#transcript article.assistant p")).to_have_text(
                HOSTILE, timeout=10_000
            )
            await page.wait_for_timeout(300)
            # Non-vacuous: the streaming case really was driven by Deltas, not by the
            # polling fallback quietly rendering the same completed projection.
            assert await page.evaluate("window.__streamed") == (
                len(STREAM_LOG) if available else 0
            )
            await assert_nothing_executed(page)
            results[path] = await rendered_answer(page)
            assert errors == [], errors
            await page.close()

    # Streaming과 completed 일치: the same adversarial answer, the same string on
    # screen, whichever path delivered it.
    assert results["streaming"] == results["completed"] == HOSTILE


async def _exercise_the_page_refuses_injected_script(url):
    """The CSP is the second line of defence and has to hold on its own: even a
    deliberately injected inline script -- something the client itself never does --
    must not run."""
    async with (
        async_playwright() as playwright,
        await playwright.chromium.launch() as browser,
    ):
        page = await browser.new_page()
        await install_probes(page, [], available=False)
        await page.goto(url)
        await page.evaluate(
            """() => {
              const node = document.createElement('script');
              node.textContent = "window.__xss.push('csp')";
              document.body.append(node);
            }"""
        )
        await page.wait_for_timeout(200)
        assert await page.evaluate("window.__xss") == []


def test_story_1_9_browser_hostile_output_is_text_on_both_paths(server_url) -> None:
    asyncio.run(_exercise_hostile_output_is_text_on_both_paths(server_url))


def test_story_1_9_browser_csp_blocks_injected_script(server_url) -> None:
    asyncio.run(_exercise_the_page_refuses_injected_script(server_url))
