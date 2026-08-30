"""``playwright.async_api``, spelled for wirespec.

Import-compatible with Playwright's async API for the surface
§15.1 defines as in scope. What is deliberately *not* here -- Firefox, WebKit,
codegen, trace files, browser downloads -- is out permanently and is listed
there.
"""

import contextlib
import weakref
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from typing import Any

from wirespec import browser as native
from wirespec import expect as native_expect
from wirespec.cdp import network as network_domain
from wirespec.dialogs import Dialog as NativeDialog
from wirespec.errors import WirespecError, WirespecTimeoutError

# `playwright.async_api.Mouse` and `Keyboard`, re-exported **unwrapped** -- the
# only two names on this surface that are. Neither takes a timeout, so there
# are no units to convert, and the method names already match. A wrapper would
# exist only to refuse gaps by name, and it would cost the thing a suite
# actually imports these for: `def drag(mouse: Mouse)` is a true annotation
# only if this is the class `page.mouse` hands back. Found by the pilot suite,
# whose page objects annotate with it (§15.3).
from wirespec.input import Keyboard, Mouse
from wirespec.locator import FrameLocator as NativeFrameLocator
from wirespec.locator import Locator as NativeLocator
from wirespec.network import Route as NativeRoute
from wirespec.page import Page as NativePage
from wirespec.sentinels import NO_ARGUMENT

__all__ = [
    "APIRequestContext",
    "APIResponse",
    "Browser",
    "BrowserContext",
    "Dialog",
    "Error",
    "Keyboard",
    "Locator",
    "Mouse",
    "Page",
    "Playwright",
    "Request",
    "Response",
    "Route",
    "TimeoutError",
    "async_playwright",
    "expect",
]

#: Playwright's own default action timeout, in milliseconds. Kept rather than
#: reused from wirespec's 15 s: a suite that relies on a slow page settling
#: inside 30 s passes under Playwright, and a compatibility layer that quietly
#: halved it would fail that suite for a reason nobody could find.
DEFAULT_TIMEOUT_MS = 30_000

#: Playwright's default for `expect`, which happens to agree with wirespec's.
DEFAULT_EXPECT_MS = 5_000


#: ``playwright.async_api.Error``. An **alias**, not a subclass: a subclass
#: would be a parallel hierarchy that nothing wirespec raises belongs to, so
#: ``except Error`` would catch nothing and ``issubclass(TimeoutError, Error)``
#: would be false -- both silently. Matching Playwright's *message* text is
#: explicitly out of scope (§15.1); matching which exceptions its
#: names catch is not.
Error = WirespecError


#: ``playwright.async_api.TimeoutError``. wirespec's already subclasses the
#: builtin ``TimeoutError`` as well, so all three spellings catch.
TimeoutError = WirespecTimeoutError


def _seconds(milliseconds: float | None, fallback: float) -> float:
    """Playwright's milliseconds as wirespec's seconds.

    The single most consequential line in this module, and the one whose bug
    would be quietest: a suite whose timeouts silently became a thousand times
    longer still *passes*. It just stops failing when it should, and nobody
    finds out until a broken build sits green.
    """
    return fallback / 1000.0 if milliseconds is None else milliseconds / 1000.0


#: One compat wrapper per native object, so ``page.context is context`` and
#: ``page in context.pages`` answer the way a Playwright suite expects. A fresh
#: wrapper each time is not wrong in any way a test can point at until a suite
#: writes ``is``, and then it reads as a bug in the suite.
#:
#: Weak, so a closed context or page is collected as it would have been. Kept
#: here rather than as an attribute on the native object, because the driver
#: has no business knowing this layer exists.
_WRAPPERS: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()


def _wrapper[T](native: Any, make: Callable[[], T]) -> T:
    """The wrapper for ``native``, made once and remembered."""
    found = _WRAPPERS.get(native)
    if found is None:
        found = _WRAPPERS[native] = make()
    return found


def _refuse(owner: object, name: str) -> Any:
    """What every wrapper does with an attribute it does not have.

    §15.4: a gap must raise and name itself, on the line that used
    it. The alternative -- forwarding unknown attributes to the wrapped object
    -- would work often enough to be trusted and then hand back a wirespec
    method with wirespec's units.
    """
    raise NotImplementedError(
        f"{type(owner).__name__}.{name} is part of Playwright's API and is not built in wirespec "
        f"(§15.1 and §12). It refuses here rather than doing something subtly different."
    )


class FrameLocator:
    """``playwright.async_api.FrameLocator``.

    A builder, not a Locator -- there is nothing to await on it -- so this is
    the one wrapper in the module with no timeout to convert. It exists anyway,
    rather than handing back wirespec's own, because ``.locator(...)`` on it
    has to produce a **wrapped** Locator: the native one takes seconds, and a
    suite that got hold of it would be passing milliseconds to it
    (§15.2).
    """

    __slots__ = ("_frame",)

    def __init__(self, frame: NativeFrameLocator) -> None:
        self._frame = frame

    def __repr__(self) -> str:
        return repr(self._frame)

    def __getattr__(self, name: str) -> Any:
        _refuse(self, name)

    def locator(self, selector: str, **kwargs: Any) -> Locator:
        found = Locator(self._frame.locator(selector))
        return found.filter(**kwargs) if kwargs else found

    def get_by_role(self, role: str, *, name: Any = None, exact: bool = False) -> Locator:
        return Locator(self._frame.get_by_role(role, name=name, exact=exact))

    def get_by_text(self, text: Any, *, exact: bool = False) -> Locator:
        return Locator(self._frame.get_by_text(text, exact=exact))

    def get_by_label(self, text: Any, *, exact: bool = False) -> Locator:
        return Locator(self._frame.get_by_label(text, exact=exact))

    def get_by_placeholder(self, text: Any, *, exact: bool = False) -> Locator:
        return Locator(self._frame.get_by_placeholder(text, exact=exact))

    def get_by_test_id(self, value: Any) -> Locator:
        return Locator(self._frame.get_by_test_id(value))

    def frame_locator(self, selector: str) -> FrameLocator:
        return FrameLocator(self._frame.frame_locator(selector))

    @property
    def owner(self) -> Locator:
        return Locator(self._frame.owner)


class Locator:
    """``playwright.async_api.Locator``."""

    __slots__ = ("_locator",)

    def __init__(self, locator: NativeLocator) -> None:
        self._locator = locator

    def __repr__(self) -> str:
        return repr(self._locator)

    def __getattr__(self, name: str) -> Any:
        _refuse(self, name)

    # -- narrowing -----------------------------------------------------------

    def locator(self, selector: str, **kwargs: Any) -> Locator:
        found = Locator(self._locator.locator(selector))
        return found.filter(**kwargs) if kwargs else found

    def get_by_role(self, role: str, *, name: Any = None, exact: bool = False) -> Locator:
        return Locator(self._locator.get_by_role(role, name=name, exact=exact))

    def get_by_text(self, text: Any, *, exact: bool = False) -> Locator:
        return Locator(self._locator.get_by_text(text, exact=exact))

    def get_by_label(self, text: Any, *, exact: bool = False) -> Locator:
        return Locator(self._locator.get_by_label(text, exact=exact))

    def get_by_placeholder(self, text: Any, *, exact: bool = False) -> Locator:
        return Locator(self._locator.get_by_placeholder(text, exact=exact))

    def get_by_test_id(self, value: Any) -> Locator:
        return Locator(self._locator.get_by_test_id(value))

    def filter(self, *, has_text: Any = None, has_not_text: Any = None) -> Locator:
        return Locator(self._locator.filter(has_text=has_text, has_not_text=has_not_text))

    def nth(self, index: int) -> Locator:
        return Locator(self._locator.nth(index))

    def or_(self, other: Locator) -> Locator:
        return Locator(self._locator.or_(other._locator))

    def frame_locator(self, selector: str) -> FrameLocator:
        return FrameLocator(self._locator.frame_locator(selector))

    @property
    def content_frame(self) -> FrameLocator:
        return FrameLocator(self._locator.content_frame)

    @property
    def page(self) -> Page:
        """The page this locator was built from.

        How a page object reaches the page it was handed a locator for -- 120
        of the pilot suite's 229 specs do exactly that (§15.3).
        """
        return _wrapper(self._locator.page, lambda: Page(self._locator.page))

    @property
    def first(self) -> Locator:
        return Locator(self._locator.first)

    @property
    def last(self) -> Locator:
        return Locator(self._locator.last)

    # -- reading -------------------------------------------------------------

    async def count(self) -> int:
        return await self._locator.count()

    async def all(self) -> list[Locator]:
        return [Locator(one) for one in await self._locator.all()]

    async def text_content(self, *, timeout: float | None = None) -> str:
        return await self._locator.text_content(timeout=_seconds(timeout, DEFAULT_EXPECT_MS))

    async def all_text_contents(self) -> list[str]:
        return await self._locator.all_text_contents()

    async def inner_text(self, *, timeout: float | None = None) -> str:
        return await self._locator.inner_text(timeout=_seconds(timeout, DEFAULT_EXPECT_MS))

    async def all_inner_texts(self) -> list[str]:
        return await self._locator.all_inner_texts()

    async def get_attribute(self, name: str, *, timeout: float | None = None) -> str | None:
        return await self._locator.get_attribute(name, timeout=_seconds(timeout, DEFAULT_EXPECT_MS))

    async def input_value(self, *, timeout: float | None = None) -> str:
        return await self._locator.input_value(timeout=_seconds(timeout, DEFAULT_EXPECT_MS))

    async def is_checked(self, *, timeout: float | None = None) -> bool:
        return await self._locator.is_checked(timeout=_seconds(timeout, DEFAULT_EXPECT_MS))

    async def is_enabled(self, *, timeout: float | None = None) -> bool:
        return await self._locator.is_enabled(timeout=_seconds(timeout, DEFAULT_EXPECT_MS))

    async def is_disabled(self, *, timeout: float | None = None) -> bool:
        return not await self._locator.is_enabled(timeout=_seconds(timeout, DEFAULT_EXPECT_MS))

    async def is_editable(self, *, timeout: float | None = None) -> bool:
        return await self._locator.is_editable(timeout=_seconds(timeout, DEFAULT_EXPECT_MS))

    async def is_visible(self, *, timeout: float | None = None) -> bool:
        return await self._locator.is_visible()

    async def is_hidden(self, *, timeout: float | None = None) -> bool:
        return not await self._locator.is_visible()

    async def bounding_box(self, *, timeout: float | None = None) -> dict[str, float] | None:
        return await self._locator.bounding_box(timeout=_seconds(timeout, DEFAULT_EXPECT_MS))

    async def evaluate(self, expression: str, arg: Any = NO_ARGUMENT, *, timeout: float | None = None) -> Any:
        return await self._locator.evaluate(expression, arg, timeout=_seconds(timeout, DEFAULT_EXPECT_MS))

    async def evaluate_all(self, expression: str) -> Any:
        return await self._locator.evaluate_all(expression)

    # -- acting --------------------------------------------------------------

    async def click(self, *, force: bool = False, timeout: float | None = None, **rest: Any) -> None:
        _only(rest, "click")
        await self._locator.click(force=force, timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS))

    async def dblclick(self, *, force: bool = False, timeout: float | None = None, **rest: Any) -> None:
        _only(rest, "dblclick")
        await self._locator.dblclick(force=force, timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS))

    async def hover(self, *, force: bool = False, timeout: float | None = None, **rest: Any) -> None:
        _only(rest, "hover")
        await self._locator.hover(force=force, timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS))

    async def fill(self, value: str, *, timeout: float | None = None, **rest: Any) -> None:
        _only(rest, "fill")
        await self._locator.fill(value, timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS))

    async def type(self, text: str, *, timeout: float | None = None, **rest: Any) -> None:
        _only(rest, "type")
        await self._locator.type(text, timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS))

    async def press(self, key: str, *, timeout: float | None = None, **rest: Any) -> None:
        _only(rest, "press")
        await self._locator.press(key, timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS))

    async def set_input_files(
        self,
        files: Any,
        *,
        timeout: float | None = None,
        **rest: Any,
    ) -> None:
        _only(rest, "set_input_files")
        await self._locator.set_input_files(files, timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS))

    async def select_option(
        self,
        value: str | list[str] | None = None,
        *,
        label: str | list[str] | None = None,
        index: int | list[int] | None = None,
        timeout: float | None = None,
        **rest: Any,
    ) -> list[str]:
        _only(rest, "select_option")
        return await self._locator.select_option(
            value, label=label, index=index, timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS)
        )

    async def focus(self, *, timeout: float | None = None) -> None:
        await self._locator.focus(timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS))

    async def scroll_into_view_if_needed(self, *, timeout: float | None = None) -> None:
        await self._locator.scroll_into_view_if_needed(timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS))

    async def drag_to(self, target: Locator, *, timeout: float | None = None, **rest: Any) -> None:
        _only(rest, "drag_to")
        await self._locator.drag_to(target._locator, timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS))


def _only(rest: Mapping[str, Any], method: str) -> None:
    """Refuse the keyword arguments this layer does not honour.

    Accepting ``position=`` or ``modifiers=`` and ignoring them is exactly the
    silent divergence §15.4 forbids: the click happens, in the
    wrong place, and the spec passes.
    """
    if rest:
        raise NotImplementedError(
            f"{method}({', '.join(sorted(rest))}=...) is not supported by wirespec. "
            f"It refuses rather than ignoring the argument and clicking somewhere else."
        )


class Request:
    """``playwright.async_api.Request``, read-only."""

    __slots__ = ("_request",)

    def __init__(self, request: Any) -> None:
        self._request = request

    def __getattr__(self, name: str) -> Any:
        _refuse(self, name)

    @property
    def url(self) -> str:
        return self._request.url

    @property
    def method(self) -> str:
        return self._request.method

    @property
    def resource_type(self) -> str:
        return self._request.resource_type

    @property
    def post_data(self) -> str | None:
        return self._request.post_data

    async def all_headers(self) -> dict[str, str]:
        return dict(self._request.headers)


class Response:
    """``playwright.async_api.Response``."""

    __slots__ = ("_response",)

    def __init__(self, response: Any) -> None:
        self._response = response

    def __getattr__(self, name: str) -> Any:
        _refuse(self, name)

    @property
    def url(self) -> str:
        return self._response.url

    @property
    def status(self) -> int:
        return self._response.status

    @property
    def ok(self) -> bool:
        return self._response.ok

    @property
    def request(self) -> Request:
        return Request(self._response.request)

    async def all_headers(self) -> dict[str, str]:
        return dict(self._response.headers)

    async def text(self) -> str:
        return await self._response.text()

    async def json(self) -> Any:
        return await self._response.json()


class Route:
    """``playwright.async_api.Route``."""

    __slots__ = ("_route",)

    def __init__(self, route: NativeRoute) -> None:
        self._route = route

    def __getattr__(self, name: str) -> Any:
        _refuse(self, name)

    @property
    def request(self) -> Request:
        return Request(self._route.request)

    async def abort(self, error_code: str = "failed") -> None:
        # Playwright's spelling is lowercase ("failed", "aborted"); CDP's is
        # capitalised, and sending the wrong case is a protocol error rather
        # than a silent no-op -- so it is worth converting rather than hoping.
        await self._route.abort(error_code[:1].upper() + error_code[1:])

    async def continue_(self, **rest: Any) -> None:
        _only(rest, "continue_")
        await self._route.continue_()

    async def fulfill(
        self,
        *,
        status: int = 200,
        body: str = "",
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
        **rest: Any,
    ) -> None:
        _only(rest, "fulfill")
        await self._route.fulfill(status=status, body=body, content_type=content_type, headers=headers)


class Dialog:
    """``playwright.async_api.Dialog``.

    Wrapped rather than handed over for the usual reason -- ``dialog.page`` has
    to give back a *wrapped* page, or a suite that took one would be calling
    wirespec's API with Playwright's units. The two methods take no timeout, so
    this is the second wrapper in the module with nothing to convert.
    """

    __slots__ = ("_dialog", "_page")

    def __init__(self, dialog: NativeDialog, page: Page) -> None:
        self._dialog = dialog
        self._page = page

    def __repr__(self) -> str:
        return repr(self._dialog)

    def __getattr__(self, name: str) -> Any:
        _refuse(self, name)

    @property
    def type(self) -> str:
        return self._dialog.type

    @property
    def message(self) -> str:
        return self._dialog.message

    @property
    def default_value(self) -> str:
        return self._dialog.default_value

    @property
    def page(self) -> Page:
        return self._page

    async def accept(self, prompt_text: str | None = None) -> None:
        await self._dialog.accept(prompt_text)

    async def dismiss(self) -> None:
        await self._dialog.dismiss()


class APIResponse:
    """``playwright.async_api.APIResponse``."""

    __slots__ = ("_response",)

    def __init__(self, response: Any) -> None:
        self._response = response

    def __repr__(self) -> str:
        return repr(self._response)

    def __getattr__(self, name: str) -> Any:
        _refuse(self, name)

    @property
    def url(self) -> str:
        return self._response.url

    @property
    def status(self) -> int:
        return self._response.status

    @property
    def ok(self) -> bool:
        return self._response.ok

    @property
    def headers(self) -> dict[str, str]:
        return self._response.headers

    async def text(self) -> str:
        return await self._response.text()

    async def json(self) -> Any:
        return await self._response.json()

    async def body(self) -> bytes:
        return await self._response.body()


class APIRequestContext:
    """``playwright.async_api.APIRequestContext``.

    Wrapped for the reason §15.2 gives and nothing else: the
    native one counts **seconds**. Handed over bare, `page.request.get(url,
    timeout=250)` is a wait of 250 seconds, and a suite whose API calls quietly
    stopped timing out still passes -- slowly, and then not at all.
    """

    __slots__ = ("_request",)

    def __init__(self, request: Any) -> None:
        self._request = request

    def __repr__(self) -> str:
        return repr(self._request)

    def __getattr__(self, name: str) -> Any:
        _refuse(self, name)

    async def fetch(self, url: str, **kwargs: Any) -> APIResponse:
        return await self._call("fetch", url, **kwargs)

    async def get(self, url: str, **kwargs: Any) -> APIResponse:
        return await self._call("get", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> APIResponse:
        return await self._call("post", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> APIResponse:
        return await self._call("put", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> APIResponse:
        return await self._call("patch", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> APIResponse:
        return await self._call("delete", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> APIResponse:
        return await self._call("head", url, **kwargs)

    async def _call(self, verb: str, url: str, **kwargs: Any) -> APIResponse:
        timeout = kwargs.pop("timeout", None)
        method = kwargs.pop("method", None)
        rest = {name: kwargs.pop(name) for name in ("headers", "data") if name in kwargs}
        _only(kwargs, f"request.{verb}")
        if method is not None:
            rest["method"] = method
        return APIResponse(
            await getattr(self._request, verb)(url, timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS), **rest)
        )


class Page:
    """``playwright.async_api.Page``."""

    # `__weakref__`, because the wrapper cache holds these weakly.
    __slots__ = ("__weakref__", "_page")

    def __init__(self, page: NativePage) -> None:
        self._page = page

    def __repr__(self) -> str:
        return repr(self._page)

    def __getattr__(self, name: str) -> Any:
        _refuse(self, name)

    @property
    def url(self) -> str:
        return self._page.url

    @property
    def context(self) -> BrowserContext:
        """The context this page was opened in.

        How a Playwright suite reaches a context-wide route from a page, and
        the reason contexts are wrapped once rather than per access.
        """
        return _wrapper(self._page.context, lambda: BrowserContext(self._page.context))

    @property
    def mouse(self) -> Mouse:
        return self._page.mouse

    @property
    def keyboard(self) -> Keyboard:
        return self._page.keyboard

    @property
    def request(self) -> APIRequestContext:
        return _wrapper(self._page.request, lambda: APIRequestContext(self._page.request))

    async def goto(self, url: str, *, timeout: float | None = None, **rest: Any) -> None:
        _only(rest, "goto")
        await self._page.goto(url, timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS))

    async def title(self) -> str:
        return await self._page.title()

    async def reload(self, *, timeout: float | None = None) -> None:
        await self._page.reload(timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS))

    async def close(self) -> None:
        await self._page.close()

    async def evaluate(self, expression: str, arg: Any = NO_ARGUMENT) -> Any:
        return await self._page.evaluate(expression, arg)

    async def set_viewport_size(self, viewport: Mapping[str, int]) -> None:
        await self._page.set_viewport_size(_viewport(viewport))

    async def wait_for_selector(
        self, selector: str, *, state: str = "visible", timeout: float | None = None
    ) -> Locator:
        return Locator(
            await self._page.wait_for_selector(selector, state=state, timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS))
        )

    def locator(self, selector: str, **kwargs: Any) -> Locator:
        found = Locator(self._page.locator(selector))
        return found.filter(**kwargs) if kwargs else found

    def frame_locator(self, selector: str) -> FrameLocator:
        return FrameLocator(self._page.frame_locator(selector))

    def get_by_role(self, role: str, *, name: Any = None, exact: bool = False) -> Locator:
        return Locator(self._page.get_by_role(role, name=name, exact=exact))

    def get_by_text(self, text: Any, *, exact: bool = False) -> Locator:
        return Locator(self._page.get_by_text(text, exact=exact))

    def get_by_label(self, text: Any, *, exact: bool = False) -> Locator:
        return Locator(self._page.get_by_label(text, exact=exact))

    def get_by_placeholder(self, text: Any, *, exact: bool = False) -> Locator:
        return Locator(self._page.get_by_placeholder(text, exact=exact))

    def get_by_test_id(self, value: Any) -> Locator:
        return Locator(self._page.get_by_test_id(value))

    def on(self, event: str, handler: Callable[[Any], object]) -> None:
        """**Synchronous**, unlike wirespec's own (§4.3).

        Playwright's returns ``None`` and a suite written against it never
        awaits it, so forwarding the coroutine would register nothing and the
        handler would simply never fire -- with the suite passing, because a
        handler that never runs breaks nothing until it was supposed to have
        recorded something.

        Scheduling it as a task does not work either, and this was measured:
        the subscription lands on the next turn of the loop, and the very next
        statement is usually the ``goto`` whose request was the point. So the
        subscription is made **here**, synchronously, against the session; what
        wirespec's version awaits is ``Network.enable``, and the compat layer
        has already done that when the page was created.
        """
        if event == "dialog":
            # No enable behind it and nothing to await, so wirespec's own is
            # already synchronous underneath -- and the handler it takes may be
            # a coroutine function, which is how Playwright's async suites
            # write one (§8.20).
            self._page._on_dialog(lambda dialog: handler(Dialog(dialog, self)))
            return
        wrap = {"request": Request, "response": Response}.get(event)
        if wrap is None:
            raise NotImplementedError(
                f"page.on({event!r}) is not supported by wirespec, which has 'request', 'response' and 'dialog' (§6.2)."
            )
        if event == "request":
            self._page.session.on(
                network_domain.RequestWillBeSent,
                lambda sent: handler(wrap(self._page._request_of(sent))),
            )
        else:
            self._page.session.on(
                network_domain.ResponseReceived,
                lambda got: handler(wrap(self._page._response_of(got))),
            )

    async def route(self, pattern: str, handler: Callable[[Route], Any]) -> None:
        await self._page.route(pattern, lambda route: handler(Route(route)))

    @contextlib.asynccontextmanager
    async def expect_popup(self, *, timeout: float | None = None):
        async with self._page.expect_popup(timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS)) as caught:
            yield _Caught(caught, Page)

    @contextlib.asynccontextmanager
    async def expect_request(self, predicate: Callable[[Request], bool], *, timeout: float | None = None):
        async with self._page.expect_request(
            lambda request: predicate(Request(request)), timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS)
        ) as caught:
            yield _Caught(caught, Request)

    @contextlib.asynccontextmanager
    async def expect_response(self, predicate: Callable[[Response], bool], *, timeout: float | None = None):
        async with self._page.expect_response(
            lambda response: predicate(Response(response)), timeout=_seconds(timeout, DEFAULT_TIMEOUT_MS)
        ) as caught:
            yield _Caught(caught, Response)


class _Caught:
    """What an ``expect_*`` block hands back, wrapped on read.

    ``value`` is a **property returning an awaitable**, because that is
    Playwright's async spelling: ``opened = await popup_info.value``. A method
    reads as working right up to ``TypeError: 'method' object can't be
    awaited``, which names neither the block nor the fix. The sync facade turns
    the same property into a plain one (§15.2).
    """

    __slots__ = ("_caught", "_wrap")

    def __init__(self, caught: Any, wrap: type) -> None:
        self._caught = caught
        self._wrap = wrap

    @property
    def value(self) -> Any:
        return self._value()

    async def _value(self) -> Any:
        # Nothing to await: wirespec's block has already settled by the time it
        # is left. The coroutine exists so `await ...value` is legal, which is
        # the whole contract.
        return self._wrap(self._caught.result())


def _viewport(viewport: Mapping[str, int]) -> tuple[int, int]:
    """Playwright's ``{"width": w, "height": h}`` as wirespec's ``(w, h)``.

    Passed through unconverted a dict becomes a two-element iterable of its
    *keys*, which is a viewport of ``("width", "height")`` -- and the failure
    surfaces somewhere else entirely.
    """
    try:
        return int(viewport["width"]), int(viewport["height"])
    except (KeyError, TypeError) as exc:
        raise ValueError(f"viewport must be {{'width': int, 'height': int}}, got {viewport!r}") from exc


class BrowserContext:
    """``playwright.async_api.BrowserContext``.

    Holds the exit stack that keeps the native context open. wirespec's
    ``new_context`` is an ``async with`` block and Playwright's is a value you
    close later, so the block has to be entered here and left in ``close`` --
    the alternative is a context that tears itself down before the first page
    is opened.
    """

    __slots__ = ("__weakref__", "_context", "_stack")

    def __init__(self, context: native.BrowserContext, stack: contextlib.AsyncExitStack | None = None) -> None:
        self._context = context
        #: ``None`` for a context reached through ``page.context``: the block
        #: that opened it is owned by whoever called ``new_context``, and this
        #: wrapper only borrows it.
        self._stack = stack

    def __getattr__(self, name: str) -> Any:
        _refuse(self, name)

    @property
    def pages(self) -> list[Page]:
        return [_wrapper(page, lambda page=page: Page(page)) for page in self._context.pages]

    async def new_page(self) -> Page:
        page = await self._context.new_page()
        # Eagerly, which wirespec's own driver deliberately does not do
        # (§3.5: watching the network costs a decode per request
        # and nothing pays for it until something asks). A Playwright suite has
        # already asked -- its `page.on` is synchronous, so there is no later
        # moment at which enabling can be awaited without racing the navigation
        # that follows it.
        await page._enable_network()
        return _wrapper(page, lambda: Page(page))

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        else:
            await self._context.close()

    async def route(self, pattern: str, handler: Callable[[Route], Any]) -> None:
        await self._context.route(pattern, lambda route: handler(Route(route)))

    async def add_cookies(self, cookies: Iterable[Mapping[str, object]]) -> None:
        await self._context.add_cookies(cookies)

    # `cookies()` and `clear_cookies()` are deliberately absent: wirespec seeds
    # a jar and does not read it back. `__getattr__` refuses them by name
    # (§15.4) rather than this layer inventing a read that would
    # have to be built in the driver first.


class Browser:
    """``playwright.async_api.Browser``."""

    __slots__ = ("_browser", "_stack")

    def __init__(self, browser: native.Browser, stack: contextlib.AsyncExitStack) -> None:
        self._browser = browser
        self._stack = stack

    def __getattr__(self, name: str) -> Any:
        _refuse(self, name)

    async def new_context(
        self,
        *,
        base_url: str = "",
        viewport: Mapping[str, int] | None = None,
        **rest: Any,
    ) -> BrowserContext:
        _only(rest, "new_context")
        size = _viewport(viewport) if viewport else native.DEFAULT_VIEWPORT
        stack = contextlib.AsyncExitStack()
        context = await stack.enter_async_context(self._browser.new_context(base_url=base_url, viewport=size))
        return _wrapper(context, lambda: BrowserContext(context, stack))

    async def new_page(self, **kwargs: Any) -> Page:
        return await (await self.new_context(**kwargs)).new_page()

    async def close(self) -> None:
        await self._stack.aclose()


class BrowserType:
    """``playwright.async_api.BrowserType`` — only ``chromium`` exists.

    Firefox and WebKit are permanently out (§15.1): wirespec is
    CDP, and CDP is Chromium. Asking for one raises here rather than launching
    Chromium under another name, which would make a cross-browser suite report
    three passes for one browser.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    async def launch(
        self,
        *,
        headless: bool = True,
        executable_path: str | None = None,
        args: Iterable[str] | None = None,
        **rest: Any,
    ) -> Browser:
        _only(rest, "launch")
        if self._name != "chromium":
            raise NotImplementedError(
                f"{self._name} is not available: wirespec speaks CDP, and CDP is Chromium "
                f"(§15.1). This is permanent, not a gap."
            )
        # `Browser.launch` is an `async with` block -- it makes a profile
        # directory and removes it on the way out. Playwright's returns a value
        # you close later, so the block is entered into a stack here and left in
        # `Browser.close`; without that the profile is deleted out from under
        # the browser before the first page opens.
        stack = contextlib.AsyncExitStack()
        launched = await stack.enter_async_context(
            native.Browser.launch(headed=not headless, binary=executable_path, extra_flags=tuple(args or ()))
        )
        return Browser(launched, stack)


class Playwright:
    """What ``async_playwright()`` yields."""

    __slots__ = ("chromium", "firefox", "webkit")

    def __init__(self) -> None:
        self.chromium = BrowserType("chromium")
        self.firefox = BrowserType("firefox")
        self.webkit = BrowserType("webkit")


@contextlib.asynccontextmanager
async def async_playwright() -> AsyncIterator[Playwright]:
    """``playwright.async_api.async_playwright()``.

    Nothing is started here. Playwright's version spawns its Node driver; there
    is no driver process to spawn, so this exists to keep the shape of the
    call and hands back the browser types directly.
    """
    yield Playwright()


class _MilliAssertions:
    """Playwright's ``expect`` return value: wirespec's, in milliseconds.

    Wrapped rather than subclassed because the timeout is fixed at construction
    in the native API and Playwright passes it per assertion -- so each call
    here builds its own.
    """

    __slots__ = ("_message", "_subject")

    def __init__(self, subject: Any, message: str | None = None) -> None:
        self._subject = subject
        #: Playwright puts this in front of the failure. Honoured rather than
        #: refused: it is the caller's own sentence, and dropping it would
        #: throw away the one part of the message they wrote.
        self._message = message

    def __getattr__(self, name: str) -> Any:
        if not name.startswith(("to_", "not_to_")):
            _refuse(self, name)

        async def assertion(*args: Any, timeout: float | None = None, **kwargs: Any) -> None:
            checker = native_expect(self._subject, timeout=_seconds(timeout, DEFAULT_EXPECT_MS))
            if not hasattr(checker, name):
                # An assertion Playwright has and wirespec does not --
                # `to_have_class`, `to_have_id`. Refused by name rather than
                # silently skipped, which is what an assertion that quietly
                # does nothing amounts to (§15.4).
                _refuse(self, name)
            try:
                await getattr(checker, name)(*args, **kwargs)
            except WirespecTimeoutError as unmet:
                # Playwright's `expect` raises `AssertionError`, and a suite is
                # entitled to catch it -- soft assertions and `pytest.raises`
                # both do. wirespec raises a timeout, which is right for
                # wirespec and wrong for this surface. The message is carried
                # over unchanged: it is the diagnosis, and §15.1 puts matching
                # Playwright's *text* out of scope, not keeping our own.
                raise AssertionError(f"{self._message}\n{unmet}" if self._message else str(unmet)) from unmet

        return assertion


def expect(subject: Locator | Page, message: str | None = None, **rest: Any) -> _MilliAssertions:
    """``playwright.async_api.expect``, with millisecond timeouts.

    ``timeout`` belongs on the *assertion*, not here -- Playwright's `expect`
    takes only an optional message -- so anything else refuses rather than
    being swallowed. Swallowing it is the worst of the three options: the suite
    believes it asked for 300 ms and waits the default five seconds.
    """
    _only(rest, "expect")
    inner = subject._locator if isinstance(subject, Locator) else subject._page
    return _MilliAssertions(inner, message)
