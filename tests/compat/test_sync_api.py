"""The sync facade, which is §15.2's largest piece.

Written the way a Playwright sync suite is written: no ``async``, no ``await``,
no event loop in sight. What is actually being tested is the threading, because
everything else is the async layer these calls delegate to and is already
covered by ``test_async_api.py``.

The failure this suite exists for is the one §15.2 names: **a sync facade that
deadlocks the first time a route handler calls back into the page is worse than
no sync facade.**
"""

import pytest

from wirespec.compat.sync_api import TimeoutError, expect, sync_playwright


@pytest.fixture(scope="module")
def sync_browser(chrome_binary: str):
    with sync_playwright() as playwright:
        launched = playwright.chromium.launch(executable_path=chrome_binary)
        try:
            yield launched
        finally:
            launched.close()


@pytest.fixture
def page(sync_browser, site: str):
    context = sync_browser.new_context(base_url=site, viewport={"width": 900, "height": 700})
    try:
        yield context.new_page()
    finally:
        context.close()


def test_a_whole_spec_runs_with_no_await_anywhere(page) -> None:
    page.goto("/list.html")
    rows = page.get_by_role("list").get_by_role("listitem")
    expect(rows).to_have_count(3)
    expect(rows.first).to_have_text("Acme")
    assert rows.first.text_content() == "Acme"
    assert page.evaluate("() => window.innerWidth") == 900


def test_actions_run_synchronously(page) -> None:
    page.goto("/actions.html")
    page.locator("#plain").click()
    page.locator("#text").fill("typed")
    assert page.locator("#text").input_value() == "typed"
    assert page.evaluate("() => window.__log")[0] == "plain"


def test_a_route_handler_may_call_back_into_the_page(page) -> None:
    """§15.2's named failure mode, and the reason handlers run in a
    thread pool.

    The handler is a **sync** function running while the loop is busy inside
    ``goto``. Every call it makes -- ``route.request.url``, ``route.fulfill`` --
    has to get back onto that same loop. Run the handler *on* the loop and it
    waits for a loop that is waiting for it: a deadlock, on the first route any
    suite installs.
    """
    seen = []

    def stub(route):
        seen.append(route.request.url)
        route.fulfill(status=201, body='{"stubbed": true}')

    page.route("**/api", stub)
    page.goto("/network.html")

    assert page.evaluate("() => fetch('/api').then(r => r.json())") == {"stubbed": True}
    assert seen and seen[0].endswith("/api")


def test_page_on_takes_a_sync_handler_too(page) -> None:
    urls = []
    page.on("request", lambda request: urls.append(request.url))
    page.goto("/network.html")
    assert any("network.html" in url for url in urls)


def test_timeouts_are_still_milliseconds(page) -> None:
    import time

    page.goto("/index.html")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        # An action, not an assertion: Playwright raises `TimeoutError` for the
        # first and a plain `AssertionError` for the second.
        page.locator("#nothing-here").click(timeout=250)
    assert time.monotonic() - started < 5.0


def test_a_gap_still_refuses_and_names_itself(page) -> None:
    """The refusals live in the async layer, and the sync facade is a mirror
    over it -- so there is one place where Playwright's semantics are decided
    and one place where they can be wrong (§15.4)."""
    page.goto("/actions.html")
    with pytest.raises(NotImplementedError, match="screenshot"):
        page.locator("#text").screenshot()
    with pytest.raises(NotImplementedError, match="position"):
        page.locator("#plain").click(position={"x": 1, "y": 1})


def test_expect_response_works_as_a_plain_with_block(page) -> None:
    page.goto("/network.html")
    with page.expect_response(lambda response: response.url.endswith("/api")) as caught:
        page.evaluate("() => window.__fetch('/api')")
    assert caught.value.status == 200


def test_calling_back_from_the_loop_thread_raises_instead_of_hanging(page) -> None:
    """The deadlock's *shape*, caught structurally rather than by waiting.

    A predicate runs on the loop thread by design -- it has to answer now, with
    a bool -- so one that calls back into the page is asking the loop to wait
    for itself. There is no timeout that helps: the loop is what would fire it,
    and every later call, teardown included, queues behind the stuck one. So the
    portal refuses the call outright, naming what happened
    (§15.2).

    Measured: with the route dispatch broken to run inline, this turns a suite
    that hung for ever into one that failed in 0.7 seconds.
    """
    page.goto("/network.html")
    with (
        pytest.raises(RuntimeError, match="event loop thread"),
        page.expect_response(lambda response: page.evaluate("() => 1") == 1),
    ):
        page.evaluate("() => window.__fetch('/api')")


def test_frame_locator_works_without_a_loop(page) -> None:
    """A FrameLocator is a plain builder -- nothing to await -- so what this
    checks is that the sync facade wraps it rather than handing back the async
    object, whose Locators would then be coroutines."""
    page.goto("/frames.html")
    frame = page.frame_locator("#widget")
    assert frame.locator("h1").text_content() == "inside the frame"
    assert frame.owner.get_attribute("id") == "widget"
    assert page.locator("#second").content_frame.locator("h1").text_content() == "inside the frame"


def test_a_dialog_handler_runs_off_the_loop_thread(page) -> None:
    """The one place a sync ``page.on`` handler cannot be fire-and-forget.

    A request listener is never awaited and may run whenever it likes. A dialog
    handler is awaited -- the page is stopped until it answers -- so a listener
    dispatched to the events thread and forgotten would let wirespec's default
    dismiss the dialog first, and the suite's ``accept`` would then arrive for a
    dialog that had already closed (§8.20).
    """
    page.goto("/dialogs.html")
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#confirm").click()
    expect(page.locator("#answer")).to_have_text("confirmed")


def test_a_dialog_nobody_listens_for_is_dismissed_without_a_loop(page) -> None:
    page.goto("/dialogs.html")
    page.locator("#confirm").click()
    expect(page.locator("#answer")).to_have_text("cancelled")


def test_expect_blocks_hand_back_a_plain_value_without_a_loop(page) -> None:
    """The sync half of Playwright's spelling: a property, and not awaited."""
    page.goto("/popup.html")
    with page.expect_popup() as popup:
        page.locator("#open").click()
    assert popup.value.url.endswith("/index.html")


def test_a_failed_assertion_is_an_assertion_error_here_too(page) -> None:
    page.goto("/index.html")
    with pytest.raises(AssertionError):
        expect(page.locator("h1")).to_have_text("something else", timeout=200)
