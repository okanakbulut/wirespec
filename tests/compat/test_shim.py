"""The import shim: a suite that runs with **zero** changed lines.

§15.3 stage 9, and the one it calls a sharp tool. Shadowing another
project's import path is the difference between "a small port" and "no port",
and it is also a way to confuse everyone who later reads the traceback -- so it
ships behind an explicit opt-in and never as a side effect of installing
wirespec.
"""

import subprocess
import sys

import pytest

pytest_plugins = ["pytester"]

INI = "[pytest]\nasyncio_default_fixture_loop_scope = function\n"


def test_installing_makes_the_playwright_imports_resolve() -> None:
    """In a subprocess, because the point is what ``import`` does and this
    process must not be left with a shadowed ``playwright`` for the rest of the
    run."""
    script = (
        "from wirespec.compat import shim; shim.install()\n"
        "from playwright.sync_api import sync_playwright, expect\n"
        "from playwright.async_api import async_playwright, expect as aexpect\n"
        "import playwright\n"
        "print(sync_playwright.__module__, async_playwright.__module__, playwright.__doc__ is not None)\n"
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["wirespec.compat.sync_api", "wirespec.compat.async_api", "True"]


def test_a_whole_suite_runs_with_the_import_line_untouched(pytester: pytest.Pytester, site: str) -> None:
    """The actual goal. Not one line of this test file mentions wirespec."""
    pytester.makeini(INI)
    pytester.makepyfile(
        """
        from playwright.sync_api import expect

        def test_it(page):
            page.goto("/list.html")
            expect(page.get_by_role("listitem")).to_have_count(3)
        """
    )
    result = pytester.runpytest_subprocess(
        "-p", "wirespec.compat.shim", "-p", "wirespec.compat.pytest_playwright", f"--base-url={site}"
    )
    result.assert_outcomes(passed=1)


def test_it_refuses_to_shadow_a_playwright_that_is_already_there() -> None:
    """The confusing half-state this must never reach: some modules the real
    Playwright, some wirespec's, and a traceback that makes sense to nobody."""
    script = (
        "import sys, types\n"
        "sys.modules['playwright'] = types.ModuleType('playwright')\n"
        "from wirespec.compat import shim\n"
        "try:\n"
        "    shim.install()\n"
        "except RuntimeError as exc:\n"
        "    print('refused:', exc)\n"
        "else:\n"
        "    print('SHADOWED ANYWAY')\n"
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("refused:")
    assert "playwright" in done.stdout


def test_importing_wirespec_does_not_shadow_anything() -> None:
    """Opt-in means opt-in. Installing wirespec must not change what
    ``import playwright`` means for an unrelated project on the same machine."""
    script = "import sys, wirespec, wirespec.compat.shim; print('playwright' in sys.modules)\n"
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False"
