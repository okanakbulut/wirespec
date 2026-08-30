"""``Browser`` — the two browser-wide commands.

Both are unusual: ``getVersion`` is the only call that needs no session at all,
and ``close`` is the only one whose success means the connection is gone.
"""

import pytest

from wirespec.cdp import browser
from wirespec.connection import Connection

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_get_version_identifies_the_browser(connection: Connection) -> None:
    """Browser.getVersion. Sent with no sessionId, because it is about the
    browser rather than about any page on it."""
    version = await connection.send(browser.GetVersion())
    assert version.product.startswith(("Chrome/", "HeadlessChrome/"))
    assert version.protocol_version.startswith("1.")
    assert version.user_agent
    assert version.js_version


async def test_close_shuts_the_browser_down(throwaway_chrome) -> None:
    """Browser.close, against a Chrome we can afford to lose.

    The orderly shutdown, as opposed to closing the pipe underneath it: Chrome
    acknowledges the command and *then* exits, so the connection is closed by
    the browser rather than by us.
    """
    async with throwaway_chrome("close") as (live_connection, _profile):
        assert (await live_connection.send(browser.GetVersion())).product
        await live_connection.send(browser.Close())
        await live_connection.wait_closed()
        assert live_connection.transport.closed
