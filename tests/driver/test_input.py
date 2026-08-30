"""Mouse and keyboard: real input events, dispatched at coordinates.

``Mouse`` is separate from ``Locator`` because a drag is not something done *to*
an element (§6.5): it starts on one, ends on another, the walk
between them is the point, and a spec may need to stop mid-gesture with the
button still down.
"""

import pytest

from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_click_lands_where_it_was_aimed(page: Page) -> None:
    await page.goto("/input.html")
    await page.mouse.click(80, 50)
    assert await page.evaluate("() => window.__clicks.map(c => c[0])") == ["mousedown", "mouseup", "click"]


async def test_the_button_and_click_count_reach_the_page(page: Page) -> None:
    await page.goto("/input.html")
    await page.mouse.click(80, 50, button="right")
    kinds = await page.evaluate("() => window.__clicks.map(c => [c[0], c[1]])")
    assert ["contextmenu", 2] in kinds


async def test_a_double_click_is_two_presses_with_a_rising_count(page: Page) -> None:
    """Not two clicks that happen to be adjacent: the ``detail`` the page sees
    has to reach 2, or a dblclick handler never fires."""
    await page.goto("/input.html")
    await page.mouse.dblclick(80, 50)
    details = await page.evaluate("() => window.__clicks.filter(c => c[0] === 'dblclick').map(c => c[2])")
    assert details == [2]


async def test_a_move_walks_in_steps(page: Page) -> None:
    """``steps`` exists because a page watching ``mousemove`` sees a jump as one
    event and a walk as many -- and hover menus are written against the walk."""
    await page.goto("/input.html")
    # Start inside the pad, so every step of the walk lands on the element that
    # is counting -- otherwise this measures where the pad is, not how many
    # events the walk produced.
    await page.mouse.move(40, 180)
    await page.evaluate("() => window.__reset()")
    await page.mouse.move(300, 270, steps=5)
    moves = await page.evaluate("() => window.__mouse.filter(m => m[0] === 'mousemove').length")
    assert moves == 5

    await page.evaluate("() => window.__reset()")
    await page.mouse.move(40, 180)
    assert await page.evaluate("() => window.__mouse.filter(m => m[0] === 'mousemove').length") == 1


async def test_down_and_up_can_be_split(page: Page) -> None:
    """The property that makes a drag possible at all: a spec can stop
    mid-gesture with the button still down."""
    await page.goto("/input.html")
    await page.mouse.move(80, 50)
    await page.mouse.down()
    assert await page.evaluate("() => window.__clicks.map(c => c[0])") == ["mousedown"]
    await page.mouse.up()
    assert await page.evaluate("() => window.__clicks.map(c => c[0])") == ["mousedown", "mouseup", "click"]


async def test_typing_produces_a_keystroke_per_character(page: Page) -> None:
    """§6.5: ``Keyboard.type`` types character by character, which
    ``fill`` does not. A combobox filtering as you type is watching the
    keystrokes, not the value."""
    await page.goto("/input.html")
    await page.evaluate("() => document.getElementById('field').focus()")
    await page.evaluate("() => window.__reset()")
    await page.keyboard.type("abc")
    assert await page.evaluate("() => document.getElementById('field').value") == "abc"
    downs = await page.evaluate("() => window.__keys.filter(k => k[0] === 'keydown').map(k => k[1])")
    assert downs == ["a", "b", "c"]


async def test_a_named_key_is_pressed(page: Page) -> None:
    await page.goto("/input.html")
    await page.evaluate("() => document.getElementById('field').focus()")
    await page.evaluate("() => window.__reset()")
    await page.keyboard.press("Enter")
    downs = await page.evaluate("() => window.__keys.filter(k => k[0] === 'keydown').map(k => k[1])")
    assert downs == ["Enter"]


async def test_backspace_actually_deletes(page: Page) -> None:
    """The check that the key code is right and not merely present: a wrong
    ``keyCode`` produces an event nothing recognises, and nothing says so
    (§12)."""
    await page.goto("/input.html")
    await page.evaluate("() => document.getElementById('field').focus()")
    await page.keyboard.type("abc")
    await page.keyboard.press("Backspace")
    assert await page.evaluate("() => document.getElementById('field').value") == "ab"


@pytest.mark.parametrize("key", ["Enter", "Escape", "Tab", "Backspace", "ArrowDown", "ArrowUp"])
async def test_every_key_in_the_table_arrives_named(page: Page, key: str) -> None:
    """The whole table, one test. It is a table rather than a computed map
    because a wrong ``keyCode`` fails silently (§12)."""
    await page.goto("/input.html")
    await page.evaluate("() => document.getElementById('field').focus()")
    await page.evaluate("() => window.__reset()")
    await page.keyboard.press(key)
    downs = await page.evaluate("() => window.__keys.filter(k => k[0] === 'keydown').map(k => k[1])")
    assert downs == [key]


async def test_an_unmapped_key_raises_rather_than_arriving_wrong(page: Page) -> None:
    """§12. A key wirespec does not know would otherwise arrive
    with a zero key code, and the handler that was supposed to see it simply
    would not -- with nothing failing."""
    await page.goto("/input.html")
    with pytest.raises(NotImplementedError, match="F13"):
        await page.keyboard.press("F13")
