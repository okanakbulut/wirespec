"""Fixtures for the driver suite.

Deliberately not the live suite's fixtures. ``tests/live`` drives a raw
``Connection`` and instruments it to prove the protocol subset is complete;
these tests go through the public API instead, which is the only way to find
out whether the API is any good.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from tests.driver.server import serve
from wirespec.browser import Browser, BrowserContext
from wirespec.page import Page


@pytest.fixture(scope="session")
def site() -> Iterator[str]:
    yield from serve()


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def browser(chrome_binary: str) -> AsyncIterator[Browser]:
    """One Chrome for the whole driver suite.

    Per-test isolation comes from contexts, not from processes: a launch is
    190 ms and a context is about one round trip.
    """
    async with Browser.launch(binary=chrome_binary) as live:
        yield live


@pytest_asyncio.fixture(loop_scope="session")
async def context(browser: Browser, site: str) -> AsyncIterator[BrowserContext]:
    """A context of its own per test, thrown away afterwards.

    ``base_url`` is the fixture site, so a test writes ``page.goto("/x.html")``
    the way a spec does.
    """
    async with browser.new_context(base_url=site) as fresh:
        yield fresh


@pytest_asyncio.fixture(loop_scope="session")
async def page(context: BrowserContext) -> Page:
    return await context.new_page()


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def chrome_locale(browser: Browser) -> str:
    """Which locale this Chrome resolved, which must be the one wirespec pinned.

    Picker inputs display their segments in the locale's order, and the order
    is what a typed fill has to match. Measured: it follows LANG/LC_ALL in the
    environment Chrome was spawned from, and Chrome's own ``--lang`` flag does
    not change it -- which is why ``Browser.launch`` sets that environment
    itself (§8.4).

    So this asserts rather than letting the tests below skip. Once the launch
    pins the locale, a Chrome that resolved another one means the pin did not
    take, and the digit orders those tests type would be wrong on a machine
    nobody was looking at. That is a failure, not a reason to run less.
    """
    async with browser.new_context() as context:
        page = await context.new_page()
        resolved = await page.evaluate("() => new Intl.DateTimeFormat().resolvedOptions().locale")
    assert resolved == browser.locale_tag, (
        f"the launch pinned {browser.locale_tag!r} and Chrome resolved {resolved!r}: "
        f"every picker order below is the pinned locale's"
    )
    return resolved
