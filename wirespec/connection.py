"""One connection, every page: call/reply matching and event routing.

A single pipe carries the browser and every target on it, using flat sessions
(§3.3). Replies are matched to calls by the ``id`` Chrome echoes
back; events are routed by method name and ``sessionId``.

Two properties of the read path are worth stating, because both are load-bearing:

* **Nothing is decoded that nobody wants.** An incoming frame is parsed into an
  envelope whose ``result`` and ``params`` are ``msgspec.Raw`` -- a byte span,
  not a parsed object. An event with no subscribers costs one envelope parse and
  stops there, which matters because enabling ``Network`` on a real application
  produces a great many of them.
* **Results are decoded in the task that asked for them**, not in the read
  callback. Parsing a large payload on the read path would stall every other
  session's traffic behind it. This is safe because the transport hands out
  slices of immutable bytes, so a ``Raw`` stays valid after the callback returns.
"""

import asyncio
import contextlib
import itertools
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

import msgspec
from msgspec import Raw

from wirespec.cdp import Command, Event
from wirespec.cdp import target as target_domain
from wirespec.cdp.base import CDPStruct, decoder_for
from wirespec.errors import CDPError, ConnectionClosedError
from wirespec.transport import PipeTransport

__all__ = ["Connection", "Session"]

#: How long expect() waits by default. The public API passes its own -- 5 s for
#: assertions, 15 s for actions (§5.1).
DEFAULT_EXPECT_TIMEOUT = 30.0

_EMPTY = Raw()


class _ErrorBody(CDPStruct):
    code: int
    message: str
    data: str | None = None


class _Request(CDPStruct, gc=False):
    """What goes out. ``gc=False`` because it holds nothing that can form a
    cycle and it is allocated once per call."""

    id: int
    method: str
    params: Any = None
    session_id: str | None = None


class _Frame(CDPStruct, gc=False):
    """What comes in -- a reply or an event, told apart by ``id``.

    ``result`` and ``params`` stay as ``Raw`` so the payload is not parsed until
    something is known to want it.
    """

    id: int | None = None
    method: str | None = None
    result: Raw = _EMPTY
    params: Raw = _EMPTY
    error: _ErrorBody | None = None
    session_id: str | None = None


class _Slot:
    """The subscribers for one event method, plus how to decode it.

    ``subscribers`` is a tuple, rebuilt on subscribe and unsubscribe, so
    dispatch iterates it without copying and a handler is free to unsubscribe
    itself mid-dispatch.
    """

    __slots__ = ("decoder", "event_type", "subscribers")

    def __init__(self, event_type: type[Event]) -> None:
        self.event_type = event_type
        self.decoder = decoder_for(event_type)
        self.subscribers: tuple[tuple[str | None, Callable[[Any], object]], ...] = ()

    def build(self, params: Raw) -> Event:
        decoder = self.decoder
        # No decoder means no parameters, so there is nothing in `params` worth
        # parsing -- construct the event and skip the wire entirely.
        if decoder is None:
            return self.event_type()
        return decoder.decode(params)


class Connection:
    """A live CDP connection. One per browser."""

    def __init__(self, transport: PipeTransport) -> None:
        self._transport = transport
        self._loop = asyncio.get_running_loop()
        self._encode = msgspec.json.Encoder().encode
        self._decode_frame = msgspec.json.Decoder(_Frame).decode
        self._next_id = itertools.count(1).__next__
        self._pending: dict[int, asyncio.Future[_Frame]] = {}
        self._slots: dict[str, _Slot] = {}
        #: Method name -> the handlers that want to know an event *happened*,
        #: without knowing what it said. See ``signal``.
        self._signals: dict[str, tuple[tuple[str | None, Callable[[], object]], ...]] = {}
        self._closed: BaseException | None = None
        transport.on_message = self._receive
        transport.on_close = self._disconnected

    @classmethod
    async def launch(
        cls,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stderr_path: str | None = None,
    ) -> Connection:
        """Start Chrome on ``argv`` and connect to its pipe."""
        transport = await PipeTransport.launch(
            argv,
            env=env,
            **({"stderr_path": stderr_path} if stderr_path is not None else {}),
        )
        return cls(transport)

    @property
    def transport(self) -> PipeTransport:
        return self._transport

    @property
    def closed(self) -> bool:
        return self._closed is not None

    async def send[R](self, command: Command[R], *, session_id: str | None = None) -> R:
        """Send one command and wait for its reply.

        Raises :class:`~wirespec.errors.CDPError` if Chrome answers with an
        error, and :class:`~wirespec.errors.ConnectionClosedError` if the pipe
        goes away first.
        """
        if self._closed is not None:
            raise ConnectionClosedError("the CDP connection is closed") from self._closed
        command_type = type(command)
        call_id = self._next_id()
        future: asyncio.Future[_Frame] = self._loop.create_future()
        self._pending[call_id] = future
        request = _Request(
            id=call_id,
            method=command_type.__method__,
            # A command with no fields has no parameters to send; skipping the
            # empty object keeps enable/disable and the input events -- the two
            # highest-volume shapes -- two bytes shorter and one encode step
            # simpler.
            params=command if command_type.__struct_fields__ else None,
            session_id=session_id,
        )
        try:
            self._transport.write(self._encode(request))
            if self._transport.write_paused:
                await self._transport.drain()
            frame = await future
        except BaseException:
            self._pending.pop(call_id, None)
            raise
        if frame.error is not None:
            error = frame.error
            raise CDPError(command_type.__method__, error.code, error.message, error.data)
        decoder = command_type.__decoder__
        if decoder is None:
            decoder = decoder_for(command_type)
            if decoder is None:
                return None  # pyright: ignore[reportReturnType]  R is None here
        return decoder.decode(frame.result)

    async def pipeline[R](
        self,
        commands: Sequence[Command[R]],
        *,
        session_id: str | None = None,
        return_exceptions: bool = False,
    ) -> list[Any]:
        """Send a batch of commands together and collect the replies in order.

        Same traffic as ``asyncio.gather`` over ``send``, and materially less of
        this process's own work. **``gather`` allocates a Task per coroutine**,
        and a Task is a context copy, two ``call_soon``s and a done-callback --
        which is nothing for eight calls and is the dominant cost for the
        resolver's fan-outs, where one query can be well over a thousand
        (§8.29). Measured on a 1440-call batch: 6% off the wall
        clock and **29% off this process's CPU**, which is the number that
        matters when a suite runs four workers on one machine.

        The requests are encoded in one pass and handed to the transport as a
        single ``writelines``; the futures are then awaited directly, with no
        Task between them and the reply. Ordering is the caller's list, not
        completion order.

        ``return_exceptions`` mirrors ``asyncio.gather``: a failed call becomes
        its exception in the returned list rather than raising. Without it the
        first failure raises and the rest of the batch is abandoned -- still
        cleanly, because every pending entry is dropped either way.
        """
        if self._closed is not None:
            raise ConnectionClosedError("the CDP connection is closed") from self._closed
        if not commands:
            return []

        encode = self._encode
        loop = self._loop
        pending = self._pending
        waiting: list[tuple[type[Command[R]], int, asyncio.Future[_Frame]]] = []
        payloads: list[bytes] = []
        for command in commands:
            command_type = type(command)
            call_id = self._next_id()
            future: asyncio.Future[_Frame] = loop.create_future()
            pending[call_id] = future
            waiting.append((command_type, call_id, future))
            payloads.append(
                encode(
                    _Request(
                        id=call_id,
                        method=command_type.__method__,
                        params=command if command_type.__struct_fields__ else None,
                        session_id=session_id,
                    )
                )
            )

        results: list[Any] = []
        try:
            self._transport.write_all(payloads)
            if self._transport.write_paused:
                await self._transport.drain()
            for command_type, _, future in waiting:
                try:
                    frame = await future
                    if frame.error is not None:
                        error = frame.error
                        raise CDPError(command_type.__method__, error.code, error.message, error.data)
                    decoder = command_type.__decoder__
                    if decoder is None:
                        decoder = decoder_for(command_type)
                    results.append(decoder.decode(frame.result) if decoder is not None else None)
                except Exception as exc:
                    if not return_exceptions:
                        raise
                    results.append(exc)
        finally:
            # Whatever went wrong -- a closed pipe, a cancellation, a decode --
            # nothing may be left addressed to a future nobody will await, or
            # the reply keeps the entry alive for the life of the connection.
            for _, call_id, _ in waiting:
                pending.pop(call_id, None)
        return results

    async def send_raw(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> Any:
        """Send a command wirespec has no struct for, and get plain JSON back.

        This is what makes the missing half of the protocol a non-problem: the
        subset covers what the public API needs, and anything else goes through
        here rather than through a fork (§6.2).
        """
        if self._closed is not None:
            raise ConnectionClosedError("the CDP connection is closed") from self._closed
        call_id = self._next_id()
        future: asyncio.Future[_Frame] = self._loop.create_future()
        self._pending[call_id] = future
        try:
            self._transport.write(
                self._encode(_Request(id=call_id, method=method, params=params, session_id=session_id))
            )
            if self._transport.write_paused:
                await self._transport.drain()
            frame = await future
        except BaseException:
            self._pending.pop(call_id, None)
            raise
        if frame.error is not None:
            error = frame.error
            raise CDPError(method, error.code, error.message, error.data)
        return msgspec.json.decode(frame.result) if frame.result else None

    def on[E: Event](
        self,
        event_type: type[E],
        handler: Callable[[E], object],
        *,
        session_id: str | None = None,
    ) -> Callable[[], None]:
        """Subscribe to every ``event_type`` from now on.

        ``session_id=None`` means every session, which is what you want for
        browser-level events and almost never what you want for page-level ones.
        Returns the function that unsubscribes.
        """
        method = event_type.__method__
        if not method:
            raise ValueError(f"{event_type.__name__} is not a CDP event")
        slot = self._slots.get(method)
        if slot is None:
            slot = self._slots[method] = _Slot(event_type)
        entry = (session_id, handler)
        slot.subscribers += (entry,)

        def unsubscribe() -> None:
            slot.subscribers = tuple(item for item in slot.subscribers if item is not entry)

        return unsubscribe

    def signal(
        self,
        methods: Sequence[str],
        handler: Callable[[], object],
        *,
        session_id: str | None = None,
    ) -> Callable[[], None]:
        """Be told that one of ``methods`` arrived, without decoding it.

        The wait loop needs to know that *something* changed and never what
        (§5.1), and a full re-render of a 200-row page produces 400
        events whose only effect is to set one ``asyncio.Event``. Decoding all
        400 to discard them would put the parse on the read path, where it
        blocks every other session's traffic behind it -- so this stops at the
        envelope, which is the same property §3.5 relies on for events nobody is
        listening to.

        Methods are named as strings rather than as structs on purpose: a
        signal subscription does not need the event to be in the protocol
        subset, because it never looks inside it.
        """
        entry = (session_id, handler)
        for method in methods:
            self._signals[method] = (*self._signals.get(method, ()), entry)

        def unsubscribe() -> None:
            for method in methods:
                remaining = tuple(item for item in self._signals.get(method, ()) if item is not entry)
                if remaining:
                    self._signals[method] = remaining
                else:
                    self._signals.pop(method, None)

        return unsubscribe

    @contextlib.contextmanager
    def queue[E: Event](
        self,
        event_type: type[E],
        *,
        session_id: str | None = None,
        maxsize: int = 0,
    ) -> Iterator[asyncio.Queue[E]]:
        """Collect every ``event_type`` raised inside the block.

        The subscription is live for the whole block, so nothing raised between
        entering it and the first await is lost.
        """
        events: asyncio.Queue[E] = asyncio.Queue(maxsize)
        unsubscribe = self.on(event_type, events.put_nowait, session_id=session_id)
        try:
            yield events
        finally:
            unsubscribe()

    @contextlib.asynccontextmanager
    async def expect[E: Event](
        self,
        event_type: type[E],
        predicate: Callable[[E], bool] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = DEFAULT_EXPECT_TIMEOUT,
    ):
        """Wait for the event the block is about to cause.

        The subscription is in place before the block runs, which is the whole
        point: an event raised by the very first line of the block is still
        caught. The event is awaited when the block exits, so ::

            async with connection.expect(page.LoadEventFired, session_id=s) as loaded:
                await session.send(page.Navigate(url=url))
            timestamp = loaded.result().timestamp

        ``timeout`` is in seconds, like every timeout in wirespec.
        """
        future: asyncio.Future[E] = self._loop.create_future()

        def catch(event: E) -> None:
            if future.done():
                return
            try:
                if predicate is None or predicate(event):
                    future.set_result(event)
            except Exception as exc:  # noqa: BLE001 -- a broken predicate must surface at the await, not vanish
                future.set_exception(exc)

        unsubscribe = self.on(event_type, catch, session_id=session_id)
        try:
            yield future
            # Awaited even when it is already done, which is the point: a
            # predicate that raised has set its exception here, and a future
            # nobody awaits swallows it -- the block leaves cleanly, the caller
            # believes the event arrived, and the traceback surfaces later as
            # "exception was never retrieved" from an unrelated place. Awaiting
            # a done future costs nothing and is what makes the comment above
            # `set_exception` true.
            async with asyncio.timeout(timeout):
                await future
        finally:
            unsubscribe()
            if not future.done():
                future.cancel()

    async def attach(self, target_id: str) -> Session:
        """Attach to a target and return the session that addresses it."""
        result = await self.send(target_domain.AttachToTarget(target_id=target_id))
        return Session(self, result.session_id)

    def session(self, session_id: str) -> Session:
        return Session(self, session_id)

    async def close(self) -> None:
        await self._transport.close()

    async def wait_closed(self) -> None:
        await self._transport.wait_closed()

    def _receive(self, payload: memoryview | bytes) -> None:
        """The read path. Runs in the event loop callback, so it must not block
        and must not raise."""
        try:
            frame = self._decode_frame(payload)
        except msgspec.DecodeError as exc:
            self._report(exc, "undecodable CDP frame")
            return

        call_id = frame.id
        if call_id is not None:
            future = self._pending.pop(call_id, None)
            if future is not None and not future.done():
                future.set_result(frame)
            return

        method = frame.method
        if method is None:
            return
        # Signals first, and without decoding: a waiter only needs to know that
        # something happened (§5.1).
        listeners = self._signals.get(method)
        if listeners is not None:
            session_id = frame.session_id
            for wanted, notify in listeners:
                if wanted is not None and wanted != session_id:
                    continue
                try:
                    notify()
                except Exception as exc:  # noqa: BLE001 -- one bad waiter must not take down the read path
                    self._report(exc, f"signal handler for {method} raised")
        slot = self._slots.get(method)
        # Nobody is listening: the envelope is as far as this frame gets.
        if slot is None or not slot.subscribers:
            return
        try:
            event = slot.build(frame.params)
        except msgspec.DecodeError as exc:
            self._report(exc, f"could not decode {method}")
            return
        session_id = frame.session_id
        for wanted, handler in slot.subscribers:
            if wanted is not None and wanted != session_id:
                continue
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 -- one bad handler must not stop the others, or take down the read path
                self._report(exc, f"handler for {method} raised")

    def _disconnected(self, exc: BaseException | None) -> None:
        self._closed = exc or ConnectionClosedError("the CDP pipe closed")
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(ConnectionClosedError("the CDP pipe closed while a call was in flight"))

    def _report(self, exc: BaseException, message: str) -> None:
        self._loop.call_exception_handler({"message": message, "exception": exc, "connection": self})


class Session:
    """A flat session: a target, addressed by ``sessionId`` on every message."""

    __slots__ = ("connection", "id")

    def __init__(self, connection: Connection, session_id: str) -> None:
        self.connection = connection
        self.id = session_id

    def __repr__(self) -> str:
        return f"<Session {self.id}>"

    async def send[R](self, command: Command[R]) -> R:
        return await self.connection.send(command, session_id=self.id)

    async def send_raw(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        return await self.connection.send_raw(method, params, session_id=self.id)

    async def pipeline[R](self, commands: Sequence[Command[R]], *, return_exceptions: bool = False) -> list[Any]:
        return await self.connection.pipeline(commands, session_id=self.id, return_exceptions=return_exceptions)

    def on[E: Event](self, event_type: type[E], handler: Callable[[E], object]) -> Callable[[], None]:
        return self.connection.on(event_type, handler, session_id=self.id)

    def signal(self, methods: Sequence[str], handler: Callable[[], object]) -> Callable[[], None]:
        return self.connection.signal(methods, handler, session_id=self.id)

    def queue[E: Event](self, event_type: type[E], *, maxsize: int = 0):
        return self.connection.queue(event_type, session_id=self.id, maxsize=maxsize)

    def expect[E: Event](
        self,
        event_type: type[E],
        predicate: Callable[[E], bool] | None = None,
        *,
        timeout: float = DEFAULT_EXPECT_TIMEOUT,
    ):
        return self.connection.expect(event_type, predicate, session_id=self.id, timeout=timeout)

    async def detach(self) -> None:
        await self.connection.send(target_domain.DetachFromTarget(session_id=self.id))
