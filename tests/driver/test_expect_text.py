"""Text assertions, and the two-element rule that makes them useful.

§5.3: text assertions over many elements are satisfied by **any**
of them, and their negations by **none** of them. Pass a list instead and the
comparison becomes positional, which is how a spec asserts an order.
"""

import re

import pytest

from wirespec.errors import WirespecTimeoutError
from wirespec.expect import expect
from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_have_text_is_the_whole_text_after_normalisation(page: Page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#one")).to_have_text("the only one")
    await expect(page.locator("#one")).not_to_have_text("the only")


async def test_contain_text_is_a_substring(page: Page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#one")).to_contain_text("only")
    await expect(page.locator("#one")).not_to_contain_text("Acme")


async def test_text_assertions_are_case_sensitive_unless_told_otherwise(page: Page) -> None:
    """Unlike the *queries*, which are case-insensitive substrings by default
    (§4.2). Playwright draws the line in the same place, and
    diverging silently on the most-used assertion pair would be the worst kind
    of difference."""
    await page.goto("/assertions.html")
    await expect(page.locator("#one")).not_to_contain_text("ONLY")
    await expect(page.locator("#one")).to_contain_text("ONLY", ignore_case=True)


async def test_many_matches_are_satisfied_by_any_of_them(page: Page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#rows li")).to_contain_text("Beta")
    await expect(page.locator("#rows li")).to_have_text("Acme")


async def test_the_negation_over_many_matches_means_none_of_them(page: Page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#rows li")).not_to_contain_text("Zeta")
    with pytest.raises(WirespecTimeoutError):
        await expect(page.locator("#rows li"), timeout=0.4).not_to_contain_text("Beta")


async def test_a_list_makes_the_comparison_positional(page: Page) -> None:
    """Which is how a spec asserts an order. The count has to match too, or
    "the first three of many" would quietly pass."""
    await page.goto("/assertions.html")
    await expect(page.locator("#rows li")).to_have_text(["Acme", "Beta", "Gamma"])
    with pytest.raises(WirespecTimeoutError):
        await expect(page.locator("#rows li"), timeout=0.4).to_have_text(["Beta", "Acme", "Gamma"])
    with pytest.raises(WirespecTimeoutError):
        await expect(page.locator("#rows li"), timeout=0.4).to_have_text(["Acme", "Beta"])


async def test_a_pattern_is_used_with_the_authors_own_flags(page: Page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#one")).to_contain_text(re.compile(r"THE\s+only", re.IGNORECASE))


async def test_text_can_be_read_from_the_rendered_tree_instead(page: Page) -> None:
    """``textContent`` by default, like Playwright. ``use_inner_text=True``
    switches to the rendered text, where a <br> is a line break rather than
    nothing at all (§8.11)."""
    await page.goto("/readers.html")
    await expect(page.locator("#lines")).to_have_text("onetwo")
    await expect(page.locator("#lines")).to_have_text("one\ntwo", use_inner_text=True)


async def test_a_failing_text_assertion_quotes_what_it_saw(page: Page) -> None:
    await page.goto("/assertions.html")
    with pytest.raises(WirespecTimeoutError) as raised:
        await expect(page.locator("#one"), timeout=0.4).to_have_text("something else")
    message = str(raised.value)
    assert "something else" in message
    assert "the only one" in message


async def test_have_value_reads_the_control(page: Page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#value")).to_have_value("in the field")
    await expect(page.locator("#value")).not_to_have_value("something else")


async def test_have_attribute_and_have_css(page: Page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#link")).to_have_attribute("data-kind", "nav")
    await expect(page.locator("#link")).not_to_have_attribute("data-kind", "other")
    await expect(page.locator("#swatch")).to_have_css("color", "rgb(20, 40, 60)")


async def test_an_absent_attribute_is_not_a_crash(page: Page) -> None:
    """Asserting an attribute is absent is a reasonable thing for a spec to
    want, and it must not depend on the element having it first."""
    await page.goto("/assertions.html")
    await expect(page.locator("#link")).not_to_have_attribute("data-missing", "anything")


async def test_to_have_text_sees_the_space_between_two_inline_elements(page: Page) -> None:
    """The assertion the reader's bug reached: two words in two elements with a
    space between them are two words, and were being read as one."""
    await page.goto("/text.html")
    await expect(page.locator("#spaced")).to_have_text("a b c")
    await expect(page.locator("#spaced")).to_contain_text("a b")
