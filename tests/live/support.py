"""The three things every live test does, so no test spells them differently."""

import asyncio
import os
import signal
import subprocess
from collections.abc import Callable, Coroutine
from typing import Any

from wirespec.cdp import page, runtime
from wirespec.connection import Session

#: Navigation is the slowest thing these tests do and CI is not fast.
NAV_TIMEOUT = 15.0


async def goto(session: Session, url: str, *, timeout: float = NAV_TIMEOUT) -> None:
    """Navigate and wait for load, failing on the *result* as well as on errors.

    ``Page.navigate`` reports a dead host in ``error_text`` rather than as a
    protocol error, so a helper that only awaits the load event hangs for the
    full timeout and then blames the wrong thing.
    """
    async with session.expect(page.LoadEventFired, timeout=timeout):
        result = await session.send(page.Navigate(url=url))
        assert result.error_text is None, f"navigating to {url}: {result.error_text}"


async def evaluate(session: Session, expression: str, *, user_gesture: bool = False) -> Any:
    """Run JavaScript and get the value, turning a page-side throw into a
    Python failure instead of a ``None`` that fails somewhere else later.

    ``user_gesture=True`` is what makes ``window.open`` and clipboard access
    work: without it Chrome treats the call as untrusted script and the
    popup blocker silently wins.
    """
    result = await session.send(
        runtime.Evaluate(expression=expression, return_by_value=True, await_promise=True, user_gesture=user_gesture)
    )
    assert result.exception_details is None, result.exception_details
    return result.result.value


async def handle_for(session: Session, expression: str) -> str:
    """A reference to a page object, left in the page."""
    result = await session.send(runtime.Evaluate(expression=expression))
    assert result.exception_details is None, result.exception_details
    assert result.result.object_id is not None, f"{expression} did not evaluate to an object"
    return result.result.object_id


async def drain_until[E](queue: asyncio.Queue[E], predicate: Callable[[E], bool], *, timeout: float = 10.0) -> E:
    """The first queued event matching ``predicate``, waiting for more if needed.

    ``Connection.expect`` cannot be used where the event may already have
    arrived before the assertion is reached -- a popup announces itself, and
    then changes title, faster than a test can subscribe between the two. A
    queue opened before the action never has that race; this drains it.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    seen: list[E] = []
    while True:
        while not queue.empty():
            event = queue.get_nowait()
            seen.append(event)
            if predicate(event):
                return event
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError(f"no event matched within {timeout}s; saw {len(seen)}: {seen!r}")
        try:
            event = await asyncio.wait_for(queue.get(), remaining)
        except TimeoutError:
            raise AssertionError(f"no event matched within {timeout}s; saw {len(seen)}: {seen!r}") from None
        seen.append(event)
        if predicate(event):
            return event


def kill_renderers(user_data_dir: str) -> list[int]:
    """SIGKILL every renderer belonging to one Chrome profile.

    The only way found to make ``Target.targetCrashed`` happen on purpose.
    ``chrome://crash`` does not crash a headless renderer -- the target simply
    sits at that URL -- and ``Page.crash`` takes the whole browser down with
    it, connection included. A killed renderer is the real thing: the browser
    survives and reports ``status="killed"``.

    The match is deliberately exact. A developer machine runs other things
    built on Chromium -- VS Code, Electron apps -- whose renderers are
    indistinguishable from Chrome's except by their profile, and this function
    sends SIGKILL. ``--user-data-dir`` is compared as a whole argument, so a
    profile path that happens to be a prefix of another cannot match it.
    """
    # -ww, or ps truncates the argument column to the terminal width and
    # --user-data-dir falls off the end -- whereupon this matches nothing and
    # the test that depends on it fails for a reason that is not the reason.
    listing = subprocess.run(["ps", "-eo", "pid,args", "-ww"], capture_output=True, text=True, check=False).stdout
    wanted = f"--user-data-dir={user_data_dir}"
    killed = []
    for line in listing.splitlines():
        pid_text, _, arguments = line.strip().partition(" ")
        if not pid_text.isdigit():
            continue
        argv = arguments.split()
        if "--type=renderer" not in argv or wanted not in argv:
            continue
        try:
            os.kill(int(pid_text), signal.SIGKILL)
        except OSError:
            continue
        killed.append(int(pid_text))
    return killed


async def eventually[T](
    probe: Callable[[], Coroutine[Any, Any, T]],
    expected: T,
    *,
    timeout: float = 5.0,
    interval: float = 0.05,
) -> T:
    """Poll ``probe`` until it returns ``expected``.

    For the handful of things Chrome applies asynchronously to the *renderer*
    after acknowledging the command -- clearing a device-metrics override is
    the one that bites -- where asserting immediately after the await is a race
    the test loses about half the time.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        seen = await probe()
        if seen == expected:
            return seen
        if loop.time() >= deadline:
            raise AssertionError(f"still {seen!r} rather than {expected!r} after {timeout}s")
        await asyncio.sleep(interval)
