"""A plain static server for the driver suite's fixture pages.

Separate from ``tests/live/server.py``, which exists to misbehave on purpose so
the protocol tests can make ``Fetch.authRequired`` and ``Network.loadingFailed``
happen deliberately. Nothing here needs to misbehave: these pages are about
what the *driver* does, so the server's only job is to hand them over.
"""

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PAGES = Path(__file__).parent / "pages"

#: Every request the server has answered, as "METHOD /path". Read back through
#: `/requests`, because the interesting failure in `page.request` is a request
#: nobody asked for -- an intercept pattern wide enough to catch the scratch
#: page rewrites it, and the caller still gets the right answer
#: (§8.14). Only the server can see that.
LOG: list[str] = []

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    #: HTTP/1.1 so keep-alive is on and a page's sub-resources do not each pay
    #: for a fresh connection.
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        self.do_GET()

    # `page.request` can send any method, so the server has to answer any
    # method; `BaseHTTPRequestHandler` answers 501 to the ones it has no
    # handler for, which looks like a driver failure and is not.
    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_HEAD = do_POST

    def do_GET(self) -> None:
        # Drain the body before doing anything else, always. `protocol_version`
        # is HTTP/1.1, so the connection is reused -- and a request body left
        # unread stays in the socket and is parsed as the start of the *next*
        # request. Measured here: a POST of "dropped" that got a 302 without
        # being read turned the following request line into "droppedGET /...",
        # answered 501, and looked like a driver fault.
        length = int(self.headers.get("Content-Length") or 0)
        self.body = self.rfile.read(length) if length else b""
        path = self.path.split("?", 1)[0]
        LOG.append(f"{self.command} {self.path}")
        if path == "/requests":
            self.respond(200, "application/json", json.dumps(LOG).encode())
            return
        if path == "/api":
            body = self.body
            payload = (
                b'{"from":"the server","method":"'
                + self.command.encode()
                + b'","body":'
                + (body or b'""').replace(b"'", b"")
                + b"}"
                if body
                else b'{"from":"the server","method":"' + self.command.encode() + b'"}'
            )
            self.respond(200, "application/json", payload)
            return
        if path == "/stall":
            self.stall(commit=True)
            return
        if path == "/silent":
            self.stall(commit=False)
            return
        if path == "/slow":
            # Accepted and then held, so a timeout is the only way out. A dead
            # port will not do: Chrome refuses that connection in a millisecond
            # whatever timeout was asked for, so a test using one passes with
            # the units the wrong way round (§15.2).
            time.sleep(8)
            self.respond(200, "text/plain", b"eventually")
            return
        if path == "/echo":
            self.echo()
            return
        if path == "/seed-cookie":
            # Sets a cookie and nothing else, so a test can prove
            # `page.request` sends the context's jar and receives into it.
            self.respond(200, "application/json", b'{"seeded":true}', extra=[("Set-Cookie", "seeded=yes; Path=/")])
            return
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/echo?landed")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/teapot":
            self.respond(418, "text/plain", b"short and stout")
            return
        name = path.lstrip("/") or "index.html"
        candidate = (PAGES / name).resolve()
        # Refuse anything that climbed out of pages/. The server is loopback and
        # short-lived, but a traversal here would silently read the repository.
        if not candidate.is_file() or PAGES.resolve() not in candidate.parents:
            self.respond(404, "text/plain", b"not found")
            return
        self.respond(200, CONTENT_TYPES.get(candidate.suffix, "application/octet-stream"), candidate.read_bytes())

    def stall(self, *, commit: bool) -> None:
        """Accept a request and never finish answering it.

        The one thing here that misbehaves, and it earns its place: a page
        whose load event never fires is the only way to make a *timeout* happen
        deliberately rather than by luck. A refused port races DNS and the
        connection pool; this fails the same way every time.

        ``commit=True`` sends the headers and part of a body, so the document
        commits and ``Page.frameNavigated`` fires -- what a slow application
        looks like. ``commit=False`` sends nothing at all, so the navigation
        never commits. wirespec's timeout message distinguishes the two, and a
        message with an untested branch is a message that is wrong in one of
        them.
        """
        if commit:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", "4096")
            self.end_headers()
            self.wfile.write(b"<!doctype html><title>stall</title>")
            self.wfile.flush()
        # Long enough that no test outlives it, short enough that the server
        # thread is not still holding it at interpreter shutdown.
        time.sleep(10)
        self.close_connection = True

    def echo(self) -> None:
        """Report the request back as JSON: method, headers, cookies, body.

        What `page.request` is tested against. A request that claims to have
        gone through the application's own door has to be checked from the
        server's side, because everything interesting about it -- the method,
        the cookie jar, the headers Chrome adds back -- is invisible from the
        client's (§6.2).
        """
        record = {
            "method": self.command,
            "path": self.path,
            "cookie": self.headers.get("Cookie"),
            "content-type": self.headers.get("Content-Type"),
            "x-custom": self.headers.get("X-Custom"),
            "sec-fetch-dest": self.headers.get("Sec-Fetch-Dest"),
            "body": self.body.decode() or None,
        }
        self.respond(200, "application/json", json.dumps(record).encode())

    def respond(self, status: int, content_type: str, body: bytes, extra: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The tests navigate to the same URL repeatedly; a cached response makes
        # a reload look like a navigation that did not happen.
        self.send_header("Cache-Control", "no-store")
        for name, value in extra or ():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the per-request logging; the tests are noisy enough."""


def serve() -> Iterator[str]:
    """Run the site on a loopback port until the caller is done with it.

    ``ThreadingHTTPServer`` on purpose: a single-threaded ``http.server``
    serialises requests and makes a fixture look 60 ms slower than it is
    (§11).
    """
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
