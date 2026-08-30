"""What happened, kept in memory until something asks for it.

The failure artefact (§16) is the recorded screen and the network
traffic on one timeline, written for the tests that fail and thrown away for
the tests that pass. This module is the collecting half: it subscribes to a
page, buffers what arrives, and holds nothing on disk. Writing is
``wirespec/artefact.py``, and the retention policy is the pytest hook.

**Three clocks arrive here, not two**, and only one of them is the one a
developer reads:

===============================================  ===========================
``Page.screencastFrame`` ``metadata.timestamp``  epoch **seconds**
``Network.*`` ``timestamp``                      **monotonic** seconds
``Network.requestWillBeSent`` ``wallTime``       epoch **seconds**
``Runtime.consoleAPICalled`` ``timestamp``       epoch **milliseconds**
===============================================  ===========================

Measured on this machine, all within the same 30 ms: a frame at
``1787954943.52``, a response at ``1450933.49``, a console message at
``1787954943509.07``. A recorder that treats those as one unit produces a
filmstrip and a waterfall that are each individually correct and cannot be read
together, which is worse than either alone. :class:`Clocks` is the conversion,
and ``requestWillBeSent`` is the only event carrying both of the first two, so
it is what teaches it.
"""

import asyncio
import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from wirespec.cdp import network as network_domain
from wirespec.cdp import page as page_domain
from wirespec.cdp import runtime as runtime_domain
from wirespec.errors import CDPError

if TYPE_CHECKING:
    from wirespec.page import Page

__all__ = ["Action", "Clocks", "Entry", "Frame", "Message", "Recorder", "Timeline"]

#: JPEG at this quality is what §16.1 measured: about 8 KB a frame on a
#: full-bleed gradient, 3 KB on an ordinary page. Dropping to 20 saved 18% and
#: made the text unreadable, which is why quality is the *second* throttle.
DEFAULT_QUALITY = 60

#: How many frames to keep. 60 fps measured on a continuously animating page,
#: so this is about ten seconds of the worst case and minutes of an ordinary
#: one -- the thinning below is what makes the difference bearable.
DEFAULT_MAX_FRAMES = 600

#: And a byte cap, because a length cap is not a memory bound: a frame measured
#: 3 KB on a text page, 8 KB on a full-bleed gradient, and a 4K viewport would
#: make it far more. Whichever cap is reached first thins the buffer.
DEFAULT_MAX_BYTES = 48 * 1024 * 1024

#: Requests and console lines are small and are dropped oldest-first rather
#: than thinned: a waterfall missing every other request is misleading in a way
#: a filmstrip at half the frame rate is not.
DEFAULT_MAX_EVENTS = 2000

#: How much of one response body to keep. Enough for the JSON a failing
#: assertion is about; far short of a bundle.
DEFAULT_MAX_BODY = 256 * 1024

#: And a budget across the whole recording, because the per-body cap is not a
#: bound: a page making four hundred XHRs would otherwise inline a hundred
#: megabytes into a file somebody has to open. Spent oldest-first -- once it is
#: gone, later bodies are *noted as skipped* rather than quietly absent
#: (§16.3: whatever the caps threw away is printed).
DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024

#: How long ``stop`` waits for the body reads it has already issued.
BODY_TIMEOUT = 2.0

#: What a redacted header value renders as.
REDACTED = "***"

#: Headers whose value is a credential. Redacted by default because an artefact
#: is a file that gets emailed and attached to CI runs, which is a wider
#: audience than the machine that made it. ``Recorder(redact=False)`` turns it
#: off, for the case where the thing being debugged *is* the auth.
SECRET_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "authentication",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
        "x-session-token",
    }
)

#: Exact media types whose body is text even though the type does not say
#: ``text/``. Anything ending ``+json`` or ``+xml`` is caught by suffix instead.
_TEXT_TYPES = frozenset(
    {
        "application/json",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "application/xml",
        "application/xhtml+xml",
        "application/x-www-form-urlencoded",
        "application/graphql",
        "image/svg+xml",
    }
)


def is_text(mime: str) -> bool:
    """Is a body of this media type worth keeping as text?

    The filter that keeps the artefact openable. Images, fonts, media and
    binaries are the bulk of an ordinary page load and none of them is readable
    in a detail pane, so their bodies are never fetched -- which also means the
    round trip is never spent on them (§16.6).

    Unknown types answer **no**. A body that cannot be shown as text is worse
    than absent: it renders as a screenful of mojibake and buries the rows.
    """
    kind = mime.split(";", 1)[0].strip().lower()
    if not kind:
        return False
    return kind.startswith("text/") or kind in _TEXT_TYPES or kind.endswith(("+json", "+xml"))


class Clocks:
    """Three clocks, one axis.

    Everything is reported in **epoch seconds**, because that is the clock a
    frame arrives on and a frame is the thing a developer scrubs to.

    The conversion is deliberately done at *write* time rather than as events
    arrive. A recorder started mid-page can see a ``loadingFinished`` for a
    request whose ``requestWillBeSent`` it missed, and converting eagerly would
    have to drop that event or invent a time for it; converting late means the
    offset only has to be known by the time somebody asks.
    """

    __slots__ = ("_offset",)

    def __init__(self) -> None:
        #: ``None`` rather than ``0.0``, because zero is a valid offset -- it is
        #: what a machine booted at the epoch would have -- and "not learned
        #: yet" must not be confusable with "learned, and it is nothing".
        self._offset: float | None = None

    def __repr__(self) -> str:
        return f"<Clocks offset={self._offset}>" if self._offset is not None else "<Clocks unlearned>"

    @property
    def known(self) -> bool:
        return self._offset is not None

    def learn(self, monotonic: float, wall: float) -> None:
        """Take the offset from ``Network.requestWillBeSent``, the one event
        carrying both clocks.

        The **first** one wins. Measured, two requests 14 ms apart agreed to six
        decimal places, so a later lesson differing at all means the wall clock
        was stepped underneath the recording -- and moving the axis then would
        shift every event already placed on it rather than fix anything.
        """
        if self._offset is None:
            self._offset = wall - monotonic

    def epoch(self, monotonic: float) -> float | None:
        """A ``Network`` timestamp as epoch seconds, or ``None`` if nothing has
        taught this yet."""
        return None if self._offset is None else monotonic + self._offset

    @staticmethod
    def from_millis(millis: float) -> float:
        """A ``Runtime`` timestamp as epoch seconds.

        The third clock, and the one that is out by a factor of a thousand
        rather than by an origin: console messages and page exceptions are epoch
        **milliseconds** while everything else here is seconds. Treated as
        seconds, a console message lands in the year 58,000 and the artefact's
        axis collapses to a single spike at the end (§16.2).
        """
        return millis / 1000.0


class Frame:
    """One screencast frame: the JPEG, and when it was taken."""

    __slots__ = ("at", "data", "height", "scroll_y", "tab", "width")

    def __init__(self, data: str, at: float, width: float, height: float, scroll_y: float, tab: int = 1) -> None:
        #: base64, as it arrived. Never decoded here: the artefact embeds it as
        #: a data URI, so decoding would only be undone again.
        self.data = data
        #: Epoch seconds.
        self.at = at
        self.width = width
        self.height = height
        self.scroll_y = scroll_y
        #: Which recorded tab this came from, 1-based in the order the
        #: recorder attached. 1 is always the page the recorder was given.
        self.tab = tab

    def __repr__(self) -> str:
        return f"<Frame {self.width:.0f}x{self.height:.0f} at {self.at:.3f}>"


class Entry:
    """One request, from the moment it went out to whatever became of it.

    Times are stored as Chrome sent them -- **monotonic** -- and converted on
    read, so an entry whose ``requestWillBeSent`` predates the recording still
    lands correctly once any later request has taught the offset.
    """

    __slots__ = (
        "_clocks",
        "_finished",
        "_responded",
        "_started",
        "body",
        "body_note",
        "cached",
        "failed",
        "kind",
        "method",
        "mime",
        "post_data",
        "protocol",
        "remote",
        "request_headers",
        "request_id",
        "response_headers",
        "size",
        "status",
        "status_text",
        "tab",
        "url",
    )

    def __init__(
        self, clocks: Clocks, request_id: str, url: str, method: str, kind: str, started: float, tab: int = 1
    ) -> None:
        self._clocks = clocks
        self.request_id = request_id
        self.url = url
        self.method = method
        #: Chrome's own resource type -- "Document", "XHR", "Image". What lets
        #: the waterfall be read at a glance rather than by URL suffix.
        self.kind = kind
        #: Which recorded tab this came from, 1-based in the order the
        #: recorder attached. 1 is always the page the recorder was given.
        self.tab = tab
        self._started = started
        self._responded: float | None = None
        self._finished: float | None = None
        self.status: int | None = None
        self.status_text: str = ""
        self.mime: str = ""
        self.size: float = 0.0
        #: Chrome's ``errorText`` for a request that produced no response, and
        #: ``None`` for one that did. A failed request is the one worth looking
        #: at, so it stays in the waterfall rather than being dropped for
        #: having no status.
        self.failed: str | None = None
        #: What went out and what came back, as Chrome sent them. Kept
        #: **unredacted** here and redacted where the artefact is written: this
        #: is the record of what happened, and the file is the thing that gets
        #: shared (§16.6).
        self.request_headers: dict[str, str] = {}
        self.response_headers: dict[str, str] = {}
        #: The request body, when there was one. Comes free with
        #: ``requestWillBeSent`` -- no round trip, and it is the half of a
        #: failing POST that the status code does not explain.
        self.post_data: str | None = None
        #: The response body, and why it is not here when it is not. Exactly one
        #: of these is ever set: a body that was skipped, truncated or refused
        #: says so in the pane rather than rendering as nothing at all.
        self.body: str | None = None
        self.body_note: str = ""
        #: Connection detail, free from ``responseReceived``.
        self.protocol: str = ""
        self.remote: str = ""
        self.cached: bool = False

    def __repr__(self) -> str:
        outcome = self.failed or self.status or "in flight"
        return f"<Entry {self.method} {self.url} {outcome}>"

    @property
    def started(self) -> float | None:
        return self._clocks.epoch(self._started)

    @property
    def responded(self) -> float | None:
        return None if self._responded is None else self._clocks.epoch(self._responded)

    @property
    def finished(self) -> float | None:
        return None if self._finished is None else self._clocks.epoch(self._finished)

    @property
    def completed(self) -> bool:
        """Did this request reach ``loadingFinished`` or ``loadingFailed``?

        The body is read on the first of those and nowhere else, so this is what
        separates "the response was empty" from "the recording stopped before
        the response did". A ``fetch()`` whose body the page never drains is the
        ordinary way to see the second -- Chrome does not finish the load until
        somebody reads it -- and the two are not the same news.
        """
        return self._finished is not None


class Message:
    """A console call or a page-side throw, on the frames' clock."""

    __slots__ = ("at", "level", "line", "tab", "text", "url")

    def __init__(self, at: float, level: str, text: str, url: str = "", line: int | None = None, tab: int = 1) -> None:
        #: Epoch seconds, already divided: ``Runtime`` reports milliseconds.
        self.at = at
        #: Chrome's own console type -- "log", "warning", "error", "debug" --
        #: except for a page-side throw, which has none and is called "error".
        self.level = level
        self.text = text
        self.url = url
        self.line = line
        #: Which recorded tab this came from, 1-based in the order the
        #: recorder attached. 1 is always the page the recorder was given.
        self.tab = tab

    def __repr__(self) -> str:
        return f"<Message {self.level} {self.text[:40]!r}>"


def as_text(arg: runtime_domain.RemoteObject) -> str:
    """One console argument as text, without asking the page anything.

    ``console.log`` arguments arrive as ``RemoteObject``s: a primitive carries
    ``value``, and everything else carries only a ``description`` because
    nothing asked for it by value. Serialising the primitives as **JSON** rather
    than with ``str`` is deliberate -- this is reporting what the *page*
    printed, and Python would render its booleans ``True`` and its ``None``
    ``None``, neither of which the page ever said.
    """
    if arg.unserializable_value is not None:
        return arg.unserializable_value
    if isinstance(arg.value, str):
        return arg.value
    if arg.value is not None:
        return json.dumps(arg.value)
    if arg.description is not None:
        return arg.description
    return arg.type


class Action:
    """One call the driver made, and how long it took.

    The lane the frames cannot supply. A filmstrip shows a field filling in; it
    does not show that the fill spent four seconds waiting for the element to
    stop moving, or that the assertion after it is the one that gave up
    (§16.2).

    Already on the frames' clock when it gets here: ``Page.acting`` reads
    ``time.time``, so there is no conversion and nothing for :class:`Clocks` to
    have learned first -- which matters, because a recording of a page that
    never made a request would have no offset to convert with.
    """

    __slots__ = ("at", "failure", "name", "tab", "target", "until")

    def __init__(self, name: str, target: str, at: float, until: float, failure: str | None, tab: int = 1) -> None:
        self.name = name
        self.target = target
        self.at = at
        self.until = until
        #: Which recorded tab this came from, 1-based in the order the
        #: recorder attached. 1 is always the page the recorder was given.
        self.tab = tab
        #: ``"WirespecTimeoutError: ..."``, or ``None`` for one that worked.
        #: Kept rather than dropped: an artefact is written *because* a test
        #: failed, so the action that raised is the row somebody opens it for.
        self.failure = failure

    def __repr__(self) -> str:
        return f"<Action {self.name} {self.target!r}{' failed' if self.failure else ''}>"

    @property
    def took(self) -> float:
        return max(self.until - self.at, 0.0)


class Timeline:
    """Everything on one axis, in seconds from the first thing that happened.

    The value the writer renders, and the reason it is a value rather than a
    method on :class:`Recorder`: the axis can be checked against known-ordered
    traffic with no browser anywhere near it, which is what §16.2
    needs. A frame 200 ms out of place is not obviously wrong to look at, and it
    sends someone hunting the wrong request.
    """

    __slots__ = (
        "actions",
        "dropped",
        "end",
        "frames",
        "messages",
        "redact",
        "start",
        "tabs",
        "thinned",
        "traffic",
        "unplaced",
    )

    def __init__(
        self,
        *,
        frames: list[Frame],
        traffic: list[Entry],
        messages: list[Message],
        thinned: int,
        dropped: int = 0,
        actions: list[Action] | None = None,
        tabs: list[str] | None = None,
        redact: bool = True,
    ) -> None:
        #: Whether the writer stars out credential headers. A property of the
        #: rendering, not of the traffic.
        self.redact = redact
        #: Where each recorded tab ended up, in tab order. One entry means the
        #: artefact is about a single page and says nothing about tabs at all;
        #: more than one is what makes the labels on the rows worth printing.
        self.tabs = tabs or []
        #: Requests whose start could not be converted, because the recording
        #: never saw a ``requestWillBeSent`` to take the offset from. Counted
        #: and reported rather than dropped: a waterfall quietly missing a
        #: request is a waterfall that answers the wrong question.
        self.unplaced = sum(1 for entry in traffic if entry.started is None)
        self.frames = sorted(frames, key=lambda frame: frame.at)
        self.traffic = sorted(
            (entry for entry in traffic if entry.started is not None),
            key=lambda entry: entry.started or 0.0,
        )
        self.messages = sorted(messages, key=lambda message: message.at)
        self.actions = sorted(actions or [], key=lambda action: action.at)
        self.thinned = thinned
        self.dropped = dropped

        moments = [frame.at for frame in self.frames]
        moments += [message.at for message in self.messages]
        moments += [moment for action in self.actions for moment in (action.at, action.until)]
        for entry in self.traffic:
            moments += [moment for moment in (entry.started, entry.responded, entry.finished) if moment is not None]
        self.start = min(moments) if moments else 0.0
        self.end = max(moments) if moments else 0.0

    def __repr__(self) -> str:
        return f"<Timeline {self.span:.3f}s, {len(self.frames)} frames, {len(self.traffic)} requests>"

    @classmethod
    def of(cls, recorder: Recorder) -> Timeline:
        return cls(
            frames=list(recorder.frames),
            traffic=list(recorder.traffic),
            messages=list(recorder.messages),
            actions=list(recorder.actions),
            # Read now rather than when the tab opened: a popup is announced at
            # `about:blank` and navigates on its own, so its opening URL names
            # nothing a reader would recognise.
            tabs=[page.url for page in recorder.pages],
            thinned=recorder.thinned,
            dropped=recorder.dropped,
            redact=recorder.redact,
        )

    @property
    def span(self) -> float:
        """How long the recording covers. **Never zero.**

        The writer divides by this to place everything, and a recording holding
        one frame or none would otherwise render a page of ``NaN`` -- which
        reads as a broken driver rather than as a test that did nothing.
        """
        return max(self.end - self.start, 0.001)

    def at(self, epoch: float) -> float:
        """An epoch time as seconds since the recording began."""
        return epoch - self.start


class Recorder:
    """Subscribes to one page and buffers what it sees."""

    def __init__(
        self,
        page: Page,
        *,
        quality: int = DEFAULT_QUALITY,
        every_nth_frame: int = 1,
        max_frames: int = DEFAULT_MAX_FRAMES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_events: int = DEFAULT_MAX_EVENTS,
        bodies: bool = True,
        redact: bool = True,
        max_body: int = DEFAULT_MAX_BODY,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        self.page = page
        #: Every page being recorded, in attach order. The first is the one the
        #: recorder was given; the rest are tabs the application opened while
        #: it was running. A record's ``tab`` is an index into this, 1-based.
        self.pages: list[Page] = []
        self.quality = quality
        self.every_nth_frame = every_nth_frame
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self.max_events = max_events
        #: Whether to read response bodies at all. Headers and the request
        #: payload cost nothing -- they arrive with events the recorder is
        #: already subscribed to -- but a body is a round trip per response, so
        #: this is the switch for a suite that wants the waterfall and not the
        #: contents (§16.6).
        self.bodies = bodies
        #: Whether credential headers are starred out where the artefact is
        #: written. Carried to the ``Timeline`` rather than applied here: the
        #: buffer is the record of what happened, and the file is what travels.
        self.redact = redact
        self.max_body = max_body
        self.max_body_bytes = max_body_bytes
        self._body_bytes = 0
        #: Body reads in flight. Held so `stop` can wait for the ones it already
        #: issued -- a task dropped on the floor here is a body missing from the
        #: artefact for no reason a reader could work out.
        self._reading: set[asyncio.Task[None]] = set()
        #: How many frames the caps threw away. Reported in the artefact rather
        #: than kept quiet: a filmstrip that silently skips is a filmstrip that
        #: gets read as "nothing happened there" (§1, goal 4).
        self.thinned = 0
        #: Frames that arrived with no timestamp and so could not be placed on
        #: the axis. Has never been observed above zero -- ``timestamp`` is
        #: optional on the wire and Chrome has always sent it -- and is counted
        #: rather than assumed so that a Chrome which stops sending it says so.
        self.dropped = 0
        self._bytes = 0
        self.clocks = Clocks()
        self.frames: list[Frame] = []
        self.traffic: list[Entry] = []
        self.messages: list[Message] = []
        self.actions: list[Action] = []
        #: (tab, request id) -> its entry, so a response can find the request
        #: it answers. Keyed by tab as well, because two targets number their
        #: requests independently and nothing promises the ids will not meet.
        #: Cleared with the rest of the buffer, and an update for an entry that
        #: is no longer here is dropped rather than resurrecting it.
        self._by_id: dict[tuple[int, str], Entry] = {}
        self._recording = False
        self._unsubscribe: list[Callable[[], None]] = []
        self._arrived = asyncio.Event()

    def __repr__(self) -> str:
        return f"<Recorder {len(self.frames)} frames, {len(self.traffic)} requests>"

    async def start(self) -> None:
        """Begin recording. Idempotent, and refuses a page that is gone."""
        if self._recording:
            return
        self.page._check_open()
        self._recording = True
        # Target discovery, so a tab the application opens is adopted and can be
        # followed. Enabled here rather than left to `expect_popup`, because a
        # recording is exactly the case where nobody wrote the block: a spec that
        # never mentions the popup is a spec whose artefact used to say nothing
        # about it (§16.2).
        await self.page.context.watch_for_popups()
        self._unsubscribe.append(self.page.context.on_page(self._opened))
        await self._attach(self.page)

    async def _attach(self, page: Page) -> None:
        """Subscribe to one page and start its screencast."""
        # A page already attached keeps its number. `stop` leaves `pages` alone
        # -- the timeline reads the tab URLs off it afterwards -- so a recorder
        # started, stopped and started again would otherwise give the same page
        # two tabs, and every row from the second run would name a tab the
        # legend has no URL for.
        if page in self.pages:
            tab = self.pages.index(page) + 1
        else:
            self.pages.append(page)
            tab = len(self.pages)
        # `Runtime` is already enabled on every page; `Network` is not, because
        # it costs a decode per request and nothing pays for it until something
        # asks to watch (§3.5). A recording is asking.
        await page._enable_network()
        session = page.session
        self._unsubscribe += [
            session.on(page_domain.ScreencastFrame, lambda event: self._frame(event, page, tab)),
            session.on(network_domain.RequestWillBeSent, lambda event: self._sent(event, tab)),
            session.on(network_domain.ResponseReceived, lambda event: self._received(event, tab)),
            session.on(network_domain.LoadingFinished, lambda event: self._done(event, page, tab)),
            session.on(network_domain.LoadingFailed, lambda event: self._failed(event, tab)),
            session.on(runtime_domain.ConsoleAPICalled, lambda event: self._console(event, tab)),
            session.on(runtime_domain.ExceptionThrown, lambda event: self._threw(event, tab)),
            # Not an event at all: the driver is the one thing on the timeline
            # Chrome cannot report, so `Page` tells the recorder directly.
            page.watch_actions(
                lambda name, target, at, until, failure: self._acted(name, target, at, until, failure, tab)
            ),
        ]
        await session.send(
            page_domain.StartScreencast(format="jpeg", quality=self.quality, every_nth_frame=self.every_nth_frame)
        )

    async def _opened(self, page: Page) -> None:
        """A tab appeared in the context while this recording was running."""
        if self._recording:
            await self._attach(page)

    async def stop(self) -> None:
        """Stop recording, keeping everything buffered. Idempotent.

        **Sends nothing if the page has already closed**, which is the ordinary
        teardown order rather than an edge case: a context closes its pages
        before a recorder's fixture unwinds. Measured, and it is worse than an
        error -- the *first* command sent on the session of a page that was
        closed while it had a screencast is **never answered at all**, so the
        stop waits for ever and buries the failure the recording was being kept
        for (§8.15).
        """
        if not self._recording:
            return
        self._recording = False
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        # The last responses of a test are the ones its failure is usually
        # about, and their body reads are in flight exactly when `stop` is
        # called. Bounded, because a page closing mid-read is the ordinary
        # teardown race and waiting for ever would bury the failure the
        # recording is being kept for -- the same hazard as §8.15 above.
        if self._reading:
            reading = set(self._reading)
            await asyncio.wait(reading, timeout=BODY_TIMEOUT)
            for task in reading:
                task.cancel()
        for page in self.pages:
            if not page.closed:
                await page.session.send(page_domain.StopScreencast())

    async def wait_for(self, enough: Callable[[Recorder], bool], *, timeout: float = 15.0) -> None:
        """Wait until the buffer satisfies ``enough``.

        Push, not poll: every handler sets the same event, so this wakes when
        something actually arrived. The alternative is a sleep, and a sleep long
        enough to be reliable is a sleep too long to run in a suite
        (§5.1).
        """
        async with asyncio.timeout(timeout):
            while True:
                # Cleared *before* the check, not between it and the wait. The
                # handlers cannot interleave here -- there is no await between
                # them -- but the other order only reads as correct once you
                # have worked that out, and it stops being correct the moment
                # anything in the loop learns to await.
                self._arrived.clear()
                if enough(self):
                    return
                await self._arrived.wait()

    def _fit(self) -> None:
        """Bring the filmstrip back inside its caps by **thinning**, not truncating.

        §16.3: "one animating page must not evict the twenty
        seconds before it". A ring buffer would do exactly that -- three seconds
        of animation is 179 frames, so an animation at the end of a run throws
        away the click that caused the failure. Halving the density of the older
        half instead keeps the whole span and lets the beginning get coarser,
        which is the right thing to lose first.

        Frame 0 always survives, because ``[:half:2]`` starts at it.
        """
        while len(self.frames) > self.max_frames or self._bytes > self.max_bytes:
            half = len(self.frames) // 2
            if half < 2:
                # Too short to thin. Drop the oldest, which is a ring buffer's
                # ordinary behaviour and the only thing left that makes progress.
                self._bytes -= len(self.frames.pop(0).data)
                self.thinned += 1
                continue
            keeping = self.frames[:half:2] + self.frames[half:]
            self.thinned += len(self.frames) - len(keeping)
            self._bytes = sum(len(frame.data) for frame in keeping)
            self.frames = keeping

    def _bound[T](self, records: list[T]) -> None:
        """Oldest-first, for the small records. See ``DEFAULT_MAX_EVENTS``."""
        if len(records) > self.max_events:
            del records[: len(records) - self.max_events]

    def _sent(self, event: network_domain.RequestWillBeSent, tab: int) -> None:
        """The Rosetta stone: the only event carrying both clocks, which is why
        the offset is taken here and nowhere else (§16.2)."""
        self.clocks.learn(event.timestamp, event.wall_time)
        entry = Entry(
            self.clocks,
            event.request_id,
            event.request.url,
            event.request.method,
            event.type or "",
            event.timestamp,
            tab,
        )
        entry.request_headers = dict(event.request.headers)
        # Free: `requestWillBeSent` carries it, so the payload of a failing POST
        # costs nothing to keep. Chrome truncates it at `Network.enable`'s
        # `maxPostDataSize`, and `has_post_data` with no `post_data` is how a
        # body larger than that arrives -- said rather than left blank.
        if event.request.post_data is not None:
            entry.post_data = event.request.post_data
        elif event.request.has_post_data:
            entry.post_data = ""
        # A redirect reuses the request id, so the hop that was replaced would
        # otherwise be overwritten and vanish from the waterfall. Both are kept;
        # only the newest is addressable by id.
        self.traffic.append(entry)
        self._by_id[tab, event.request_id] = entry
        self._bound(self.traffic)
        self._arrived.set()

    def _received(self, event: network_domain.ResponseReceived, tab: int) -> None:
        entry = self._by_id.get((tab, event.request_id))
        if entry is None:
            return
        entry._responded = event.timestamp  # Entry is this module's own record type
        entry.status = event.response.status
        entry.status_text = event.response.status_text
        entry.mime = event.response.mime_type
        entry.response_headers = dict(event.response.headers)
        entry.protocol = event.response.protocol or ""
        if event.response.remote_ip_address:
            port = f":{event.response.remote_port}" if event.response.remote_port else ""
            entry.remote = f"{event.response.remote_ip_address}{port}"
        entry.cached = event.response.from_disk_cache
        self._arrived.set()

    def _done(self, event: network_domain.LoadingFinished, page: Page, tab: int) -> None:
        """The response is complete, which is the **only** moment its body can
        be read: Chrome holds it until the page navigates away and then answers
        "No resource with given identifier" (``Network.getResponseBody``).

        So the read is issued here and off the read path, the same shape as the
        screencast ack -- awaiting inside a handler deadlocks the connection the
        command has to go out on (§16.2).
        """
        entry = self._by_id.get((tab, event.request_id))
        if entry is None:
            return
        entry._finished = event.timestamp  # as above
        entry.size = event.encoded_data_length
        if self._wants_body(entry):
            task = asyncio.get_running_loop().create_task(self._body(page, entry, event.request_id))
            self._reading.add(task)
            task.add_done_callback(self._reading.discard)
        self._arrived.set()

    def _wants_body(self, entry: Entry) -> bool:
        """Should this response's body be fetched? Every no is *recorded*.

        §16.3's rule applies to bodies as much as to frames: a pane
        that is simply empty reads as "the response was empty", which is a
        different and much more interesting fact than "we did not ask".
        """
        if not self.bodies:
            return False
        if not is_text(entry.mime):
            entry.body_note = f"not captured: {entry.mime or 'unknown media type'}"
            return False
        if self._body_bytes >= self.max_body_bytes:
            entry.body_note = "not captured: the recording's body budget was spent"
            return False
        return True

    async def _body(self, page: Page, entry: Entry, request_id: str) -> None:
        """Read one response body. Never raises into the loop's handler."""
        if page.closed:
            entry.body_note = "not captured: the page had closed"
            return
        try:
            reply = await page.session.send(network_domain.GetResponseBody(request_id=request_id))
        except CDPError as exc:
            # Routine rather than exceptional: a request the page navigated away
            # from, a redirect hop, a body Chrome never buffered. The row keeps
            # its headers and timing and says why the body is missing.
            entry.body_note = f"not available: {exc.message}"
            return
        if reply.base64_encoded:
            # A type `is_text` accepted whose body Chrome hands back base64
            # anyway. Decoding it would produce the mojibake the filter exists
            # to keep out of the pane.
            entry.body_note = "not captured: Chrome returned it as binary"
            return
        text = reply.body
        if len(text) > self.max_body:
            entry.body = text[: self.max_body]
            entry.body_note = f"truncated: {self.max_body:,} of {len(text):,} characters"
        else:
            entry.body = text
        self._body_bytes += len(entry.body)
        self._arrived.set()

    def _failed(self, event: network_domain.LoadingFailed, tab: int) -> None:
        entry = self._by_id.get((tab, event.request_id))
        if entry is None:
            return
        entry._finished = event.timestamp  # as above
        entry.failed = event.error_text or ("canceled" if event.canceled else "failed")
        self._arrived.set()

    def _acted(self, name: str, target: str, at: float, until: float, failure: str | None, tab: int) -> None:
        """One completed action. Already on the frames' clock."""
        self.actions.append(Action(name, target, at, until, failure, tab))
        self._bound(self.actions)
        self._arrived.set()

    def _console(self, event: runtime_domain.ConsoleAPICalled, tab: int) -> None:
        frame = event.stack_trace.call_frames[0] if event.stack_trace and event.stack_trace.call_frames else None
        self.messages.append(
            Message(
                Clocks.from_millis(event.timestamp),
                event.type,
                " ".join(as_text(arg) for arg in event.args),
                frame.url if frame else "",
                frame.line_number + 1 if frame else None,
                tab,
            )
        )
        self._bound(self.messages)
        self._arrived.set()

    def _threw(self, event: runtime_domain.ExceptionThrown, tab: int) -> None:
        """A page-side throw. ``exception.description`` is the JavaScript stack,
        which is the part worth having; ``text`` alone says "Uncaught"."""
        details = event.exception_details
        described = details.exception.description if details.exception else None
        self.messages.append(
            Message(
                Clocks.from_millis(event.timestamp),
                "error",
                described or details.text,
                details.url or "",
                details.line_number + 1,
                tab,
            )
        )
        self._bound(self.messages)
        self._arrived.set()

    def _frame(self, event: page_domain.ScreencastFrame, page: Page, tab: int) -> None:
        """A frame arrived. **Ack off the read path.**

        The ack is a command on the connection this handler is being dispatched
        on, so awaiting it here deadlocks the read path -- the same shape as
        answering ``Fetch.requestPaused`` (§16.2, §6.5). And it is
        not optional: measured, a recorder that never acks receives **three**
        frames and then silence, against 179 in the same three seconds with the
        ack in place.
        """
        metadata = event.metadata
        # A frame with no timestamp cannot be placed on the axis, and a
        # filmstrip is nothing but an axis. `timestamp` is optional on the wire;
        # it has never been seen absent, and if it ever is, the frame is counted
        # as dropped rather than silently given a time it did not have.
        if metadata.timestamp is None:
            self.dropped += 1
        else:
            self.frames.append(
                Frame(
                    event.data,
                    metadata.timestamp,
                    metadata.device_width,
                    metadata.device_height,
                    metadata.scroll_offset_y,
                    tab,
                )
            )
            self._bytes += len(event.data)
            self._fit()
            self._arrived.set()
        asyncio.get_running_loop().create_task(self._ack(page, event.session_id))

    async def _ack(self, page: Page, session_id: int) -> None:
        # Not `if closed: return` and then send -- the page can close between
        # the two. The check is worth having anyway because it is the common
        # case; the guard below is what makes it correct.
        if page.closed:
            return
        try:
            await page.session.send(page_domain.ScreencastFrameAck(session_id=session_id))
        except CDPError:
            # This one exception, and only from here. The ack for the last frame
            # routinely races the page closing and loses, and Chrome answers
            # "Session with given id not found" -- there is nothing left to
            # record either way. Anything else is a real failure and is left to
            # the loop's exception handler, which the live suite turns back into
            # a test failure (§11.1).
            pass
