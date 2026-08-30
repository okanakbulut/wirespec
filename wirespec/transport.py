"""The CDP wire: NUL-delimited JSON over a pipe to a Chrome we spawned.

Chrome started with ``--remote-debugging-pipe`` reads CDP from fd 3 and writes
it to fd 4, one JSON message per NUL byte. There is no HTTP, no open port and
no framing beyond that separator, which is why wirespec has no websocket
dependency — see §3.2.

**uvloop.** Everything here is written against the event loop *interface* and
never against asyncio's own implementation of it, so it runs unchanged on
uvloop. What is absent matters as much as what is here: no
``asyncio.create_subprocess_exec`` (that would drag in a child watcher, which
uvloop implements differently), no ``add_reader``, no signal handlers. Chrome
is spawned with ``os.posix_spawn`` and reaped by hand, so the loop never needs
to know a subprocess exists.
"""

import asyncio
import os
import signal
from collections.abc import Callable, Mapping, Sequence

from wirespec.errors import LaunchError

__all__ = ["PipeTransport"]

_NUL = b"\x00"

#: Chrome's own shutdown after the pipe closes was measured at ~0.1 s.
_EXIT_GRACE = 5.0
_KILL_GRACE = 2.0


class _ReadProtocol(asyncio.Protocol):
    """Splits Chrome's fd 4 into whole messages.

    Slices handed to ``on_message`` are always views over *immutable* bytes, so
    a ``msgspec.Raw`` cut from one stays valid after this callback returns.
    That is what lets Connection decode a result in the task awaiting it rather
    than here, on the read path, where the decode would block every other
    session's traffic behind it.
    """

    def __init__(
        self, on_message: Callable[[memoryview | bytes], None], on_close: Callable[[BaseException | None], None]
    ) -> None:
        self._on_message = on_message
        self._on_close = on_close
        self._buf = bytearray()

    def data_received(self, data: bytes) -> None:
        buf = self._buf
        on_message = self._on_message
        if buf:
            # A message straddled a chunk boundary. Scan only the bytes that
            # are new -- rescanning from zero on every chunk turns one large
            # message into quadratic work, and CDP payloads do get large.
            scan = len(buf)
            buf += data
            view = memoryview(buf)
            start = 0
            i = buf.find(0, scan)
            while i >= 0:
                on_message(bytes(view[start:i]))
                start = i + 1
                i = buf.find(0, start)
            view.release()
            if start:
                del buf[:start]
            return

        view = memoryview(data)
        start = 0
        i = data.find(0)
        while i >= 0:
            on_message(view[start:i])
            start = i + 1
            i = data.find(0, start)
        if start < len(data):
            buf += view[start:]

    def connection_lost(self, exc: BaseException | None) -> None:
        self._on_close(exc)


class _WriteProtocol(asyncio.BaseProtocol):
    """Carries write backpressure. A screenshot or a full document's HTML can
    outrun the pipe, and an unbounded send queue would just move the problem
    into our own memory."""

    def __init__(self) -> None:
        self._unpaused = asyncio.Event()
        self._unpaused.set()

    def is_set(self) -> bool:
        return self._unpaused.is_set()

    def pause_writing(self) -> None:
        self._unpaused.clear()

    def resume_writing(self) -> None:
        self._unpaused.set()

    async def drain(self) -> None:
        if not self._unpaused.is_set():
            await self._unpaused.wait()


class PipeTransport:
    """A live pipe to a Chrome process. Owns the child and both pipe ends."""

    def __init__(self) -> None:
        self.pid: int = -1
        self.on_message: Callable[[memoryview | bytes], None] = _ignore
        self.on_close: Callable[[BaseException | None], None] = _ignore
        self._reader: asyncio.ReadTransport | None = None
        self._writer: asyncio.WriteTransport | None = None
        self._write_proto: _WriteProtocol | None = None
        self._closed = asyncio.Event()
        self._stderr_path: str | None = None

    @classmethod
    async def launch(
        cls,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stderr_path: str = os.devnull,
    ) -> PipeTransport:
        """Spawn ``argv`` with the CDP pipe on fds 3 and 4 and connect to it.

        ``argv[0]`` is the executable. ``stderr_path`` is opened by the child,
        not inherited: Chrome blocks forever on a stderr pipe nobody drains, so
        it must go somewhere that always accepts writes. Point it at a file to
        keep the diagnostics a failed launch leaves behind.
        """
        self = cls()
        self._stderr_path = stderr_path
        loop = asyncio.get_running_loop()

        # r3/w4 are the child's ends; w3/r4 are ours.
        r3, w3 = os.pipe()
        r4, w4 = os.pipe()
        # dup2 onto an fd that already holds the same descriptor is a no-op and
        # would leave FD_CLOEXEC set. Clear it here instead of relying on the
        # libc spawn helper to special-case it.
        os.set_inheritable(r3, True)
        os.set_inheritable(w4, True)

        actions: list[tuple[object, ...]] = [
            (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0o644),
            (os.POSIX_SPAWN_OPEN, 2, stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644),
            (os.POSIX_SPAWN_DUP2, r3, 3),
            (os.POSIX_SPAWN_DUP2, w4, 4),
        ]
        # os.pipe hands out ascending fds and 0-2 are taken, so w4 is never 3 in
        # practice; ordering the dup2s anyway costs nothing and removes the need
        # to reason about it again.
        if w4 == 3:
            actions[2], actions[3] = actions[3], actions[2]

        try:
            # ASYNC221 is about blocking the loop; posix_spawn is exactly the
            # call that does not -- the child is on its own after the C helper
            # runs the file actions, with no Python between fork and exec.
            self.pid = os.posix_spawn(  # noqa: ASYNC221
                argv[0],
                list(argv),
                dict(env if env is not None else os.environ),
                file_actions=actions,
                setpgroup=0,  # Ctrl-C on the terminal must not reach Chrome: it
            )  # exits on pipe EOF, and that path is orderly.
        except OSError as exc:
            for fd in (r3, w3, r4, w4):
                _close(fd)
            raise LaunchError(f"could not start {argv[0]}: {exc}") from exc
        finally:
            _close(r3)
            _close(w4)

        try:
            read_proto = _ReadProtocol(self._deliver, self._on_pipe_closed)
            self._reader, _ = await loop.connect_read_pipe(lambda: read_proto, os.fdopen(r4, "rb", 0))
            r4 = -1
            self._write_proto = _WriteProtocol()
            write_proto = self._write_proto
            self._writer, _ = await loop.connect_write_pipe(lambda: write_proto, os.fdopen(w3, "wb", 0))
            w3 = -1
        except BaseException:
            _close(r4)
            _close(w3)
            await self.close()
            raise
        return self

    def write(self, payload: bytes) -> None:
        """Queue one already-encoded message. Appends the separator."""
        writer = self._writer
        if writer is None or writer.is_closing():
            raise ConnectionResetError("the CDP pipe is closed")
        # writelines, not write(payload + _NUL): asyncio joins the pieces the
        # same way we would, and uvloop passes them to a scatter-gather write
        # without joining at all.
        writer.writelines((payload, _NUL))

    def write_all(self, payloads: Sequence[bytes]) -> None:
        """Queue a whole batch of already-encoded messages, separators and all.

        One ``writelines`` rather than one per message. asyncio writes straight
        to the pipe when its buffer is empty, so a batch sent a message at a
        time is a syscall a message; sent this way it is one, and under uvloop a
        single scatter-gather write over the lot.

        Only worth using where the batch is built in one place -- the resolver's
        fan-outs, which are the only calls here that ever number in the
        thousands (§8.29).
        """
        writer = self._writer
        if writer is None or writer.is_closing():
            raise ConnectionResetError("the CDP pipe is closed")
        pieces: list[bytes] = []
        for payload in payloads:
            pieces.append(payload)
            pieces.append(_NUL)
        writer.writelines(pieces)

    @property
    def write_paused(self) -> bool:
        """True when the pipe is behind. Checking this before awaiting drain()
        keeps the common case free of a coroutine nobody needed."""
        return self._write_proto is not None and not self._write_proto.is_set()

    async def drain(self) -> None:
        """Wait until the write buffer has room again."""
        if self._write_proto is not None:
            await self._write_proto.drain()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        """Close the pipe and wait for Chrome to exit.

        Closing our end of fd 3 is the shutdown signal: Chrome sees EOF and
        exits on its own, ~0.1 s later, with no signal involved. Signals are
        only a fallback for a browser that has stopped reading.
        """
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        self._closed.set()
        if self.pid > 0:
            await self._reap()
            self.pid = -1

    async def _reap(self) -> None:
        if not await self._wait_exit(_EXIT_GRACE):
            _signal(self.pid, signal.SIGTERM)
            if not await self._wait_exit(_KILL_GRACE):
                _signal(self.pid, signal.SIGKILL)
                await self._wait_exit(_KILL_GRACE)

    async def _wait_exit(self, timeout: float) -> bool:
        """Poll for the child, backing off. waitpid is done by hand because the
        process was not started through the loop, and neither asyncio's child
        watcher nor uvloop's knows about it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        interval = 0.001
        while True:
            try:
                pid, _ = os.waitpid(self.pid, os.WNOHANG)  # noqa: ASYNC222  WNOHANG never blocks
            except ChildProcessError:
                return True
            if pid:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(interval)
            interval = min(interval * 2, 0.05)

    def stderr_tail(self, limit: int = 4096) -> str:
        """The last of whatever Chrome said before it died. Empty unless the
        caller pointed ``stderr_path`` at a real file."""
        path = self._stderr_path
        if not path or path == os.devnull:
            return ""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - limit))
                return fh.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""

    def _deliver(self, payload: memoryview | bytes) -> None:
        self.on_message(payload)

    def _on_pipe_closed(self, exc: BaseException | None) -> None:
        self._closed.set()
        self.on_close(exc)


def _ignore(*_: object) -> None:
    pass


def _close(fd: int) -> None:
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError, PermissionError:
        pass
