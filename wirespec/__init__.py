"""wirespec — a browser driver for Python end-to-end tests.

CDP over a pipe, no Node, no bundled browser.
"""

from wirespec.api import APIRequestContext, APIResponse, RequestError
from wirespec.browser import DEFAULT_LOCALE, DEFAULT_VIEWPORT, Browser, BrowserContext, find_chrome
from wirespec.connection import Connection, Session
from wirespec.dialogs import Dialog
from wirespec.errors import (
    CDPError,
    ConnectionClosedError,
    JavaScriptError,
    LaunchError,
    NavigationError,
    PageClosedError,
    WirespecError,
    WirespecTimeoutError,
)
from wirespec.expect import expect
from wirespec.input import Keyboard, Mouse
from wirespec.locator import FrameLocator, Locator
from wirespec.network import Request, Response, Route
from wirespec.page import Page
from wirespec.pickers import PickerError
from wirespec.recorder import Recorder, Timeline
from wirespec.timeouts import DEFAULT_ACTION_TIMEOUT, DEFAULT_ASSERTION_TIMEOUT
from wirespec.transport import PipeTransport

__all__ = [
    "DEFAULT_ACTION_TIMEOUT",
    "DEFAULT_ASSERTION_TIMEOUT",
    "DEFAULT_LOCALE",
    "DEFAULT_VIEWPORT",
    "APIRequestContext",
    "APIResponse",
    "Browser",
    "BrowserContext",
    "CDPError",
    "Connection",
    "ConnectionClosedError",
    "Dialog",
    "FrameLocator",
    "JavaScriptError",
    "Keyboard",
    "LaunchError",
    "Locator",
    "Mouse",
    "NavigationError",
    "Page",
    "PageClosedError",
    "PickerError",
    "PipeTransport",
    "Recorder",
    "Request",
    "RequestError",
    "Response",
    "Route",
    "Session",
    "Timeline",
    "WirespecError",
    "WirespecTimeoutError",
    "expect",
    "find_chrome",
]
