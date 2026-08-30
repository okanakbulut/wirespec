"""``Network`` — cookies, and watching requests go by.

Observation only. Changing a request in flight is ``Fetch``'s job, and enabling
``Fetch`` costs every request a round trip, so the two are kept apart and
``Fetch`` is enabled lazily (§6.2).
"""

from typing import Any, ClassVar

from msgspec import field

from wirespec.cdp.base import CDPStruct, Command, Event, Headers

__all__ = [
    "ClearBrowserCookies",
    "Cookie",
    "CookieParam",
    "Disable",
    "Enable",
    "GetCookies",
    "GetCookiesResult",
    "GetResponseBody",
    "GetResponseBodyResult",
    "Initiator",
    "LoadingFailed",
    "LoadingFinished",
    "Request",
    "RequestWillBeSent",
    "Response",
    "ResponseReceived",
    "SetCookie",
    "SetCookieResult",
    "SetCookies",
    "SetExtraHTTPHeaders",
]


class CookieParam(CDPStruct):
    """One cookie to install. Either ``url`` or ``domain`` must be given, or
    Chrome has nowhere to put it."""

    name: str
    value: str
    url: str | None = None
    domain: str | None = None
    path: str | None = None
    secure: bool | None = None
    http_only: bool | None = None
    same_site: str | None = None
    expires: float | None = None


class Cookie(CDPStruct):
    name: str
    value: str
    domain: str
    path: str
    expires: float
    size: int
    http_only: bool
    secure: bool
    session: bool
    same_site: str | None = None


class Request(CDPStruct):
    url: str
    method: str
    headers: Headers
    post_data: str | None = None
    has_post_data: bool = False
    url_fragment: str | None = None
    referrer_policy: str | None = None


class Response(CDPStruct):
    url: str
    status: int
    status_text: str
    headers: Headers
    mime_type: str = ""
    encoded_data_length: float = 0.0
    protocol: str | None = None
    #: "remoteIPAddress", not "remoteIpAddress". Optional, so the camel rename
    #: costs no error -- just an address that is always None.
    remote_ip_address: str | None = field(default=None, name="remoteIPAddress")
    remote_port: int | None = None
    from_disk_cache: bool = False
    from_service_worker: bool = False


class Initiator(CDPStruct):
    type: str
    url: str | None = None
    line_number: float | None = None
    column_number: float | None = None
    request_id: str | None = None


class Enable(Command[None]):
    __method__: ClassVar[str] = "Network.enable"

    max_total_buffer_size: int | None = None
    max_resource_buffer_size: int | None = None
    max_post_data_size: int | None = None


class Disable(Command[None]):
    __method__: ClassVar[str] = "Network.disable"


class SetCookieResult(CDPStruct):
    success: bool = True


class SetCookie(Command[SetCookieResult], omit_defaults=False):
    """``omit_defaults=False`` so a cookie asked for as non-secure, non-httpOnly
    or session-scoped is sent that way rather than left to Chrome's defaults."""

    __method__: ClassVar[str] = "Network.setCookie"

    name: str
    value: str
    url: str
    path: str = "/"
    secure: bool = False
    http_only: bool = False


class SetCookies(Command[None]):
    __method__: ClassVar[str] = "Network.setCookies"

    cookies: list[CookieParam]


class GetCookiesResult(CDPStruct):
    cookies: list[Cookie]


class GetCookies(Command[GetCookiesResult]):
    __method__: ClassVar[str] = "Network.getCookies"

    urls: list[str] | None = None


class ClearBrowserCookies(Command[None]):
    __method__: ClassVar[str] = "Network.clearBrowserCookies"


class GetResponseBodyResult(CDPStruct):
    body: str
    base64_encoded: bool


class GetResponseBody(Command[GetResponseBodyResult]):
    """Only valid while Chrome still holds the body, which is until the page
    navigates away. Read it when ``loadingFinished`` says so, not later."""

    __method__: ClassVar[str] = "Network.getResponseBody"

    request_id: str


class SetExtraHTTPHeaders(Command[None]):
    __method__: ClassVar[str] = "Network.setExtraHTTPHeaders"

    headers: Headers


class RequestWillBeSent(Event):
    __method__: ClassVar[str] = "Network.requestWillBeSent"

    request_id: str
    loader_id: str
    # CDP spells this "documentURL", with the acronym uppercase, which
    # rename="camel" renders as "documentUrl" and Chrome never sends. Left to
    # the automatic rename the whole event fails to decode -- and because a
    # decode failure on the read path is reported rather than raised, the
    # symptom is not an error but an event that silently never arrives.
    document_url: str = field(name="documentURL")
    request: Request
    timestamp: float
    wall_time: float
    initiator: Initiator
    redirect_response: Response | None = None
    type: str | None = None
    frame_id: str | None = None
    has_user_gesture: bool = False


class ResponseReceived(Event):
    __method__: ClassVar[str] = "Network.responseReceived"

    request_id: str
    loader_id: str
    timestamp: float
    type: str
    response: Response
    has_extra_info: bool = False
    frame_id: str | None = None


class LoadingFinished(Event):
    __method__: ClassVar[str] = "Network.loadingFinished"

    request_id: str
    timestamp: float
    encoded_data_length: float


class LoadingFailed(Event):
    __method__: ClassVar[str] = "Network.loadingFailed"

    request_id: str
    timestamp: float
    type: str
    error_text: str
    canceled: bool = False
    blocked_reason: str | None = None
    cors_error_status: dict[str, Any] | None = None
