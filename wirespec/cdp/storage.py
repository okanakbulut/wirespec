"""``Storage`` — the context-level half of cookies.

``Network.setCookies`` is addressed to a *session*, which means a tab. Cookies
belong to the browser context, and the normal shape a spec writes is to seed a
context and then open a page into an application that already believes you are
logged in — so there is no tab to talk through yet. This is the command that
takes a ``browserContextId`` instead (§6.1).
"""

from typing import ClassVar

from wirespec.cdp.base import Command
from wirespec.cdp.network import CookieParam

__all__ = ["SetCookies"]


class SetCookies(Command[None]):
    __method__: ClassVar[str] = "Storage.setCookies"

    cookies: list[CookieParam]
    #: Omitted means the default context, which is the one wirespec never uses.
    browser_context_id: str | None = None
