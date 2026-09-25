import asyncio
from contextlib import suppress
import os
from pathlib import Path
import socket
import subprocess
import sys
from urllib.request import urlopen

import pytest
from playwright.async_api import Error as PlaywrightError, async_playwright, expect


ROOT = Path(__file__).parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_server(url: str, process: subprocess.Popen[bytes]) -> None:
    for _ in range(100):
        if process.poll() is not None:
            raise AssertionError("uvicorn이 시작되기 전에 종료되었습니다")
        try:
            with urlopen(f"{url}/live", timeout=0.2) as response:
                if response.status == 204:
                    return
        except OSError:
            pass
    raise AssertionError("uvicorn startup timeout")


@pytest.fixture()
def server_url():
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "aidd_chat.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "error",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(url, process)
        yield url
    finally:
        process.terminate()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


async def _exercise_browser(url: str) -> None:
    response_body = (
        '{"conversation_id":"00000000-0000-4000-8000-000000000001",'
        '"created_at":"2026-01-01T00:00:00Z",'
        '"expires_at":"2026-01-01T01:00:00Z"}'
    )
    mode = "success"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 320, "height": 800})
        page = await context.new_page()

        async def conversation_route(route):
            if mode == "success":
                await route.fulfill(
                    status=201,
                    content_type="application/json",
                    body=response_body,
                )
            elif mode == "error":
                await route.fulfill(status=503, body="")
            else:
                await asyncio.sleep(10)
                with suppress(PlaywrightError):
                    await route.fulfill(
                        status=201,
                        content_type="application/json",
                        body=response_body,
                    )

        await page.route("**/api/v1/conversations", conversation_route)
        await page.goto(url, wait_until="domcontentloaded")

        await page.keyboard.press("Tab")
        assert await page.evaluate(
            "document.activeElement.matches('a.skip-link')"
        )
        await page.keyboard.press("Enter")
        assert await page.evaluate("document.activeElement.id") == "main-content"
        assert await page.evaluate(
            "Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) <= window.innerWidth"
        )

        controls = page.locator("[data-new-conversation]")
        status = page.locator("#conversation-status")
        # The order the .first pick below depends on, asserted rather than assumed:
        # navigation, then the recovery panel. (Task 24 retired the hero's 새 대화 시작
        # gate -- the first question creates the conversation -- so the navigation's
        # 새 대화 is the explicit control on an empty screen.)
        assert await controls.count() == 2
        assert await controls.first.evaluate(
            "(node) => node.closest('[data-ux=\"UX-NAV-SHEET\"]') !== null"
        )
        assert await controls.nth(1).evaluate("(node) => node.closest('[data-run-recovery]') !== null")

        mode = "success"
        # Story 1.10 folded the navigation into UX-NAV-SHEET below 1024px, so the
        # navigation's 새 대화 is reached through the Sheet at this width. The
        # control itself, and everything asserted about it, is unchanged.
        await page.locator("[data-sheet-trigger]").click()
        await controls.first.click()
        await expect(status).to_contain_text("빈 새 대화를 만들었습니다")
        assert await page.locator("[data-new-conversation]:disabled").count() == 0

        mode = "error"
        await page.locator("[data-sheet-trigger]").click()
        await controls.first.click()
        await expect(status).to_contain_text("새 대화를 만들지 못했습니다")
        assert await page.locator("[data-new-conversation]:disabled").count() == 0

        mode = "pending"
        await page.locator("[data-sheet-trigger]").click()
        await controls.first.click()
        await page.wait_for_timeout(100)
        # UX-NAV-SHEET and the cancelled-run recovery panel.
        assert await page.locator("[data-new-conversation]:disabled").count() == 2
        assert await status.text_content() == "새 대화를 만드는 중입니다."
        await page.wait_for_timeout(3_200)
        assert "시간이 초과되어 요청을 중단했습니다" in (await status.text_content())
        assert await page.locator("[data-new-conversation]:disabled").count() == 0

        await browser.close()


def test_story_1_1_browser_e2e_is_registered_and_runs(server_url):
    asyncio.run(_exercise_browser(server_url))
