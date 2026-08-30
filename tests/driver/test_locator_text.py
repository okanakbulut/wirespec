"""``get_by_text`` -- the one query wirespec has to define itself.

It is not an accessibility question, so there is no browser computation to defer
to the way there is for roles (§4.2). §8.5 is the rule:
an element matches when its whole normalised ``textContent`` matches *and* no
child element's text matches too.
"""

import re

import pytest

from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_text_matches_across_a_child_element(page: Page) -> None:
    """§8.5's example. The obvious implementation reads an
    element's *direct* text children -- it is what Testing Library's
    getNodeText does -- and cannot see text that is partly inside a child."""
    await page.goto("/prose.html")
    assert await page.get_by_text("VO-2026-00001 · sandbox tenant").count() == 1


async def test_the_innermost_matching_element_wins(page: Page) -> None:
    """Both the <div> and the <p> contain the text. Without the innermost
    predicate every ancestor up to <html> matches, and a locator that should
    name one element names six."""
    await page.goto("/prose.html")
    assert await page.get_by_text("the innermost element wins").count() == 1


async def test_a_title_is_in_the_document_and_not_on_the_screen(page: Page) -> None:
    """§8.3's surviving half. There is no query root left to
    enforce this accidentally -- it is a predicate somebody has to have written
    on purpose -- so the fixture's <title>, <script> and <style> all contain
    the text being searched for."""
    await page.goto("/prose.html")
    assert await page.get_by_text("VO-2026-00001").count() == 1


async def test_the_default_is_a_case_insensitive_substring(page: Page) -> None:
    """§4.2's table, on text."""
    await page.goto("/prose.html")
    assert await page.get_by_text("acme").count() == 2
    assert await page.get_by_text("ACME HOLDINGS").count() == 1


async def test_exact_compares_after_whitespace_normalisation(page: Page) -> None:
    """Markup wraps and indents; the spec's author wrote one line."""
    await page.goto("/prose.html")
    assert await page.get_by_text("Acme", exact=True).count() == 1
    assert await page.get_by_text("Acme Holdings", exact=True).count() == 1


async def test_a_text_step_is_scoped_by_what_came_before_it(page: Page) -> None:
    """``performSearch`` has no root parameter and searches the whole document,
    so a text step that is not first has to be scoped afterwards."""
    await page.goto("/prose.html")
    assert await page.get_by_text("Acme").count() == 2
    assert await page.locator("ul").get_by_text("Acme").count() == 2
    assert await page.locator("#outer").get_by_text("Acme").count() == 0


async def test_a_regular_expression_narrows_to_every_text_bearing_element(page: Page) -> None:
    """The narrowing widens and the confirm does the work -- so a pattern finds
    what a string finds, including the elements a ``contains()`` could never
    have been built for."""
    await page.goto("/prose.html")
    assert await page.get_by_text(re.compile("Acme")).count() == 2
    assert await page.locator("#outer").get_by_text(re.compile("Acme")).count() == 0


async def test_case_insensitive_matching_is_not_ascii_only(page: Page) -> None:
    """The translate table is built from the needle, not from the ASCII
    alphabet, so an all-caps Greek heading is found by a lowercase query."""
    await page.goto("/prose.html")
    assert await page.get_by_text("αθηνα").count() == 1


async def test_a_phrase_holding_both_quote_kinds_still_works(page: Page) -> None:
    """XPath 1.0 has no escape character, so a string containing both ' and "
    can only be written with concat(). Page objects do locate things by real
    quoted phrases."""
    await page.goto("/prose.html")
    assert await page.get_by_text("""said 'hello' and "goodbye\"""").count() == 1


async def test_a_text_query_sees_the_space_between_two_inline_elements(page: Page) -> None:
    """The reader's whitespace bug reached the resolver too, through the same
    ``describeNode`` rebuild: ``<b>a</b> <b>b</b>`` confirmed as ``"ab"``, so a
    spec searching for the words it can see on the screen found nothing
    (§8.21)."""
    await page.goto("/text.html")
    assert await page.get_by_text("a b c").count() == 1
    assert await page.locator("#spaced").filter(has_text="a b").count() == 1
    assert await page.locator("#spaced").filter(has_not_text="a b").count() == 0


async def test_a_text_query_takes_a_pattern(page: Page) -> None:
    """The narrowing an XPath cannot express: a pattern has no ``contains()``,
    so the query falls back to "every innermost element bearing any text" and
    lets Python decide -- the same fallback ``_contains`` already used for a
    needle whose case mapping is not one-to-one.

    Cheap now and not before: the confirm reads one ``DOMSnapshot`` for all the
    candidates rather than a ``describeNode`` each (§8.21).
    """
    await page.goto("/list.html")
    assert await page.get_by_text(re.compile(r"^(Acme|Beta)$")).count() == 2
    assert await page.get_by_text(re.compile(r"^acme$", re.IGNORECASE)).count() == 1
    assert await page.get_by_text(re.compile(r"amm")).count() == 1
    assert await page.get_by_text(re.compile(r"nothing here")).count() == 0


async def test_a_pattern_is_used_with_the_authors_own_flags(page: Page) -> None:
    """§4.2: nothing crosses a language boundary, so the pattern
    is used as written -- no reconstruction, no flag subset."""
    await page.goto("/list.html")
    assert await page.get_by_text(re.compile(r"ACME")).count() == 0
    assert await page.get_by_text(re.compile(r"ACME", re.IGNORECASE)).count() == 1


async def test_a_pattern_in_a_filter_and_inside_a_frame(page: Page) -> None:
    await page.goto("/list.html")
    assert await page.locator("#packs li").filter(has_text=re.compile(r"^Beta$")).count() == 1
    await page.goto("/frames.html")
    assert await page.frame_locator("#widget").get_by_text(re.compile(r"^inside")).count() == 1
