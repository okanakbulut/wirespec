"""Discovery: Chrome is found, never downloaded (§7).

No browser is started here -- every one of these is about which path comes
back, which is exactly the part that is wrong on a machine that is not this
one.
"""

import pytest

from wirespec.browser import find_chrome


def test_discovery_finds_a_chrome_on_this_machine() -> None:
    binary = find_chrome()
    if binary is None:
        pytest.skip("no Chrome on this machine")
    assert binary.endswith(("chrome", "chrome-stable", "chromium", "chromium-browser"))


def test_the_binary_override_is_not_fallen_back_from(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A wrong ``E2E_CHROME_BINARY`` must fail, not quietly find another Chrome.

    Falling back would run the suite against a browser nobody asked for and
    report it as a pass, which is the failure mode this project keeps
    returning to.
    """
    monkeypatch.setenv("E2E_CHROME_BINARY", str(tmp_path / "no-such-chrome"))
    assert find_chrome() is None

    real = tmp_path / "my-chrome"
    real.touch()
    monkeypatch.setenv("E2E_CHROME_BINARY", str(real))
    assert find_chrome() == str(real)


def test_a_playwright_cached_chromium_is_the_last_resort(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """§6.1 ends the discovery order with a Playwright download.

    A machine that has only ever run Playwright has a perfectly good Chromium
    and nothing on PATH; refusing to use it would mean telling that developer
    to install a browser they already have.
    """
    monkeypatch.delenv("E2E_CHROME_BINARY", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    cached = tmp_path / "ms-playwright" / "chromium-1200" / "chrome-linux" / "chrome"
    cached.parent.mkdir(parents=True)
    cached.touch(mode=0o755)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "ms-playwright"))
    assert find_chrome() == str(cached)


def test_the_newest_cached_chromium_wins_numerically(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Sorted as strings, ``chromium-1091`` loses to ``chromium-999`` -- and the
    suite then runs against a build two years old without saying so."""
    monkeypatch.delenv("E2E_CHROME_BINARY", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    cache = tmp_path / "ms-playwright"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    for build in ("999", "1091"):
        binary = cache / f"chromium-{build}" / "chrome-linux" / "chrome"
        binary.parent.mkdir(parents=True)
        binary.touch(mode=0o755)
    assert find_chrome() == str(cache / "chromium-1091" / "chrome-linux" / "chrome")
