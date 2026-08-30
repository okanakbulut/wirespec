"""``Runtime`` — the domain every locator, probe and assertion goes through.

If any one domain has to be right, it is this one: wirespec resolves a locator
chain with a single ``callFunctionOn``, so the handle model and the
value/reference distinction are load-bearing rather than incidental.
"""

import pytest

from tests.live.support import drain_until, evaluate, goto, handle_for
from wirespec.cdp import page, runtime, target
from wirespec.connection import Connection, Session
from wirespec.errors import CDPError

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_enable_announces_the_context_that_already_exists(connection: Connection, site: str) -> None:
    """Runtime.enable, and executionContextCreated.

    Enabling is not passive: Chrome replays the contexts that already exist, so
    a driver that enables *after* navigating still learns about the main world
    rather than waiting forever for a context that was created before it looked.
    """
    context = await connection.send(target.CreateBrowserContext())
    created = await connection.send(
        target.CreateTarget(url="about:blank", browser_context_id=context.browser_context_id)
    )
    fresh = await connection.attach(created.target_id)
    try:
        with fresh.queue(runtime.ExecutionContextCreated) as contexts:
            await fresh.send(runtime.Enable())
            announced = await drain_until(contexts, lambda event: True, timeout=10.0)
        assert announced.context.id > 0
        assert announced.context.unique_id
    finally:
        await connection.send(target.CloseTarget(target_id=created.target_id))
        await connection.send(target.DisposeBrowserContext(browser_context_id=context.browser_context_id))


async def test_disable_stops_the_events(connection: Connection, site: str) -> None:
    """Runtime.disable. The events stop; evaluation keeps working, because
    ``Runtime.evaluate`` is a command and not a subscription."""
    context = await connection.send(target.CreateBrowserContext())
    created = await connection.send(
        target.CreateTarget(url="about:blank", browser_context_id=context.browser_context_id)
    )
    fresh = await connection.attach(created.target_id)
    try:
        await fresh.send(page.Enable())
        await fresh.send(runtime.Enable())
        await fresh.send(runtime.Disable())
        with fresh.queue(runtime.ExecutionContextCreated) as contexts:
            await goto(fresh, f"{site}/index.html")
            assert await evaluate(fresh, "1 + 1") == 2
        assert contexts.qsize() == 0, "disable should have stopped the context events"
    finally:
        await connection.send(target.CloseTarget(target_id=created.target_id))
        await connection.send(target.DisposeBrowserContext(browser_context_id=context.browser_context_id))


async def test_evaluate_returns_a_value_when_asked_by_value(live: Session, site: str) -> None:
    """Runtime.evaluate with return_by_value: the answer is serialised and
    crosses the wire."""
    await goto(live, f"{site}/index.html")
    result = await live.send(runtime.Evaluate(expression="({title: document.title, n: 6 * 7})", return_by_value=True))
    assert result.exception_details is None
    assert result.result.value == {"title": "wirespec live suite", "n": 42}


async def test_evaluate_returns_a_handle_when_not(live: Session, site: str) -> None:
    """Runtime.evaluate without return_by_value. The array stays in the page and
    only its identity crosses -- the model the whole design rests on."""
    await goto(live, f"{site}/index.html")
    result = await live.send(runtime.Evaluate(expression="document.querySelectorAll('*')"))
    assert result.result.object_id is not None
    # Chrome reports a NodeList as subtype "array", not "nodelist": the subtype
    # is about how DevTools should display the value, not about its class.
    assert result.result.subtype == "array"
    assert result.result.value is None
    assert result.result.class_name == "NodeList"


async def test_evaluate_awaits_a_promise_when_asked(live: Session) -> None:
    """Runtime.evaluate with await_promise. Without it the result is the
    Promise itself, and every assertion downstream is about the wrong object."""
    # By reference, because return_by_value serialises a pending Promise to an
    # empty object and loses the very thing this test is about.
    unresolved = await live.send(runtime.Evaluate(expression="Promise.resolve('done')"))
    assert unresolved.result.subtype == "promise"

    resolved = await live.send(
        runtime.Evaluate(expression="Promise.resolve('done')", return_by_value=True, await_promise=True)
    )
    assert resolved.result.value == "done"


async def test_evaluate_reports_a_page_side_throw_in_the_result(live: Session) -> None:
    """A page that throws is not a protocol error: the command succeeded and
    the page failed, so it comes back in ``exception_details`` and the caller
    decides what that means."""
    result = await live.send(runtime.Evaluate(expression="throw new TypeError('nope')"))
    assert result.exception_details is not None
    assert result.exception_details.exception is not None
    assert "nope" in (result.exception_details.exception.description or "")
    assert result.exception_details.stack_trace is not None


async def test_evaluate_honours_its_own_timeout(live: Session) -> None:
    """``timeout_ms`` is spelled in milliseconds because CDP spells it that way;
    wirespec counts seconds everywhere else, so the field name says which.

    A timed-out evaluation is a *protocol* error, unlike a page-side throw:
    Chrome terminated the execution rather than the script failing, so it
    arrives as CDPError and not in ``exception_details``. Code that only checks
    ``exception_details`` sails past an infinite loop.
    """
    with pytest.raises(CDPError) as raised:
        await live.send(runtime.Evaluate(expression="while (true) {}", timeout_ms=250.0))
    assert "terminated" in str(raised.value).lower()


async def test_call_function_on_receives_the_handle_as_this(live: Session, site: str) -> None:
    """Runtime.callFunctionOn. The handle arrives as ``this``, not as the first
    argument -- the single most common way to get this call wrong."""
    await goto(live, f"{site}/index.html")
    heading = await handle_for(live, "document.getElementById('heading')")
    result = await live.send(
        runtime.CallFunctionOn(
            function_declaration="function () { return this.textContent; }",
            object_id=heading,
            return_by_value=True,
        )
    )
    assert result.result.value == "wirespec live suite"


async def test_call_function_on_tells_null_apart_from_undefined(live: Session) -> None:
    """The UNSET design, checked where it decides behaviour: a CallArgument with
    no value passes ``undefined``, one holding None passes ``null``. With a
    plain None default, omit_defaults would collapse the second into the first.
    """
    result = await live.send(
        runtime.CallFunctionOn(
            function_declaration="function (a, b) { return [a === null, b === undefined].join(','); }",
            object_id=await handle_for(live, "globalThis"),
            arguments=[runtime.CallArgument(value=None), runtime.CallArgument()],
            return_by_value=True,
        )
    )
    assert result.result.value == "true,true"


async def test_call_function_on_passes_a_handle_as_an_argument(live: Session, site: str) -> None:
    """A CallArgument carrying an object_id, which is how one in-page object is
    handed to a function operating on another without either crossing the wire."""
    await goto(live, f"{site}/index.html")
    heading = await handle_for(live, "document.getElementById('heading')")
    result = await live.send(
        runtime.CallFunctionOn(
            function_declaration="function (node) { return node === document.getElementById('heading'); }",
            object_id=await handle_for(live, "globalThis"),
            arguments=[runtime.CallArgument(object_id=heading)],
            return_by_value=True,
        )
    )
    assert result.result.value is True


async def test_release_object_invalidates_the_handle(live: Session, site: str) -> None:
    """Runtime.releaseObject. Handles are a leak if nobody drops them: the page
    cannot collect a node the debugger still holds."""
    await goto(live, f"{site}/index.html")
    heading = await handle_for(live, "document.getElementById('heading')")
    await live.send(runtime.ReleaseObject(object_id=heading))
    with pytest.raises(CDPError) as raised:
        await live.send(
            runtime.CallFunctionOn(
                function_declaration="function () { return 1; }", object_id=heading, return_by_value=True
            )
        )
    assert "object" in str(raised.value).lower()


async def test_release_object_group_drops_a_whole_batch(live: Session, site: str) -> None:
    """Runtime.releaseObjectGroup. One call retires every handle a spec took,
    which is what makes per-test cleanup affordable."""
    await goto(live, f"{site}/index.html")
    handles = []
    for _ in range(3):
        result = await live.send(runtime.Evaluate(expression="document.createElement('div')", object_group="batch"))
        assert result.result.object_id
        handles.append(result.result.object_id)

    await live.send(runtime.ReleaseObjectGroup(object_group="batch"))
    for handle in handles:
        with pytest.raises(CDPError):
            await live.send(
                runtime.CallFunctionOn(
                    function_declaration="function () { return 1; }", object_id=handle, return_by_value=True
                )
            )


async def test_a_binding_lets_the_page_push_to_python(live: Session, site: str) -> None:
    """Runtime.addBinding / removeBinding, and bindingCalled.

    The page calls a function; Python hears about it. No polling, and no
    round trip from the driver asking "has anything happened yet".
    """
    await live.send(runtime.AddBinding(name="wirespecReport"))
    try:
        await goto(live, f"{site}/binding.html")
        with live.queue(runtime.BindingCalled) as calls:
            assert await evaluate(live, "window.report({ok: true, n: 3})") is True
            called = await drain_until(calls, lambda event: event.name == "wirespecReport", timeout=10.0)
        assert called.payload == '{"ok":true,"n":3}'
        assert called.execution_context_id > 0
    finally:
        await live.send(runtime.RemoveBinding(name="wirespecReport"))

    # Removed means removed: the page's own guard sees the function is gone.
    await goto(live, f"{site}/binding.html")
    assert await evaluate(live, "window.report({ok: true})") is False


async def test_console_calls_arrive_with_their_arguments(live: Session, site: str) -> None:
    """Runtime.consoleAPICalled. The arguments come as RemoteObjects, so a
    driver that assumes strings loses the numbers and the objects."""
    await goto(live, f"{site}/console.html")
    with live.queue(runtime.ConsoleAPICalled) as logged:
        await evaluate(live, "document.getElementById('log').click()")
        call = await drain_until(logged, lambda event: event.type == "log", timeout=10.0)
    assert [argument.value for argument in call.args[:2]] == ["hello from the page", 42]
    assert call.args[2].type == "object"
    assert call.timestamp > 0


async def test_an_uncaught_page_error_is_reported(live: Session, site: str) -> None:
    """Runtime.exceptionThrown. Nothing asked for this: it is the page failing
    on its own, which is exactly the failure a spec would otherwise miss."""
    await goto(live, f"{site}/console.html")
    with live.queue(runtime.ExceptionThrown) as thrown:
        await evaluate(live, "document.getElementById('boom').click()")
        failure = await drain_until(
            thrown,
            lambda event: (
                event.exception_details.exception is not None
                and "uncaught from the page" in (event.exception_details.exception.description or "")
            ),
            timeout=10.0,
        )
    assert failure.exception_details.stack_trace is not None
    assert failure.timestamp > 0


async def test_a_removed_frame_destroys_its_context(live: Session, site: str) -> None:
    """Runtime.executionContextDestroyed. Each iframe is its own world, and
    dropping the frame retires it -- so any handle into that frame is now
    worthless, which is why a driver has to hear about this."""
    await goto(live, f"{site}/frames.html")
    with live.queue(runtime.ExecutionContextDestroyed) as destroyed:
        await evaluate(live, "document.getElementById('drop').click()")
        gone = await drain_until(destroyed, lambda event: True, timeout=10.0)
    assert gone.execution_context_id is not None or gone.execution_context_unique_id is not None


async def test_navigation_clears_every_context(live: Session, site: str) -> None:
    """Runtime.executionContextsCleared. A cross-document navigation retires
    the whole world at once rather than frame by frame."""
    await goto(live, f"{site}/frames.html")
    with live.queue(runtime.ExecutionContextsCleared) as cleared:
        await goto(live, f"{site}/index.html")
        await drain_until(cleared, lambda event: True, timeout=10.0)
