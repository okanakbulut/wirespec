"""A real recording, written out and opened in a real browser.

The unit suite (``tests/test_artefact.py``) pins the document's content. This
one answers the question that matters at the moment somebody needs the file:
**does it open, and does it work when it does?** It is also the only honest way
to check "self-contained" -- a missing stylesheet is invisible in a string and
obvious to a browser watching the network.
"""

import pytest

from wirespec.artefact import write
from wirespec.browser import BrowserContext
from wirespec.expect import expect
from wirespec.network import Request
from wirespec.page import Page
from wirespec.recorder import Recorder, Timeline

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def record(page: Page) -> Recorder:
    """A recording with something in it of every kind.

    It ends on the animating page for one reason: the screencast is
    damage-driven, so a static page gives **one** frame (§16.1) and
    a filmstrip of one frame cannot be scrubbed, seeked or told apart from a
    broken one. Anything asserting about the strip needs a strip.
    """
    recorder = Recorder(page)
    await recorder.start()
    try:
        await page.goto("/network.html")
        await page.evaluate("() => window.__fetch('/api')")
        await page.evaluate("() => console.warn('careful', 42)")
        await page.goto("/animating.html")
        await recorder.wait_for(
            lambda seen: len(seen.frames) >= 5 and len(seen.traffic) >= 2 and bool(seen.messages),
            timeout=15.0,
        )
    finally:
        await recorder.stop()
    return recorder


async def test_the_written_file_opens_and_asks_the_network_for_nothing(
    page: Page, context: BrowserContext, tmp_path
) -> None:
    """Opened from ``file://`` with no server behind it, which is how it will be
    opened: out of a CI artefact bundle, on a laptop, offline. Anything the
    document reached for would show up here as a request that is not the
    document itself -- and would otherwise show up as a page that renders
    differently depending on where it was opened, silently."""
    recorder = await record(page)
    path = write(Timeline.of(recorder), tmp_path / "run" / "failure.html", title="a failing spec")

    viewer = await context.new_page()
    asked: list[Request] = []
    await viewer.on("request", asked.append)
    await viewer.goto(path.as_uri())

    await expect(viewer.locator("h1")).to_have_text("a failing spec")
    # `data:` counts as a request to Chrome -- setting the image's src to an
    # embedded frame raises `requestWillBeSent` like any other load -- and it is
    # precisely the thing that never leaves the machine. Everything else has to
    # be the document itself.
    fetched = [request.url for request in asked if not request.url.startswith("data:")]
    assert fetched == [path.as_uri()]


async def test_the_artefact_shows_the_screen_and_the_traffic(page: Page, context: BrowserContext, tmp_path) -> None:
    """What a developer sees: a frame, and the requests that produced it."""
    recorder = await record(page)
    path = write(Timeline.of(recorder), tmp_path / "failure.html", title="a failing spec")

    viewer = await context.new_page()
    await viewer.goto(path.as_uri())

    # The frame is inline, so the image is showing before anything is fetched.
    assert (await viewer.locator("#shot").get_attribute("src") or "").startswith("data:image/jpeg;base64,")
    await expect(viewer.locator(".row .url").filter(has_text="/network.html")).to_be_visible()
    await expect(viewer.locator(".row .url").filter(has_text="/api")).to_be_visible()
    await expect(viewer.locator(".row.log .text")).to_contain_text("careful 42")


async def test_clicking_a_request_seeks_the_filmstrip_to_it(page: Page, context: BrowserContext, tmp_path) -> None:
    """The one interaction the artefact has, and the reason it beats a video:
    seeking is exact, to the frame that request belongs with, rather than to
    wherever a scrub bar lands."""
    recorder = await record(page)
    timeline = Timeline.of(recorder)
    path = write(timeline, tmp_path / "failure.html", title="a failing spec")

    viewer = await context.new_page()
    await viewer.goto(path.as_uri())
    # It opens on the last frame, which is the end state -- the one a plain
    # screenshot would have given, and the wrong one.
    await expect(viewer.locator("#clock")).to_have_text(f"{timeline.at(timeline.frames[-1].at):.3f}s")

    document = next(entry for entry in timeline.traffic if entry.url.endswith("/network.html"))
    assert document.started is not None
    began = timeline.at(document.started)
    # The *nearest* frame to when that request began. Not the first frame and
    # not one at the request's own time: there is no frame at the instant a
    # request begins, and the recording starts before the navigation does, so
    # the strip already has frames of the page that was there before.
    nearest = min(timeline.frames, key=lambda frame: abs(timeline.at(frame.at) - began))
    assert nearest is not timeline.frames[-1], "the seek would be indistinguishable from not seeking"

    await viewer.locator("#network .row").filter(has_text="/network.html").first.click()
    await expect(viewer.locator("#clock")).to_have_text(f"{timeline.at(nearest.at):.3f}s")
