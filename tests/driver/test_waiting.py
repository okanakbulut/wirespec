"""The wait loop: pushed by Chrome, backstopped by a timer.

§5.1. The two mechanisms exist for different reasons and both are
load-bearing, so both are tested here -- including the case that has no DOM
event at all, which is the one that would otherwise be found by a flaky suite
six months from now.
"""

import time

import pytest

from wirespec.cdp import dom as dom_domain
from wirespec.cdp import runtime as runtime_domain
from wirespec.errors import NODE_GONE, CDPError, WirespecTimeoutError
from wirespec.expect import expect
from wirespec.page import Page
from wirespec.retry import POLL_INTERVAL, poll

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_an_assertion_already_true_costs_one_look(page: Page) -> None:
    """Read first, wait second. A loop that waits before its first look adds
    its interval to every passing assertion in the suite."""
    await page.goto("/waiting.html")
    started = time.perf_counter()
    assert await page.locator("#host").text_content() == ""
    assert (time.perf_counter() - started) < POLL_INTERVAL, "a true assertion must not pay the interval"


async def test_a_dom_change_wakes_the_loop_before_the_interval_would(page: Page) -> None:
    """The push half. The element arrives 20 ms in -- well inside one 100 ms
    interval -- so a timer-only loop would not see it until 100 ms.
    Mutation-to-wakeup was measured at 0.65 ms."""
    await page.goto("/waiting.html")
    await page.evaluate("() => window.__appear(20)")
    started = time.perf_counter()
    assert await page.locator("#appeared").text_content() == "here now"
    elapsed = time.perf_counter() - started
    assert elapsed < POLL_INTERVAL, f"woken after {elapsed * 1000:.1f} ms, which is the timer and not the push"


async def test_a_stylesheet_edit_is_caught_by_the_timer(page: Page) -> None:
    """The backstop half, and the reason it cannot be removed.

    Editing a rule in the stylesheet changes what is on screen and produces **no
    DOM event of any kind**. Media queries, ``:hover`` and scroll-driven changes
    are the same shape. If this ever passes quickly it means Chrome started
    reporting it, not that the timer is unnecessary.
    """
    await page.goto("/waiting.html")
    assert await page.locator("#by-stylesheet").is_visible() is False
    await page.evaluate("() => window.__unhide(20)")

    async def visible() -> bool:
        return await page.locator("#by-stylesheet").is_visible()

    from wirespec.retry import poll

    await poll(page, visible, lambda seen: seen, lambda seen: "never became visible", 3.0)


async def test_a_wait_that_runs_out_quotes_the_last_reading(page: Page) -> None:
    """§5.1: "expected one, saw none" has usually already answered
    the question that "timed out" sends someone to the browser for."""
    await page.goto("/waiting.html")
    with pytest.raises(WirespecTimeoutError) as raised:
        await page.locator("#never-arrives").text_content(timeout=0.4)
    message = str(raised.value)
    assert "exactly one" in message
    assert "none" in message
    assert "0.4s" in message
    assert "#never-arrives" in message, "the message should name the locator that failed"


async def test_wait_for_selector_waits_for_visible_by_default(page: Page) -> None:
    await page.goto("/waiting.html")
    await page.evaluate("() => window.__appear(50)")
    await page.wait_for_selector("#appeared")
    assert await page.locator("#appeared").is_visible() is True


async def test_wait_for_selector_can_wait_only_for_attached(page: Page) -> None:
    """An element that is in the document and not on the screen satisfies
    ``attached`` and not ``visible``."""
    await page.goto("/waiting.html")
    await page.wait_for_selector("#by-stylesheet", state="attached")
    with pytest.raises(WirespecTimeoutError):
        await page.wait_for_selector("#by-stylesheet", state="visible", timeout=0.4)


async def test_wait_for_selector_can_wait_for_hidden(page: Page) -> None:
    """The other direction, and the one a spec reaches for after a delete: the
    row is on screen now and the assertion is about it going away."""
    await page.goto("/waiting.html")
    assert await page.locator("#vanishing").is_visible() is True
    await page.evaluate("() => window.__hide(50)")
    await page.wait_for_selector("#vanishing", state="hidden")
    assert await page.locator("#vanishing").count() == 1


async def test_hidden_is_satisfied_by_an_element_that_was_never_there(page: Page) -> None:
    """Playwright's definition, and the one that makes ``hidden`` usable: an
    element that never rendered is not on the screen. A stricter reading would
    make the assertion depend on how the page chose to remove the row."""
    await page.goto("/waiting.html")
    await page.wait_for_selector("#never-existed", state="hidden", timeout=1)


async def test_wait_for_selector_can_wait_for_detached(page: Page) -> None:
    await page.goto("/waiting.html")
    await page.evaluate("() => window.__remove(50)")
    await page.wait_for_selector("#doomed", state="detached")
    assert await page.locator("#doomed").count() == 0


async def test_detached_is_not_satisfied_by_merely_hidden(page: Page) -> None:
    """The distinction the two states exist for. ``#by-stylesheet`` is in the
    document and off the screen: ``hidden`` now, ``detached`` never."""
    await page.goto("/waiting.html")
    await page.wait_for_selector("#by-stylesheet", state="hidden", timeout=1)
    with pytest.raises(WirespecTimeoutError) as raised:
        await page.wait_for_selector("#by-stylesheet", state="detached", timeout=0.4)
    assert "#by-stylesheet" in str(raised.value)
    assert "attached" in str(raised.value)


async def test_an_unsupported_state_says_so(page: Page) -> None:
    """A state that silently waited for the wrong thing would be worse than one
    that raises, and the message has to list the four that do exist."""
    await page.goto("/waiting.html")
    with pytest.raises(NotImplementedError, match="enabled") as raised:
        await page.wait_for_selector("#host", state="enabled")
    assert "detached" in str(raised.value)


async def test_an_element_replaced_under_the_driver_is_waited_for_not_an_error(page: Page) -> None:
    """A node id resolved a moment ago can be gone by the time Chrome is asked
    about it, and every reader answers that with ``Could not find node with
    given id`` -- a raw ``CDPError`` out of ``CSS.getComputedStyleForNode`` or
    ``Accessibility.getPartialAXTree``, on a page that is merely re-rendering.

    Playwright re-resolves on every attempt and never sees it. wirespec
    resolves inside the read, so the *next* attempt would have been fine -- the
    raise is what stops it getting there (§8.23).
    """
    await page.goto("/waiting.html")
    await page.evaluate("() => window.__churn(12, 40)")
    await expect(page.locator("#churn")).to_be_visible()
    await expect(page.locator("#churn")).to_have_text("settled")


async def test_is_visible_survives_the_node_vanishing_between_its_two_calls(page: Page) -> None:
    """The window the pilot suite found: ``GetBoxModel`` answers, the
    application re-renders, and ``GetComputedStyleForNode`` is then asked about
    a node Chrome has already forgotten -- ``CDPError: Could not find node with
    given id``, out of ``to_be_hidden``, on a page that was merely redrawing
    (§8.23).

    Driven rather than raced: the replacement is triggered *by* the first call,
    so the window is opened on purpose and the test cannot be flaky.
    """
    await page.goto("/waiting.html")
    node_id = (await page.resolve(page.locator("#churn").chain))[0]
    real = page.session

    class Replacing:
        """The real session, with the window held open after one command."""

        def __getattr__(self, name):
            return getattr(real, name)

        async def send(self, command):
            answer = await real.send(command)
            if isinstance(command, dom_domain.GetBoxModel):
                # Through the real session, or this recurses: everything sends.
                await real.send(
                    runtime_domain.Evaluate(
                        expression=(
                            "(() => { const old = document.getElementById('churn');"
                            " old.replaceWith(old.cloneNode(true)); })()"
                        )
                    )
                )
            return answer

    page.session = Replacing()  # type: ignore[assignment]
    try:
        assert await page.is_visible(node_id) is False
    finally:
        page.session = real


async def test_a_read_whose_node_vanished_mid_way_is_read_again(page: Page) -> None:
    """The window, opened deliberately rather than raced for.

    A read resolves a node id and then asks Chrome about it -- two round trips,
    and an application re-rendering between them leaves the second one holding
    an id Chrome has already forgotten. Racing a fixture for that window makes
    a flaky test; opening it on purpose makes a test.
    """
    await page.goto("/waiting.html")
    attempts: list[int] = []

    async def read() -> bool:
        attempts.append(len(attempts))
        node_ids = await page.resolve(page.locator("#churn").chain)
        if len(attempts) == 1:
            await page.evaluate(
                "() => { const old = document.getElementById('churn');"
                " const fresh = old.cloneNode(true); fresh.textContent = 'settled';"
                " old.replaceWith(fresh); }"
            )
        return await page.is_visible(node_ids[0])

    assert await poll(page, read, bool, lambda seen: "was not visible", 3.0) is True
    assert len(attempts) == 2, "the first read should have been abandoned and taken again"


async def test_a_node_that_never_comes_back_still_times_out_by_name(page: Page) -> None:
    """Tolerating a stale node must not turn a genuine failure into a hang: the
    deadline is still the deadline, and the message still names what was
    waited for."""
    await page.goto("/waiting.html")
    with pytest.raises(WirespecTimeoutError) as raised:
        await expect(page.locator("#never-here"), timeout=0.4).to_be_visible()
    assert "#never-here" in str(raised.value)


async def test_a_read_that_finds_its_node_gone_is_taken_again(page: Page) -> None:
    """``poll``'s side of it, for every reader rather than just ``is_visible``.

    ``ax_properties``, ``control_value``, ``computed_style`` and the rest all
    take a node id and can all be asked about one Chrome has just forgotten.
    Inside a polling read that means "resolve again", not "fail".
    """
    attempts: list[int] = []

    async def read() -> bool:
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise CDPError("DOM.describeNode", -32000, NODE_GONE)
        return True

    assert await poll(page, read, bool, lambda seen: "never became true", 3.0) is True
    assert attempts == [0, 1]


async def test_any_other_protocol_error_still_goes_straight_up(page: Page) -> None:
    """One message, matched on purpose. A blanket ``except CDPError`` here
    would turn a genuine protocol failure into a timeout five seconds later,
    naming the wrong thing."""

    async def read() -> bool:
        raise CDPError("DOM.querySelectorAll", -32000, "Invalid parameters")

    with pytest.raises(CDPError, match="Invalid parameters"):
        await poll(page, read, bool, lambda seen: "never", 3.0)


async def test_a_node_that_is_replaced_for_ever_times_out_saying_so(page: Page) -> None:
    """Tolerating it must not become hanging on it, and the message has to say
    what happened -- there is no last reading to quote, because no read ever
    finished."""

    async def read() -> bool:
        raise CDPError("DOM.describeNode", -32000, NODE_GONE)

    with pytest.raises(WirespecTimeoutError, match="kept being replaced"):
        await poll(page, read, bool, lambda seen: "never", 0.4)
