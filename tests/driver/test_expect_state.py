"""State assertions: visible, hidden, enabled, checked, focused, empty.

Single-element assertions are **strict** (§5.3): asked about a
locator matching three elements they fail and say so, because a locator matching
more than it meant to is a bug that otherwise surfaces much later in a different
test.
"""

import re

import pytest

from wirespec.errors import WirespecTimeoutError
from wirespec.expect import expect
from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_visible_and_hidden_are_opposites_that_both_wait(page: Page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#one")).to_be_visible()
    await expect(page.locator("#invisible")).to_be_hidden()
    await expect(page.locator("#removed")).to_be_hidden()
    await expect(page.locator("#one")).not_to_be_hidden()


async def test_an_element_that_never_rendered_is_hidden(page: Page) -> None:
    """§6.4: ``to_be_hidden`` treats absent as hidden. An element
    that never rendered is not on screen, and making a spec say which of the two
    it meant would be asking about the implementation."""
    await page.goto("/assertions.html")
    await expect(page.locator("#never-existed")).to_be_hidden()


async def test_a_single_element_assertion_is_strict(page: Page) -> None:
    """§5.3. Three matches is a spec bug, reported here rather than
    silently answered about whichever one came first."""
    await page.goto("/assertions.html")
    with pytest.raises(WirespecTimeoutError) as raised:
        await expect(page.locator("#rows li"), timeout=0.4).to_be_visible()
    assert "exactly one" in str(raised.value)
    assert "3" in str(raised.value)


async def test_visibility_is_waited_for(page: Page) -> None:
    await page.goto("/assertions.html")
    await page.evaluate("() => window.__grow(100)")
    await expect(page.locator(".grown")).to_be_visible()


async def test_enabled_and_disabled(page: Page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#value")).to_be_enabled()
    await expect(page.locator("#disabled")).to_be_disabled()
    await expect(page.locator("#disabled")).not_to_be_enabled()


async def test_checked(page: Page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#checked")).to_be_checked()
    await expect(page.locator("#unchecked")).not_to_be_checked()


async def test_editable(page: Page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#value")).to_be_editable()
    await expect(page.locator("#readonly")).not_to_be_editable()


async def test_focused(page: Page) -> None:
    """The AX node's own ``focused`` property, which arrived with the role and
    cost nothing extra (§5.2)."""
    await page.goto("/assertions.html")
    await expect(page.locator("#focusable")).not_to_be_focused()
    await page.evaluate("() => document.getElementById('focusable').focus()")
    await expect(page.locator("#focusable")).to_be_focused()


async def test_empty(page: Page) -> None:
    """Empty means no text, for an element; and no value, for a control."""
    await page.goto("/assertions.html")
    await expect(page.locator("#empty")).to_be_empty()
    await expect(page.locator("#one")).not_to_be_empty()
    await expect(page.locator("#blank")).to_be_empty()
    await expect(page.locator("#value")).not_to_be_empty()


async def test_the_page_url_can_be_asserted(page: Page, site: str) -> None:
    """A relative path is resolved against the context's ``base_url``, so a spec
    writes the path it navigated to rather than reassembling the fixture
    server's port."""
    await page.goto("/assertions.html")
    await expect(page).to_have_url("/assertions.html")
    await expect(page).to_have_url(f"{site}/assertions.html")
    await expect(page).not_to_have_url("/somewhere-else")


async def test_a_url_assertion_waits_for_a_navigation_the_page_started(page: Page, site: str) -> None:
    """Which is the point of it being a retrying assertion rather than a
    comparison: the application navigates, not the spec."""
    await page.goto("/assertions.html")
    await page.evaluate("() => window.__later(100, () => { location.href = '/index.html'; })")
    await expect(page).to_have_url("/index.html")


async def test_a_url_assertion_quotes_where_the_page_actually_is(page: Page) -> None:
    await page.goto("/assertions.html")
    with pytest.raises(WirespecTimeoutError) as raised:
        await expect(page, timeout=0.4).to_have_url("/nowhere.html")
    assert "nowhere.html" in str(raised.value)
    assert "assertions.html" in str(raised.value)


async def test_not_to_be_visible_waits_for_something_to_stop_showing(page: Page) -> None:
    """The negation is not "check it is not there yet" -- it **polls until the
    condition is false**, which is what makes it usable against a spinner or a
    toast (§6.4). Asserted the hard way: the element is on screen
    when the assertion starts.
    """
    await page.goto("/assertions.html")
    await expect(page.locator("#one")).to_be_visible()
    await page.evaluate(
        "() => window.__later(100, () => { document.getElementById('one').style.visibility = 'hidden'; })"
    )
    await expect(page.locator("#one")).not_to_be_visible()


async def test_not_to_be_visible_holds_for_an_element_that_is_gone(page: Page) -> None:
    """Zero matches satisfies the negation, matching Playwright
    (§4.3). An element the page removed is not on screen, which is
    all the assertion claims.

    *Two* matches still refuses, in both directions: only zero is forgiven, the
    same asymmetry ``to_be_hidden`` makes (§6.4).
    """
    await page.goto("/assertions.html")
    await page.evaluate("() => window.__later(100, () => { document.getElementById('one').remove(); })")
    await expect(page.locator("#one")).not_to_be_visible()
    await expect(page.locator("#one")).to_be_hidden()

    with pytest.raises(WirespecTimeoutError) as raised:
        await expect(page.locator("#rows li"), timeout=0.3).not_to_be_visible()
    assert "exactly one" in str(raised.value)


async def test_not_to_be_disabled_waits_for_a_control_to_come_back(page: Page) -> None:
    """The shape a spec actually needs: a form disabled while it submits, and
    an assertion that waits for it rather than for a fixed number of seconds."""
    await page.goto("/assertions.html")
    await expect(page.locator("#disabled")).to_be_disabled()
    await page.evaluate("() => window.__later(100, () => { document.getElementById('disabled').disabled = false; })")
    await expect(page.locator("#disabled")).not_to_be_disabled()


async def test_css_is_compared_after_the_browser_has_computed_it(page: Page) -> None:
    """§6.4: the *computed* value is the only comparable one -- a
    colour declared as a keyword comes back as ``rgb(...)``. Both directions,
    because the negation is what a spec writes to assert a class came off."""
    await page.goto("/assertions.html")
    await expect(page.locator("#swatch")).to_have_css("color", "rgb(20, 40, 60)")
    await expect(page.locator("#swatch")).not_to_have_css("color", "rgb(0, 0, 0)")


async def test_not_to_have_css_waits_for_a_style_to_change(page: Page) -> None:
    await page.goto("/assertions.html")
    await page.evaluate(
        "() => window.__later(100, () => { document.getElementById('swatch').style.color = 'rgb(9, 9, 9)'; })"
    )
    await expect(page.locator("#swatch")).not_to_have_css("color", "rgb(20, 40, 60)")


async def test_to_have_title_reads_the_documents_title(page: Page) -> None:
    await page.goto("/index.html")
    await expect(page).to_have_title("wirespec driver fixture")
    await expect(page).to_have_title(re.compile(r"^wirespec"))
    await expect(page).not_to_have_title("something else")


async def test_to_have_title_waits_for_a_script_to_set_it(page: Page) -> None:
    """The reason it is a polling assertion rather than a read: an application
    sets the title when the data arrives, not when the document does."""
    await page.goto("/index.html")
    await page.evaluate("() => setTimeout(() => { document.title = 'arrived'; }, 150)")
    await expect(page).to_have_title("arrived")


async def test_to_have_title_says_what_it_saw(page: Page) -> None:
    await page.goto("/index.html")
    with pytest.raises(WirespecTimeoutError) as raised:
        await expect(page, timeout=0.3).to_have_title("not this")
    assert "not this" in str(raised.value)
    assert "wirespec driver fixture" in str(raised.value)
