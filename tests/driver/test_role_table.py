"""§11.2's obligation, discharged.

The role narrowing table (``wirespec/roles.py``) is allowed to be too generous
and is never allowed to be too strict, because a candidate it misses is an
element ``get_by_role`` silently cannot find. This checks that direction, and
only that direction: for each supported role, the CSS candidate set must be a
**superset** of every element ``Accessibility.getFullAXTree`` gives that role.

Superset, not equality. A test demanding equality would fail for the harmless
direction and teach whoever hit it to loosen the table -- which is the one
change that could make it wrong.

``getFullAXTree`` is far too slow for the driver (§3.4) and
perfectly affordable here: it runs once for the whole module.
"""

import asyncio

import pytest
import pytest_asyncio

from wirespec.cdp import accessibility, dom
from wirespec.page import Page
from wirespec.roles import NARROWING, selector_for

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def roles_page(page: Page):
    await page.goto("/roles.html")
    await page.session.send(accessibility.Enable())
    return page


async def backend_ids(page: Page, selector: str) -> set[int]:
    """The candidate set, as backend node ids so it can be compared with the
    accessibility tree's own way of naming nodes."""
    root = await page.document()
    found = await page.session.send(dom.QuerySelectorAll(node_id=root, selector=selector))
    if not found.node_ids:
        return set()
    described = await asyncio.gather(
        *(page.session.send(dom.DescribeNode(node_id=node_id)) for node_id in found.node_ids)
    )
    return {reply.node.backend_node_id for reply in described}


@pytest.mark.parametrize("role", sorted(NARROWING))
async def test_the_narrowing_selector_is_a_superset_of_the_tree(roles_page: Page, role: str) -> None:
    tree = await roles_page.session.send(accessibility.GetFullAXTree())
    wanted = {
        node.backend_dom_node_id
        for node in tree.nodes
        if node.backend_dom_node_id is not None and node.role is not None and node.role.value == role
    }
    assert wanted, f"the fixture holds nothing Chrome calls {role!r} -- the test would pass vacuously"
    candidates = await backend_ids(roles_page, selector_for(role))
    missed = wanted - candidates
    assert not missed, (
        f"{len(missed)} element(s) Chrome calls {role!r} are not in the narrowing set. "
        f"They would be silently unfindable. Widen wirespec/roles.py[{role!r}]."
    )
