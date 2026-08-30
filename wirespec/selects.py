"""``select_option``, driven the way a person drives a ``<select>``.

There is **no CDP command that sets a select's value**, and there is no
JavaScript here to do it with (§3.1). What there is, is the widget
itself: focus it, press Home, and press ArrowDown until the wanted option is
under the cursor. Chrome does the rest, and fires ``input`` and ``change`` on
the way -- which a value assigned from outside would not, so an application
listening for ``change`` hears this and would not hear the other.

Three measurements shape the counting, and each of them is an off-by-one waiting
to happen (§8.16):

* **Arrow keys skip disabled options.** From ``Apple`` in
  ``Apple/Banana/Cherry(disabled)/Damson``, two presses land on ``Damson`` --
  DOM index 3. Counting DOM positions stops one short, on ``Cherry``, and
  reports success.
* **Chrome starts on the first *enabled* option, and ``Home`` goes there too**,
  not to index zero. Every "Choose one…" placeholder is a disabled first
  option, so a count from zero is wrong on the commonest select there is.
* **Optgroups do not count.** Indices are flat across them and arrow keys cross
  without stopping.

A ``<select multiple>`` is a list box rather than a dropdown, and needs three
more measurements:

* **``Ctrl+ArrowDown`` moves the cursor without changing the selection**, and
  skips disabled options exactly as the plain arrows do. Plain ``ArrowDown``
  *replaces* the selection, which is why the walk cannot use it.
* **``Ctrl+Space`` toggles the option under the cursor**, both ways, and fires
  ``input`` then ``change``.
* **``Home`` still selects the first enabled option and clears the rest.** That
  is what makes the walk deterministic: whatever the page or an earlier test
  left selected, the cursor is at a known place and exactly one thing is on.

The selection is read back through the accessibility tree, which reports the
selected option's *label* -- the same route a picker input's value comes back
by (§8.4). That route gives one value and a list box can hold
several, so a multiple select is read back per option instead: each ``<option>``
carries a ``selected`` property of its own.
"""

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING

from wirespec.cdp import dom as dom_domain

if TYPE_CHECKING:
    from wirespec.locator import Locator

__all__ = ["Option", "options_of", "select_option"]


class Option:
    """One ``<option>``: what it is worth, what it says, and whether it can be
    chosen."""

    __slots__ = ("disabled", "label", "node_id", "value")

    def __init__(self, value: str, label: str, disabled: bool, node_id: int) -> None:
        self.value = value
        self.label = label
        self.disabled = disabled
        #: Kept so a list box can be read back option by option. A single
        #: select answers through the control itself and never needs it.
        self.node_id = node_id

    def __repr__(self) -> str:
        return f"<Option {self.value!r}{' disabled' if self.disabled else ''}>"


async def options_of(select: Locator) -> list[Option]:
    """Every option in document order, read with no JavaScript.

    One ``getAttributes`` per option and one batched text read for all of them.
    """
    found = select.locator("option")
    node_ids = await select.page.resolve(found.chain)
    if not node_ids:
        return []
    attributes, labels = await asyncio.gather(
        asyncio.gather(*(select.page.session.send(dom_domain.GetAttributes(node_id=one)) for one in node_ids)),
        # The batched read is the point: one snapshot for every option.
        found._text_contents(node_ids),
    )
    options = []
    for node_id, reply, label in zip(node_ids, attributes, labels, strict=True):
        pairs = dict(zip(reply.attributes[::2], reply.attributes[1::2], strict=False))
        text = label.strip()
        # HTML's own rule: an option with no `value` attribute is worth its own
        # text. Treating the absence as an empty string makes every select
        # written that way unselectable, and the error would name the value the
        # caller asked for rather than the reason.
        options.append(Option(pairs.get("value", text), text, "disabled" in pairs, node_id))
    return options


def _requested(
    value: str | Sequence[str] | None,
    label: str | Sequence[str] | None,
    index: int | Sequence[int] | None,
) -> tuple[str, list[str] | list[int]]:
    """Which way the caller asked, and for how many.

    Exactly one of the three, because letting one win silently is how a spec
    that says ``select_option("a", label="Banana")`` gets Apple and never finds
    out. A single value and a list of one mean the same thing here; the
    difference that matters is a list of *several*, and the empty list, which
    is a request to select nothing rather than a mistake.
    """
    given = [name for name, asked in (("value", value), ("label", label), ("index", index)) if asked is not None]
    if not given:
        raise TypeError("select_option needs one of value, label or index")
    if len(given) > 1:
        raise TypeError(f"select_option takes one of value, label or index, not {' and '.join(given)}")
    if index is not None:
        return "index", [index] if isinstance(index, int) else list(index)
    if value is not None:
        return "value", [value] if isinstance(value, str) else list(value)
    return "label", [label] if isinstance(label, str) else list(label)  # type: ignore[arg-type]


def _wanted(options: list[Option], how: str, asked: str | int) -> Option:
    """Which option was asked for, or a message saying what was there instead."""
    if how == "index":
        assert isinstance(asked, int)
        if not 0 <= asked < len(options):
            raise LookupError(f"no option at index {asked}; there are {len(options)}")
        chosen = options[asked]
    else:
        match = (lambda option: option.value == asked) if how == "value" else (lambda o: o.label == asked)
        chosen = next((option for option in options if match(option)), None)
        if chosen is None:
            # Both spellings, always. A caller who searched by value and gets
            # back a list of values learns nothing they did not already know;
            # the labels are what they can see on the page, and the pairing is
            # what shows a select whose values are "1", "2", "3".
            offered = ", ".join(f"{option.value!r} ({option.label})" for option in options)
            raise LookupError(f"no option with {how} {asked!r}; has {offered}")
    if chosen.disabled:
        raise LookupError(f"the option {chosen.value!r} is disabled and cannot be selected")
    return chosen


async def select_option(
    select: Locator,
    value: str | Sequence[str] | None = None,
    *,
    label: str | Sequence[str] | None = None,
    index: int | Sequence[int] | None = None,
    timeout: float,
) -> list[str]:
    """Choose the options asked for, and confirm they were chosen."""
    how, asked = _requested(value, label, index)

    node_id = await select.one(timeout=timeout)
    attributes = await select.page.session.send(dom_domain.GetAttributes(node_id=node_id))
    multiple = "multiple" in attributes.attributes[::2]

    options = await options_of(select)
    if not options:
        raise LookupError(f"{select!r} has no options")
    chosen = [_wanted(options, how, one) for one in asked]
    if not multiple and len(chosen) != 1:
        raise NotImplementedError(
            f"{select!r} holds one option and was asked for {len(chosen)}. Only a <select multiple> "
            f"can hold several or none; this one refuses rather than selecting the last of them and "
            f"reporting success."
        )

    # Positions among the **enabled** options, because that is what the arrow
    # keys count and `Home` starts at.
    reachable = [option for option in options if not option.disabled]
    steps = [reachable.index(one) for one in chosen]

    await select.focus(timeout=timeout)
    if multiple:
        await _walk(select, sorted(steps))
    else:
        await select.page.keyboard.press("Home")
        for _ in range(steps[0]):
            await select.page.keyboard.press("ArrowDown")

    # Read it back. Nothing here can be assumed to have worked: a select the
    # keyboard could not reach leaves the old value in place and would
    # otherwise report the value it was asked for (§1, goal 4).
    if multiple:
        landed = await _selected(select, options)
        if landed != [one.value for one in sorted(chosen, key=options.index)]:
            raise AssertionError(
                f"{select!r} was asked for {[one.value for one in chosen]!r} and shows {landed!r}. "
                f"The walk covered {len(reachable)} enabled options."
            )
        return landed
    landed_label = await select.page.control_value(await select.one(timeout=timeout))
    if landed_label != chosen[0].label:
        raise AssertionError(
            f"{select!r} was asked for {chosen[0].value!r} ({chosen[0].label!r}) and shows "
            f"{landed_label!r}. Arrow keys reached {steps[0]} of {len(reachable)} enabled options."
        )
    return [chosen[0].value]


async def _walk(select: Locator, steps: list[int]) -> None:
    """Toggle exactly the wanted positions in a list box, in one pass.

    ``Home`` leaves position 0 selected and everything else clear, so the walk
    starts from a known state whatever was selected before -- and position 0 is
    the one case that needs *un*-toggling rather than toggling.
    """
    keyboard = select.page.keyboard
    await keyboard.press("Home")
    if 0 not in steps:
        await keyboard.press("Space", modifiers=["Control"])
    cursor = 0
    for step in steps:
        if step == 0:
            continue
        # Control held, or the arrow replaces the whole selection with whatever
        # it lands on and every option toggled so far is lost.
        for _ in range(step - cursor):
            await keyboard.press("ArrowDown", modifiers=["Control"])
        cursor = step
        await keyboard.press("Space", modifiers=["Control"])


async def _selected(select: Locator, options: list[Option]) -> list[str]:
    """What a list box currently holds, in document order.

    Per option, because the control's own accessible value is ``None`` on a
    multiple select -- measured -- and there is no JavaScript here to ask the
    page with.
    """
    properties = await asyncio.gather(*(select.page.ax_properties(one.node_id) for one in options))
    return [one.value for one, props in zip(options, properties, strict=True) if props.get("selected") == "true"]
