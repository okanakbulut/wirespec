"""Fixtures: one Chrome for the run, and a static server to point it at."""

import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import pytest_asyncio

from tests.support import chrome, find_chrome
from wirespec.cdp import page as page_domain
from wirespec.cdp import runtime, target
from wirespec.connection import Connection, Session

PAGES: dict[str, tuple[str, bytes]] = {
    "/": ("text/html", b"<!doctype html><title>root</title><h1>root</h1>"),
    "/input.html": (
        "text/html",
        b"""<!doctype html><title>input</title>
        <style>body{margin:0}</style>
        <button id="b" style="position:absolute;left:20px;top:30px;width:120px;height:40px">Press</button>
        <input id="f" style="position:absolute;left:20px;top:100px;width:200px">
        <script>
          window.__clicks = 0;
          window.__keys = [];
          document.getElementById('b').addEventListener('click', () => { window.__clicks++; });
          document.getElementById('f').addEventListener('keydown', e => { window.__keys.push(e.key); });
        </script>""",
    ),
    "/api": ("application/json", b'{"from":"the server"}'),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        content_type, body = PAGES.get(self.path, ("text/plain", b"not found"))
        self.send_response(200 if self.path in PAGES else 404)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the per-request logging; the tests are noisy enough.

        The signature matches ``BaseHTTPRequestHandler``'s exactly -- a
        narrower one type-checks as an incompatible override.
        """


@pytest.fixture(scope="session")
def server() -> Iterator[str]:
    """A *threading* server on purpose: a single-threaded http.server serialises
    requests and makes a fixture look 60 ms slower than it is (§11)."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def chrome_binary() -> str:
    binary = find_chrome()
    if binary is None:
        pytest.skip("no Chrome on this machine")
    return binary


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def connection(chrome_binary: str, tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Connection]:
    profile = tmp_path_factory.mktemp("chrome-profile")
    async with chrome(str(profile)) as live:
        yield live


@pytest_asyncio.fixture(loop_scope="session")
async def session(connection: Connection) -> AsyncIterator[Session]:
    """A fresh page in its own browser context, disposed afterwards.

    Real contexts, not tabs that happen to share nothing: separate cookies,
    storage and cache (§6.1).
    """
    context = await connection.send(target.CreateBrowserContext())
    created = await connection.send(
        target.CreateTarget(url="about:blank", browser_context_id=context.browser_context_id)
    )
    attached = await connection.attach(created.target_id)
    await attached.send(page_domain.Enable())
    await attached.send(runtime.Enable())
    try:
        yield attached
    finally:
        await connection.send(target.CloseTarget(target_id=created.target_id))
        await connection.send(target.DisposeBrowserContext(browser_context_id=context.browser_context_id))
