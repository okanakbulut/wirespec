"""``DOMSnapshot`` — the rendered document, flattened into parallel arrays.

This is where rendered text comes from, because wirespec has no JavaScript to
evaluate ``element.innerText`` with (§3.4). What comes back is not
``innerText``: it is the **source** text of each layout run, plus whichever
computed styles were asked for. §8.11 measures the difference and
gives the six rules that reconstruct ``innerText`` from it -- sixteen of
seventeen cases, with the one residual named.

**The shape is unusual and worth reading once.** Everything is an index. A
snapshot is a list of ``documents`` plus one flat ``strings`` table shared by
all of them, and every name, value and text in a document is an offset into that
table. The node tree and the layout tree are each a struct of parallel arrays
rather than an array of structs, so "the third node's name" is
``strings[nodes.node_name[2]]``. It is an odd thing to read and a fast thing to
send, and only the fields wirespec actually uses are modelled here.
"""

from typing import ClassVar

from msgspec import field

from wirespec.cdp.base import CDPStruct, Command

__all__ = [
    "CaptureSnapshot",
    "CaptureSnapshotResult",
    "Disable",
    "DocumentSnapshot",
    "Enable",
    "LayoutTreeSnapshot",
    "NodeTreeSnapshot",
]


class NodeTreeSnapshot(CDPStruct):
    """The document's nodes, as parallel arrays indexed by node position."""

    #: -1 for a root. In document order, so one forward pass closes a subtree.
    parent_index: list[int] = field(default_factory=list)
    node_type: list[int] = field(default_factory=list)
    #: Indices into the shared ``strings`` table.
    node_name: list[int] = field(default_factory=list)
    node_value: list[int] = field(default_factory=list)
    backend_node_id: list[int] = field(default_factory=list)
    #: Per node, a flat ``[name, value, name, value, …]`` of string indices.
    attributes: list[list[int]] = field(default_factory=list)


class LayoutTreeSnapshot(CDPStruct):
    """The boxes that were actually laid out. Shorter than the node tree: a
    ``display: none`` element has no entry here at all, which is the difference
    §4.3 says matters."""

    #: Which node each box belongs to, as an index into ``NodeTreeSnapshot``.
    node_index: list[int] = field(default_factory=list)
    #: Per box, one string index per style named in ``computed_styles``.
    styles: list[list[int]] = field(default_factory=list)
    bounds: list[list[float]] = field(default_factory=list)
    #: The box's own text, as a string index, or -1 for a box with none.
    text: list[int] = field(default_factory=list)


class DocumentSnapshot(CDPStruct):
    nodes: NodeTreeSnapshot
    layout: LayoutTreeSnapshot


class CaptureSnapshotResult(CDPStruct):
    documents: list[DocumentSnapshot]
    #: The one shared table every index in every document points into.
    strings: list[str]


class CaptureSnapshot(Command[CaptureSnapshotResult]):
    """0.225 ms, and the only way to rendered text without JavaScript.

    ``computed_styles`` is required and is the whole of what the reconstruction
    in §8.11 needs: ``display`` to put block boundaries back,
    ``visibility`` to drop hidden runs, ``white-space`` to know whether
    collapsing is allowed.
    """

    __method__: ClassVar[str] = "DOMSnapshot.captureSnapshot"

    computed_styles: list[str]
    include_paint_order: bool = False
    #: "includeDOMRects" -- acronym uppercase, and the camel rename gets it
    #: wrong. Chrome would accept the misspelling, ignore it and say nothing
    #: (§8.10).
    include_dom_rects: bool = field(default=False, name="includeDOMRects")
    include_blended_background_colors: bool = False
    include_text_color_opacities: bool = False


class Enable(Command[None]):
    __method__: ClassVar[str] = "DOMSnapshot.enable"


class Disable(Command[None]):
    __method__: ClassVar[str] = "DOMSnapshot.disable"
