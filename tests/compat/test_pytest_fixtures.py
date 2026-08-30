"""The `pytest-playwright` fixtures, over wirespec.

§15.3 stage 8, whose bar is precise: **a suite using them needs no
``conftest.py`` change.** So these run a real pytest over a real test file that
does nothing but ask for ``page`` -- which is what a `pytest-playwright` suite
does -- and check it passes for the right reason.
"""

import pytest

pytest_plugins = ["pytester"]

INI = "[pytest]\nasyncio_default_fixture_loop_scope = function\n"


def run(pytester: pytest.Pytester, body: str, *options: str) -> pytest.RunResult:
    pytester.makeini(INI)
    pytester.makepyfile(body)
    return pytester.runpytest("-p", "wirespec.compat.pytest_playwright", *options)


def test_a_suite_that_only_asks_for_page_works(pytester: pytest.Pytester, site: str) -> None:
    result = run(
        pytester,
        """
        def test_it(page):
            page.goto("/index.html")
            assert page.locator("h1").is_visible()
        """,
        f"--base-url={site}",
    )
    result.assert_outcomes(passed=1)


def test_base_url_makes_relative_paths_work(pytester: pytest.Pytester, site: str) -> None:
    """The option a `pytest-playwright` suite passes on the command line, not in
    a fixture -- so it has to be an option here too, spelled the same."""
    result = run(
        pytester,
        """
        def test_it(page, base_url):
            assert base_url
            page.goto("/list.html")
            assert "list.html" in page.url
        """,
        f"--base-url={site}",
    )
    result.assert_outcomes(passed=1)


def test_browser_context_args_can_be_overridden(pytester: pytest.Pytester, site: str) -> None:
    """The documented way a `pytest-playwright` suite changes the viewport or
    the locale: override the fixture, not the plugin."""
    result = run(
        pytester,
        """
        import pytest

        @pytest.fixture(scope="session")
        def browser_context_args(browser_context_args):
            return {**browser_context_args, "viewport": {"width": 640, "height": 480}}

        def test_it(page):
            assert page.evaluate("() => window.innerWidth") == 640
        """,
        f"--base-url={site}",
    )
    result.assert_outcomes(passed=1)


def test_browser_name_and_the_is_flags_are_there(pytester: pytest.Pytester, site: str) -> None:
    """Suites branch on these. ``is_chromium`` being true and the other two
    false is the honest answer, not a stub."""
    result = run(
        pytester,
        """
        def test_it(browser_name, is_chromium, is_firefox, is_webkit):
            assert browser_name == "chromium"
            assert is_chromium and not is_firefox and not is_webkit
        """,
        f"--base-url={site}",
    )
    result.assert_outcomes(passed=1)


def test_another_browser_is_refused_by_name(pytester: pytest.Pytester, site: str) -> None:
    """§15.1: permanent. Running Chromium and reporting it as
    Firefox is the one outcome worse than not supporting Firefox."""
    result = run(
        pytester,
        """
        def test_it(page):
            page.goto("/index.html")
        """,
        f"--base-url={site}",
        "--browser=firefox",
    )
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*firefox*"])


def test_an_unsupported_option_refuses_rather_than_being_ignored(pytester: pytest.Pytester, site: str) -> None:
    """``--video=on`` accepted and ignored is a suite that believes it has
    videos (§15.4). wirespec has the *capability* under its own
    name (§16) and not this flag."""
    result = run(
        pytester,
        """
        def test_it(page):
            page.goto("/index.html")
        """,
        f"--base-url={site}",
        "--video=on",
    )
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*--video*"])


def test_each_test_gets_a_context_of_its_own(pytester: pytest.Pytester, site: str) -> None:
    """`pytest-playwright`'s isolation guarantee, and the reason its `page` is
    function-scoped while its `browser` is not."""
    result = run(
        pytester,
        """
        def test_one(page):
            page.goto("/index.html")
            page.evaluate("() => { window.__marker = 'one'; }")
            assert page.evaluate("() => window.__marker") == "one"

        def test_two(page):
            page.goto("/index.html")
            assert page.evaluate("() => window.__marker") is None
        """,
        f"--base-url={site}",
    )
    result.assert_outcomes(passed=2)
