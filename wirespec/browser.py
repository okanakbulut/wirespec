"""Finding a Chrome, starting it, and the contexts that live on it.

Chrome is discovered, never downloaded (§7): a self-hosted runner
image already has one, and downloading a 389 MB Chromium is the single largest
thing wirespec refuses to do.
"""

import asyncio
import contextlib
import inspect
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from pathlib import Path

from wirespec.api import APIRequestContext
from wirespec.cdp import accessibility as ax_domain
from wirespec.cdp import animation as animation_domain
from wirespec.cdp import browser as browser_domain
from wirespec.cdp import css as css_domain
from wirespec.cdp import dom as dom_domain
from wirespec.cdp import domsnapshot as snapshot_domain
from wirespec.cdp import emulation as emulation_domain
from wirespec.cdp import network as network_domain
from wirespec.cdp import page as page_domain
from wirespec.cdp import runtime as runtime_domain
from wirespec.cdp import storage as storage_domain
from wirespec.cdp import target as target_domain
from wirespec.connection import Connection
from wirespec.errors import CDPError, LaunchError
from wirespec.network import Handler
from wirespec.page import Page

__all__ = [
    "CANDIDATES",
    "DEFAULT_LOCALE",
    "DEFAULT_VIEWPORT",
    "Browser",
    "BrowserContext",
    "chrome_argv",
    "find_chrome",
]

#: How long to let a freshly announced popup commit a document before handing
#: it over. Short: it is a local navigation that has already begun, and a
#: `window.open()` with no argument never commits one at all.
POPUP_SETTLE = 5.0

#: The locale wirespec spawns Chrome in, and therefore the segment order a
#: date picker displays. **Pinned, not inherited** -- measured on Chrome 150,
#: ``LANG``/``LC_ALL`` is what decides a date input's order, and neither
#: ``--lang`` nor ``Emulation.setLocaleOverride`` changes it. Left to the
#: machine, ``fill("2026-03-15")`` sets March 15th on a US developer's laptop
#: and the 3rd of December on a European CI runner, with nothing failing
#: (§8.4).
#:
#: The page's own locale is a separate thing and stays available:
#: ``Emulation.setLocaleOverride`` moves ``Intl`` and ``navigator.language``
#: without touching the widget. Measured, and that independence is what makes
#: pinning this safe.
DEFAULT_LOCALE = "en_US.UTF-8"

#: §6.1. A headless window's default size is not a promise and
#: every measured box is relative to it (§8.9), so wirespec always
#: says what it wants rather than accepting what it is given.
DEFAULT_VIEWPORT = (1280, 720)

#: In preference order. ``google-chrome`` first because that is what a
#: developer machine and a CI image both usually have, and the two Chromium
#: names last because a distribution Chromium can be a snap wrapper that
#: refuses to see a ``--user-data-dir`` outside the home directory.
CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")


def find_chrome() -> str | None:
    """The Chrome to drive, or ``None`` if this machine has none.

    ``E2E_CHROME_BINARY`` wins outright and is not fallen back from: an
    override that silently resolved to some other browser would make a run
    against the wrong Chrome look like a run against the right one.
    """
    override = os.environ.get("E2E_CHROME_BINARY")
    if override:
        return override if os.path.exists(override) else None
    for name in CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return _playwright_cached_chromium()


def _playwright_cached_chromium() -> str | None:
    """The last resort: a Chromium some other tool already downloaded.

    A machine that has only ever run Playwright has a working browser and
    nothing on PATH. wirespec will not download one (§7), but
    refusing to *use* one would mean telling that developer to install a
    browser they already have.

    Highest build number wins, and the numbers are compared as integers: sorted
    as strings, ``chromium-1091`` beats ``chromium-999``.
    """
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    base = Path(root) if root else _playwright_cache_root()
    if not base.is_dir():
        return None
    builds: list[tuple[int, str]] = []
    for entry in base.iterdir():
        prefix, _, build = entry.name.rpartition("-")
        if prefix not in ("chromium", "chromium_headless_shell") or not build.isdigit():
            continue
        for relative in _CHROMIUM_LAYOUTS:
            candidate = entry / relative
            if candidate.is_file():
                builds.append((int(build), str(candidate)))
                break
    if not builds:
        return None
    return max(builds)[1]


#: Where Playwright puts the executable inside one downloaded build, per
#: platform. macOS is listed but untested (§12).
_CHROMIUM_LAYOUTS = (
    "chrome-linux/chrome",
    "chrome-linux/headless_shell",
    "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
)


def _playwright_cache_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def chrome_argv(
    binary: str,
    user_data_dir: str,
    *,
    headed: bool = False,
    extra_flags: Sequence[str] = (),
) -> list[str]:
    """The command line, with the flags §6.1 says to keep and why.

    ``extra_flags`` goes last so a caller can override anything here; the
    starting URL stays after it, because Chrome treats the first non-flag
    argument as the page to open and a flag after it is ignored.
    """
    argv = [
        binary,
        "--remote-debugging-pipe",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # A CI container's /dev/shm is 64 MB, and Chrome crashes rather than
        # degrades when it fills (§6.1).
        "--disable-dev-shm-usage",
        # Headless Chrome throttles pages it thinks nobody is looking at, which
        # for a test runner is every page. Without these a spec's own timers run
        # at a fraction of speed and the flake looks like the application's.
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ]
    if not headed:
        argv.append("--headless=new")
    argv.extend(extra_flags)
    argv.append("about:blank")
    return argv


class Browser:
    """One Chrome process, and the single connection that carries every page.

    Made by :meth:`launch`, which is an async context manager because a browser
    that outlives the block that started it is a leaked process and a leaked
    profile directory.
    """

    def __init__(
        self,
        connection: Connection,
        *,
        user_data_dir: str,
        binary: str,
        locale: str = DEFAULT_LOCALE,
    ) -> None:
        #: The escape hatch, one level below ``page.send`` (§6.2).
        self.connection = connection
        self.user_data_dir = user_data_dir
        self.binary = binary
        self.locale = locale

    @property
    def locale_tag(self) -> str:
        """The launch locale as a BCP 47 tag -- ``en_US.UTF-8`` as ``en-US``.

        Which is what a picker's segment order is keyed on
        (``wirespec/pickers.py``).
        """
        return self.locale.split(".")[0].replace("_", "-")

    def __repr__(self) -> str:
        state = "closed" if self.connection.closed else "open"
        return f"<Browser {self.binary} pid={self.connection.transport.pid} {state}>"

    @classmethod
    @contextlib.asynccontextmanager
    async def launch(
        cls,
        *,
        headed: bool = False,
        extra_flags: Sequence[str] = (),
        binary: str | None = None,
        user_data_dir: str | None = None,
        locale: str = DEFAULT_LOCALE,
    ) -> AsyncIterator[Browser]:
        """Start a Chrome and hand it over for the duration of the block.

        The profile directory is made here and removed on the way out unless
        the caller supplied one, in which case it is theirs and is left alone.
        ``headed=True`` is the same pipe with ``--headless=new`` omitted; there
        is no second transport (§3.2).

        ``locale`` is put in the child's environment, and it is what decides a
        date picker's segment order -- see ``DEFAULT_LOCALE`` for why that is
        pinned rather than inherited.
        """
        binary = binary or find_chrome()
        if binary is None:
            raise LaunchError("no Chrome found: set E2E_CHROME_BINARY, or install one of " + ", ".join(CANDIDATES))
        owned = user_data_dir is None
        profile = user_data_dir or tempfile.mkdtemp(prefix="wirespec-profile-")
        # Chrome blocks forever on a stderr pipe nobody drains, so it goes to a
        # file inside the profile -- which also means a launch that dies leaves
        # its own diagnostics behind for ``transport.stderr_tail``.
        connection = await Connection.launch(
            chrome_argv(binary, profile, headed=headed, extra_flags=extra_flags),
            env=dict(os.environ, LANG=locale, LC_ALL=locale),
            stderr_path=os.path.join(profile, "chrome-stderr.log"),
        )
        browser = cls(connection, user_data_dir=profile, binary=binary, locale=locale)
        try:
            yield browser
        finally:
            await connection.close()
            if owned:
                shutil.rmtree(profile, ignore_errors=True)

    async def version(self) -> browser_domain.GetVersionResult:
        """What Chrome says it is. The cheapest complete round trip there is."""
        return await self.connection.send(browser_domain.GetVersion())

    @contextlib.asynccontextmanager
    async def new_context(
        self,
        *,
        base_url: str = "",
        viewport: tuple[int, int] = DEFAULT_VIEWPORT,
    ) -> AsyncIterator[BrowserContext]:
        """A real browser context -- its own cookies, storage and cache.

        Not a tab that happens to share nothing (§6.1). Disposed
        when the block ends, taking every page in it with it.
        """
        created = await self.connection.send(target_domain.CreateBrowserContext())
        context = BrowserContext(self, created.browser_context_id, base_url=base_url, viewport=viewport)
        try:
            yield context
        finally:
            await context.close()


def _record(into: list[str], event: page_domain.FrameNavigated) -> None:
    """Remember a main-frame navigation seen before the Page object existed."""
    if event.frame.parent_id is None:
        into.append(event.frame.url)


class BrowserContext:
    """One browser context, and the pages opened in it."""

    def __init__(
        self,
        browser: Browser,
        context_id: str,
        *,
        base_url: str = "",
        viewport: tuple[int, int] = DEFAULT_VIEWPORT,
    ) -> None:
        self.browser = browser
        self.id = context_id
        #: What a relative ``page.goto`` is resolved against.
        self.base_url = base_url
        self.viewport = viewport
        #: Every page opened here, so disposing the context can tell them they
        #: are gone rather than leaving objects that fail on their next call.
        self.pages: list[Page] = []
        #: Popups waiting to be handed to whoever asked. A queue rather than a
        #: single slot, because two clicks can open two tabs before anything
        #: reads either (§3.3).
        self._popups: asyncio.Queue[Page] = asyncio.Queue()
        #: Targets an adoption is already in flight for. **Not** optional:
        #: `targetCreated` can arrive more than once for one target -- turning
        #: discovery on replays the ones that already exist -- and adopting
        #: twice attaches twice. The second `DOM.enable` resets the node-id
        #: space, so the first page's cached document id stops resolving and
        #: every query on it fails with "Could not find node with given id".
        #: Found exactly that way: one run in three.
        self._adopting: set[str] = set()
        self._watching = False
        self._unsubscribe: list[Callable[[], None]] = []
        #: Who wants to be told about a page opening here. Used by a recording
        #: to follow a tab the application opened (§16.2); empty
        #: on a context nobody is watching, which is nearly all of them.
        self._openings: tuple[Callable[[Page], object], ...] = ()
        #: Routes set on the context, replayed onto every page opened later.
        self.routes: list[tuple[str, Handler]] = []
        #: Built on the first `page.request` call, because most contexts never
        #: make one and it costs a target.
        self._request: APIRequestContext | None = None

    def __repr__(self) -> str:
        return f"<BrowserContext {self.id}>"

    @property
    def connection(self) -> Connection:
        return self.browser.connection

    async def new_page(self) -> Page:
        """Open a page in this context and attach to it."""
        created = await self.connection.send(target_domain.CreateTarget(url="about:blank", browser_context_id=self.id))
        return await self._adopt(created.target_id, url="about:blank")

    async def _adopt(self, target_id: str, *, url: str) -> Page:
        """Attach to a target and make it a page of this context.

        Shared with popups, which is the whole reason it is a method: a tab the
        *application* opened has to end up indistinguishable from one wirespec
        opened -- same domains enabled, same viewport, same routes -- or a spec
        that stubs an endpoint finds the popup reaching the real server
        (§6.1).
        """
        session = await self.connection.attach(target_id)
        # **Subscribed before anything is enabled.** `Page.enable` is what turns
        # these events on, and the six round trips that follow it are long
        # enough for a popup to commit its document -- so a Page that only
        # subscribes at the end misses the one `frameNavigated` it will ever
        # get, keeps `about:blank` as its url, and answers a query against a
        # document id that no longer exists. Measured: one run in three, as
        # "DOM.querySelectorAll: Could not find node with given id".
        arrived: list[str] = []
        stop = (session.on(page_domain.FrameNavigated, lambda event: _record(arrived, event)),)
        # Page first: without it there is no loadEventFired for `goto` to wait
        # on and no frameNavigated to keep `page.url` current.
        await session.send(page_domain.Enable())
        await session.send(runtime_domain.Enable())
        # DOM last of the three, and not optional: it is what makes the node-id
        # space exist at all, and what turns on the mutation events the wait
        # loop listens to (§5.1).
        await session.send(dom_domain.Enable())
        # Enabling Accessibility did not measurably slow anything else -- a
        # plain Runtime.evaluate was 0.079 ms before and 0.089 ms after, which
        # was the thing worth being afraid of (§9).
        await session.send(ax_domain.Enable())
        # CSS answers "is it visible" and DOMSnapshot answers "what does it
        # say" (§3.4). Both are enabled once here rather than
        # lazily, because a reader that has to enable a domain first pays a
        # round trip the second reader does not, and the difference shows up as
        # an unexplained first-call cost.
        await session.send(css_domain.Enable())
        await session.send(snapshot_domain.Enable())
        # And Animation answers "is it still moving" (§5.2 step 6).
        # Enabled here rather than before the first action for the same reason
        # as the two above, and because the events it turns on are the *record*
        # of what started: a page that enables it late has already missed the
        # animation running when the action arrived. 0.21 ms.
        await session.send(animation_domain.Enable())
        # Every page believes it is the focused one, which is not a nicety: a
        # tab that is not in front stops producing frames, and Chrome will not
        # answer `Input.dispatchMouseEvent(mouseMoved)` until it produces one.
        # Measured on an idle background page, exactly 5.001 s per move -- a
        # watchdog, not a wait -- so every click on any page but the frontmost
        # cost five seconds (§8.26).
        await session.send(emulation_domain.SetFocusEmulationEnabled(enabled=True))
        # One round trip, once per page, to learn which frame this page is.
        # `navigatedWithinDocument` carries a frame id and no parent id, so
        # there is nothing in the event itself to tell the main frame from an
        # iframe -- and flat sessions deliver both on this session.
        tree = await session.send(page_domain.GetFrameTree())
        for unsubscribe in stop:
            unsubscribe()
        page = Page(
            self,
            session,
            target_id,
            # Whatever is most recent: a navigation caught while the domains
            # were being enabled, then the frame tree, then what the caller
            # believed. A popup is announced with an empty url and navigates on
            # its own, so the announcement is stale before it is used.
            url=(arrived[-1] if arrived else "") or tree.frame_tree.frame.url or url,
            main_frame_id=tree.frame_tree.frame.id,
            viewport=self.viewport,
        )
        # Said explicitly, always. The window Chrome opens is whatever size it
        # felt like, and every box the driver measures is relative to it
        # (§8.9).
        await page.set_viewport_size(self.viewport)
        for pattern, handler in self.routes:
            await page.route(pattern, handler)
        self.pages.append(page)
        # After the routes and the viewport, so an observer that starts
        # recording this page finds it already set up the way every other page
        # in the context is.
        for observe in self._openings:
            outcome = observe(page)
            if inspect.isawaitable(outcome):
                await outcome
        return page

    @property
    def request(self) -> APIRequestContext:
        """Issue requests on this context's cookie jar (§6.2).

        On the context rather than on the page, because the jar is what makes
        these the application's requests rather than somebody else's, and the
        jar belongs to the context. ``page.request`` is this one.
        """
        if self._request is None:
            self._request = APIRequestContext(self)
        return self._request

    def on_page(self, observe: Callable[[Page], object]) -> Callable[[], None]:
        """Be told about every page opened in this context from now on.

        The observer may be a coroutine function: it is called from ``_adopt``,
        which is already async, and a recorder attaching to the new page has
        several round trips to make before the page does anything worth
        recording.
        """
        entry = observe
        self._openings += (entry,)

        def unsubscribe() -> None:
            self._openings = tuple(item for item in self._openings if item is not entry)

        return unsubscribe

    async def watch_for_popups(self) -> None:
        """Start noticing tabs the application opens. Idempotent.

        Awaited rather than scheduled, and enabled lazily: target discovery is
        a browser-wide subscription, and a suite that never opens a popup should
        not pay for one. ``Page.expect_popup`` is what turns it on.
        """
        if self._watching:
            return
        self._watching = True
        # Kept, and dropped when the context closes. These are on the *browser*
        # connection, which outlives the context, so a suite that opens a
        # hundred contexts would otherwise leave a hundred handlers behind --
        # each one decoding every target event for a context that is gone.
        self._unsubscribe += [
            self.connection.on(target_domain.TargetCreated, self._target_created),
            self.connection.on(target_domain.TargetDestroyed, self._target_destroyed),
        ]
        await self.connection.send(target_domain.SetDiscoverTargets(discover=True))

    def _target_created(self, event: target_domain.TargetCreated) -> None:
        """A target appeared. Off the read path, like every handler that has to
        send something (§6.5) -- attaching is several round trips
        and awaiting them here would deadlock the connection they go out on."""
        info = event.target_info
        if info.type != "page" or info.browser_context_id != self.id:
            return
        if info.target_id in self._adopting or any(page.target_id == info.target_id for page in self.pages):
            return
        self._adopting.add(info.target_id)
        asyncio.get_running_loop().create_task(self._adopt_popup(info.target_id, info.url))

    async def _adopt_popup(self, target_id: str, url: str) -> None:
        try:
            page = await self._adopt(target_id, url=url)
            if page.url in ("", "about:blank"):
                # Announced before it navigated, which is the ordinary case: a
                # popup is a target first and a document a moment later. Handing
                # it over now would give the caller a page whose `url` is blank
                # and whose content arrives while they are asserting on it.
                # `window.open()` with no argument genuinely stays blank, so
                # this waits briefly and then hands over what there is.
                with contextlib.suppress(TimeoutError):
                    async with page.session.expect(page_domain.FrameNavigated, timeout=POPUP_SETTLE):
                        pass
            self._popups.put_nowait(page)
        except CDPError:
            # The tab closed again before the attach landed, which a page that
            # opens and immediately closes a window does routinely. Nothing was
            # adopted and nothing is waiting on this one; anything else would
            # surface as an unretrievable task exception in an unrelated test.
            pass
        finally:
            self._adopting.discard(target_id)

    def _target_destroyed(self, event: target_domain.TargetDestroyed) -> None:
        """A tab went away. Usually because the *page* closed it -- a
        ``window.close()`` -- since a page closed through ``Page.close`` has
        already taken itself out of the list."""
        for page in list(self.pages):
            if page.target_id == event.target_id:
                page._mark_closed()  # the page cannot see its own target dying
                self.forget(page)

    def forget(self, page: Page) -> None:
        """Drop a page from the context. Idempotent."""
        if page in self.pages:
            self.pages.remove(page)

    async def next_popup(self, timeout: float) -> Page:
        """The next tab the application opens."""
        async with asyncio.timeout(timeout):
            return await self._popups.get()

    async def route(self, pattern: str, handler: Handler) -> None:
        """Intercept matching requests on every page in this context.

        Which is how a spec stubs an API once rather than once per tab
        (§6.1). Pages already open get it too, and pages opened
        afterwards inherit it.
        """
        self.routes.append((pattern, handler))
        for page in self.pages:
            await page.route(pattern, handler)

    async def add_cookies(self, cookies: Iterable[Mapping[str, object] | network_domain.CookieParam]) -> None:
        """Install cookies into this context, with or without a page open.

        Mappings are accepted as well as ``CookieParam`` because that is how
        every spec already writes a cookie, and converting is one line here
        instead of one at each call site. Chrome needs somewhere to put each
        one, so a cookie with neither ``url`` nor ``domain`` is rejected by
        name rather than silently dropped.
        """
        params = []
        for cookie in cookies:
            param = cookie if isinstance(cookie, network_domain.CookieParam) else network_domain.CookieParam(**cookie)  # type: ignore[arg-type]
            if not param.url and not param.domain:
                raise ValueError(f"cookie {param.name!r} needs a url or a domain, or Chrome has nowhere to put it")
            params.append(param)
        await self.connection.send(storage_domain.SetCookies(cookies=params, browser_context_id=self.id))

    async def close(self) -> None:
        """Dispose the context. Chrome closes its pages as part of this.

        The pages are told first. Chrome closing them is invisible from here,
        and a Page object that does not know would fail on its next call with a
        protocol error naming a session id, a long way from the line that
        disposed the context.
        """
        # The target handlers are on the *browser* connection, which outlives
        # this context. Left behind, they would decode every target event for
        # a context that no longer exists, one set per context ever opened.
        for stop in self._unsubscribe:
            stop()
        self._unsubscribe.clear()
        for page in self.pages:
            page._mark_closed()
        self.pages.clear()
        # Disposing the context closes the scratch page with everything else;
        # this only stops the next `context.request` handing back a session id
        # that no longer addresses anything.
        self._request = None
        await self.connection.send(target_domain.DisposeBrowserContext(browser_context_id=self.id))
