"""Step 6: whether an element is moving, and what it costs to find out.

§8.28. The answer is now pushed rather than read -- Chrome names
the animating element and the driver keeps a set -- with the computed styles
still read for the element and its ancestors, because a class is set
synchronously and an animation starts a frame later.

The tests that prove the *waiting* still happens live in
``tests/driver/test_actions.py``. These are about the machinery underneath it:
what goes into the set, what comes out of it again, and the property that made
the change worth making -- that the check no longer reads the whole document.
"""

import asyncio

import pytest

from wirespec.actionable import _may_move
from wirespec.cdp import css as css_domain
from wirespec.cdp import dom as dom_domain
from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _may_move_for(page: Page, selector: str) -> bool:
    """``_may_move`` as ``actionable`` calls it -- with the element's own
    computed style, which the visibility check has already paid for."""
    node_id = (await page.resolve(page.locator(selector).chain))[0]
    style = await page.session.send(css_domain.GetComputedStyleForNode(node_id=node_id))
    return await _may_move(page, node_id, style)


async def test_a_still_page_costs_no_round_trip_at_all(page: Page) -> None:
    """The property the whole change is for. On a page with nothing animating
    and no transition declared anywhere above the element, step 6 is a dict
    lookup and a walk of a map that was already in memory."""
    await page.goto("/markup.html")
    await page.document()
    sent: list[str] = []
    original = page.session.connection.send

    async def counted(command, **kwargs):
        sent.append(type(command).__method__)
        return await original(command, **kwargs)

    node_id = (await page.resolve(page.locator("#quotes").chain))[0]
    style = await page.session.send(css_domain.GetComputedStyleForNode(node_id=node_id))
    page.session.connection.send = counted  # type: ignore[method-assign]
    try:
        assert await _may_move(page, node_id, style) is False
    finally:
        page.session.connection.send = original  # type: ignore[method-assign]
    assert sent == [], f"step 6 should send nothing on a still page, sent {sent}"


async def test_an_animation_puts_its_element_in_the_moving_set(page: Page) -> None:
    """``Animation.animationStarted`` is what fills the set, and it names the
    element by backend node id."""
    await page.goto("/animating.html")
    node_id = (await page.resolve(page.locator(".sweep").chain))[0]
    backend = (await page.backend_ids([node_id]))[0]
    for _ in range(50):
        if backend in page.moving():
            break
        await asyncio.sleep(0.02)
    assert backend in page.moving()


async def test_a_finished_animation_leaves_the_set_on_its_own(page: Page) -> None:
    """Chrome sends nothing when an animation merely ends, so the expiry is
    computed from the duration it reported at the start (§8.28).
    An entry that outstayed its animation would cost every later action on that
    element an animation frame of settling."""
    await page.goto("/markup.html")
    await page.evaluate(
        """
        () => {
          const box = document.createElement('div');
          box.id = 'brief';
          box.style.cssText = 'width:10px;height:10px;animation: nudge 120ms linear 1';
          const rule = document.createElement('style');
          rule.textContent = '@keyframes nudge { from { left: 0 } to { left: 5px } }';
          document.head.appendChild(rule);
          document.body.appendChild(box);
        }
        """
    )
    for _ in range(50):
        if page.moving():
            break
        await asyncio.sleep(0.02)
    assert page.moving(), "the animation should have been reported"

    await asyncio.sleep(0.4)
    assert page.moving() == set(), "a 120ms animation should not still be in the set"


async def test_an_endless_animation_never_leaves_the_set(page: Page) -> None:
    """``iterations`` is absent for ``infinite``, and absent has to mean for
    ever. Reading it as a missing number would expire a spinner after one round
    and then click something still moving."""
    await page.goto("/animating.html")
    for _ in range(50):
        if page.moving():
            break
        await asyncio.sleep(0.02)
    assert page.moving()
    await asyncio.sleep(1.0)  # several times the 0.6s sweep
    assert page.moving(), "an infinite animation must stay in the set"


async def test_a_cancelled_animation_leaves_the_set(page: Page) -> None:
    """``animationCanceled`` names the animation and not the element, which is
    why the driver keys the set by animation id."""
    await page.goto("/animating.html")
    for _ in range(50):
        if page.moving():
            break
        await asyncio.sleep(0.02)
    assert page.moving()

    await page.evaluate("() => document.querySelector('.sweep').style.animation = 'none'")
    for _ in range(50):
        if not page.moving():
            break
        await asyncio.sleep(0.02)
    assert page.moving() == set()


async def test_a_declared_transition_is_caught_before_it_starts(page: Page) -> None:
    """The frame-wide gap the styles still cover. A class is set synchronously
    and the animation begins at the next frame, so between the two there is a
    moment when the push knows nothing and the computed style already does."""
    await page.goto("/actions.html")
    assert await _may_move_for(page, "#sliding") is False
    await page.evaluate("() => window.__slide()")
    # No sleep on purpose: this is the window in which nothing has been pushed.
    assert await _may_move_for(page, "#sliding") is True


async def test_an_ancestor_being_told_to_move_moves_this_too(page: Page) -> None:
    """§8.7's named case: a dialog sliding into place moves a
    button inside it whose own style says nothing at all."""
    await page.goto("/actions.html")
    assert await _may_move_for(page, "#in-dialog") is False
    await page.evaluate("() => window.__slideDialog()")
    assert await _may_move_for(page, "#in-dialog") is True


async def test_an_animation_elsewhere_does_not_make_this_element_move(page: Page) -> None:
    """The regression §8.7 exists to prevent: a spinner in the
    corner must not cost every action on the page an animation frame."""
    await page.goto("/markup.html")
    await page.document()
    await page.evaluate(
        """
        () => {
          const spinner = document.createElement('div');
          spinner.id = 'spinner';
          spinner.style.cssText = 'width:10px;height:10px;animation: turn 1s linear infinite';
          const rule = document.createElement('style');
          rule.textContent = '@keyframes turn { from { opacity: 0 } to { opacity: 1 } }';
          document.head.appendChild(rule);
          document.body.appendChild(spinner);
        }
        """
    )
    for _ in range(50):
        if page.moving():
            break
        await asyncio.sleep(0.02)
    assert page.moving(), "the spinner should be reported as moving"

    # Created after the document map was built, so the driver cannot place it
    # from the map and has to ask Chrome rather than assume the worst.
    assert await _may_move_for(page, "#quotes") is False
    assert await _may_move_for(page, "#spinner") is True


async def test_a_descendant_animating_counts_as_moving(page: Page) -> None:
    """The subtree is part of §8.7's scope: a child growing changes the box
    this action is about to aim at."""
    await page.goto("/markup.html")
    await page.document()
    await page.evaluate(
        """
        () => {
          const rule = document.createElement('style');
          rule.textContent = '@keyframes grow { from { width: 1px } to { width: 90px } }';
          document.head.appendChild(rule);
          const child = document.createElement('span');
          child.style.cssText = 'display:inline-block;animation: grow 1s linear infinite';
          document.querySelector('#deep').appendChild(child);
        }
        """
    )
    for _ in range(50):
        if page.moving():
            break
        await asyncio.sleep(0.02)
    assert page.moving()
    assert await _may_move_for(page, "#deep") is True
    assert await _may_move_for(page, "#quotes") is False


async def test_the_check_does_not_grow_with_the_document(page: Page) -> None:
    """What the change was for. The previous reading captured a whole-document
    snapshot per action, so this cost rose with the page around the element --
    59.7 ms at fourteen thousand nodes (§8.28). It is now a walk up
    the element's own ancestry, so the traffic depends on depth and not on size.
    """
    await page.goto("/list.html")
    await page.document()
    node_id = (await page.resolve(page.locator("li").chain))[0]
    style = await page.session.send(css_domain.GetComputedStyleForNode(node_id=node_id))
    depth = len(page.ancestor_ids(node_id))

    sent: list[str] = []
    original = page.session.connection.pipeline

    async def counted(commands, **kwargs):
        sent.extend(type(command).__method__ for command in commands)
        return await original(commands, **kwargs)

    page.session.connection.pipeline = counted  # type: ignore[method-assign]
    try:
        await _may_move(page, node_id, style)
    finally:
        page.session.connection.pipeline = original  # type: ignore[method-assign]

    assert sent == ["CSS.getComputedStyleForNode"] * depth
    assert "DOMSnapshot.captureSnapshot" not in sent


async def test_the_document_map_carries_ancestry_in_node_ids(page: Page) -> None:
    """``CSS.getComputedStyleForNode`` takes a node id and nothing else, so the
    map has to hold the ancestry in that currency as well as in backend ids."""
    await page.goto("/markup.html")
    root = await page.document()
    node_id = (await page.session.send(dom_domain.QuerySelectorAll(node_id=root, selector="#deep span"))).node_ids[0]
    ancestors = page.ancestor_ids(node_id)

    assert ancestors, "a nested element should have ancestors"
    assert ancestors[-1] == root, "the walk should end at the document"
    assert len(ancestors) == len(set(ancestors)), "no node may appear twice"
