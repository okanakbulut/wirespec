"""Browser contexts: real ones, with their own cookies, storage and cache.

Not tabs that happen to share nothing (§6.1) -- the isolation is
what lets a suite run in parallel without every spec seeing every other spec's
login.
"""

from urllib.parse import urlsplit

import pytest

from wirespec.browser import Browser
from wirespec.cdp import target
from wirespec.errors import CDPError, PageClosedError

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_context_is_disposed_when_its_block_ends(browser: Browser) -> None:
    """The context manager is the whole lifetime. A context that outlived its
    block would be a leaked profile inside a browser that looks idle."""
    async with browser.new_context() as context:
        context_id = context.id
        assert context_id
    with pytest.raises(CDPError):
        await browser.connection.send(target.CreateTarget(url="about:blank", browser_context_id=context_id))


async def test_a_page_can_be_closed_on_its_own(browser: Browser, site: str) -> None:
    """A context is per-spec isolation; a page is a tab in it. Closing one must
    not take the context with it."""
    async with browser.new_context(base_url=site) as context:
        first = await context.new_page()
        second = await context.new_page()
        await first.close()
        assert first.closed
        # The context and its other page are untouched.
        await second.goto("/index.html")
        assert await second.evaluate("document.title") == "wirespec driver fixture"


async def test_a_closed_page_refuses_to_be_used(browser: Browser, site: str) -> None:
    """Rather than hanging on a session Chrome has forgotten, or raising a
    protocol error that names an internal id and nothing else."""
    async with browser.new_context(base_url=site) as context:
        page = await context.new_page()
        await page.close()
        with pytest.raises(PageClosedError, match="closed"):
            await page.goto("/index.html")


async def test_closing_a_context_closes_its_pages(browser: Browser, site: str) -> None:
    async with browser.new_context(base_url=site) as context:
        page = await context.new_page()
        await page.goto("/index.html")
    assert page.closed


async def test_cookies_can_be_added_before_a_page_exists(browser: Browser, site: str) -> None:
    """The normal pre-authentication shape: seed the context, then open a page
    into an application that already believes you are logged in. A context-level
    command, so it does not need a tab to talk through."""
    host = urlsplit(site).hostname or "127.0.0.1"
    async with browser.new_context(base_url=site) as context:
        await context.add_cookies([{"name": "seeded", "value": "before-any-page", "domain": host, "path": "/"}])
        page = await context.new_page()
        await page.goto("/index.html")
        assert await page.evaluate("document.cookie") == "seeded=before-any-page"


async def test_cookies_do_not_leak_between_contexts(browser: Browser, site: str) -> None:
    """The isolation claim in §6.1, on the thing specs actually
    care about: a login in one context is not a login in the next."""
    host = urlsplit(site).hostname or "127.0.0.1"
    async with browser.new_context(base_url=site) as one, browser.new_context(base_url=site) as two:
        await one.add_cookies([{"name": "who", "value": "context-one", "domain": host, "path": "/"}])
        first, second = await one.new_page(), await two.new_page()
        await first.goto("/index.html")
        await second.goto("/index.html")
        assert await first.evaluate("document.cookie") == "who=context-one"
        assert await second.evaluate("document.cookie") == ""
