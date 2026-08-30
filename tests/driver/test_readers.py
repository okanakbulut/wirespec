"""Reading the page: text, state, geometry, and the caller's own JavaScript.

Every reader that needs exactly one element **waits** for exactly one
(§6.3). Returning "no such element" the instant it does not exist
would make every page object carry its own sleep, which is the thing the locator
model exists to remove.
"""

import pytest

from wirespec.errors import WirespecTimeoutError
from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_text_content_is_the_markups_text(page: Page) -> None:
    await page.goto("/readers.html")
    assert await page.locator("#lines").text_content() == "onetwo"


async def test_inner_text_is_the_rendered_text(page: Page) -> None:
    """The difference that matters: a <br> is a line break on the screen and
    nothing at all in textContent (§8.11)."""
    await page.goto("/readers.html")
    assert await page.locator("#lines").inner_text() == "one\ntwo"
    assert await page.locator("#blocks").inner_text() == "alpha\nbeta"


async def test_inner_text_agrees_with_the_browsers_own(page: Page) -> None:
    """Held against ``element.innerText`` directly, on the cases the fixture
    covers. Where they diverge, §4.3 says so and says why."""
    await page.goto("/readers.html")
    for element in ("#lines", "#blocks", "#transparent", "#rows", "#generated"):
        mine = await page.locator(element).inner_text()
        theirs = await page.evaluate(f"() => document.querySelector({element!r}).innerText")
        assert mine == theirs, element


async def test_all_inner_texts_reads_every_match_in_one_snapshot(page: Page) -> None:
    """Which is what makes reading two hundred rows cost what reading one
    costs: the snapshot is taken once, however many elements are asked about."""
    await page.goto("/readers.html")
    assert await page.locator("#rows li").all_inner_texts() == ["Row one", "Row two", "Row three"]


async def test_generated_content_is_not_rendered_text(page: Page) -> None:
    """``::marker``, ``::before`` and ``::after`` have layout boxes with text in
    them and ``innerText`` does not report it. Measured on a <ul>: without the
    exclusion every row reads "\u2022 Row one"."""
    await page.goto("/readers.html")
    assert await page.locator("#rows li").first.inner_text() == "Row one"
    assert await page.locator("#generated").inner_text() == "real text"


async def test_all_text_contents_keeps_document_order(page: Page) -> None:
    await page.goto("/readers.html")
    assert await page.locator("#rows li").all_text_contents() == ["Row one", "Row two", "Row three"]


async def test_visibility_is_a_box_and_not_hidden(page: Page) -> None:
    """§5.2, and the two exclusions it is careful about: opacity
    and in-viewport are deliberately *not* part of it, because both make
    correct specs flake."""
    await page.goto("/readers.html")
    assert await page.locator("#lines").is_visible() is True
    assert await page.locator("#hidden-by-visibility").is_visible() is False
    assert await page.locator("#hidden-by-display").is_visible() is False
    assert await page.locator("#transparent").is_visible() is True, "opacity is not visibility"
    assert await page.locator("#below").is_visible() is True, "below the fold is one scroll away"


async def test_is_visible_answers_no_for_something_absent(page: Page) -> None:
    """It does not wait and does not raise: "is it visible" about an element
    that is not there has an answer."""
    await page.goto("/readers.html")
    assert await page.locator("#nothing-like-this").is_visible() is False


async def test_the_input_state_comes_from_the_accessibility_node(page: Page) -> None:
    """checked, disabled and readonly arrived with the role and cost nothing
    extra (§5.2)."""
    await page.goto("/readers.html")
    assert await page.locator("#checked").is_checked() is True
    assert await page.locator("#unchecked").is_checked() is False
    assert await page.locator("#disabled").is_enabled() is False
    assert await page.locator("#value").is_enabled() is True
    assert await page.locator("#readonly").is_editable() is False
    assert await page.locator("#value").is_editable() is True


async def test_input_value_reads_a_picker_too(page: Page) -> None:
    """The finding from §8.4's experiment, put to use: a date
    input's value is on its accessibility node, so reading it needs no
    JavaScript."""
    await page.goto("/readers.html")
    assert await page.locator("#value").input_value() == "in the field"
    assert await page.locator("#date").input_value() == "2026-03-15"


async def test_get_attribute_returns_none_for_one_that_is_absent(page: Page) -> None:
    await page.goto("/readers.html")
    assert await page.locator("#rows li").first.get_attribute("data-kind") == "a"
    assert await page.locator("#rows li").first.get_attribute("data-nothing") is None


async def test_the_bounding_box_is_the_border_box(page: Page) -> None:
    """Border, not content. ``getBoundingClientRect`` is the border box and
    every spec's mental model is built on it; a padded button's content box can
    sit tens of pixels from where the button looks (§8.9)."""
    await page.goto("/readers.html")
    box = await page.locator("#padded").bounding_box()
    assert box is not None
    # 100 content + 20 padding + 5 border on each side.
    assert box["width"] == pytest.approx(150, abs=1)
    assert box["height"] == pytest.approx(90, abs=1)
    # left/top place the *margin* box; the border box is the 8px margin further
    # in. Confusing the two is how a click lands just outside what it aimed at.
    assert (box["x"], box["y"]) == pytest.approx((58, 68), abs=1)


async def test_a_reader_waits_for_the_element_to_arrive(page: Page) -> None:
    """The whole point of the wait: the fixture adds this element 300 ms after
    load, and no spec had to say so."""
    await page.goto("/readers.html")
    assert await page.locator("#late").text_content() == "arrived late"


async def test_a_reader_that_never_gets_one_element_says_what_it_saw(page: Page) -> None:
    """§5.1: keep the last reading. "expected one, saw three" has
    usually already answered the question "timed out" sends someone to the
    browser for."""
    await page.goto("/readers.html")
    with pytest.raises(WirespecTimeoutError) as raised:
        await page.locator("#rows li").text_content(timeout=0.5)
    assert "exactly one" in str(raised.value)
    assert "3" in str(raised.value)


async def test_evaluate_runs_the_callers_function_on_the_element(page: Page) -> None:
    """``callFunctionOn`` passes the handle as ``this``; specs write
    ``node => ...``. The wrapper is what makes both spellings work."""
    await page.goto("/readers.html")
    assert await page.locator("#lines").evaluate("node => node.tagName") == "DIV"


async def test_evaluate_takes_an_argument_after_the_element(page: Page) -> None:
    """Playwright's ``(element, arg) => ...``. The element still arrives as
    ``this`` from ``callFunctionOn``, so the wrapper hands in both, in that
    order, and a spec that passes nothing keeps the one-parameter shape."""
    await page.goto("/readers.html")
    assert await page.locator("#lines").evaluate("(node, suffix) => node.tagName + suffix", "!") == "DIV!"
    assert await page.locator("#lines").evaluate("(node, n) => n * 2", 21) == 42


async def test_evaluate_all_runs_once_over_the_whole_match_array(page: Page) -> None:
    """Reading an attribute off every row costs one call, because the array is
    built from references inside the page and never crosses the wire."""
    await page.goto("/readers.html")
    kinds = await page.locator("#rows li").evaluate_all("nodes => nodes.map(n => n.dataset.kind)")
    assert kinds == ["a", "b", "c"]


async def test_all_gives_one_locator_per_match(page: Page) -> None:
    await page.goto("/readers.html")
    rows = await page.locator("#rows li").all()
    assert len(rows) == 3
    assert [await row.text_content() for row in rows] == ["Row one", "Row two", "Row three"]


async def test_text_content_keeps_the_whitespace_between_elements(page: Page) -> None:
    """``textContent`` includes the whitespace-only text nodes between inline
    elements, and dropping them runs the words together.

    ``DOM.describeNode`` does not report those nodes at all -- not even in
    ``childNodeCount`` -- so a rebuild from the DOM tree answers ``"ab"`` where
    the page says ``"a b"`` (§8.21). Not a formatting difference:
    ``to_have_text("a b")`` fails against it, and normalising cannot put back a
    space that was never read.
    """
    await page.goto("/text.html")
    assert await page.locator("#spaced").text_content() == "a b c"


async def test_text_content_is_textcontent_indentation_and_all(page: Page) -> None:
    """The whole of it, byte for byte -- which is what Playwright's returns."""
    await page.goto("/readers.html")
    assert await page.locator("#rows").text_content() == "\n  Row one\n  Row two\n  Row three\n"


async def test_text_content_still_leaves_out_what_is_not_on_the_screen(page: Page) -> None:
    """§8.3, unchanged: a ``<script>``'s source is in
    ``textContent`` and is not text anyone can see."""
    await page.goto("/text.html")
    assert await page.locator("#scripts").text_content() == "visible"
