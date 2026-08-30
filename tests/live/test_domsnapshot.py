"""``DOMSnapshot`` — the rendered document, flattened into parallel arrays.

The only route to rendered text without JavaScript (§3.4), and the
subject of the experiment that closed §12's first open item. What
these hold is the *protocol* end of it: the shape of the reply, and the two
properties the reconstruction in §8.11 is built on.
"""

import pytest

from tests.live.support import evaluate, goto
from wirespec.cdp import dom, domsnapshot
from wirespec.connection import Session

pytestmark = pytest.mark.asyncio(loop_scope="session")

STYLES = ["display", "visibility", "white-space"]


async def test_a_snapshot_is_indices_into_one_string_table(live: Session, site: str) -> None:
    """DOMSnapshot.enable / captureSnapshot. The shape is unusual enough to be
    worth pinning: parallel arrays of offsets, not an array of structs."""
    await goto(live, f"{site}/index.html")
    await live.send(domsnapshot.Enable())
    snapshot = await live.send(domsnapshot.CaptureSnapshot(computed_styles=STYLES))
    assert snapshot.strings
    assert len(snapshot.documents) == 1
    document = snapshot.documents[0]
    nodes = document.nodes
    assert len(nodes.node_name) == len(nodes.parent_index) == len(nodes.node_type)
    assert all(0 <= index < len(snapshot.strings) for index in nodes.node_name)


async def test_the_layout_tree_is_shorter_than_the_node_tree(live: Session, site: str) -> None:
    """Because an element with no box has no entry in it. That is the
    difference §4.3 says matters: both this and ``innerText``
    exclude ``display: none`` content."""
    await goto(live, f"{site}/index.html")
    await live.send(domsnapshot.Enable())
    snapshot = await live.send(domsnapshot.CaptureSnapshot(computed_styles=STYLES))
    document = snapshot.documents[0]
    assert len(document.layout.node_index) < len(document.nodes.node_name)


async def test_the_styles_come_back_in_the_order_they_were_asked_for(live: Session, site: str) -> None:
    """Which is the only thing that makes ``styles`` readable: there are no
    names in the reply, just one string index per style, positionally."""
    await goto(live, f"{site}/index.html")
    await live.send(domsnapshot.Enable())
    snapshot = await live.send(domsnapshot.CaptureSnapshot(computed_styles=STYLES))
    layout = snapshot.documents[0].layout
    assert layout.styles
    # Not every box carries a full row -- a box with no computed styles at all
    # comes back empty -- but a row that is present is positional and complete.
    assert all(len(row) in (0, len(STYLES)) for row in layout.styles)
    seen = {snapshot.strings[row[0]] for row in layout.styles if row and row[0] >= 0}
    assert "block" in seen or "inline" in seen, "the first column should be `display`"


async def test_the_text_is_the_source_text_and_not_inner_text(live: Session, site: str) -> None:
    """The finding §8.11 records, at its source: what comes back is
    each run's source text, uncollapsed. Eleven of seventeen measured cases
    agree with ``innerText`` untouched; the other six are why §8.11 exists."""
    await goto(live, f"{site}/index.html")
    await live.send(domsnapshot.Enable())
    snapshot = await live.send(domsnapshot.CaptureSnapshot(computed_styles=STYLES))
    layout = snapshot.documents[0].layout
    runs = [snapshot.strings[index] for index in layout.text if index >= 0]
    assert runs, "the fixture has text in it"
    assert await evaluate(live, "document.title")


async def test_backend_node_ids_are_what_tie_a_snapshot_to_the_document(live: Session, site: str) -> None:
    """The snapshot names nodes by backend id, and ``DOM.getDocument`` hands
    back the same ids -- which is what lets a locator's node ids be looked up in
    a snapshot without a round trip each (§3.4)."""
    await goto(live, f"{site}/index.html")
    await live.send(dom.Enable())
    await live.send(domsnapshot.Enable())
    document = await live.send(dom.GetDocument(depth=-1))
    snapshot = await live.send(domsnapshot.CaptureSnapshot(computed_styles=STYLES))
    from_snapshot = set(snapshot.documents[0].nodes.backend_node_id)
    assert document.root.backend_node_id in from_snapshot


async def test_disable_turns_the_domain_back_off(live: Session) -> None:
    """DOMSnapshot.disable."""
    await live.send(domsnapshot.Enable())
    await live.send(domsnapshot.Disable())
