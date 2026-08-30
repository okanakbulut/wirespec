"""The same transport, on uvloop.

wirespec must never depend on uvloop, and must always run on it: a host project
that installs uvloop for its own server does not expect its test driver to be
the one thing still on the stdlib loop.

These tests deliberately do not go through pytest-asyncio. They call
``uvloop.run`` themselves, so what is proved is that the code works on a loop
this suite did not create and does not manage -- which is the situation it will
actually be in.
"""

import asyncio
import os

import pytest

from tests.support import chrome, find_chrome
from wirespec.cdp import browser, page, runtime, target
from wirespec.connection import Connection, Session

uvloop = pytest.importorskip("uvloop")

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="no Chrome on this machine")


#: The modules allowed to import pytest: the retention hook (§16.5)
#: and the `pytest-playwright` fixtures (§15.3 stage 8). Both are pytest
#: plugins, both are opt-in, and neither is imported by anything in the package.
PLUGINS = {"pytest_plugin.py", "pytest_playwright.py"}


def _imports_of(path) -> set[str]:
    import ast

    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_wirespec_depends_on_nothing_but_msgspec() -> None:
    """The dependency list has to stay one line long (§10.1).
    uvloop is a thing wirespec runs on, never a thing it needs -- and neither is
    anything else outside the standard library.

    The **pytest plugins** are the exception, and they are excluded by name
    rather than by loosening the rule: pytest is present whenever a pytest
    plugin is imported, and nothing in the package imports them. The test below
    is what holds that second half true.
    """
    import pathlib
    import sys

    allowed = sys.stdlib_module_names | {"msgspec", "wirespec"}
    source = pathlib.Path(__file__).parent.parent / "wirespec"
    imported: set[str] = set()
    for path in source.rglob("*.py"):
        if path.name in PLUGINS:
            assert _imports_of(path) <= allowed | {"pytest"}, path
            continue
        imported |= _imports_of(path)
    assert imported <= allowed, f"wirespec imports {sorted(imported - allowed)}"


def test_wirespec_imports_with_pytest_uninstallable() -> None:
    """The half of the rule above that a source scan cannot check.

    An exception for one module is only safe while nothing reaches it, and
    "nothing reaches it" is a property of the import graph, not of the file. So
    this runs a Python that **cannot** import pytest at all and imports wirespec
    anyway. A stray ``from wirespec.pytest_plugin import ...`` in some other
    module fails here, naming itself, rather than at a user's install.
    """
    import subprocess
    import sys

    guard = (
        "import sys\n"
        # `find_spec`, not `find_module`: the legacy finder protocol was
        # removed in 3.12, so a `find_module` guard is a no-op that makes the
        # test pass for the wrong reason.
        "class Blocked:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'pytest' or name.startswith('pytest.'):\n"
        "            raise ImportError('pytest is not installed here, on purpose')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocked())\n"
        "import wirespec\n"
        "assert 'wirespec.pytest_plugin' not in sys.modules\n"
        "assert 'wirespec.compat.pytest_playwright' not in sys.modules\n"
        "import wirespec.compat.async_api, wirespec.compat.sync_api, wirespec.compat.shim\n"
        "print(wirespec.Browser.__name__)\n"
    )
    done = subprocess.run([sys.executable, "-c", guard], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "Browser"


def test_a_full_round_trip_on_uvloop(tmp_path) -> None:
    async def main() -> tuple[str, int]:
        assert isinstance(asyncio.get_running_loop(), uvloop.Loop)
        async with chrome(str(tmp_path / "profile")) as connection:
            version = await connection.send(browser.GetVersion())
            results = await asyncio.gather(*(connection.send(browser.GetVersion()) for _ in range(20)))
            return version.product, len({result.revision for result in results})

    product, revisions = uvloop.run(main())
    assert product.startswith(("Chrome/", "HeadlessChrome/"))
    assert revisions == 1


def test_sessions_events_and_a_large_payload_on_uvloop(tmp_path, server: str) -> None:
    """Everything the read and write paths do differently under load: flat
    sessions, event routing, a message far larger than one pipe buffer in each
    direction."""

    async def main() -> dict[str, object]:
        async with chrome(str(tmp_path / "profile")) as connection:
            session = await _new_page(connection)
            async with session.expect(page.LoadEventFired, timeout=15.0) as loaded:
                result = await session.send(page.Navigate(url=f"{server}/"))
                assert result.error_text is None
            title = await _evaluate(session, "document.title")

            size = 8 << 20
            down = await _evaluate(session, f"'x'.repeat({size})")

            handle = await session.send(runtime.Evaluate(expression="globalThis"))
            assert handle.result.object_id
            up = await session.send(
                runtime.CallFunctionOn(
                    function_declaration="function (s) { return s.length; }",
                    object_id=handle.result.object_id,
                    arguments=[runtime.CallArgument(value="y" * size)],
                    return_by_value=True,
                )
            )
            return {
                "loaded": loaded.result().timestamp > 0,
                "title": title,
                "down": len(str(down)),
                "up": up.result.value,
            }

    outcome = uvloop.run(main())
    assert outcome == {"loaded": True, "title": "root", "down": 8 << 20, "up": 8 << 20}


def test_chrome_exits_when_the_pipe_closes_on_uvloop(tmp_path) -> None:
    """Reaping is done by hand precisely so no child watcher is involved, which
    is the part of subprocess handling uvloop implements differently."""

    async def main() -> int:
        async with chrome(str(tmp_path / "profile")) as connection:
            await connection.send(browser.GetVersion())
            return connection.transport.pid

    pid = uvloop.run(main())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def _new_page(connection: Connection) -> Session:
    context = await connection.send(target.CreateBrowserContext())
    created = await connection.send(
        target.CreateTarget(url="about:blank", browser_context_id=context.browser_context_id)
    )
    session = await connection.attach(created.target_id)
    await session.send(page.Enable())
    await session.send(runtime.Enable())
    return session


async def _evaluate(session: Session, expression: str) -> object:
    result = await session.send(runtime.Evaluate(expression=expression, return_by_value=True, await_promise=True))
    assert result.exception_details is None, result.exception_details
    return result.result.value
