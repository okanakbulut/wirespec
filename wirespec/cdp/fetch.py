"""``Fetch`` — pausing a request so a spec can answer it instead of the server.

Enabling this domain puts every matching request through a round trip to
Python, so it is enabled lazily and only for the patterns a spec actually
routes (§6.2).
"""

from typing import ClassVar

from wirespec.cdp import network
from wirespec.cdp.base import CDPStruct, Command, Event

__all__ = [
    "AuthChallenge",
    "AuthChallengeResponse",
    "AuthRequired",
    "ContinueRequest",
    "ContinueWithAuth",
    "Disable",
    "Enable",
    "FailRequest",
    "FulfillRequest",
    "GetResponseBody",
    "GetResponseBodyResult",
    "HeaderEntry",
    "RequestPattern",
    "RequestPaused",
]


class HeaderEntry(CDPStruct):
    """``Fetch`` spells headers as a list of pairs, not as an object, because a
    response may carry the same header more than once."""

    name: str
    value: str


class RequestPattern(CDPStruct):
    """``url_pattern`` is a glob -- ``*`` and ``?`` -- not a regular expression.
    ``request_stage`` is ``Request`` or ``Response``."""

    url_pattern: str | None = None
    resource_type: str | None = None
    request_stage: str | None = None


class AuthChallenge(CDPStruct):
    origin: str
    scheme: str
    realm: str
    source: str | None = None


class AuthChallengeResponse(CDPStruct):
    response: str
    username: str | None = None
    password: str | None = None


class Enable(Command[None]):
    __method__: ClassVar[str] = "Fetch.enable"

    patterns: list[RequestPattern] | None = None
    handle_auth_requests: bool = False


class Disable(Command[None]):
    __method__: ClassVar[str] = "Fetch.disable"


class ContinueRequest(Command[None]):
    """Let the request proceed, optionally rewritten. ``post_data`` is base64."""

    __method__: ClassVar[str] = "Fetch.continueRequest"

    request_id: str
    url: str | None = None
    method: str | None = None
    post_data: str | None = None
    headers: list[HeaderEntry] | None = None
    intercept_response: bool = False


class FailRequest(Command[None]):
    """``error_reason`` is a CDP ``ErrorReason`` -- ``Failed``, ``Aborted``,
    ``TimedOut``, ``AccessDenied``, ``ConnectionRefused`` and the rest."""

    __method__: ClassVar[str] = "Fetch.failRequest"

    request_id: str
    error_reason: str


class FulfillRequest(Command[None]):
    """Answer the request from Python. ``body`` is base64; Chrome has no way to
    take raw bytes over the protocol."""

    __method__: ClassVar[str] = "Fetch.fulfillRequest"

    request_id: str
    response_code: int
    response_headers: list[HeaderEntry] | None = None
    body: str | None = None
    response_phrase: str | None = None


class ContinueWithAuth(Command[None]):
    __method__: ClassVar[str] = "Fetch.continueWithAuth"

    request_id: str
    auth_challenge_response: AuthChallengeResponse


class GetResponseBodyResult(CDPStruct):
    body: str
    base64_encoded: bool


class GetResponseBody(Command[GetResponseBodyResult]):
    """Only valid at the ``Response`` stage, which means the pattern that caught
    this request asked for ``request_stage="Response"``."""

    __method__: ClassVar[str] = "Fetch.getResponseBody"

    request_id: str


class RequestPaused(Event):
    """A request Chrome is holding. Every one of these must be answered --
    continued, failed or fulfilled -- or the page waits forever."""

    __method__: ClassVar[str] = "Fetch.requestPaused"

    request_id: str
    request: network.Request
    frame_id: str
    resource_type: str
    response_error_reason: str | None = None
    response_status_code: int | None = None
    response_status_text: str | None = None
    response_headers: list[HeaderEntry] | None = None
    network_id: str | None = None
    redirected_request_id: str | None = None


class AuthRequired(Event):
    __method__: ClassVar[str] = "Fetch.authRequired"

    request_id: str
    request: network.Request
    frame_id: str
    resource_type: str
    auth_challenge: AuthChallenge
