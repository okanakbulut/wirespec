"""``Page`` — navigation, the per-document script hook, and screenshots."""

from typing import ClassVar

from msgspec import field

from wirespec.cdp.base import CDPStruct, Command, Event

__all__ = [
    "AddScriptToEvaluateOnNewDocument",
    "AddScriptToEvaluateOnNewDocumentResult",
    "BringToFront",
    "CaptureScreenshot",
    "CaptureScreenshotResult",
    "Disable",
    "DomContentEventFired",
    "Enable",
    "Frame",
    "FrameNavigated",
    "FrameStoppedLoading",
    "FrameTree",
    "GetFrameTree",
    "GetFrameTreeResult",
    "GetLayoutMetrics",
    "GetLayoutMetricsResult",
    "HandleJavaScriptDialog",
    "JavascriptDialogOpening",
    "LayoutViewport",
    "LifecycleEvent",
    "LoadEventFired",
    "Navigate",
    "NavigateResult",
    "NavigatedWithinDocument",
    "Reload",
    "RemoveScriptToEvaluateOnNewDocument",
    "ScreencastFrame",
    "ScreencastFrameAck",
    "ScreencastFrameMetadata",
    "SetLifecycleEventsEnabled",
    "StartScreencast",
    "StopLoading",
    "StopScreencast",
    "Viewport",
    "VisualViewport",
]


class Frame(CDPStruct):
    """Only ``id`` and ``url`` are required here. Chrome adds fields to this
    across versions and msgspec ignores what it was not told about, so being
    liberal costs nothing and stops a Chrome upgrade from breaking decoding."""

    id: str
    url: str
    loader_id: str = ""
    parent_id: str | None = None
    name: str | None = None
    security_origin: str = ""
    mime_type: str = ""


class FrameTree(CDPStruct):
    frame: Frame
    # Quoted, and staying quoted: under PEP 649 annotations resolve lazily, but
    # msgspec builds the struct layout eagerly and the class does not exist yet
    # at that point. noqa: UP037 -- these quotes are load-bearing.
    child_frames: "list[FrameTree] | None" = None  # noqa: UP037


class Viewport(CDPStruct):
    x: float
    y: float
    width: float
    height: float
    scale: float = 1.0


class Enable(Command[None]):
    __method__: ClassVar[str] = "Page.enable"


class Disable(Command[None]):
    __method__: ClassVar[str] = "Page.disable"


class NavigateResult(CDPStruct):
    frame_id: str
    loader_id: str | None = None
    #: Set when the navigation itself failed. Chrome reports this as a *result*,
    #: not as a protocol error, so it must be checked rather than caught.
    error_text: str | None = None


class Navigate(Command[NavigateResult]):
    __method__: ClassVar[str] = "Page.navigate"

    url: str
    referrer: str | None = None
    transition_type: str | None = None
    frame_id: str | None = None


class Reload(Command[None]):
    __method__: ClassVar[str] = "Page.reload"

    ignore_cache: bool = False
    script_to_evaluate_on_load: str | None = None


class StopLoading(Command[None]):
    __method__: ClassVar[str] = "Page.stopLoading"


class AddScriptToEvaluateOnNewDocumentResult(CDPStruct):
    identifier: str


class AddScriptToEvaluateOnNewDocument(Command[AddScriptToEvaluateOnNewDocumentResult]):
    """Runs before anything in the document does.

    Prefer serving the in-page runtime as an ordinary ``<script>`` where you
    control the build (§10.3) -- Chrome then gives it the HTTP
    cache and the V8 code cache, and no protocol call happens at all. This is
    for the pages where you do not.
    """

    __method__: ClassVar[str] = "Page.addScriptToEvaluateOnNewDocument"

    source: str
    world_name: str | None = None
    #: "includeCommandLineAPI" on the wire; the camel rename gets the acronym
    #: wrong and Chrome ignores the parameter without erroring.
    include_command_line_api: bool = field(default=False, name="includeCommandLineAPI")
    run_immediately: bool = False


class RemoveScriptToEvaluateOnNewDocument(Command[None]):
    __method__: ClassVar[str] = "Page.removeScriptToEvaluateOnNewDocument"

    identifier: str


class CaptureScreenshotResult(CDPStruct):
    #: base64. Chrome has no way to hand back bytes.
    data: str


class CaptureScreenshot(Command[CaptureScreenshotResult]):
    __method__: ClassVar[str] = "Page.captureScreenshot"

    format: str | None = None
    quality: int | None = None
    clip: Viewport | None = None
    from_surface: bool = True
    capture_beyond_viewport: bool = False
    optimize_for_speed: bool = False


class StartScreencast(Command[None]):
    """Stream the page as JPEG frames until told to stop (§16.1).

    **Damage-driven**, which is the property that makes the failure artefact
    affordable: a page that is not repainting costs nothing at all, and the
    frames appear exactly where something happened. Measured on this machine, a
    continuously animating 1280x720 page gives **60.2 fps** at 8 KB a frame,
    3 KB on an ordinary one, and a static page gives a handful and then nothing.

    The throttles are listed here in the order §16.3 wants them reached for, and
    the order is measured rather than assumed: ``every_nth_frame=3`` saved 67%,
    ``quality`` 60 to 20 saved 18% and made text unreadable, and
    ``max_width``/``max_height`` come last because a picture too small to read
    is a file that gets deleted unread.
    """

    __method__: ClassVar[str] = "Page.startScreencast"

    format: str | None = None
    quality: int | None = None
    max_width: int | None = None
    max_height: int | None = None
    every_nth_frame: int | None = None


class StopScreencast(Command[None]):
    __method__: ClassVar[str] = "Page.stopScreencast"


class ScreencastFrameAck(Command[None]):
    """Tell Chrome the frame arrived, so it renders another.

    Not optional and not a formality: measured, a stream that is never acked
    delivers **three** frames and then nothing, against 179 in the same three
    seconds with the ack in place. Three frames and silence does not look like a
    fault -- it looks like a page that stopped changing.

    The trap is *where* it is sent from -- the ack is a command on the
    connection the ``screencastFrame`` handler is being dispatched on, so
    awaiting it inside the handler deadlocks the read path. Same shape as
    answering ``Fetch.requestPaused`` (§16.2, §6.5).
    """

    __method__: ClassVar[str] = "Page.screencastFrameAck"

    session_id: int


class GetFrameTreeResult(CDPStruct):
    frame_tree: FrameTree


class GetFrameTree(Command[GetFrameTreeResult]):
    __method__: ClassVar[str] = "Page.getFrameTree"


class LayoutViewport(CDPStruct):
    page_x: int
    page_y: int
    client_width: int
    client_height: int


class VisualViewport(CDPStruct):
    offset_x: float
    offset_y: float
    #: The scroll offset, in CSS pixels. What ``DOM.getNodeForLocation`` has to
    #: be given on top of a viewport coordinate -- see ``GetLayoutMetrics``.
    page_x: float
    page_y: float
    client_width: float
    client_height: float
    scale: float
    zoom: float = 1.0


class GetLayoutMetricsResult(CDPStruct):
    #: The CSS-pixel views. The unprefixed ``layoutViewport`` and
    #: ``visualViewport`` are deprecated in favour of these, and only these are
    #: modelled.
    css_layout_viewport: LayoutViewport
    css_visual_viewport: VisualViewport


class GetLayoutMetrics(Command[GetLayoutMetricsResult]):
    """Where the page is scrolled to, without asking the page.

    Needed for one reason, and it is a trap worth stating here as well as in
    §8.13: **``DOM.getNodeForLocation`` takes document
    coordinates**, while ``DOM.getBoxModel`` returns viewport coordinates and
    ``Input.dispatchMouseEvent`` takes viewport coordinates. The two agree on an
    unscrolled page and diverge by exactly the scroll offset on every other one.
    """

    __method__: ClassVar[str] = "Page.getLayoutMetrics"


class BringToFront(Command[None]):
    __method__: ClassVar[str] = "Page.bringToFront"


class SetLifecycleEventsEnabled(Command[None]):
    __method__: ClassVar[str] = "Page.setLifecycleEventsEnabled"

    enabled: bool


class HandleJavaScriptDialog(Command[None]):
    __method__: ClassVar[str] = "Page.handleJavaScriptDialog"

    accept: bool
    prompt_text: str | None = None


class LoadEventFired(Event):
    """What ``goto`` waits for (§6.2)."""

    __method__: ClassVar[str] = "Page.loadEventFired"

    timestamp: float


class DomContentEventFired(Event):
    __method__: ClassVar[str] = "Page.domContentEventFired"

    timestamp: float


class FrameNavigated(Event):
    __method__: ClassVar[str] = "Page.frameNavigated"

    frame: Frame
    type: str | None = None


class NavigatedWithinDocument(Event):
    """A same-document navigation: a fragment, or a History API call.

    The one Chrome sends *instead of* ``frameNavigated``, not as well as it.
    Measured, Chrome 150: navigating to ``#where`` produces
    ``frameStartedNavigating`` with ``navigationType: "sameDocument"``, then
    this, then ``frameStoppedLoading`` -- and no ``loadEventFired`` and no
    ``frameNavigated`` at all. A page tracking only ``frameNavigated`` reports
    a stale URL for ever after the application changes the hash.
    """

    __method__: ClassVar[str] = "Page.navigatedWithinDocument"

    frame_id: str
    url: str
    navigation_type: str | None = None


class FrameStoppedLoading(Event):
    __method__: ClassVar[str] = "Page.frameStoppedLoading"

    frame_id: str


class LifecycleEvent(Event):
    __method__: ClassVar[str] = "Page.lifecycleEvent"

    frame_id: str
    loader_id: str
    name: str
    timestamp: float


class ScreencastFrameMetadata(CDPStruct):
    """The page as it was when the frame was taken.

    ``timestamp`` is **epoch seconds**, and it is the only clock in this
    document that agrees with a wall clock. Every ``Network`` timestamp is
    monotonic with an arbitrary origin, so putting frames and requests on one
    axis needs the offset from ``Network.requestWillBeSent``, the one event
    carrying both (§16.2). Optional on the wire, so a recorder must
    cope with a frame that has no time at all rather than assume one.
    """

    offset_top: float
    page_scale_factor: float
    device_width: float
    device_height: float
    scroll_offset_x: float
    scroll_offset_y: float
    timestamp: float | None = None


class ScreencastFrame(Event):
    """One frame, base64, with the scroll position it was taken at.

    ``session_id`` is per frame and is what ``ScreencastFrameAck`` matches on --
    not a target session id, despite the name.
    """

    __method__: ClassVar[str] = "Page.screencastFrame"

    data: str
    metadata: ScreencastFrameMetadata
    session_id: int


class JavascriptDialogOpening(Event):
    """An unhandled dialog blocks the page and every command against it, so a
    driver that ignores this hangs instead of failing."""

    __method__: ClassVar[str] = "Page.javascriptDialogOpening"

    url: str
    message: str
    type: str
    has_browser_handler: bool = False
    default_prompt: str | None = None
