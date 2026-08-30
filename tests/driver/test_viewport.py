"""The viewport, which is not optional.

A headless window's default size is not a promise, and every measured box is
relative to it (§8.9). So wirespec always says what it wants
rather than accepting what it is given.
"""

import pytest

from wirespec.browser import Browser
from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_page_gets_the_contexts_viewport(browser: Browser, site: str) -> None:
    async with browser.new_context(base_url=site, viewport=(900, 400)) as context:
        page = await context.new_page()
        await page.goto("/index.html")
        assert await page.evaluate("[innerWidth, innerHeight]") == [900, 400]


async def test_the_default_viewport_is_the_documented_one(page: Page) -> None:
    """§6.1 says 1280x720. A default nobody stated is a default
    that changes with the Chrome version."""
    await page.goto("/index.html")
    assert await page.evaluate("[innerWidth, innerHeight]") == [1280, 720]


async def test_set_viewport_size_changes_it_afterwards(page: Page) -> None:
    await page.goto("/index.html")
    await page.set_viewport_size((640, 480))
    assert await page.evaluate("[innerWidth, innerHeight]") == [640, 480]
