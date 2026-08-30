"""When to look again, and what to say when looking again stops helping.

This is the whole of the auto-waiting behaviour. Every assertion and every
action is a ``read`` returning what the page currently says and an ``accept``
deciding whether that is good enough yet; this runs the first until the second
is satisfied (§5.1)::

    read -> accept?   -> done
         -> deadline? -> raise, quoting the last reading
         -> await (the page changed OR the interval elapsed), then read again

**The sleep is a backstop, not the mechanism.** Chrome pushes DOM mutations, so
the loop waits on a notification and re-reads when one arrives: measured
mutation-to-wakeup 0.65 ms, against ~50 ms of average lag for a poll backing off
to a 100 ms ceiling. That turns a five-second wait from ~50 reads into two or
three, which matters far more here than it would for Playwright -- Playwright
polls *inside* the page, where a re-check costs nothing, and since
§3.4 every wirespec re-check is a round trip.

**Push cannot be the only mechanism.** DOM events say the DOM changed; they do
not say the *rendering* changed. A rule inserted into a stylesheet moves what is
on screen with no DOM event of any kind, and the 60 ms an element spends fading
in produces exactly one event at the start and none while it moves. So:
notification is a hint that arrives quickly and is allowed to be incomplete; the
interval is a guarantee that is allowed to be slow. Because the hint carries the
common case, the interval can be a flat 100 ms rather than a backoff tuned to
trade latency against load -- there is nothing left to trade.

Two properties rank above all of that:

* **Read first, wait second.** The overwhelmingly common case is an assertion
  already true, and it must cost exactly one round trip. A loop that waits
  before its first look adds its interval to every passing assertion in the
  suite. Subscribe before the first read, though, or a change landing between
  the read and the subscription is a wakeup nobody receives.
* **Keep the last reading.** ``expected 2, last saw 0`` has usually already
  answered the question that ``timed out`` sends someone to the browser for.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from wirespec.errors import NODE_GONE, CDPError, WirespecTimeoutError

if TYPE_CHECKING:
    from wirespec.page import Page

__all__ = ["MUTATION_EVENTS", "POLL_INTERVAL", "poll"]

#: The flat backstop. Not a backoff: the push carries the common case.
POLL_INTERVAL = 0.1

#: Every ``DOM`` event that means "something about the document changed".
#: Subscribed by name and never decoded (§5.1) -- a waiter needs to
#: know that something happened, never what.
#:
#: The list is deliberately over-inclusive, which is the safe direction: a
#: spurious wakeup costs one re-read, and a missing one costs the difference
#: between 0.65 ms and the full 100 ms interval.
MUTATION_EVENTS = (
    "DOM.attributeModified",
    "DOM.attributeRemoved",
    "DOM.characterDataModified",
    "DOM.childNodeCountUpdated",
    "DOM.childNodeInserted",
    "DOM.childNodeRemoved",
    "DOM.documentUpdated",
    "DOM.inlineStyleInvalidated",
    "DOM.setChildNodes",
)


async def poll[T](
    page: Page,
    read: Callable[[], Awaitable[T]],
    accept: Callable[[T], bool],
    describe: Callable[[T], str],
    timeout: float,
) -> T:
    """Read until ``accept``, then return the reading that satisfied it."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    #: What the last read saw, and whether there has been one. A read whose
    #: node vanished has nothing to report, and a loop that only ever hit those
    #: must say *that* rather than quote a reading it never took.
    last: Any = None
    seen = False
    changed = asyncio.Event()
    # Subscribed before the first read. A mutation landing between the read and
    # the subscription would otherwise be a wakeup nobody receives, and the loop
    # would wait out its whole interval for news it already had.
    unsubscribe = page.session.signal(MUTATION_EVENTS, changed.set)
    try:
        while True:
            changed.clear()
            try:
                last, seen = await read(), True
            except CDPError as exc:
                # The node this read resolved was gone by the time Chrome was
                # asked about it -- an application re-rendering between two of
                # the round trips the read is made of. Not an error: the next
                # read resolves it afresh, which is what Playwright does on
                # every attempt anyway (§8.23). Every other CDP
                # error is a real one and goes straight up.
                if NODE_GONE not in exc.message:
                    raise
                if loop.time() >= deadline:
                    said = describe(last) if seen else "the element kept being replaced while it was read"
                    raise WirespecTimeoutError(f"{said}\n\n(waited {timeout:g}s)") from exc
            else:
                if accept(last):
                    return last
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise WirespecTimeoutError(f"{describe(last)}\n\n(waited {timeout:g}s)")
            remaining = max(deadline - loop.time(), 0.0)
            try:
                # Whichever comes first: Chrome says the page changed, or the
                # backstop expires because it changed in a way Chrome does not
                # report -- a stylesheet edit, a running transition.
                async with asyncio.timeout(min(POLL_INTERVAL, remaining)):
                    await changed.wait()
            except TimeoutError:
                # The interval elapsed. That is the backstop doing its job, not
                # a failure: the deadline above is the only thing that gives up.
                pass
    finally:
        unsubscribe()
