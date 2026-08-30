"""Frames: a document inside a document, addressed from the outside.

Everything else in the driver suite resolves against one document. A frame is
the case where the chain has to *change document* half way through, and the
reason it is a step rather than a second `Page` is that node ids for a
same-process frame live in the page's own id space -- so a query root inside
the frame is an ordinary root, and every reader, matcher and action below it
works unchanged (§8.19).

The fixture is built to catch a leak in either direction: the host document has
a `#press` button with the same text as the one in every frame, and each frame
has its own. A frame step that resolved against the whole document would find
the wrong one and the assertion would still pass.
"""

import pytest

from wirespec.errors import WirespecError
from wirespec.expect import expect
from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_locator_inside_a_frame_finds_the_frames_element(page: Page) -> None:
    """The whole point: `#press` exists three times over and this is the one
    inside `#widget`."""
    await page.goto("/frames.html")
    inside = page.frame_locator("#widget").locator("#press")
    assert await inside.count() == 1
    await expect(inside).to_be_visible()


async def test_the_host_document_is_not_searched(page: Page) -> None:
    """`h1` is "host document" outside and "inside the frame" within."""
    await page.goto("/frames.html")
    assert await page.locator("h1").text_content() == "host document"
    assert await page.frame_locator("#widget").locator("h1").text_content() == "inside the frame"


async def test_two_frames_of_the_same_document_are_told_apart(page: Page) -> None:
    """Same URL, same markup, two frames. Clicking in one must not move the
    other -- which is the only way to see that the node ids really are the
    frame's own and not the first frame's, reused."""
    await page.goto("/frames.html")
    first = page.frame_locator("#widget")
    second = page.frame_locator("#second")

    await first.locator("#press").click()

    await expect(first.locator("#said")).to_have_text("pressed")
    await expect(second.locator("#said")).to_have_text("nothing yet")
    await expect(page.locator("#said")).to_have_text("host says nothing")


async def test_a_frame_inside_a_frame(page: Page) -> None:
    """Chained. `#outer` holds a document that itself holds `#deepest`."""
    await page.goto("/frames.html")
    deep = page.frame_locator("#outer").frame_locator("#deepest")
    assert await deep.locator("h1").text_content() == "inside the frame"
    assert await page.frame_locator("#outer").locator("h1").text_content() == "the middle document"


async def test_every_query_kind_works_inside_a_frame(page: Page) -> None:
    """A frame changes the root and nothing else, so the role, text and label
    queries have to keep working -- including the two that do not start from a
    CSS selector."""
    await page.goto("/frames.html")
    frame = page.frame_locator("#widget")
    await expect(frame.get_by_role("button", name="Press me")).to_be_visible()
    await expect(frame.get_by_text("nothing yet")).to_be_visible()
    await expect(frame.get_by_label("Name")).to_be_visible()


async def test_a_text_query_inside_a_frame_does_not_escape_it(page: Page) -> None:
    """`performSearch` has no root and searches every document in the target,
    frames included -- measured, three hits for "Press me" on this page. So the
    scoping that follows it is what keeps a frame a frame."""
    await page.goto("/frames.html")
    assert await page.frame_locator("#widget").get_by_text("Press me").count() == 1
    assert await page.get_by_text("Press me").count() == 1


async def test_waiting_happens_inside_the_frame(page: Page) -> None:
    """The frame appends `#eventually` 300 ms in. Mutations inside a
    same-process frame arrive on the page's own session, so the wait loop needs
    no help."""
    await page.goto("/frames.html")
    await expect(page.frame_locator("#widget").locator("#eventually")).to_have_text("here at last")


async def test_typing_into_a_frame_goes_to_the_frames_field(page: Page) -> None:
    """Actions use main-frame coordinates for a same-process frame -- measured,
    the box model already comes back in the host's space."""
    await page.goto("/frames.html")
    field = page.frame_locator("#widget").locator("#who")
    await field.fill("Okan")
    await expect(field).to_have_value("Okan")
    await expect(page.frame_locator("#second").locator("#who")).to_have_value("")


async def test_a_frame_that_appears_late_is_waited_for(page: Page) -> None:
    """The frame element does not exist when the locator is built. Nothing
    special is needed: an unmatched frame step resolves to nothing and the wait
    loop looks again."""
    await page.goto("/frames.html")
    eventual = page.frame_locator("#late").locator("h1")
    await page.locator("#add").click()
    await expect(eventual).to_have_text("inside the frame")


async def test_a_missing_frame_times_out_naming_the_frame(page: Page) -> None:
    """Not an error about the element inside it, which is the wrong end of the
    problem to hand somebody."""
    await page.goto("/frames.html")
    with pytest.raises(WirespecError) as raised:
        await expect(page.frame_locator("#absent").locator("h1"), timeout=1).to_be_visible()
    assert "#absent" in str(raised.value)


async def test_an_element_that_is_not_a_frame_says_so(page: Page) -> None:
    """A `<div>` has no document inside it. Waiting for one would time out
    saying the element was not visible, which is true and useless."""
    await page.goto("/frames.html")
    with pytest.raises(WirespecError) as raised:
        await page.frame_locator("#not-a-frame").locator("h1").text_content(timeout=1)
    assert "not-a-frame" in str(raised.value)
    assert "DIV" in str(raised.value)


async def test_a_cross_origin_frame_refuses_by_name(page: Page) -> None:
    """Measured: a frame Chrome puts in another renderer process comes back
    with `contentDocument: null`, and no query against the page's session can
    reach it. It is a named gap rather than a timeout (§8.19)."""
    await page.goto("/frames.html")
    with pytest.raises(WirespecError) as raised:
        await page.frame_locator("#stranger").locator("h1").text_content(timeout=1)
    message = str(raised.value)
    assert "#stranger" in message
    assert "cross-origin" in message
    assert "localhost" in message


async def test_the_frames_owner_is_a_locator_on_the_host(page: Page) -> None:
    """`owner` is the way back out: the `<iframe>` element itself, in the
    document that contains it."""
    await page.goto("/frames.html")
    owner = page.frame_locator("#widget").owner
    assert await owner.get_attribute("id") == "widget"


async def test_content_frame_descends_from_a_locator(page: Page) -> None:
    """The other direction Playwright offers, and the one that composes: any
    locator that names an `<iframe>` can be stepped into."""
    await page.goto("/frames.html")
    frame = page.locator("iframe#second").content_frame
    assert await frame.locator("h1").text_content() == "inside the frame"
