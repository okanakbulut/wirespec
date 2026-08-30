"""``Network`` — cookies, and watching requests go by.

Observation only. Changing a request in flight is ``Fetch``'s job, and enabling
``Fetch`` costs every request a round trip, so the two are kept apart and
``Fetch`` is enabled lazily (§6.2).
"""

import asyncio
import base64
import json

import pytest

from tests.live.support import drain_until, evaluate, goto
from wirespec.cdp import network, page, runtime, storage, target
from wirespec.connection import Connection, Session

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_enable_is_what_turns_the_request_events_on(connection: Connection, site: str) -> None:
    """Network.enable / Network.disable.

    Enabling is not free -- it is what makes Chrome report every request -- so
    a driver turns it on for the specs that watch traffic and leaves it off for
    the ones that do not.
    """
    context = await connection.send(target.CreateBrowserContext())
    created = await connection.send(
        target.CreateTarget(url="about:blank", browser_context_id=context.browser_context_id)
    )
    fresh = await connection.attach(created.target_id)
    try:
        await fresh.send(page.Enable())
        await fresh.send(runtime.Enable())

        with fresh.queue(network.ResponseReceived) as before:
            await goto(fresh, f"{site}/index.html")
            await asyncio.sleep(0.3)
        assert before.qsize() == 0, "no request events before Network.enable"

        await fresh.send(network.Enable())
        with fresh.queue(network.ResponseReceived) as during:
            await goto(fresh, f"{site}/xhr.html")
            await drain_until(during, lambda event: event.response.url.endswith("/xhr.html"), timeout=15.0)

        await fresh.send(network.Disable())
        with fresh.queue(network.ResponseReceived) as after:
            await goto(fresh, f"{site}/index.html")
            await asyncio.sleep(0.3)
        assert after.qsize() == 0, "request events should stop after Network.disable"
    finally:
        await connection.send(target.CloseTarget(target_id=created.target_id))
        await connection.send(target.DisposeBrowserContext(browser_context_id=context.browser_context_id))


async def test_a_request_is_reported_before_it_is_sent_and_after_it_lands(live: Session, site: str) -> None:
    """Network.requestWillBeSent, responseReceived and loadingFinished -- the
    three events that bracket one request, sharing a ``request_id``."""
    await live.send(network.Enable())
    await goto(live, f"{site}/xhr.html")

    with (
        live.queue(network.RequestWillBeSent) as sent,
        live.queue(network.ResponseReceived) as received,
        live.queue(network.LoadingFinished) as finished,
    ):
        await evaluate(live, f"window.__get('{site}/api')")

        will_be_sent = await drain_until(sent, lambda event: event.request.url.endswith("/api"), timeout=15.0)
        response = await drain_until(received, lambda event: event.request_id == will_be_sent.request_id, timeout=15.0)
        done = await drain_until(finished, lambda event: event.request_id == will_be_sent.request_id, timeout=15.0)

    assert will_be_sent.request.method == "GET"
    assert will_be_sent.initiator.type in {"script", "other"}
    assert will_be_sent.document_url.endswith("/xhr.html")
    assert response.response.status == 200
    assert response.response.headers.get("Content-Type") == "application/json"
    assert response.response.mime_type == "application/json"
    assert done.encoded_data_length > 0


async def test_a_redirect_is_reported_as_a_second_request(live: Session, site: str) -> None:
    """``redirect_response`` on Network.requestWillBeSent. Chrome reports the
    hop as a *new* requestWillBeSent carrying the response that caused it,
    reusing the same request id -- so a driver that keys on the id alone sees
    one request and misses the chain."""
    await live.send(network.Enable())
    await goto(live, f"{site}/xhr.html")

    with live.queue(network.RequestWillBeSent) as sent:
        await evaluate(live, f"window.__get('{site}/redirect?to=/api')")
        hop = await drain_until(sent, lambda event: event.redirect_response is not None, timeout=15.0)

    assert hop.redirect_response is not None
    assert hop.redirect_response.status == 302
    assert hop.redirect_response.url.endswith("/redirect?to=/api")
    assert hop.request.url.endswith("/api")


async def test_a_request_that_dies_is_reported_as_failed(live: Session, site: str) -> None:
    """Network.loadingFailed.

    The server answers with a Content-Length it then does not honour, and hangs
    up. That is deterministic in a way a refused port is not -- a refused
    connection races DNS and the connection pool, and fails as a different
    error on different machines.
    """
    await live.send(network.Enable())
    await goto(live, f"{site}/xhr.html")

    with live.queue(network.LoadingFailed) as failed:
        result = await evaluate(live, f"window.__try('{site}/never')")
        failure = await drain_until(failed, lambda event: True, timeout=15.0)

    assert result["ok"] is False
    assert failure.error_text
    assert failure.type in {"XHR", "Fetch", "Other"}


async def test_a_cookie_set_over_the_protocol_reaches_the_page(live: Session, site: str) -> None:
    """Network.setCookie. ``omit_defaults=False`` on this struct is what makes
    a cookie asked for as non-secure actually go out as non-secure, rather than
    being dropped from the wire and defaulted by Chrome."""
    await live.send(network.Enable())
    result = await live.send(network.SetCookie(name="wirespec", value="yes", url=site))
    assert result.success
    await goto(live, f"{site}/index.html")
    assert "wirespec=yes" in str(await evaluate(live, "document.cookie"))


async def test_several_cookies_can_be_installed_at_once(live: Session, site: str) -> None:
    """Network.setCookies, and Network.getCookies to read them back.

    ``http_only`` is the one worth checking: it is invisible to
    ``document.cookie`` by definition, so only ``getCookies`` can prove it
    arrived.
    """
    await live.send(network.Enable())
    await live.send(
        network.SetCookies(
            cookies=[
                network.CookieParam(name="first", value="1", url=site),
                network.CookieParam(name="second", value="2", url=site, path="/"),
                network.CookieParam(name="hidden", value="3", url=site, http_only=True),
            ]
        )
    )
    await goto(live, f"{site}/index.html")

    stored = await live.send(network.GetCookies(urls=[site]))
    by_name = {cookie.name: cookie for cookie in stored.cookies}
    assert by_name["first"].value == "1"
    assert by_name["second"].path == "/"
    assert by_name["hidden"].http_only is True
    assert by_name["first"].session is True

    visible = str(await evaluate(live, "document.cookie"))
    assert "first=1" in visible
    assert "hidden" not in visible, "an httpOnly cookie must not be visible to script"


async def test_clearing_cookies_empties_the_jar(live: Session, site: str) -> None:
    """Network.clearBrowserCookies."""
    await live.send(network.Enable())
    await live.send(network.SetCookie(name="doomed", value="1", url=site))
    await goto(live, f"{site}/index.html")
    assert "doomed=1" in str(await evaluate(live, "document.cookie"))

    await live.send(network.ClearBrowserCookies())
    assert (await live.send(network.GetCookies(urls=[site]))).cookies == []
    await goto(live, f"{site}/index.html")
    assert "doomed" not in str(await evaluate(live, "document.cookie"))


async def test_a_context_can_be_seeded_before_it_has_a_page(connection: Connection, site: str) -> None:
    """Storage.setCookies -- the context-level twin of Network.setCookies.

    ``Network.setCookies`` is addressed to a session, which means a tab. This
    one takes a browserContextId, so a context can be given its cookies before
    anything is open in it, which is the shape a spec writes when it wants an
    application to be already logged in on the first paint.
    """
    context = await connection.send(target.CreateBrowserContext())
    try:
        await connection.send(
            storage.SetCookies(
                cookies=[network.CookieParam(name="seeded", value="before-any-page", url=site)],
                browser_context_id=context.browser_context_id,
            )
        )
        created = await connection.send(
            target.CreateTarget(url="about:blank", browser_context_id=context.browser_context_id)
        )
        fresh = await connection.attach(created.target_id)
        await fresh.send(page.Enable())
        await fresh.send(runtime.Enable())
        await goto(fresh, f"{site}/index.html")
        assert "seeded=before-any-page" in str(await evaluate(fresh, "document.cookie"))
    finally:
        await connection.send(target.DisposeBrowserContext(browser_context_id=context.browser_context_id))


async def test_extra_headers_are_added_to_every_request(live: Session, site: str) -> None:
    """Network.setExtraHTTPHeaders. The header is proved by the server echoing
    it back, not merely by Chrome accepting the command."""
    await live.send(network.Enable())
    await live.send(network.SetExtraHTTPHeaders(headers={"X-Wirespec": "live-suite"}))
    try:
        await goto(live, f"{site}/xhr.html")
        answered = await evaluate(live, f"window.__get('{site}/echo')")
        assert json.loads(answered["body"])["headers"]["x-wirespec"] == "live-suite"
    finally:
        await live.send(network.SetExtraHTTPHeaders(headers={}))


async def test_a_response_body_can_be_read_back(live: Session, site: str) -> None:
    """Network.getResponseBody.

    Only valid while Chrome still holds the body, which is until the page
    navigates away -- so it is read when ``loadingFinished`` says so, and not
    later.
    """
    await live.send(network.Enable())
    await goto(live, f"{site}/xhr.html")

    with (
        live.queue(network.ResponseReceived) as received,
        live.queue(network.LoadingFinished) as finished,
    ):
        await evaluate(live, f"window.__get('{site}/big?bytes=4096')")
        response = await drain_until(received, lambda event: "/big" in event.response.url, timeout=15.0)
        await drain_until(finished, lambda event: event.request_id == response.request_id, timeout=15.0)

    body = await live.send(network.GetResponseBody(request_id=response.request_id))
    assert body.base64_encoded is False
    assert body.body == "x" * 4096


async def test_a_binary_response_comes_back_base64(live: Session, site: str) -> None:
    """``base64_encoded`` is not decoration: the flag is the only way to know
    whether ``body`` is text or bytes-in-a-string."""
    await live.send(network.Enable())
    await goto(live, f"{site}/xhr.html")

    with (
        live.queue(network.ResponseReceived) as received,
        live.queue(network.LoadingFinished) as finished,
    ):
        await evaluate(live, f"window.__get('{site}/binary')")
        response = await drain_until(received, lambda event: "/binary" in event.response.url, timeout=15.0)
        await drain_until(finished, lambda event: event.request_id == response.request_id, timeout=15.0)

    body = await live.send(network.GetResponseBody(request_id=response.request_id))
    assert body.base64_encoded is True
    assert base64.b64decode(body.body) == bytes(range(256))
