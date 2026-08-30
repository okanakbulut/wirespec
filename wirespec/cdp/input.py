"""``Input`` — real mouse and keyboard events, and native HTML5 drag.

Two traps live here and both cost real time to find (§8.1, §8.2):
a move event during a drag must name the held ``button``, not only the
``buttons`` mask; and a ``draggable`` element is run by the browser's own drag
session, which synthetic mouse input never starts.
"""

from typing import ClassVar

from msgspec import field

from wirespec.cdp.base import CDPStruct, Command, Event

__all__ = [
    "DispatchDragEvent",
    "DispatchKeyEvent",
    "DispatchMouseEvent",
    "DragData",
    "DragDataItem",
    "DragIntercepted",
    "InsertText",
    "SetInterceptDrags",
]


class DragDataItem(CDPStruct):
    mime_type: str
    data: str
    title: str | None = None
    #: "baseURL" on the wire, not "baseUrl". Optional, so getting this wrong
    #: costs a silently absent value rather than a failed decode -- which is
    #: worse, not better.
    base_url: str | None = field(default=None, name="baseURL")


class DragData(CDPStruct):
    items: list[DragDataItem]
    drag_operations_mask: int
    files: list[str] | None = None


class DispatchMouseEvent(Command[None]):
    """``type`` is ``mousePressed``, ``mouseReleased``, ``mouseMoved`` or
    ``mouseWheel``.

    Set ``button="left"`` on the *moves* of a drag as well as on the press.
    Without it Chrome's drag controller never recognises the gesture,
    ``Input.dragIntercepted`` never fires, and there is no drag to drive -- with
    the failure surfacing four steps later as an element that never appeared
    (§8.1).
    """

    __method__: ClassVar[str] = "Input.dispatchMouseEvent"

    type: str
    x: float
    y: float
    button: str | None = None
    buttons: int | None = None
    click_count: int | None = None
    modifiers: int = 0
    delta_x: float | None = None
    delta_y: float | None = None
    pointer_type: str | None = None


class DispatchKeyEvent(Command[None]):
    """``type`` is ``keyDown``, ``keyUp``, ``rawKeyDown`` or ``char``.

    ``windows_virtual_key_code`` is not optional in practice: a wrong or missing
    key code fails silently, because the event does arrive and no handler
    recognises it (§12).
    """

    __method__: ClassVar[str] = "Input.dispatchKeyEvent"

    type: str
    key: str | None = None
    code: str | None = None
    text: str | None = None
    unmodified_text: str | None = None
    windows_virtual_key_code: int | None = None
    native_virtual_key_code: int | None = None
    modifiers: int = 0
    auto_repeat: bool = False
    is_keypad: bool = False
    is_system_key: bool = False
    location: int | None = None


class InsertText(Command[None]):
    """Types text as if composed. The right tool for ordinary fields and the
    wrong one for ``<input type="date">`` and its relatives, which are segmented
    pickers and silently keep only the segment that had focus
    (§8.4)."""

    __method__: ClassVar[str] = "Input.insertText"

    text: str


class SetInterceptDrags(Command[None]):
    __method__: ClassVar[str] = "Input.setInterceptDrags"

    enabled: bool


class DispatchDragEvent(Command[None]):
    """``type`` is ``dragEnter``, ``dragOver``, ``drop`` or ``dragCancel``.

    ``dragEnter`` before ``dragOver``, in that order, or the drop target never
    learns the drag arrived. Releasing during a drag is a ``drop``, never a
    ``mouseReleased``: the browser consumed the button when it took the drag
    over (§8.2).
    """

    __method__: ClassVar[str] = "Input.dispatchDragEvent"

    type: str
    x: float
    y: float
    data: DragData
    modifiers: int = 0


class DragIntercepted(Event):
    """The move that *would* have started a native drag, handed back with its
    ``dataTransfer`` payload instead of being performed."""

    __method__: ClassVar[str] = "Input.dragIntercepted"

    data: DragData
