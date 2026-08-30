"""Tabs and popups: pages this driver did not open.

Everything else in ``tests/driver`` is about a page wirespec created. A popup is
the opposite -- the *application* opens it, and the first wirespec hears of it
is a target appearing. What is asserted here is that it arrives, that it is a
real page with the context's routes and viewport on it, and that closing it is
noticed (§15.3 stage 10).
"""

import pytest

from wirespec.browser import BrowserContext
from wirespec.expect import expect
from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_popup_is_caught_and_is_a_real_page(page: Page) -> None:
    """``window.open`` from the page's own script. The block is what makes it
    raceless: the popup announces itself before anything could subscribe
    afterwards."""
    await page.goto("/popup.html")
    async with page.expect_popup() as popup:
        await page.locator("#open").click()

    opened = popup.result()
    assert isinstance(opened, Page)
    assert opened is not page
    await expect(opened.locator("h1")).to_be_visible()
    assert opened.url.endswith("/index.html")


async def test_a_target_blank_link_is_a_popup_too(page: Page) -> None:
    """No script involved -- the browser opens it. Same event either way."""
    await page.goto("/popup.html")
    async with page.expect_popup() as popup:
        await page.locator("#link").click()
    assert popup.result().url.endswith("/list.html")


async def test_the_popup_joins_the_context(page: Page, context: BrowserContext) -> None:
    """A spec counting tabs has to see it, and closing the context has to close
    it -- which needs it to be in the list."""
    await page.goto("/popup.html")
    before = len(context.pages)
    async with page.expect_popup() as popup:
        await page.locator("#open").click()
    assert len(context.pages) == before + 1
    assert popup.result() in context.pages


async def test_the_popup_inherits_the_contexts_routes(page: Page, context: BrowserContext) -> None:
    """§6.1: a route is installed on the context so a spec stubs an
    endpoint once rather than once per tab. A popup that skipped them would
    reach the real server, and the spec would pass or fail for the wrong
    reason."""
    await context.route("**/api", lambda route: route.fulfill(body='{"from": "the route"}'))
    await page.goto("/popup.html")
    async with page.expect_popup() as popup:
        await page.locator("#open-network").click()

    opened = popup.result()
    assert await opened.evaluate("() => fetch('/api').then(r => r.json())") == {"from": "the route"}


async def test_the_popup_has_the_contexts_viewport(page: Page, context: BrowserContext) -> None:
    """Every box the driver measures is relative to it, so a popup Chrome sized
    however it felt like would measure differently from every other page
    (§8.9)."""
    await page.goto("/popup.html")
    async with page.expect_popup() as popup:
        await page.locator("#open").click()
    width, height = context.viewport
    assert await popup.result().evaluate("() => [window.innerWidth, window.innerHeight]") == [width, height]


async def test_closing_a_popup_takes_it_out_of_the_context(page: Page, context: BrowserContext) -> None:
    await page.goto("/popup.html")
    async with page.expect_popup() as popup:
        await page.locator("#open").click()
    opened = popup.result()
    await opened.close()
    assert opened.closed
    assert opened not in context.pages


async def test_a_popup_that_never_comes_times_out_saying_so(page: Page) -> None:
    await page.goto("/popup.html")
    with pytest.raises(TimeoutError):
        async with page.expect_popup(timeout=0.4):
            await page.locator("h1").click()
