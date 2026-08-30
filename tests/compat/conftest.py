"""Fixtures for the compatibility suite.

Deliberately its own directory. These tests are written the way a **Playwright**
suite is written -- milliseconds, dict viewports, ``async_playwright()`` -- so
mixing them into ``tests/driver`` would blur the one thing they exist to check:
that a suite nobody rewrote still runs (§15.3 stage 6).
"""

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from tests.driver.server import serve
from wirespec.compat.async_api import Browser, async_playwright


@pytest.fixture(scope="session")
def site() -> Iterator[str]:
    yield from serve()


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def browser(chrome_binary: str) -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        launched = await playwright.chromium.launch(executable_path=chrome_binary)
        try:
            yield launched
        finally:
            await launched.close()
