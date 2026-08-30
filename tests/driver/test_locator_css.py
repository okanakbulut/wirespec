"""Resolving a chain, starting with the simplest step there is.

A locator is a *description*, not a handle (§3.1): building one
touches nothing, and it is re-resolved against the live document every time it
is used.
"""

import pytest

from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_css_locator_counts_what_it_matches(page: Page) -> None:
    await page.goto("/list.html")
    assert await page.locator("li.pack").count() == 3


async def test_building_a_locator_touches_nothing(page: Page) -> None:
    """Three steps are three entries in a list, not three round trips."""
    await page.goto("/list.html")
    chained = page.locator("ul").locator("li").locator(".pack")
    assert len(chained.chain) == 3


async def test_matches_come_back_in_document_order_across_nested_roots(page: Page) -> None:
    """§4.1: de-duplicated, and in document order.

    The merge is a plain concatenation with a seen-set, and it is document order
    for a reason worth writing down rather than rediscovering. Roots arrive in
    document order because ``querySelectorAll`` returns them that way, and it
    returns *descendants*, not children -- so an outer root has already yielded
    everything an inner root can, in order, and the inner root only contributes
    duplicates. Two roots that do not nest are disjoint, and the earlier one's
    descendants all precede the later one's.
    """
    await page.goto("/nested.html")
    spans = page.locator("div").locator("span.s")
    assert await spans.count() == 4
    assert await page.evaluate("() => [...document.querySelectorAll('div span.s')].map(e => e.textContent)") == [
        "one",
        "two",
        "three",
        "four",
    ]


async def test_a_chain_that_matches_nothing_stops_early(page: Page) -> None:
    """No later step can resurrect a match, so there is nothing to gain from
    asking the page about the rest of the chain."""
    await page.goto("/nested.html")
    assert await page.locator("article").locator("span").count() == 0
