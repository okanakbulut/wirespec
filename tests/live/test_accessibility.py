"""``Accessibility`` — roles and accessible names, as Chrome computes them.

This domain exists in the subset because §4.2 chose to ask the
browser rather than reimplement ARIA or vendor somebody's implementation of it.
These tests are about the protocol: that the calls work, what shape the answers
have, and -- for ``queryAXTree``'s absence -- why the cheap-looking one is not
here.
"""

import pytest

from tests.live.support import goto
from wirespec.cdp import accessibility, dom
from wirespec.connection import Session

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def node_for(live: Session, selector: str) -> int:
    root = (await live.send(dom.GetDocument(depth=-1))).root.node_id
    found = await live.send(dom.QuerySelectorAll(node_id=root, selector=selector))
    assert found.node_ids, f"the fixture has no {selector}"
    return found.node_ids[0]


async def test_a_partial_tree_names_one_node_as_chrome_sees_it(live: Session, site: str) -> None:
    """Accessibility.enable / getPartialAXTree. **0.253 ms**, and the confirm
    half of every role query."""
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    await live.send(accessibility.Enable())
    reply = await live.send(
        accessibility.GetPartialAXTree(node_id=await node_for(live, "input"), fetch_relatives=False)
    )
    assert len(reply.nodes) == 1, "fetch_relatives=False should answer about exactly the node asked about"
    node = reply.nodes[0]
    assert node.role is not None
    assert node.backend_dom_node_id is not None, (
        "backendDOMNodeId spells its acronym uppercase; a camel-renamed struct "
        "leaves this permanently None and nothing fails (§8.10)"
    )


async def test_relatives_are_returned_when_they_are_asked_for(live: Session, site: str) -> None:
    """The CDP default is ``true``, which the struct matches. The resolver turns
    it off; a test that never exercised the default would leave that choice
    unexamined."""
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    await live.send(accessibility.Enable())
    node_id = await node_for(live, "input")
    alone = await live.send(accessibility.GetPartialAXTree(node_id=node_id, fetch_relatives=False))
    with_relatives = await live.send(accessibility.GetPartialAXTree(node_id=node_id))
    assert len(with_relatives.nodes) > len(alone.nodes)


async def test_the_name_arrives_with_the_cascade_that_produced_it(live: Session, site: str) -> None:
    """``sources`` is the whole computation -- every candidate, which won and
    which were superseded. It is what lets ``get_by_label`` tell "named by a
    label" from "named by its own contents", and it is the material a failure
    message needs to explain itself (§3.4)."""
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    await live.send(accessibility.Enable())
    reply = await live.send(
        accessibility.GetPartialAXTree(node_id=await node_for(live, "input"), fetch_relatives=False)
    )
    name = reply.nodes[0].name
    assert name is not None and name.sources, "an input's name should come with its cascade"
    assert {source.type for source in name.sources} >= {"attribute"}


async def test_properties_arrive_with_the_role(live: Session, site: str) -> None:
    """``disabled``, ``focused``, ``required``, ``checked`` -- the actionability
    checks that used to need a probe now need nothing (§5.2)."""
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    await live.send(accessibility.Enable())
    reply = await live.send(
        accessibility.GetPartialAXTree(node_id=await node_for(live, "input"), fetch_relatives=False)
    )
    properties = {item.name for item in reply.nodes[0].properties or ()}
    assert properties, "an input should carry AX properties"


async def test_the_full_tree_is_the_thing_the_role_table_is_checked_against(live: Session, site: str) -> None:
    """Accessibility.getFullAXTree. Far too slow for the driver and perfectly
    affordable for a test, which is the only place wirespec uses it: it is what
    ``tests/driver/test_role_table.py`` holds the narrowing selectors to
    (§11.2)."""
    await goto(live, f"{site}/form.html")
    await live.send(accessibility.Enable())
    tree = await live.send(accessibility.GetFullAXTree())
    assert len(tree.nodes) > 5
    assert any(node.role is not None and node.role.value == "RootWebArea" for node in tree.nodes)


async def test_disable_turns_the_domain_back_off(live: Session) -> None:
    """Accessibility.disable."""
    await live.send(accessibility.Enable())
    await live.send(accessibility.Disable())
    await live.send(accessibility.Enable())
