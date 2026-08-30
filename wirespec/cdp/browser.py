"""``Browser`` — the handful of commands that address the browser itself."""

from typing import ClassVar

from wirespec.cdp.base import CDPStruct, Command

__all__ = ["Close", "GetVersion", "GetVersionResult"]


class GetVersionResult(CDPStruct):
    protocol_version: str
    product: str
    revision: str
    user_agent: str
    js_version: str


class GetVersion(Command[GetVersionResult]):
    """The cheapest complete round trip there is, which is why it is what the
    transport is proved with (§13)."""

    __method__: ClassVar[str] = "Browser.getVersion"


class Close(Command[None]):
    """Ask Chrome to shut down. Rarely needed: closing the pipe does it, and
    does it whether or not Chrome is still answering commands."""

    __method__: ClassVar[str] = "Browser.close"
