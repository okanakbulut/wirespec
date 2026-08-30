"""``Accessibility`` — roles and accessible names, computed by Chrome itself.

This is where §4.2's decision lands. Accessible-name computation is
a real specification with a long tail -- ``aria-labelledby`` chasing, native
label association, ``alt``, ``title``, the text-content fallback and its pruning
rules -- and there are three ways to have it. Reimplementing it means wirespec
disagrees with the browser and the bug is in wirespec; vendoring someone else's
means it disagrees with the browser and the bug is in an artifact nobody here
maintains. Asking the browser leaves nothing to disagree with, and has no bytes,
no build step and no version to keep current.

**Note what is absent: ``queryAXTree``.** CDP appears to offer exactly what
``get_by_role`` wants -- pass a role and an accessible name, get matching nodes.
It was measured at **16.6 ms** warm, unchanged by adding a name filter and
unchanged by scoping to a twenty-element subtree, against **0.253 ms** for one
``getPartialAXTree``. The two flat rows are the informative ones: the cost is
not the search and not the size of the tree being asked about, it is a full
accessibility-tree construction paid per call, and no amount of scoping avoids
it. Closed on performance, twice, and now with the reason and not just the
number (§3.4).
"""

from typing import Any, ClassVar

from msgspec import field

from wirespec.cdp.base import CDPStruct, Command

__all__ = [
    "AXNode",
    "AXProperty",
    "AXRelatedNode",
    "AXValue",
    "AXValueSource",
    "Disable",
    "Enable",
    "GetFullAXTree",
    "GetFullAXTreeResult",
    "GetPartialAXTree",
    "GetPartialAXTreeResult",
]


class AXRelatedNode(CDPStruct):
    #: "backendDOMNodeId" -- the acronym is uppercase and ``rename="camel"``
    #: spells it ``backendDomNodeId``. See ``AXNode`` for what that cost.
    backend_dom_node_id: int | None = field(default=None, name="backendDOMNodeId")
    idref: str | None = None
    text: str | None = None


class AXValueSource(CDPStruct):
    """Where one candidate for a name came from.

    The whole cascade is here -- that a name came from ``aria-label`` rather
    than from contents, and which candidates were tried and superseded. That is
    exactly the material a failure message needs to explain itself
    (§1, goal 4).
    """

    type: str
    value: "AXValue | None" = None  # noqa: UP037 -- quoted for msgspec's eager layout
    attribute: str | None = None
    attribute_value: "AXValue | None" = None  # noqa: UP037
    superseded: bool = False
    native_source: str | None = None
    native_source_value: "AXValue | None" = None  # noqa: UP037
    invalid: bool = False
    invalid_reason: str | None = None


class AXValue(CDPStruct):
    type: str
    value: Any = None
    related_nodes: list[AXRelatedNode] | None = None
    sources: list[AXValueSource] | None = None


class AXProperty(CDPStruct):
    """``checked``, ``disabled``, ``focused``, ``required``, ``expanded`` and
    the rest -- which arrive with the role and the name, so the actionability
    checks that used to need a probe now need nothing (§5.2)."""

    name: str
    value: AXValue


class AXNode(CDPStruct):
    #: A string, and *not* a DOM node id. ``backend_dom_node_id`` is the one
    #: that maps back to the document.
    node_id: str
    ignored: bool
    ignored_reasons: list[AXProperty] | None = None
    role: AXValue | None = None
    chrome_role: AXValue | None = None
    name: AXValue | None = None
    description: AXValue | None = None
    value: AXValue | None = None
    properties: list[AXProperty] | None = None
    child_ids: list[str] | None = None
    #: **"backendDOMNodeId"**, with the acronym uppercase, and the reason this
    #: line has a comment. Under ``rename="camel"`` it goes out as
    #: ``backendDomNodeId``, the key never arrives, and the field is silently
    #: ``None`` for ever -- §8.10's second face exactly. It was
    #: written that way here first, and what it looked like was a role table
    #: that matched nothing at all while the accessibility tree was full of
    #: perfectly good roles.
    backend_dom_node_id: int | None = field(default=None, name="backendDOMNodeId")


class Enable(Command[None]):
    __method__: ClassVar[str] = "Accessibility.enable"


class Disable(Command[None]):
    __method__: ClassVar[str] = "Accessibility.disable"


class GetPartialAXTreeResult(CDPStruct):
    nodes: list[AXNode]


class GetPartialAXTree(Command[GetPartialAXTreeResult]):
    """One node's accessibility view. **0.253 ms**, and the confirm half of
    every role query.

    ``fetch_relatives`` defaults to CDP's own ``true``, which returns ancestors
    and siblings as well; the resolver leaves it on because the wanted node is
    then identified by ``backend_dom_node_id`` rather than by position, and
    turning it off does not measurably help.
    """

    __method__: ClassVar[str] = "Accessibility.getPartialAXTree"

    node_id: int | None = None
    backend_node_id: int | None = None
    object_id: str | None = None
    fetch_relatives: bool = True


class GetFullAXTreeResult(CDPStruct):
    nodes: list[AXNode]


class GetFullAXTree(Command[GetFullAXTreeResult]):
    """The whole accessibility tree.

    Far too slow for the driver and perfectly affordable for a test, which is
    the only place it is used: §11.2 checks that the role narrowing
    table's candidate set is a *superset* of what this reports.
    """

    __method__: ClassVar[str] = "Accessibility.getFullAXTree"

    depth: int | None = None
