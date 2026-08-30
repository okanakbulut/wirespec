"""Fixtures for the differential suite — the same ones under both drivers.

§15.4: compatibility is measured by running one spec file under
Playwright and under wirespec against the same fixture server and the same
Chrome, and comparing the results. So everything here is written in
**Playwright's** API and nothing imports wirespec: under the reference
interpreter there is no wirespec to import, and under wirespec's the shim is
what makes ``playwright`` resolve.

The fixture server is shared rather than duplicated. ``tests/driver/server.py``
is pure standard library, so the reference interpreter can import it without
having wirespec's one dependency installed.
"""

import os
import shutil
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from tests.driver.server import serve

#: Where the comparison driver puts the Chrome both runs must share. Set by
#: ``differential/compare.py``; the fallback is only so the file can be run by
#: hand under either interpreter.
CHROME = "WIRESPEC_DIFFERENTIAL_CHROME"

#: Playwright's default, restated so the two runs cannot differ by it. wirespec
#: has its own default and it is not the same number.
VIEWPORT = {"width": 1280, "height": 720}


def _chrome() -> str:
    found = os.environ.get(CHROME) or next(
        (path for name in ("google-chrome", "chromium", "chrome") if (path := shutil.which(name))),
        None,
    )
    if not found:
        pytest.skip(f"no Chrome: set {CHROME}")
    return found


@pytest.fixture(scope="session")
def site() -> Iterator[str]:
    yield from serve()


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def browser() -> AsyncIterator[object]:
    """One browser for the run, driving the Chrome already on this machine.

    ``executable_path`` is not a detail: it is what keeps the comparison honest
    (the same binary under both) and what keeps the reference interpreter from
    needing ``playwright install`` and the 150 MB that comes with it.
    """
    async with async_playwright() as playwright:
        launched = await playwright.chromium.launch(executable_path=_chrome())
        try:
            yield launched
        finally:
            await launched.close()


@pytest_asyncio.fixture(loop_scope="session")
async def context(browser, site: str) -> AsyncIterator[object]:
    made = await browser.new_context(base_url=site, viewport=VIEWPORT)
    try:
        yield made
    finally:
        await made.close()


@pytest_asyncio.fixture(loop_scope="session")
async def page(context):
    return await context.new_page()
