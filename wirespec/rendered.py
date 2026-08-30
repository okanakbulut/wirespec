"""Rendered text, reconstructed from a ``DOMSnapshot``.

§8.11 is the finding and this is the implementation of it. The
snapshot hands back the **source** text of each layout run, not ``innerText``:
uncollapsed whitespace, no separator at block boundaries, and hidden runs
present but flagged. Out of the box that agrees with ``innerText`` on eleven of
seventeen measured cases; the six rules below take it to sixteen.

Each rule is a clause of the CSSOM "rendered text collection steps", and two of
them -- collapsing per run rather than across runs, and treating a ``<br>``'s
newline as hard -- were each found by a case that a *previous* rule had broken.
That is the signature of reimplementing a browser algorithm, and it is why
§4.3 records the remainder as a divergence rather than claiming
parity: whitespace either side of a ``display: none`` element merges in
``innerText`` and does not here.

It cannot change the outcome of an assertion. Every wirespec matcher normalises
whitespace before comparing (§4.2), so the difference is only
visible to a spec reading ``inner_text()`` verbatim.
"""

import re

from wirespec.cdp import domsnapshot as snapshot_domain

__all__ = ["STYLES", "source_text_by_backend_id", "text_by_backend_id"]

#: The computed styles the reconstruction needs, in the order it reads them.
STYLES = ("display", "visibility", "white-space")

#: Display values that do *not* start a new line.
_INLINE = frozenset({"inline", "inline-block", "inline-flex", "inline-grid", "contents", "ruby", "ruby-text", "none"})

#: ``white-space`` values under which text is taken verbatim.
_PRESERVING = frozenset({"pre", "pre-wrap", "break-spaces", "pre-line"})

#: Collapsible whitespace. Deliberately **not** ``\\s``: U+00A0 is whitespace to
#: Python and is not collapsible to CSS, and eating it changes the text.
_COLLAPSIBLE = " \t\n\r\f"

_TEXT_NODE = 3

_LINE_EDGES = re.compile(r"[ \t]*\n[ \t]*")


def source_text_by_backend_id(
    snapshot: snapshot_domain.CaptureSnapshotResult,
    wanted: set[int],
    skip: frozenset[str],
) -> dict[int, str]:
    """``textContent`` for each wanted element, keyed by backend node id.

    From the snapshot's DOM tree rather than from ``DOM.describeNode``, and
    that is the whole reason this function exists: **describeNode does not
    report whitespace-only text nodes at all** -- not in ``children`` and not
    in ``childNodeCount`` -- so a rebuild from it reads ``<b>a</b> <b>b</b>``
    as ``"ab"``. Two words become one, and no amount of normalising afterwards
    can put back a space that was never read (§8.21). The snapshot
    carries the whole tree, whitespace nodes included.

    ``skip`` is the tags whose text is not on the screen (§8.3);
    their subtrees are left out, which is the one way this deliberately differs
    from ``textContent``.

    Every document is scanned, not just the first, because a locator may be
    inside a frame and a frame is a document of its own in the snapshot.
    """
    found = dict.fromkeys(wanted, "")
    strings = snapshot.strings
    for document in snapshot.documents:
        nodes = document.nodes
        index_of = {backend: position for position, backend in enumerate(nodes.backend_node_id) if backend in wanted}
        if not index_of:
            continue
        roots_by_index = {position: backend for backend, position in index_of.items()}
        pieces: dict[int, list[str]] = {backend: [] for backend in index_of}
        # One forward pass. `parentIndex` is in document order, so a parent's
        # answers -- which wanted elements it is inside, and whether it is
        # inside something skipped -- are always already known.
        owners: list[frozenset[int]] = []
        skipped: list[bool] = []
        for position, parent in enumerate(nodes.parent_index):
            inherited = owners[parent] if 0 <= parent < position else frozenset()
            own = roots_by_index.get(position)
            owners.append(inherited | {own} if own is not None else inherited)
            name = strings[nodes.node_name[position]] if nodes.node_name[position] >= 0 else ""
            hidden = (skipped[parent] if 0 <= parent < position else False) or name in skip
            skipped.append(hidden)
            if hidden or nodes.node_type[position] != _TEXT_NODE:
                continue
            value = strings[nodes.node_value[position]] if nodes.node_value[position] >= 0 else ""
            for backend in owners[position]:
                pieces[backend].append(value)
        for backend, parts in pieces.items():
            found[backend] = "".join(parts)
    return found


def text_by_backend_id(
    snapshot: snapshot_domain.CaptureSnapshotResult,
    wanted: set[int],
) -> dict[int, str]:
    """The rendered text of each wanted element, keyed by backend node id.

    One pass over the snapshot for however many elements are asked about, which
    is what makes ``all_inner_texts`` over two hundred rows cost the same as one.
    """
    if not snapshot.documents:
        return {}
    document = snapshot.documents[0]
    nodes, layout, strings = document.nodes, document.layout, snapshot.strings

    # Which node index each wanted element is, and the reverse.
    index_of = {backend: position for position, backend in enumerate(nodes.backend_node_id) if backend in wanted}
    if not index_of:
        return dict.fromkeys(wanted, "")

    # One forward pass gives every node its wanted ancestors: parentIndex is in
    # document order, so a parent's answer is always already known.
    owners: list[frozenset[int]] = []
    roots_by_index = {position: backend for backend, position in index_of.items()}
    for position, parent in enumerate(nodes.parent_index):
        inherited = owners[parent] if 0 <= parent < position else frozenset()
        own = roots_by_index.get(position)
        owners.append(inherited | {own} if own is not None else inherited)

    pieces: dict[int, list[tuple[str, bool]]] = {backend: [] for backend in index_of}
    for box, node_index in enumerate(layout.node_index):
        holders = owners[node_index] if node_index < len(owners) else frozenset()
        if not holders:
            continue
        # Generated content -- ``::marker``, ``::before``, ``::after`` -- has
        # layout boxes with text in them, and ``innerText`` does not report it.
        # Measured on a <ul>: the marker arrives as a child node literally named
        # "::marker" whose box holds "\u2022 ", so every row would read
        # "\u2022 Row one".
        if nodes.node_name[node_index] and strings[nodes.node_name[node_index]].startswith("::"):
            continue
        style = layout.styles[box] if box < len(layout.styles) else []

        def styled(name: str, style: list[int] = style) -> str:
            position = STYLES.index(name)
            return strings[style[position]] if position < len(style) and style[position] >= 0 else ""

        if styled("visibility") == "hidden":
            continue
        text_index = layout.text[box] if box < len(layout.text) else -1
        if text_index < 0:
            display = styled("display")
            if display and display not in _INLINE:
                for backend in holders:
                    # Not guarded: a boundary newline at the very start or end
                    # of the result is trimmed, and the element's own box
                    # produces exactly one of those.
                    pieces[backend].append(("\n", False))
            continue
        raw = strings[text_index]
        if strings[nodes.node_name[node_index]] == "BR":
            # Chrome already put a newline in this run, and collapsing it
            # would turn the line break back into a space -- but it is still a
            # boundary, so the surrounding spaces fold into it.
            piece = ("\n", False)
        elif styled("white-space") in _PRESERVING:
            piece = (raw, True)
        else:
            # Per run, never across two: `before <hidden/> after` keeps the
            # space that innerText keeps.
            piece = (re.sub(f"[{_COLLAPSIBLE}]+", " ", raw), False)
        for backend in holders:
            pieces[backend].append(piece)

    return {backend: _join(parts) for backend, parts in pieces.items()} | {
        backend: "" for backend in wanted - set(index_of)
    }


def _join(parts: list[tuple[str, bool]]) -> str:
    """Assemble the runs, without letting the edge rules reach into a preserved
    one -- which is what would otherwise strip the whitespace a ``<pre>`` exists
    to keep."""
    text = "".join(piece for piece, _ in parts)
    guarded = "".join(("P" if keep else "c") * len(piece) for piece, keep in parts)

    out: list[str] = []
    mask: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\n" and guarded[index] == "c":
            while out and out[-1] in " \t" and mask[-1] == "c":
                out.pop()
                mask.pop()
            out.append("\n")
            mask.append("c")
            index += 1
            while index < len(text) and guarded[index] == "c" and (text[index] in " \t\n"):
                index += 1
            continue
        out.append(text[index])
        mask.append(guarded[index])
        index += 1

    start, end = 0, len(out)
    while start < end and out[start] in _COLLAPSIBLE and mask[start] == "c":
        start += 1
    while end > start and out[end - 1] in _COLLAPSIBLE and mask[end - 1] == "c":
        end -= 1
    return "".join(out[start:end])
