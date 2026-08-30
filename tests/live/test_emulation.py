"""``Emulation`` — pinning the viewport a test measures against.

A headless window's default size is not a promise, and every box the DOM domain
reports is relative to it. A spec that asserts on geometry without setting this
is asserting about whatever Chrome felt like today (§8.9).
"""

import pytest

from tests.live.support import evaluate, eventually, goto
from wirespec.cdp import emulation, runtime
from wirespec.cdp import page as page_domain
from wirespec.cdp import target as target_domain
from wirespec.connection import Connection, Session

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_device_metrics_set_the_viewport(live: Session, site: str) -> None:
    """Emulation.setDeviceMetricsOverride."""
    await goto(live, f"{site}/index.html")
    await live.send(emulation.SetDeviceMetricsOverride(width=800, height=600, device_scale_factor=1.0, mobile=False))
    try:
        assert await evaluate(live, "innerWidth") == 800
        assert await evaluate(live, "innerHeight") == 600
        assert await evaluate(live, "devicePixelRatio") == 1
    finally:
        await live.send(emulation.ClearDeviceMetricsOverride())


async def test_a_scale_factor_changes_the_pixel_ratio_not_the_layout(live: Session, site: str) -> None:
    """A retina viewport is the same CSS size at twice the density -- the
    distinction that decides whether a screenshot comparison is portable."""
    await goto(live, f"{site}/index.html")
    await live.send(emulation.SetDeviceMetricsOverride(width=400, height=300, device_scale_factor=2.0, mobile=False))
    try:
        assert await evaluate(live, "innerWidth") == 400
        assert await evaluate(live, "devicePixelRatio") == 2
    finally:
        await live.send(emulation.ClearDeviceMetricsOverride())


async def test_mobile_emulation_drives_the_page_breakpoints(live: Session, site: str) -> None:
    """``mobile=True`` is not cosmetic: it enables the layout viewport, which is
    what makes a responsive breakpoint behave the way a phone does.

    The fixture page carries ``<meta name="viewport">`` on purpose. Without it
    Chrome lays a mobile-emulated page out at the 980px desktop fallback, so
    ``innerWidth`` comes back as 981 and every breakpoint assertion is about a
    viewport no phone ever had.
    """
    await goto(live, f"{site}/responsive.html")
    assert "desktop" in str(await evaluate(live, "window.__layout()"))

    await live.send(emulation.SetDeviceMetricsOverride(width=375, height=667, device_scale_factor=3.0, mobile=True))
    try:
        assert await evaluate(live, "innerWidth") == 375
        assert await evaluate(live, "devicePixelRatio") == 3
        assert await evaluate(live, "matchMedia('(max-width: 480px)').matches") is True
        assert "phone" in str(await evaluate(live, "window.__layout()"))
    finally:
        await live.send(emulation.ClearDeviceMetricsOverride())


async def test_clearing_the_override_restores_the_window(live: Session, site: str) -> None:
    """Emulation.clearDeviceMetricsOverride. Overrides outlive a navigation, so
    a test that forgets this one hands its viewport to the next test."""
    await goto(live, f"{site}/index.html")
    baseline = await evaluate(live, "innerWidth")
    assert baseline != 321, "pick a width the headless window does not already have"

    await live.send(emulation.SetDeviceMetricsOverride(width=321, height=241, device_scale_factor=1.0, mobile=False))
    assert await evaluate(live, "innerWidth") == 321

    await live.send(emulation.ClearDeviceMetricsOverride())
    # Polled, not asserted outright: Chrome acknowledges the clear before the
    # renderer has resized, so an immediate read still sees the override.
    await eventually(lambda: evaluate(live, "innerWidth"), baseline)


async def test_focus_emulation_makes_a_background_page_believe_it_is_in_front(
    connection: Connection, site: str
) -> None:
    """Emulation.setFocusEmulationEnabled.

    A tab that is not the frontmost one is not focused, and Chrome does more
    with that than change ``document.hasFocus()``: it stops producing frames for
    it, and an ``Input.dispatchMouseEvent`` move is not answered until one is
    produced -- measured at exactly 5.001 s, which is a watchdog rather than a
    wait (§8.26). This is the switch that turns that off, so it is
    on for every page wirespec opens.

    Two tabs of its own, rather than the ``live`` fixture's one: the whole
    behaviour only exists when a *second* tab is in front, so the test has to
    own both.
    """
    context = await connection.send(target_domain.CreateBrowserContext())
    behind = await connection.send(
        target_domain.CreateTarget(url="about:blank", browser_context_id=context.browser_context_id)
    )
    session = await connection.attach(behind.target_id)
    await session.send(page_domain.Enable())
    await session.send(runtime.Enable())
    front = await connection.send(
        target_domain.CreateTarget(url="about:blank", browser_context_id=context.browser_context_id)
    )
    try:
        await goto(session, f"{site}/index.html")
        await eventually(lambda: evaluate(session, "document.hasFocus()"), False)
        await session.send(emulation.SetFocusEmulationEnabled(enabled=True))
        assert await evaluate(session, "document.hasFocus()") is True
    finally:
        for target_id in (front.target_id, behind.target_id):
            await connection.send(target_domain.CloseTarget(target_id=target_id))
        await connection.send(target_domain.DisposeBrowserContext(browser_context_id=context.browser_context_id))
