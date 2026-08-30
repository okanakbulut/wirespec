"""Pages: opening one, navigating it, and asking it where it is.

Everything here goes through the public API. A test that reached for the
connection would be testing the protocol floor again, which ``tests/live``
already does.
"""

import asyncio

import pytest

from wirespec.browser import Browser
from wirespec.errors import NavigationError, WirespecTimeoutError
from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_new_page_starts_blank(browser: Browser) -> None:
    async with browser.new_context() as context:
        page = await context.new_page()
        assert page.url == "about:blank"


async def test_goto_lands_on_the_url_it_was_given(page: Page, site: str) -> None:
    await page.goto(f"{site}/index.html")
    assert page.url == f"{site}/index.html"


async def test_goto_resolves_a_relative_path_against_the_context_base_url(page: Page, site: str) -> None:
    """What a spec actually writes. The context fixture sets ``base_url``."""
    await page.goto("/index.html")
    assert page.url == f"{site}/index.html"


async def test_a_relative_goto_with_no_base_url_says_so(browser: Browser) -> None:
    """Handed to Chrome as-is, a relative URL becomes a file:// lookup in the
    working directory and fails a long way from here."""
    async with browser.new_context() as context:
        page = await context.new_page()
        with pytest.raises(ValueError, match="relative"):
            await page.goto("/index.html")


async def test_a_navigation_that_did_not_happen_raises(page: Page) -> None:
    """Chrome reports this in the result rather than as a protocol error, so a
    driver that only catches CDPError waits out its whole timeout instead."""
    with pytest.raises(NavigationError) as raised:
        await page.goto("http://127.0.0.1:1/nothing-here", timeout=5.0)
    assert "127.0.0.1:1" in str(raised.value)


async def test_a_fragment_navigation_is_not_waited_out(page: Page, site: str) -> None:
    """Measured: a same-document navigation fires no ``load`` event at all --
    Chrome sends ``navigatedWithinDocument`` instead and allocates no loader.
    A goto that waits for load hangs for its whole timeout and then blames the
    load event. ``Page.navigate`` says which kind it was in the reply, before
    any event arrives, so this costs nothing to get right."""
    await page.goto("/index.html")
    await page.goto("/index.html#where", timeout=3.0)
    assert page.url == f"{site}/index.html#where"


async def test_the_page_notices_a_fragment_it_did_not_navigate_itself(page: Page, site: str) -> None:
    """``frameNavigated`` does not fire for a same-document navigation, so a
    page that tracked only that would report a stale URL for ever after the
    application changed the hash."""
    await page.goto("/index.html")
    await page.evaluate("() => { location.hash = 'set-by-the-page'; }")
    await asyncio.sleep(0.2)
    assert page.url == f"{site}/index.html#set-by-the-page"


async def test_reload_runs_the_document_again(page: Page, site: str) -> None:
    """A fresh document, not a re-render: whatever the last one left on
    ``window`` is gone, and the URL is unchanged."""
    await page.goto("/index.html")
    await page.evaluate("() => { window.__survived = true; }")
    await page.reload()
    assert await page.evaluate("() => window.__survived === undefined") is True
    assert page.url == f"{site}/index.html"


async def test_send_is_the_documented_escape_hatch(page: Page) -> None:
    """§6.2. Three lines, against the alternative of someone
    forking the library the first time they need a call wirespec does not
    wrap."""
    await page.goto("/index.html")
    result = await page.send("Runtime.evaluate", {"expression": "document.title", "returnByValue": True})
    assert result["result"]["value"] == "wirespec driver fixture"


async def test_a_navigation_that_never_loads_says_what_it_was_waiting_for(page: Page) -> None:
    """A bare ``TimeoutError`` from the wait machinery says nothing about what
    was being waited for. Every timeout wirespec raises names it, and quotes
    where the page still is (§1, goal 4)."""
    await page.goto("/index.html")
    with pytest.raises(WirespecTimeoutError) as raised:
        await page.goto("/stall", timeout=1.0)
    message = str(raised.value)
    assert "/stall" in message
    assert "1.0s" in message
    # The two failures have different causes -- a stalled response against a
    # navigation that never got started -- so the message has to tell them
    # apart. ``/stall`` sends its headers and then stops, so the document does
    # commit; ``frameNavigated`` fires on commit, well before load.
    assert "committed and never fired load" in message


async def test_a_navigation_that_never_commits_says_that_instead(page: Page, site: str) -> None:
    """The other branch of the same message. ``/silent`` answers nothing at
    all, so no document commits and the page is still where it was -- which is
    a different bug from a response that arrived and stalled."""
    await page.goto("/index.html")
    with pytest.raises(WirespecTimeoutError) as raised:
        await page.goto("/silent", timeout=1.0)
    assert f"still at {site}/index.html" in str(raised.value)


async def test_a_reload_that_never_loads_names_the_page(page: Page) -> None:
    """The stalling route again, reached through reload rather than goto: the
    document is already there, and running it again never finishes."""
    with pytest.raises(WirespecTimeoutError):
        await page.goto("/stall", timeout=1.0)
    with pytest.raises(WirespecTimeoutError, match="reloading"):
        await page.reload(timeout=1.0)


async def test_title_is_the_documents_title(page: Page) -> None:
    """``document.title``, without evaluating ``document.title``.

    Read off ``head > title`` rather than ``Target.getTargetInfo``, which looks
    like the obvious source and is not: measured, a page with no title has a
    ``targetInfo.title`` of its **URL** -- what the tab strip shows -- where
    ``document.title`` is ``""``. An assertion about a missing title would pass
    against the address bar (§8.21).
    """
    await page.goto("/index.html")
    assert await page.title() == "wirespec driver fixture"
    await page.goto("/list.html")
    assert await page.title() == "a list"


async def test_title_follows_a_script_that_changes_it(page: Page) -> None:
    await page.goto("/index.html")
    await page.evaluate("() => { document.title = 'set by script'; }")
    assert await page.title() == "set by script"


async def test_a_page_with_no_title_has_an_empty_one(page: Page) -> None:
    """``""``, not the URL and not ``None``. The ``<title>`` inside the SVG is
    there on purpose: a bare ``title`` selector would find it, and
    ``document.title`` never does."""
    await page.goto("data:text/html,<svg><title>an svg title</title></svg><h1>body</h1>")
    assert await page.title() == ""


async def test_title_is_stripped_and_collapsed_like_the_dom_property(page: Page) -> None:
    """HTML's own rule for ``document.title``: leading and trailing whitespace
    stripped, runs of it collapsed. Reading the text node raw would disagree
    with every spec that asserts on a prettily-indented ``<title>``."""
    await page.goto("data:text/html,<title>\n  lots   of\n  space\n</title><h1>body</h1>")
    assert await page.title() == "lots of space"
