"""A Playwright suite, run against wirespec with only the import changed.

Every line here is written the way Playwright's own documentation writes it:
milliseconds, ``{"width": ..., "height": ...}``, a synchronous ``page.on``
handler. Nothing in this file knows it is not talking to Playwright, which is
the whole test (§15.3 stage 6).
"""

import asyncio
import time

import pytest

from wirespec.compat.async_api import Browser, Error, TimeoutError, expect

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
async def context(browser: Browser, site: str):
    made = await browser.new_context(base_url=site, viewport={"width": 900, "height": 700})
    try:
        yield made
    finally:
        await made.close()


@pytest.fixture
async def page(context):
    return await context.new_page()


async def test_a_page_navigates_and_reads(page) -> None:
    await page.goto("/index.html")
    assert "/index.html" in page.url
    await expect(page.locator("h1")).to_be_visible()


async def test_timeouts_are_milliseconds(page) -> None:
    """The conversion §15.2 exists for, and the **elapsed time** is
    the assertion.

    ``timeout=250`` must mean a quarter of a second, not four minutes. Checking
    only that it raised would pass either way: unconverted, the wait is 250
    *seconds*, which is a suite that still goes green and takes until CI gives
    up. So the clock is what is asserted, with wide enough bounds to survive a
    slow machine and narrow enough to catch a factor of a thousand.
    """
    await page.goto("/index.html")
    started = time.monotonic()
    # The budget is outside and raises the *builtin* TimeoutError, which the
    # `pytest.raises` below does not catch -- `TimeoutError` is wirespec's here.
    # So an unconverted timeout fails this test in five seconds instead of
    # hanging the suite for the four minutes it was actually asked to wait.
    async with asyncio.timeout(5.0):
        with pytest.raises(TimeoutError):
            # An *action*, not an assertion. Measured against Playwright 1.62:
            # `expect` raises a plain `AssertionError` and an action raises
            # `TimeoutError`, and the two hierarchies do not meet.
            await page.locator("#nothing-here").click(timeout=250)
    waited = time.monotonic() - started
    assert 0.2 < waited < 5.0, f"a 250 ms timeout waited {waited:.2f}s"


async def test_the_viewport_is_a_dict(page) -> None:
    """Playwright spells it ``{"width": w, "height": h}``; wirespec takes a
    tuple. A layer that passed the dict straight through would set a viewport of
    two dictionary keys."""
    assert await page.evaluate("() => window.innerWidth") == 900
    await page.set_viewport_size({"width": 500, "height": 400})
    assert await page.evaluate("() => window.innerWidth") == 500


async def test_locators_chain_and_filter(page) -> None:
    await page.goto("/list.html")
    rows = page.get_by_role("list").get_by_role("listitem")
    await expect(rows).to_have_count(3)
    await expect(rows.filter(has_text="Acme")).to_have_count(1)
    assert await rows.first.text_content() == "Acme"


async def test_actions_and_readers(page) -> None:
    await page.goto("/actions.html")
    await page.locator("#plain").click()
    assert await page.evaluate("() => window.__log") == ["plain"]
    await page.locator("#text").fill("typed")
    assert await page.locator("#text").input_value() == "typed"


async def test_page_on_takes_a_synchronous_handler(page) -> None:
    """**Not** awaited, unlike wirespec's own (§4.3). Playwright's
    ``page.on`` returns None, and a suite written against it never awaits --
    so a layer that only forwarded the coroutine would register nothing and
    the handler would silently never fire."""
    seen = []
    page.on("request", lambda request: seen.append(request.url))
    await page.goto("/network.html")
    assert any("network.html" in url for url in seen)


async def test_routes_stub_a_request(page) -> None:
    await page.route("**/api", lambda route: route.fulfill(status=201, body='{"stubbed": true}'))
    await page.goto("/network.html")
    got = await page.evaluate("() => fetch('/api').then(r => r.json())")
    assert got == {"stubbed": True}


async def test_an_unsupported_feature_raises_and_names_itself(page) -> None:
    """§15.4. A compatibility layer whose gaps are silent turns a
    missing feature into a wrong result."""
    with pytest.raises(NotImplementedError, match="screenshot"):
        await page.locator("#text").screenshot()


async def test_the_error_types_are_importable_and_catch(page) -> None:
    """A Playwright suite writes ``except TimeoutError`` and
    ``except Error``; both have to exist and both have to catch.

    An assertion is *not* one of them. Measured against Playwright 1.62:
    `expect` raises a plain `AssertionError`, which is neither -- so this uses
    an action, and the assertion contract is pinned separately below.
    """
    assert issubclass(TimeoutError, Error)
    assert not issubclass(TimeoutError, AssertionError)
    await page.goto("/index.html")
    with pytest.raises(Error):
        await page.locator("#nothing-here").click(timeout=200)


async def test_a_firefox_launch_is_refused_permanently(browser: Browser) -> None:
    """§15.1: not a gap. Launching Chromium under another name
    would make a cross-browser suite report three passes for one browser."""
    from wirespec.compat.async_api import async_playwright

    async with async_playwright() as playwright:
        with pytest.raises(NotImplementedError, match="webkit"):
            await playwright.webkit.launch()


async def test_an_ignored_argument_is_refused_rather_than_ignored(page) -> None:
    """Accepting ``position=`` and dropping it clicks the middle of the element
    instead, and the spec passes (§15.4)."""
    await page.goto("/actions.html")
    with pytest.raises(NotImplementedError, match="position"):
        await page.locator("#plain").click(position={"x": 1, "y": 1})


async def test_an_assertion_playwright_has_and_wirespec_does_not_is_refused(page) -> None:
    await page.goto("/index.html")
    with pytest.raises(NotImplementedError, match="to_have_class"):
        await expect(page.locator("h1")).to_have_class("headline")


async def test_reading_cookies_back_is_refused_by_name(browser: Browser, site: str) -> None:
    """wirespec seeds a jar and does not read it back, so this is a **gap**
    rather than a decision -- and it says so on the line that used it."""
    context = await browser.new_context(base_url=site)
    try:
        await context.add_cookies([{"name": "seeded", "value": "yes", "url": site}])
        with pytest.raises(NotImplementedError, match="cookies"):
            await context.cookies()
    finally:
        await context.close()


async def test_expect_response_catches_what_the_block_causes(page) -> None:
    await page.goto("/network.html")
    async with page.expect_response(lambda response: response.url.endswith("/api")) as caught:
        await page.evaluate("() => window.__fetch('/api')")
    response = await caught.value
    assert response.status == 200
    assert response.request.method == "GET"
    assert (await response.json())["from"] == "the server"


async def test_context_routes_reach_a_page_opened_afterwards(browser: Browser, site: str) -> None:
    context = await browser.new_context(base_url=site)
    try:
        await context.route("**/api", lambda route: route.fulfill(body='{"from": "the route"}'))
        page = await context.new_page()
        await page.goto("/network.html")
        got = await page.evaluate("() => fetch('/api').then(r => r.json())")
        assert got == {"from": "the route"}
    finally:
        await context.close()


async def test_route_abort_takes_playwrights_lowercase_error_code(page) -> None:
    """Playwright spells it ``"failed"``; CDP spells it ``"Failed"``. The wrong
    case is a protocol error, not a silent no-op, so the conversion is here."""
    await page.route("**/api", lambda route: route.abort("failed"))
    await page.goto("/network.html")
    failed = await page.evaluate("() => fetch('/api').then(() => 'ok', () => 'refused')")
    assert failed == "refused"


async def test_wait_for_selector_takes_milliseconds_too(page) -> None:
    await page.goto("/assertions.html")
    found = await page.wait_for_selector("#one", timeout=5000)
    assert await found.text_content() == "the only one"
    with pytest.raises(TimeoutError):
        await page.wait_for_selector("#never", timeout=250)


async def test_select_option_is_available_and_takes_playwrights_spellings(page) -> None:
    await page.goto("/actions.html")
    assert await page.locator("#fruit").select_option("d") == ["d"]
    assert await page.locator("#fruit").select_option(["b"]) == ["b"]
    assert await page.locator("#fruit").select_option(label="Apple") == ["a"]
    assert await page.locator("#many").select_option(["q", "t"]) == ["q", "t"]
    assert await page.locator("#many").select_option(index=[0, 4]) == ["p", "t"]
    with pytest.raises(NotImplementedError, match="holds one option"):
        await page.locator("#fruit").select_option(["a", "b"])


async def test_set_input_files_is_available(page, tmp_path) -> None:
    payload = tmp_path / "invoice.pdf"
    payload.write_bytes(b"%PDF-1.4\n")
    await page.goto("/actions.html")
    await page.locator("#file").set_input_files(str(payload))
    assert await page.evaluate("() => document.getElementById('file').files[0].name") == "invoice.pdf"


async def test_expect_popup_catches_a_new_tab(page, context) -> None:
    await page.goto("/popup.html")
    async with page.expect_popup() as popup:
        await page.locator("#open").click()
    opened = await popup.value
    assert opened.url.endswith("/index.html")
    assert len(context.pages) == 2


async def test_frame_locator_reaches_into_a_frame(page) -> None:
    """Playwright's spelling exactly, including that the FrameLocator is not a
    Locator: it builds, and what it builds is a Locator."""
    await page.goto("/frames.html")
    frame = page.frame_locator("#widget")
    assert await frame.locator("h1").text_content() == "inside the frame"
    await expect(frame.get_by_role("button", name="Press me")).to_be_visible()
    assert await frame.owner.get_attribute("id") == "widget"


async def test_content_frame_and_nesting_come_through_too(page) -> None:
    await page.goto("/frames.html")
    assert await page.locator("#second").content_frame.locator("h1").text_content() == "inside the frame"
    deep = page.frame_locator("#outer").frame_locator("#deepest")
    assert await deep.locator("h1").text_content() == "inside the frame"


async def test_a_gap_on_a_frame_locator_refuses_by_name(page) -> None:
    """The FrameLocator wrapper is a wrapper like every other one: what
    Playwright has and wirespec does not raises here rather than forwarding."""
    await page.goto("/frames.html")
    with pytest.raises(NotImplementedError, match="FrameLocator.get_by_alt_text"):
        page.frame_locator("#widget").get_by_alt_text("anything")


async def test_a_dialog_is_dismissed_when_nobody_listens(page) -> None:
    """Playwright's documented default, and wirespec's for the same reason: a
    suite that never mentions dialogs must not stop at the first one."""
    await page.goto("/dialogs.html")
    await page.locator("#confirm").click()
    await expect(page.locator("#answer")).to_have_text("cancelled")


async def test_page_on_dialog_registers_without_being_awaited(page) -> None:
    """``page.on`` is **synchronous** in Playwright and a suite never awaits it.
    Forwarding wirespec's coroutine would register nothing at all, and the
    dialog would be dismissed by the default while the suite passed."""
    seen = []

    async def answer(dialog) -> None:
        seen.append((dialog.type, dialog.message, dialog.default_value))
        await dialog.accept("Ada")

    page.on("dialog", answer)
    await page.goto("/dialogs.html")
    await page.locator("#prompt").click()

    await expect(page.locator("#answer")).to_have_text("Ada")
    assert seen == [("prompt", "your name?", "default name")]


async def test_a_dialog_knows_which_page_asked(page) -> None:
    pages = []
    page.on("dialog", lambda dialog: pages.append(dialog.page))
    await page.goto("/dialogs.html")
    await page.locator("#confirm").click()
    await expect(page.locator("#answer")).to_have_text("cancelled")
    assert pages[0] is page


async def test_a_gap_on_a_dialog_refuses_by_name(page) -> None:
    seen = []
    page.on("dialog", lambda dialog: seen.append(dialog))
    await page.goto("/dialogs.html")
    await page.locator("#confirm").click()
    await expect(page.locator("#answer")).to_have_text("cancelled")
    with pytest.raises(NotImplementedError, match="Dialog.default_prompt"):
        # The protocol's spelling of `default_value`. Refused rather than
        # quietly absent, which is what forwarding to the native object would
        # have made it.
        _ = seen[0].default_prompt


async def test_evaluate_takes_playwrights_second_argument(page) -> None:
    """A suite written against Playwright passes values positionally, and a
    signature that refused one would be a TypeError on a line that works."""
    await page.goto("/readers.html")
    assert await page.evaluate("x => x * 2", 21) == 42
    assert await page.locator("#lines").evaluate("(node, suffix) => node.tagName + suffix", "!") == "DIV!"


async def test_a_failed_assertion_is_an_assertion_error(page) -> None:
    """Playwright's `expect` raises `AssertionError`, and a suite is entitled to
    catch it -- soft assertions and `pytest.raises` both do. wirespec's own
    raises a `WirespecTimeoutError`, which is right for wirespec and wrong for
    this surface (§15.4, found by the differential suite)."""
    await page.goto("/index.html")
    with pytest.raises(AssertionError) as raised:
        await expect(page.locator("h1")).to_have_text("something else", timeout=200)
    # And it still says what it saw: the message is the diagnosis.
    assert "something else" in str(raised.value)


async def test_expect_blocks_hand_back_an_awaitable_value(page) -> None:
    """Playwright's async API spells it ``await info.value`` -- a property, not
    a call. A method here reads as working right up to ``TypeError: 'method'
    object can't be awaited``, which names nothing."""
    await page.goto("/popup.html")
    async with page.expect_popup() as popup:
        await page.locator("#open").click()
    opened = await popup.value
    assert opened.url.endswith("/index.html")

    await page.goto("/network.html")
    async with page.expect_response(lambda response: response.url.endswith("/api")) as caught:
        await page.locator("#go").click()
    assert (await caught.value).status == 200


async def test_expect_refuses_an_argument_it_would_have_ignored(page) -> None:
    """``expect(locator, timeout=300)`` is not Playwright's spelling -- the
    timeout goes on the assertion -- and swallowing it meant a suite got the
    default five seconds while believing it had asked for 300 ms."""
    await page.goto("/index.html")
    with pytest.raises(NotImplementedError, match="timeout"):
        expect(page.locator("h1"), timeout=300)


async def test_title_and_the_assertion_over_it(page) -> None:
    await page.goto("/index.html")
    assert await page.title() == "wirespec driver fixture"
    await expect(page).to_have_title("wirespec driver fixture")


async def test_page_context_is_the_context_that_opened_it(page, context) -> None:
    """``page.context`` is how a Playwright suite reaches the context-wide
    route, and it has to be the **same object**: a fresh wrapper each time
    makes ``page.context is context`` false, which reads as a bug in the suite
    rather than in the layer under it."""
    assert page.context is context
    await page.context.route("**/api", lambda route: route.fulfill(body='{"from": "the context"}'))
    await page.goto("/network.html")
    await page.locator("#go").click()
    await expect(page.locator("#out")).to_have_text('{"from": "the context"}')


async def test_the_pages_a_context_lists_are_the_same_objects(page, context) -> None:
    """The same rule from the other side. ``page in context.pages`` is how a
    suite asserts a tab is still open."""
    assert page in context.pages


async def test_mouse_and_keyboard_are_importable_names(page) -> None:
    """A page object annotates its parameters, and an annotation for a name the
    module does not export is an ImportError at collection time -- the whole
    file, before a single test runs."""
    from wirespec.compat.async_api import Keyboard, Mouse

    assert isinstance(page.mouse, Mouse)
    assert isinstance(page.keyboard, Keyboard)


async def test_locator_page_is_the_page_it_was_built_from(page) -> None:
    """A page object holds a Locator and reaches the page through it. 120 of
    the pilot suite's 229 specs did exactly that (§15.3)."""
    await page.goto("/index.html")
    assert page.locator("h1").page is page


async def test_page_request_counts_in_milliseconds(page) -> None:
    """The silent thousandfold again (§15.2): handed over
    unwrapped, ``page.request.get(url, timeout=250)`` is a wait of 250
    **seconds**, and a suite whose API calls quietly stopped timing out still
    passes -- slowly, and then not at all."""
    import time

    await page.goto("/network.html")
    assert (await page.request.get("/echo")).ok

    started = time.monotonic()
    # The budget is outside and raises the *builtin* TimeoutError, so an
    # unconverted 250 fails this in five seconds rather than waiting the four
    # minutes it was actually asked for.
    async with asyncio.timeout(5.0):
        with pytest.raises(Error):
            await page.request.get("/slow", timeout=250)
    assert time.monotonic() - started < 5.0


async def test_page_request_refuses_an_argument_it_would_have_ignored(page) -> None:
    await page.goto("/index.html")
    with pytest.raises(NotImplementedError, match="max_redirects"):
        await page.request.get("/echo", max_redirects=2)


async def test_page_request_takes_playwrights_data_shapes(page) -> None:
    await page.goto("/index.html")
    answer = await page.request.post("/echo", data={"a": 1})
    assert answer.status == 200
    assert (await answer.json())["content-type"] == "application/json"
