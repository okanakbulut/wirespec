"""Watching traffic go by, and changing it in flight.

Two things, kept apart because they cost differently (§6.2).
``Network`` **observes**: it is cheap and produces a great many events, which is
why nothing decodes one until something is known to want it
([§3.5](#35-the-protocol-subset)). ``Fetch`` **intercepts**: it pauses every
matching request until wirespec answers, so it costs a round trip per request
and is enabled only once a route exists.

**The one trap here is the same shape as the screencast ack in
§16.2.** A ``Fetch.requestPaused`` handler must answer Chrome, and
the answer is a command that goes out on the connection the handler is being
dispatched *on*. Awaiting inside the handler deadlocks the read path. So handlers
run as tasks, off the read path, and the dispatch itself returns immediately.

A handler that raises must still answer, or the request stays paused for ever
and the page hangs on it -- with the traceback appearing somewhere else entirely,
if at all.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from wirespec.cdp import fetch as fetch_domain
from wirespec.cdp import network as network_domain

if TYPE_CHECKING:
    from wirespec.page import Page

__all__ = ["Request", "Response", "Route", "glob_to_pattern"]


class Request:
    """One request, as Chrome described it on the way out."""

    __slots__ = ("headers", "method", "post_data", "resource_type", "url")

    def __init__(self, url: str, method: str, headers: dict[str, str], post_data: str | None, kind: str) -> None:
        self.url = url
        self.method = method
        self.headers = headers
        self.post_data = post_data
        self.resource_type = kind

    def __repr__(self) -> str:
        return f"<Request {self.method} {self.url}>"


class Response:
    """One response, and the body Chrome still has a copy of."""

    __slots__ = ("_page", "_request_id", "headers", "request", "status", "url")

    def __init__(
        self, page: Page, request_id: str, url: str, status: int, headers: dict[str, str], request: Request
    ) -> None:
        self._page = page
        self._request_id = request_id
        self.url = url
        self.status = status
        self.headers = headers
        self.request = request

    def __repr__(self) -> str:
        return f"<Response {self.status} {self.url}>"

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    async def text(self, *, timeout: float = 15.0) -> str:
        """The body.

        Waits for the body to finish arriving first. ``responseReceived`` fires
        when the **headers** land, so reading straight after it fails with
        "No data found for resource with given identifier" -- and fails
        *intermittently*, depending on whether the body happened to have caught
        up, which is the worst way to find out about a race.

        Chrome keeps the body only until the buffer is reused, so this can still
        fail on a response read long after it arrived. It raises rather than
        returning "" so "empty body" and "too late to ask" stay distinguishable.
        """
        await self._page.loaded(self._request_id, timeout)
        body = await self._page.session.send(network_domain.GetResponseBody(request_id=self._request_id))
        if body.base64_encoded:
            import base64

            return base64.b64decode(body.body).decode("utf-8", "replace")
        return body.body

    async def json(self, *, timeout: float = 15.0) -> Any:
        return json.loads(await self.text(timeout=timeout))


class Route:
    """A paused request, and the three things that can be done with it."""

    __slots__ = ("_answered", "_page", "_request_id", "request")

    def __init__(self, page: Page, request_id: str, request: Request) -> None:
        self._page = page
        self._request_id = request_id
        self.request = request
        self._answered = False

    def __repr__(self) -> str:
        return f"<Route {self.request.method} {self.request.url}>"

    @property
    def answered(self) -> bool:
        return self._answered

    async def abort(self, error: str = "Failed") -> None:
        """Fail the request, as if the network had."""
        if self._answered:
            return
        self._answered = True
        await self._page.session.send(fetch_domain.FailRequest(request_id=self._request_id, error_reason=error))

    async def continue_(self) -> None:
        """Let it through unchanged. Named with the trailing underscore because
        ``continue`` is a keyword, and Playwright spells it the same way."""
        if self._answered:
            return
        self._answered = True
        await self._page.session.send(fetch_domain.ContinueRequest(request_id=self._request_id))

    async def fulfill(
        self,
        *,
        status: int = 200,
        body: str = "",
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> None:
        """Answer it here, without the server hearing about it."""
        if self._answered:
            return
        self._answered = True
        import base64

        entries = [fetch_domain.HeaderEntry(name="Content-Type", value=content_type)]
        entries += [fetch_domain.HeaderEntry(name=name, value=value) for name, value in (headers or {}).items()]
        await self._page.session.send(
            fetch_domain.FulfillRequest(
                request_id=self._request_id,
                response_code=status,
                response_headers=entries,
                body=base64.b64encode(body.encode()).decode(),
            )
        )


type Handler = Callable[[Route], Awaitable[None] | None]


def glob_to_pattern(glob: str) -> re.Pattern[str]:
    """A URL glob, as a regex.

    ``**/api`` is what a spec writes, and it has to match a path segment across
    slashes while ``*`` does not. ``fnmatch`` gets that wrong -- its ``*``
    crosses slashes too -- so the two are translated in order, longest first.
    """
    out: list[str] = []
    index = 0
    while index < len(glob):
        if glob.startswith("**", index):
            out.append(".*")
            index += 2
        elif glob[index] == "*":
            out.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(glob[index]))
            index += 1
    return re.compile("^" + "".join(out) + "$")


class Router:
    """The routes on one page, and the ``Fetch`` subscription behind them."""

    __slots__ = ("_page", "_routes", "_started", "_unsubscribe")

    def __init__(self, page: Page) -> None:
        self._page = page
        self._routes: list[tuple[re.Pattern[str], Handler]] = []
        self._started = False
        self._unsubscribe: Callable[[], None] | None = None

    async def add(self, glob: str, handler: Handler) -> None:
        self._routes.append((glob_to_pattern(glob), handler))
        if self._started:
            return
        # Lazily, because enabling Fetch costs every request on the page a round
        # trip whether or not anybody wanted to intercept it.
        self._started = True
        self._unsubscribe = self._page.session.on(fetch_domain.RequestPaused, self._paused)
        await self._page.session.send(fetch_domain.Enable())

    def _paused(self, event: fetch_domain.RequestPaused) -> None:
        # Off the read path. The handler has to answer Chrome, and the answer
        # goes out on the connection this callback is running on -- awaiting
        # here deadlocks it (§16.2).
        asyncio.get_running_loop().create_task(self._handle(event))

    async def _handle(self, event: fetch_domain.RequestPaused) -> None:
        request = Request(
            url=event.request.url,
            method=event.request.method,
            headers=dict(event.request.headers or {}),
            post_data=event.request.post_data,
            kind=event.resource_type,
        )
        route = Route(self._page, event.request_id, request)
        for pattern, handler in self._routes:
            if not pattern.match(request.url):
                continue
            try:
                result = handler(route)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 -- see below
                # A handler that raises must still answer, or the request stays
                # paused for ever and the page hangs on it -- with the traceback
                # surfacing somewhere else entirely, if at all. Letting it
                # through is the least surprising answer, and the exception is
                # reported so it is not swallowed.
                self._page.session.connection._report(
                    RuntimeError(f"route handler for {request.url} raised"),
                    "route handler raised; the request was continued",
                )
            if route.answered:
                return
        # No pattern matched, or nothing answered: let it through. A request
        # left paused is a page that hangs.
        await route.continue_()

    def close(self) -> None:
        """Stop listening. Deliberately not a ``Fetch.disable``: the page is
        going away, and a command to a target Chrome has already closed is an
        error rather than a tidy-up."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
