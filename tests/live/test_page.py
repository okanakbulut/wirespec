"""``Page`` against real Chrome: navigation, lifecycle, dialogs, screenshots."""

import asyncio
import base64

import pytest

from tests.live.support import drain_until, evaluate, goto
from wirespec.cdp import emulation, page, runtime, target
from wirespec.connection import Connection, Session

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_navigate_commits_a_frame(live: Session, site: str) -> None:
    """Page.navigate. The frame id it returns is the one the document ends up
    in, which is what every later frame-scoped call has to be given."""
    async with live.expect(page.LoadEventFired, timeout=15.0):
        result = await live.send(page.Navigate(url=f"{site}/index.html"))
    assert result.error_text is None
    assert result.frame_id
    tree = await live.send(page.GetFrameTree())
    assert tree.frame_tree.frame.id == result.frame_id
    assert tree.frame_tree.frame.url == f"{site}/index.html"


async def test_enable_is_what_turns_the_events_on(connection: Connection, site: str) -> None:
    """Page.enable / Page.disable. Navigation works either way; the difference
    is whether anyone hears about it."""
    context = await connection.send(target.CreateBrowserContext())
    created = await connection.send(
        target.CreateTarget(url="about:blank", browser_context_id=context.browser_context_id)
    )
    fresh = await connection.attach(created.target_id)
    try:
        with fresh.queue(page.LoadEventFired) as before_enable:
            await fresh.send(page.Navigate(url=f"{site}/index.html"))
            await asyncio.sleep(0.5)
        assert before_enable.qsize() == 0, "events should not arrive before Page.enable"

        await fresh.send(page.Enable())
        async with fresh.expect(page.LoadEventFired, timeout=15.0) as loaded:
            await fresh.send(page.Navigate(url=f"{site}/tall.html"))
        assert loaded.result().timestamp > 0

        await fresh.send(page.Disable())
        with fresh.queue(page.LoadEventFired) as after_disable:
            await fresh.send(page.Navigate(url=f"{site}/index.html"))
            await asyncio.sleep(0.5)
        assert after_disable.qsize() == 0, "events should stop after Page.disable"
    finally:
        await connection.send(target.CloseTarget(target_id=created.target_id))
        await connection.send(target.DisposeBrowserContext(browser_context_id=context.browser_context_id))


async def test_a_navigation_failure_arrives_as_a_result(live: Session) -> None:
    """Chrome reports an unreachable host in ``error_text`` rather than as a
    protocol error. A driver that only catches CDPError sails straight past
    this and fails somewhere else entirely, several steps later."""
    result = await live.send(page.Navigate(url="http://127.0.0.1:1/nothing-here"))
    assert result.error_text
    assert result.frame_id


async def test_the_document_and_load_events_arrive_in_order(live: Session, site: str) -> None:
    """Page.domContentEventFired then Page.loadEventFired. The gap between them
    is where sub-resources load, which is why waiting for the wrong one is a
    flake rather than a failure."""
    with live.queue(page.DomContentEventFired) as dom_ready, live.queue(page.LoadEventFired) as loaded:
        await live.send(page.Navigate(url=f"{site}/tall.html"))
        dom_event = await drain_until(dom_ready, lambda event: True, timeout=15.0)
        load_event = await drain_until(loaded, lambda event: True, timeout=15.0)
    assert dom_event.timestamp <= load_event.timestamp


async def test_frame_navigated_and_stopped_loading_describe_the_commit(live: Session, site: str) -> None:
    """Page.frameNavigated carries the frame that committed; frameStoppedLoading
    says it is finished. Together they bracket a navigation."""
    with (
        live.queue(page.FrameNavigated) as navigated,
        live.queue(page.FrameStoppedLoading) as stopped,
    ):
        result = await live.send(page.Navigate(url=f"{site}/index.html"))
        committed = await drain_until(navigated, lambda event: event.frame.id == result.frame_id, timeout=15.0)
        finished = await drain_until(stopped, lambda event: event.frame_id == result.frame_id, timeout=15.0)
    assert committed.frame.url == f"{site}/index.html"
    assert committed.frame.loader_id
    assert finished.frame_id == result.frame_id


async def test_a_same_document_navigation_reports_itself_differently(live: Session, site: str) -> None:
    """Page.navigatedWithinDocument, and the absence that makes it necessary.

    Chrome sends this *instead of* frameNavigated for a fragment or a History
    API call, and fires no load event at all. Measured on Chrome 150: a
    navigation to ``#fragment`` produces frameStartedNavigating with
    ``navigationType: "sameDocument"``, then this, then frameStoppedLoading --
    and nothing else. A driver waiting for load waits out its whole timeout on
    every anchor click.
    """
    await goto(live, f"{site}/index.html")
    with (
        live.queue(page.NavigatedWithinDocument) as within,
        live.queue(page.LoadEventFired) as loaded,
        live.queue(page.FrameNavigated) as navigated,
    ):
        result = await live.send(page.Navigate(url=f"{site}/index.html#fragment"))
        # No loader is allocated, which is the cheapest way to tell the two
        # kinds apart -- it is in the reply, before any event arrives.
        assert result.loader_id is None
        event = await drain_until(within, lambda seen: seen.frame_id == result.frame_id, timeout=15.0)
        await asyncio.sleep(0.3)
        assert loaded.qsize() == 0, "a same-document navigation fires no load event"
        assert navigated.qsize() == 0, "a same-document navigation fires no frameNavigated"
    assert event.url == f"{site}/index.html#fragment"
    assert event.navigation_type == "fragment"


async def test_lifecycle_events_are_opt_in(live: Session, site: str) -> None:
    """Page.setLifecycleEventsEnabled, and Page.lifecycleEvent.

    Finer-grained than load: ``firstContentfulPaint`` and friends are the only
    way to wait for a page that never fires load because something on it is
    still streaming.
    """
    await live.send(page.SetLifecycleEventsEnabled(enabled=True))
    try:
        with live.queue(page.LifecycleEvent) as lifecycle:
            await live.send(page.Navigate(url=f"{site}/tall.html"))
            names = set()
            for _ in range(40):
                event = await drain_until(lifecycle, lambda _: True, timeout=15.0)
                names.add(event.name)
                assert event.frame_id and event.loader_id
                if {"init", "DOMContentLoaded", "load"} <= names:
                    break
        assert {"init", "DOMContentLoaded", "load"} <= names, names
    finally:
        await live.send(page.SetLifecycleEventsEnabled(enabled=False))


async def test_reload_runs_the_document_again(live: Session, site: str) -> None:
    """Page.reload. The marker set on the window is gone afterwards, which is
    how you tell a reload from a no-op."""
    await goto(live, f"{site}/index.html")
    await evaluate(live, "window.__survived = true")
    assert await evaluate(live, "window.__survived") is True

    async with live.expect(page.LoadEventFired, timeout=15.0):
        await live.send(page.Reload(ignore_cache=True))
    assert await evaluate(live, "window.__survived") is None
    assert await evaluate(live, "window.__page") == "index"


async def test_stop_loading_ends_a_navigation_in_flight(live: Session, site: str) -> None:
    """Page.stopLoading, against a document whose stylesheet the server is
    deliberately sitting on. The stop has to land while the page is still
    loading, so the navigation is fired and not awaited."""
    await goto(live, f"{site}/index.html")
    navigating = asyncio.ensure_future(live.send(page.Navigate(url=f"{site}/slow.html")))
    try:
        with live.queue(page.FrameStoppedLoading) as stopped:
            # Long enough for the document to commit and the stylesheet request
            # to be outstanding; far short of the server's ten-second stall.
            await asyncio.sleep(1.0)
            await live.send(page.StopLoading())
            await drain_until(stopped, lambda event: True, timeout=15.0)
        assert await evaluate(live, "document.readyState") in {"interactive", "complete"}
        assert await evaluate(live, "document.title") == "slow page"
    finally:
        navigating.cancel()


async def test_a_script_runs_before_anything_in_the_document(live: Session, site: str) -> None:
    """Page.addScriptToEvaluateOnNewDocument / removeScriptToEvaluateOnNewDocument.

    The hook runs before the page's own first line, which is the only place a
    driver can install anything the page will then use.
    """
    added = await live.send(page.AddScriptToEvaluateOnNewDocument(source="window.__early = 'ran before the page';"))
    assert added.identifier
    try:
        await goto(live, f"{site}/index.html")
        assert await evaluate(live, "window.__early") == "ran before the page"
    finally:
        await live.send(page.RemoveScriptToEvaluateOnNewDocument(identifier=added.identifier))

    await goto(live, f"{site}/index.html")
    assert await evaluate(live, "window.__early") is None


async def test_bring_to_front_activates_the_page(live: Session, site: str) -> None:
    """Page.bringToFront. Headless still tracks visibility, and a page Chrome
    thinks is hidden gets its timers throttled."""
    await goto(live, f"{site}/index.html")
    await live.send(page.BringToFront())
    assert await evaluate(live, "document.visibilityState") == "visible"


async def test_a_screenshot_comes_back_as_a_png(live: Session, site: str) -> None:
    """Page.captureScreenshot. Base64, because the protocol has no way to carry
    bytes."""
    await goto(live, f"{site}/tall.html")
    await live.send(emulation.SetDeviceMetricsOverride(width=400, height=300, device_scale_factor=1.0, mobile=False))
    try:
        shot = await live.send(page.CaptureScreenshot(format="png"))
        decoded = base64.b64decode(shot.data)
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n"
        width = int.from_bytes(decoded[16:20], "big")
        height = int.from_bytes(decoded[20:24], "big")
        assert (width, height) == (400, 300)
    finally:
        await live.send(emulation.ClearDeviceMetricsOverride())


async def test_a_screenshot_can_be_clipped_to_one_region(live: Session, site: str) -> None:
    """The ``clip`` viewport, which is what turns a full-page shot into an
    element shot once the element's box is known."""
    await goto(live, f"{site}/tall.html")
    shot = await live.send(
        page.CaptureScreenshot(format="png", clip=page.Viewport(x=0, y=0, width=120, height=80, scale=1.0))
    )
    decoded = base64.b64decode(shot.data)
    assert int.from_bytes(decoded[16:20], "big") == 120
    assert int.from_bytes(decoded[20:24], "big") == 80


async def test_a_screenshot_can_reach_past_the_viewport(live: Session, site: str) -> None:
    """``capture_beyond_viewport`` renders what is scrolled off, which is the
    difference between a full-page screenshot and a picture of the fold."""
    await goto(live, f"{site}/tall.html")
    await live.send(emulation.SetDeviceMetricsOverride(width=400, height=300, device_scale_factor=1.0, mobile=False))
    try:
        shot = await live.send(page.CaptureScreenshot(format="png", capture_beyond_viewport=True))
        decoded = base64.b64decode(shot.data)
        assert int.from_bytes(decoded[20:24], "big") > 300
    finally:
        await live.send(emulation.ClearDeviceMetricsOverride())


async def test_a_jpeg_screenshot_honours_quality(live: Session, site: str) -> None:
    """``quality`` applies to JPEG only, and is the knob that decides whether a
    failure artefact is 40 kB or 400 kB."""
    await goto(live, f"{site}/tall.html")
    coarse = await live.send(page.CaptureScreenshot(format="jpeg", quality=10))
    fine = await live.send(page.CaptureScreenshot(format="jpeg", quality=95))
    assert base64.b64decode(coarse.data)[:3] == b"\xff\xd8\xff"
    assert len(base64.b64decode(coarse.data)) < len(base64.b64decode(fine.data))


async def test_a_screencast_delivers_acked_frames(live: Session, site: str) -> None:
    """Page.startScreencast / screencastFrame / screencastFrameAck / stopScreencast.

    The four members the failure artefact is built on (§16). Every
    frame is a JPEG and carries metadata describing the page at the moment it
    was taken -- including a ``timestamp`` in **epoch seconds**, which is the
    clock the network events do not use (§16.2).

    The ack is not optional. Chrome hands out a frame and then waits to be told
    it arrived before rendering another; a recorder that never acks gets one or
    two frames and a filmstrip that stops at the beginning. Acking from inside
    the event handler is the other half of the trap and is why the driver's
    recorder acks off the read path -- here the test is not on the read path, so
    it can ack inline.
    """
    await goto(live, f"{site}/animating.html")
    with live.queue(page.ScreencastFrame) as frames:
        await live.send(page.StartScreencast(format="jpeg", quality=60, every_nth_frame=1))
        try:
            seen = []
            for _ in range(4):
                frame = await drain_until(frames, lambda _: True, timeout=15.0)
                seen.append(frame)
                await live.send(page.ScreencastFrameAck(session_id=frame.session_id))
        finally:
            await live.send(page.StopScreencast())

    assert len(seen) == 4
    for frame in seen:
        assert base64.b64decode(frame.data)[:3] == b"\xff\xd8\xff"
        assert frame.metadata.device_width > 0 and frame.metadata.device_height > 0
        # Epoch seconds, not the monotonic clock Network uses. Anything after
        # 2020 proves which one this is without pinning a date.
        assert frame.metadata.timestamp is not None and frame.metadata.timestamp > 1_577_836_800
    # Session ids are per frame and increment, which is what the ack matches on.
    assert [frame.session_id for frame in seen] == sorted(frame.session_id for frame in seen)


async def test_the_frame_tree_includes_child_frames(live: Session, site: str) -> None:
    """Page.getFrameTree. An iframe is a child frame with its own id, which is
    what a frame-scoped navigation has to be given."""
    await goto(live, f"{site}/frames.html")
    tree = await live.send(page.GetFrameTree())
    assert tree.frame_tree.frame.url == f"{site}/frames.html"
    assert tree.frame_tree.child_frames
    child = tree.frame_tree.child_frames[0]
    assert child.frame.url == f"{site}/index.html"
    assert child.frame.parent_id == tree.frame_tree.frame.id


@pytest.mark.parametrize(
    ("button", "accept", "prompt_text", "expected"),
    [
        ("alert", True, None, "alert dismissed"),
        ("confirm", True, None, "confirmed"),
        ("confirm", False, None, "cancelled"),
        ("prompt", True, "wirespec", "wirespec"),
    ],
)
async def test_a_dialog_blocks_until_it_is_handled(
    live: Session, site: str, button: str, accept: bool, prompt_text: str | None, expected: str
) -> None:
    """Page.javascriptDialogOpening and Page.handleJavaScriptDialog.

    The click cannot be awaited: ``alert`` blocks the renderer, so the
    ``Runtime.evaluate`` that triggered it does not answer until the dialog is
    handled. A driver that awaits the click and only then looks for the dialog
    deadlocks -- and reports it as whatever it was doing at the time.
    """
    await goto(live, f"{site}/dialogs.html")
    async with live.expect(page.JavascriptDialogOpening, timeout=15.0) as opening:
        clicking = asyncio.ensure_future(
            live.send(runtime.Evaluate(expression=f"document.getElementById('{button}').click()"))
        )
    dialog = opening.result()
    assert dialog.type == button
    assert dialog.url.endswith("/dialogs.html")
    if button == "prompt":
        assert dialog.default_prompt == "default name"

    await live.send(page.HandleJavaScriptDialog(accept=accept, prompt_text=prompt_text))
    await clicking
    assert await evaluate(live, "document.getElementById('answer').textContent") == expected


async def test_layout_metrics_report_the_scroll_offset(live: Session, site: str) -> None:
    """Page.getLayoutMetrics, and the reason wirespec needs it.

    ``DOM.getNodeForLocation`` hit-tests in **document** coordinates, while
    ``DOM.getBoxModel`` returns viewport coordinates and
    ``Input.dispatchMouseEvent`` takes them. The two agree on an unscrolled page
    and diverge by exactly this offset on every other one, so a driver that does
    not add it works until a page scrolls and then refuses every click below the
    fold (§8.13).
    """
    await goto(live, f"{site}/tall.html")
    before = await live.send(page.GetLayoutMetrics())
    assert before.css_visual_viewport.page_y == 0

    await evaluate(live, "window.scrollTo(0, 500)")
    after = await live.send(page.GetLayoutMetrics())
    assert after.css_visual_viewport.page_y == pytest.approx(500, abs=2)
    assert after.css_layout_viewport.client_height > 0
