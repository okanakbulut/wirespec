"""The recorder: frames, traffic and console, all on one clock.

Through the public API, like every test in this directory. What is asserted
here is what a developer opening the artefact would notice was missing --
frames that stop after three, a waterfall with no request in it, a console
message on a clock a thousand times off (§16).
"""

import base64

import pytest

from wirespec.errors import WirespecTimeoutError
from wirespec.expect import expect
from wirespec.page import Page
from wirespec.pytest_plugin import Artefacts
from wirespec.recorder import Recorder, Timeline

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_it_records_frames_while_the_page_animates(page: Page) -> None:
    """More than a handful, which is the whole proof that the ack is working.

    Chrome hands out a frame and waits to be told it arrived. Measured: with
    the ack, 179 frames in three seconds; without it, **three** and then
    silence for ever (§16.2). Ten is far below the first and far
    above the second, so this fails the moment the ack stops going out and does
    not fail because a machine was slow.
    """
    recorder = Recorder(page)
    await recorder.start()
    try:
        await page.goto("/animating.html")
        await recorder.wait_for(lambda seen: len(seen.frames) >= 10, timeout=15.0)
    finally:
        await recorder.stop()

    assert len(recorder.frames) >= 10
    for frame in recorder.frames:
        assert base64.b64decode(frame.data)[:3] == b"\xff\xd8\xff"
        assert frame.width > 0 and frame.height > 0
        # Epoch seconds. A monotonic timestamp would be about 1.4 million.
        assert frame.at > 1_577_836_800
    assert recorder.frames == sorted(recorder.frames, key=lambda frame: frame.at)


async def test_it_records_the_traffic_on_the_frames_clock(page: Page) -> None:
    """The waterfall, and the reconciliation that makes it readable.

    Network timestamps are monotonic with an arbitrary origin and frames are
    epoch seconds; ten and a half years apart on this machine. What is asserted
    is the thing that proves the conversion happened: every request's time sits
    inside the span the frames cover, not somewhere in 1970 or 2026.
    """
    recorder = Recorder(page)
    await recorder.start()
    try:
        await page.goto("/network.html")
        await page.evaluate("() => window.__fetch('/api')")
        await recorder.wait_for(lambda seen: len(seen.frames) >= 1, timeout=15.0)
    finally:
        await recorder.stop()

    traffic = {entry.url.rsplit("/", 1)[-1]: entry for entry in recorder.traffic}
    assert "network.html" in traffic
    assert "api" in traffic

    document = traffic["network.html"]
    assert document.method == "GET"
    assert document.status == 200
    assert document.kind == "Document"
    assert document.started is not None and document.finished is not None
    assert document.finished >= document.started

    # The proof that the clocks were reconciled: converted, the document
    # request began within a second of the first frame; unconverted it would be
    # 337 million seconds away.
    first = recorder.frames[0].at
    assert document.started is not None
    assert abs(document.started - first) < 5.0


async def test_a_request_that_failed_says_so_rather_than_vanishing(page: Page) -> None:
    """A request with no response is the one worth looking at, so it has to be
    in the waterfall with its error rather than dropped for having no status
    (§1, goal 4)."""
    recorder = Recorder(page)
    await recorder.start()
    try:
        await page.goto("/network.html")
        await page.evaluate("() => window.__fetch('http://127.0.0.1:1/nope').catch(() => {})")
        await recorder.wait_for(lambda seen: any(entry.failed for entry in seen.traffic), timeout=15.0)
    finally:
        await recorder.stop()

    failed = [entry for entry in recorder.traffic if entry.failed is not None]
    assert failed, [entry.url for entry in recorder.traffic]
    assert failed[0].url == "http://127.0.0.1:1/nope"
    assert failed[0].status is None
    assert "net::" in (failed[0].failed or "")


async def test_it_records_the_console_and_page_errors(page: Page) -> None:
    """On the same axis, which needs the millisecond clock divided by a
    thousand (§16.2)."""
    recorder = Recorder(page)
    await recorder.start()
    try:
        await page.goto("/network.html")
        await page.evaluate("() => console.warn('careful', 42)")
        await page.evaluate("() => setTimeout(() => { throw new Error('from the page'); }, 0)")
        await recorder.wait_for(lambda seen: len(seen.messages) >= 2, timeout=15.0)
    finally:
        await recorder.stop()

    levels = {message.level for message in recorder.messages}
    assert "warning" in levels
    assert "error" in levels
    text = " | ".join(message.text for message in recorder.messages)
    assert "careful" in text and "42" in text
    assert "from the page" in text
    for message in recorder.messages:
        # Seconds, not milliseconds. Undivided these are around 1.8e12.
        assert 1_577_836_800 < message.at < 4_102_444_800


async def test_a_burst_thins_the_filmstrip_rather_than_forgetting_the_start(page: Page) -> None:
    """The cap §16.3 asks for, and the shape of it that matters.

    A plain ring buffer would satisfy "bounded" and lose the test. Three
    seconds of animation is 179 frames at 60 fps, so an animation at the *end*
    of a run would evict everything before it -- including the click that
    caused the failure, which is the one frame anybody opens the artefact to
    see. So the buffer **thins**: over cap, it halves the density of the older
    half and keeps the span. The filmstrip gets coarser towards the beginning
    and never gets shorter.
    """
    recorder = Recorder(page, max_frames=20)
    await recorder.start()
    try:
        await page.goto("/index.html")
        await recorder.wait_for(lambda seen: len(seen.frames) >= 1, timeout=15.0)
        opening = recorder.frames[0]
        await page.goto("/animating.html")
        await recorder.wait_for(lambda seen: seen.thinned >= 40, timeout=15.0)
    finally:
        await recorder.stop()

    assert len(recorder.frames) <= 20
    # The opening frame is still the opening frame, by identity.
    assert recorder.frames[0] is opening
    # And the strip still spans the whole recording rather than its tail.
    assert recorder.frames[-1].at - recorder.frames[0].at > 0.5
    # Nothing silent: the artefact has to be able to say what it threw away.
    assert recorder.thinned >= 40


async def test_the_byte_cap_bounds_a_page_whose_frames_are_large(page: Page) -> None:
    """A length cap alone is not a memory bound. Frames on this fixture measure
    about 8 KB each and on a text page about 3 KB, so twenty frames is anywhere
    between 60 KB and 160 KB -- and a 4K viewport would make it megabytes. Both
    caps apply, and either one thins."""
    recorder = Recorder(page, max_frames=10_000, max_bytes=40 * 1024)
    await recorder.start()
    try:
        await page.goto("/animating.html")
        await recorder.wait_for(lambda seen: seen.thinned >= 20, timeout=15.0)
    finally:
        await recorder.stop()

    assert sum(len(frame.data) for frame in recorder.frames) <= 40 * 1024
    assert len(recorder.frames) < 10_000


async def test_stopping_after_the_page_closed_keeps_what_was_recorded(page: Page) -> None:
    """The ordinary teardown order, not an edge case.

    A context closes its pages before a recorder's fixture unwinds, so ``stop``
    routinely runs against a session that is gone. Raising there would replace
    the test's own failure with a ``PageClosedError`` from teardown -- burying
    the thing the artefact was being kept for.
    """
    recorder = Recorder(page)
    await recorder.start()
    await page.goto("/animating.html")
    await recorder.wait_for(lambda seen: len(seen.frames) >= 3, timeout=15.0)
    captured = len(recorder.frames)

    await page.close()
    await recorder.stop()

    assert len(recorder.frames) == captured


async def test_record_keeps_the_artefact_when_the_block_throws(page: Page, request, tmp_path) -> None:
    """The inline form of the retention policy, against a real page.

    An exception passing through ``record`` counts as a failure on its own, so
    the block does not have to wait for a call report that has not been written
    yet (§16.3).
    """
    artefacts = Artefacts(request.node, "on-failure", tmp_path)
    with pytest.raises(ZeroDivisionError):
        async with artefacts.record(page) as recorder:
            await page.goto("/animating.html")
            await recorder.wait_for(lambda seen: len(seen.frames) >= 2, timeout=15.0)
            raise ZeroDivisionError("what the spec would have raised")

    kept = list(tmp_path.iterdir())
    assert len(kept) == 1
    assert kept[0].suffix == ".html"
    assert "data:image/jpeg;base64," in kept[0].read_text(encoding="utf-8")


async def test_record_keeps_nothing_when_the_block_does_not_throw(page: Page, request, tmp_path) -> None:
    """Dropped at teardown, and nothing touches the disk. This is the case that
    runs thousands of times a day."""
    artefacts = Artefacts(request.node, "on-failure", tmp_path)
    async with artefacts.record(page) as recorder:
        await page.goto("/animating.html")
        await recorder.wait_for(lambda seen: len(seen.frames) >= 2, timeout=15.0)

    assert list(tmp_path.iterdir()) == []


async def test_it_records_the_actions_on_the_same_axis(page: Page) -> None:
    """Which call was in flight, which is the one thing the frames do not say.

    A filmstrip shows a field filling in; it does not show that the fill took
    four seconds waiting for the element to stop moving. The lane is what turns
    "something was slow" into "this was slow" (§16.2).
    """
    recorder = Recorder(page)
    await recorder.start()
    try:
        await page.goto("/actions.html")
        await page.locator("#plain").click()
        await page.locator("#text").fill("hello")
    finally:
        await recorder.stop()

    assert [action.name for action in recorder.actions] == ["goto", "click", "fill"]
    assert recorder.actions[0].target.endswith("/actions.html")
    assert "#plain" in recorder.actions[1].target
    assert all(action.failure is None for action in recorder.actions)
    for action in recorder.actions:
        # The frames' clock, not a monotonic one: a lane on its own axis is a
        # lane nobody can line up against the screen.
        assert action.at > 1_577_836_800
        assert action.until >= action.at


async def test_an_action_that_failed_is_kept_and_says_why(page: Page) -> None:
    """The most useful row in the file, and the one an artefact that only
    recorded successes would be missing: the artefact exists *because* the test
    failed, so the last action is usually the one that raised."""
    recorder = Recorder(page)
    await recorder.start()
    try:
        await page.goto("/actions.html")
        with pytest.raises(WirespecTimeoutError):
            await page.locator("#never-here").click(timeout=0.3)
    finally:
        await recorder.stop()

    failed = recorder.actions[-1]
    assert failed.name == "click"
    assert "#never-here" in failed.target
    assert failed.failure is not None
    assert "WirespecTimeoutError" in failed.failure


async def test_actions_are_recorded_only_while_something_is_watching(page: Page) -> None:
    """The lane costs two clock reads and a tuple per action, and only for a
    page being recorded. A driver that paid for it on every action in a suite
    would be paying for a file almost every run throws away."""
    await page.goto("/actions.html")
    await page.locator("#plain").click()

    recorder = Recorder(page)
    await recorder.start()
    try:
        await page.locator("#plain").click()
    finally:
        await recorder.stop()
    assert len(recorder.actions) == 1

    await page.locator("#plain").click()
    assert len(recorder.actions) == 1


async def test_it_follows_a_tab_the_application_opens(page: Page) -> None:
    """The gap this closes: a recorder attached to one page recorded the click
    that opened a popup and then nothing at all about the popup — which is
    exactly the test somebody opens the artefact for (§16.2)."""
    recorder = Recorder(page)
    await recorder.start()
    try:
        await page.goto("/popup.html")
        async with page.expect_popup() as popup:
            await page.locator("#open").click()
        opened = popup.result()
        await expect(opened.locator("h1")).to_be_visible()
        # **Not** the popup's first request: attaching is several round trips
        # and the tab navigates on its own before they land, so its opening
        # document is already in flight. What is recorded is everything from
        # the attach onward, which is what the reload below makes visible.
        await opened.reload()
        await recorder.wait_for(
            lambda seen: any(frame.tab == 2 for frame in seen.frames) and any(entry.tab == 2 for entry in seen.traffic),
            timeout=15.0,
        )
    finally:
        await recorder.stop()

    assert recorder.pages == [page, opened]
    assert {entry.tab for entry in recorder.traffic} == {1, 2}
    assert {action.tab for action in recorder.actions} == {1, 2}
    assert any(entry.tab == 2 and entry.url.endswith("/index.html") for entry in recorder.traffic)


async def test_a_tab_opened_before_the_recording_started_is_not_followed(page: Page) -> None:
    """The recorder is about the page it was given and what that page opens.
    A context-wide recorder would quietly pick up every other test's tab in a
    suite that shares one."""
    other = await page.context.new_page()
    recorder = Recorder(page)
    await recorder.start()
    try:
        await page.goto("/index.html")
        await other.goto("/list.html")
    finally:
        await recorder.stop()

    assert recorder.pages == [page]
    assert all(entry.tab == 1 for entry in recorder.traffic)
    assert not any("list.html" in entry.url for entry in recorder.traffic)


async def test_the_timeline_names_the_tabs_it_recorded(page: Page) -> None:
    """A row saying "tab 2" is only useful next to something saying what tab 2
    was. Read at the end rather than when the tab opened, because a popup is
    announced at ``about:blank`` and navigates on its own."""
    recorder = Recorder(page)
    await recorder.start()
    try:
        await page.goto("/popup.html")
        async with page.expect_popup() as popup:
            await page.locator("#open").click()
        await expect(popup.result().locator("h1")).to_be_visible()
    finally:
        await recorder.stop()

    timeline = Timeline.of(recorder)
    assert len(timeline.tabs) == 2
    assert timeline.tabs[0].endswith("/popup.html")
    assert timeline.tabs[1].endswith("/index.html")


async def test_starting_again_after_stopping_does_not_invent_a_second_tab(page: Page) -> None:
    """``stop`` leaves the buffer and the tab list alone, because the timeline
    is read off them afterwards. A second ``start`` that appended the same page
    again would number every later row for a tab the legend cannot name."""
    recorder = Recorder(page)
    await recorder.start()
    await page.goto("/index.html")
    await recorder.stop()

    await recorder.start()
    try:
        await page.goto("/list.html")
    finally:
        await recorder.stop()

    assert recorder.pages == [page]
    assert {entry.tab for entry in recorder.traffic} == {1}
    assert len(Timeline.of(recorder).tabs) == 1


# --- request and response detail (§16.6) -----------------------


async def _traffic(page: Page) -> Recorder:
    """Record ``/traffic.html`` through one click, and stop."""
    recorder = Recorder(page)
    await recorder.start()
    try:
        await page.goto("/traffic.html")
        await page.locator("#go").click()
        await recorder.wait_for(
            lambda seen: sum(1 for entry in seen.traffic if entry.url.endswith("dot.png")) > 0,
            timeout=15.0,
        )
        # The image's own `loadingFinished` lands after the request appears.
        await recorder.wait_for(
            lambda seen: all(entry.completed or entry.failed for entry in seen.traffic),
            timeout=15.0,
        )
    finally:
        await recorder.stop()
    return recorder


def _find(recorder: Recorder, ending: str, method: str = "GET"):
    return next(entry for entry in recorder.traffic if entry.url.endswith(ending) and entry.method == method)


async def test_it_records_the_headers_of_both_halves(page: Page) -> None:
    """Free: both arrive on events the recorder is already subscribed to, so
    the headers cost no round trip at all (§16.6)."""
    recorder = await _traffic(page)
    api = _find(recorder, "/api")

    assert api.request_headers.get("Authorization") == "Bearer squirrel"
    lowered = {name.lower(): value for name, value in api.response_headers.items()}
    assert lowered.get("content-type") == "application/json"
    # And the connection detail the pane shows next to them.
    assert api.protocol
    assert api.remote


async def test_it_records_the_payload_a_post_sent(page: Page) -> None:
    """The half of a failing POST that the status code does not explain."""
    recorder = await _traffic(page)
    posted = _find(recorder, "/api", "POST")
    assert posted.post_data == '{"who":"me"}'


async def test_it_reads_the_body_of_a_text_response(page: Page) -> None:
    """Read at ``loadingFinished`` and nowhere else: Chrome holds a body until
    the page navigates away and then answers "No resource with given
    identifier" (§16.6)."""
    recorder = await _traffic(page)
    api = _find(recorder, "/api")
    assert api.body is not None
    assert "the server" in api.body
    assert api.body_note == ""

    document = _find(recorder, "/traffic.html")
    assert document.body is not None
    assert "<title>traffic</title>" in document.body


async def test_a_binary_body_is_skipped_and_says_so(page: Page) -> None:
    """The filter that keeps the artefact openable, and the round trip that is
    never spent. A pane rendering a PNG as text is a screenful of mojibake that
    buries the rows above it."""
    recorder = await _traffic(page)
    dot = _find(recorder, "/dot.png")

    assert dot.body is None
    assert "not captured" in dot.body_note
    # Named, so the reason is the media type rather than a shrug.
    assert dot.mime in dot.body_note
    # And the request is still fully in the waterfall.
    assert dot.status == 200
    assert dot.response_headers


async def test_bodies_can_be_turned_off_entirely(page: Page) -> None:
    """For a suite that wants the waterfall and not the contents. Headers and
    the payload still arrive, because they never cost anything."""
    recorder = Recorder(page, bodies=False)
    await recorder.start()
    try:
        await page.goto("/traffic.html")
        await page.locator("#go").click()
        await recorder.wait_for(lambda seen: len(seen.traffic) >= 3, timeout=15.0)
    finally:
        await recorder.stop()

    assert recorder.traffic
    assert all(entry.body is None for entry in recorder.traffic)
    assert all(entry.body_note == "" for entry in recorder.traffic)
    assert any(entry.request_headers for entry in recorder.traffic)


async def test_a_body_over_the_cap_is_truncated_and_says_by_how_much(page: Page) -> None:
    """§16.3: whatever a cap threw away is printed."""
    recorder = Recorder(page, max_body=64)
    await recorder.start()
    try:
        await page.goto("/traffic.html")
        await recorder.wait_for(
            lambda seen: any(entry.body_note.startswith("truncated") for entry in seen.traffic),
            timeout=15.0,
        )
    finally:
        await recorder.stop()

    document = _find(recorder, "/traffic.html")
    assert document.body is not None
    assert len(document.body) == 64
    assert document.body_note.startswith("truncated: 64 of ")


async def test_the_timeline_carries_the_redaction_choice(page: Page) -> None:
    """The buffer keeps what happened; the file is what travels, so the choice
    belongs to the writer rather than to the capture."""
    recorder = Recorder(page, redact=False)
    await recorder.start()
    try:
        await page.goto("/traffic.html")
        await recorder.wait_for(lambda seen: len(seen.traffic) >= 1, timeout=15.0)
    finally:
        await recorder.stop()

    assert Timeline.of(recorder).redact is False
    assert Timeline.of(Recorder(page)).redact is True
