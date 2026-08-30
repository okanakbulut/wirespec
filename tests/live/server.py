"""A static server with the hostile routes the protocol subset needs.

Most live tests want a plain page off disk. A handful want the server to
misbehave in a specific, *deterministic* way -- to stall, to demand
credentials, to redirect, to hang up mid-body -- because that is the only way
to make ``Page.stopLoading``, ``Fetch.authRequired`` and
``Network.loadingFailed`` happen on purpose rather than by luck.

Routes live here rather than in a fixture so a failing test can be reproduced
by hand: run this module and point a browser at it.
"""

import base64
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

PAGES = Path(__file__).parent / "pages"

#: What ``/auth`` accepts. Used by the Fetch.continueWithAuth test.
AUTH_USER = "wirespec"
AUTH_PASSWORD = "open-sesame"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    #: HTTP/1.1 so keep-alive is on and a page's sub-resources do not each pay
    #: for a fresh connection; the tests measure event ordering, not TCP.
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.route()

    def do_POST(self) -> None:
        self.route()

    def route(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        handler = getattr(self, f"route_{path.strip('/').replace('/', '_').replace('-', '_')}", None)
        if handler is not None:
            handler(query)
            return
        self.serve_file(path)

    # -- dynamic routes ---------------------------------------------------

    def route_api(self, query: dict[str, list[str]]) -> None:
        """Plain JSON, the thing every Network and Fetch test asks for."""
        self.respond(200, "application/json", json.dumps({"from": "the server", "path": self.path}).encode())

    def route_echo(self, query: dict[str, list[str]]) -> None:
        """Reflects the request headers, so an override can be proved to have
        reached the server rather than merely to have been accepted."""
        headers = {name.lower(): value for name, value in self.headers.items()}
        self.respond(200, "application/json", json.dumps({"headers": headers}).encode())

    def route_slow(self, query: dict[str, list[str]]) -> None:
        """Stalls before answering. ``Page.stopLoading`` needs a navigation that
        is still in flight when the command arrives."""
        delay = float(query.get("ms", ["1000"])[0]) / 1000
        time.sleep(delay)
        self.respond(200, "text/html; charset=utf-8", b"<!doctype html><title>slow</title><h1>slow</h1>")

    def route_auth(self, query: dict[str, list[str]]) -> None:
        """401 with a Basic challenge, unless the right credentials arrive.

        ``Fetch.authRequired`` fires only for a real challenge, so this route
        has to mean it.
        """
        expected = "Basic " + base64.b64encode(f"{AUTH_USER}:{AUTH_PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") == expected:
            self.respond(200, "application/json", b'{"authenticated":true}')
            return
        body = b"unauthorized"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="wirespec"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def route_redirect(self, query: dict[str, list[str]]) -> None:
        """302 to ``to``. Gives ``Network.requestWillBeSent`` a redirect chain,
        which is the only way ``redirect_response`` is ever populated."""
        target = query.get("to", ["/api"])[0]
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def route_big(self, query: dict[str, list[str]]) -> None:
        """A body large enough that ``getResponseBody`` is worth calling."""
        size = int(query.get("bytes", ["65536"])[0])
        self.respond(200, "text/plain", b"x" * size)

    def route_binary(self, query: dict[str, list[str]]) -> None:
        """Non-UTF-8 bytes, so ``base64Encoded`` on a response body comes back
        true and the flag is proved to mean something."""
        self.respond(200, "application/octet-stream", bytes(range(256)))

    def route_never(self, query: dict[str, list[str]]) -> None:
        """Accepts the request and never answers it, then drops the connection.

        This is what makes ``Network.loadingFailed`` deterministic: a refused
        port races DNS and the connection pool, but a server that hangs up
        mid-flight fails exactly once, every time.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "1000")
        self.end_headers()
        self.wfile.write(b"partial")
        self.wfile.flush()
        self.close_connection = True

    # -- static -----------------------------------------------------------

    def serve_file(self, path: str) -> None:
        name = path.lstrip("/") or "index.html"
        candidate = (PAGES / name).resolve()
        # Refuse anything that climbed out of pages/. The server is loopback and
        # short-lived, but a traversal here would silently read the repository.
        if not candidate.is_file() or PAGES.resolve() not in candidate.parents:
            self.respond(404, "text/plain", b"not found")
            return
        content_type = CONTENT_TYPES.get(candidate.suffix, "application/octet-stream")
        self.respond(200, content_type, candidate.read_bytes())

    def respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The tests navigate to the same URL repeatedly and expect the network
        # events each time; a cached response produces none.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the per-request logging; the tests are noisy enough."""


def serve() -> Iterator[str]:
    """Run the site on a loopback port until the caller is done with it."""
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


if __name__ == "__main__":
    for url in serve():
        print(f"serving {PAGES} at {url}")
        input("enter to stop\n")
