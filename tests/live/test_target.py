"""``Target`` — contexts, pages, flat sessions, and the events that track them.

Every one of these is about a page other than the one you are on, which is the
part of a driver that has no equivalent in a single-page script.
"""

import pytest

from tests.live.support import drain_until, evaluate, goto, kill_renderers
from wirespec.cdp import page, runtime, target
from wirespec.connection import Connection

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_browser_context_isolates_storage(connection: Connection, site: str) -> None:
    """Target.createBrowserContext / createTarget / closeTarget /
    disposeBrowserContext -- the whole lifecycle, which is what the ``live``
    fixture does for every other test in this suite.

    A context is not a tab: localStorage written in one is invisible in the
    other, which is the property that lets specs run in parallel.
    """
    contexts = [await connection.send(target.CreateBrowserContext()) for _ in range(2)]
    sessions = []
    targets = []
    try:
        for context in contexts:
            created = await connection.send(
                target.CreateTarget(url="about:blank", browser_context_id=context.browser_context_id)
            )
            targets.append(created.target_id)
            attached = await connection.attach(created.target_id)
            await attached.send(page.Enable())
            await attached.send(runtime.Enable())
            sessions.append(attached)

        for index, session in enumerate(sessions):
            await goto(session, f"{site}/index.html")
            await evaluate(session, f"localStorage.setItem('who', 'context{index}')")

        assert await evaluate(sessions[0], "localStorage.getItem('who')") == "context0"
        assert await evaluate(sessions[1], "localStorage.getItem('who')") == "context1"
    finally:
        for target_id in targets:
            await connection.send(target.CloseTarget(target_id=target_id))
        for context in contexts:
            await connection.send(target.DisposeBrowserContext(browser_context_id=context.browser_context_id))


async def test_close_target_reports_success(connection: Connection) -> None:
    """Target.closeTarget answers with a boolean rather than an error, so a
    close that did nothing looks exactly like one that worked unless it is
    checked."""
    created = await connection.send(target.CreateTarget(url="about:blank"))
    result = await connection.send(target.CloseTarget(target_id=created.target_id))
    assert result.success


async def test_get_targets_lists_the_pages_that_exist(connection: Connection) -> None:
    """Target.getTargets. The browser's own view, independent of what we are
    attached to."""
    created = await connection.send(target.CreateTarget(url="about:blank"))
    try:
        result = await connection.send(target.GetTargets())
        by_id = {info.target_id: info for info in result.target_infos}
        assert created.target_id in by_id
        assert by_id[created.target_id].type == "page"
    finally:
        await connection.send(target.CloseTarget(target_id=created.target_id))


async def test_attach_and_detach_bracket_a_session(connection: Connection) -> None:
    """Target.attachToTarget / detachFromTarget, and the two events that
    mirror them.

    ``flatten=True`` is not optional here: a non-flat session wraps protocol
    inside protocol and nothing in wirespec knows how to route it.
    """
    created = await connection.send(target.CreateTarget(url="about:blank"))
    try:
        async with connection.expect(target.AttachedToTarget, timeout=10.0) as attached_event:
            result = await connection.send(target.AttachToTarget(target_id=created.target_id))
        assert result.session_id
        assert attached_event.result().session_id == result.session_id
        assert attached_event.result().target_info.target_id == created.target_id

        async with connection.expect(
            target.DetachedFromTarget, lambda event: event.session_id == result.session_id, timeout=10.0
        ) as detached_event:
            await connection.send(target.DetachFromTarget(session_id=result.session_id))
        assert detached_event.result().session_id == result.session_id
    finally:
        await connection.send(target.CloseTarget(target_id=created.target_id))


async def test_discovery_announces_pages_the_driver_did_not_open(connection: Connection, site: str) -> None:
    """Target.setDiscoverTargets, and targetCreated / targetInfoChanged /
    targetDestroyed.

    Without discovery, a page opened by ``window.open`` is invisible: the
    driver finds out only when something it was waiting for never happens.

    The queues are opened before the click rather than awaited after it. A
    popup announces itself and then changes title faster than a test can
    subscribe in between, so ``expect`` would lose the race about half the time.
    """
    await connection.send(target.SetDiscoverTargets(discover=True))
    opener_target = await connection.send(target.CreateTarget(url="about:blank"))
    opener = await connection.attach(opener_target.target_id)
    popup_id: str | None = None
    try:
        await opener.send(page.Enable())
        await opener.send(runtime.Enable())
        await goto(opener, f"{site}/popup.html")

        with (
            connection.queue(target.TargetCreated) as created,
            connection.queue(target.TargetInfoChanged) as changed,
            connection.queue(target.TargetDestroyed) as destroyed,
        ):
            # user_gesture, or the popup blocker eats it and nothing is created.
            await evaluate(opener, "document.getElementById('open').click()", user_gesture=True)

            born = await drain_until(
                created,
                lambda event: (
                    event.target_info.type == "page" and event.target_info.target_id != opener_target.target_id
                ),
                timeout=15.0,
            )
            popup_id = born.target_info.target_id
            assert born.target_info.opener_id == opener_target.target_id

            # The popup is announced before it has loaded anything, so its URL
            # arrives as a later change rather than as a field on the first event.
            settled = await drain_until(
                changed,
                lambda event: event.target_info.target_id == popup_id and event.target_info.url.endswith("/index.html"),
                timeout=15.0,
            )
            assert settled.target_info.type == "page"

            assert popup_id is not None
            await connection.send(target.CloseTarget(target_id=popup_id))
            gone = await drain_until(destroyed, lambda event: event.target_id == popup_id, timeout=15.0)
            assert gone.target_id == popup_id
            popup_id = None
    finally:
        if popup_id is not None:
            await connection.send(target.CloseTarget(target_id=popup_id))
        await connection.send(target.CloseTarget(target_id=opener_target.target_id))
        await connection.send(target.SetDiscoverTargets(discover=False))


async def test_a_crashed_renderer_is_announced(throwaway_chrome, site: str) -> None:
    """Target.targetCrashed, against a Chrome we can afford to lose.

    Without discovery on, a crashed page is simply a page that stopped
    answering: every command against it hangs until its timeout, and the
    failure is reported against whatever was asked last rather than against the
    crash.
    """
    async with throwaway_chrome("crash") as (live_connection, profile):
        await live_connection.send(target.SetDiscoverTargets(discover=True))
        created = await live_connection.send(target.CreateTarget(url="about:blank"))
        victim = await live_connection.attach(created.target_id)
        await victim.send(page.Enable())
        await victim.send(runtime.Enable())
        await goto(victim, f"{site}/index.html")

        with live_connection.queue(target.TargetCrashed) as crashes:
            assert kill_renderers(profile), "no renderer found to kill"
            crashed = await drain_until(crashes, lambda event: event.target_id == created.target_id, timeout=20.0)
        assert crashed.status == "killed"
        assert crashed.error_code != 0
        # The browser itself is untouched -- that is the whole difference
        # between a crashed tab and a crashed browser.
        assert not live_connection.closed
