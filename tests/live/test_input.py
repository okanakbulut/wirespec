"""``Input`` — real mouse and keyboard events, and native HTML5 drag.

Two traps live here, and both cost real time to find (§8.1, §8.2):
a move during a drag must name the held ``button`` and not only the ``buttons``
mask, and a ``draggable`` element is run by the browser's own drag session,
which synthetic mouse input never starts.
"""

import pytest

from tests.live.support import drain_until, evaluate, eventually, goto, handle_for
from wirespec.cdp import dom
from wirespec.cdp import input as input_domain
from wirespec.connection import Session

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: CDP's modifier mask. Alt 1, Ctrl 2, Meta 4, Shift 8.
SHIFT = 8


async def centre_of(session: Session, selector: str) -> tuple[float, float]:
    """Where to aim, in viewport coordinates -- which is what Input wants and
    what ``getBoxModel`` returns."""
    box = await session.send(
        dom.GetBoxModel(object_id=await handle_for(session, f"document.querySelector('{selector}')"))
    )
    quad = box.model.border
    return (quad[0] + quad[4]) / 2, (quad[1] + quad[5]) / 2


async def click(session: Session, x: float, y: float, *, button: str = "left", count: int = 1) -> None:
    """Press and release. Chrome wants ``buttons`` as a mask on the press and
    zero on the release, and gets confused if the release still claims the
    button is down."""
    mask = {"left": 1, "right": 2, "middle": 4}[button]
    await session.send(
        input_domain.DispatchMouseEvent(type="mousePressed", x=x, y=y, button=button, buttons=mask, click_count=count)
    )
    await session.send(
        input_domain.DispatchMouseEvent(type="mouseReleased", x=x, y=y, button=button, buttons=0, click_count=count)
    )


async def test_a_left_click_reaches_the_page(live: Session, site: str) -> None:
    """Input.dispatchMouseEvent. A trusted event, indistinguishable from a real
    one -- which ``element.click()`` from script is not."""
    await goto(live, f"{site}/interactions.html")
    x, y = await centre_of(live, "#button")
    await click(live, x, y)
    clicks = await evaluate(live, "window.__clicks")
    assert len(clicks) == 1
    assert clicks[0]["button"] == 0
    assert clicks[0]["detail"] == 1
    assert clicks[0]["x"] == pytest.approx(x, abs=2)


async def test_a_double_click_carries_its_count(live: Session, site: str) -> None:
    """``click_count`` is what makes two clicks a double click. Sending two
    single clicks produces two separate ones, and ``dblclick`` never fires."""
    await goto(live, f"{site}/interactions.html")
    x, y = await centre_of(live, "#button")
    await click(live, x, y, count=1)
    await click(live, x, y, count=2)
    clicks = await evaluate(live, "window.__clicks")
    assert [entry["detail"] for entry in clicks] == [1, 2]


async def test_a_right_click_is_a_different_button(live: Session, site: str) -> None:
    await goto(live, f"{site}/interactions.html")
    x, y = await centre_of(live, "#button")
    await click(live, x, y, button="right")
    clicks = await evaluate(live, "window.__clicks")
    assert clicks[-1]["button"] == 2
    assert clicks[-1]["context"] is True


async def test_a_wheel_event_scrolls_the_container_under_the_pointer(live: Session, site: str) -> None:
    """``mouseWheel`` with ``delta_y``. Scrolls whatever is under the cursor,
    which is how a scroll container is driven rather than the window."""
    await goto(live, f"{site}/interactions.html")
    x, y = await centre_of(live, "#scroller")
    assert await evaluate(live, "document.getElementById('scroller').scrollTop") == 0
    await live.send(input_domain.DispatchMouseEvent(type="mouseWheel", x=x, y=y, delta_x=0.0, delta_y=240.0))
    # Wheel scrolling is handled on the compositor and lands after the command
    # is acknowledged, so this is polled rather than asserted outright.
    await eventually(lambda: evaluate(live, "document.getElementById('scroller').scrollTop > 0"), True)


async def test_insert_text_types_into_the_focused_field(live: Session, site: str) -> None:
    """Input.insertText. Composed input, the right tool for ordinary fields and
    the wrong one for ``<input type="date">`` and its relatives, which are
    segmented pickers that keep only the segment that had focus."""
    await goto(live, f"{site}/interactions.html")
    await evaluate(live, "document.getElementById('field').focus()")
    await live.send(input_domain.InsertText(text="hello wirespec"))
    assert await evaluate(live, "document.getElementById('field').value") == "hello wirespec"


async def test_a_key_event_needs_its_virtual_key_code(live: Session, site: str) -> None:
    """Input.dispatchKeyEvent.

    ``windows_virtual_key_code`` is not optional in practice: a missing or
    wrong code fails *silently*, because the event does arrive and no handler
    recognises it (§12).
    """
    await goto(live, f"{site}/interactions.html")
    await evaluate(live, "document.getElementById('field').focus()")
    for key, code, virtual in (("Enter", "Enter", 13), ("ArrowDown", "ArrowDown", 40), ("Escape", "Escape", 27)):
        for kind in ("keyDown", "keyUp"):
            await live.send(
                input_domain.DispatchKeyEvent(type=kind, key=key, code=code, windows_virtual_key_code=virtual)
            )
    assert await evaluate(live, "window.__keys") == ["Enter", "ArrowDown", "Escape"]


async def test_tab_moves_focus_the_way_it_would_for_a_user(live: Session, site: str) -> None:
    """Tab is not just another key: the browser acts on it, and focus leaves the
    element that was listening. Any test that sends Tab and then keeps
    asserting about the old element is asserting about the wrong one -- which is
    quiet, because the later keys still arrive, just somewhere else.
    """
    await goto(live, f"{site}/interactions.html")
    await evaluate(live, "document.getElementById('field').focus()")
    assert await evaluate(live, "document.activeElement.id") == "field"
    for kind in ("keyDown", "keyUp"):
        await live.send(input_domain.DispatchKeyEvent(type=kind, key="Tab", code="Tab", windows_virtual_key_code=9))
    assert await evaluate(live, "window.__keys") == ["Tab"]
    assert await evaluate(live, "document.activeElement.id") != "field"


async def test_a_modifier_reaches_the_page_as_a_modifier(live: Session, site: str) -> None:
    """The ``modifiers`` mask, which is what a keyboard shortcut is made of."""
    await goto(live, f"{site}/interactions.html")
    await evaluate(live, "document.getElementById('field').focus()")
    await live.send(
        input_domain.DispatchKeyEvent(
            type="keyDown", key="A", code="KeyA", text="A", windows_virtual_key_code=65, modifiers=SHIFT
        )
    )
    await live.send(
        input_domain.DispatchKeyEvent(type="keyUp", key="A", code="KeyA", windows_virtual_key_code=65, modifiers=SHIFT)
    )
    recorded = await evaluate(live, "window.__modifiers")
    assert recorded[-1] == {"key": "A", "shift": True, "ctrl": False, "alt": False, "meta": False}


async def test_typing_a_character_key_puts_text_in_the_field(live: Session, site: str) -> None:
    """A ``char``-bearing keyDown is what actually inserts text through the
    keyboard path, as opposed to ``insertText`` which bypasses it."""
    await goto(live, f"{site}/interactions.html")
    await evaluate(live, "document.getElementById('field').focus()")
    for character in "hi":
        await live.send(
            input_domain.DispatchKeyEvent(
                type="keyDown",
                key=character,
                code=f"Key{character.upper()}",
                text=character,
                unmodified_text=character,
                windows_virtual_key_code=ord(character.upper()),
            )
        )
        await live.send(
            input_domain.DispatchKeyEvent(
                type="keyUp",
                key=character,
                code=f"Key{character.upper()}",
                windows_virtual_key_code=ord(character.upper()),
            )
        )
    assert await evaluate(live, "document.getElementById('field').value") == "hi"


async def test_a_native_drag_is_intercepted_and_then_performed(live: Session, site: str) -> None:
    """Input.setInterceptDrags, Input.dragIntercepted and Input.dispatchDragEvent.

    A ``draggable`` element is run by the browser's own drag session, which
    synthetic mouse events never start. With interception on, Chrome hands back
    the drag it *would* have begun, and the driver performs it as drag events.

    Two things here are load-bearing and neither is obvious:

    * the move must name ``button="left"`` as well as the ``buttons`` mask, or
      the drag controller never recognises the gesture and nothing is
      intercepted (§8.1);
    * releasing is a ``drop``, never a ``mouseReleased``: the browser consumed
      the button when it took the drag over (§8.2).
    """
    await goto(live, f"{site}/drag.html")
    await live.send(input_domain.SetInterceptDrags(enabled=True))
    try:
        start_x, start_y = await centre_of(live, "#card")
        end_x, end_y = await centre_of(live, "#bin")

        with live.queue(input_domain.DragIntercepted) as intercepted:
            await live.send(
                input_domain.DispatchMouseEvent(
                    type="mousePressed", x=start_x, y=start_y, button="left", buttons=1, click_count=1
                )
            )
            # button= on a *move*, not only buttons=. Without it there is no
            # drag, and the failure surfaces four steps later as an element
            # that never appeared.
            await live.send(
                input_domain.DispatchMouseEvent(
                    type="mouseMoved", x=start_x + 40, y=start_y + 10, button="left", buttons=1
                )
            )
            drag = await drain_until(intercepted, lambda event: True, timeout=15.0)

        data = drag.data
        assert data.drag_operations_mask != 0
        assert any(item.mime_type == "text/plain" and item.data == "card-1" for item in data.items), data.items

        # dragEnter before dragOver, in that order, or the drop target never
        # learns the drag arrived.
        for kind in ("dragEnter", "dragOver", "drop"):
            await live.send(input_domain.DispatchDragEvent(type=kind, x=end_x, y=end_y, data=data))

        assert await evaluate(live, "window.__dropped") == "card-1"
        assert await evaluate(live, "document.getElementById('bin').textContent") == "dropped: card-1"
    finally:
        await live.send(input_domain.SetInterceptDrags(enabled=False))


async def test_an_html_drag_item_carries_its_base_url(live: Session, site: str) -> None:
    """``DragDataItem.base_url`` is spelled ``baseURL`` on the wire, not
    ``baseUrl`` as the camel rename would have it.

    This has its own test because the failure is silent in a way the rest of
    the suite cannot see. The field is optional, so a wrong name costs no
    decode error and no missing event -- just a value that is permanently None,
    on an item most drags do not carry at all. Only a drag with a ``text/html``
    item makes Chrome send it.
    """
    await goto(live, f"{site}/drag.html")
    await live.send(input_domain.SetInterceptDrags(enabled=True))
    try:
        start_x, start_y = await centre_of(live, "#card")
        with live.queue(input_domain.DragIntercepted) as intercepted:
            await live.send(
                input_domain.DispatchMouseEvent(
                    type="mousePressed", x=start_x, y=start_y, button="left", buttons=1, click_count=1
                )
            )
            await live.send(
                input_domain.DispatchMouseEvent(
                    type="mouseMoved", x=start_x + 40, y=start_y + 10, button="left", buttons=1
                )
            )
            drag = await drain_until(intercepted, lambda event: True, timeout=15.0)
    finally:
        await live.send(input_domain.SetInterceptDrags(enabled=False))

    html = next((item for item in drag.data.items if item.mime_type == "text/html"), None)
    assert html is not None, [item.mime_type for item in drag.data.items]
    assert html.data == "<b>card-1</b>"
    assert html.base_url is not None, "baseURL did not decode -- check the field's wire name"


async def test_a_cancelled_drag_leaves_the_page_alone(live: Session, site: str) -> None:
    """``dragCancel`` is the fourth drag event type, and the one a test needs
    when it is checking that an abandoned drag changes nothing."""
    await goto(live, f"{site}/drag.html")
    await live.send(input_domain.SetInterceptDrags(enabled=True))
    try:
        start_x, start_y = await centre_of(live, "#card")
        end_x, end_y = await centre_of(live, "#bin")
        with live.queue(input_domain.DragIntercepted) as intercepted:
            await live.send(
                input_domain.DispatchMouseEvent(
                    type="mousePressed", x=start_x, y=start_y, button="left", buttons=1, click_count=1
                )
            )
            await live.send(
                input_domain.DispatchMouseEvent(
                    type="mouseMoved", x=start_x + 40, y=start_y + 10, button="left", buttons=1
                )
            )
            drag = await drain_until(intercepted, lambda event: True, timeout=15.0)

        await live.send(input_domain.DispatchDragEvent(type="dragEnter", x=end_x, y=end_y, data=drag.data))
        await live.send(input_domain.DispatchDragEvent(type="dragCancel", x=end_x, y=end_y, data=drag.data))
        assert await evaluate(live, "window.__dropped") is None
    finally:
        await live.send(input_domain.SetInterceptDrags(enabled=False))
