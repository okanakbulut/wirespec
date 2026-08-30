"""The rest of the step vocabulary: label, placeholder, test id, filter, nth, or.

Together with css, text and role these are the nine step kinds §4.1
names. Everything here is a *refinement* except the first three.
"""

import pytest

from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_label_finds_the_control_and_not_the_button(page: Page) -> None:
    """The distinction that makes this worth doing properly. A <button> whose
    text is "Email address" has that accessible name too -- but it got it from
    its own contents, not from a label, and Chrome says so in the name cascade.
    """
    await page.goto("/forms.html")
    assert await page.get_by_label("Email address").count() == 1
    assert await page.get_by_role("button", name="Email address").count() == 1


@pytest.mark.parametrize(
    ("label", "control"),
    [
        ("Email address", "email"),  # <label for=...>
        ("Postcode", "postcode"),  # a wrapping <label>
        ("Telephone", "aria"),  # aria-label
        ("Date of birth", "dob"),  # aria-labelledby
    ],
)
async def test_every_way_of_labelling_a_control_is_found(page: Page, label: str, control: str) -> None:
    """All four are what Playwright's get_by_label accepts, and all four are one
    branch of Chrome's own name cascade rather than four rules here."""
    await page.goto("/forms.html")
    found = page.get_by_label(label, exact=True)
    assert await found.count() == 1
    assert await page.locator(f"#{control}").count() == 1


async def test_a_placeholder_is_matched_on_the_attribute(page: Page) -> None:
    await page.goto("/forms.html")
    assert await page.get_by_placeholder("you@example.test").count() == 1
    assert await page.get_by_placeholder("example").count() == 1


async def test_a_test_id_is_exact_by_default(page: Page) -> None:
    """Unlike every other query. ``row-1`` matching ``row-12`` is a bug that
    waits for the twelfth row to exist before it appears."""
    await page.goto("/forms.html")
    assert await page.get_by_test_id("row-1").count() == 1
    assert await page.get_by_test_id("row-1", exact=False).count() == 2


async def test_filter_keeps_and_drops_by_text(page: Page) -> None:
    await page.goto("/forms.html")
    rows = page.locator("#rows li")
    assert await rows.count() == 3
    assert await rows.filter(has_text="Acme").count() == 2
    assert await rows.filter(has_not_text="Acme").count() == 1
    assert await rows.filter(has_text="active").count() == 2


async def test_nth_first_and_last_pick_one(page: Page) -> None:
    await page.goto("/forms.html")
    rows = page.locator("#rows li")
    assert await rows.first.count() == 1
    assert await rows.nth(1).count() == 1
    assert await rows.last.count() == 1
    assert await rows.nth(9).count() == 0, "an index past the end is empty, not an error"


async def test_or_offers_an_alternative_to_the_whole_chain(page: Page) -> None:
    """How a spec waits for one of two outcomes without knowing which arrives."""
    await page.goto("/forms.html")
    either = page.locator("#either-a").or_(page.locator("#either-b"))
    assert await either.count() == 1
    neither = page.locator("#either-c").or_(page.locator("#either-b"))
    assert await neither.count() == 0


async def test_or_still_offers_it_when_the_left_side_matched_nothing(page: Page) -> None:
    """The case `or_` exists for, and the one the other test cannot reach.

    A chain resolves left to right and stops the moment it matches nothing,
    which is right for every step except this one: `or_` is an alternative to
    everything before it, so "before it matched nothing" is the *normal* way to
    reach it. Short-circuiting there makes `or_` answer 0 whenever the outcome
    that arrived was the second one -- which is half the time, silently.
    """
    await page.goto("/forms.html")
    either = page.locator("#either-c").or_(page.locator("#either-a"))
    assert await either.count() == 1
    assert await either.text_content() == "the first outcome"


async def test_a_chain_of_pure_refinements_resolves_to_nothing(page: Page) -> None:
    """§4.1. A filter with no query in front of it must not
    quietly mean "the whole document"."""
    await page.goto("/forms.html")
    from wirespec.locator import Locator

    assert await Locator(page).filter(has_text="Acme").count() == 0
