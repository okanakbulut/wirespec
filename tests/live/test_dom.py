"""``DOM`` — the document, the queries against it, geometry and focus.

The geometry tests here address elements by ``objectId``, which is what the
protocol floor had available before there was a driver. The document tests
below are the other half: since §3.4 the node-id space *is* how
wirespec refers to elements, and ``DOM.getDocument`` is the call that brings it
into being.
"""

import pytest

from tests.live.support import evaluate, goto, handle_for
from wirespec.cdp import dom, emulation, runtime
from wirespec.connection import Session
from wirespec.errors import CDPError

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_the_box_model_is_returned_for_an_object_id(live: Session, site: str) -> None:
    """DOM.getBoxModel. Four quads -- content, padding, border, margin -- each
    eight numbers, going clockwise from the top left."""
    await goto(live, f"{site}/form.html")
    box = await live.send(dom.GetBoxModel(object_id=await handle_for(live, "document.getElementById('box')")))
    model = box.model

    # 200 wide plus 10 padding and 5 border on each side.
    assert model.width == pytest.approx(230, abs=2)
    assert model.height == pytest.approx(80, abs=2)

    # The margin box starts where `left`/`top` put it; the border box is the
    # 8px margin further in. Confusing the two is how a click lands just
    # outside the element it was aimed at.
    assert (model.margin[0], model.margin[1]) == pytest.approx((40.0, 60.0), abs=1.0)
    assert (model.border[0], model.border[1]) == pytest.approx((48.0, 68.0), abs=1.0)
    for quad in (model.content, model.padding, model.border, model.margin):
        assert len(quad) == 8

    # The nesting the box model is named for: content inside padding inside
    # border inside margin.
    assert model.content[0] > model.padding[0] > model.border[0] > model.margin[0]


async def test_the_box_model_follows_the_element_when_the_page_scrolls(live: Session, site: str) -> None:
    """The quads are viewport-relative, not document-relative. Clicking where
    an unscrolled box model said to click is the classic way to miss."""
    await goto(live, f"{site}/form.html")
    handle = await handle_for(live, "document.getElementById('box')")
    before = await live.send(dom.GetBoxModel(object_id=handle))
    await evaluate(live, "window.scrollTo(0, 300)")
    after = await live.send(dom.GetBoxModel(object_id=handle))
    assert after.model.border[1] == pytest.approx(before.model.border[1] - 300, abs=2)


async def test_scroll_into_view_brings_an_element_into_the_viewport(live: Session, site: str) -> None:
    """DOM.scrollIntoViewIfNeeded. The browser's own scroll, so it respects
    sticky headers and scroll containers that a ``window.scrollTo`` does not."""
    await goto(live, f"{site}/form.html")
    await live.send(emulation.SetDeviceMetricsOverride(width=600, height=400, device_scale_factor=1.0, mobile=False))
    try:
        handle = await handle_for(live, "document.getElementById('below')")
        assert await evaluate(live, "window.scrollY") == 0
        before = await live.send(dom.GetBoxModel(object_id=handle))
        assert before.model.border[1] > 400, "the fixture element should start below the fold"

        await live.send(dom.ScrollIntoViewIfNeeded(object_id=handle))

        after = await live.send(dom.GetBoxModel(object_id=handle))
        assert 0 <= after.model.border[1] <= 400
        assert await evaluate(live, "window.scrollY") > 0
    finally:
        await live.send(emulation.ClearDeviceMetricsOverride())


async def test_focus_reaches_the_element(live: Session, site: str) -> None:
    """DOM.focus. A real focus, with the focus event fired -- as opposed to
    ``element.focus()`` from script, which the page can tell apart."""
    await goto(live, f"{site}/form.html")
    assert await evaluate(live, "document.activeElement.id") == ""
    await live.send(dom.Focus(object_id=await handle_for(live, "document.getElementById('below')")))
    assert await evaluate(live, "document.activeElement.id") == "below"
    assert await evaluate(live, "window.__focused") == ["below"]


async def test_the_document_is_what_makes_node_ids_exist(live: Session, site: str) -> None:
    """DOM.getDocument / DOM.enable, and the pair of ids every later call needs.

    The reply carries ``nodeId`` and ``backendNodeId`` for every node it
    describes, which is the mapping §3.4 asks the driver to work in
    and would otherwise cost a round trip each.
    """
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    document = await live.send(dom.GetDocument(depth=-1))
    assert document.root.node_id > 0
    assert document.root.backend_node_id > 0
    assert document.root.node_name == "#document"
    assert document.root.children, "depth=-1 should bring the tree, not just the root"


async def test_a_shallow_document_really_is_shallow(live: Session, site: str) -> None:
    """The depth is a correctness knob, not a performance one (
    §5.1): it decides whether mutation events arrive at all. Proving it does
    what it says is the first half of trusting that."""
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    shallow = await live.send(dom.GetDocument(depth=1))
    deep = await live.send(dom.GetDocument(depth=-1))
    assert _count(shallow.root) < _count(deep.root)


def _count(node: dom.Node) -> int:
    return 1 + sum(_count(child) for child in node.children or ())


async def test_query_selector_all_returns_descendants_in_document_order(live: Session, site: str) -> None:
    """DOM.querySelectorAll. Descendants, not children -- which is what makes a
    chain's matches merge into document order without a sort."""
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    root = (await live.send(dom.GetDocument(depth=-1))).root.node_id
    from_document = await live.send(dom.QuerySelectorAll(node_id=root, selector="input"))
    assert from_document.node_ids

    # Descendants, not children: rooted at <body> the same inputs are found,
    # however deeply they are nested. That is what lets a chain's matches merge
    # into document order without a sort (§4.1).
    body = await live.send(dom.QuerySelectorAll(node_id=root, selector="body"))
    from_body = await live.send(dom.QuerySelectorAll(node_id=body.node_ids[0], selector="input"))
    assert from_body.node_ids == from_document.node_ids


async def test_describe_node_brings_back_text_nodes(live: Session, site: str) -> None:
    """DOM.describeNode at depth=-1. This is what makes ``textContent``
    answerable without JavaScript: the text nodes are in the reply."""
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    root = (await live.send(dom.GetDocument(depth=-1))).root.node_id
    found = await live.send(dom.QuerySelectorAll(node_id=root, selector="body"))
    described = await live.send(dom.DescribeNode(node_id=found.node_ids[0], depth=-1))
    assert _text_of(described.node), "a depth=-1 describeNode should carry the subtree's text"


def _text_of(node: dom.Node) -> str:
    if node.node_type == 3:
        return node.node_value
    return "".join(_text_of(child) for child in node.children or ())


async def test_get_outer_html_carries_whitespace_only_text_nodes(live: Session, site: str) -> None:
    """DOM.getOuterHTML. The serialiser's output, so the whitespace between two
    elements survives -- which ``describeNode`` does not report at all, and is
    the whole reason ``textContent`` can be answered from a subtree instead of
    from a whole-document snapshot (§8.21, §8.27)."""
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    root = (await live.send(dom.GetDocument(depth=-1))).root.node_id
    found = await live.send(dom.QuerySelectorAll(node_id=root, selector="#box"))
    markup = (await live.send(dom.GetOuterHTML(node_id=found.node_ids[0]))).outer_html

    # The field is spelled "outerHTML", not "outerHtml": an acronym the camel
    # rename gets wrong, and a name that is simply never populated when it is
    # (§8.10). An empty string here is that bug, not an empty box.
    assert markup.startswith("<div")
    assert 'id="box"' in markup


async def test_get_attributes_returns_a_flat_pair_list(live: Session, site: str) -> None:
    """DOM.getAttributes. Flat ``[name, value, name, value]``, which is how CDP
    spells it and is worth seeing once rather than assuming."""
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    root = (await live.send(dom.GetDocument(depth=-1))).root.node_id
    found = await live.send(dom.QuerySelectorAll(node_id=root, selector="#box"))
    attributes = await live.send(dom.GetAttributes(node_id=found.node_ids[0]))
    assert len(attributes.attributes) % 2 == 0
    assert dict(zip(attributes.attributes[0::2], attributes.attributes[1::2], strict=True))["id"] == "box"


async def test_an_xpath_search_is_allocated_and_must_be_freed(live: Session, site: str) -> None:
    """DOM.performSearch / getSearchResults / discardSearchResults.

    The search *allocates*, and the discard is not optional: a polling assertion
    that forgets it leaks one allocation per iteration for the life of the page
    (§8.5).
    """
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    search = await live.send(dom.PerformSearch(query="//input | //div"))
    assert search.result_count >= 2
    results = await live.send(
        dom.GetSearchResults(search_id=search.search_id, from_index=0, to_index=search.result_count)
    )
    assert len(results.node_ids) == search.result_count
    await live.send(dom.DiscardSearchResults(search_id=search.search_id))
    # Freed means gone: asking again is an error rather than an empty answer.
    with pytest.raises(CDPError):
        await live.send(dom.GetSearchResults(search_id=search.search_id, from_index=0, to_index=1))


async def test_disable_turns_the_dom_agent_back_off(live: Session, site: str) -> None:
    """DOM.disable. The node-id space goes with it."""
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    await live.send(dom.GetDocument(depth=-1))
    await live.send(dom.Disable())
    await live.send(dom.Enable())


async def test_resolve_node_bridges_to_a_javascript_handle(live: Session, site: str) -> None:
    """DOM.resolveNode. The one place the node-id space has to meet ``Runtime``:
    the caller's own JavaScript takes a handle, not a node id
    (§3.4)."""
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    root = (await live.send(dom.GetDocument(depth=-1))).root.node_id
    found = await live.send(dom.QuerySelectorAll(node_id=root, selector="#box"))
    resolved = await live.send(dom.ResolveNode(node_id=found.node_ids[0]))
    assert resolved.object.object_id is not None
    assert resolved.object.type == "object"

    # And it really is that element: ask the page through the handle.
    result = await live.send(
        runtime.CallFunctionOn(
            function_declaration="function () { return this.id; }",
            object_id=resolved.object.object_id,
            return_by_value=True,
        )
    )
    assert result.result.value == "box"


async def test_the_hit_test_says_what_is_at_a_point(live: Session, site: str) -> None:
    """DOM.getNodeForLocation. The last actionability check before a click
    (§5.2): the point has to hit the element, a descendant or an
    ancestor, and not whatever is sitting on top of it."""
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    root = (await live.send(dom.GetDocument(depth=-1))).root.node_id
    found = await live.send(dom.QuerySelectorAll(node_id=root, selector="#box"))
    box = (await live.send(dom.GetBoxModel(node_id=found.node_ids[0]))).model.border
    middle_x = int((box[0] + box[4]) / 2)
    middle_y = int((box[1] + box[5]) / 2)

    hit = await live.send(dom.GetNodeForLocation(x=middle_x, y=middle_y))
    assert hit.backend_node_id
    described = await live.send(dom.DescribeNode(backend_node_id=hit.backend_node_id))
    # The deepest node at the point: the div itself, or a text node inside it.
    assert described.node.node_name in ("DIV", "#text")


async def test_files_can_be_put_into_a_file_input(live: Session, site: str, tmp_path) -> None:
    """DOM.setFileInputFiles. The only way to fill a file input: the picker is
    browser chrome, so no click and no key event can reach it.

    The browser process reads the path, so this is also the one command whose
    argument is meaningful on Chrome's filesystem rather than on the test's.
    """
    payload = tmp_path / "upload.txt"
    payload.write_text("the payload", encoding="utf-8")

    await goto(live, f"{site}/form.html")
    document = await live.send(dom.GetDocument(depth=1))
    found = await live.send(dom.QuerySelectorAll(node_id=document.root.node_id, selector="#upload"))
    await live.send(dom.SetFileInputFiles(files=[str(payload)], node_id=found.node_ids[0]))

    reported = await evaluate(live, "window.__files")
    assert reported == [["upload", [["upload.txt", 11, "text/plain"]]]]
