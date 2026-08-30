"""Mouse and keyboard: real input events, dispatched by Chrome.

``Mouse`` is separate from ``Locator`` on purpose (§6.5). A drag is
not something done *to* an element: it starts on one, ends on another, the walk
between them is the point, and a spec may need to stop mid-gesture with the
button still down. A locator-shaped API cannot express any of that.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from wirespec.cdp import input as input_domain

if TYPE_CHECKING:
    from wirespec.connection import Session

__all__ = ["KEYS", "MODIFIERS", "Keyboard", "Mouse"]

#: Which bit each button contributes to the ``buttons`` bitmask, and what CDP
#: calls it. Chrome wants both spellings and disagrees with itself if they do
#: not match.
_BUTTONS = {"left": 1, "middle": 4, "right": 2, "back": 8, "forward": 16}

#: The named keys wirespec knows: the Windows virtual key code Chrome needs,
#: then ``key`` and ``code`` as the page will see them.
#:
#: **A table rather than a computed map**, and deliberately (§12):
#: a wrong ``keyCode`` fails silently, because the event does arrive and no
#: handler recognises it. There is no way to compute these correctly for the
#: general case, so the ones that are known are written down and everything else
#: raises.
#:
#: ``key`` and ``code`` are spelled out separately rather than shared, because
#: they are not the same thing and ``Space`` is where that stops being
#: academic: its ``code`` is ``"Space"`` and its ``key`` is a literal space. A
#: page testing ``event.key === " "`` sees nothing if the two are conflated,
#: and sees nothing *quietly*.
KEYS: dict[str, tuple[int, str, str]] = {
    "Enter": (0x0D, "Enter", "Enter"),
    "Escape": (0x1B, "Escape", "Escape"),
    "Tab": (0x09, "Tab", "Tab"),
    "Backspace": (0x08, "Backspace", "Backspace"),
    "ArrowDown": (0x28, "ArrowDown", "ArrowDown"),
    "ArrowUp": (0x26, "ArrowUp", "ArrowUp"),
    "ArrowLeft": (0x25, "ArrowLeft", "ArrowLeft"),
    "ArrowRight": (0x27, "ArrowRight", "ArrowRight"),
    # Home and End arrived with `select_option`, which needs a known starting
    # point before it can count (wirespec/selects.py). Measured: on a focused
    # `<select>`, Home moves to the first **enabled** option -- not to index
    # zero, which is the off-by-one every "Choose one…" placeholder would cause.
    "Home": (0x24, "Home", "Home"),
    "End": (0x23, "End", "End"),
    # Space arrived with `<select multiple>`: held with Control it toggles the
    # focused option, which is the only gesture that selects a second one
    # without dropping the first (§8.16).
    "Space": (0x20, " ", "Space"),
}

#: CDP's modifier bitmask, which is one number rather than a set of flags.
#: Chrome's own order, and not guessable: ``Meta`` is 4 and ``Shift`` is 8.
MODIFIERS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}

#: The text Chrome should insert for a named key, where it inserts anything.
#: ``Enter`` types a newline; the arrows and Escape type nothing, and sending
#: text for them puts stray characters in the field.
_KEY_TEXT = {"Enter": "\r", "Tab": "\t", "Space": " "}


class Mouse:
    """The pointer, in viewport coordinates."""

    __slots__ = ("_buttons", "_session", "_x", "_y")

    def __init__(self, session: Session) -> None:
        self._session = session
        self._x = 0.0
        self._y = 0.0
        #: Which buttons are currently held. Kept here because Chrome needs it
        #: on every event, including moves, and a driver that forgets it during
        #: a drag produces a gesture the page never recognises (§8.1).
        self._buttons = 0

    @property
    def position(self) -> tuple[float, float]:
        return (self._x, self._y)

    async def move(self, x: float, y: float, *, steps: int = 1) -> None:
        """Walk the pointer to a point.

        ``steps`` matters more than it looks: a page watching ``mousemove`` sees
        a jump as one event and a walk as many, and hover menus and drag
        libraries are written against the walk.
        """
        start_x, start_y = self._x, self._y
        for step in range(1, max(steps, 1) + 1):
            fraction = step / max(steps, 1)
            await self._dispatch(
                "mouseMoved",
                start_x + (x - start_x) * fraction,
                start_y + (y - start_y) * fraction,
            )

    async def down(self, *, button: str = "left", click_count: int = 1) -> None:
        self._buttons |= _button_bit(button)
        await self._dispatch("mousePressed", self._x, self._y, button=button, click_count=click_count)

    async def up(self, *, button: str = "left", click_count: int = 1) -> None:
        self._buttons &= ~_button_bit(button)
        await self._dispatch("mouseReleased", self._x, self._y, button=button, click_count=click_count)

    async def click(self, x: float, y: float, *, button: str = "left", click_count: int = 1) -> None:
        await self.move(x, y)
        await self.down(button=button, click_count=click_count)
        await self.up(button=button, click_count=click_count)

    async def dblclick(self, x: float, y: float, *, button: str = "left") -> None:
        """Two presses at one point with a **rising** click count.

        That is what makes it a double click rather than two clicks that happen
        to be adjacent: the ``detail`` the page sees has to reach 2, or a
        ``dblclick`` handler never fires.
        """
        await self.move(x, y)
        for count in (1, 2):
            await self.down(button=button, click_count=count)
            await self.up(button=button, click_count=count)

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await self._dispatch("mouseWheel", self._x, self._y, delta_x=delta_x, delta_y=delta_y)

    async def _dispatch(
        self,
        kind: str,
        x: float,
        y: float,
        *,
        button: str | None = None,
        click_count: int | None = None,
        delta_x: float | None = None,
        delta_y: float | None = None,
    ) -> None:
        self._x, self._y = x, y
        # The held button is named on *moves* as well as on presses. Without it
        # Chrome's drag controller never recognises the gesture, and the failure
        # surfaces four steps later as an element that never appeared
        # (§8.1).
        if button is None:
            button = _held(self._buttons) or "none"
        await self._session.send(
            input_domain.DispatchMouseEvent(
                type=kind,
                x=x,
                y=y,
                button=button,
                buttons=self._buttons,
                click_count=click_count,
                delta_x=delta_x,
                delta_y=delta_y,
            )
        )


class Keyboard:
    """Keystrokes, as the page would see them from a person."""

    __slots__ = ("_session",)

    def __init__(self, session: Session) -> None:
        self._session = session

    async def press(self, key: str, *, modifiers: Sequence[str] = ()) -> None:
        """One named key, down and up, optionally with modifiers held.

        Raises on a key that is not in ``KEYS``. That is the whole point: an
        unmapped key would otherwise be dispatched with a zero key code, the
        event would arrive, the handler that was supposed to see it would not
        recognise it, and nothing would fail (§12). The same
        applies to a modifier name: ``"Ctrl"`` is not ``"Control"``, and a
        mask that silently stayed zero is a keystroke without the modifier --
        which for ``Ctrl+Space`` on a list box means *replacing* the selection
        instead of adding to it.

        Playwright's ``"Control+Space"`` spelling is not parsed here; it is not
        in ``KEYS``, so it raises rather than being read as a key named
        ``Control+Space``.
        """
        if key not in KEYS:
            known = ", ".join(KEYS)
            raise NotImplementedError(
                f"press({key!r}) is not supported: wirespec's key table is {known}. "
                f"A key code guessed wrongly arrives as an event no handler recognises, "
                f"and nothing says so -- so unknown keys raise (§12)."
            )
        mask = 0
        for name in modifiers:
            if name not in MODIFIERS:
                raise NotImplementedError(
                    f"press({key!r}, modifiers={list(modifiers)!r}): {name!r} is not a modifier; "
                    f"wirespec knows {', '.join(MODIFIERS)}."
                )
            mask |= MODIFIERS[name]
        code, name, physical = KEYS[key]
        text = _KEY_TEXT.get(key)
        # No text under Control, Alt or Meta. Chrome does not produce any for a
        # chord, and sending it anyway makes `press("Enter", modifiers=["Control"])`
        # insert a newline as well as firing the shortcut. Shift is not in that
        # set: it is part of ordinary typing.
        if mask & (MODIFIERS["Control"] | MODIFIERS["Alt"] | MODIFIERS["Meta"]):
            text = None
        for kind in ("keyDown", "keyUp"):
            await self._session.send(
                input_domain.DispatchKeyEvent(
                    type=kind,
                    key=name,
                    code=physical,
                    text=text if kind == "keyDown" else None,
                    unmodified_text=text if kind == "keyDown" else None,
                    windows_virtual_key_code=code,
                    native_virtual_key_code=code,
                    modifiers=mask,
                )
            )

    async def type(self, text: str) -> None:
        """Type text one character at a time.

        Which ``fill`` does not, and the difference matters for anything
        watching keystrokes rather than the value: a combobox filtering as you
        type, a shortcut handler (§6.5).
        """
        for character in text:
            await self._character(character)

    async def _character(self, character: str) -> None:
        """One printable character.

        Chrome derives the key code from ``text`` for an ordinary character, so
        there is no table to be wrong here -- which is exactly why ``press``
        needs one and this does not.
        """
        code = _key_code_for(character)
        for kind in ("keyDown", "keyUp"):
            await self._session.send(
                input_domain.DispatchKeyEvent(
                    type=kind,
                    key=character,
                    text=character if kind == "keyDown" else None,
                    unmodified_text=character if kind == "keyDown" else None,
                    windows_virtual_key_code=code,
                    native_virtual_key_code=code,
                )
            )


def _key_code_for(character: str) -> int:
    """A best-effort virtual key code for a printable character.

    Correct for letters and digits, which is what a spec types; anything else
    gets 0, and Chrome uses ``text`` instead. That is safe here in a way it is
    not in ``press``, because the character *is* the payload -- a wrong code
    cannot make the wrong thing appear in the field.
    """
    if character.isalpha() and character.isascii():
        return ord(character.upper())
    if character.isdigit() and character.isascii():
        return ord(character)
    return 0


def _button_bit(button: str) -> int:
    if button not in _BUTTONS:
        raise ValueError(f"unknown mouse button {button!r}; wirespec knows {', '.join(_BUTTONS)}")
    return _BUTTONS[button]


def _held(buttons: int) -> str | None:
    for name, bit in _BUTTONS.items():
        if buttons & bit:
            return name
    return None
