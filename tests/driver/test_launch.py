"""Launch: a browser for the duration of a block, and nothing left over.

A launch owns everything it created -- the process, the pipe and the profile
directory -- and the point of the context manager is that leaving the block
disposes of all three.
"""

import os

import pytest

from wirespec.browser import Browser
from wirespec.cdp import browser as browser_domain
from wirespec.errors import LaunchError

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_launch_gives_a_live_chrome_and_takes_it_away_again(chrome_binary: str) -> None:
    """The whole of §6.1's first line, in one test: a context
    manager whose body has a browser and whose exit has none."""
    async with Browser.launch() as browser:
        version = await browser.connection.send(browser_domain.GetVersion())
        assert "Chrome" in version.product
        pid = browser.connection.transport.pid
        assert pid > 0
        profile = browser.user_data_dir
        assert os.path.isdir(profile)
    assert browser.connection.closed
    assert not os.path.exists(profile), "the profile directory outlived the browser that owned it"


async def test_a_machine_with_no_chrome_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Naming what it looked for, because the fix is either a package or an
    environment variable and the message is what decides which."""
    monkeypatch.delenv("E2E_CHROME_BINARY", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(LaunchError) as raised:
        async with Browser.launch():
            pass
    assert "E2E_CHROME_BINARY" in str(raised.value)
    assert "google-chrome" in str(raised.value)
