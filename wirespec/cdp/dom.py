"""``DOM`` — box geometry, and only by object reference.

Conspicuously absent: ``DOM.getDocument`` and the whole ``NodeId`` space. The
DOM agent's node ids do not exist until ``getDocument`` has been called, and
every call that needs one then needs that round trip first. Passing an
``objectId`` from ``Runtime`` straight to ``getBoxModel`` skips the problem
entirely (§8.9).
"""

from typing import ClassVar

from msgspec import field

from wirespec.cdp.base import CDPStruct, Command

__all__ = [
    "BoxModel",
    "DescribeNode",
    "DescribeNodeResult",
    "Disable",
    "DiscardSearchResults",
    "Enable",
    "Focus",
    "GetAttributes",
    "GetAttributesResult",
    "GetBoxModel",
    "GetBoxModelResult",
    "GetDocument",
    "GetDocumentResult",
    "GetNodeForLocation",
    "GetNodeForLocationResult",
    "GetOuterHTML",
    "GetOuterHTMLResult",
    "GetSearchResults",
    "GetSearchResultsResult",
    "Node",
    "PerformSearch",
    "PerformSearchResult",
    "Quad",
    "QuerySelectorAll",
    "QuerySelectorAllResult",
    "ResolveNode",
    "ResolveNodeResult",
    "ScrollIntoViewIfNeeded",
    "SetFileInputFiles",
]

#: Eight numbers: four corners, clockwise from top-left.
type Quad = list[float]


class Node(CDPStruct):
    """One node of the document, as ``getDocument`` describes it.

    Liberal on purpose: Chrome adds fields to this across versions and msgspec
    ignores what it was not told about, so a Chrome upgrade cannot break
    decoding. What is *needed* is the first four -- the two ids, so a nodeId can
    be turned into a backendNodeId without a round trip, and the name and type,
    so the resolver can tell an element from a text node.
    """

    node_id: int
    backend_node_id: int
    node_type: int
    node_name: str
    local_name: str = ""
    node_value: str = ""
    #: Flat ``[name, value, name, value, …]``, which is how CDP spells it.
    attributes: list[str] | None = None
    child_node_count: int | None = None
    # Quoted, and staying quoted: under PEP 649 annotations resolve lazily, but
    # msgspec builds the struct layout eagerly and the class does not exist yet
    # at that point. noqa: UP037 -- these quotes are load-bearing.
    children: "list[Node] | None" = None  # noqa: UP037
    content_document: "Node | None" = None  # noqa: UP037
    parent_id: int | None = None


class Enable(Command[None]):
    """Turns on the node-id space *and* the mutation events the wait loop
    listens to (§5.1)."""

    __method__: ClassVar[str] = "DOM.enable"


class Disable(Command[None]):
    __method__: ClassVar[str] = "DOM.disable"


class GetDocumentResult(CDPStruct):
    root: Node


class GetDocument(Command[GetDocumentResult]):
    """The root node, and -- at ``depth=-1`` -- every node under it.

    **The depth is not a performance knob, it is a correctness one.** Mutations
    are only reported for nodes already pushed to the client, so after a
    ``depth=1`` call *nothing arrives at all* -- not one event for any mutation
    anywhere -- and the wait loop silently degrades to timer-only without
    anything failing. A later shallow call turns the push back off again, just
    as quietly. Measured: 3.51 ms at ``depth=-1`` against 0.49 ms shallow, on a
    200-row page (§5.1).

    So the one call does three jobs: it gives the root that ``querySelectorAll``
    is anchored at, it enables mutation notifications, and its reply carries
    every node's ``backendNodeId`` alongside its ``nodeId``, which is the
    mapping §3.4 wants and would otherwise cost a round trip each.
    Whatever owns the document must own the depth.
    """

    __method__: ClassVar[str] = "DOM.getDocument"

    depth: int = 1
    pierce: bool = False


class QuerySelectorAllResult(CDPStruct):
    node_ids: list[int]


class QuerySelectorAll(Command[QuerySelectorAllResult]):
    """Every descendant of ``node_id`` matching a CSS selector, in document
    order. 0.104 ms, and the step every chain is built out of."""

    __method__: ClassVar[str] = "DOM.querySelectorAll"

    node_id: int
    selector: str


class GetNodeForLocationResult(CDPStruct):
    backend_node_id: int
    frame_id: str = ""
    #: Only set when the node has already been pushed to the client, which
    #: after a ``getDocument(depth=-1)`` it has been.
    node_id: int | None = None


class GetNodeForLocation(Command[GetNodeForLocationResult]):
    """What is actually at this point. 0.055 ms, and the last actionability
    check before a click (§5.2 step 7).

    ``ignore_pointer_events_none`` is left at CDP's ``false``: an overlay with
    ``pointer-events: none`` does not receive the click, so it must not count as
    something in the way either.
    """

    __method__: ClassVar[str] = "DOM.getNodeForLocation"

    x: int
    y: int
    #: "includeUserAgentShadowDOM" -- acronym uppercase (§8.10).
    include_user_agent_shadow_dom: bool = field(default=False, name="includeUserAgentShadowDOM")
    ignore_pointer_events_none: bool = False


class GetAttributesResult(CDPStruct):
    #: Flat ``[name, value, name, value, …]``, which is how CDP spells it.
    attributes: list[str]


class GetAttributes(Command[GetAttributesResult]):
    """One element's attributes. 0.073 ms, and pipelines like everything else."""

    __method__: ClassVar[str] = "DOM.getAttributes"

    node_id: int


class DescribeNodeResult(CDPStruct):
    node: Node


class DescribeNode(Command[DescribeNodeResult]):
    """One node, and at ``depth=-1`` its whole subtree -- **text nodes
    included**, which is what makes ``textContent`` answerable without
    JavaScript. Measured: a ``<div>`` with a nested ``<span>`` comes back with
    both text nodes in order."""

    __method__: ClassVar[str] = "DOM.describeNode"

    node_id: int | None = None
    backend_node_id: int | None = None
    object_id: str | None = None
    depth: int = 1
    pierce: bool = False


class PerformSearchResult(CDPStruct):
    search_id: str
    result_count: int


class PerformSearch(Command[PerformSearchResult]):
    """A plain-text, selector or **XPath** search of the whole document.

    XPath is how §8.5's rule is expressed without JavaScript, and
    it is the slowest query wirespec has at 2.763 ms -- 25x a
    ``querySelectorAll`` -- which is worth knowing before putting one inside a
    poll loop.

    The search is *allocated*, and stays allocated: every call must be paired
    with ``DiscardSearchResults`` or a polling assertion leaks one per iteration
    for the life of the page.
    """

    __method__: ClassVar[str] = "DOM.performSearch"

    query: str
    #: "includeUserAgentShadowDOM", acronym uppercase. The camel rename spells
    #: it ``includeUserAgentShadowDom``, which Chrome accepts, ignores, and says
    #: nothing about -- §8.10's third face. Caught by the
    #: conformance suite the moment this member landed, which is what that suite
    #: is for.
    include_user_agent_shadow_dom: bool = field(default=False, name="includeUserAgentShadowDOM")


class GetSearchResultsResult(CDPStruct):
    node_ids: list[int]


class GetSearchResults(Command[GetSearchResultsResult]):
    """A window of a search's hits. ``to_index`` is exclusive."""

    __method__: ClassVar[str] = "DOM.getSearchResults"

    search_id: str
    from_index: int
    to_index: int


class DiscardSearchResults(Command[None]):
    """Frees what ``PerformSearch`` allocated. Not optional -- see there."""

    __method__: ClassVar[str] = "DOM.discardSearchResults"

    search_id: str


class _Resolved(CDPStruct):
    type: str
    object_id: str | None = None
    class_name: str | None = None
    description: str | None = None


class ResolveNodeResult(CDPStruct):
    #: A ``Runtime.RemoteObject``. Typed loosely here so ``dom`` does not have
    #: to import ``runtime``: only ``object_id`` is ever read.
    object: _Resolved


class ResolveNode(Command[ResolveNodeResult]):
    """A node id to a ``Runtime`` handle -- the bridge to the caller's own
    JavaScript, and the only place wirespec needs one (§3.4)."""

    __method__: ClassVar[str] = "DOM.resolveNode"

    node_id: int | None = None
    backend_node_id: int | None = None
    object_group: str | None = None


class BoxModel(CDPStruct):
    """Use ``border``. ``getBoundingClientRect``, which every spec's mental model
    is built on, is the border box, and a padded button's content box can sit
    tens of pixels from where the button looks (§8.9)."""

    content: Quad
    padding: Quad
    border: Quad
    margin: Quad
    width: int
    height: int


class GetOuterHTMLResult(CDPStruct):
    #: "outerHTML" -- acronym uppercase, like every other one here
    #: (§8.10). The camel rename spells it ``outerHtml``, which
    #: Chrome answers and this struct would never see.
    outer_html: str = field(name="outerHTML")


class GetOuterHTML(Command[GetOuterHTMLResult]):
    """One node's serialised subtree, **whitespace text nodes included**.

    Which is the whole reason it is here. ``textContent`` has to be answered
    from something that reports whitespace-only text nodes -- ``describeNode``
    does not (§8.21) -- and until now the only other thing that
    did was ``DOMSnapshot.captureSnapshot``, which reads the *entire document*
    to answer about one element. Measured on a 14k-node page: 58.2 ms for the
    snapshot against 0.13 ms here, and the two agree on every element of every
    fixture page in the suite.

    The cost is proportional to the subtree, so it is the right call for one
    element and the wrong one for all of them at once; ``resolve.text_contents``
    is where that choice is made.
    """

    __method__: ClassVar[str] = "DOM.getOuterHTML"

    node_id: int | None = None
    backend_node_id: int | None = None
    object_id: str | None = None


class GetBoxModelResult(CDPStruct):
    model: BoxModel


class GetBoxModel(Command[GetBoxModelResult]):
    __method__: ClassVar[str] = "DOM.getBoxModel"

    object_id: str | None = None
    node_id: int | None = None
    backend_node_id: int | None = None


class ScrollIntoViewIfNeeded(Command[None]):
    __method__: ClassVar[str] = "DOM.scrollIntoViewIfNeeded"

    object_id: str | None = None
    node_id: int | None = None
    backend_node_id: int | None = None
    rect: dict[str, float] | None = None


class Focus(Command[None]):
    __method__: ClassVar[str] = "DOM.focus"

    object_id: str | None = None
    node_id: int | None = None
    backend_node_id: int | None = None


class SetFileInputFiles(Command[None]):
    """Put files into an ``<input type="file">`` without a file dialog.

    The **only** way to do it: a file picker is chrome, not page, so no click
    and no key event reaches it. Paths are absolute and are read by the
    *browser* process, which matters when it is not the machine the test is on.

    Chrome checks the ``multiple`` attribute and refuses more than one file for
    an input without it, which is a real error rather than a silent truncation.
    """

    __method__: ClassVar[str] = "DOM.setFileInputFiles"

    files: list[str]
    node_id: int | None = None
    backend_node_id: int | None = None
    object_id: str | None = None
