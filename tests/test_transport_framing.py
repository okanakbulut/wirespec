"""Splitting Chrome's fd 4 into messages, which is all the framing there is."""

import time

import msgspec
import pytest

from wirespec.transport import _ReadProtocol


class Collector:
    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.closed_with: BaseException | None = None
        self.was_closed = False

    def message(self, payload: memoryview | bytes) -> None:
        self.messages.append(bytes(payload))

    def close(self, exc: BaseException | None) -> None:
        self.was_closed = True
        self.closed_with = exc


def feed(*chunks: bytes) -> list[bytes]:
    collector = Collector()
    protocol = _ReadProtocol(collector.message, collector.close)
    for chunk in chunks:
        protocol.data_received(chunk)
    return collector.messages


def test_one_message_per_chunk() -> None:
    assert feed(b"a\x00", b"bb\x00") == [b"a", b"bb"]


def test_several_messages_in_one_chunk() -> None:
    assert feed(b"one\x00two\x00three\x00") == [b"one", b"two", b"three"]


def test_a_message_split_across_chunks() -> None:
    assert feed(b'{"id":', b'1,"result', b'":{}}\x00') == [b'{"id":1,"result":{}}']


def test_a_separator_landing_exactly_on_a_boundary() -> None:
    assert feed(b"one", b"\x00", b"two\x00") == [b"one", b"two"]


def test_a_trailing_partial_message_waits_for_the_rest() -> None:
    collector = Collector()
    protocol = _ReadProtocol(collector.message, collector.close)
    protocol.data_received(b"done\x00partial")
    assert collector.messages == [b"done"]
    protocol.data_received(b"-rest\x00")
    assert collector.messages == [b"done", b"partial-rest"]


def test_several_complete_messages_after_a_carried_over_prefix() -> None:
    """The carry-over path has its own scanning loop, so it needs its own case."""
    assert feed(b"head", b"-tail\x00two\x00three\x00") == [b"head-tail", b"two", b"three"]


def test_empty_messages_do_not_break_the_scan() -> None:
    assert feed(b"\x00a\x00\x00b\x00") == [b"", b"a", b"", b"b"]


def test_a_large_message_arriving_in_many_chunks() -> None:
    payload = msgspec.json.encode({"id": 1, "result": {"data": "x" * (4 << 20)}})
    collector = Collector()
    protocol = _ReadProtocol(collector.message, collector.close)
    for start in range(0, len(payload), 65536):
        protocol.data_received(payload[start : start + 65536])
    protocol.data_received(b"\x00")
    assert collector.messages == [payload]


def test_scanning_does_not_restart_from_the_beginning() -> None:
    """A regression guard with teeth: rescanning the whole buffer on every chunk
    is quadratic, and CDP payloads are big enough for that to matter. 50k chunks
    of a message that never completes would be ~80 GB of scanning if it did."""
    collector = Collector()
    protocol = _ReadProtocol(collector.message, collector.close)
    protocol.data_received(b"x" * 64)  # puts the framer on the carry-over path
    started = time.monotonic()
    for _ in range(50_000):
        protocol.data_received(b"y" * 64)
    elapsed = time.monotonic() - started
    protocol.data_received(b"\x00")
    assert len(collector.messages[0]) == 64 + 50_000 * 64
    assert elapsed < 5.0, f"framing 3 MB in 64-byte chunks took {elapsed:.1f}s"


@pytest.mark.parametrize("split", [False, True], ids=["one chunk", "two chunks"])
def test_raw_views_stay_valid_after_more_data_arrives(split: bool) -> None:
    """The invariant the whole read path rests on.

    Connection resolves a call's future with a ``msgspec.Raw`` and decodes it in
    the *awaiting task*, which runs long after ``data_received`` returned. That
    is only safe if the slices the framer hands out are views over immutable
    bytes -- so both paths through the framer are checked, the direct one and
    the carry-over one that has a mutable buffer underneath.
    """

    class Frame(msgspec.Struct):
        id: int
        result: msgspec.Raw = msgspec.Raw()

    decode = msgspec.json.Decoder(Frame).decode
    collector = Collector()
    protocol = _ReadProtocol(collector.message, collector.close)

    first = b'{"id":1,"result":{"userAgent":"Chrome/150"}}\x00'
    if split:
        protocol.data_received(first[:10])
        protocol.data_received(first[10:])
    else:
        protocol.data_received(first)

    held = decode(collector.messages[0]).result

    # Everything that could disturb the buffer underneath that Raw.
    for _ in range(200):
        protocol.data_received(b'{"id":2,"result":{"noise":"' + b"z" * 4096 + b'"}}\x00')
    protocol.data_received(b"a partial message that forces the carry-over buffer to grow")

    assert msgspec.json.decode(held) == {"userAgent": "Chrome/150"}


def test_connection_lost_is_reported() -> None:
    collector = Collector()
    protocol = _ReadProtocol(collector.message, collector.close)
    protocol.connection_lost(None)
    assert collector.was_closed
