"""The clock reconciliation, without a browser.

§16.5 calls this the step to test hardest, and it is right: a
frame 200 ms out of place is not obviously wrong to look at, and it sends
someone hunting the wrong request. Every number below is one this machine
actually produced, inside the same 30 ms — see the table in
``wirespec/recorder.py``.
"""

import pytest

from wirespec.recorder import Clocks, Entry, Frame, Message, Timeline

#: All three from one measured run, within 30 ms of each other.
FRAME_EPOCH_SECONDS = 1787954943.521093
REQUEST_MONOTONIC = 1450933.489874
REQUEST_WALL = 1787954943.495
CONSOLE_EPOCH_MILLIS = 1787954943509.067


def test_it_knows_nothing_until_something_teaches_it() -> None:
    """Deliberately not zero. An offset of zero is a *valid* offset -- it is
    what a machine booted in 1970 would have -- so "not learned yet" has to be
    a different answer from "learned, and it is nothing"."""
    clocks = Clocks()
    assert not clocks.known
    assert clocks.epoch(REQUEST_MONOTONIC) is None


def test_request_will_be_sent_is_what_teaches_it() -> None:
    """The only event carrying both clocks, which is why the recorder keeps a
    standing subscription to it (§16.2)."""
    clocks = Clocks()
    clocks.learn(REQUEST_MONOTONIC, REQUEST_WALL)
    assert clocks.known
    assert clocks.epoch(REQUEST_MONOTONIC) == pytest.approx(REQUEST_WALL)


def test_a_monotonic_timestamp_lands_beside_the_frame_it_belongs_with() -> None:
    """The whole point. Converted, the response is 26 ms before the frame; not
    converted, it is 337 million seconds before it -- ten and a half years."""
    clocks = Clocks()
    clocks.learn(REQUEST_MONOTONIC, REQUEST_WALL)
    response = clocks.epoch(1450933.491711)
    assert response is not None
    assert 0.0 < FRAME_EPOCH_SECONDS - response < 0.100
    assert FRAME_EPOCH_SECONDS - 1450933.491711 > 300_000_000


def test_the_console_clock_is_milliseconds() -> None:
    """The third clock, and the one §16.2 did not name. ``Runtime`` timestamps
    are epoch **milliseconds**; treated as seconds a console message lands in
    the year 58,000 and the artefact's axis is a single spike at the end."""
    assert Clocks.from_millis(CONSOLE_EPOCH_MILLIS) == pytest.approx(FRAME_EPOCH_SECONDS, abs=0.050)


def test_the_first_offset_is_the_one_that_is_kept() -> None:
    """A second lesson does not move an axis that events have already been
    placed on. Measured, two requests 14 ms apart agreed to six decimal places,
    so this is about a wall clock being *stepped* mid-recording, not about
    jitter."""
    clocks = Clocks()
    clocks.learn(REQUEST_MONOTONIC, REQUEST_WALL)
    clocks.learn(REQUEST_MONOTONIC + 10.0, REQUEST_WALL + 3610.0)
    assert clocks.epoch(REQUEST_MONOTONIC) == pytest.approx(REQUEST_WALL)


def entry(clocks: Clocks, monotonic: float, *, url: str = "http://x/y") -> Entry:
    made = Entry(clocks, "r1", url, "GET", "Document", monotonic)
    made._responded = monotonic + 0.010
    made._finished = monotonic + 0.020
    return made


def test_the_axis_starts_at_the_earliest_thing_on_it_whatever_kind_it_is() -> None:
    """Frames, requests and console lines are three different clocks and any of
    them can be first. An origin taken from the frames alone puts a request
    that preceded the first paint at a negative offset, and the waterfall draws
    it off the left edge."""
    clocks = Clocks()
    clocks.learn(REQUEST_MONOTONIC, REQUEST_WALL)
    timeline = Timeline(
        frames=[Frame("", FRAME_EPOCH_SECONDS, 800, 600, 0)],
        traffic=[entry(clocks, REQUEST_MONOTONIC)],
        messages=[Message(Clocks.from_millis(CONSOLE_EPOCH_MILLIS), "log", "hello")],
        thinned=0,
    )
    assert timeline.start == pytest.approx(REQUEST_WALL)
    assert timeline.at(FRAME_EPOCH_SECONDS) == pytest.approx(FRAME_EPOCH_SECONDS - REQUEST_WALL)
    assert timeline.at(REQUEST_WALL) == 0.0


def test_the_axis_ends_at_the_latest_thing_on_it() -> None:
    clocks = Clocks()
    clocks.learn(REQUEST_MONOTONIC, REQUEST_WALL)
    timeline = Timeline(
        frames=[Frame("", REQUEST_WALL + 1.0, 800, 600, 0)],
        traffic=[entry(clocks, REQUEST_MONOTONIC + 4.0)],
        messages=[],
        thinned=0,
    )
    # The axis begins at the frame, the earliest thing on it, and ends where
    # the request *finished* -- not where it started, which is the off-by-one
    # that draws every response as though it were instant.
    assert timeline.start == pytest.approx(REQUEST_WALL + 1.0)
    assert timeline.span == pytest.approx(3.020)


def test_an_empty_recording_still_has_a_span() -> None:
    """The writer divides by the span to place things. A recording with one
    frame in it, or none, would otherwise produce a page of NaN -- which reads
    as a bug in the driver rather than as a test that did nothing."""
    assert Timeline(frames=[], traffic=[], messages=[], thinned=0).span > 0
    single = Timeline(frames=[Frame("", FRAME_EPOCH_SECONDS, 800, 600, 0)], traffic=[], messages=[], thinned=0)
    assert single.span > 0


def test_a_request_the_clocks_never_learned_about_is_counted_not_dropped() -> None:
    """Only reachable if a recording saw a ``loadingFinished`` and never a
    single ``requestWillBeSent``, which is what a recorder started mid-page
    would see. Silently dropping it would make the waterfall quietly wrong;
    ``unplaced`` is what the artefact prints instead (§1, goal 4)."""
    timeline = Timeline(frames=[], traffic=[entry(Clocks(), 1450933.0)], messages=[], thinned=0)
    assert timeline.traffic == []
    assert timeline.unplaced == 1
