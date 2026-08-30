"""Everything above, against a real Chrome over a real pipe.

This is the layer that decides whether the subset is right. The loopback tests
prove the machinery; only Chrome proves the method names, the parameter
spellings and the shapes coming back.
"""

import asyncio
import base64
import os
import time

import pytest

from tests.support import chrome
from wirespec.cdp import browser, dom, emulation, fetch, network, page, runtime, target
from wirespec.cdp import input as input_domain
from wirespec.connection import Connection, Session
from wirespec.errors import CDPError

# Everything here shares the session-scoped Chrome, so it shares its loop.
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def evaluate(session: Session, expression: str) -> object:
    result = await session.send(runtime.Evaluate(expression=expression, return_by_value=True, await_promise=True))
    assert result.exception_details is None, result.exception_details
    return result.result.value


async def goto(session: Session, url: str) -> None:
    async with session.expect(page.LoadEventFired, timeout=15.0):
        result = await session.send(page.Navigate(url=url))
        assert result.error_text is None, result.error_text


async def test_the_transport_carries_a_round_trip(connection: Connection) -> None:
    """§13 step 1: provable with Browser.getVersion."""
    version = await connection.send(browser.GetVersion())
    assert version.product.startswith(("Chrome/", "HeadlessChrome/"))
    assert version.protocol_version
    assert version.user_agent


async def test_a_flat_session_addresses_its_own_page(connection: Connection, session: Session) -> None:
    """One connection, every page, each message naming its sessionId."""
    assert await evaluate(session, "1 + 1") == 2
    other = await connection.send(target.CreateTarget(url="about:blank"))
    second = await connection.attach(other.target_id)
    try:
        await second.send(runtime.Enable())
        await evaluate(session, "window.__mark = 'first'")
        await evaluate(second, "window.__mark = 'second'")
        assert await evaluate(session, "window.__mark") == "first"
        assert await evaluate(second, "window.__mark") == "second"
    finally:
        await connection.send(target.CloseTarget(target_id=other.target_id))


async def test_navigation_and_the_load_event(session: Session, server: str) -> None:
    async with session.expect(page.LoadEventFired, timeout=15.0) as loaded:
        result = await session.send(page.Navigate(url=f"{server}/"))
        assert result.error_text is None
        assert result.frame_id
    assert loaded.result().timestamp > 0
    assert await evaluate(session, "document.title") == "root"


async def test_a_navigation_failure_comes_back_as_a_result_not_an_error(session: Session) -> None:
    """Chrome reports this in the result, so a driver that only catches protocol
    errors sails past it and fails somewhere else entirely."""
    result = await session.send(page.Navigate(url="http://127.0.0.1:1/nothing-here"))
    assert result.error_text


async def test_an_unknown_method_raises_naming_itself(connection: Connection) -> None:
    with pytest.raises(CDPError) as raised:
        await connection.send_raw("Nonexistent.method")
    assert "Nonexistent.method" in str(raised.value)


async def test_a_page_side_throw_is_a_result_not_a_protocol_error(session: Session) -> None:
    result = await session.send(runtime.Evaluate(expression="throw new Error('from the page')"))
    assert result.exception_details is not None
    thrown = result.exception_details.exception
    assert thrown is not None
    assert "from the page" in (thrown.description or "")


async def test_call_function_on_tells_null_apart_from_undefined(session: Session) -> None:
    """The UNSET design, checked where it actually matters. A CallArgument with
    no value passes `undefined`; one holding None passes `null`."""
    result = await session.send(
        runtime.CallFunctionOn(
            function_declaration="function (a, b) { return [a === null, b === undefined].join(','); }",
            object_id=await global_handle(session),
            arguments=[runtime.CallArgument(value=None), runtime.CallArgument()],
            return_by_value=True,
        )
    )
    assert result.exception_details is None, result.exception_details
    assert result.result.value == "true,true"


async def global_handle(session: Session) -> str:
    """A reference to the page's own globalThis, which is how callFunctionOn is
    pointed at the main world without naming a context id."""
    handle = await session.send(runtime.Evaluate(expression="globalThis"))
    assert handle.result.object_id is not None
    return handle.result.object_id


async def test_evaluate_returns_by_reference_when_asked(session: Session, server: str) -> None:
    """The model the whole design rests on: the array stays in the page and only
    the answer crosses the wire (§3.1)."""
    await goto(session, f"{server}/input.html")
    handle = await session.send(runtime.Evaluate(expression="document.querySelectorAll('*')"))
    assert handle.result.object_id is not None
    counted = await session.send(
        runtime.CallFunctionOn(
            function_declaration="function () { return this.length; }",
            object_id=handle.result.object_id,
            return_by_value=True,
        )
    )
    assert counted.result.value > 3
    await session.send(runtime.ReleaseObject(object_id=handle.result.object_id))


async def test_concurrent_calls_interleave(connection: Connection) -> None:
    """Ten calls in flight at once, each getting its own answer back."""
    results = await asyncio.gather(*(connection.send(browser.GetVersion()) for _ in range(10)))
    assert len({result.product for result in results}) == 1


async def test_a_large_payload_survives_the_framing(session: Session) -> None:
    """8 MiB in one message, which arrives in ~128 chunks and exercises every
    line of the carry-over path."""
    size = 8 << 20
    value = await evaluate(session, f"'x'.repeat({size})")
    assert isinstance(value, str)
    assert len(value) == size


async def test_a_large_payload_goes_out_as_well_as_comes_back(session: Session) -> None:
    payload = "y" * (4 << 20)
    result = await session.send(
        runtime.CallFunctionOn(
            function_declaration="function (s) { return s.length; }",
            object_id=await global_handle(session),
            arguments=[runtime.CallArgument(value=payload)],
            return_by_value=True,
        )
    )
    assert result.result.value == len(payload)


async def test_the_box_model_comes_back_by_object_id(session: Session, server: str) -> None:
    """No DOM.getDocument anywhere: passing an objectId straight to getBoxModel
    skips the node-id space entirely (§8.9)."""
    await goto(session, f"{server}/input.html")
    handle = await session.send(runtime.Evaluate(expression="document.getElementById('b')"))
    assert handle.result.object_id
    box = await session.send(dom.GetBoxModel(object_id=handle.result.object_id))
    left, top = box.model.border[0], box.model.border[1]
    assert (left, top) == pytest.approx((20.0, 30.0), abs=1.0)
    assert box.model.width == pytest.approx(120, abs=2)


async def test_a_dispatched_click_reaches_the_page(session: Session, server: str) -> None:
    await goto(session, f"{server}/input.html")
    handle = await session.send(runtime.Evaluate(expression="document.getElementById('b')"))
    assert handle.result.object_id
    box = await session.send(dom.GetBoxModel(object_id=handle.result.object_id))
    quad = box.model.border
    x = (quad[0] + quad[4]) / 2
    y = (quad[1] + quad[5]) / 2
    for kind in ("mousePressed", "mouseReleased"):
        await session.send(
            input_domain.DispatchMouseEvent(type=kind, x=x, y=y, button="left", buttons=1, click_count=1)
        )
    assert await evaluate(session, "window.__clicks") == 1


async def test_insert_text_and_a_key_event_reach_the_page(session: Session, server: str) -> None:
    await goto(session, f"{server}/input.html")
    await evaluate(session, "document.getElementById('f').focus()")
    await session.send(input_domain.InsertText(text="hello"))
    assert await evaluate(session, "document.getElementById('f').value") == "hello"
    await session.send(
        input_domain.DispatchKeyEvent(type="keyDown", key="Enter", code="Enter", windows_virtual_key_code=13)
    )
    await session.send(
        input_domain.DispatchKeyEvent(type="keyUp", key="Enter", code="Enter", windows_virtual_key_code=13)
    )
    assert await evaluate(session, "window.__keys") == ["Enter"]


async def test_the_viewport_can_be_set_explicitly(session: Session) -> None:
    """A headless window's default size is not a promise, and every measured box
    is relative to it (§8.9)."""
    await session.send(emulation.SetDeviceMetricsOverride(width=800, height=600, device_scale_factor=1.0, mobile=False))
    assert await evaluate(session, "innerWidth") == 800
    assert await evaluate(session, "innerHeight") == 600
    await session.send(emulation.ClearDeviceMetricsOverride())


async def test_a_cookie_set_over_cdp_is_visible_to_the_page(session: Session, server: str) -> None:
    await session.send(network.Enable())
    result = await session.send(network.SetCookie(name="wirespec", value="yes", url=server))
    assert result.success
    await goto(session, f"{server}/")
    assert "wirespec=yes" in str(await evaluate(session, "document.cookie"))


async def test_requests_can_be_watched(session: Session, server: str) -> None:
    await session.send(network.Enable())
    with session.queue(network.ResponseReceived) as responses:
        await goto(session, f"{server}/")
        await evaluate(session, f"fetch('{server}/api').then(r => r.text())")
        await asyncio.sleep(0.2)
    seen = [responses.get_nowait() for _ in range(responses.qsize())]
    api = [event for event in seen if event.response.url.endswith("/api")]
    assert api, [event.response.url for event in seen]
    assert api[0].response.status == 200
    assert api[0].response.headers.get("Content-Type") == "application/json"


async def test_a_request_can_be_answered_from_python(session: Session, server: str) -> None:
    """Fetch is enabled lazily and only for the pattern a spec actually routes."""
    await session.send(fetch.Enable(patterns=[fetch.RequestPattern(url_pattern="*/api")]))
    answered: list[str] = []

    # Answering happens in a task: the handler runs on the read path, where an
    # await would deadlock the very connection the reply has to go out on.
    replies: list[asyncio.Task[None]] = []

    def fulfil(event: fetch.RequestPaused) -> None:
        answered.append(event.request.url)
        replies.append(
            asyncio.create_task(
                session.send(
                    fetch.FulfillRequest(
                        request_id=event.request_id,
                        response_code=200,
                        response_headers=[fetch.HeaderEntry(name="Content-Type", value="application/json")],
                        body=base64.b64encode(b'{"from":"python"}').decode(),
                    )
                )
            )
        )

    unsubscribe = session.on(fetch.RequestPaused, fulfil)
    try:
        await goto(session, f"{server}/")
        body = await evaluate(session, f"fetch('{server}/api').then(r => r.text())")
        assert body == '{"from":"python"}'
        assert answered and answered[0].endswith("/api")
    finally:
        unsubscribe()
        await session.send(fetch.Disable())
        await asyncio.gather(*replies, return_exceptions=True)


async def test_a_script_can_run_before_the_document_does(session: Session, server: str) -> None:
    added = await session.send(page.AddScriptToEvaluateOnNewDocument(source="window.__early = 'ran before the page';"))
    try:
        await goto(session, f"{server}/")
        assert await evaluate(session, "window.__early") == "ran before the page"
    finally:
        await session.send(page.RemoveScriptToEvaluateOnNewDocument(identifier=added.identifier))


async def test_a_screenshot_comes_back_as_a_png(session: Session, server: str) -> None:
    """§12 calls this the gap that will hurt first; the protocol
    side of it is one call."""
    await goto(session, f"{server}/input.html")
    shot = await session.send(page.CaptureScreenshot(format="png"))
    decoded = base64.b64decode(shot.data)
    assert decoded[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(decoded) > 500


async def test_events_are_routed_to_the_session_that_caused_them(
    connection: Connection, session: Session, server: str
) -> None:
    other = await connection.send(target.CreateTarget(url="about:blank"))
    second = await connection.attach(other.target_id)
    try:
        await second.send(page.Enable())
        with session.queue(page.LoadEventFired) as first_loads, second.queue(page.LoadEventFired) as second_loads:
            await goto(second, f"{server}/")
            await asyncio.sleep(0.1)
        assert second_loads.qsize() >= 1
        assert first_loads.qsize() == 0
    finally:
        await connection.send(target.CloseTarget(target_id=other.target_id))


async def test_contexts_do_not_share_cookies(connection: Connection, session: Session, server: str) -> None:
    """Real browser contexts, not tabs that happen to share nothing."""
    await session.send(network.Enable())
    await session.send(network.SetCookie(name="isolated", value="1", url=server))

    context = await connection.send(target.CreateBrowserContext())
    created = await connection.send(
        target.CreateTarget(url="about:blank", browser_context_id=context.browser_context_id)
    )
    stranger = await connection.attach(created.target_id)
    try:
        await stranger.send(page.Enable())
        await stranger.send(runtime.Enable())
        await goto(stranger, f"{server}/")
        assert "isolated" not in str(await evaluate(stranger, "document.cookie"))
    finally:
        await connection.send(target.CloseTarget(target_id=created.target_id))
        await connection.send(target.DisposeBrowserContext(browser_context_id=context.browser_context_id))


async def test_send_raw_reaches_a_domain_the_subset_does_not_cover(session: Session) -> None:
    result = await session.send_raw("Runtime.evaluate", {"expression": "40 + 2", "returnByValue": True})
    assert result["result"]["value"] == 42


@pytest.mark.asyncio(loop_scope="function")
async def test_closing_the_pipe_is_what_shuts_chrome_down(tmp_path) -> None:
    """No signal involved: EOF on fd 3 *is* the shutdown signal, and Chrome acts
    on it in about a tenth of a second. This is why a killed test run leaves no
    orphaned browser behind, which a debugging port does."""
    async with chrome(str(tmp_path / "profile")) as live:
        assert (await live.send(browser.GetVersion())).product
        pid = live.transport.pid
        started = time.monotonic()
        await live.close()
        elapsed = time.monotonic() - started
    assert live.transport.pid == -1
    assert elapsed < 5.0, f"Chrome took {elapsed:.2f}s to exit after the pipe closed"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
