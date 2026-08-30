"""Acting on an element a scrollable ancestor has clipped out of sight.

Found by the pilot suite, as an assertion that made no sense: a Radix radio in
the Inspector dock, clicked, and then `to_be_checked` waiting its whole five
seconds and only ever reading "not checked". The failure artefact ruled out
everything it looked like -- the click reported success in 27 ms, the console
was empty, the network was quiet. Nothing had gone wrong *after* the click.

Nothing had gone right during it either. The radio sat at ``y=658`` inside a
dock body whose ``clientHeight`` was 650, so it was scrolled past the bottom of
its own scroller while still inside the viewport. ``scrollIntoView`` moves an
element into the *viewport*, and this one was already there
(§8.6 makes the same distinction for a covered element); the
scroller was never scrolled, the press went to the coordinates the element
would have occupied, and the page's ``<html>`` took it.

So the click "succeeds", the control never changes, and every assertion after it
is correct to say so. That is the worst shape a driver bug can have: the action
reports success, the failure surfaces one line later, and the message describes
the element rather than the click.

Kept as one page and four tests because each one rules something out.

Fixed in §8.31. The cause was not the scroll but the hit test: it
accepted the point when the node found there was an *ancestor* of the element,
and ``<html>`` is an ancestor of everything, so the empty point passed and
§8.6's scroll-and-retry fallback -- which would have scrolled the scroller --
never ran.
"""

import pytest

from wirespec import Page, expect

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: Where the radio is, and where its scroller ends. The gap between them is the
#: bug's whole premise, so it is asserted rather than described.
SCROLLER = "document.querySelector('.scroller')"
RADIO = "document.querySelector('button[value=per_tenant]')"


async def test_the_control_is_clipped_by_its_scroller_but_inside_the_viewport(page: Page) -> None:
    """The premise, measured.

    Two things have to be true at once for this page to be about anything: the
    element is inside the viewport, so nothing that scrolls the *window* will
    help, and it is past the bottom edge of its own scroll container, so it
    cannot be hit. If a layout change ever makes the radio reachable this test
    fails first and says which half stopped holding.
    """
    await page.goto("/clipped.html")

    geometry = await page.evaluate(
        f"""() => {{
            const radio = {RADIO}, scroller = {SCROLLER};
            const box = radio.getBoundingClientRect(), clip = scroller.getBoundingClientRect();
            return {{
                top: box.top, bottom: box.bottom,
                clip_bottom: clip.bottom, viewport: window.innerHeight,
                scroll_top: scroller.scrollTop,
                scrollable: scroller.scrollHeight > scroller.clientHeight,
            }};
        }}"""
    )

    assert geometry["scrollable"], "the dock body has to have somewhere to scroll"
    assert geometry["scroll_top"] == 0, "and has to start unscrolled"
    assert geometry["bottom"] <= geometry["viewport"], "the radio is inside the viewport"
    assert geometry["top"] > geometry["clip_bottom"], "and below the bottom edge of its scroller"


async def test_nothing_is_at_the_point_the_box_reports(page: Page) -> None:
    """Why the press goes nowhere: the box is real and the point is empty.

    ``bounding_box`` is not wrong -- a clipped element keeps its layout box, and
    that box is where the element *would* be drawn. It is simply not where the
    element can be reached, and a hit test at its centre says so.
    """
    await page.goto("/clipped.html")
    radio = page.get_by_role("complementary").get_by_role("radio", name="Per tenant")

    box = await radio.bounding_box()
    assert box is not None

    at_centre = await page.evaluate(
        """(point) => {
            const found = document.elementFromPoint(point.x, point.y);
            return found ? found.tagName : null;
        }""",
        {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2},
    )
    assert at_centre == "HTML", f"expected the point to be empty, and {at_centre} was there"


async def test_the_click_reaches_the_control(page: Page) -> None:
    """The reproduction.

    Exactly the pilot suite's two lines, on its own markup: click the radio, then assert
    it is checked. Before the fix the click returned without complaint and this
    failed on the assertion, with the message that sent nobody anywhere::

        expected get_by_role('complementary') -> get_by_role('radio', name='Per tenant')
            to be checked
        last saw that it was not

    An action that cannot reach its element must scroll the scroller that hid it
    or refuse and say what is in the way -- never press an empty point and report
    that it worked.
    """
    await page.goto("/clipped.html")
    radio = page.get_by_role("complementary").get_by_role("radio", name="Per tenant")

    await radio.click()
    await expect(radio).to_be_checked()


async def test_the_same_click_works_once_the_scroller_is_scrolled(page: Page) -> None:
    """The control, which is what makes the test above a driver bug.

    Same page, same locator, same click -- with one ``scrollIntoView`` on the
    element first, which is the thing the action did not do. It passes. So the
    radio is fine, the label wrapper is fine, the assertion is fine, and what is
    missing is scrolling the *ancestor* rather than the window.
    """
    await page.goto("/clipped.html")
    radio = page.get_by_role("complementary").get_by_role("radio", name="Per tenant")

    await page.evaluate(f"() => {RADIO}.scrollIntoView({{block: 'center'}})")
    assert await page.evaluate(f"() => {SCROLLER}.scrollTop") > 0

    await radio.click()
    await expect(radio).to_be_checked()
