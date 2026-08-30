"""Chrome's own description of the protocol, fetched from Chrome.

The subset's field names are generated: ``rename="camel"`` turns
``document_url`` into ``documentUrl``. That is right for 359 of 362 fields and
silently wrong for the ones CDP spells with an uppercase acronym --
``documentURL``, ``baseURL``, ``includeCommandLineAPI``, ``remoteIPAddress``.

Wrong here is not loud. An outgoing parameter under the wrong name is accepted
and ignored, so the feature just does not happen; an incoming field under the
wrong name is simply never populated. Neither raises. The only defence is to
check the generated names against the protocol itself, which is what this
module makes possible.

The definition is fetched rather than vendored so it always describes the
Chrome actually on the machine. wirespec's own transport is a pipe with no
debugging port, so this is the one place a port is opened -- on loopback, on a
throwaway profile, for one request.
"""

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any

from tests.support import find_chrome

#: Chrome writes the port it actually chose here when asked for port 0.
PORT_FILE = "DevToolsActivePort"


def fetch_protocol(timeout: float = 30.0) -> dict[str, Any] | None:
    """The protocol definition of the Chrome on this machine, or None.

    None rather than an exception: a machine without a usable Chrome should
    skip this check, the same way the rest of the live suite skips.
    """
    binary = find_chrome()
    if binary is None:
        return None

    profile = tempfile.mkdtemp(prefix="wirespec-protocol-")
    process = subprocess.Popen(
        [
            binary,
            "--headless=new",
            # Port 0: let the OS choose, so a developer already running Chrome
            # on 9222 does not have this fail mysteriously.
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        port = _wait_for_port(profile, process, timeout)
        if port is None:
            return None
        url = f"http://127.0.0.1:{port}/json/protocol"
        deadline = time.monotonic() + timeout
        while True:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    return json.loads(response.read())
            except urllib.error.URLError, TimeoutError, ConnectionError:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.1)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _wait_for_port(profile: str, process: subprocess.Popen[bytes], timeout: float) -> int | None:
    path = os.path.join(profile, PORT_FILE)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return None
        try:
            with open(path) as handle:
                first = handle.readline().strip()
            if first:
                return int(first)
        except OSError, ValueError:
            pass
        time.sleep(0.1)
    return None


class Protocol:
    """Lookup over the fetched definition, by the names wirespec uses."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self._domains = {domain["domain"]: domain for domain in raw["domains"]}
        self.version = raw.get("version", {})

    def _member(self, domain: str, kind: str, name: str) -> dict[str, Any] | None:
        for item in self._domains.get(domain, {}).get(kind, []):
            if item["name"] == name:
                return item
        return None

    # Each of these returns the protocol's own property declarations, keyed by
    # wire name -- not just the names, because the declaration also carries
    # ``optional`` and the type, and both of those go wrong the same silent way
    # a name does.

    def command_parameters(self, method: str) -> dict[str, dict[str, Any]] | None:
        domain, _, name = method.partition(".")
        entry = self._member(domain, "commands", name)
        return None if entry is None else {p["name"]: p for p in entry.get("parameters", [])}

    def command_returns(self, method: str) -> dict[str, dict[str, Any]] | None:
        domain, _, name = method.partition(".")
        entry = self._member(domain, "commands", name)
        return None if entry is None else {p["name"]: p for p in entry.get("returns", [])}

    def event_parameters(self, method: str) -> dict[str, dict[str, Any]] | None:
        domain, _, name = method.partition(".")
        entry = self._member(domain, "events", name)
        return None if entry is None else {p["name"]: p for p in entry.get("parameters", [])}

    def type_properties(self, domain: str, name: str) -> dict[str, dict[str, Any]] | None:
        for entry in self._domains.get(domain, {}).get("types", []):
            if entry["id"] == name:
                return {p["name"]: p for p in entry.get("properties", [])}
        return None

    def base_type(self, declaration: dict[str, Any], domain: str) -> str | None:
        """What a property ultimately is -- following ``$ref`` to its target.

        CDP names most of its scalars: ``Network.MonotonicTime`` is a number,
        ``Page.FrameId`` is a string. Comparing against the reference rather
        than the underlying type would compare nothing.
        """
        if "type" in declaration:
            return declaration["type"]
        reference = declaration.get("$ref", "")
        referenced_domain, _, referenced_name = reference.rpartition(".")
        for entry in self._domains.get(referenced_domain or domain, {}).get("types", []):
            if entry["id"] == referenced_name:
                return entry.get("type")
        return None
