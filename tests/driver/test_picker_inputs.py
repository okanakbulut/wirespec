"""What a picker input will and will not accept, measured.

§12 carried "picker inputs have no verified fill path" as open,
with §8.4 proposing focus-and-type-the-segments and calling it unverified. It
is verified now, and the answer is not the one §8.4 assumed: it works for four
of the seven types, does nothing at all for three, and where it works the digit
order is the *process locale's*.

``fill`` is step 7 (§13) and does not exist yet. These pin Chrome's
behaviour so that whatever is built on it is built on a measurement, and so a
Chrome upgrade that changes one of them fails here and names it.
"""

import pytest

from wirespec.cdp import input as input_domain
from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: Every picker type §8.4 names.
PICKERS = ("date", "datetime-local", "month", "time", "week", "color", "range")

_DIGITS = {str(d): 0x30 + d for d in range(10)}
_LETTERS = {"P": (0x50, "KeyP"), "A": (0x41, "KeyA")}
_ARROWS = {"ArrowUp": 0x26, "ArrowDown": 0x28}


async def press(page: Page, key: str) -> None:
    """One keystroke, spelled the way Chrome needs it.

    ``windows_virtual_key_code`` is not optional: a missing or wrong code fails
    silently, because the event does arrive and nothing recognises it
    (§12).
    """
    if key in _DIGITS:
        code, key_code, text = f"Digit{key}", _DIGITS[key], key
    elif key in _LETTERS:
        key_code, code = _LETTERS[key]
        text = key
    else:
        code, key_code, text = key, _ARROWS[key], None
    for kind in ("keyDown", "keyUp"):
        await page.session.send(
            input_domain.DispatchKeyEvent(
                type=kind,
                key=key,
                code=code,
                text=text if kind == "keyDown" else None,
                unmodified_text=text if kind == "keyDown" else None,
                windows_virtual_key_code=key_code,
                native_virtual_key_code=key_code,
            )
        )


async def type_into(page: Page, element_id: str, keys) -> str:
    await page.evaluate(f"() => document.getElementById({element_id!r}).focus()")
    for key in keys:
        await press(page, key)
    return await page.evaluate(f"() => document.getElementById({element_id!r}).value")


async def test_insert_text_sets_nothing_at_all_on_a_picker(page: Page) -> None:
    """§8.4 said insertText "sets whichever segment has focus and
    silently drops the rest". Measured on Chrome 150 it is worse than that: the
    value is left completely empty. Same class of failure, and the account of
    the symptom was wrong."""
    await page.goto("/pickers.html")
    assert await type_into(page, "text", []) == ""
    for kind in ("date", "datetime-local", "month", "time", "week"):
        await page.evaluate(f"() => document.getElementById({kind!r}).focus()")
        await page.session.send(input_domain.InsertText(text="2026-03-15"))
        assert await page.evaluate(f"() => document.getElementById({kind!r}).value") == "", kind


async def test_insert_text_is_right_for_an_ordinary_field(page: Page) -> None:
    """The contrast that makes the trap a trap: the same call is exactly right
    one element away, and assigning ``.value`` instead would not raise the
    ``input`` event a controlled React field listens for."""
    await page.goto("/pickers.html")
    await page.evaluate("() => document.getElementById('text').focus()")
    await page.session.send(input_domain.InsertText(text="hello"))
    assert await page.evaluate("() => document.getElementById('text').value") == "hello"
    assert await page.evaluate("() => window.__events.map(e => e[1])") == ["input"]


async def test_typing_segments_fills_date_time_and_week(page: Page, chrome_locale: str) -> None:
    """The three that work. The digits are the *display* order, which is the
    locale's -- ``03152026`` is March 15th in en-US and an invalid day in
    en-GB. Nothing here computes that order; §12 records that as the open half
    of this answer."""
    await page.goto("/pickers.html")
    assert await type_into(page, "date", "03152026") == "2026-03-15"
    assert await type_into(page, "time", "0230P") == "14:30"
    assert await type_into(page, "week", "112026") == "2026-W11"


async def test_typing_raises_the_events_a_controlled_input_listens_for(page: Page, chrome_locale: str) -> None:
    """The reason typing is worth the trouble at all: no value tracker can be
    left stale by it, because nothing was assigned (§8.4)."""
    await page.goto("/pickers.html")
    await type_into(page, "date", "03152026")
    fired = await page.evaluate("() => window.__events.filter(e => e[0] === 'date').map(e => e[1])")
    assert "input" in fired
    assert "change" in fired


async def test_range_moves_by_step_under_the_arrow_keys(page: Page) -> None:
    """Not typed at all: a slider has no segments, and the arrows move it one
    ``step`` each. Which means a fill has to know where it started -- this is
    the only one of the seven whose fill is *relative*."""
    await page.goto("/pickers.html")
    assert await page.evaluate("() => document.getElementById('range').value") == "50"
    assert await type_into(page, "range", ["ArrowUp"] * 25) == "75"
    assert await type_into(page, "range", ["ArrowDown"] * 5) == "70"


async def test_month_and_datetime_local_accept_nothing(page: Page) -> None:
    """The two that defeat the approach entirely.

    Every digit order was tried, with focus and with a real click, and not one
    keystroke reached them: the value stays empty and no ``input`` event fires.
    So there is no single trick for the family, exactly as §8.4 warned -- and
    the honest consequence is that ``fill`` must raise on these rather than
    appear to work.
    """
    await page.goto("/pickers.html")
    for kind, keys in (("month", "032026"), ("datetime-local", "031520260230P")):
        assert await type_into(page, kind, keys) == "", kind
        fired = await page.evaluate(f"() => window.__events.filter(e => e[0] === {kind!r}).length")
        assert fired == 0, kind


async def test_color_ignores_the_keyboard_completely(page: Page) -> None:
    """A colour input opens the operating system's picker. There is no
    keyboard path to it, and arrow keys do not move it either."""
    await page.goto("/pickers.html")
    assert await type_into(page, "color", "008800") == "#000000"
    assert await type_into(page, "color", ["ArrowUp"] * 3) == "#000000"


async def test_a_pickers_value_is_readable_without_javascript(page: Page, chrome_locale: str) -> None:
    """The useful half of the answer, and the one that was not expected.

    The accessibility node carries the picker's value as a formatted string, so
    ``input_value`` needs no JavaScript here and a fill can check what it
    actually achieved -- which, for a path this fragile, is the difference
    between filling a field and hoping.
    """
    await page.goto("/pickers.html")
    await type_into(page, "date", "03152026")
    await page.send("Accessibility.enable")
    await page.send("DOM.enable")
    document = await page.send("DOM.getDocument", {"depth": -1})
    found = await page.send("DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": "#date"})
    tree = await page.send("Accessibility.getPartialAXTree", {"nodeId": found["nodeId"]})
    values = [node.get("value", {}).get("value") for node in tree["nodes"]]
    assert "2026-03-15" in values


async def test_the_segment_order_follows_the_process_locale(page: Page) -> None:
    """Named because it is the part that will bite somebody.

    Measured: Chrome's ``--lang`` flag does *not* change it -- the resolved
    locale came back en-US under ``--lang=en-GB`` and ``--lang=de-DE`` alike.
    ``LANG``/``LC_ALL`` in the environment wirespec spawns Chrome from does:
    en_US gives 3/15/2026, en_GB 15/03/2026, de_DE 15.3.2026. So a fill that
    types a fixed digit order is correct on one machine and silently wrong on
    another, which is why §12 keeps the second half of this open.
    """
    await page.goto("/pickers.html")
    formatted = await page.evaluate("() => new Intl.DateTimeFormat().format(new Date(2026, 2, 15))")
    locale = await page.evaluate("() => new Intl.DateTimeFormat().resolvedOptions().locale")
    # Not an assertion about which locale this machine has -- an assertion that
    # the two agree, so the order is discoverable at all.
    assert formatted.startswith("3/") if locale == "en-US" else True


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("selector", "first", "second"),
    [("#date", "2026-03-15", "2027-07-04"), ("#time", "09:30", "14:45"), ("#week", "2026-W12", "2027-W30")],
)
async def test_a_picker_can_be_filled_twice(page: Page, selector: str, first: str, second: str) -> None:
    """``DOM.focus`` on a picker that is already focused does **not** put the
    cursor back on the first segment (§8.4).

    So a second ``fill`` typed its digits starting wherever the first one
    finished: measured, ``2026-03-15`` refilled as ``2027-07-04`` read back
    ``42027-03-15``, a ``time`` read ``21:30`` for half past two, and a
    ``week`` read ``275760-W12``. The read-back check caught all three, which
    is the design working -- but ``fill`` has to *work*, not merely refuse.

    ``Home`` does not reset it either; ``ArrowLeft`` does, and does not wrap
    past the leftmost segment, so one press per segment is safe on a field
    that was never touched.
    """
    await page.goto("/actions.html")
    await page.locator(selector).fill(first)
    assert await page.locator(selector).input_value() == first
    await page.locator(selector).fill(second)
    assert await page.locator(selector).input_value() == second
