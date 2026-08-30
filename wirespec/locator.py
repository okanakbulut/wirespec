"""A locator: a description of elements, and the things you can ask of them.

Nothing here touches the page until something is read. Building a locator
appends a step to an immutable list, so ``page.locator("ul").locator("li")`` is
two values and no round trips, and the whole chain is resolved against the live
document each time it is used (§3.1).

Re-resolving on every use is the reason the validating suite contains no
explicit waits: the alternative -- acquiring a handle and acting on it -- fails
twice against a React application, because handles detach on every re-render and
acquiring one costs a round trip before anything can happen.
"""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wirespec.actionable import Verdict, actionable
from wirespec.cdp import accessibility as ax_domain
from wirespec.cdp import dom as dom_domain
from wirespec.cdp import domsnapshot as snapshot_domain
from wirespec.cdp import input as input_domain
from wirespec.errors import CDPError
from wirespec.pickers import PICKER_TYPES, PickerError, fill_picker
from wirespec.rendered import STYLES, text_by_backend_id
from wirespec.resolve import text_contents
from wirespec.retry import POLL_INTERVAL, poll
from wirespec.selects import select_option
from wirespec.sentinels import NO_ARGUMENT
from wirespec.steps import Attribute, Css, Frame, Label, Matcher, Nth, Or, Role, Step, Text, TextFilter
from wirespec.timeouts import DEFAULT_ACTION_TIMEOUT, DEFAULT_ASSERTION_TIMEOUT

if TYPE_CHECKING:
    from wirespec.page import Page

__all__ = ["TEST_ID_ATTRIBUTE", "FrameLocator", "Locator"]

#: What ``get_by_test_id`` looks at. Playwright's default, and configurable
#: there; a constant here until something needs it to be otherwise.
TEST_ID_ATTRIBUTE = "data-testid"


class Locator:
    """Elements named by a chain of steps, resolved only when used."""

    __slots__ = ("chain", "page")

    def __init__(self, page: Page, chain: tuple[Step, ...] = ()) -> None:
        self.page = page
        self.chain = chain

    def __repr__(self) -> str:
        return " -> ".join(str(step) for step in self.chain) or "locator(<nothing>)"

    def _then(self, step: Step) -> Locator:
        return Locator(self.page, (*self.chain, step))

    # -- building ------------------------------------------------------------

    def locator(self, selector: str) -> Locator:
        return self._then(Css(selector))

    def get_by_role(self, role: str, *, name: Matcher | None = None, exact: bool = False) -> Locator:
        return self._then(Role(role, name, exact))

    def get_by_text(self, text: Matcher, *, exact: bool = False) -> Locator:
        return self._then(Text(text, exact))

    def get_by_label(self, text: Matcher, *, exact: bool = False) -> Locator:
        return self._then(Label(text, exact))

    def get_by_placeholder(self, text: Matcher, *, exact: bool = False) -> Locator:
        return self._then(Attribute("placeholder", text, exact, "placeholder"))

    def get_by_test_id(self, value: Matcher, *, exact: bool = True) -> Locator:
        """``exact=True`` by default, unlike every other query here: a test id is
        an identifier somebody chose, not prose, and ``row-1`` matching
        ``row-12`` is a bug waiting for the twelfth row to exist."""
        return self._then(Attribute(TEST_ID_ATTRIBUTE, value, exact, "test_id"))

    def filter(self, *, has_text: Matcher | None = None, has_not_text: Matcher | None = None) -> Locator:
        chained = self
        if has_text is not None:
            chained = chained._then(TextFilter(has_text, negate=False))
        if has_not_text is not None:
            chained = chained._then(TextFilter(has_not_text, negate=True))
        return chained

    def nth(self, index: int) -> Locator:
        return self._then(Nth(index))

    @property
    def first(self) -> Locator:
        return self.nth(0)

    @property
    def last(self) -> Locator:
        return self.nth(-1)

    def or_(self, other: Locator) -> Locator:
        """Either this or that -- how a spec waits for one of two outcomes."""
        return self._then(Or(other.chain))

    def frame_locator(self, selector: str) -> FrameLocator:
        """The document inside an ``<iframe>`` matched within this locator."""
        return FrameLocator(self.page, (*self.chain, Css(selector), Frame()))

    @property
    def content_frame(self) -> FrameLocator:
        """The document inside the ``<iframe>`` this locator already names.

        Playwright's newer spelling, and the one that composes: any locator
        that reaches an ``<iframe>`` -- by role, by test id, by anything -- can
        be stepped into, rather than only a CSS selector.
        """
        return FrameLocator(self.page, (*self.chain, Frame()))

    # -- reading -------------------------------------------------------------

    async def count(self) -> int:
        """How many elements this currently matches. Does not wait: a count of
        zero is an answer, not a failure."""
        return len(await self.page.resolve(self.chain))

    async def all(self) -> list[Locator]:
        """One locator per current match, each pinned to its index.

        Each is still a description, not a handle: it re-resolves the whole
        chain when used, so a re-render between this call and the next does not
        leave anything stale (§3.1).
        """
        return [self.nth(index) for index in range(await self.count())]

    async def one(self, *, timeout: float = DEFAULT_ASSERTION_TIMEOUT) -> int:
        """Wait until this names exactly one element, and return its node id.

        Readers that need exactly one element wait for exactly one
        (§6.3). Returning "no such element" the instant it does not
        exist would make every page object carry its own sleep -- and *two*
        matches is a spec bug, reported here rather than as whichever of the two
        happened to be first (§5.3).
        """
        found = await poll(
            self.page,
            lambda: self.page.resolve(self.chain),
            lambda ids: len(ids) == 1,
            lambda ids: f"{self!r} should match exactly one element, but matched {'none' if not ids else len(ids)}",
            timeout,
        )
        return found[0]

    # -- reading the text ----------------------------------------------------

    async def text_content(self, *, timeout: float = DEFAULT_ASSERTION_TIMEOUT) -> str:
        """The element's ``textContent``, minus the tags whose text is not on
        the screen (§8.3)."""
        return (await self._text_contents([await self.one(timeout=timeout)]))[0]

    async def all_text_contents(self) -> list[str]:
        return await self._text_contents(await self.page.resolve(self.chain))

    async def _text_contents(self, node_ids: list[int]) -> list[str]:
        return await text_contents(self.page, node_ids)

    async def inner_text(self, *, timeout: float = DEFAULT_ASSERTION_TIMEOUT) -> str:
        """The element's *rendered* text.

        Not ``element.innerText`` -- wirespec has no JavaScript to evaluate that
        with -- but a reconstruction from ``DOMSnapshot`` that agrees with it on
        sixteen of seventeen measured cases. §8.11 names the
        seventeenth.
        """
        return (await self._inner_texts([await self.one(timeout=timeout)]))[0]

    async def all_inner_texts(self) -> list[str]:
        return await self._inner_texts(await self.page.resolve(self.chain))

    async def _inner_texts(self, node_ids: list[int]) -> list[str]:
        if not node_ids:
            return []
        backends = await self.page.backend_ids(node_ids)
        # One snapshot answers for all of them, which is what makes reading two
        # hundred rows cost what reading one costs.
        snapshot = await self.page.session.send(snapshot_domain.CaptureSnapshot(computed_styles=list(STYLES)))
        found = text_by_backend_id(snapshot, set(backends))
        return [found.get(backend, "") for backend in backends]

    # -- reading the state ---------------------------------------------------

    async def get_attribute(self, name: str, *, timeout: float = DEFAULT_ASSERTION_TIMEOUT) -> str | None:
        node_id = await self.one(timeout=timeout)
        reply = await self.page.session.send(dom_domain.GetAttributes(node_id=node_id))
        return dict(zip(reply.attributes[0::2], reply.attributes[1::2], strict=True)).get(name)

    async def input_value(self, *, timeout: float = DEFAULT_ASSERTION_TIMEOUT) -> str:
        """The control's value, as the accessibility tree reports it.

        Which is how a picker input's value is readable at all without
        JavaScript: a ``<input type="date">`` comes back as ``2026-03-15``
        (§8.4).
        """
        node_id = await self.one(timeout=timeout)
        return await self.page.control_value(node_id) or ""

    async def is_checked(self, *, timeout: float = DEFAULT_ASSERTION_TIMEOUT) -> bool:
        """From the AX node's own property, which arrived with the role and cost
        nothing extra (§5.2)."""
        return await self._ax_property(await self.one(timeout=timeout), "checked") in ("true", "mixed")

    async def is_enabled(self, *, timeout: float = DEFAULT_ASSERTION_TIMEOUT) -> bool:
        return await self._ax_property(await self.one(timeout=timeout), "disabled") != "true"

    async def is_editable(self, *, timeout: float = DEFAULT_ASSERTION_TIMEOUT) -> bool:
        node_id = await self.one(timeout=timeout)
        if await self._ax_property(node_id, "disabled") == "true":
            return False
        return await self._ax_property(node_id, "readonly") != "true"

    async def is_focused(self, *, timeout: float = DEFAULT_ASSERTION_TIMEOUT) -> bool:
        return await self._ax_property(await self.one(timeout=timeout), "focused") == "true"

    async def is_visible(self) -> bool:
        """Does not wait, and does not raise on nothing: "is it visible" about
        an element that is not there has an answer, and the answer is no."""
        found = await self.page.resolve(self.chain)
        if len(found) != 1:
            return False
        return await self.page.is_visible(found[0])

    async def bounding_box(self, *, timeout: float = DEFAULT_ASSERTION_TIMEOUT) -> dict[str, float] | None:
        """The **border** box, in viewport coordinates.

        Border, not content: ``getBoundingClientRect`` is the border box, every
        spec's mental model is built on that, and a padded button's content box
        can sit tens of pixels from where the button looks (§8.9).
        """
        node_id = await self.one(timeout=timeout)
        try:
            model = (await self.page.session.send(dom_domain.GetBoxModel(node_id=node_id))).model
        except CDPError:
            # Chrome refuses a box for an element that is not rendered. That is
            # an answer -- "it has no box" -- and Playwright spells it None.
            return None
        quad = model.border
        xs, ys = quad[0::2], quad[1::2]
        return {"x": min(xs), "y": min(ys), "width": max(xs) - min(xs), "height": max(ys) - min(ys)}

    async def _ax_node(self, node_id: int) -> ax_domain.AXNode:
        reply = await self.page.session.send(ax_domain.GetPartialAXTree(node_id=node_id, fetch_relatives=False))
        if not reply.nodes:
            raise LookupError(f"{self!r} has no accessibility node")
        return reply.nodes[0]

    async def _ax_property(self, node_id: int, name: str) -> str:
        node = await self._ax_node(node_id)
        for item in node.properties or ():
            if item.name == name:
                return str(item.value.value).lower()
        return ""

    # -- acting --------------------------------------------------------------

    async def _actionable(
        self,
        *,
        enabled: bool = True,
        editable: bool = False,
        force: bool = False,
        timeout: float = DEFAULT_ACTION_TIMEOUT,
    ) -> Verdict:
        """Wait until this is one visible, settled, reachable element.

        The refusal, not a bare timeout, is what the message carries: "covered
        at 40,600 by something else" and "is disabled" send someone to two very
        different places (§5.2).
        """
        return await poll(
            self.page,
            lambda: actionable(self, enabled=enabled, editable=editable, force=force),
            lambda verdict: verdict.ok,
            lambda verdict: verdict.refusal or f"{self!r} did not become actionable",
            timeout,
        )

    async def click(
        self,
        *,
        button: str = "left",
        click_count: int = 1,
        force: bool = False,
        timeout: float = DEFAULT_ACTION_TIMEOUT,
    ) -> None:
        with self.page.acting("click", self):
            verdict = await self._actionable(force=force, timeout=timeout)
            assert verdict.point is not None
            await self.page.mouse.click(*verdict.point, button=button, click_count=click_count)

    async def dblclick(self, *, force: bool = False, timeout: float = DEFAULT_ACTION_TIMEOUT) -> None:
        with self.page.acting("dblclick", self):
            verdict = await self._actionable(force=force, timeout=timeout)
            assert verdict.point is not None
            await self.page.mouse.dblclick(*verdict.point)

    async def hover(self, *, force: bool = False, timeout: float = DEFAULT_ACTION_TIMEOUT) -> None:
        """``enabled=False``: hovering a disabled control is a thing a person
        can do, and a spec checking a disabled button's tooltip has to."""
        with self.page.acting("hover", self):
            verdict = await self._actionable(enabled=False, force=force, timeout=timeout)
            assert verdict.point is not None
            await self.page.mouse.move(*verdict.point)

    async def scroll_into_view_if_needed(self, *, timeout: float = DEFAULT_ACTION_TIMEOUT) -> None:
        with self.page.acting("scroll_into_view_if_needed", self):
            node_id = await self.one(timeout=timeout)
            await self.page.session.send(dom_domain.ScrollIntoViewIfNeeded(node_id=node_id))

    async def focus(self, *, timeout: float = DEFAULT_ACTION_TIMEOUT) -> None:
        with self.page.acting("focus", self):
            node_id = await self.one(timeout=timeout)
            await self.page.session.send(dom_domain.Focus(node_id=node_id))

    async def press(self, key: str, *, timeout: float = DEFAULT_ACTION_TIMEOUT) -> None:
        with self.page.acting("press", self):
            await self.focus(timeout=timeout)
            await self.page.keyboard.press(key)

    async def type(self, text: str, *, timeout: float = DEFAULT_ACTION_TIMEOUT) -> None:
        """Type into this element, character by character.

        Which ``fill`` does not: a combobox filtering as you type is watching
        the keystrokes, not the value (§6.5).
        """
        with self.page.acting("type", self):
            await self.focus(timeout=timeout)
            await self.page.keyboard.type(text)

    async def fill(self, value: str, *, timeout: float = DEFAULT_ACTION_TIMEOUT) -> None:
        """Replace the field's contents.

        Focus, select, and ``Input.insertText``. Assigning ``.value`` does not
        raise the ``input`` event React's controlled inputs listen for, and the
        field visibly reverts on the next render (§8.4) -- which is
        also why this cannot be done with JavaScript wirespec does not have.

        Picker inputs are not text fields and take a different path; three of
        them have no keyboard path at all and raise rather than appear to work.
        """
        with self.page.acting("fill", self):
            verdict = await self._actionable(editable=True, timeout=timeout)
            assert verdict.node_id is not None
            kind = await self.page.input_type(verdict.node_id)
            if kind in PICKER_TYPES:
                await self._fill_picker(verdict.node_id, kind, value, timeout)
                return
            await self.page.session.send(dom_domain.Focus(node_id=verdict.node_id))
            await self.page.select_all(verdict.node_id)
            await self.page.session.send(input_domain.InsertText(text=value))

    async def _fill_picker(self, node_id: int, kind: str, value: str, timeout: float) -> None:
        """Type into a picker, and try again while there is time.

        Every other action retries through ``_actionable``. This one *acts*
        after that loop has finished and then checks what it achieved, so the
        check needs a loop of its own -- and it earns one: a picker's segments
        go in one at a time, so an application that re-renders the field on the
        first ``input`` event moves the ground under the remaining keystrokes,
        and the read-back finds a field half filled. Catching that is what
        §8.4 already required; retrying is what turns catching it
        into filling the field.

        A refusal that cannot change -- an unfillable type, a locale with no
        segment order -- is re-raised at once rather than waited out.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                await fill_picker(self, node_id, kind, value, max(deadline - loop.time(), 0.0))
                return
            except PickerError as refused:
                if refused.permanent or loop.time() >= deadline:
                    raise
            await asyncio.sleep(POLL_INTERVAL)
            # Resolved again, because the attempt that just failed may have
            # failed *by* the element being replaced.
            node_id = await self.one(timeout=max(deadline - loop.time(), 0.0))

    async def set_input_files(
        self,
        files: str | Path | Sequence[str | Path],
        *,
        timeout: float = DEFAULT_ACTION_TIMEOUT,
    ) -> None:
        """Attach files to an ``<input type="file">``.

        The only way to do it: a file picker is browser chrome, not page, so no
        click and no key event can reach it. An empty list clears the field, and
        clearing fires ``change`` like any other set -- a form that enables its
        submit button on that event would otherwise stay enabled with nothing
        attached.

        **Chrome reads the paths in its own process.** A missing file therefore
        comes back as a protocol error naming a node id and nothing else, so the
        check happens here, where the path is still in hand.
        """
        with self.page.acting("set_input_files", self):
            paths = [files] if isinstance(files, str | Path) else list(files)
            if not paths:
                raise NotImplementedError(
                    "set_input_files([]) cannot clear a file input: Chrome ignores an empty file list "
                    "and leaves what was there, silently (§8.17). Reload the page, or "
                    "re-create the input, to empty it."
                )
            absolute = []
            for one in paths:
                resolved = Path(one).resolve()
                if not resolved.is_file():
                    raise FileNotFoundError(f"{one} is not a file, so there is nothing to attach")
                absolute.append(str(resolved))

            node_id = await self.one(timeout=timeout)
            if (await self.page.input_type(node_id)) != "file":
                raise ValueError(f"{self!r} is not a file input")
            await self.page.session.send(dom_domain.SetFileInputFiles(files=absolute, node_id=node_id))

    async def select_option(
        self,
        value: str | Sequence[str] | None = None,
        *,
        label: str | Sequence[str] | None = None,
        index: int | Sequence[int] | None = None,
        timeout: float = DEFAULT_ACTION_TIMEOUT,
    ) -> list[str]:
        """Choose options in a ``<select>``, by value, label or index.

        Driven through the widget rather than assigned to it -- there is no CDP
        command that sets a select and no JavaScript here to do it with. See
        ``wirespec/selects.py`` for the measurements the counting rests on, and
        for the three more a ``<select multiple>`` needs. Returns the values
        selected, in document order, as Playwright's does.

        Several at once, or an empty list to select nothing, only on a
        ``<select multiple>``; an ordinary one refuses rather than taking the
        last of them and reporting success.
        """
        with self.page.acting("select_option", self):
            return await select_option(self, value, label=label, index=index, timeout=timeout)

    async def drag_to(self, target: Locator, *, timeout: float = DEFAULT_ACTION_TIMEOUT) -> None:
        """Drag this element onto another.

        Native HTML5 drag is **not** a sequence of mouse events (
        §8.2): a ``draggable`` element is run by the browser's own drag session,
        which synthetic mouse input does not start. The way in is
        ``Input.setInterceptDrags(true)`` -- the move that *would* have begun a
        drag is reported back as ``Input.dragIntercepted`` carrying the
        ``dataTransfer`` payload, and from then on the gesture is driven with
        ``Input.dispatchDragEvent``.
        """
        with self.page.acting("drag_to", self):
            from wirespec.dragging import drag

            source = await self._actionable(timeout=timeout)
            landing = await target._actionable(timeout=timeout)
            assert source.point is not None and landing.point is not None
            await drag(self.page, source.point, landing.point)

        # -- the caller's own JavaScript -----------------------------------------

    async def evaluate(
        self, expression: str, arg: Any = NO_ARGUMENT, *, timeout: float = DEFAULT_ASSERTION_TIMEOUT
    ) -> Any:
        """Run the caller's function against this element.

        ``Runtime.callFunctionOn`` passes the handle as ``this``, not as an
        argument, and specs write ``node => ...`` -- so the expression is
        wrapped to hand ``this`` in as the first argument (§8.9).
        ``arg`` follows it, which is where Playwright puts it.
        """
        node_id = await self.one(timeout=timeout)
        handle = await self.page.handle_for(node_id)
        return await self.page.call_on(handle, expression, arg)

    async def evaluate_all(self, expression: str) -> Any:
        """Run one function over the whole match array, by reference.

        Reading an attribute off two hundred rows costs what reading it off one
        costs, because the array never crosses the wire (§6.3).
        """
        found = await self.page.resolve(self.chain)
        handles = await asyncio.gather(*(self.page.handle_for(node_id) for node_id in found))
        return await self.page.call_on_all(handles, expression)


class FrameLocator:
    """A document inside a frame, as somewhere to start a query.

    Not a `Locator`: it names a document, and a document is not an element, so
    nothing here reads, asserts or clicks. It builds -- and what it builds is an
    ordinary `Locator` whose chain happens to change document part way along
    (§8.19). That is the whole of the frame support: no second
    session, no second node-id space, no `Frame` object with a life of its own
    to fall out of step with the page.

    Nothing is resolved here either. ``page.frame_locator("#a").locator("b")``
    is two values and no round trips, exactly like a locator chain, and the
    frame is looked up again on every use -- so a frame that reloads between two
    assertions is simply found again.
    """

    __slots__ = ("chain", "page")

    def __init__(self, page: Page, chain: tuple[Step, ...]) -> None:
        self.page = page
        self.chain = chain

    def __repr__(self) -> str:
        return " -> ".join(str(step) for step in self.chain)

    def _then(self, step: Step) -> Locator:
        return Locator(self.page, (*self.chain, step))

    def locator(self, selector: str) -> Locator:
        return self._then(Css(selector))

    def get_by_role(self, role: str, *, name: Matcher | None = None, exact: bool = False) -> Locator:
        return self._then(Role(role, name, exact))

    def get_by_text(self, text: Matcher, *, exact: bool = False) -> Locator:
        return self._then(Text(text, exact))

    def get_by_label(self, text: Matcher, *, exact: bool = False) -> Locator:
        return self._then(Label(text, exact))

    def get_by_placeholder(self, text: Matcher, *, exact: bool = False) -> Locator:
        return self._then(Attribute("placeholder", text, exact, "placeholder"))

    def get_by_test_id(self, value: Matcher, *, exact: bool = True) -> Locator:
        return self._then(Attribute(TEST_ID_ATTRIBUTE, value, exact, "test_id"))

    def frame_locator(self, selector: str) -> FrameLocator:
        """A frame inside this frame. Nesting is just a longer chain."""
        return FrameLocator(self.page, (*self.chain, Css(selector), Frame()))

    @property
    def owner(self) -> Locator:
        """The ``<iframe>`` element itself, in the document that holds it.

        The way back out, and the only reason this class keeps the whole chain
        rather than just the part after the frame: dropping the trailing `Frame`
        step is what "the element this frame lives in" means.
        """
        return Locator(self.page, self.chain[:-1])
