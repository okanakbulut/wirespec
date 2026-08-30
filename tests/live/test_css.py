"""``CSS`` — computed style, which is how wirespec decides what is visible.

§5.2 defines visible as a non-empty box model *and* not
``visibility: hidden``, and deliberately not opacity and not in-viewport. These
tests are about the protocol call behind the second half of that.
"""

import pytest

from tests.live.support import goto
from wirespec.cdp import css, dom
from wirespec.connection import Session
from wirespec.errors import CDPError

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def style_of(live: Session, selector: str) -> dict[str, str]:
    root = (await live.send(dom.GetDocument(depth=-1))).root.node_id
    found = await live.send(dom.QuerySelectorAll(node_id=root, selector=selector))
    assert found.node_ids, f"the fixture has no {selector}"
    reply = await live.send(css.GetComputedStyleForNode(node_id=found.node_ids[0]))
    return {item.name: item.value for item in reply.computed_style}


async def test_computed_style_answers_the_visibility_question(live: Session, site: str) -> None:
    """CSS.enable / getComputedStyleForNode. The whole property set comes back
    -- CDP has no way to ask for less -- which is why this is the most
    expensive per-node call in the resolution set at 0.721 ms."""
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    await live.send(css.Enable())
    computed = await style_of(live, "#box")
    assert computed["visibility"] == "visible"
    assert len(computed) > 100, "CDP returns every property, not a chosen subset"


async def test_the_style_is_the_computed_one_and_not_the_declared_one(live: Session, site: str) -> None:
    """A width declared in one unit comes back resolved, which is what makes it
    comparable at all."""
    await goto(live, f"{site}/form.html")
    await live.send(dom.Enable())
    await live.send(css.Enable())
    computed = await style_of(live, "#box")
    assert computed["width"].endswith("px")


async def test_the_dom_agent_has_to_be_enabled_first(live: Session) -> None:
    """CSS.disable, and the ordering it exposes.

    ``CSS.enable`` fails outright with "DOM agent needs to be enabled first" --
    a protocol error rather than a quiet no-op, which is the good kind. It is
    why ``BrowserContext.new_page`` enables DOM before CSS rather than in
    whatever order the imports happen to be in.
    """
    # DOM is not enabled on a fresh `live` session, and disabling what was
    # never enabled is itself an error -- so enable it, then take both down in
    # the order that leaves CSS with nothing under it.
    await live.send(dom.Enable())
    await live.send(css.Enable())
    await live.send(css.Disable())
    await live.send(dom.Disable())
    with pytest.raises(CDPError, match="DOM agent"):
        await live.send(css.Enable())
    await live.send(dom.Enable())
    await live.send(css.Enable())
