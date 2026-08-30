"""Filling ``<input type="date">`` and its six relatives.

These are segmented pickers, not text fields. ``Input.insertText`` does not
partly work on them -- measured on Chrome 150, it sets **nothing at all** -- so
a spec that fills a date bound this way keeps its default and fails somewhere
else entirely (§8.4).

The replacement is to do what a person does: focus the field and type the
segments. Measured across all seven types, that works for four of them:

| type | how |
|---|---|
| ``date`` | digits, in the widget's segment order |
| ``time`` | digits plus an AM/PM letter |
| ``week`` | digits |
| ``range`` | arrow keys, one ``step`` each -- the only **relative** fill here |
| ``month``, ``datetime-local`` | **nothing works.** Every digit order was tried, with focus and with a real click, and not one keystroke arrives |
| ``color`` | opens the operating system's picker; there is no keyboard path |

The three that cannot be filled raise, naming the type. Appearing to work is the
one outcome worse than not supporting it.

**The segment order is the browser's UI locale, and wirespec pins it.** Measured
three ways: Chrome's ``--lang`` flag does not change it, and
``Emulation.setLocaleOverride`` does not change it either -- that one moves the
page's ``Intl`` and leaves the widget alone. What does change it is ``LANG`` /
``LC_ALL`` in the environment Chrome was spawned from: ``en_US`` gives
MM/DD/YYYY, ``en_GB`` and ``de_DE`` give DD/MM/YYYY.

So ``Browser.launch`` sets that environment (``locale=`` on the launch), and
this module knows the order for what it set. The two being independent is what
makes it safe: a spec that needs the *page* in another locale still has
``Emulation.setLocaleOverride``, and the widget stays where wirespec put it.
"""

import asyncio
import re
from typing import TYPE_CHECKING

from wirespec.cdp import dom as dom_domain
from wirespec.errors import NODE_GONE, CDPError, WirespecError, WirespecTimeoutError
from wirespec.retry import POLL_INTERVAL, poll

if TYPE_CHECKING:
    from wirespec.locator import Locator

__all__ = ["PICKER_TYPES", "UNFILLABLE", "fill_picker"]

#: Every type §8.4 names.
PICKER_TYPES = frozenset({"date", "datetime-local", "month", "time", "week", "color", "range"})

#: The three with no keyboard path at all, and what to say about each.
UNFILLABLE = {
    "month": "no digit order reaches its segments -- every one was tried, with focus and with a real click",
    "datetime-local": "no digit order reaches its segments -- every one was tried, with focus and with a real click",
    "color": "it opens the operating system's colour picker, which has no keyboard path",
}

#: How each fillable type's value is spelled as keystrokes, per UI locale.
#: Only the locales whose order has actually been measured are here; anything
#: else raises rather than typing a guessed order, because a wrong date that
#: looks like a right one is the failure this whole module exists to avoid.
_ORDERS: dict[str, dict[str, tuple[str, ...]]] = {
    "en-US": {
        "date": ("month", "day", "year"),
        "time": ("hour12", "minute", "meridiem"),
        "week": ("week", "year"),
    },
}

_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_TIME = re.compile(r"^(\d{2}):(\d{2})$")
_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")


class PickerError(WirespecError):
    """A picker input could not be filled, and why.

    ``permanent`` says whether trying again could possibly help. An
    ``<input type=month>`` has no keyboard path in any locale and a locale with
    no segment order written down will not acquire one by waiting; a fill that
    typed and then read back wrong might simply have been outrun by the page.
    """

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        self.permanent = permanent
        super().__init__(message)


class _Field:
    """The picker's node id, resolved again whenever the page replaces it.

    A picker is filled over a dozen round trips, and the page gets to run
    between every pair of them: the application that re-renders the field on
    its first ``input`` event leaves the id this fill started with naming
    nothing (§8.23). Every command here goes through one of these
    two methods so that "the field moved" is answered once, here, rather than
    surfacing as a protocol error from whichever round trip happened to be next.
    """

    def __init__(self, locator: Locator, node_id: int, timeout: float) -> None:
        self.page = locator.page
        self._locator = locator
        self._node = node_id
        self._timeout = timeout

    def __repr__(self) -> str:
        return repr(self._locator)

    async def _again(self) -> int:
        self._node = await self._locator.one(timeout=self._timeout)
        return self._node

    async def at(self, order: tuple[str, ...], name: str) -> None:
        """Put the cursor on one segment, from wherever it happened to be.

        ``DOM.focus`` on a picker that is *already* focused leaves the cursor
        where the last fill left it, so a second fill types its digits from the
        middle: measured, a date refilled from ``2026-03-15`` to ``2027-07-04``
        read back ``42027-03-15``, a time read ``21:30`` for half past two, and a
        week read ``275760-W12`` (§8.4). ``Home`` does not reset it.
        ``ArrowLeft`` does, and does not wrap, so one press per segment reaches
        the leftmost from anywhere; ``ArrowRight`` counts along to the one wanted.
        """
        page = self.page
        try:
            await page.session.send(dom_domain.Focus(node_id=self._node))
        except CDPError as exc:
            if NODE_GONE not in exc.message:
                raise
            await page.session.send(dom_domain.Focus(node_id=await self._again()))
        for _ in order:
            await page.keyboard.press("ArrowLeft")
        for _ in range(order.index(name)):
            await page.keyboard.press("ArrowRight")

    async def value(self) -> str:
        """What the field reads now, which the accessibility node carries.

        ``""`` for a picker with an unset segment, which is also what
        ``control_value`` answers for an element that has no value at all --
        the two cannot be confused here, because this only ever wraps an input.
        """
        page = self.page
        try:
            return await page.control_value(self._node) or ""
        except CDPError as exc:
            if NODE_GONE not in exc.message:
                raise
            return await page.control_value(await self._again()) or ""


async def fill_picker(locator: Locator, node_id: int, kind: str, value: str, timeout: float) -> None:
    """Fill one picker, or raise naming the type and the reason."""
    page = locator.page
    if kind in UNFILLABLE:
        raise PickerError(
            f"{locator!r} is <input type={kind!r}>, which wirespec cannot fill: {UNFILLABLE[kind]}. "
            f"Set it through the application instead, or assert on it without setting it "
            f"(§8.4).",
            permanent=True,
        )
    if kind == "range":
        await _fill_range(locator, node_id, value)
        return

    locale = page.context.browser.locale_tag
    order = _ORDERS.get(locale, {}).get(kind)
    if order is None:
        known = ", ".join(sorted(_ORDERS))
        raise PickerError(
            f"{locator!r} is <input type={kind!r}> and wirespec has no segment order for the "
            f"browser locale {locale!r} (it knows {known}). Typing a guessed order would set a "
            f"different date and look like it worked (§8.4).",
            permanent=True,
        )

    segments = _segments(kind, value)
    missing = [name for name in order if name not in segments]
    if missing:
        raise PickerError(f"cannot spell {value!r} for a {kind} field: no {', '.join(missing)} in it")
    last = _last_segment(kind, segments)
    field = _Field(locator, node_id, timeout)
    deadline = asyncio.get_running_loop().time() + timeout

    # Empty the segment that goes in last, so that the field has no value at all
    # while the rest of it is filled in. That is the whole trick, and
    # §8.24 is why: a picker has no value until every segment is
    # set, so an application only sees an `input` event -- and only re-renders,
    # and only writes its own idea of the value back over the field -- once the
    # last segment lands. Everything before that is invisible to the page.
    await field.at(order, last)
    await field.page.keyboard.press("Backspace")

    for name in order:
        if name == last:
            continue
        await field.at(order, name)
        for character in segments[name]:
            # Digits and the AM/PM letter go the same way: as typed characters,
            # where Chrome derives the key code from the text itself.
            await field.page.keyboard.type(character)

    digits = segments[last]
    if len(digits) == 1 or digits.startswith("0"):
        # This one can be typed, because none of its keystrokes completes the
        # value until the last: a leading `0` is not a month, a day or a week on
        # its own, and the meridiem is a single letter. What the page does after
        # that is its own business -- there is nothing left to interrupt.
        await field.at(order, last)
        for character in digits:
            await field.page.keyboard.type(character)
    else:
        # This one cannot: `1` already spells January, so a December typed as
        # `12` is complete after its first digit, and the `2` lands in whatever
        # the page put there in the meantime -- measured, the second digit of a
        # 31st went into the *month* and turned December into January. The
        # arrows do it instead, one aimed press at a time (§8.24).
        await _walk(field, order, last, int(digits), deadline)

    settled = await _settled(field, value, max(deadline - asyncio.get_running_loop().time(), 0.0))
    if settled != value:
        raise PickerError(
            f"{locator!r} was filled for {value!r} but reads {settled!r}. "
            f"That usually means the browser's segment order is not {locale!r}'s."
        )


def _last_segment(kind: str, segments: dict[str, str]) -> str:
    """Which segment to fill last: the only one the page ever sees being filled.

    Everything comes down to what an arrow walk of it passes through. The walk
    goes *up* from empty, and every value below one that exists exists too --
    but only within a segment. Walking the month with the 29th, 30th or 31st set
    passes February, where the date has no value at all, and an application
    handed an empty value writes that emptiness straight back over everything
    else that was typed. So those days are walked instead, up a range where
    every step on the way is a real date.
    """
    if kind == "time":
        return "meridiem"
    if kind == "week":
        return "week"
    return "day" if int(segments["day"]) > 28 else "month"


async def _walk(field: _Field, order: tuple[str, ...], last: str, wanted: int, deadline: float) -> None:
    """Step one segment up from empty to the value it is meant to hold.

    An empty segment under ``ArrowUp`` starts at a measured place -- a month, a
    day and a week at ``01`` -- so the count is exact rather than searched for.

    Aimed again before every press, and *waited on* after every press. The press
    that gives the field a value is the one the page reacts to, and what a
    controlled field does then is assign its own value over the top: measured on
    the pilot application's builder, that empties every segment for one render and puts the
    value back on the next. A press sent into that window moves a segment that
    is about to be overwritten, so each one waits for the page to come back
    before the next is sent.
    """
    for _ in range(wanted):
        before = await field.value()
        await field.at(order, last)
        await field.page.keyboard.press("ArrowUp")
        remaining = max(deadline - asyncio.get_running_loop().time(), 0.0)
        try:
            await poll(
                field.page,
                field.value,
                lambda now, was=before: bool(now) and now != was,
                lambda now: f"a press of ArrowUp left the field reading {now!r}",
                remaining,
            )
        except WirespecTimeoutError as stalled:
            raise PickerError(f"{field!r} did not move under ArrowUp: {stalled}") from stalled


async def _settled(field: _Field, value: str, timeout: float) -> str:
    """Read the field back until it says ``value``, or the time runs out.

    Not a single read, because a read is not a fence. Measured on the pilot
    suite: ``Accessibility.getPartialAXTree`` -- and ``Runtime.evaluate`` the
    same way -- is answered from the renderer *before* keystrokes dispatched
    ahead of it have been processed, so the value read straight after typing is
    routinely the one from before (§8.25). A single read there does
    not just fail a fill that worked: it reports the value the caller asked for
    while the field has already moved one past it.

    The common case is still one round trip, because the read comes first.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        settled = await field.value()
        if settled == value or loop.time() >= deadline:
            return settled
        await asyncio.sleep(POLL_INTERVAL)


def _segments(kind: str, value: str) -> dict[str, str]:
    if kind == "date":
        matched = _DATE.match(value)
        if not matched:
            raise PickerError(f"a date field wants YYYY-MM-DD, not {value!r}")
        year, month, day = matched.groups()
        return {"year": year, "month": month, "day": day}
    if kind == "time":
        matched = _TIME.match(value)
        if not matched:
            raise PickerError(f"a time field wants HH:MM, not {value!r}")
        hour, minute = int(matched.group(1)), matched.group(2)
        # The en-US widget is twelve-hour with an AM/PM segment, so 14:30 is
        # typed as 02, 30, P -- not as 14, 30.
        twelve = hour % 12 or 12
        return {"hour12": f"{twelve:02d}", "minute": minute, "meridiem": "P" if hour >= 12 else "A"}
    if kind == "week":
        matched = _WEEK.match(value)
        if not matched:
            raise PickerError(f"a week field wants YYYY-Www, not {value!r}")
        year, week = matched.groups()
        return {"year": year, "week": week}
    raise PickerError(f"no keystroke spelling for a {kind} field")


#: More presses than this and something is wrong with the step, or the caller
#: means a slider with ten thousand positions -- either way it is not a fill.
_MAX_PRESSES = 200


async def _fill_range(locator: Locator, node_id: int, value: str) -> None:
    """A slider has no segments. The arrows move it one ``step`` each, which
    makes this the only **relative** fill of the seven -- it has to know where
    the slider started."""
    page = locator.page
    attributes = await page.session.send(dom_domain.GetAttributes(node_id=node_id))
    pairs = dict(zip(attributes.attributes[0::2], attributes.attributes[1::2], strict=True))
    step = _number(pairs.get("step"), 1.0)
    try:
        wanted = float(value)
    except ValueError:
        raise PickerError(f"a range field wants a number, not {value!r}") from None
    current = _number(await page.control_value(node_id), 0.0)

    presses = round((wanted - current) / step) if step else 0
    if abs(presses) > _MAX_PRESSES:
        raise PickerError(
            f"{locator!r} would need {abs(presses)} arrow presses to go from {current:g} to {wanted:g} "
            f"in steps of {step:g}, which is not a fill. Widen the step, or set it another way."
        )
    await page.session.send(dom_domain.Focus(node_id=node_id))
    key = "ArrowUp" if presses > 0 else "ArrowDown"
    for _ in range(abs(presses)):
        await page.keyboard.press(key)

    settled = await page.control_value(node_id)
    # A sentinel the slider cannot be at, so "unreadable" and "at the wrong
    # place" both fail rather than one of them passing by coincidence.
    if _number(settled, wanted - 1) != wanted:
        raise PickerError(f"{locator!r} was moved to {settled!r} rather than {value!r}")


def _number(text: str | None, fallback: float) -> float:
    """A number, or the fallback. Never ``None`` -- a slider always has a
    position, even when the field has not been touched."""
    if not text:
        return fallback
    try:
        return float(text)
    except ValueError:
        return fallback
