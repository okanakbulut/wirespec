"""`page.request` -- a request through the application's own door.

The requirement (§6.2) is that a spec asserting an API refuses
something puts the request through the same door the application uses, with the
same origin and the same cookies. Otherwise it is asserting about a different
request.

The spec used to say that meant "the page's own ``fetch``", which would mean
``Runtime.evaluate`` on an expression wirespec composed -- JavaScript of
wirespec's own, forbidden by §3.1. The mechanism was wrong; the requirement was
not.

**``Fetch`` originates the request.** It reads as an interception domain,
because that is how ``page.route`` uses it, but ``Fetch.continueRequest`` takes
``method``, ``postData`` *and* ``headers``. So any request that can be made to
pause can be rewritten into an arbitrary one before it leaves, and the browser
sends the result from its own network stack. The request to rewrite is a
navigation on a scratch page, which the browser makes with no JavaScript in
sight::

    Fetch.enable(exact url, requestStage=Request)  on the scratch session
    Page.navigate(url)                              not awaited -- see below
    requestPaused, no status  -> continueRequest(method, postData, headers,
                                                 interceptResponse=True)
    requestPaused, a status   -> the status and headers are on the event,
                                 Fetch.getResponseBody has the body
    Fetch.failRequest(Aborted)  after reading, so nothing ever commits

Measured (§9): method, body and headers all arrive, the context's cookies go out
and the response's ``Set-Cookie`` comes back into the jar, 404 and 500 are
responses rather than errors, and it costs about 10 ms over what the page's own
``fetch`` costs.
"""

import asyncio
import base64
import binascii
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from wirespec.cdp import fetch as fetch_domain
from wirespec.cdp import page as page_domain
from wirespec.cdp import target as target_domain
from wirespec.errors import WirespecError, WirespecTimeoutError
from wirespec.timeouts import DEFAULT_ACTION_TIMEOUT

if TYPE_CHECKING:
    from wirespec.browser import BrowserContext
    from wirespec.connection import Session

__all__ = ["APIRequestContext", "APIResponse", "RequestError"]

#: How many hops a redirect chain may take before it is called a loop. Chrome's
#: own limit is 20; a test suite that needs more than that has a bug, and the
#: number exists so the failure is "too many redirects" rather than a hang.
MAX_REDIRECTS = 20

#: The methods a redirect turns into a GET, per RFC 9110 §15.4. 307 and 308
#: exist precisely to *not* do this, so they are absent on purpose.
REDIRECTS_BECOME_GET = (301, 302, 303)


class RequestError(WirespecError):
    """A request that never produced a response.

    Separate from a 4xx or a 5xx, which are responses and are returned. This is
    the connection refused, the DNS failure, the redirect loop -- the cases
    where there is nothing to hand back and the only useful thing to say is
    Chrome's own ``errorText`` (§1, goal 4).
    """


class APIResponse:
    """What an API call answered. Status, headers and body, already read."""

    __slots__ = ("_body", "headers", "status", "url")

    def __init__(self, url: str, status: int, headers: dict[str, str], body: str) -> None:
        self.url = url
        self.status = status
        self.headers = headers
        self._body = body

    def __repr__(self) -> str:
        return f"<APIResponse {self.status} {self.url}>"

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    async def text(self) -> str:
        """The body.

        ``async`` although the body is already in hand, because the whole
        surface is awaited and one method that is not would be a trap of its
        own -- and because ``Response.text`` next to it genuinely does wait
        (§15.2).
        """
        return self._body

    async def json(self) -> Any:
        return json.loads(self._body)

    async def body(self) -> bytes:
        return self._body.encode()


class APIRequestContext:
    """Issues requests from inside the browser, on one context's cookie jar.

    One per ``BrowserContext``, because the cookie jar is what makes these
    requests the application's rather than somebody else's. The scratch page is
    created on the first call and never navigated anywhere the caller can see.
    """

    def __init__(self, context: BrowserContext) -> None:
        self.context = context
        self._session: Session | None = None
        self._target_id: str | None = None

    async def _scratch(self) -> Session:
        """The page whose navigations get rewritten.

        Deliberately *not* ``context.new_page``: that enables six domains, adds
        the page to ``context.pages`` where a test counting tabs would find it,
        and replays the context's routes onto it -- which would put a second
        ``Fetch`` subscriber on the very session this depends on owning alone.
        A bare target and a session is all that is needed.
        """
        if self._session is None:
            created = await self.context.connection.send(
                target_domain.CreateTarget(url="about:blank", browser_context_id=self.context.id)
            )
            self._target_id = created.target_id
            self._session = await self.context.connection.attach(created.target_id)
        return self._session

    async def dispose(self) -> None:
        """Close the scratch page, if one was ever opened."""
        if self._target_id is not None:
            await self.context.connection.send(target_domain.CloseTarget(target_id=self._target_id))
            self._session = None
            self._target_id = None

    async def get(self, url: str, **kwargs: Any) -> APIResponse:
        return await self.fetch(url, method="GET", **kwargs)

    async def post(self, url: str, **kwargs: Any) -> APIResponse:
        return await self.fetch(url, method="POST", **kwargs)

    async def put(self, url: str, **kwargs: Any) -> APIResponse:
        return await self.fetch(url, method="PUT", **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> APIResponse:
        return await self.fetch(url, method="PATCH", **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> APIResponse:
        return await self.fetch(url, method="DELETE", **kwargs)

    async def head(self, url: str, **kwargs: Any) -> APIResponse:
        return await self.fetch(url, method="HEAD", **kwargs)

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: Any = None,
        follow_redirects: bool = True,
        timeout: float = DEFAULT_ACTION_TIMEOUT,
    ) -> APIResponse:
        """One request, and its response.

        ``data`` that is not a ``str`` or ``bytes`` is serialised to JSON and
        the request declares itself ``application/json`` -- Playwright's rule,
        measured against 1.62, and the shape every API test in the validating
        suite sends. A string or bytes is passed through untouched, because a
        caller who wrote the bytes has already decided what they are.

        Redirects are followed **by hand**, one hop at a time. Letting Chrome
        follow a 3xx works, and the redirected request is at a different URL, so
        the exact intercept pattern no longer matches it -- it is not
        intercepted, it is *performed*, and the scratch page navigates to it for
        real (§8.14).
        """
        session = await self._scratch()
        target = self._resolve(url)
        sending = method.upper()
        body, headers = _serialised(data, headers)
        seen: list[str] = []

        try:
            async with asyncio.timeout(timeout):
                for _ in range(MAX_REDIRECTS):
                    seen.append(target)
                    answer = await _one_hop(session, target, sending, headers, body)
                    if not (300 <= answer.status < 400) or not follow_redirects:
                        return answer
                    location = _header(answer.headers, "location")
                    if location is None:
                        # A 3xx with no Location is not a redirect; it is a
                        # response, and pretending otherwise would hang.
                        return answer
                    target = urljoin(target, location)
                    if answer.status in REDIRECTS_BECOME_GET and sending != "HEAD":
                        sending, body = "GET", None
        except TimeoutError:
            # A bare TimeoutError names nothing -- not the method, not the URL,
            # not the hop it stalled on. Every timeout wirespec raises names
            # what it was waiting for (§1, goal 4), and this one
            # also has to be a `WirespecError`, or a suite writing
            # `except Error` around its API calls catches nothing.
            raise WirespecTimeoutError(
                f"{sending} {seen[-1] if seen else target}: no response after {timeout:g}s"
            ) from None

        raise RequestError(f"{method} {url}: more than {MAX_REDIRECTS} redirects: {' -> '.join(seen[:4])} ...")

    def _resolve(self, url: str) -> str:
        if urlsplit(url).scheme:
            return url
        if not self.context.base_url:
            raise ValueError(f"{url!r} is relative and this context has no base_url")
        return urljoin(self.context.base_url, url)


def _serialised(data: Any, headers: dict[str, str] | None) -> tuple[str | bytes | None, dict[str, str] | None]:
    """The body as bytes-or-text, and the headers that go with it.

    A ``Content-Type`` the caller set wins. Serialising is a convenience;
    deciding what the request *says it is* belongs to whoever wrote it.
    """
    if data is None or isinstance(data, str | bytes):
        return data, headers
    if _header(headers or {}, "content-type") is not None:
        return json.dumps(data), headers
    return json.dumps(data), {**(headers or {}), "Content-Type": "application/json"}


def _header(headers: dict[str, str], name: str) -> str | None:
    """Header lookup, case-insensitively, because HTTP header names are."""
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


async def _one_hop(
    session: Session,
    url: str,
    method: str,
    headers: dict[str, str] | None,
    data: str | bytes | None,
) -> APIResponse:
    """One request/response pair, with no redirect following."""
    loop = asyncio.get_running_loop()
    answered: asyncio.Future[APIResponse] = loop.create_future()

    def paused(event: fetch_domain.RequestPaused) -> None:
        # Off the read path, always. Answering from inside the handler would
        # deadlock the connection the answer has to go out on -- the same shape
        # as `page.route` (§6.5).
        loop.create_task(_answer(session, event, url, method, headers, data, answered))

    unsubscribe = session.on(fetch_domain.RequestPaused, paused)
    # Bound before the try, because the cleanup below refers to it and
    # `Fetch.enable` can raise -- which would replace a protocol error with an
    # unbound-name error and lose what actually went wrong.
    navigating: asyncio.Task[page_domain.NavigateResult] | None = None
    try:
        await session.send(
            fetch_domain.Enable(
                # The **exact** URL, never a glob. Measured with a catch-all:
                # the scratch page's own /favicon.ico paused too and was
                # rewritten with this call's method and body, so the server got
                # a second write nobody asked for (§8.14).
                patterns=[fetch_domain.RequestPattern(url_pattern=url, request_stage="Request")]
            )
        )
        # Not awaited: `Page.navigate` does not return until the navigation
        # commits or fails, and this one is going to be aborted on purpose
        # (§8.12).
        navigating = loop.create_task(session.send(page_domain.Navigate(url=url)))
        # Raced, because a request that never connects reaches the Request
        # stage and no further -- measured: four pauses and no response at all.
        # Without the race the call waits out its whole timeout and then has
        # nothing to say (§6.2).
        done, _ = await asyncio.wait((answered, navigating), return_when=asyncio.FIRST_COMPLETED)
        if answered in done:
            return answered.result()
        outcome = navigating.result()
        raise RequestError(f"{method} {url}: {outcome.error_text or 'the navigation ended with no response'}")
    finally:
        unsubscribe()
        if navigating is not None and not navigating.done():
            navigating.cancel()
        await session.send(fetch_domain.Disable())


async def _answer(
    session: Session,
    event: fetch_domain.RequestPaused,
    url: str,
    method: str,
    headers: dict[str, str] | None,
    data: str | bytes | None,
    answered: asyncio.Future[APIResponse],
) -> None:
    try:
        if event.response_status_code is None:
            payload = data.encode() if isinstance(data, str) else data
            await session.send(
                fetch_domain.ContinueRequest(
                    request_id=event.request_id,
                    method=method,
                    post_data=base64.b64encode(payload).decode() if payload else None,
                    headers=[fetch_domain.HeaderEntry(name=n, value=v) for n, v in (headers or {}).items()] or None,
                    intercept_response=True,
                )
            )
            return

        got = {entry.name: entry.value for entry in (event.response_headers or ())}
        if 300 <= event.response_status_code < 400:
            # A 3xx pause has no body to read: `getResponseBody` answers "Can
            # only get response body on requests captured after headers
            # received". The status and Location are on the event, which is
            # everything a redirect carries anyway (§6.2).
            body = ""
        else:
            reply = await session.send(fetch_domain.GetResponseBody(request_id=event.request_id))
            body = _decoded(reply.body) if reply.base64_encoded else reply.body

        # Abort *after* reading, so the response never commits, the scratch page
        # never moves and nothing renders.
        await session.send(fetch_domain.FailRequest(request_id=event.request_id, error_reason="Aborted"))
        if not answered.done():
            answered.set_result(APIResponse(url, event.response_status_code, got, body))
    except Exception as exc:  # noqa: BLE001 -- handed to the awaiting caller below
        if not answered.done():
            answered.set_exception(exc)


def _decoded(body: str) -> str:
    """A base64 body as text.

    Chrome base64-encodes a response body when it is not valid UTF-8, so this
    is where a binary response arrives. Decoding it as text is wrong for a PNG
    and right for everything a spec asserts on; ``APIResponse.body`` is where
    bytes would belong if something needs them.
    """
    try:
        return base64.b64decode(body).decode("utf-8", errors="replace")
    except binascii.Error as exc:
        raise RequestError(f"the response body was marked base64 and is not: {exc}") from exc
