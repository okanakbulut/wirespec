"""The failure artefact as a document, without a browser.

What is checked here is what makes the file worth having: that it opens
anywhere, that everything it needs is inside it, and that nothing it recorded
can break it. The driver suite opens a real one in a real Chrome
(``tests/driver/test_artefact.py``); this pins the content.
"""

from wirespec.artefact import MIN_BAR, render, write
from wirespec.recorder import Action, Clocks, Entry, Frame, Message, Timeline

#: One measured run, in the units each clock actually uses.
START_MONOTONIC = 1450933.489874
START_WALL = 1787954943.495

#: A one-pixel JPEG, so the assertions are about the document and not the codec.
PIXEL = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0"
    "Hyc5PTgyPC4zNDL/wAAFCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAv/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QA"
    "FQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX/9k="
)


def timeline(*, failed: str | None = None, url: str = "http://127.0.0.1:8000/api", thinned: int = 0) -> Timeline:
    clocks = Clocks()
    clocks.learn(START_MONOTONIC, START_WALL)
    document = Entry(clocks, "1", "http://127.0.0.1:8000/index.html", "GET", "Document", START_MONOTONIC)
    document._responded = START_MONOTONIC + 0.012
    document._finished = START_MONOTONIC + 0.018
    document.status = 200
    document.status_text = "OK"
    document.mime = "text/html"
    document.size = 412.0

    api = Entry(clocks, "2", url, "POST", "XHR", START_MONOTONIC + 0.4)
    if failed is None:
        api._responded = START_MONOTONIC + 0.44
        api._finished = START_MONOTONIC + 0.46
        api.status = 500
        api.status_text = "Internal Server Error"
    else:
        api._finished = START_MONOTONIC + 0.44
        api.failed = failed

    return Timeline(
        frames=[
            Frame(PIXEL, START_WALL + 0.05, 1280, 720, 0),
            Frame(PIXEL, START_WALL + 0.50, 1280, 720, 120),
        ],
        traffic=[document, api],
        messages=[
            Message(START_WALL + 0.30, "warning", "careful 42", "http://127.0.0.1:8000/index.html", 7),
            Message(START_WALL + 0.52, "error", "Error: from the page", "http://127.0.0.1:8000/index.html", 9),
        ],
        actions=[
            Action("goto", "http://127.0.0.1:8000/index.html", START_WALL + 0.01, START_WALL + 0.20, None),
            Action("click", "<Locator locator('#save')>", START_WALL + 0.38, START_WALL + 0.39, None),
            Action(
                "expect",
                "expected <Locator locator('#done')> to be visible",
                START_WALL + 0.40,
                START_WALL + 0.55,
                "WirespecTimeoutError: expected to be visible\nlast saw that it was not",
            ),
        ],
        thinned=thinned,
    )


def test_the_document_reaches_for_nothing_outside_itself() -> None:
    """The property the whole design rests on (§16.4). One file,
    an email or a CI artefact away, opening on a machine with no network and no
    viewer installed. A single ``<link rel=stylesheet>`` would turn it into a
    page that renders differently depending on where it is opened, and would do
    so silently."""
    html = render(timeline(), title="a failing spec")
    assert "<link" not in html
    assert "@import" not in html
    assert "<script src" not in html
    assert 'src="http' not in html and "src='http" not in html
    assert "https://" not in html
    # The only thing an image src is ever set from is a data URI built out of
    # the frames already in the file. `test_every_frame_is_in_it` counts those.
    assert "data:image/jpeg;base64," in html


def test_every_frame_is_in_it() -> None:
    html = render(timeline(), title="t")
    assert html.count(PIXEL) == 2


def test_the_clock_label_is_formatted_here_not_by_the_script() -> None:
    """Python's ``%.3f`` and JavaScript's ``toFixed(3)`` do not agree on every
    half-way case, and a clock that disagrees with the timeline by a millisecond
    is a clock nothing can be asserted against. Caught exactly that way: the
    page read ``0.029s`` where the timeline said ``0.030s``. The label is
    rendered once, in Python, and the script prints it."""
    html = render(timeline(), title="t")
    assert '"0.050s"' in html
    assert '"0.500s"' in html
    assert "toFixed" not in html


def test_the_waterfall_names_each_request_and_what_became_of_it() -> None:
    html = render(timeline(), title="t")
    assert "/index.html" in html
    assert "/api" in html
    assert "200" in html
    assert "500" in html
    assert "POST" in html


def test_a_failed_request_shows_its_error_rather_than_a_blank() -> None:
    html = render(timeline(failed="net::ERR_CONNECTION_REFUSED"), title="t")
    assert "net::ERR_CONNECTION_REFUSED" in html


def test_the_console_is_on_the_same_axis() -> None:
    html = render(timeline(), title="t")
    assert "careful 42" in html
    assert "Error: from the page" in html
    assert "warning" in html


def test_it_says_when_it_threw_frames_away() -> None:
    """Nothing fails silently (§1, goal 4). A filmstrip that
    quietly skips is read as "nothing happened there"."""
    assert "thinned" not in render(timeline(), title="t").lower()
    assert "147" in render(timeline(thinned=147), title="t")


def test_it_says_when_a_frame_or_a_request_could_not_be_placed() -> None:
    """The two other ways the recording can be incomplete, and both of them are
    supposed to be impossible. A frame with no timestamp has never been seen --
    the field is optional on the wire and Chrome has always sent it -- and a
    request the clocks never learned about needs a recording that saw a
    ``loadingFinished`` and no ``requestWillBeSent`` at all. Printed anyway,
    because "impossible and silent" is the pair that costs a day."""
    stranded = Entry(Clocks(), "9", "http://x/y", "GET", "XHR", START_MONOTONIC)
    page = render(
        Timeline(frames=[], traffic=[stranded], messages=[], thinned=0, dropped=2),
        title="t",
    )
    assert "2 frames arrived with no timestamp" in page
    assert "1 requests could not be placed" in page


def test_a_url_that_could_close_the_script_tag_does_not() -> None:
    """The frames and the waterfall are handed to the page as JSON inside a
    ``<script>``, and the HTML parser ends that tag at the first ``</script>``
    **inside a string literal as readily as outside one**. A recorded URL is
    attacker-adjacent input in exactly the way a test fixture never is, and the
    failure mode is a blank artefact rather than an error."""
    html = render(timeline(url="http://x/?q=</script><b>oops"), title="t")
    assert "</script><b>oops" not in html
    assert "<\\/script>" in html or "&lt;/script&gt;" in html


def test_a_title_with_markup_in_it_is_escaped() -> None:
    """Test names contain ``<`` more often than you would think --
    ``test_a_<b>_tag`` is a legal pytest id."""
    html = render(timeline(), title="test_x[<b>&]")
    assert "<b>&]" not in html
    assert "&lt;b&gt;" in html


def test_write_puts_exactly_that_on_disk(tmp_path) -> None:
    path = write(timeline(), tmp_path / "run" / "failure.html", title="t")
    assert path.read_text(encoding="utf-8") == render(timeline(), title="t")
    # The directory is made rather than required: a pytest hook writing into
    # a per-run directory should not have to create it first.
    assert path.parent.is_dir()


def test_an_empty_recording_still_produces_a_readable_page() -> None:
    """A test that failed before the first paint records nothing at all, and
    that is exactly when somebody opens the artefact. It has to say "no frames"
    rather than render a broken scrubber."""
    html = render(Timeline(frames=[], traffic=[], messages=[], thinned=0), title="t")
    assert "no frames" in html.lower()
    assert "NaN" not in html


def test_the_actions_are_a_lane_of_their_own() -> None:
    """The one thing on the timeline Chrome cannot report. A filmstrip shows a
    field filling in; it does not say which call was in flight while it did."""
    page = render(timeline())
    assert "<h2>actions</h2>" in page
    assert ">goto<" in page and ">click<" in page and ">expect<" in page
    assert "locator(&#x27;#save&#x27;)" in page


def test_an_action_that_failed_shows_why_and_only_the_first_line() -> None:
    """A Python traceback in a column 200 pixels wide is a column of nothing.
    The rest is on the row's title, which is one hover away."""
    page = render(timeline())
    assert "WirespecTimeoutError: expected to be visible" in page
    assert "last saw that it was not" in page  # on the title
    assert "row act failed" in page


def test_an_action_with_no_measurable_duration_still_draws_a_bar() -> None:
    """The same reason a request that took no measurable time does: a bar too
    thin to see is a row that reads as absent."""
    page = render(
        Timeline(
            frames=[],
            traffic=[],
            messages=[],
            thinned=0,
            actions=[Action("click", "<Locator>", START_WALL, START_WALL, None)],
        )
    )
    assert f"width:{MIN_BAR:.3f}%" in page


def test_a_recording_with_no_actions_says_so() -> None:
    page = render(Timeline(frames=[], traffic=[], messages=[], thinned=0))
    assert "no actions were recorded" in page


def test_a_single_tab_recording_says_nothing_about_tabs() -> None:
    """The badge and the legend cost every row of every artefact ever written,
    and answer a question a one-page recording does not raise."""
    page = render(timeline())
    assert 'class="meta tabs"' not in page
    assert 'class="tab"' not in page


def test_a_second_tab_is_named_on_every_row_that_came_from_it() -> None:
    """Otherwise a request the popup made reads as one the page under test
    made, which is a wrong answer rather than a missing one."""
    story = timeline()
    story.tabs = ["http://127.0.0.1:8000/index.html", "http://127.0.0.1:8000/popup.html"]
    story.traffic[1].tab = 2
    story.messages[1].tab = 2
    story.actions[2].tab = 2
    story.frames[1].tab = 2

    page = render(story)
    assert "tab 1: 127.0.0.1:8000/index.html" in page
    assert "tab 2: 127.0.0.1:8000/popup.html" in page
    assert page.count('class="tab">2<') == 3
    assert page.count('class="tab">1<') == 4
    # And the filmstrip captions the frame, so the screen and the rows agree.
    assert page.count("tab 2: 127.0.0.1:8000/popup.html") == 2  # the legend, and the frame's caption


# --- the detail drawer (§16.6) ---------------------------------


def _detailed() -> Timeline:
    """A timeline whose requests carry everything a pane can show."""
    story = timeline()
    document, api = story.traffic
    document.request_headers = {"Accept": "text/html", "Cookie": "session=abc123"}
    document.response_headers = {"Content-Type": "text/html", "Set-Cookie": "session=abc123; Path=/"}
    document.body = "<!doctype html><title>hi</title>"
    document.protocol = "http/1.1"
    document.remote = "127.0.0.1:8000"
    api.request_headers = {"Authorization": "Bearer squirrel", "Content-Type": "application/json"}
    api.response_headers = {"Content-Type": "application/json"}
    api.post_data = '{"who":"me"}'
    api.body = '{"error":"boom"}'
    return story


def test_every_row_has_a_pane_and_every_pane_has_a_row() -> None:
    """The drawer is addressed by id, so a row pointing at a pane that is not
    there is a click that silently does nothing."""
    page = render(_detailed())
    assert page.count('data-pane="req-') == 2
    assert page.count('<div class="pane" id="req-') == 2
    for index in (0, 1):
        assert f'data-pane="req-{index}"' in page
        assert f'id="req-{index}"' in page


def test_headers_payload_and_body_are_all_rendered() -> None:
    """All of it laid out in Python: §16.4 keeps the script to
    seeking, playing and the playhead, and a pane built by script would also be
    a pane that vanishes when the file is opened with JavaScript off."""
    page = render(_detailed())
    assert "<summary>request headers</summary>" in page
    assert "<summary>response headers</summary>" in page
    assert "<summary>payload</summary>" in page
    assert "<summary>response body</summary>" in page
    assert "&quot;who&quot;:&quot;me&quot;" in page  # the request payload
    assert "&quot;error&quot;:&quot;boom&quot;" in page  # the response body
    assert "http/1.1" in page and "127.0.0.1:8000" in page


def test_credential_headers_are_starred_out_by_default() -> None:
    """An artefact is a file that gets emailed and attached to CI runs, which
    is a wider audience than the machine that made it."""
    page = render(_detailed())
    assert "Bearer squirrel" not in page
    assert "session=abc123" not in page
    assert page.count(">***<") == 3  # Authorization, Cookie, Set-Cookie
    # And the ordinary headers are untouched, or the pane says nothing at all.
    assert "application/json" in page


def test_redaction_can_be_turned_off() -> None:
    """For the case where the thing being debugged *is* the auth."""
    story = _detailed()
    story.redact = False
    page = render(story)
    assert "Bearer squirrel" in page
    assert "session=abc123" in page


def test_a_body_that_is_not_there_says_which_kind_of_not_there() -> None:
    """§16.3's rule, applied to bodies. "Empty" and "we did not
    ask" are different facts and send a reader to different places."""
    story = _detailed()
    skipped, unfinished = story.traffic
    skipped.body = None
    skipped.body_note = "not captured: image/png"
    unfinished.body = None
    unfinished.body_note = ""
    unfinished._finished = None

    page = render(story)
    assert "not captured: image/png" in page
    assert "had not completed when recording stopped" in page
    # And neither of them renders as an empty pane, which would read as a
    # server that answered with nothing.
    assert page.count('<p class="absent">') >= 2


def test_a_body_is_escaped_rather_than_closing_the_document() -> None:
    """A page under test that returns markup must not be able to end the
    artefact early. The same rule the console lines already live under."""
    story = _detailed()
    story.traffic[0].body = "</pre></div></body></html><script>alert(1)</script>"
    page = render(story)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert page.rstrip().endswith("</html>")
