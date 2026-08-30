"""``pytest-playwright``'s fixtures, over wirespec.

§15.3 stage 8, whose bar is that **a suite using them needs no
``conftest.py`` change**: the same fixture names, the same scopes, the same
override points, and the same command-line options.

Sync, like `pytest-playwright`'s own. wirespec is async to the bottom and stays
that way ([§3.1](#31-one-half)); ``page`` here is a
``wirespec.compat.sync_api`` page because that is what a `pytest-playwright`
suite is written against.

**Opt-in**, for the same reason ``wirespec.pytest_plugin`` is: installing
wirespec must not add ``--browser`` to every pytest run on the machine::

    # conftest.py -- or -p on the command line
    pytest_plugins = ["wirespec.compat.pytest_playwright"]

Options `pytest-playwright` has that wirespec does not **refuse when set**
rather than being accepted and ignored. ``--video=on`` quietly ignored is a
suite that believes it has videos of its failures; wirespec has that capability
under its own name ([§16](#16-the-failure-artefact)) and not this flag.

``--output`` is the one accepted and unused, and deliberately so: everything
that would write into it -- tracing, video, screenshots -- refuses first, so
there is nothing to put there and refusing the directory as well would break a
suite that passes it out of habit. wirespec's own artefacts go where
``--wirespec-artefacts`` says ([§16.5](#165-retention-and-the-pytest-plugin)).
"""

from collections.abc import Iterator
from typing import Any

import pytest

from wirespec.compat.sync_api import sync_playwright

__all__ = ["base_url", "browser", "browser_name", "context", "page", "playwright"]

#: Options `pytest-playwright` accepts that wirespec has no answer for. The
#: value each one has when nobody asked, so that "not passed" and "passed the
#: default" are the same thing and only a real request refuses.
UNSUPPORTED: dict[str, Any] = {
    "--video": "off",
    "--tracing": "off",
    "--screenshot": "off",
    "--slowmo": 0,
    "--device": None,
    "--browser-channel": None,
}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("playwright", "Playwright compatibility (wirespec)")
    group.addoption("--browser", action="append", default=[], help="chromium only; anything else refuses")
    group.addoption("--headed", action="store_true", default=False, help="run with a visible browser window")
    group.addoption("--base-url", default=None, help="base URL for page.goto('/path')")
    group.addoption("--browser-channel", default=None, help="not supported by wirespec")
    group.addoption("--device", default=None, help="not supported by wirespec")
    group.addoption("--slowmo", type=int, default=0, help="not supported by wirespec")
    group.addoption("--output", default="test-results", help="where artefacts go")
    group.addoption("--tracing", default="off", help="not supported by wirespec; see §16")
    group.addoption("--video", default="off", help="not supported by wirespec; see §16")
    group.addoption("--screenshot", default="off", help="not supported by wirespec; see §16")


@pytest.fixture(scope="session")
def browser_name(pytestconfig: pytest.Config) -> str:
    """Always ``chromium``, and it **refuses** rather than reporting chromium
    for a run that asked for something else.

    §15.1: Firefox and WebKit are permanently out, because wirespec
    is CDP and CDP is Chromium. Running Chromium under another name would make a
    cross-browser suite report three passes for one browser, which is the single
    outcome worse than not supporting them.
    """
    asked = pytestconfig.getoption("--browser") or ["chromium"]
    other = [name for name in asked if name != "chromium"]
    if other:
        raise NotImplementedError(
            f"--browser={other[0]} is not available: wirespec speaks CDP, and CDP is Chromium "
            f"(§15.1). This is permanent, not a gap."
        )
    return "chromium"


@pytest.fixture(scope="session")
def is_chromium(browser_name: str) -> bool:
    return browser_name == "chromium"


@pytest.fixture(scope="session")
def is_firefox() -> bool:
    return False


@pytest.fixture(scope="session")
def is_webkit() -> bool:
    return False


@pytest.fixture(scope="session")
def _refuse_unsupported(pytestconfig: pytest.Config) -> None:
    """Every option that would otherwise be accepted and ignored.

    Checked once, at the first fixture that needs a browser, so a run that asked
    for something wirespec cannot do stops before it produces results nobody
    should trust (§15.4).
    """
    for flag, default in UNSUPPORTED.items():
        if pytestconfig.getoption(flag.lstrip("-").replace("-", "_")) != default:
            raise NotImplementedError(
                f"{flag} is not supported by wirespec. It refuses rather than being ignored, which "
                f"would leave a suite believing it had them. wirespec keeps the *capability* -- the "
                f"screen and the network on one timeline -- under its own name (§16)."
            )


@pytest.fixture(scope="session")
def base_url(pytestconfig: pytest.Config) -> str | None:
    return pytestconfig.getoption("--base-url")


@pytest.fixture(scope="session")
def playwright(_refuse_unsupported: None) -> Iterator[Any]:
    with sync_playwright() as started:
        yield started


@pytest.fixture(scope="session")
def browser_type(playwright: Any, browser_name: str) -> Any:
    return getattr(playwright, browser_name)


@pytest.fixture(scope="session")
def browser_type_launch_args(pytestconfig: pytest.Config) -> dict[str, Any]:
    """The documented override point for launch arguments. Returned as a plain
    dict so a suite can spread it: ``{**browser_type_launch_args, ...}``."""
    return {"headless": not pytestconfig.getoption("--headed")}


@pytest.fixture(scope="session")
def browser(browser_type: Any, browser_type_launch_args: dict[str, Any]) -> Iterator[Any]:
    launched = browser_type.launch(**browser_type_launch_args)
    try:
        yield launched
    finally:
        launched.close()


@pytest.fixture(scope="session")
def browser_context_args(base_url: str | None) -> dict[str, Any]:
    """The documented override point for context arguments — viewport, locale,
    and anything else a suite wants on every context."""
    return {"base_url": base_url} if base_url else {}


@pytest.fixture
def context(browser: Any, browser_context_args: dict[str, Any]) -> Iterator[Any]:
    """A context per test, which is `pytest-playwright`'s isolation guarantee
    and the reason ``page`` is function-scoped while ``browser`` is not."""
    made = browser.new_context(**browser_context_args)
    try:
        yield made
    finally:
        made.close()


@pytest.fixture
def page(context: Any) -> Any:
    return context.new_page()
