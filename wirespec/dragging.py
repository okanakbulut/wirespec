"""Native HTML5 drag, which is not a sequence of mouse events.

§8.2. A ``draggable`` element is run by **the browser's own drag
session**, and synthetic mouse input does not start one. Pressing, moving and
releasing produces a page that saw a click and a drop target that never heard
anything -- with the failure surfacing far away, as a locator for something that
was supposed to appear after the drop.

The way in is ``Input.setInterceptDrags(true)``: the move that *would* have
begun a drag is reported back as ``Input.dragIntercepted`` carrying the
``dataTransfer`` payload, and from then on the gesture is driven with
``Input.dispatchDragEvent``.

Three things about that sequence are load-bearing, and each was expensive to
find:

* **``button`` must be named on the moves, not only on the press**
  (§8.1). Without it Chrome's drag controller never recognises the
  gesture and ``dragIntercepted`` never fires. ``Mouse`` already does this for
  every event, which is why this module does not have to.
* **``dragEnter`` before ``dragOver``, in that order**, or the drop target never
  learns the drag arrived.
* **Releasing during a drag is a ``drop``, not a ``mouseReleased``.** The
  browser consumed the button when it took the drag over, so sending a mouse
  release instead leaves the drag hanging and the drop never happens.

Also worth knowing before assuming: check which drag the application actually
uses. Pointer-based libraries and native HTML5 drag need completely different
handling, and the events look similar until they do not -- a pointer-based one
never produces ``dragIntercepted`` at all, which is what the timeout here says.
"""

import asyncio
from typing import TYPE_CHECKING

from wirespec.cdp import input as input_domain
from wirespec.errors import WirespecTimeoutError

if TYPE_CHECKING:
    from wirespec.page import Page

__all__ = ["drag"]

#: How far to move before Chrome decides a drag has begun. A few pixels is
#: enough; the threshold is small and the walk is what matters.
_NUDGE = 6

#: How long to wait for Chrome to report a drag it decided to take over. Short,
#: because it either happens on the first qualifying move or the element is not
#: natively draggable at all.
_INTERCEPT_TIMEOUT = 2.0


async def drag(
    page: Page,
    source: tuple[float, float],
    target: tuple[float, float],
    *,
    steps: int = 5,
) -> None:
    """Drag from one point to another, natively."""
    loop = asyncio.get_running_loop()
    intercepted: asyncio.Future[input_domain.DragIntercepted] = loop.create_future()

    def caught(event: input_domain.DragIntercepted) -> None:
        if not intercepted.done():
            intercepted.set_result(event)

    # Subscribed and enabled before the press, or the very first qualifying move
    # is a report nobody receives.
    unsubscribe = page.session.on(input_domain.DragIntercepted, caught)
    await page.session.send(input_domain.SetInterceptDrags(enabled=True))
    try:
        start_x, start_y = source
        await page.mouse.move(start_x, start_y)
        await page.mouse.down()
        # One nudge is what makes Chrome decide. `Mouse` names the held button
        # on the move, which is the whole of §8.1.
        await page.mouse.move(start_x + _NUDGE, start_y + _NUDGE)
        try:
            async with asyncio.timeout(_INTERCEPT_TIMEOUT):
                event = await intercepted
        except TimeoutError:
            raise WirespecTimeoutError(
                f"dragging from {start_x:.0f},{start_y:.0f}: Chrome never reported a native drag. "
                f"The element is probably not `draggable` -- a pointer-based drag library needs "
                f"mouse events instead, and the two look similar until they do not "
                f"(§8.2)."
            ) from None

        data = event.data
        end_x, end_y = target
        # dragEnter before dragOver, in that order, or the drop target never
        # learns the drag arrived.
        for kind in ("dragEnter", "dragOver"):
            await _dispatch(page, kind, end_x, end_y, data)
        # And the walk between them, which is the point of a drag: a target that
        # highlights on dragover needs to have seen one at its own coordinates.
        for step in range(1, steps + 1):
            fraction = step / steps
            await _dispatch(
                page,
                "dragOver",
                start_x + (end_x - start_x) * fraction,
                start_y + (end_y - start_y) * fraction,
                data,
            )
        # A drop, not a mouseReleased: the browser consumed the button when it
        # took the drag over.
        await _dispatch(page, "drop", end_x, end_y, data)
    finally:
        unsubscribe()
        await page.session.send(input_domain.SetInterceptDrags(enabled=False))


async def _dispatch(page: Page, kind: str, x: float, y: float, data: input_domain.DragData) -> None:
    await page.session.send(input_domain.DispatchDragEvent(type=kind, x=x, y=y, data=data))
