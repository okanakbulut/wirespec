"""``expect`` — the retrying assertions, starting with the most-used one.

``to_have_count`` is 207 call sites in the validating suite, more than any other
assertion. Everything about the loop it runs on is §5.1: read
first, wait second, and quote the last reading when it gives up.
"""

import pytest

from wirespec.errors import WirespecTimeoutError
from wirespec.expect import expect
from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_count_that_is_already_right_passes(page: Page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#rows li")).to_have_count(3)


async def test_a_count_is_waited_for(page: Page) -> None:
    """The rows are removed 100 ms after the assertion starts. No spec had to
    say so."""
    await page.goto("/assertions.html")
    await page.evaluate("() => window.__shrink(100)")
    await expect(page.locator("#rows li")).to_have_count(1)


async def test_a_count_that_never_arrives_quotes_the_last_reading(page: Page) -> None:
    """§5.1. "expected 9, last saw 3" has usually already answered
    the question that "timed out" sends someone to the browser for."""
    await page.goto("/assertions.html")
    with pytest.raises(WirespecTimeoutError) as raised:
        await expect(page.locator("#rows li"), timeout=0.4).to_have_count(9)
    message = str(raised.value)
    assert "9" in message
    assert "3" in message
    assert "#rows li" in message


async def test_the_negation_waits_for_the_count_to_stop_matching(page: Page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#rows li")).not_to_have_count(1)
    await page.evaluate("() => window.__shrink(100)")
    await expect(page.locator("#rows li")).not_to_have_count(3)
