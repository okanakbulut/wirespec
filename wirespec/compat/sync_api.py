"""``playwright.sync_api``: the async surface, driven from a thread with no loop.

§15.2 calls this the largest single piece of the compatibility work
and the one most likely to leak, and names the way it leaks: **a sync facade
that deadlocks the first time a route handler calls back into the page is worse
than no sync facade.**

**A mirror, not a second implementation.** Every class here delegates to
``wirespec.compat.async_api``, so Playwright's semantics -- the millisecond
conversion, the viewport dict, which gaps refuse -- are decided in exactly one
place and can only be wrong in one place. What lives here is the threading, and
nothing else.

Two directions of traffic, and they are not the same problem:

**Sync calling async.** A dedicated thread runs an event loop for the life of
the ``sync_playwright()`` block; each call is ``run_coroutine_threadsafe`` and a
``.result()``. Chosen over Playwright's greenlet bridge because greenlet is a
second runtime dependency, which the property §1 ranks first does
not have room for. It is the same design as anyio's blocking portal, in about
forty lines of standard library.

**Async calling sync** -- the callback direction, and the one that deadlocks.
A route handler is the suite's own synchronous function, called from the loop
while the loop is busy inside ``goto``; everything the handler then does
(``route.request.url``, ``route.fulfill``) has to get back onto that loop. Run
the handler *on* the loop thread and it waits for a loop that is waiting for it.
So handlers go to a thread pool -- ``loop.run_in_executor`` -- which is
Starlette's ``run_in_threadpool`` and exactly the shape this needs.
"""

import asyncio
import contextlib
import inspect
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from wirespec.compat import async_api
from wirespec.compat.async_api import Error, TimeoutError

__all__ = ["Error", "TimeoutError", "expect", "sync_playwright"]

#: The wrapped types. Anything coming back from the async layer that is one of
#: these is re-wrapped, so a sync suite never gets its hands on a coroutine.
_WRAPPED = (
    async_api.Browser,
    async_api.BrowserType,
    async_api.FrameLocator,
    async_api.BrowserContext,
    async_api.Dialog,
    async_api.Locator,
    async_api.Page,
    async_api.Request,
    async_api.Response,
    async_api.Route,
    async_api._Caught,
    async_api._MilliAssertions,
)


class _Portal:
    """A loop on a thread of its own, and the way onto it.

    One per ``sync_playwright()`` block. The loop is *not* the caller's --
    there is no caller's loop, which is the whole point -- so it is created
    here, run until the block ends, and closed.
    """

    __slots__ = ("_events", "_executor", "_listening", "_loop", "_ready", "_thread")

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="wirespec-sync", daemon=True)
        # Route handlers, not driver work. Bounded because an unbounded pool
        # hides a handler that never returns; four is enough for concurrent
        # routes on one page and small enough that a leak shows up as a hang in
        # tests rather than as a thousand threads in production.
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="wirespec-route")
        # Event listeners get **one** thread, so `page.on` handlers run in the
        # order the events arrived. Playwright's do, and a suite appending to a
        # list from one is entitled to assume it.
        self._events = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wirespec-events")
        self._listening: list[Any] = []
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def run(self, coroutine: Any) -> Any:
        """Run a coroutine on the loop thread and wait here for its answer.

        Then wait for any event listeners it set off. Without that, ``goto``
        returns before the ``page.on("request")`` handler its own navigation
        triggered has run, and the assertion on the next line is a race the
        suite did not write. Waiting here cannot deadlock: the listeners are on
        another thread and the loop is free for whatever they call back into.
        """
        if threading.current_thread() is self._thread:
            # The deadlock §15.2 names, caught by its shape rather
            # than by waiting for it. Something called back into the portal
            # *from* the loop thread -- a handler dispatched inline instead of
            # into the pool -- and the loop is now waiting for itself. There is
            # no timeout that helps: the loop is the thing that would fire it,
            # and every later call, including teardown, hangs behind this one.
            coroutine.close()
            raise RuntimeError(
                "a wirespec.compat.sync_api call was made from the event loop thread, which cannot "
                "complete: the loop is waiting for this call. A handler must run in the thread pool "
                "(§15.2)."
            )
        answer = asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()
        pending, self._listening = self._listening, []
        for future in pending:
            future.result()
        return answer

    def close(self) -> None:
        self._events.shutdown(wait=False)
        self._executor.shutdown(wait=False)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()

    def handler(self, handler: Callable[..., Any]) -> Callable[..., Any]:
        """The suite's sync callback, as something the loop can await.

        **In the thread pool, always.** Awaiting the handler on the loop thread
        would be correct only for a handler that touches nothing -- and the
        first thing any route handler does is read ``route.request`` or call
        ``route.fulfill``, both of which come straight back here. That is a
        deadlock, and it is the one §15.2 says to write down before
        writing the code.
        """

        async def called(*args: Any) -> None:
            wrapped = [_wrap(self, arg) for arg in args]
            await self._loop.run_in_executor(self._executor, lambda: handler(*wrapped))

        return called

    def listener(self, handler: Callable[..., Any]) -> Callable[..., Any]:
        """A ``page.on`` handler. Called from the read path, never awaited.

        So it cannot be a coroutine -- nothing would run it -- and it must not
        run inline either, because a handler that touches the page would then be
        on the loop thread it needs. Submitted to the events thread and awaited
        by the next ``run``, which is what keeps "goto, then assert on what the
        handler saw" from being a race.
        """

        def called(*args: Any) -> None:
            wrapped = [_wrap(self, arg) for arg in args]
            self._listening.append(self._events.submit(lambda: handler(*wrapped)))

        return called

    def predicate(self, handler: Callable[..., Any]) -> Callable[..., Any]:
        """An ``expect_response`` predicate: it has to answer, now, with a bool.

        Runs on the loop thread, so a predicate that calls back into the page
        would deadlock. Playwright's are one comparison on a URL, and anything
        more belongs in the block rather than in the predicate.
        """

        def called(*args: Any) -> Any:
            return handler(*[_wrap(self, arg) for arg in args])

        return called


class _Sync:
    """One async-layer object, addressed without ``await``.

    ``__getattr__`` is doing the work, and doing it by delegation on purpose:
    an attribute the async layer refuses raises **its** ``NotImplementedError``,
    naming the feature, before anything here sees it. A sync facade with its own
    list of what exists would be a second list to keep in step
    (§15.4).
    """

    __slots__ = ("_inner", "_portal")

    def __init__(self, portal: _Portal, inner: Any) -> None:
        self._portal = portal
        self._inner = inner

    def __repr__(self) -> str:
        return repr(self._inner)

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if inspect.isawaitable(attribute):
            # A property whose value is awaitable -- `expect_popup(...).value`
            # is the one. Playwright's async API spells it `await info.value`
            # and its sync API spells it `info.value`, so the await happens
            # here: the caller is on a thread with no loop to do it on.
            return _wrap(self._portal, self._portal.run(attribute))
        if not callable(attribute):
            return _wrap(self._portal, attribute)
        return _method(self._portal, name, attribute)


#: How the async layer treats a callable argument, which decides where the
#: sync one has to run. Three different answers, and using the wrong one is
#: silent every time: a route handler on the loop thread deadlocks, a listener
#: turned into a coroutine never runs, and a predicate turned into either
#: returns something that is not a bool and matches every response.
_AWAITED = {"route", "on:dialog"}
_LISTENED = {"on", "on:request", "on:response"}
_ASKED = {"expect_request", "expect_response"}


def _method(portal: _Portal, name: str, attribute: Callable[..., Any]) -> Callable[..., Any]:
    def call(*args: Any, **kwargs: Any) -> Any:
        # `page.on` takes two kinds of callback and they are not
        # interchangeable, so the event name is part of the question. A request
        # listener is never awaited and may run whenever it likes; a dialog
        # handler is awaited, because the page is stopped until it answers and
        # wirespec's default dismiss goes out the moment it returns
        # (§8.20). Dispatched as a listener, the suite's `accept`
        # would arrive after the dialog had already closed.
        kind = f"{name}:{args[0]}" if name == "on" and args and isinstance(args[0], str) else name
        args = tuple(_unwrap(portal, kind, arg) for arg in args)
        kwargs = {key: _unwrap(portal, kind, value) for key, value in kwargs.items()}
        result = attribute(*args, **kwargs)
        if inspect.isawaitable(result):
            return _wrap(portal, portal.run(result))
        # On the **type**, not the instance: `hasattr` on one of these wrappers
        # goes through its `__getattr__`, which refuses by design -- so asking
        # the instance turns every return value into a NotImplementedError
        # about `__aenter__`. Python looks special methods up on the type
        # anyway, so this is also the more correct question.
        if hasattr(type(result), "__aenter__"):
            # `expect_request`/`expect_response`: an async context manager,
            # which a sync suite writes as a plain `with`. Both halves have to
            # cross to the loop thread, and the block in between runs here.
            return _SyncContext(portal, result)
        return _wrap(portal, result)

    return call


class _SyncContext:
    """An async context manager, entered and left from this thread."""

    __slots__ = ("_inner", "_portal")

    def __init__(self, portal: _Portal, inner: Any) -> None:
        self._portal = portal
        self._inner = inner

    def __enter__(self) -> Any:
        return _wrap(self._portal, self._portal.run(self._inner.__aenter__()))

    def __exit__(self, *exc: object) -> None:
        self._portal.run(self._inner.__aexit__(*exc))


def _wrap(portal: _Portal, value: Any) -> Any:
    if isinstance(value, _WRAPPED):
        return _Sync(portal, value)
    if isinstance(value, list):
        return [_wrap(portal, item) for item in value]
    return value


def _unwrap(portal: _Portal, method: str, value: Any) -> Any:
    """A sync object back into the async one, and a callback into whichever
    shape the method that receives it is going to use."""
    if isinstance(value, _Sync):
        return value._inner
    if callable(value) and not isinstance(value, type):
        if method in _AWAITED:
            return portal.handler(value)
        if method in _LISTENED:
            return portal.listener(value)
        if method in _ASKED:
            return portal.predicate(value)
    return value


class _Playwright(_Sync):
    """What ``sync_playwright()`` yields. Closing it stops the loop thread."""

    __slots__ = ()

    def close(self) -> None:
        self._portal.close()


@contextlib.contextmanager
def sync_playwright() -> Iterator[_Playwright]:
    """``playwright.sync_api.sync_playwright()``.

    The loop thread lives exactly as long as this block. A suite that leaves the
    block and keeps a page is holding a page whose loop has stopped, which
    raises rather than hanging -- the same shape as using a closed browser.
    """
    portal = _Portal()
    try:
        yield _Playwright(portal, async_api.Playwright())
    finally:
        portal.close()


def expect(subject: Any, **kwargs: Any) -> Any:
    """``playwright.sync_api.expect``."""
    if not isinstance(subject, _Sync):
        raise TypeError(f"expect() wants a sync Page or Locator, got {type(subject).__name__}")
    return _Sync(subject._portal, async_api.expect(subject._inner, **kwargs))
