"""An element that is not the target, animating across the point being pressed.

The second half of what the pilot suite's flaking radio turned out to be, and the half
§8.31 did not reach. The dock's floating pill is
``position: fixed`` with ``transition: right 0.2s``, so opening the Inspector
sends it sliding *through* the space the radio occupies. It is neither an
ancestor nor a descendant of the radio, so step 6's "is it moving" check --
correctly scoped to the element and its own chain (§8.7) -- never looks at it.

That leaves the hit test deciding reachability from one instant, and the answer
does not keep. The pill is clear when it is asked and over the point by the time
the press goes out, so ``click()`` reports success and a different element is
pressed. In the pilot application that surfaced as ``to_be_checked`` waiting out its whole five
seconds, one line after the click that had "worked".

The rule that is missing: a **finite** animation that is still running can change
what is reachable, and an action must let it finish first. Finite is the
load-bearing word. Waiting for animations in general is what §8.7 removed and it
must stay removed -- a spinner never ends, and a driver that waits for one waits
for ever. ``test_an_endless_animation_elsewhere_is_not_waited_for`` is that half.

``sweeping.html`` drives the pill from the test rather than from a gesture, so
the sweep starts at a moment these tests choose.
"""

import asyncio

import pytest

from wirespec import Page, expect

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: How long the pill takes to cross, and how close to the target's centre its
#: leading edge is allowed to get before the action starts. Measured rather than
#: guessed: at 8 px the pill is clear when the hit test asks and over the point
#: when the press lands, 8 times out of 8. Either side of that the race does not
#: happen and the test would pass while proving nothing -- nearer, the hit test
#: already sees the pill and the retry loop rescues it; further, the press is out
#: before the pill arrives. Hence a *position* rather than a delay: the window is
#: a few milliseconds wide and no test can time the driver's internals from
#: outside.
SWEEP = 0.3
MARGIN_PX = 8


async def test_the_pill_crosses_the_target_and_ends_clear(page: Page) -> None:
    """The fixture's premise, in three positions.

    Every test below is about what is at the target's centre, so a layout change
    could quietly make them all vacuous. This is what says so first. The *ends
    clear* half matters as much as the crossing: it is what makes waiting the
    right answer rather than a slower way to refuse.
    """
    await page.goto("/sweeping.html")
    at_centre = """() => {
        const box = document.getElementById('target').getBoundingClientRect();
        const found = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
        return found ? found.id : null;
    }"""

    assert await page.evaluate(at_centre) == "target", "starts reachable"

    await page.evaluate("() => window.sweep('over', 0.01)")
    await asyncio.sleep(0.2)
    assert await page.evaluate(at_centre) == "pill", "the pill's path covers it"

    await page.evaluate("() => window.sweep('past', 0.01)")
    await asyncio.sleep(0.2)
    assert await page.evaluate(at_centre) == "target", "and it is reachable again once past"


async def test_a_press_is_not_stolen_by_something_sweeping_over_the_target(page: Page) -> None:
    """The reproduction: clear at the check, covered at the press.

    The pilot application's shape, and the dangerous one. Everything the driver
    can see says the target is reachable, and it stops being true in the gap between the hit test
    and the dispatch. Nothing raises. ``click()`` returns having pressed the
    wrong element, and the spec is left to fail on its next line.

    Asserted on both counters, because "the target did not get it" and "something
    else did" are different bugs and only one of them is this one.
    """
    await page.goto("/sweeping.html")

    await page.evaluate(f"() => window.sweepUntilNear({MARGIN_PX}, {SWEEP})")
    await page.get_by_role("button", name="Per tenant").click()

    state = await page.evaluate("() => window.state()")
    assert state["swallowed"] == 0, "the press landed on the pill passing over the target"
    assert state["clicks"] == 1, "and the target never got it"


async def test_an_action_survives_a_cover_that_is_animating_away(page: Page) -> None:
    """The safe half of the same race, kept as a regression guard.

    The pill is over the point when the hit test asks and leaving. This already
    works, and it is worth knowing *why*: the action refuses, and the caller's
    retry loop asks again once the pill has gone. Nothing here should get slower
    or start refusing when the rule above is added.
    """
    await page.goto("/sweeping.html")

    await page.evaluate("() => window.sweep('over', 0.01)")
    await asyncio.sleep(0.2)
    await page.evaluate(f"() => window.sweep('away', {SWEEP})")

    await page.get_by_role("button", name="Per tenant").click()
    await expect(page.get_by_text("clicked 1")).to_be_visible()


async def test_an_endless_animation_elsewhere_is_not_waited_for(page: Page) -> None:
    """§8.7, which the fix must not undo.

    A page with one indefinite animation anywhere must not make every action
    wait. An infinite animation never finishes, so a rule that waited for *any*
    running animation would stall every click on such a page until its deadline
    -- the exact regression §8.7 exists to prevent, reintroduced by the cure.

    The pill is parked clear of the target here and pulsing for ever. The click
    has nothing to wait for and must not wait.
    """
    await page.goto("/sweeping.html")
    await page.evaluate(
        """() => {
            const pill = document.getElementById('pill');
            pill.animate(
                [{ transform: 'translateY(0)' }, { transform: 'translateY(6px)' }],
                { duration: 300, iterations: Infinity },
            );
        }"""
    )

    started = asyncio.get_running_loop().time()
    await page.get_by_role("button", name="Per tenant").click()
    elapsed = asyncio.get_running_loop().time() - started

    await expect(page.get_by_text("clicked 1")).to_be_visible()
    assert elapsed < SWEEP, f"took {elapsed:.2f}s, so it waited for an animation that never ends"


async def test_a_mover_the_point_is_out_of_reach_of_is_not_waited_for(page: Page) -> None:
    """The rule is about the point, not about the page.

    ``#far`` is a genuine mover -- ``left``, on screen, running for two whole
    seconds -- and it is 640 px from the target's centre and travelling away.
    Nothing it does can put it over the point in the few milliseconds between
    the hit test and the press, so the action has nothing to wait for.

    Waiting for it anyway is what the first draft of this rule did, and the cost
    was not marginal: an action paid for every finite animation *anywhere*, so a
    toast fading in a corner or a dock sliding along the bottom taxed every click
    on the page for its whole duration (§8.32).

    The premise is asserted rather than assumed. A transition starts on the frame
    after the style is set (§8.28), so a test that clicked immediately would find
    nothing registered and pass without the rule ever being consulted.
    """
    await page.goto("/sweeping.html")
    await page.evaluate("() => window.sweepFar(2)")
    await asyncio.sleep(0.05)
    assert page.finite_movers(), "nothing was registered as moving, so this would pass vacuously"

    started = asyncio.get_running_loop().time()
    await page.get_by_role("button", name="Per tenant").click()
    elapsed = asyncio.get_running_loop().time() - started

    await expect(page.get_by_text("clicked 1")).to_be_visible()
    # Well clear of `_MOTION_CAP`, which is the least the page-wide rule could
    # have cost: it slept out the cap and came back for the rest of the two
    # seconds. Measured at 0.52s against this fixture before the narrowing.
    assert elapsed < 0.3, f"took {elapsed:.2f}s, so it waited for something that could not reach the point"


async def test_a_colour_transition_elsewhere_is_not_waited_for(page: Page) -> None:
    """The other half of the cost, and the reason the rule reads the property.

    An application transitions colours constantly -- Tailwind puts
    ``transition-colors`` on every button, input and row -- and none of it can
    change which element is at a point. Waiting out each one cost the pilot suite
    37% of its wall clock, which is the §8.7 regression wearing a different hat.

    ``Animation.name`` is the property for a ``CSSTransition``, so this costs a
    set lookup and no round trip.
    """
    await page.goto("/sweeping.html")
    await page.evaluate(
        """() => {
            const pill = document.getElementById('pill');
            pill.style.transition = 'background-color 2s linear';
            void pill.offsetWidth;
            pill.style.backgroundColor = 'rgb(0, 0, 255)';
        }"""
    )

    started = asyncio.get_running_loop().time()
    await page.get_by_role("button", name="Per tenant").click()
    elapsed = asyncio.get_running_loop().time() - started

    await expect(page.get_by_text("clicked 1")).to_be_visible()
    assert elapsed < 0.5, f"took {elapsed:.2f}s, so it waited out a transition that only repaints"
