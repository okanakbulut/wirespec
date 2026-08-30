"""One page: navigation, evaluation, and the escape hatch.

A ``Page`` is a flat session (§3.3) with the things a spec does to
a tab wrapped around it.
"""

import asyncio
import contextlib
import inspect
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from wirespec.api import APIRequestContext
from wirespec.cdp import accessibility as ax_domain
from wirespec.cdp import animation as animation_domain
from wirespec.cdp import css as css_domain
from wirespec.cdp import dom as dom_domain
from wirespec.cdp import emulation as emulation_domain
from wirespec.cdp import input as input_domain
from wirespec.cdp import network as network_domain
from wirespec.cdp import page as page_domain
from wirespec.cdp import runtime as runtime_domain
from wirespec.cdp import target as target_domain
from wirespec.connection import Session
from wirespec.dialogs import Dialog
from wirespec.errors import (
    CDPError,
    JavaScriptError,
    NavigationError,
    PageClosedError,
    WirespecError,
    WirespecTimeoutError,
)
from wirespec.input import Keyboard, Mouse
from wirespec.locator import FrameLocator, Locator
from wirespec.markup import INVISIBLE_TAGS
from wirespec.network import Handler, Request, Response, Router
from wirespec.resolve import resolve
from wirespec.retry import poll
from wirespec.sentinels import NO_ARGUMENT
from wirespec.steps import Matcher, Step
from wirespec.timeouts import DEFAULT_ACTION_TIMEOUT

if TYPE_CHECKING:
    from wirespec.browser import BrowserContext

__all__ = ["Page"]

#: Roles whose elements have a value even when it is empty, so that "" and "no
#: value at all" stay distinguishable.
#: A document deeper than this is a cycle, and walking it forever would hang
#: the hit test rather than failing it.
_MAX_DEPTH = 512

#: Every state `wait_for_selector` knows, in the order Playwright documents
#: them. Named once so the refusal message cannot drift from the check.
_SELECTOR_STATES = ("attached", "detached", "visible", "hidden")

#: How many requests to remember, so a response can name its request's method.
#: A long-running page makes a great many and none of this is worth a leak.
_REQUEST_MEMORY = 2048

#: Transitions that repaint an element without moving it or changing what it
#: covers. A transition of one of these cannot change which element is at a
#: point, so an action has nothing to wait for -- and these are the ones
#: applications run constantly: Tailwind's `transition-colors` is on every
#: button, input and row in the pilot application, and waiting out each one cost
#: that suite 37% of its wall clock before this list existed (§8.32).
#:
#: An allowlist rather than a list of the properties that *do* move things,
#: because the two directions fail differently: an unknown property treated as
#: paint-only is a press at coordinates something has left, and an unknown
#: property treated as motion is a few milliseconds. `opacity` is here on
#: purpose -- a fully transparent element still takes the press, so fading one
#: in or out changes nothing about reachability. `filter` and `backdrop-filter`
#: likewise: a blur changes what an element looks like and not where it is, and
#: a filtered element is still hit-tested over its own box. Going from or to
#: `none` does make a containing block for fixed-position descendants, which can
#: move them -- but it moves them in one frame rather than sweeping them, and a
#: jump is already at the point when the hit test asks. What waiting protects
#: against is a gradual arrival inside the few milliseconds after it.
#: `visibility` is deliberately *not*: a `visibility: hidden` element is not
#: hit-tested, so a transition to or from it does change the answer. Nor is anything that resizes
#: a box -- `border-*-width`, `padding-*`, `width` -- even though the effect is
#: often only visual: a resize can rewrap content and move a sibling.
_PAINT_ONLY = frozenset(
    {
        "accent-color",
        "backdrop-filter",
        "background",
        "background-color",
        "background-image",
        "background-position",
        "background-position-x",
        "background-position-y",
        "background-size",
        "border-block-color",
        "border-block-end-color",
        "border-block-start-color",
        "border-bottom-color",
        "border-color",
        "border-inline-color",
        "border-inline-end-color",
        "border-inline-start-color",
        "border-left-color",
        "border-right-color",
        "border-top-color",
        "box-shadow",
        "caret-color",
        "color",
        "column-rule-color",
        "fill",
        "filter",
        "flood-color",
        "lighting-color",
        "opacity",
        "outline-color",
        "stop-color",
        "stroke",
        "text-decoration-color",
        "text-emphasis-color",
        "text-shadow",
        "-webkit-text-fill-color",
        "-webkit-text-stroke-color",
    }
)


def _moves(animation: animation_domain.Animation) -> bool:
    """Could this animation change what is at a point?

    Only a ``CSSTransition`` can be answered cheaply: its ``name`` is the
    property in transition, so the question is a set lookup and costs nothing.
    A ``CSSAnimation``'s name is its keyframes rule and a ``WebAnimation``'s is
    whatever the caller passed, and neither says what is being animated without
    reading the keyframes -- which is the largest part of the message and is not
    parsed (``wirespec/cdp/animation.py``). Those are assumed to move, which is
    the safe direction and costs nothing in practice: the ones that run
    indefinitely are excluded by ``finite_movers`` anyway.
    """
    if animation.type != "CSSTransition":
        return True
    return animation.name not in _PAINT_ONLY


#: How many animations to hold before sweeping the ones that have finished.
#: Chrome sends nothing when an animation merely ends (§8.28), so
#: the expired entries are only ever dropped by somebody looking -- and an
#: application that animates a row on every render would otherwise accumulate
#: them for the life of the page even if nothing ever asked.
_ANIMATION_MEMORY = 256


class _Caught[T]:
    """What an ``expect_*`` block hands back: the event, in wirespec's own
    shape rather than the protocol's."""

    __slots__ = ("_future", "_map")

    def __init__(self, future, mapper) -> None:
        self._future = future
        self._map = mapper

    def result(self) -> T:
        return self._map(self._future.result())


#: Roles whose elements have a value even when it is empty, so that "" and "no
#: value at all" stay distinguishable.
_VALUED_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton", "slider", "Date", "DateTime"})


def _index(
    node: dom_domain.Node,
    backends: dict[int, int],
    parents: dict[int, int],
    invisible: set[int],
    ancestors: dict[int, int],
    parent: int | None = None,
    parent_node: int | None = None,
) -> bool:
    """Fill the id maps from one ``getDocument`` reply; say whether it held a
    framed document.

    The return value is what lets the text query stay cheap. ``performSearch``
    searches every document in the target, frames included (
    §8.19), so its results have to be scoped -- but scoping costs a round trip,
    and the overwhelming majority of pages have no frame to leak from. This is
    the free answer to "could anything have leaked", read off a reply the page
    was fetching anyway.

    Sound in the direction that matters: a childless ``contentDocument`` is
    still a ``contentDocument``, so a frame nested inside a frame -- which this
    walk cannot see, because an unpierced ``getDocument`` stops at each frame's
    document node -- is still reported by the frame that holds it.
    """
    backends[node.node_id] = node.backend_node_id
    if parent is not None:
        parents[node.backend_node_id] = parent
    if parent_node is not None:
        # The same edge in node-id space. Kept alongside rather than derived,
        # because the two consumers want different currencies: the hit test
        # compares backend ids, and `CSS.getComputedStyleForNode` takes a node
        # id and nothing else.
        ancestors[node.node_id] = parent_node
    # Whether *this element itself* is one whose text is not on the screen
    # (§8.3). Recorded here because `nodeName` is the only place
    # an HTML `<title>` and an SVG one differ -- `TITLE` against `title` -- and
    # the serialiser writes both the same way, so a subtree read back from
    # `DOM.getOuterHTML` cannot tell them apart on its own (wirespec/markup.py).
    # The set stays tiny: an ordinary page has a handful of these.
    if node.node_name in INVISIBLE_TAGS:
        invisible.add(node.node_id)
    framed = False
    for child in node.children or ():
        framed = _index(child, backends, parents, invisible, ancestors, node.backend_node_id, node.node_id) or framed
    if node.content_document is not None:
        _index(node.content_document, backends, parents, invisible, ancestors, node.backend_node_id, node.node_id)
        framed = True
    return framed


#: Does this source text mean "run this" rather than "give me this value"?
#: §8.8 says to detect *narrowly*: an expression that merely opens
#: with a parenthesis must not be mistaken for a function, because ``(a || b).c``
#: is an expression and calling it would throw. So the parenthesised form has to
#: be a complete, paren-free parameter list followed by an arrow -- which
#: ``(null || document).title`` is not.
_FUNCTION = re.compile(
    r"""\s*(?:async\s+)?(?:
        function\b
      | \w+\s*=>
      | \([^()]*\)\s*=>
    )""",
    re.VERBOSE,
)


class Page:
    """A tab, addressed by its session id on every message."""

    def __init__(
        self,
        context: BrowserContext,
        session: Session,
        target_id: str,
        *,
        url: str,
        main_frame_id: str,
        viewport: tuple[int, int],
    ) -> None:
        self.context = context
        self.viewport = viewport
        self.session = session
        self.target_id = target_id
        #: Which frame this page *is*. Flat sessions deliver every frame's
        #: events on one session, so the main frame has to be named rather than
        #: assumed -- ``navigatedWithinDocument`` carries no parent id to test.
        self.main_frame_id = main_frame_id
        self._url = url
        self._closed = False
        #: The root node id of the current document, or None when it has to be
        #: fetched again. Owned here rather than by the resolver because
        #: whatever owns the document must own the depth (§5.1).
        self._document: int | None = None
        self._backends: dict[int, int] = {}
        #: Backend id -> its parent's backend id, for the hit test's ancestry
        #: walk. Built from the same reply, so it costs nothing extra.
        self._parents: dict[int, int] = {}
        #: Node id -> its parent's node id. The same edges as `_parents`, in
        #: the currency `CSS.getComputedStyleForNode` accepts.
        self._ancestors: dict[int, int] = {}
        #: Node ids that are themselves a `<script>`, `<style>` and so on --
        #: the tags of §8.3, as the *DOM* spells them. Read off
        #: the same reply, and what lets a subtree read back from
        #: `DOM.getOuterHTML` tell an HTML `<title>` from an SVG one.
        self._invisible: set[int] = set()
        #: Does the current document hold a frame? Read off the same reply, and
        #: the only reason a text query on a frameless page still costs one
        #: round trip instead of two (§8.19).
        self.framed = False
        #: Separate from Locator on purpose: a drag is not something done *to*
        #: an element (§6.5).
        self.mouse = Mouse(session)
        self.keyboard = Keyboard(session)
        self._router = Router(self)
        self._network_on = False
        #: What `page.on("dialog", ...)` registered. Empty is not the same as
        #: "ignore dialogs": it means the default answer applies.
        self._dialog_handlers: tuple[Callable[[Dialog], object], ...] = ()
        #: Who wants to be told what this page was asked to do. Empty on
        #: every page nobody is recording, which is what keeps `acting` free:
        #: an action pays two clock reads only when something will read them.
        self._acting: tuple[Callable[[str, str, float, float, str | None], object], ...] = ()
        #: How deep in an action this page currently is. Actions call each
        #: other -- `press` focuses first, `select_option` focuses and then
        #: presses nine keys -- and a lane with a row for every one of those is
        #: a lane nobody reads. Only the outermost is recorded.
        self._acting_depth = 0
        #: Tasks answering dialogs. Held only so the loop keeps a strong
        #: reference -- a task nobody holds can be collected mid-await, and the
        #: dialog it was about to answer would stay open for ever.
        self._answering: set[asyncio.Task[None]] = set()
        #: request id -> what went out, so a response can name its request's
        #: method. ``responseReceived`` does not carry it.
        self._requests: dict[str, Request] = {}
        #: request id -> "the body is complete". `getResponseBody` has nothing
        #: to give before this is set.
        self._complete: dict[str, asyncio.Event] = {}
        #: Animation id -> (the backend id of the element it moves, when it is
        #: due to stop, whether it can move anything). Chrome pushes both
        #: halves: `animationStarted` names the element and says how long,
        #: `animationCanceled` names only the animation -- which is why the id is
        #: the key and the node is not (§8.28). This is the whole of
        #: "is it still moving", and on a page with nothing animating it is an
        #: empty dict.
        self._animating: dict[str, tuple[int, float, bool]] = {}
        # Push, not poll: the main frame announces where it went, so `url` is
        # a property with nothing to await rather than a round trip. Both
        # events are needed -- Chrome sends navigatedWithinDocument *instead
        # of* frameNavigated for a fragment, never as well as it.
        self._unsubscribe = (
            session.on(page_domain.FrameNavigated, self._frame_navigated),
            session.on(page_domain.NavigatedWithinDocument, self._navigated_within_document),
            # Not lazy, unlike `Network` and `Fetch`: an unanswered dialog stops
            # the page, so the subscription has to predate the first thing that
            # could open one -- and that is the `goto`, which a top-level
            # `alert()` blocks before any spec has had a line to register a
            # handler on (§8.20).
            session.on(page_domain.JavascriptDialogOpening, self._dialog_opening),
            # Likewise not lazy, and for a sharper reason: these are a *record*
            # of what started moving. A page that subscribes when the first
            # action arrives has already missed the animation that was running
            # when it did, and would click a moving target (§8.28).
            session.on(animation_domain.AnimationStarted, self._animation_started),
            session.on(animation_domain.AnimationCanceled, self._animation_canceled),
        )

    def __repr__(self) -> str:
        return f"<Page {self._url}>"

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        """Close this tab. Idempotent, and it leaves the context alone."""
        if self._closed:
            return
        self._mark_closed()
        # Out of the context's list here rather than waiting for
        # `Target.targetDestroyed`: that event only arrives if something turned
        # target discovery on, and a spec counting tabs right after a close is
        # entitled to a straight answer.
        self.context.forget(self)
        await self.context.connection.send(target_domain.CloseTarget(target_id=self.target_id))

    def _mark_closed(self) -> None:
        """Stop listening, and refuse anything further.

        Separate from ``close`` because a context disposing itself takes its
        pages with it: Chrome closes them, and the Page objects have to find
        out without sending a command to a target that is already gone.
        """
        self._closed = True
        for stop in self._unsubscribe:
            stop()
        self._router.close()

    def _check_open(self) -> None:
        if self._closed:
            raise PageClosedError(f"this page was closed (it was at {self._url})")

    @property
    def url(self) -> str:
        """Where the page currently is.

        A property, and synchronous, because it is kept current by
        ``Page.frameNavigated`` and ``Page.navigatedWithinDocument`` rather than
        fetched on demand -- there is nothing to await, and Playwright spells it
        the same way.
        """
        return self._url

    async def goto(self, url: str, *, timeout: float = DEFAULT_ACTION_TIMEOUT) -> None:
        """Navigate, and wait for the page to arrive.

        The wait is pure push and involves no loop at all (§5.1):
        the subscriptions are in place before the command goes out, so a page
        that loads instantly is still caught. Which event ends the wait depends
        on the kind of navigation -- see below, and §8.12.
        """
        self._check_open()
        destination = self.resolve_url(url)
        with self.acting("goto", destination):
            loop = asyncio.get_running_loop()
            arrived: asyncio.Future[str] = loop.create_future()

            def loaded(event: page_domain.LoadEventFired) -> None:
                if not arrived.done():
                    arrived.set_result("load")

            def within(event: page_domain.NavigatedWithinDocument) -> None:
                if event.frame_id == self.main_frame_id and not arrived.done():
                    arrived.set_result("navigatedWithinDocument")

            # Two events, because Chrome sends one *or* the other and never both.
            # Measured, Chrome 150: a different-document navigation gives
            # frameNavigated then loadEventFired; a fragment or History API call
            # gives navigatedWithinDocument and no load event whatsoever. A goto
            # waiting only for load hangs for its full timeout on every anchor
            # click. Both are subscribed before the command goes out, or a page
            # that loads instantly is a wakeup nobody receives (§5.1).
            unsubscribe = (
                self.session.on(page_domain.LoadEventFired, loaded),
                self.session.on(page_domain.NavigatedWithinDocument, within),
            )
            try:
                # The deadline covers the *command* as well as the wait. Measured:
                # Page.navigate does not reply until the navigation has committed
                # or failed, so a server that accepts a connection and answers
                # nothing holds the reply for as long as it likes -- and a timeout
                # wrapped around only the wait would be a timeout the caller asked
                # for and did not get.
                async with asyncio.timeout(timeout):
                    result = await self.session.send(page_domain.Navigate(url=destination))
                    # Chrome reports a dead host here rather than as a protocol
                    # error. Left unchecked, the wait below runs out its whole
                    # timeout and then blames the load event.
                    if result.error_text:
                        raise NavigationError(f"navigating to {destination}: {result.error_text}")
                    await arrived
            except TimeoutError:
                # Which of the two happened is the whole diagnosis, and the two
                # have different causes: a document that committed and never
                # finished is a slow or stalled response, while one that never
                # committed did not get that far. `frameNavigated` fires on commit,
                # well before load, so `self._url` already tells them apart.
                where = (
                    "the document committed and never fired load"
                    if self._url == destination
                    else f"the page is still at {self._url}"
                )
                raise WirespecTimeoutError(
                    f"navigating to {destination}: nothing after {timeout}s -- {where}"
                ) from None
            finally:
                for stop in unsubscribe:
                    stop()

    async def reload(self, *, timeout: float = DEFAULT_ACTION_TIMEOUT) -> None:
        """Run the current document again, and wait for ``load``.

        ``Page.reload`` reports nothing back, so unlike ``goto`` there is no
        result to check -- the load event is the whole answer.
        """
        self._check_open()
        was_at = self._url
        loaded: asyncio.Future[float] = asyncio.get_running_loop().create_future()

        def caught(event: page_domain.LoadEventFired) -> None:
            if not loaded.done():
                loaded.set_result(event.timestamp)

        with self.acting("reload", was_at):
            unsubscribe = self.session.on(page_domain.LoadEventFired, caught)
            try:
                # As in `goto`, one deadline over the command and the wait
                # together, rather than one around each -- the caller asked for a
                # bound on the whole thing. A reload is always a new document, so
                # unlike `goto` there is only one event it can end with.
                async with asyncio.timeout(timeout):
                    await self.session.send(page_domain.Reload())
                    await loaded
            except TimeoutError:
                # A bare TimeoutError says nothing about what was being waited for.
                # Every timeout wirespec raises names it (§1, goal 4).
                raise WirespecTimeoutError(f"reloading {was_at}: no load event after {timeout}s") from None
            finally:
                unsubscribe()

    async def send(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """Send a CDP command wirespec has no wrapper for, in this page's session.

        Public on purpose (§6.2): it is three lines, and the
        alternative is someone forking the library the first time they need a
        call the subset does not cover.
        """
        self._check_open()
        return await self.session.send_raw(method, params)

    async def set_viewport_size(self, viewport: tuple[int, int]) -> None:
        """Resize the page.

        ``Emulation.setDeviceMetricsOverride``, not a window resize: a headless
        window's default size is not a promise, and every box wirespec measures
        is relative to the viewport (§8.9). A tuple rather than
        Playwright's ``{"width": …, "height": …}`` so that this and
        ``new_context(viewport=…)`` are spelled the same way; the compatibility
        surface converts at its boundary (§15.2).
        """
        self._check_open()
        width, height = viewport
        await self.session.send(
            emulation_domain.SetDeviceMetricsOverride(
                width=width,
                height=height,
                # Both are CDP-required and both are wirespec's own defaults:
                # a scale factor of 1 means CSS pixels are device pixels, so a
                # measured box reads the same as the spec's mental model of it,
                # and mobile emulation beyond size is out of scope (§2).
                device_scale_factor=1,
                mobile=False,
            )
        )
        self.viewport = viewport

    async def evaluate(self, expression: str, arg: Any = NO_ARGUMENT) -> Any:
        """Run the caller's JavaScript in the page and return its value.

        Calls the expression if it is a function (§8.8), awaits it
        if it returns a promise, and turns a page-side throw into a Python
        exception rather than a ``None`` that fails somewhere else later.

        ``arg`` is Playwright's second parameter, and it is passed as an
        *argument* rather than spliced into the source. That is the whole point
        of it: a value put into the text would be code, and a spec handing back
        a piece of the page's own text would be one apostrophe from a syntax
        error -- or from running something it did not write. Chrome does the
        serialising, so any JSON shape crosses unchanged.
        """
        self._check_open()
        if arg is not NO_ARGUMENT:
            return await self._called_with(expression, arg)
        source = f"({expression})()" if _FUNCTION.match(expression) else expression
        result = await self.session.send(
            runtime_domain.Evaluate(expression=source, return_by_value=True, await_promise=True)
        )
        if result.exception_details is not None:
            detail = result.exception_details
            thrown = detail.exception
            raise JavaScriptError(
                thrown.description.splitlines()[0] if thrown and thrown.description else detail.text,
                stack=thrown.description if thrown else None,
            )
        # The detection above is narrow on purpose and will sometimes miss a
        # function -- a default argument with parentheses in it, say. Chrome
        # then hands back the function itself, which would otherwise reach the
        # caller as an empty dict and fail as an assertion somewhere else.
        if result.result.type == "function":
            raise JavaScriptError(
                f"{expression.strip()[:60]!r} evaluated to a function rather than a value; "
                "wirespec did not recognise it as one to call (§8.8). "
                "Wrap it yourself -- (EXPR)() -- or simplify the parameter list."
            )
        return result.result.value

    async def title(self) -> str:
        """The document's title, as ``document.title`` reports it.

        Read off ``head > title``, and **not** off ``Target.getTargetInfo``,
        which is the obvious source and the wrong one: measured, a page with no
        title reports a ``targetInfo.title`` of its own *URL* -- what the tab
        strip shows -- where ``document.title`` is ``""``. An assertion about a
        missing title would pass against the address bar (§8.21).

        ``head >`` rather than a bare ``title``, because an inline ``<svg>``
        may carry one of its own and ``document.title`` never means that one.
        And the text is stripped and collapsed, which is HTML's own rule for
        the property -- a prettily-indented ``<title>`` otherwise disagrees
        with every spec written against it.
        """
        found = await self.session.send(
            dom_domain.QuerySelectorAll(node_id=await self.document(), selector="head > title")
        )
        if not found.node_ids:
            return ""
        # depth=-1, because at depth 1 Chrome reports `childNodeCount: 1` and
        # sends no children at all -- the title would read as empty.
        described = await self.session.send(dom_domain.DescribeNode(node_id=found.node_ids[0], depth=-1))
        raw = "".join(child.node_value for child in described.node.children or ())
        return " ".join(raw.split())

    @property
    def request(self) -> APIRequestContext:
        """Issue a request from inside the browser (§6.2).

        The context's, not one of its own: what makes these the application's
        requests is the cookie jar, and the jar belongs to the context. Two
        pages in one context share it, which is what a spec expects.
        """
        self._check_open()
        return self.context.request

    def resolve_url(self, url: str) -> str:
        """A relative path against the context's ``base_url``.

        Raises rather than guessing when there is no base to resolve against:
        a relative URL handed to Chrome becomes a ``file://`` lookup in the
        working directory, which fails somewhere far away from here.
        """
        if urlsplit(url).scheme:
            return url
        if not self.context.base_url:
            raise ValueError(f"{url!r} is relative and this context has no base_url")
        return urljoin(self.context.base_url, url)

    def locator(self, selector: str) -> Locator:
        """A locator for a CSS selector. Touches nothing until it is used."""
        return Locator(self).locator(selector)

    def frame_locator(self, selector: str) -> FrameLocator:
        """The document inside a matching ``<iframe>``, as a place to query.

        Same-process frames only. A frame Chrome runs in another renderer
        process refuses by name when it is used (§8.19).
        """
        return Locator(self).frame_locator(selector)

    # -- watching the network ------------------------------------------------

    async def on(self, event: str, handler: Callable[[Any], object]) -> Callable[[], None]:
        """Subscribe to every ``"request"``, ``"response"`` or ``"dialog"``
        from now on.

        **Async, unlike Playwright's**, and that is a deliberate divergence
        (§4.3): it enables ``Network`` on the first subscription,
        and a synchronous version could only schedule that and hope it landed
        before the next navigation. A subscription that silently misses the
        first request would be worse than one that has to be awaited.

        A ``"dialog"`` handler may be an ordinary function or a coroutine
        function; the other two may not, because they are called from the read
        path where nothing may block (§8.20).
        """
        if event not in ("request", "response", "dialog"):
            raise ValueError(f"page.on({event!r}) is not a thing; wirespec has 'request', 'response' and 'dialog'")
        if event == "dialog":
            return self._on_dialog(handler)
        await self._enable_network()
        if event == "request":
            return self.session.on(network_domain.RequestWillBeSent, lambda sent: handler(self._request_of(sent)))
        return self.session.on(network_domain.ResponseReceived, lambda got: handler(self._response_of(got)))

    @contextlib.asynccontextmanager
    async def expect_request(
        self,
        predicate: Callable[[Request], bool],
        *,
        timeout: float = DEFAULT_ACTION_TIMEOUT,
    ) -> AsyncIterator[_Caught[Request]]:
        """Wait for the request the block is about to cause.

        The subscription is in place before the block runs, so a request made by
        its very first line is still caught (§3.3).
        """
        await self._enable_network()
        async with self.session.expect(
            network_domain.RequestWillBeSent,
            lambda sent: predicate(self._request_of(sent)),
            timeout=timeout,
        ) as future:
            yield _Caught(future, self._request_of)

    @contextlib.asynccontextmanager
    async def expect_response(
        self,
        predicate: Callable[[Response], bool],
        *,
        timeout: float = DEFAULT_ACTION_TIMEOUT,
    ) -> AsyncIterator[_Caught[Response]]:
        """Wait for the response the block is about to cause."""
        await self._enable_network()
        async with self.session.expect(
            network_domain.ResponseReceived,
            lambda got: predicate(self._response_of(got)),
            timeout=timeout,
        ) as future:
            yield _Caught(future, self._response_of)

    @contextlib.asynccontextmanager
    async def expect_popup(self, *, timeout: float = DEFAULT_ACTION_TIMEOUT) -> AsyncIterator[_Caught[Page]]:
        """Wait for the tab the block is about to open.

        A block rather than a reader, for the same reason ``expect_request`` is
        one: the popup announces itself while the click is still being handled,
        so anything subscribing *after* the click has already missed it
        (§3.3).

        Target discovery is turned on here, lazily. It is a browser-wide
        subscription and a suite that never opens a tab should not pay for one.
        """
        await self.context.watch_for_popups()
        opened: asyncio.Future[Page] = asyncio.get_running_loop().create_future()
        try:
            yield _Caught(opened, lambda page: page)
            if not opened.done():
                opened.set_result(await self.context.next_popup(timeout))
        finally:
            if not opened.done():
                opened.cancel()

    async def route(self, pattern: str, handler: Handler) -> None:
        """Intercept requests matching a URL glob.

        Enables ``Fetch`` lazily, because interception costs every request on
        the page a round trip whether or not anybody wanted it
        (§6.2).
        """
        await self._router.add(pattern, handler)

    async def _enable_network(self) -> None:
        """Idempotent, and awaited rather than scheduled: a subscription that
        raced the navigation it was meant to watch would miss it about half the
        time, which is the worst kind of flake."""
        if self._network_on:
            return
        await self.session.send(network_domain.Enable())
        self._network_on = True
        # A standing subscription, so a response can name its request's method:
        # `responseReceived` does not carry it, and only `requestWillBeSent`
        # does. It costs a decode per request, which is a real cost on a busy
        # page (§3.5) -- but it is only paid once somebody has
        # asked to watch the network at all, and a `Response` whose `request` is
        # blank would be a worse answer than a slightly more expensive one.
        self.session.on(network_domain.RequestWillBeSent, self._request_of)
        # And when each one finishes, because `Network.getResponseBody` has
        # nothing to give until it has. `responseReceived` fires when the
        # *headers* arrive, so a body read straight after it fails with
        # "No data found for resource with given identifier" -- intermittently,
        # which is the worst way to find out.
        self.session.on(network_domain.LoadingFinished, lambda done: self._finished(done.request_id))
        self.session.on(network_domain.LoadingFailed, lambda done: self._finished(done.request_id))

    def _request_of(self, sent: network_domain.RequestWillBeSent) -> Request:
        request = Request(
            url=sent.request.url,
            method=sent.request.method,
            headers=dict(sent.request.headers or {}),
            post_data=sent.request.post_data,
            kind=sent.type or "",
        )
        # Remembered so a response can name its request's method, which
        # ``responseReceived`` does not carry. Bounded, because a long-running
        # page makes a great many requests and none of this is worth a leak.
        self._requests[sent.request_id] = request
        if len(self._requests) > _REQUEST_MEMORY:
            for stale in list(self._requests)[: len(self._requests) - _REQUEST_MEMORY]:
                del self._requests[stale]
        return request

    def _finished(self, request_id: str) -> None:
        self._complete.setdefault(request_id, asyncio.Event()).set()
        if len(self._complete) > _REQUEST_MEMORY:
            for stale in list(self._complete)[: len(self._complete) - _REQUEST_MEMORY]:
                del self._complete[stale]

    async def loaded(self, request_id: str, timeout: float) -> None:
        """Wait until a response's body is complete, or give up quietly.

        Giving up quietly is right here: the caller is about to ask for the body
        and will get the protocol's own error if there is none, which says more
        than a timeout from this layer would.
        """
        event = self._complete.setdefault(request_id, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            # TimeoutError only: the body may genuinely never arrive, and the
            # read below is what reports that.
            async with asyncio.timeout(timeout):
                await event.wait()

    def _response_of(self, got: network_domain.ResponseReceived) -> Response:
        request = self._requests.get(got.request_id) or Request(
            url=got.response.url, method="", headers={}, post_data=None, kind=got.type or ""
        )
        return Response(
            self,
            got.request_id,
            got.response.url,
            got.response.status,
            dict(got.response.headers or {}),
            request,
        )

    # -- saying what is being done ---------------------------------------------

    def watch_actions(self, observe: Callable[[str, str, float, float, str | None], object]) -> Callable[[], None]:
        """Be told the name, target, start, end and failure of every action.

        Five plain values rather than an object, so that nothing in ``page.py``
        has to know what a recording is -- the artefact's ``Action`` is built on
        the other side of this boundary (§16.2).
        """
        entry = observe
        self._acting += (entry,)

        def unsubscribe() -> None:
            self._acting = tuple(item for item in self._acting if item is not entry)

        return unsubscribe

    @contextlib.contextmanager
    def acting(self, name: str, target: object) -> Iterator[None]:
        """Wrap one action, so a recording can put it on the timeline.

        Synchronous, and it wraps ``await``s: there is nothing to await here,
        and a sync manager around an awaiting block costs one generator rather
        than the coroutine an async one would.

        ``time.time`` rather than the loop clock, because this has to land on
        the same axis as a screencast frame, whose timestamp is epoch seconds
        (§16.2). Reading it twice for an action nobody is watching
        would be two syscalls on every click in a suite, so the whole thing
        short-circuits when the list is empty.

        A failure is recorded and **re-raised**: an artefact that quietly
        dropped the action that failed would be missing the one row the file
        was kept for.
        """
        # Nested, or nobody watching: either way there is nothing to record.
        # A second action running *concurrently* on one page would also be
        # swallowed by the depth count; that costs a row and never adds a wrong
        # one, and a spec doing two things to one page at once has no ordering
        # for the lane to show anyway.
        if not self._acting or self._acting_depth:
            yield
            return
        self._acting_depth += 1
        started = time.time()
        failure: str | None = None
        try:
            yield
        except BaseException as exc:
            failure = f"{type(exc).__name__}: {exc}".strip()
            raise
        finally:
            self._acting_depth -= 1
            ended = time.time()
            # A string is already a description -- a URL, an assertion's
            # sentence -- and quoting it would only add noise to the lane.
            described = target if isinstance(target, str) else repr(target)
            for observe in self._acting:
                observe(name, described, started, ended, failure)

    # -- answering the page ---------------------------------------------------

    def _on_dialog(self, handler: Callable[[Dialog], object]) -> Callable[[], None]:
        """Register a dialog handler and hand back the way to remove it."""
        entry = (handler,)
        self._dialog_handlers += entry

        def unsubscribe() -> None:
            self._dialog_handlers = tuple(item for item in self._dialog_handlers if item is not handler)

        return unsubscribe

    def _animation_started(self, event: animation_domain.AnimationStarted) -> None:
        """Remember that an element is moving, and until when.

        The expiry is computed here rather than asked for later, because Chrome
        does not send anything when an animation simply *finishes* -- only when
        one is cancelled. Measured: a 400 ms animation produces
        ``animationStarted`` and then silence.

        ``iterations`` is absent for an animation that never ends, and that is
        the case this has to get right: a spinner must stay in the set for ever,
        so a missing count means forever rather than one round
        (``wirespec/cdp/animation.py``).

        Erring late is safe and erring early is not. An entry that lingers costs
        one animation frame of settling on an element that has stopped; an entry
        that expires early is a click at coordinates the element has left.
        """
        effect = event.animation.source
        if effect is None or effect.backend_node_id is None:
            return
        if effect.iterations is None or effect.iterations <= 0:
            until = float("inf")
        else:
            total = effect.delay + effect.duration * effect.iterations + effect.end_delay
            rate = abs(event.animation.playback_rate) or 1.0
            # `currentTime` is how far in it already is when we hear about it,
            # and is negative while the animation is still in its delay.
            remaining = max(total - max(event.animation.current_time, 0.0), 0.0) / rate
            until = time.monotonic() + remaining / 1000.0
        self._animating[event.animation.id] = (effect.backend_node_id, until, _moves(event.animation))
        if len(self._animating) > _ANIMATION_MEMORY:
            self._sweep()

    def _animation_canceled(self, event: animation_domain.AnimationCanceled) -> None:
        """It stopped early -- the rule was removed, the element was, or a
        transition was interrupted. The message names the animation and not the
        element, which is why the id is what this is keyed by."""
        self._animating.pop(event.id, None)

    def moving(self) -> set[int]:
        """The backend ids of everything currently animating.

        Expired entries are dropped on the way past, so nothing has to sweep
        this and a page that animated once does not carry the entry for ever.
        """
        if not self._animating:
            return set()
        self._sweep()
        return {backend for backend, _, _moving in self._animating.values()}

    def finite_movers(self) -> list[tuple[int, float]]:
        """Everything running that could move, and when each of them stops.

        One entry per element, carrying the latest of its animations, because
        the caller's next move is to ask Chrome where each of them is and a
        second question about the same node is a wasted round trip.

        Two exclusions, and both are load-bearing. An animation of a paint-only
        property is not here at all: it cannot change which element is at a
        point, and applications run them constantly (``_PAINT_ONLY`` above).
        Neither is an animation that **never ends** -- an action waits on this,
        and waiting for a spinner is the regression §8.7 exists to
        prevent. The endless entries stay in the dict, because ``moving`` still
        reports them: "is this element moving" has a different answer from "will
        this element stop".
        """
        if not self._animating:
            return []
        self._sweep()
        latest: dict[int, float] = {}
        for backend, until, moves in self._animating.values():
            if moves and until != float("inf") and until > latest.get(backend, 0.0):
                latest[backend] = until
        return list(latest.items())

    def _sweep(self) -> None:
        """Drop the animations that have run their course."""
        now = time.monotonic()
        done = [key for key, (_, until, _moving) in self._animating.items() if until <= now]
        for key in done:
            del self._animating[key]

    def _dialog_opening(self, opening: page_domain.JavascriptDialogOpening) -> None:
        """A question from the page, arriving on the read path.

        Answering it is a command, and a command cannot be awaited from here --
        this runs inside the loop callback that reads the pipe, and blocking it
        would stop the very reply the answer is waiting for. So the work goes
        into a task, and this returns immediately.
        """
        task = asyncio.get_running_loop().create_task(self._answer(Dialog(self, opening)))
        self._answering.add(task)
        task.add_done_callback(self._answered)

    async def _answer(self, dialog: Dialog) -> None:
        """Give the handlers their say, then make sure the dialog is closed.

        The page is stopped for the whole of this, so a slow handler is a slow
        test -- but a handler that never answers is not allowed to be a stopped
        page for ever, which is where Playwright leaves it. Every path out of
        here ends with the dialog answered.
        """
        try:
            for handler in self._dialog_handlers:
                outcome = handler(dialog)
                if inspect.isawaitable(outcome):
                    await outcome
        except Exception as exc:  # noqa: BLE001 -- caller code; whatever it did, the dialog still has to close
            self._complain("a page.on('dialog') handler raised", exc)
        if dialog.handled:
            return
        if self._closed:
            # The tab went down with the question still on screen -- closing a
            # page with a dialog open takes both away. There is nothing left to
            # answer, and sending to a dead session would raise instead.
            return
        if self._dialog_handlers:
            # Not silent: a handler that looked at the dialog and said nothing
            # is almost always an omission, and dismissing on its behalf is a
            # decision wirespec made rather than one the spec asked for.
            self._complain(
                "a dialog was dismissed by default",
                WirespecError(
                    f"a page.on('dialog') handler saw the {dialog.type} {dialog.message!r} and did not "
                    f"answer it, so wirespec dismissed it. An unanswered dialog stops the page and every "
                    f"command after it (§8.20) -- call dialog.accept() or dialog.dismiss()."
                ),
            )
        await dialog.dismiss()

    def _answered(self, task: asyncio.Task[None]) -> None:
        """Retire the task and report anything it could not deal with itself.

        There is no caller to raise into: the dialog arrived from the pipe, and
        the only thing above this task is the loop. Losing the exception here
        would leave a page that stopped answering with nothing at all to say
        about why.
        """
        self._answering.discard(task)
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            self._complain("could not answer a dialog", failure)

    def _complain(self, message: str, exc: BaseException) -> None:
        """Say something from a task nobody awaits.

        ``call_exception_handler`` is where asyncio already sends this class of
        problem, so it surfaces wherever the suite already looks for unhandled
        loop errors rather than in a channel wirespec invented.
        """
        asyncio.get_running_loop().call_exception_handler({"message": message, "exception": exc, "page": self})

    def get_by_role(self, role: str, *, name: Matcher | None = None, exact: bool = False) -> Locator:
        return Locator(self).get_by_role(role, name=name, exact=exact)

    def get_by_text(self, text: Matcher, *, exact: bool = False) -> Locator:
        return Locator(self).get_by_text(text, exact=exact)

    def get_by_label(self, text: Matcher, *, exact: bool = False) -> Locator:
        return Locator(self).get_by_label(text, exact=exact)

    def get_by_placeholder(self, text: Matcher, *, exact: bool = False) -> Locator:
        return Locator(self).get_by_placeholder(text, exact=exact)

    def get_by_test_id(self, value: Matcher, *, exact: bool = True) -> Locator:
        return Locator(self).get_by_test_id(value, exact=exact)

    async def document(self) -> int:
        """The current document's root node id, fetched at most once per document.

        ``depth=-1`` is not negotiable: mutations are only reported for nodes
        already pushed to the client, so a shallow call leaves the wait loop
        with no notifications at all and nothing fails to say so
        (§5.1). Cached because a navigation is what invalidates it,
        and a navigation is something this page already hears about.
        """
        if self._document is None:
            result = await self.session.send(dom_domain.GetDocument(depth=-1))
            self._document = result.root.node_id
            # The reply already carries both ids for every node it describes, so
            # the mapping §3.4 asks the driver to work in is free
            # here and would otherwise be a round trip each. Nodes created after
            # this call are not in it; `backend_ids` falls back for those.
            self._backends = {}
            self._parents = {}
            self._ancestors = {}
            self._invisible = set()
            self.framed = _index(result.root, self._backends, self._parents, self._invisible, self._ancestors)
        return self._document

    async def backend_ids(self, node_ids: list[int]) -> list[int]:
        """Backend node ids for these node ids.

        The cheap path is the map built with the document. The fallback is one
        ``describeNode`` per unknown id, pipelined -- which is what a node
        created since the last navigation costs, and only that node.
        """
        await self.document()
        missing = [node_id for node_id in node_ids if node_id not in self._backends]
        if missing:
            described = await self.session.pipeline([dom_domain.DescribeNode(node_id=node_id) for node_id in missing])
            for node_id, reply in zip(missing, described, strict=True):
                self._backends[node_id] = reply.node.backend_node_id
        return [self._backends[node_id] for node_id in node_ids]

    async def is_invisible_element(self, node_id: int) -> bool:
        """Is this element one whose own text is not on the screen?

        The tags of §8.3, judged by the DOM's ``nodeName`` rather
        than by the serialiser's -- which is the only thing that separates an
        HTML ``<title>`` from an SVG one, since Chrome writes both as
        ``<title>`` (wirespec/markup.py).

        Free for anything in the document map, which is everything that has not
        been created since the last navigation. The fallback is one
        ``describeNode``, and it is only ever reached for a text read whose
        subtree is *rooted* at one of five tag names -- which in practice means
        never.
        """
        await self.document()
        if node_id in self._backends:
            return node_id in self._invisible
        try:
            described = await self.session.send(dom_domain.DescribeNode(node_id=node_id))
        except CDPError:
            # Gone since it was resolved. It has no text either way.
            return False
        return described.node.node_name in INVISIBLE_TAGS

    def invalidate(self) -> None:
        """Forget the document. The next resolution fetches it again."""
        self._document = None
        self._backends = {}
        self._parents = {}
        self._ancestors = {}
        self._invisible = set()
        self.framed = False

    async def resolve(self, chain: tuple[Step, ...]) -> list[int]:
        """The node ids a chain currently names, in document order.

        Retried once against a **stale document**. The root node id is cached
        and dropped when this page hears about a navigation, but a document that
        commits *between* the cached root being read and the query using it
        leaves no window for that: the query goes out against an id Chrome has
        already forgotten and comes back "Could not find node with given id".

        Reachable on any page mid-navigation and reliably on a freshly opened
        popup, which is where it was found -- one run in three. Retrying is
        correct rather than defensive: the document really did change, and the
        answer to a query about "now" is the one from the document that exists
        now. It is retried exactly once, so a genuinely missing node still
        fails rather than looping.
        """
        try:
            return await resolve(self, chain)
        except CDPError as exc:
            if "Could not find node" not in exc.message:
                raise
            self.invalidate()
            return await resolve(self, chain)

    async def wait_for_selector(
        self,
        selector: str,
        *,
        state: str = "visible",
        timeout: float = DEFAULT_ACTION_TIMEOUT,
    ) -> Locator:
        """Wait until a CSS selector matches something in the wanted state.

        Playwright's four, with Playwright's definitions -- which for
        ``hidden`` is the one worth spelling out: an element that is *not
        there* counts as hidden. The stricter reading would make an assertion
        about a row disappearing depend on whether the application hid it or
        removed it, which is an implementation detail the spec should not have
        to know. ``detached`` is the state for when it does know.

        Unlike Playwright this returns a ``Locator`` in every state rather than
        ``None`` for the negative two -- wirespec has no element handle to
        return, so there is nothing for the distinction to carry.
        """
        if state not in _SELECTOR_STATES:
            raise NotImplementedError(
                f"wait_for_selector(state={state!r}) is not supported: wirespec has "
                f"{', '.join(repr(known) for known in _SELECTOR_STATES)}."
            )
        found = self.locator(selector)
        # `hidden` is satisfied by absence as well as by invisibility, so both
        # negative states read the same list the positive one they invert does.
        wants_visible = state in ("visible", "hidden")
        wants_any = state in ("visible", "attached")

        async def read() -> list[int]:
            node_ids = await self.resolve(found.chain)
            if not wants_visible:
                return node_ids
            return [node_id for node_id in node_ids if await self.is_visible(node_id)]

        await poll(
            self,
            read,
            lambda node_ids: bool(node_ids) is wants_any,
            lambda node_ids: (
                f"no element matching {selector!r} was {state}"
                if wants_any
                else f"an element matching {selector!r} was still {'visible' if wants_visible else 'attached'}"
            ),
            timeout,
        )
        return found

    def parent_of(self, backend_id: int) -> int | None:
        """The parent's backend id, from the map built with the document."""
        return self._parents.get(backend_id)

    def ancestor_ids(self, node_id: int, limit: int = _MAX_DEPTH) -> list[int]:
        """This node's ancestors, outermost last, as node ids.

        From the map built with the document, so it costs nothing. Empty for a
        node created since the last navigation -- which is the honest answer
        rather than a wrong one, and the caller decides what to do about it.
        """
        found: list[int] = []
        walk = node_id
        for _ in range(limit):
            parent = self._ancestors.get(walk)
            if parent is None:
                break
            found.append(parent)
            walk = parent
        return found

    @property
    def node_count(self) -> int:
        """How many nodes the document map holds.

        Zero until something has resolved a query, which is the honest answer:
        the map is built by the first ``getDocument`` and not before. Used to
        decide whether narrowing a query is cheaper than confirming every
        candidate (§8.30), where zero simply means "do not
        narrow".
        """
        return len(self._backends)

    def knows_node(self, node_id: int) -> bool:
        """Was this node id in the document map when it was built?

        The node-id counterpart of ``knows``. ``False`` means "created since the
        last navigation", so its ancestry is unknown here -- which for a caller
        that must not under-approximate is a reason to stop rather than to
        assume.
        """
        return node_id in self._ancestors or node_id == self._document

    def knows(self, backend_id: int) -> bool:
        """Can the parent map place this node at all?

        ``_parents`` holds an entry for every node the document walk saw except
        the root, so membership is the same question as "was this here when the
        map was built" -- and ``False`` means "created since", not "not in the
        document". A caller that needs an answer anyway has to ask Chrome.
        """
        return backend_id in self._parents

    def contains(self, ancestor_backend: int, descendant_backend: int) -> bool | None:
        """Is one node inside another? ``None`` when the tree does not say.

        Walks the parent map built with the document, so it is free and -- more
        to the point -- it works for **text nodes**, which a
        ``querySelectorAll("*")`` cannot see at all. That matters because
        ``getNodeForLocation`` returns the deepest node at a point, and the
        deepest node over a button is usually the button's own text.

        ``None`` rather than ``False`` for a node the map has never heard of:
        that is "created since the last navigation", not "not inside", and the
        caller has a slower way to find out.
        """
        if descendant_backend not in self._parents and descendant_backend not in self._backends.values():
            return None
        seen = descendant_backend
        for _ in range(_MAX_DEPTH):
            if seen == ancestor_backend:
                return True
            parent = self._parents.get(seen)
            if parent is None:
                return False
            seen = parent
        return False

    async def is_visible(self, node_id: int) -> bool:
        """§5.2's definition, and only that definition.

        A non-empty box model, and not ``visibility: hidden``. Deliberately
        **not** opacity and **not** in-viewport: both of those make correct
        specs flake, an element faded to zero is still on the page, and one
        below the fold is one scroll away.
        """
        try:
            model = (await self.session.send(dom_domain.GetBoxModel(node_id=node_id))).model
            if not model.width or not model.height:
                return False
            style = await self.session.send(css_domain.GetComputedStyleForNode(node_id=node_id))
        except CDPError:
            # Chrome refuses a box for an element with no layout at all --
            # display:none, or detached since it was resolved. Both mean the
            # same thing here and neither is an error.
            #
            # The *style* call is inside the guard for a second reason, and it
            # is a measured one: these are two round trips, and an application
            # redrawing between them leaves the second asking about a node
            # Chrome has already forgotten. The pilot suite failed exactly there
            # -- `CSS.getComputedStyleForNode: Could not find node with given
            # id`, raised out of `to_be_hidden` on a page that was merely
            # re-rendering (§8.23). A node that is gone is not
            # visible, which is the same answer either way.
            return False
        return all(property.value != "hidden" for property in style.computed_style if property.name == "visibility")

    async def computed_style(self, node_id: int, name: str) -> str:
        """One computed property. CDP returns all of them -- there is no way to
        ask for less -- so this picks one out of ~340."""
        reply = await self.session.send(css_domain.GetComputedStyleForNode(node_id=node_id))
        for property in reply.computed_style:
            if property.name == name:
                return property.value
        return ""

    async def input_type(self, node_id: int) -> str:
        """The ``type`` of an ``<input>``, lowercased, or "" for anything else.

        A ``<textarea>`` and a ``<div contenteditable>`` are text fields with no
        type at all, and an ``<input>`` with no type is a text field too.
        """
        reply = await self.session.send(dom_domain.GetAttributes(node_id=node_id))
        pairs = dict(zip(reply.attributes[0::2], reply.attributes[1::2], strict=True))
        return pairs.get("type", "").lower()

    async def select_all(self, node_id: int) -> None:
        """Select the field's contents, so the next insert replaces them.

        ``DOM.setNodeValue`` would not raise the events a controlled input
        listens for, so the selection is made the way a person makes it: focus,
        then Ctrl+A. The modifier is 2 (Ctrl) on every platform wirespec
        supports; macOS is untested (§12).
        """
        for kind in ("keyDown", "keyUp"):
            await self.session.send(
                input_domain.DispatchKeyEvent(
                    type=kind,
                    key="a",
                    code="KeyA",
                    windows_virtual_key_code=0x41,
                    native_virtual_key_code=0x41,
                    modifiers=2,
                )
            )

    async def ax_properties(self, node_id: int) -> dict[str, str]:
        """The accessibility node's properties, lowercased, as a plain mapping.

        ``disabled``, ``focused``, ``required``, ``readonly``, ``checked`` --
        all of which arrived with the role while it was being confirmed, so the
        actionability checks that used to need a probe need nothing
        (§5.2).
        """
        reply = await self.session.send(ax_domain.GetPartialAXTree(node_id=node_id, fetch_relatives=False))
        if not reply.nodes:
            return {}
        return {item.name: str(item.value.value).lower() for item in reply.nodes[0].properties or ()}

    async def control_value(self, node_id: int) -> str | None:
        """The value of a form control, or ``None`` if it is not one.

        From the accessibility node, which is also what makes a picker input's
        value readable at all without JavaScript (§8.4).
        """
        reply = await self.session.send(ax_domain.GetPartialAXTree(node_id=node_id, fetch_relatives=False))
        for node in reply.nodes:
            # A slider's value arrives as a *number*, a textbox's as a string.
            # Reading only strings made `fill` on a range field compare "" with
            # "75" and report a failure that had not happened.
            if node.value is not None and isinstance(node.value.value, str | int | float):
                return str(node.value.value)
            # A textbox with nothing in it has a value that is simply absent,
            # which is not the same as "this element has no value at all".
            if node.role is not None and node.role.value in _VALUED_ROLES:
                return ""
        return None

    async def handle_for(self, node_id: int) -> str:
        """A ``Runtime`` object id for a node, so the caller's own JavaScript
        can be handed it."""
        resolved = await self.session.send(dom_domain.ResolveNode(node_id=node_id))
        if resolved.object.object_id is None:
            raise LookupError(f"node {node_id} could not be resolved to a JavaScript handle")
        return resolved.object.object_id

    async def _called_with(self, expression: str, arg: Any) -> Any:
        """``evaluate`` with an argument, which has to be a call rather than an
        evaluation -- there is nowhere to put an argument otherwise.

        The handle is the document's, because ``callFunctionOn`` needs
        *something* to run against and the document is the one object already
        known to be in the page's main world. It arrives as ``this`` and the
        wrapper drops it, so the caller's function sees exactly one parameter.
        """
        if not _FUNCTION.match(expression):
            raise JavaScriptError(
                f"{expression.strip()[:60]!r} was given an argument but is not a function, so there is "
                f"nowhere to put it (§8.8). Write it as `x => ...`."
            )
        return await self._called(
            runtime_domain.CallFunctionOn(
                function_declaration=f"function (argument) {{ return ({expression})(argument); }}",
                object_id=await self.handle_for(await self.document()),
                arguments=[runtime_domain.CallArgument(value=arg)],
                return_by_value=True,
                await_promise=True,
            ),
            expression,
        )

    async def call_on(self, object_id: str, expression: str, arg: Any = NO_ARGUMENT) -> Any:
        """Call the caller's function with one element as its argument.

        ``Runtime.callFunctionOn`` passes the handle as ``this``, not as an
        argument, and specs write ``node => node.tagName``. The wrapper is what
        makes both spellings work (§8.9).

        With an ``arg``, the wrapper hands in both -- element first, the way
        Playwright's ``(element, arg) => ...`` reads. Without one it stays a
        single-parameter call, because a function declared to take one and
        handed two is fine in JavaScript but a function *reading* a second
        would silently see ``undefined``.
        """
        passed = [] if arg is NO_ARGUMENT else [runtime_domain.CallArgument(value=arg)]
        declaration = (
            f"function () {{ return ({expression})(this); }}"
            if arg is NO_ARGUMENT
            else f"function (argument) {{ return ({expression})(this, argument); }}"
        )
        return await self._called(
            runtime_domain.CallFunctionOn(
                function_declaration=declaration,
                object_id=object_id,
                arguments=passed or None,
                return_by_value=True,
                await_promise=True,
            ),
            expression,
        )

    async def call_on_all(self, object_ids: list[str], expression: str) -> Any:
        """Call the caller's function once, with the whole match array.

        The array is built inside the page from handles passed as arguments, so
        two hundred elements cross the wire as two hundred references and the
        function runs once (§6.3).
        """
        arguments = [runtime_domain.CallArgument(object_id=object_id) for object_id in object_ids]
        return await self._called(
            runtime_domain.CallFunctionOn(
                function_declaration=f"function (...nodes) {{ return ({expression})(nodes); }}",
                execution_context_id=None,
                object_id=await self.handle_for(await self.document()),
                arguments=arguments,
                return_by_value=True,
                await_promise=True,
            ),
            expression,
        )

    async def _called(self, command: runtime_domain.CallFunctionOn, expression: str) -> Any:
        result = await self.session.send(command)
        if result.exception_details is not None:
            thrown = result.exception_details.exception
            raise JavaScriptError(
                thrown.description.splitlines()[0] if thrown and thrown.description else result.exception_details.text,
                stack=thrown.description if thrown else None,
            )
        return result.result.value

    def _frame_navigated(self, event: page_domain.FrameNavigated) -> None:
        # Main frame only: an iframe navigating does not move the page.
        if event.frame.parent_id is None:
            self._url = event.frame.url
            # A navigation invalidates the node-id space, and the mutation
            # notifications with it (§5.1). Dropped rather than
            # refetched: the next resolution will ask, and a page nobody
            # queries should not pay 3.51 ms for a tree nobody wants.
            self.invalidate()

    def _navigated_within_document(self, event: page_domain.NavigatedWithinDocument) -> None:
        # No parent id on this one, so the main frame is told apart by its own
        # id -- which is the frame this session is attached to.
        if event.frame_id == self.main_frame_id:
            self._url = event.url
