"""Fixtures for the live suite, and the ledger that proves it is complete.

The suite's claim is that every command and event in the subset has been sent
to, or received from, a real Chrome. Grepping the tests for struct names would
only prove they were *mentioned*. So the connection is instrumented instead:
a command counts when it goes out, and an event counts when it is dispatched
to a subscriber. ``test_zz_protocol_coverage`` reads the ledger at the end.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from typing import Any

import msgspec
import pytest
import pytest_asyncio

from tests.live.server import serve
from tests.support import chrome
from wirespec.cdp import COMMANDS, EVENTS, Command, Event, runtime, target
from wirespec.cdp import page as page_domain
from wirespec.connection import Connection, Session


class ProtocolLog:
    """Which protocol members this run actually exercised, and how they looked.

    ``wire_keys`` is what Chrome actually put in an event's ``params``, kept so
    the suite can check the subset's field names against the protocol rather
    than against its own assumptions. It exists because ``rename="camel"``
    turns ``document_url`` into ``documentUrl`` while CDP spells it
    ``documentURL`` -- and a mismatch there is invisible: the decode fails on
    the read path, which reports rather than raises, so the event simply never
    arrives.

    ``decode_failures`` catches the same class of bug from the other side.
    """

    def __init__(self) -> None:
        self.commands: set[str] = set()
        self.events: set[str] = set()
        self.wire_keys: dict[str, set[str]] = {}
        self.decode_failures: list[str] = []

    def __repr__(self) -> str:
        return f"<ProtocolLog {len(self.commands)} commands, {len(self.events)} events>"


def _instrument(connection: Connection, log: ProtocolLog) -> None:
    """Wrap the connection's public surface so it records as it works.

    Instance attributes, not subclassing: ``Session`` and ``Connection.attach``
    both route through ``self.connection.send``, so one wrapper here catches
    every call site including the ones inside wirespec itself.
    """
    send, send_raw, on = connection.send, connection.send_raw, connection.on

    async def recording_send[R](command: Command[R], *, session_id: str | None = None) -> R:
        log.commands.add(type(command).__method__)
        return await send(command, session_id=session_id)

    async def recording_send_raw(
        method: str, params: Mapping[str, Any] | None = None, *, session_id: str | None = None
    ) -> Any:
        log.commands.add(method)
        return await send_raw(method, params, session_id=session_id)

    def recording_on[E: Event](
        event_type: type[E], handler: Callable[[E], object], *, session_id: str | None = None
    ) -> Callable[[], None]:
        method = event_type.__method__

        def record(event: E) -> object:
            # Recorded on arrival, not on subscription: subscribing to an event
            # Chrome never sends must not count as having exercised it.
            log.events.add(method)
            return handler(event)

        return on(event_type, record, session_id=session_id)

    connection.send = recording_send  # type: ignore[method-assign]
    connection.send_raw = recording_send_raw  # type: ignore[method-assign]
    connection.on = recording_on  # type: ignore[method-assign]
    _watch_the_wire(connection, log)


def _watch_the_wire(connection: Connection, log: ProtocolLog) -> None:
    """Record the raw parameter names of every event frame that goes past.

    Sits in front of the connection's own reader rather than replacing it, so
    what the tests see is unchanged. Only frames for events the subset declares
    are parsed; everything else is passed straight through.
    """
    transport = connection.transport
    inner = transport.on_message
    decode = msgspec.json.Decoder(dict).decode

    def spy(payload):
        try:
            frame = decode(payload)
        except msgspec.DecodeError:
            return inner(payload)
        method = frame.get("method")
        if isinstance(method, str) and method in EVENTS:
            params = frame.get("params")
            if isinstance(params, dict):
                log.wire_keys.setdefault(method, set()).update(params)
        return inner(payload)

    transport.on_message = spy


@pytest.fixture(scope="session")
def protocol_log(request: pytest.FixtureRequest) -> ProtocolLog:
    # Stashed on the config so pytest_terminal_summary, which gets no
    # fixtures, can read the same ledger the tests filled in.
    log = ProtocolLog()
    request.config._wirespec_protocol_log = log  # type: ignore[attr-defined]
    return log


@pytest.fixture(scope="session")
def site() -> Iterator[str]:
    """The base URL of the scenario site. Separate from the root ``server``
    fixture: this one serves real files off disk and has the hostile routes."""
    yield from serve()


@pytest.fixture(scope="session", autouse=True)
def _instrumented(connection: Connection, protocol_log: ProtocolLog) -> None:
    """Autouse so no live test can forget to be counted."""
    _instrument(connection, protocol_log)


@pytest_asyncio.fixture(loop_scope="session")
async def live(connection: Connection, site: str) -> AsyncIterator[Session]:
    """A page of its own, in a context of its own, thrown away afterwards.

    Deliberately not the root ``session`` fixture: the live tests enable and
    disable domains, install bindings and override device metrics, and none of
    that may leak into the next test.
    """
    context = await connection.send(target.CreateBrowserContext())
    created = await connection.send(
        target.CreateTarget(url="about:blank", browser_context_id=context.browser_context_id)
    )
    attached = await connection.attach(created.target_id)
    await attached.send(page_domain.Enable())
    await attached.send(runtime.Enable())
    try:
        yield attached
    finally:
        await connection.send(target.CloseTarget(target_id=created.target_id))
        await connection.send(target.DisposeBrowserContext(browser_context_id=context.browser_context_id))


#: Filled at collection so the coverage test can tell "nothing exercised this"
#: from "you ran one file". Without it, ``pytest tests/live/test_page.py``
#: would report 78 missing members and bury the failure you were chasing.
_COLLECTED: set[str] = set()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        path = getattr(item, "path", None)
        if path is not None and path.parent.name == "live":
            _COLLECTED.add(path.name)


@pytest.fixture(scope="session")
def collected_live_modules() -> set[str]:
    return _COLLECTED


def pytest_terminal_summary(terminalreporter, exitstatus: int, config: pytest.Config) -> None:
    """Print what the run exercised, and what it did not.

    Always, not only on failure: the number is the point of the suite, and a
    coverage figure you have to ask for is a coverage figure nobody reads.
    """
    log = getattr(config, "_wirespec_protocol_log", None)
    if log is None or not _COLLECTED:
        return
    missing_commands = sorted(set(COMMANDS) - log.commands)
    missing_events = sorted(set(EVENTS) - log.events)
    covered = len(log.commands & set(COMMANDS)) + len(log.events & set(EVENTS))
    total = len(COMMANDS) + len(EVENTS)
    terminalreporter.write_sep("=", "CDP subset exercised against Chrome")
    terminalreporter.write_line(f"{covered}/{total} members  ({len(COMMANDS)} commands, {len(EVENTS)} events)")
    for label, missing in (("commands", missing_commands), ("events", missing_events)):
        if missing:
            terminalreporter.write_line(f"  {len(missing)} {label} not exercised:")
            for method in missing:
                terminalreporter.write_line(f"    {method}")


@pytest.fixture(scope="session")
def throwaway_chrome(protocol_log: ProtocolLog, tmp_path_factory: pytest.TempPathFactory):
    """A Chrome of its own, for the tests that end by destroying one.

    ``Browser.close`` and a killed renderer both take the browser or the tab
    down with them, which the session-scoped Chrome cannot survive. The
    connection is instrumented like the shared one, so what these tests
    exercise still counts towards coverage.

    Yields the connection and its profile directory: the crash test needs the
    directory to tell its own renderers apart from every other Chrome on the
    machine.
    """

    @contextlib.asynccontextmanager
    async def factory(name: str, *, extra: tuple[str, ...] = ()):
        profile = tmp_path_factory.mktemp(f"chrome-{name}")
        async with chrome(str(profile), extra=extra) as live_connection:
            _instrument(live_connection, protocol_log)
            yield live_connection, str(profile)

    return factory


@pytest_asyncio.fixture(loop_scope="session", scope="session", autouse=True)
async def _catch_decode_failures(protocol_log: ProtocolLog):
    """Turn a swallowed decode failure into a visible one.

    ``Connection`` reports an undecodable frame to the loop's exception handler
    instead of raising, because the read path must not die on one bad message.
    That is right for production and dangerous for a test suite: a struct whose
    field names do not match the protocol produces no error, no event, and a
    test that fails somewhere else entirely.
    """
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(active_loop, context):
        message = str(context.get("message", ""))
        if "decode" in message:
            protocol_log.decode_failures.append(f"{message}: {context.get('exception')!r}")
        if previous is not None:
            previous(active_loop, context)
        else:
            active_loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
    try:
        yield
    finally:
        loop.set_exception_handler(previous)


@pytest.fixture(autouse=True)
def _no_decode_failures(protocol_log: ProtocolLog):
    """Fail the test that caused a decode failure, rather than the run."""
    already = len(protocol_log.decode_failures)
    yield
    new = protocol_log.decode_failures[already:]
    assert not new, "Chrome sent a frame the subset could not decode:\n  " + "\n  ".join(new)
