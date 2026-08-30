"""``get_by_role`` -- the most-used query there is, and the one wirespec does
not compute itself.

The role and the accessible name come from Chrome (§4.2), so there
is nothing here that can drift from what a screen reader would say. What
wirespec contributes is the narrowing selector, which ``test_role_table.py``
holds to being a superset, and the matching, which happens in Python.
"""

import re

import pytest

from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_role_finds_native_and_claimed_elements_alike(page: Page) -> None:
    await page.goto("/roles.html")
    assert await page.get_by_role("button", name="A button").count() == 1
    assert await page.get_by_role("button", name="A div claiming button").count() == 1


async def test_a_role_without_a_name_matches_every_element_with_it(page: Page) -> None:
    await page.goto("/roles.html")
    assert await page.get_by_role("checkbox").count() == 2


async def test_the_confirm_drops_what_the_selector_over_approximated(page: Page) -> None:
    """The narrowing selector for ``textbox`` takes every ``<input>``, because
    a bare one with no type is a text field. Chrome then says a checkbox is not
    a textbox, and the confirm is what removes it."""
    await page.goto("/roles.html")
    assert await page.locator("input").count() > await page.get_by_role("textbox").count()
    assert await page.get_by_role("textbox", name="A checkbox").count() == 0


async def test_a_name_is_a_case_insensitive_substring_by_default(page: Page) -> None:
    await page.goto("/roles.html")
    assert await page.get_by_role("button", name="a BUTTON").count() >= 1
    assert await page.get_by_role("button", name="A button", exact=True).count() == 1
    assert await page.get_by_role("button", name="A butt", exact=True).count() == 0


async def test_a_name_may_be_a_python_pattern_with_python_flags(page: Page) -> None:
    """§4.2's clearest dividend. Nothing crosses a language
    boundary, so the pattern is simply used -- including flags JavaScript does
    not have, which used to be untranslatable and is now ordinary."""
    await page.goto("/roles.html")
    verbose = re.compile(
        r"""
        An\s        # the article, then a space
        input\s
        button
        """,
        re.VERBOSE,
    )
    assert await page.get_by_role("button", name=verbose).count() == 1


async def test_an_unsupported_role_says_so_and_lists_what_is_supported(page: Page) -> None:
    """Returning "no matches" for a role wirespec has no selector for would be
    an element that is simply never found (§5)."""
    await page.goto("/roles.html")
    with pytest.raises(NotImplementedError, match="treegrid"):
        await page.get_by_role("treegrid").count()


async def test_a_role_query_is_scoped_by_what_came_before_it(page: Page) -> None:
    await page.goto("/roles.html")
    assert await page.get_by_role("radio").count() == 4
    assert await page.locator("[role=radiogroup]").get_by_role("radio").count() == 1
