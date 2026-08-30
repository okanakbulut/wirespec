"""``Animation`` — the push that replaced a whole-document read.

Deciding whether an element is about to move used to cost a
``DOMSnapshot.captureSnapshot`` on every action, which read the entire document
to answer about one element (§8.28). These are the two events that
replaced it, held against real Chrome: what they carry, and -- the part the
driver's design turns on -- what they do *not*.
"""

import asyncio

import pytest

from tests.live.support import evaluate, goto
from wirespec.cdp import animation, dom
from wirespec.connection import Session

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: Long enough for Chrome to start an animation and report it. An animation
#: begins on the next frame, which is the gap ``actionable._declares_motion``
#: exists to cover.
_A_FRAME_OR_TWO = 0.3


async def test_an_animation_names_its_element_and_its_duration(live: Session, site: str) -> None:
    """Animation.animationStarted. The two fields the driver reads:
    ``source.backendNodeId`` says *what* is moving and ``source.duration`` says
    for how long, which together are the whole of the animating set."""
    await live.send(animation.Enable())
    with live.queue(animation.AnimationStarted) as started:
        await goto(live, f"{site}/animating.html")
        event = await asyncio.wait_for(started.get(), timeout=5.0)

    assert event.animation.type == "CSSAnimation"
    assert event.animation.source is not None
    assert event.animation.source.backend_node_id
    assert event.animation.source.duration == pytest.approx(600, abs=1)
    await live.send(animation.Disable())


async def test_an_endless_animation_reports_no_iteration_count(live: Session, site: str) -> None:
    """**The field is absent, not zero, not infinity.**

    ``animating.html`` sweeps ``infinite``, and the count simply does not
    arrive. A driver reading it as a missing number would expire a spinner after
    one round and then click something still moving, so ``None`` has to mean
    "for ever" (``wirespec/cdp/animation.py``).
    """
    await live.send(animation.Enable())
    with live.queue(animation.AnimationStarted) as started:
        await goto(live, f"{site}/animating.html")
        event = await asyncio.wait_for(started.get(), timeout=5.0)

    assert event.animation.source is not None
    assert event.animation.source.iterations is None
    await live.send(animation.Disable())


async def test_a_transition_arrives_the_same_way_as_an_animation(live: Session, site: str) -> None:
    """A CSS transition is an animation to this domain, which is what lets one
    set answer for both. It is reported only when it actually *runs* -- the
    computed-style reading it replaced could not tell a declared transition from
    a running one."""
    await live.send(animation.Enable())
    await goto(live, f"{site}/animating.html")
    with live.queue(animation.AnimationStarted) as started:
        await evaluate(
            live,
            """
            const box = document.createElement('div');
            box.style.cssText = 'width:20px;height:20px;position:absolute;left:0;transition:left 400ms';
            document.body.appendChild(box);
            // A transition needs a *computed* starting value to move away
            // from, and appending alone does not produce one -- the style
            // recalc is batched, both values land in the same frame, and
            // nothing transitions. Reading a layout property forces it.
            box.offsetWidth;
            box.style.left = '200px';
            """,
        )
        while True:
            event = await asyncio.wait_for(started.get(), timeout=5.0)
            if event.animation.type == "CSSTransition":
                break

    assert event.animation.source is not None
    assert event.animation.source.duration == pytest.approx(400, abs=1)
    await live.send(animation.Disable())


async def test_a_cancellation_names_the_animation_and_not_the_element(live: Session, site: str) -> None:
    """Animation.animationCanceled, and the reason the driver keeps a mapping.

    **Only the animation's id is in the message.** Whatever is tracking which
    *elements* are moving therefore has to have kept the id it was told at the
    start, which is why ``Page._animating`` is keyed by id and not by node.

    Nothing is sent when an animation merely finishes -- only when one is cut
    short -- which is why the driver computes an expiry rather than waiting to
    be told.
    """
    await live.send(animation.Enable())
    with live.queue(animation.AnimationStarted) as started:
        await goto(live, f"{site}/animating.html")
        begun = await asyncio.wait_for(started.get(), timeout=5.0)

    with live.queue(animation.AnimationCanceled) as cancelled:
        await evaluate(live, "document.querySelector('.sweep').style.animation = 'none'")
        event = await asyncio.wait_for(cancelled.get(), timeout=5.0)

    assert event.id == begun.animation.id
    # The whole of the message: an id, and nothing that says which element it
    # was moving.
    assert set(animation.AnimationCanceled.__struct_fields__) == {"id"}
    await live.send(animation.Disable())


async def test_nothing_is_reported_once_the_domain_is_disabled(live: Session, site: str) -> None:
    """Animation.disable. The page keeps animating and the driver stops hearing
    about it, which is what makes enabling it a per-page decision."""
    await live.send(animation.Enable())
    await goto(live, f"{site}/animating.html")
    await live.send(animation.Disable())

    with live.queue(animation.AnimationStarted) as started:
        await evaluate(
            live,
            """
            const box = document.createElement('div');
            box.style.cssText = 'width:10px;height:10px;animation: spin 1s linear infinite';
            document.body.appendChild(box);
            """,
        )
        await asyncio.sleep(_A_FRAME_OR_TWO)
        assert started.empty()


async def test_the_element_it_names_can_be_found_again(live: Session, site: str) -> None:
    """A backend node id is only useful if it addresses something. This is the
    step the driver takes when the animating element was created since the
    document map was built (``actionable._holds_any``)."""
    await live.send(animation.Enable())
    with live.queue(animation.AnimationStarted) as started:
        await goto(live, f"{site}/animating.html")
        event = await asyncio.wait_for(started.get(), timeout=5.0)

    assert event.animation.source is not None
    await live.send(dom.Enable())
    await live.send(dom.GetDocument(depth=-1))
    described = await live.send(dom.DescribeNode(backend_node_id=event.animation.source.backend_node_id))
    assert described.node.node_name == "DIV"
    await live.send(animation.Disable())
