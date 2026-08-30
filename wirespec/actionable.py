"""Everything that must be true before an action, and the point to press.

§5.2. Before §3.4 this was literally one call into
the page; it is now several CDP calls issued together and awaited together,
which costs one round trip rather than eight.

The checks, in order:

1. exactly one element -- strict, because two matches is a spec bug and is
   reported as one rather than answered about whichever came first
2. visible: a non-empty box model **and** not ``visibility: hidden``.
   Deliberately *not* opacity and *not* in-viewport: an element faded to zero is
   still on the page, one below the fold is one scroll away, and both make
   correct specs flake
3. enabled, unless the caller asked otherwise -- free, the AX node already
   said so while confirming the role
4. editable, when filling -- likewise free
5. scrolled into view if it is outside the viewport
6. not moving
7. nothing else on its way across the point -- step 6 settles the element and
   is scoped to its own chain, so an unrelated element sweeping over the point
   is invisible to it (§8.32, and ``_stop_moving`` below)
8. the point actually hits the element or a descendant -- **not an ancestor**,
   which is a correction rather than a simplification: ``<html>`` is an
   ancestor of everything, so accepting one meant every empty point on the page
   passed the hit test and a clipped element was pressed where it was not
   (§8.31, and ``_hits`` below)

``force=True`` drops steps 7 and 8, for the case they exist to catch and the
caller means anyway: hovering something a tooltip is deliberately sitting on.

**On step 6**, which is the one with a history. Deciding whether an element is
moving has been done three ways, and each replacement was made for a measured
reason rather than a tidier one:

1. ``document.getAnimations()`` in the page. Permanently true on any page with
   one indefinite animation anywhere -- a spinner, a pulsing skeleton -- so
   every action waited two animation frames it had no reason to: 25.2 ms per
   action instead of 17.0 ms, 64% of all protocol time in the suite
   (§8.7). The lesson kept from it is that the question must be
   **scoped to the element, its ancestors and its subtree**.
2. ``animation-name`` and ``transition-duration`` out of a ``DOMSnapshot``.
   Correctly scoped, and the wrong shape: it read the entire document to answer
   about one element, on every action, and it grew with the page -- 4.5 ms at a
   thousand nodes and 59.7 ms at fourteen thousand.
3. The ``Animation`` domain, which is what runs now. Chrome pushes
   ``animationStarted`` naming the element and the duration, so the page keeps
   the answer as a set and this step costs **no round trip at all** on a page
   with nothing animating (§8.28, ``wirespec/cdp/animation.py``).

The waiting itself is unchanged, and so is what makes it affordable: the second
box sample is free. The box is read at the start of the checks and again just
before the point is used, and the several round trips in between are ones the
action was making anyway. Sampling twice *without* a reason does not work and is
worth recording -- on a local pipe the two samples land about a millisecond
apart, a fifteenth of a frame, so a genuinely animating element looks perfectly
still. Measured, on an element moving 300px in 600ms.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from wirespec.cdp import css as css_domain
from wirespec.cdp import dom as dom_domain
from wirespec.cdp import page as page_domain
from wirespec.errors import CDPError
from wirespec.resolve import describe

if TYPE_CHECKING:
    from wirespec.locator import Locator

__all__ = ["Verdict", "actionable"]

#: One animation frame at 60 Hz. Only ever waited when the element was seen to
#: move (see the module docstring).
_FRAME = 1 / 60


@dataclass(frozen=True, slots=True)
class Verdict:
    """Either a point to act at, or the reason there is not one."""

    node_id: int | None = None
    point: tuple[float, float] | None = None
    refusal: str = ""

    @property
    def ok(self) -> bool:
        return self.point is not None


async def actionable(
    locator: Locator,
    *,
    enabled: bool = True,
    editable: bool = False,
    force: bool = False,
) -> Verdict:
    """One pass of the checks. Returns a verdict; the caller polls it."""
    page = locator.page
    found = await page.resolve(locator.chain)
    if len(found) != 1:
        return Verdict(refusal=f"{locator!r} matched {'nothing' if not found else f'{len(found)} elements'}")
    node_id = found[0]

    box = await _border_box(page, node_id)
    if box is None:
        return Verdict(node_id=node_id, refusal=f"{locator!r} has no box: it is not rendered")

    # Pipelined: the visibility read and the AX properties go out together, so
    # the batch costs roughly one round trip rather than three.
    style, properties = await asyncio.gather(
        page.session.send(css_domain.GetComputedStyleForNode(node_id=node_id)),
        page.ax_properties(node_id),
    )
    if any(item.name == "visibility" and item.value == "hidden" for item in style.computed_style):
        return Verdict(node_id=node_id, refusal=f"{locator!r} is visibility:hidden")
    if enabled and properties.get("disabled") == "true":
        return Verdict(node_id=node_id, refusal=f"{locator!r} is disabled")
    if editable:
        if properties.get("disabled") == "true":
            return Verdict(node_id=node_id, refusal=f"{locator!r} is disabled")
        if properties.get("readonly") == "true":
            return Verdict(node_id=node_id, refusal=f"{locator!r} is readonly")

    viewport = page.viewport
    if not _inside(box, viewport):
        await page.session.send(dom_domain.ScrollIntoViewIfNeeded(node_id=node_id))
        box = await _border_box(page, node_id)
        if box is None:
            return Verdict(node_id=node_id, refusal=f"{locator!r} lost its box while being scrolled to")

    # Step 6. Two samples an animation frame apart is ~33 ms of pure waiting,
    # far more than the check costs, so it must not happen on every action
    # (§8.7). What decides is `_may_move` below.
    if await _may_move(page, node_id, style):
        settled = await _settle(page, node_id)
        if settled is None:
            return Verdict(node_id=node_id, refusal=f"{locator!r} is still moving")
        box = settled

    point = _centre(box)
    if force:
        return Verdict(node_id=node_id, point=point)

    # Step 7 and a half, and the reason it is here rather than folded into the
    # hit test: the hit test answers for the instant it is asked, and the press
    # happens in the next one. Steps 5 and 6 have settled *this element*; what
    # neither of them can see is something else on its way across the point,
    # because it is neither an ancestor nor a descendant and step 6 is scoped --
    # correctly (§8.7) -- to the element's own chain.
    #
    # So wait for anything that could arrive at **this point** to stop first --
    # not for the page, which is the same mistake §8.7 made one scope out
    # (§8.32).
    if await _stop_moving(page, point):
        box = await _border_box(page, node_id)
        if box is None:
            return Verdict(node_id=node_id, refusal=f"{locator!r} lost its box while the point cleared")
        point = _centre(box)

    if (blocking := await _hits(page, node_id, point)) is None:
        return Verdict(node_id=node_id, point=point)

    # §8.6: "in the viewport" is not the same as "reachable". A
    # sticky header sits over its own scroll container without occupying any
    # room in it; a scroller shorter than its content clips its own children out
    # of sight while leaving them on screen (§8.31). Neither is
    # answered by step 5, which asks only about the viewport -- so scroll the
    # element toward the middle and test once more. This is what rescues the
    # clipped case: `scrollIntoViewIfNeeded` scrolls every scrolling ancestor,
    # not just the window. Confirmed against what Playwright actually
    # dispatches: it clicked a drop zone at y=606 whose box had been at
    # y=617.5 -- it had scrolled 51px first.
    await page.session.send(dom_domain.ScrollIntoViewIfNeeded(node_id=node_id))
    box = await _border_box(page, node_id)
    if box is None:
        return Verdict(node_id=node_id, refusal=f"{locator!r} lost its box while being scrolled clear")
    point = _centre(box)
    if (blocking := await _hits(page, node_id, point)) is None:
        return Verdict(node_id=node_id, point=point)
    return Verdict(node_id=node_id, refusal=await _unreachable(page, locator, point, blocking))


#: The longest one attempt will wait for something to stop crossing the point.
#: Not a budget for the whole action: the caller's retry loop comes straight
#: back, so a transition longer than this is waited out across several attempts
#: and the action's own deadline stays the only thing that ends it. What this
#: stops is a single `sleep` swallowing a timeout the caller asked for.
_MOTION_CAP = 0.5

#: How close a moving element has to be to the point before the action waits for
#: it, in CSS pixels from its border box.
#:
#: **The exposure being closed is a few milliseconds long, so the region that
#: can close it is a few tens of pixels wide.** Measured, with the wait removed,
#: on `tests/driver/pages/sweeping.html`: from the hit test to `mousePressed` is
#: 11.6 ms median and 30.3 ms at the worst of thirty, and the pill crosses 14.4
#: px in that window at 867 px/s and 43.3 px at 2,600 px/s -- and 2,600 px/s is
#: already far quicker than an application animates, being the fixture's 260 px
#: driven in a tenth of a second. This is twelve times the worst of those, so an
#: element would have to close it at about 17,000 px/s to be somewhere else when
#: the check ran and over the point when the press landed.
#:
#: The number is generous on purpose and still narrows hard, because what an
#: action competes with is mostly nowhere near it: a toast in a corner, a dock
#: along the bottom, a drawer down the far edge. Waiting for those was the whole
#: cost of §8.32's first draft -- 2.79 s and 3.50 s against a 0.42 s floor, on a
#: bench of twelve clicks, for animations 630 px and 994 px from the point being
#: pressed. Both come back to within a tenth of a second of the floor.
_REACH = 512.0


async def _stop_moving(page, point: tuple[float, float]) -> bool:
    """Wait for whatever could cross this point to stop. Did we wait?

    Costs nothing at all on a still page -- ``finite_movers`` is a dict lookup
    and the dict is empty -- which is what makes this affordable on every action.
    When something *is* running it costs one pipelined batch of box reads, and
    that is what buys the narrowing: **the question is about the point, not
    about the page.**

    Asking about the page was the first draft's mistake, and it is the shape of
    §8.7 one scope further out. §8.7's lesson was that "is anything
    animating" is the wrong question because the answer is yes on any page with
    a spinner; the same is true of "will the page stop", because the answer is
    "in 600 ms" on any page with a toast fading in somewhere else entirely. An
    animation that cannot reach the point cannot take the press, whatever it is
    doing.

    A mover Chrome will not give a box for is dropped rather than waited for: it
    has gone since it started animating, and a node that is not there is not
    over the point. So is one whose box has no area, for the same reason.
    """
    movers = page.finite_movers()
    if not movers:
        return False
    boxes = await page.session.pipeline(
        [dom_domain.GetBoxModel(backend_node_id=backend) for backend, _ in movers], return_exceptions=True
    )
    ends = [
        until
        for (_, until), reply in zip(movers, boxes, strict=True)
        if not isinstance(reply, BaseException)
        and reply.model.width
        and reply.model.height
        and _reaches(reply.model.border, point)
    ]
    if not ends:
        return False
    remaining = max(ends) - time.monotonic()
    if remaining <= 0:
        return False
    await asyncio.sleep(min(remaining, _MOTION_CAP))
    return True


def _reaches(quad: list[float], point: tuple[float, float]) -> bool:
    """Is this box near enough to the point to be over it before the press?

    Per axis rather than as a radius, which is the conservative direction: it
    treats the square around the point as in range where a circle would not.
    A box the point is inside of is at distance zero on both.
    """
    x, y = point
    xs, ys = quad[0::2], quad[1::2]
    return max(min(xs) - x, x - max(xs), 0.0) <= _REACH and max(min(ys) - y, y - max(ys), 0.0) <= _REACH


#: How many times to re-sample before giving up on something settling.
_SETTLE_TRIES = 20

#: The computed properties that say an element has been *told* to move, whether
#: or not it has started. Two, because these are the two that matter.
_MOTION_STYLES = ("animation-name", "transition-duration")


def _declares_motion(computed: list[css_domain.CSSComputedStyleProperty]) -> bool:
    """Does this style say an animation or transition applies?

    The push half of ``_may_move`` cannot answer this, and that is the whole
    reason this half exists. **A style is set synchronously and an animation
    starts on the next frame**, so between adding a class and Chrome's
    ``animationStarted`` there is about one frame in which the element is about
    to move and nothing has said so. Measured: a hover issued straight after the
    class was added found an empty animating set, acted immediately, and landed
    on the element's old position -- which is exactly the flake step 6 exists to
    prevent, and is what ``tests/driver/test_actions.py`` catches.

    ``transition-duration`` is non-zero on an element with a transition merely
    *declared*, so this over-reports on purpose: the cost of a yes is one
    animation frame of settling, and the cost of a wrong no is a click at
    coordinates the element has left.
    """
    for item in computed:
        if item.name == "animation-name":
            if item.value not in ("", "none"):
                return True
        elif item.name == "transition-duration" and any(
            part.strip() not in ("", "0s") for part in item.value.split(",")
        ):
            return True
    return False


async def _may_move(page, node_id: int, style: css_domain.GetComputedStyleForNodeResult) -> bool:
    """Is there reason to think this element is moving?

    §8.7 and §8.28 are the whole of why this function exists, and
    it has now been answered three ways.

    The prototype asked ``document.getAnimations()``, which is permanently true
    on any page with one indefinite animation anywhere -- a spinner, a pulsing
    skeleton -- so every click and every keystroke waited two animation frames
    it had no reason to: **25.2 ms per action instead of 17.0 ms, and 64% of all
    protocol time in the suite.** The fix was to scope the question to the
    element, its subtree and its ancestors.

    The naive replacement -- sample the box twice and compare -- does not work,
    for a reason worth recording: on a local pipe the two samples land about a
    millisecond apart, which is a fifteenth of a frame, so a genuinely animating
    element looks perfectly still. Measured, on an element moving 300px in
    600ms.

    So the second answer read ``animation-name`` and ``transition-duration`` out
    of a ``DOMSnapshot``. Correctly scoped, and the wrong shape: it read the
    **entire document** to answer about one element, on **every action**, and it
    grew with the page -- 4.5 ms at a thousand nodes, 59.7 ms at fourteen
    thousand.

    This is the third. Chrome pushes ``Animation.animationStarted`` with the
    element and the duration, so the page keeps the answer as a set and this
    costs no round trip at all. Two things get *better* rather than merely
    cheaper:

    * A declared transition is not a running one. ``transition-duration`` is
      non-zero on an element with a transition declared even when nothing is
      happening, so the snapshot reading spent a frame on every action against
      such an element. This is true only while one actually runs.
    * ``element.animate()`` sets no ``animation-name``, so the snapshot reading
      could not see the Web Animations API at all. This does.

    The scope is unchanged -- the element, its ancestors and its subtree -- and
    the ancestry still comes from the parent map built with the document.

    **The push is not the whole answer, and the gap is one frame wide.** A style
    is set synchronously and an animation starts on the next frame, so an
    element whose class was added a millisecond ago is about to move and Chrome
    has not said so yet. So the styles are still read -- but only for this
    element, whose style the visibility check has already fetched, and for its
    ancestors, which is a walk up rather than a read of the whole document. See
    ``_declares_motion``.
    """
    # The element's own style is free: `actionable` fetched it to answer
    # "visible", and the reply carries every property there is.
    if _declares_motion(style.computed_style):
        return True

    moving = page.moving()
    ancestors = page.ancestor_ids(node_id)
    if not moving and not ancestors:
        return False
    if ancestors:
        # An ancestor that has been told to move takes this element with it --
        # §8.7's named case, a dialog sliding into place with a
        # button inside it whose own style says nothing at all.
        styles = await page.session.pipeline(
            [css_domain.GetComputedStyleForNode(node_id=each) for each in ancestors], return_exceptions=True
        )
        for reply in styles:
            if isinstance(reply, BaseException):
                continue  # gone while being asked about; it is not moving this
            if _declares_motion(reply.computed_style):
                return True
    if not moving:
        return False

    backend = (await page.backend_ids([node_id]))[0]
    if backend in moving:
        return True

    # An ancestor already running moves this too.
    seen = backend
    for _ in range(_ANCESTOR_LIMIT):
        parent = page.parent_of(seen)
        if parent is None:
            break
        if parent in moving:
            return True
        seen = parent

    # And a descendant moving may move this one's box with it -- the subtree is
    # part of §8.7's scope. Walked from each animating node *upward* rather than
    # over the subtree downward: the parent map only goes one way, and there are
    # few animating nodes and many descendants.
    unknown: list[int] = []
    for animating in moving:
        if not page.knows(animating):
            # Created since the last navigation, so the map cannot place it.
            # That is the everyday case on an application that inserts its own
            # spinner, so it has to be answered rather than assumed.
            unknown.append(animating)
            continue
        walk = animating
        for _ in range(_ANCESTOR_LIMIT):
            parent = page.parent_of(walk)
            if parent is None:
                break  # reached the document without meeting this element
            if parent == backend:
                return True
            walk = parent
    if not unknown:
        return False
    return await _holds_any(page, node_id, unknown)


async def _holds_any(page, node_id: int, backends: list[int]) -> bool:
    """Is any of these inside ``node_id``, asked of Chrome rather than the map?

    The fallback for an element that started animating after the document map
    was built. Two pipelined round trips -- a ``describeNode`` for each
    animating node and one ``querySelectorAll`` over the subtree -- and only
    ever reached while something the map has not heard of is actually moving.

    Assuming ``True`` instead would be the cheap answer and the wrong one: an
    application that inserts a spinner anywhere on the page would put every
    action back to waiting an animation frame it has no reason to, which is
    exactly the regression §8.7 exists to prevent.
    """
    described, inside = await asyncio.gather(
        page.session.pipeline(
            [dom_domain.DescribeNode(backend_node_id=backend) for backend in backends], return_exceptions=True
        ),
        page.session.send(dom_domain.QuerySelectorAll(node_id=node_id, selector="*")),
    )
    descendants = set(inside.node_ids)
    for reply in described:
        if isinstance(reply, BaseException):
            continue  # gone since it started animating, so it is not in the way
        found = reply.node.node_id
        if not found:
            # Chrome has no node id for it, which means it has never been pushed
            # to the frontend and nothing here can place it. It is moving
            # somewhere; assume it could be here.
            return True
        if found in descendants:
            return True
    return False


#: A chain longer than this is a cycle, and walking it forever would hang.
_ANCESTOR_LIMIT = 512


async def _settle(page, node_id: int) -> list[float] | None:
    """Sample until two readings a frame apart agree, or give up."""
    previous = await _border_box(page, node_id)
    for _ in range(_SETTLE_TRIES):
        await asyncio.sleep(_FRAME)
        current = await _border_box(page, node_id)
        if current is None:
            return None
        if previous is not None and _same(previous, current):
            return current
        previous = current
    return None


async def _border_box(page, node_id: int) -> list[float] | None:
    """The **border** quad. ``getBoundingClientRect``, which every spec's mental
    model is built on, is the border box, and a padded button's content box can
    sit tens of pixels from where the button looks (§8.9)."""
    try:
        model = (await page.session.send(dom_domain.GetBoxModel(node_id=node_id))).model
    except CDPError:
        # Chrome refuses a box for an element with no layout. That is an answer
        # -- "it is not rendered" -- and the caller turns it into a refusal.
        return None
    return model.border if model.width and model.height else None


def _centre(quad: list[float]) -> tuple[float, float]:
    xs, ys = quad[0::2], quad[1::2]
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


def _same(one: list[float], other: list[float]) -> bool:
    #: Sub-pixel jitter is not movement; half a pixel is the threshold Chrome's
    #: own layout rounding produces.
    return all(abs(a - b) < 0.5 for a, b in zip(one, other, strict=True))


def _inside(quad: list[float], viewport: tuple[int, int]) -> bool:
    width, height = viewport
    xs, ys = quad[0::2], quad[1::2]
    return min(xs) >= 0 and min(ys) >= 0 and max(xs) <= width and max(ys) <= height


#: ``_hits``'s answer for "there is no node at that point at all". A real
#: backend node id is never 0, so this cannot collide with one.
_NOTHING = 0

#: The elements behind every point on the page. Finding one of these at the
#: element's centre is not finding something *on top of* the element -- it is
#: finding that the element is not painted there, and the press would land on
#: the page itself.
_BACKDROP = frozenset({"HTML", "BODY"})


async def _hits(page, node_id: int, point: tuple[float, float]) -> int | None:
    """What a press here would land on -- ``None`` when that is the element.

    The element itself counts, and so does **anything inside it**:
    ``getNodeForLocation`` returns the deepest node at the point, and the
    deepest node over a button is usually the button's own text node.

    An **ancestor** does not count, and that is a correction. This check used to
    accept one, reasoning that a click on a label reaches the input it wraps.
    Two things are wrong with that. A press whose target is an ancestor is
    delivered *to the ancestor*; the element inside it never hears the event. And
    the case it was written for never arises: a label wrapping a rendered control
    does not cover it, so the control wins its own hit test and the first check
    below is what accepts it. Measured on the three shapes that could have
    produced an ancestor at the control's centre -- a label around an ``<input>``
    hits the ``INPUT``, a label with a positioned overlay hits the overlay, which
    is a *sibling*, and one painting through ``::after`` hits the pseudo-element.
    None of them is an ancestor, and ``test_a_click_on_a_label_reaches_the_input
    _it_wraps`` passes either way.

    What the rule did accept, on every page, was ``<html>``. It is an ancestor of
    everything, so **any point with nothing painted at it passed the hit test**
    -- and an element clipped out of sight by a scrolling ancestor was pressed at
    coordinates it did not occupy, and the press reported as a click that worked
    (§8.31). One false positive on every page, no true ones.

    Returns the backend node id of whatever is in the way rather than a name,
    because the first caller throws the answer away and scrolls past it: naming
    the node costs a round trip and only the refusal ever needs one.
    """
    x, y = point
    # **The one call in the resolution set that wants document coordinates.**
    # `getBoxModel` returns viewport coordinates and `dispatchMouseEvent` takes
    # them, so `point` is a viewport point -- but `getNodeForLocation` hit-tests
    # in document space despite the protocol describing it as viewport-relative.
    # The two agree on an unscrolled page and diverge by exactly the scroll
    # offset on every other one, so this works everywhere until a page scrolls
    # and then refuses every click below the fold as "covered"
    # (§8.13). Measured: at scrollY 1698, a button whose box was at
    # y=709 was found at y=2407 and nowhere else.
    metrics = await page.session.send(page_domain.GetLayoutMetrics())
    scroll = metrics.css_visual_viewport
    try:
        hit = await page.session.send(dom_domain.GetNodeForLocation(x=int(x + scroll.page_x), y=int(y + scroll.page_y)))
    except CDPError:
        # Nothing at that point at all.
        return _NOTHING
    target_backend = (await page.backend_ids([node_id]))[0]
    if hit.backend_node_id == target_backend:
        return None

    # The parent map first, because it is free and because it can see **text
    # nodes**, which a `querySelectorAll("*")` cannot match at all -- so an
    # element-only check would refuse a click on a button's own text.
    inside = page.contains(target_backend, hit.backend_node_id)
    if inside is True:
        return None
    if inside is False:
        return hit.backend_node_id

    # The map has never heard of the node at the point, which means it was
    # created since the last navigation. Ask Chrome instead -- and only for the
    # node id, which the hit itself often already carries.
    hit_node_id = hit.node_id
    if hit_node_id is None:
        described = await page.session.send(dom_domain.DescribeNode(backend_node_id=hit.backend_node_id))
        hit_node_id = described.node.node_id
    if hit_node_id:
        descendants = await page.session.send(dom_domain.QuerySelectorAll(node_id=node_id, selector="*"))
        if hit_node_id in descendants.node_ids:
            return None
    return hit.backend_node_id


async def _unreachable(page, locator: Locator, point: tuple[float, float], blocking: int) -> str:
    """Why the press cannot land, in the terms the person reading it needs.

    Two different failures wear the same shape -- the point does not reach the
    element -- and they send someone to completely different places, so they must
    not be phrased the same way (§5.2). Something *on top of* the
    element is a stacking problem, and the message names the thing so it can be
    found in the source. **Nothing** at the point is not that: the element has a
    box on the screen and is not drawn in it, which is a different bug in a
    different place, and saying "covered by something else" sends the reader
    looking for an overlay that does not exist. That mis-description is half of
    what made §8.31 so hard to see.
    """
    where = f"{point[0]:.0f},{point[1]:.0f}"
    node = None if blocking == _NOTHING else await _node_at(page, blocking)
    if node is None or node.node_name in _BACKDROP:
        return (
            f"{locator!r} has a box at {where} and is not painted there, so the press would land on "
            f"the page behind it. Something took it out of sight without taking away its layout: an "
            f"ancestor clipping it that scrolling could not clear, or `pointer-events: none` on it or "
            f"above it."
        )
    return f"{locator!r} is covered at {where} by {describe(node)}"


async def _node_at(page, backend_node_id: int) -> dom_domain.Node | None:
    """The node in the way, or ``None`` if it has gone since the hit test."""
    try:
        return (await page.session.send(dom_domain.DescribeNode(backend_node_id=backend_node_id))).node
    except CDPError:
        return None
