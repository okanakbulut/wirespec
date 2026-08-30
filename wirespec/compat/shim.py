"""Making ``import playwright`` mean wirespec — behind an explicit opt-in.

§15.3 stage 9, which is deliberately last and deliberately
optional. It is what turns "a small port" into "no port": the suite's import
lines stay exactly as they are, and nothing in it mentions wirespec.

It is also a sharp tool, and the spec says why: shadowing another project's
import path is a good way to confuse everyone who later reads the traceback. So
**nothing happens on import.** ``install()`` has to be called, or the module has
to be named as a pytest plugin::

    pytest -p wirespec.compat.shim -p wirespec.compat.pytest_playwright

which changes no line of the suite, because a command-line flag is not one.

The one thing it will not do is shadow a Playwright that is already loaded.
Half the modules resolving to one implementation and half to the other is a
state nobody can debug, and it is reachable by accident -- a plugin that
imported Playwright before this ran. It refuses instead.
"""

import sys
import types
from typing import Any

from wirespec.compat import async_api, sync_api

__all__ = ["install", "installed"]

#: What a ``playwright`` package would contain, as far as §15.1
#: defines it. ``playwright._impl`` and friends are absent on purpose: a suite
#: reaching into those is asserting on Playwright's internals, which §15.1 puts
#: permanently out of scope.
_MODULES = {"playwright.sync_api": sync_api, "playwright.async_api": async_api}

_ROOT_DOC = """wirespec, wearing Playwright's name.

Installed by ``wirespec.compat.shim`` at the caller's explicit request. This is
**not** Playwright: it is ``wirespec.compat``, which implements Playwright's API
over a CDP driver with no Node and no bundled browser. Chromium only, and the
boundaries are §15.1.

If this is a surprise -- if you are reading it out of a traceback and did not
expect it -- something passed ``-p wirespec.compat.shim`` or called
``wirespec.compat.shim.install()``.
"""


def installed() -> bool:
    """Whether the shim is what ``playwright`` currently resolves to."""
    root = sys.modules.get("playwright")
    return root is not None and getattr(root, "__wirespec_shim__", False)


def install() -> None:
    """Point ``playwright`` and its two API modules at the compat surface.

    Idempotent. Raises if a *real* Playwright is already imported, because the
    alternative is a process where ``playwright.sync_api`` is wirespec and
    ``playwright._impl`` is not.
    """
    if installed():
        return
    if "playwright" in sys.modules:
        raise RuntimeError(
            "playwright is already imported, so wirespec.compat.shim will not shadow it: half the "
            "modules would resolve to one implementation and half to the other. Install the shim "
            "before anything imports playwright, or uninstall Playwright (§15.3)."
        )

    root = types.ModuleType("playwright")
    root.__doc__ = _ROOT_DOC
    # A real package, so `import playwright.sync_api` works rather than only
    # `from playwright import sync_api`. Empty `__path__` because there is
    # nothing on disk to find and nothing else should be findable under it.
    root.__path__ = []  # type: ignore[attr-defined]
    root.__wirespec_shim__ = True  # type: ignore[attr-defined]
    sys.modules["playwright"] = root

    for name, module in _MODULES.items():
        sys.modules[name] = module
        setattr(root, name.rsplit(".", 1)[1], module)


def uninstall() -> None:
    """Undo :func:`install`. For tests, and for a REPL that changed its mind."""
    if not installed():
        return
    for name in (*_MODULES, "playwright"):
        sys.modules.pop(name, None)


def pytest_load_initial_conftests(early_config: Any, parser: Any, args: Any) -> None:
    """Installed as early as pytest will let this run, which is **here**.

    Not ``pytest_configure``, and the difference is a whole class of suite:
    pytest loads the rootdir ``conftest.py`` *before* configure runs, so a
    conftest whose first line is ``from playwright.sync_api import Page`` --
    which is how most Playwright suites are written -- would fail to import
    before the shim had been given a chance to exist. This hook fires before
    that load. Found by the differential suite, whose own conftest is written
    exactly that way (§15.4).
    """
    install()


def pytest_configure(config: Any) -> None:
    """A second chance, for a pytest that reached configure without the hook
    above -- and idempotent, so it costs nothing when it did not."""
    install()
