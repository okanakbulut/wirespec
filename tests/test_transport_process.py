"""Spawning, backpressure, and the ways a browser goes away."""

import asyncio
import os
import signal

import msgspec
import pytest

from tests.support import chrome, find_chrome
from wirespec.cdp import runtime, target
from wirespec.connection import Connection
from wirespec.errors import ConnectionClosedError, LaunchError
from wirespec.transport import PipeTransport

pytestmark = pytest.mark.asyncio(loop_scope="function")

needs_chrome = pytest.mark.skipif(find_chrome() is None, reason="no Chrome on this machine")


async def test_a_missing_binary_fails_saying_which(tmp_path) -> None:
    with pytest.raises(LaunchError) as raised:
        await PipeTransport.launch([str(tmp_path / "no-such-chrome")])
    assert "no-such-chrome" in str(raised.value)


async def test_what_the_child_said_on_stderr_is_kept(tmp_path) -> None:
    """Chrome blocks forever on a stderr pipe nobody drains, so it goes to a
    file instead -- which also means a browser that refuses to start leaves its
    reason behind rather than nothing at all."""
    log = tmp_path / "stderr.log"
    transport = await PipeTransport.launch(
        ["/bin/sh", "-c", "echo 'the reason it would not start' >&2; exec sleep 5"],
        stderr_path=str(log),
    )
    try:
        for _ in range(100):
            if transport.stderr_tail():
                break
            await asyncio.sleep(0.01)
        assert transport.stderr_tail() == "the reason it would not start"
    finally:
        await transport.close()


async def test_closing_twice_is_harmless(tmp_path) -> None:
    transport = await PipeTransport.launch(["/bin/sh", "-c", "exec sleep 5"])
    await transport.close()
    await transport.close()
    assert transport.closed


@needs_chrome
async def test_a_large_write_waits_for_the_pipe(tmp_path) -> None:
    """The backpressure path is not decoration: a single 1 MiB command already
    fills the pipe, and without the drain the send queue would just grow in our
    own memory instead."""
    async with chrome(str(tmp_path / "profile")) as connection:
        created = await connection.send(target.CreateTarget(url="about:blank"))
        session = await connection.attach(created.target_id)
        await session.send(runtime.Enable())
        handle = await session.send(runtime.Evaluate(expression="globalThis"))
        assert handle.result.object_id

        command = runtime.CallFunctionOn(
            function_declaration="function (s) { return s.length; }",
            object_id=handle.result.object_id,
            arguments=[runtime.CallArgument(value="y" * (4 << 20))],
            return_by_value=True,
        )
        connection.transport.write(msgspec.json.encode(command))
        assert connection.transport.write_paused, "a 4 MiB write should not have fitted in the pipe"
        await connection.transport.drain()
        assert not connection.transport.write_paused


@needs_chrome
async def test_a_browser_that_dies_fails_the_calls_waiting_on_it(tmp_path) -> None:
    """A driver that hangs here is worse than one that fails: the test run stops
    telling you anything at all."""
    async with chrome(str(tmp_path / "profile")) as connection:
        created = await connection.send(target.CreateTarget(url="about:blank"))
        session = await connection.attach(created.target_id)
        await session.send(runtime.Enable())

        # A promise that never settles, so the call is unambiguously in flight.
        in_flight = asyncio.create_task(
            session.send(runtime.Evaluate(expression="new Promise(() => {})", await_promise=True))
        )
        await asyncio.sleep(0.1)
        assert not in_flight.done()

        os.kill(connection.transport.pid, signal.SIGKILL)
        with pytest.raises(ConnectionClosedError):
            await asyncio.wait_for(in_flight, timeout=10)

        assert connection.closed
        with pytest.raises(ConnectionClosedError):
            await connection.send(runtime.Enable(), session_id=session.id)


@needs_chrome
async def test_a_connection_can_be_closed_while_calls_are_pending(tmp_path) -> None:
    connection = None
    async with chrome(str(tmp_path / "profile")) as live:
        connection = live
        created = await live.send(target.CreateTarget(url="about:blank"))
        session = await live.attach(created.target_id)
        await session.send(runtime.Enable())
        in_flight = asyncio.create_task(
            session.send(runtime.Evaluate(expression="new Promise(() => {})", await_promise=True))
        )
        await asyncio.sleep(0.1)
    with pytest.raises(ConnectionClosedError):
        await asyncio.wait_for(in_flight, timeout=10)
    assert isinstance(connection, Connection)
    assert connection.closed
