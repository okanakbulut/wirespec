"""Call/reply matching and event routing, with the pipe replaced by a loopback."""

import asyncio

import msgspec
import pytest
import pytest_asyncio

from wirespec.cdp import browser, page, runtime, target
from wirespec.connection import Connection
from wirespec.errors import CDPError, ConnectionClosedError
from wirespec.transport import PipeTransport


class Loopback(PipeTransport):
    """A transport that records what was written and lets a test feed replies."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []
        #: How many messages each ``write_all`` carried.
        self.batches: list[int] = []

    def write(self, payload: bytes) -> None:
        self.sent.append(msgspec.json.decode(payload))

    def write_all(self, payloads) -> None:
        #: Recorded one message at a time so a test can reply to each, but the
        #: batching is what is under test elsewhere: the real transport hands
        #: the whole list to one ``writelines``.
        self.batches.append(len(payloads))
        for payload in payloads:
            self.write(payload)

    @property
    def write_paused(self) -> bool:
        return False

    async def close(self) -> None:
        self._closed.set()

    def deliver(self, frame: dict) -> None:
        self.on_message(msgspec.json.encode(frame))

    def reply(self, result: dict | None = None, *, to: int = -1) -> None:
        call_id = self.sent[to]["id"]
        self.deliver({"id": call_id, "result": result if result is not None else {}})

    def fail(self, code: int, message: str, data: str | None = None, *, to: int = -1) -> None:
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self.deliver({"id": self.sent[to]["id"], "error": error})


@pytest.fixture
def loopback() -> Loopback:
    return Loopback()


# loop_scope="function" so the Connection binds to the same loop the test runs
# on; the project default is a session-scoped loop, which is what the real
# browser fixtures want and exactly what these must not have.
@pytest_asyncio.fixture(loop_scope="function")
async def connection(loopback: Loopback) -> Connection:
    return Connection(loopback)


async def test_a_command_goes_out_as_the_protocol_expects(connection: Connection, loopback: Loopback) -> None:
    call = asyncio.ensure_future(connection.send(page.Navigate(url="http://x/"), session_id="S"))
    await asyncio.sleep(0)
    assert loopback.sent == [{"id": 1, "method": "Page.navigate", "params": {"url": "http://x/"}, "sessionId": "S"}]
    loopback.reply({"frameId": "F", "loaderId": "L"})
    result = await call
    assert result.frame_id == "F"
    assert result.error_text is None


async def test_a_command_with_no_parameters_sends_no_params_key(connection: Connection, loopback: Loopback) -> None:
    call = asyncio.ensure_future(connection.send(browser.GetVersion()))
    await asyncio.sleep(0)
    assert loopback.sent == [{"id": 1, "method": "Browser.getVersion"}]
    loopback.reply(
        {"protocolVersion": "1.3", "product": "Chrome/150", "revision": "r", "userAgent": "ua", "jsVersion": "12"}
    )
    assert (await call).product == "Chrome/150"


async def test_a_void_command_answers_none_without_decoding(connection: Connection, loopback: Loopback) -> None:
    call = asyncio.ensure_future(connection.send(page.Enable()))
    await asyncio.sleep(0)
    loopback.reply()
    assert await call is None


async def test_replies_are_matched_by_id_not_by_order(connection: Connection, loopback: Loopback) -> None:
    first = asyncio.ensure_future(connection.send(browser.GetVersion()))
    second = asyncio.ensure_future(connection.send(target.CreateBrowserContext()))
    await asyncio.sleep(0)
    assert [message["id"] for message in loopback.sent] == [1, 2]
    loopback.reply({"browserContextId": "B"}, to=1)
    loopback.reply(
        {"protocolVersion": "1.3", "product": "Chrome/150", "revision": "r", "userAgent": "ua", "jsVersion": "12"},
        to=0,
    )
    assert (await second).browser_context_id == "B"
    assert (await first).product == "Chrome/150"


async def test_an_error_reply_raises_naming_the_method(connection: Connection, loopback: Loopback) -> None:
    call = asyncio.ensure_future(connection.send(page.Navigate(url="http://x/")))
    await asyncio.sleep(0)
    loopback.fail(-32000, "Cannot navigate to invalid URL", "the detail Chrome attached")
    with pytest.raises(CDPError) as raised:
        await call
    assert raised.value.method == "Page.navigate"
    assert raised.value.code == -32000
    assert "the detail Chrome attached" in str(raised.value)


async def test_send_raw_carries_anything_the_subset_does_not(connection: Connection, loopback: Loopback) -> None:
    """The escape hatch from §6.2, so a missing struct is never a
    reason to fork the library."""
    call = asyncio.ensure_future(connection.send_raw("Accessibility.queryAXTree", {"objectId": "O"}))
    await asyncio.sleep(0)
    assert loopback.sent[-1] == {
        "id": 1,
        "method": "Accessibility.queryAXTree",
        "params": {"objectId": "O"},
    }
    loopback.reply({"nodes": [{"nodeId": "1"}]})
    assert await call == {"nodes": [{"nodeId": "1"}]}


async def test_a_pending_call_fails_when_the_pipe_goes(connection: Connection, loopback: Loopback) -> None:
    call = asyncio.ensure_future(connection.send(browser.GetVersion()))
    await asyncio.sleep(0)
    loopback.on_close(None)
    with pytest.raises(ConnectionClosedError):
        await call
    assert connection.closed


async def test_sending_after_close_fails_immediately(connection: Connection, loopback: Loopback) -> None:
    loopback.on_close(None)
    with pytest.raises(ConnectionClosedError):
        await connection.send(browser.GetVersion())


async def test_events_reach_their_subscribers(connection: Connection, loopback: Loopback) -> None:
    seen: list[page.LoadEventFired] = []
    connection.on(page.LoadEventFired, seen.append)
    loopback.deliver({"method": "Page.loadEventFired", "params": {"timestamp": 7.5}, "sessionId": "S"})
    assert [event.timestamp for event in seen] == [7.5]


async def test_a_session_scoped_subscription_ignores_other_sessions(connection: Connection, loopback: Loopback) -> None:
    mine: list[page.LoadEventFired] = []
    everyones: list[page.LoadEventFired] = []
    connection.on(page.LoadEventFired, mine.append, session_id="MINE")
    connection.on(page.LoadEventFired, everyones.append)
    loopback.deliver({"method": "Page.loadEventFired", "params": {"timestamp": 1.0}, "sessionId": "MINE"})
    loopback.deliver({"method": "Page.loadEventFired", "params": {"timestamp": 2.0}, "sessionId": "THEIRS"})
    assert [event.timestamp for event in mine] == [1.0]
    assert [event.timestamp for event in everyones] == [1.0, 2.0]


async def test_unsubscribing_stops_delivery(connection: Connection, loopback: Loopback) -> None:
    seen: list[page.LoadEventFired] = []
    unsubscribe = connection.on(page.LoadEventFired, seen.append)
    loopback.deliver({"method": "Page.loadEventFired", "params": {"timestamp": 1.0}})
    unsubscribe()
    loopback.deliver({"method": "Page.loadEventFired", "params": {"timestamp": 2.0}})
    assert len(seen) == 1


async def test_a_handler_may_unsubscribe_itself_mid_dispatch(connection: Connection, loopback: Loopback) -> None:
    seen: list[float] = []

    def once(event: page.LoadEventFired) -> None:
        seen.append(event.timestamp)
        unsubscribe()

    unsubscribe = connection.on(page.LoadEventFired, once)
    also: list[float] = []
    connection.on(page.LoadEventFired, lambda event: also.append(event.timestamp))
    loopback.deliver({"method": "Page.loadEventFired", "params": {"timestamp": 1.0}})
    loopback.deliver({"method": "Page.loadEventFired", "params": {"timestamp": 2.0}})
    assert seen == [1.0]
    assert also == [1.0, 2.0], "unsubscribing mid-dispatch must not skip the handlers after it"


async def test_a_parameterless_event_is_built_without_touching_the_wire(
    connection: Connection, loopback: Loopback
) -> None:
    seen: list[runtime.ExecutionContextsCleared] = []
    connection.on(runtime.ExecutionContextsCleared, seen.append)
    loopback.deliver({"method": "Runtime.executionContextsCleared"})
    assert len(seen) == 1


async def test_an_event_nobody_wants_is_never_decoded(connection: Connection, loopback: Loopback) -> None:
    """The read path's headline property. Params that could not possibly decode
    go unnoticed while nobody is subscribed, and are reported the moment someone
    is -- which is the only way to observe, from outside, that the parse was
    skipped."""
    reported: list[str] = []
    connection._report = lambda exc, message: reported.append(message)  # type: ignore[method-assign]

    undecodable = {"method": "Page.loadEventFired", "params": {"timestamp": "not a number"}}
    loopback.deliver(undecodable)
    assert reported == []

    connection.on(page.LoadEventFired, lambda event: None)
    loopback.deliver(undecodable)
    assert reported == ["could not decode Page.loadEventFired"]


async def test_a_raising_handler_does_not_stop_the_others(connection: Connection, loopback: Loopback) -> None:
    reported: list[str] = []
    connection._report = lambda exc, message: reported.append(message)  # type: ignore[method-assign]
    seen: list[float] = []

    def explode(event: page.LoadEventFired) -> None:
        raise RuntimeError("boom")

    connection.on(page.LoadEventFired, explode)
    connection.on(page.LoadEventFired, lambda event: seen.append(event.timestamp))
    loopback.deliver({"method": "Page.loadEventFired", "params": {"timestamp": 1.0}})
    assert seen == [1.0]
    assert reported == ["handler for Page.loadEventFired raised"]


async def test_queue_collects_everything_raised_inside_the_block(connection: Connection, loopback: Loopback) -> None:
    with connection.queue(page.LoadEventFired) as loads:
        loopback.deliver({"method": "Page.loadEventFired", "params": {"timestamp": 1.0}})
        loopback.deliver({"method": "Page.loadEventFired", "params": {"timestamp": 2.0}})
        assert loads.qsize() == 2
    loopback.deliver({"method": "Page.loadEventFired", "params": {"timestamp": 3.0}})
    assert loads.qsize() == 2


async def test_expect_catches_an_event_raised_by_the_first_line_of_the_block(
    connection: Connection, loopback: Loopback
) -> None:
    """The subscription has to be live before the block runs, or the fast case --
    an event that arrives before the first await -- is the one that gets lost."""
    async with connection.expect(page.LoadEventFired) as loaded:
        loopback.deliver({"method": "Page.loadEventFired", "params": {"timestamp": 9.0}})
    assert loaded.result().timestamp == 9.0


async def test_expect_applies_its_predicate(connection: Connection, loopback: Loopback) -> None:
    async with connection.expect(page.LoadEventFired, lambda event: event.timestamp > 5) as loaded:
        loopback.deliver({"method": "Page.loadEventFired", "params": {"timestamp": 1.0}})
        loopback.deliver({"method": "Page.loadEventFired", "params": {"timestamp": 6.0}})
    assert loaded.result().timestamp == 6.0


async def test_expect_times_out_in_seconds(connection: Connection, loopback: Loopback) -> None:
    with pytest.raises(TimeoutError):
        async with connection.expect(page.LoadEventFired, timeout=0.01):
            pass


async def test_expect_does_not_swallow_a_failure_in_its_block(connection: Connection, loopback: Loopback) -> None:
    with pytest.raises(ZeroDivisionError):
        async with connection.expect(page.LoadEventFired, timeout=30.0):
            raise ZeroDivisionError("what the block under test did")


async def test_a_session_addresses_its_target(connection: Connection, loopback: Loopback) -> None:
    session = connection.session("S")
    call = asyncio.ensure_future(session.send(page.Enable()))
    await asyncio.sleep(0)
    assert loopback.sent[-1] == {"id": 1, "method": "Page.enable", "sessionId": "S"}
    loopback.reply()
    await call


async def test_attach_returns_a_session_for_the_target(connection: Connection, loopback: Loopback) -> None:
    call = asyncio.ensure_future(connection.attach("T"))
    await asyncio.sleep(0)
    assert loopback.sent[-1]["params"] == {"targetId": "T", "flatten": True}
    loopback.reply({"sessionId": "S"})
    session = await call
    assert session.id == "S"


# -- pipeline ----------------------------------------------------------------


async def test_a_pipeline_sends_every_command_in_one_batch(connection: Connection, loopback: Loopback) -> None:
    """§8.29. The point of the primitive is that the batch reaches
    the transport as a batch -- one ``writelines``, and no Task per call."""
    sending = asyncio.ensure_future(
        connection.pipeline([browser.GetVersion(), browser.GetVersion(), browser.GetVersion()])
    )
    await asyncio.sleep(0)
    assert loopback.batches == [3]
    assert len(loopback.sent) == 3
    assert [message["method"] for message in loopback.sent] == ["Browser.getVersion"] * 3
    for index in range(3):
        loopback.reply(_version(f"Chrome/{index}"), to=index)
    assert [result.product for result in await sending] == ["Chrome/0", "Chrome/1", "Chrome/2"]


async def test_a_pipeline_answers_in_the_order_it_was_given(connection: Connection, loopback: Loopback) -> None:
    """Replies are matched by id, so Chrome is free to answer out of order and
    the caller still gets its own list back in its own order."""
    sending = asyncio.ensure_future(connection.pipeline([browser.GetVersion(), browser.GetVersion()]))
    await asyncio.sleep(0)
    loopback.reply(_version("second"), to=1)
    loopback.reply(_version("first"), to=0)
    assert [result.product for result in await sending] == ["first", "second"]


async def test_a_pipeline_carries_the_session_id_on_every_message(connection: Connection, loopback: Loopback) -> None:
    sending = asyncio.ensure_future(connection.pipeline([browser.GetVersion(), browser.GetVersion()], session_id="S1"))
    await asyncio.sleep(0)
    assert [message["sessionId"] for message in loopback.sent] == ["S1", "S1"]
    for index in range(2):
        loopback.reply(_version("x"), to=index)
    await sending


async def test_a_failure_in_a_pipeline_raises_naming_the_method(connection: Connection, loopback: Loopback) -> None:
    sending = asyncio.ensure_future(connection.pipeline([browser.GetVersion(), browser.GetVersion()]))
    await asyncio.sleep(0)
    loopback.reply(_version("fine"), to=0)
    loopback.fail(-32000, "no such node", to=1)
    with pytest.raises(CDPError) as raised:
        await sending
    assert "Browser.getVersion" in str(raised.value)


async def test_a_pipeline_can_return_its_failures_instead(connection: Connection, loopback: Loopback) -> None:
    """Which is what a reader over a whole match set needs: one element
    re-rendering mid-read must not take the other answers down with it."""
    sending = asyncio.ensure_future(
        connection.pipeline([browser.GetVersion(), browser.GetVersion()], return_exceptions=True)
    )
    await asyncio.sleep(0)
    loopback.fail(-32000, "no such node", to=0)
    loopback.reply(_version("fine"), to=1)
    first, second = await sending
    assert isinstance(first, CDPError)
    assert second.product == "fine"


async def test_a_pipeline_leaves_nothing_pending_when_it_fails(connection: Connection, loopback: Loopback) -> None:
    """Every entry is dropped however the batch ends. A reply addressed to a
    future nobody will await keeps the entry alive for the life of the
    connection, which is a leak per failed query."""
    sending = asyncio.ensure_future(connection.pipeline([browser.GetVersion() for _ in range(4)]))
    await asyncio.sleep(0)
    assert len(connection._pending) == 4
    loopback.fail(-32000, "gone", to=0)
    with pytest.raises(CDPError):
        await sending
    assert connection._pending == {}


async def test_an_empty_pipeline_sends_nothing(connection: Connection, loopback: Loopback) -> None:
    assert await connection.pipeline([]) == []
    assert loopback.sent == []


async def test_a_pipeline_after_close_fails_immediately(connection: Connection, loopback: Loopback) -> None:
    await connection.close()
    connection._disconnected(None)
    with pytest.raises(ConnectionClosedError):
        await connection.pipeline([browser.GetVersion()])


async def test_a_pipeline_of_void_commands_answers_none(connection: Connection, loopback: Loopback) -> None:
    sending = asyncio.ensure_future(connection.pipeline([page.Enable(), page.Enable()]))
    await asyncio.sleep(0)
    for index in range(2):
        loopback.reply(to=index)
    assert await sending == [None, None]


def _version(product: str) -> dict:
    return {
        "protocolVersion": "1.3",
        "product": product,
        "revision": "1",
        "userAgent": "test",
        "jsVersion": "1",
    }
