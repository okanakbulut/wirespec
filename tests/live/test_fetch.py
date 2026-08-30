"""``Fetch`` — pausing a request so a spec can answer it instead of the server.

Enabling this domain puts every matching request through a round trip to
Python, so it is enabled lazily and only for the patterns a spec actually
routes (§6.2).

Every test here answers the pause from the test coroutine rather than from an
event handler. Handlers run on the read path, where an await would deadlock the
very connection the answer has to go out on -- so the request is fired without
being awaited, the pause is taken off a queue, answered, and only then is the
page's promise awaited.
"""

import asyncio
import base64
import json

import pytest

from tests.live.server import AUTH_PASSWORD, AUTH_USER
from tests.live.support import drain_until, evaluate, goto
from wirespec.cdp import fetch, network
from wirespec.connection import Session

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_request_can_be_answered_from_python(live: Session, site: str) -> None:
    """Fetch.enable, Fetch.requestPaused and Fetch.fulfillRequest.

    The server is never reached: the body comes from Python, which is what
    makes a spec able to stage a backend it does not have.
    """
    await goto(live, f"{site}/xhr.html")
    await live.send(fetch.Enable(patterns=[fetch.RequestPattern(url_pattern="*/api*")]))
    try:
        with live.queue(fetch.RequestPaused) as paused:
            requesting = asyncio.ensure_future(evaluate(live, f"window.__get('{site}/api')"))
            held = await drain_until(paused, lambda event: event.request.url.endswith("/api"), timeout=15.0)

            assert held.request.method == "GET"
            assert held.resource_type in {"Fetch", "XHR"}
            assert held.frame_id
            assert held.response_status_code is None, "paused at the request stage, so there is no response yet"

            await live.send(
                fetch.FulfillRequest(
                    request_id=held.request_id,
                    response_code=200,
                    response_headers=[fetch.HeaderEntry(name="Content-Type", value="application/json")],
                    body=base64.b64encode(b'{"from":"python"}').decode(),
                )
            )
            answered = await requesting

        assert answered["status"] == 200
        assert answered["body"] == '{"from":"python"}'
        assert await evaluate(live, "document.getElementById('result').textContent") == '{"from":"python"}'
    finally:
        await live.send(fetch.Disable())


async def test_a_fulfilled_response_can_carry_a_custom_status_and_phrase(live: Session, site: str) -> None:
    """``response_phrase`` and a non-200 code, which is how a spec stages the
    error branches a real backend rarely produces on demand."""
    await goto(live, f"{site}/xhr.html")
    await live.send(fetch.Enable(patterns=[fetch.RequestPattern(url_pattern="*/api*")]))
    try:
        with live.queue(fetch.RequestPaused) as paused:
            requesting = asyncio.ensure_future(evaluate(live, f"window.__get('{site}/api')"))
            held = await drain_until(paused, lambda event: True, timeout=15.0)
            await live.send(
                fetch.FulfillRequest(
                    request_id=held.request_id,
                    response_code=503,
                    response_phrase="Service Unavailable",
                    response_headers=[fetch.HeaderEntry(name="Content-Type", value="text/plain")],
                    body=base64.b64encode(b"the backend is having a day").decode(),
                )
            )
            answered = await requesting
        assert answered["status"] == 503
        assert answered["body"] == "the backend is having a day"
    finally:
        await live.send(fetch.Disable())


async def test_a_request_can_be_let_through_untouched(live: Session, site: str) -> None:
    """Fetch.continueRequest with nothing overridden. The server answers, and
    the only evidence Fetch was involved is that the pause happened at all."""
    await goto(live, f"{site}/xhr.html")
    await live.send(fetch.Enable(patterns=[fetch.RequestPattern(url_pattern="*/api*")]))
    try:
        with live.queue(fetch.RequestPaused) as paused:
            requesting = asyncio.ensure_future(evaluate(live, f"window.__get('{site}/api')"))
            held = await drain_until(paused, lambda event: True, timeout=15.0)
            await live.send(fetch.ContinueRequest(request_id=held.request_id))
            answered = await requesting
        assert json.loads(answered["body"])["from"] == "the server"
    finally:
        await live.send(fetch.Disable())


async def test_a_request_can_be_rewritten_on_its_way_out(live: Session, site: str) -> None:
    """Fetch.continueRequest, rewriting the URL, the method and the headers.

    The redirect a spec wants without a redirect: the page asked for one thing
    and the server was asked for another, with the page none the wiser.
    """
    await goto(live, f"{site}/xhr.html")
    await live.send(fetch.Enable(patterns=[fetch.RequestPattern(url_pattern="*/api*")]))
    try:
        with live.queue(fetch.RequestPaused) as paused:
            requesting = asyncio.ensure_future(evaluate(live, f"window.__get('{site}/api')"))
            held = await drain_until(paused, lambda event: True, timeout=15.0)
            await live.send(
                fetch.ContinueRequest(
                    request_id=held.request_id,
                    url=f"{site}/echo",
                    method="POST",
                    post_data=base64.b64encode(b"rerouted").decode(),
                    headers=[
                        fetch.HeaderEntry(name="X-Rewritten", value="by-python"),
                        fetch.HeaderEntry(name="Content-Type", value="text/plain"),
                    ],
                )
            )
            answered = await requesting

        echoed = json.loads(answered["body"])["headers"]
        assert echoed["x-rewritten"] == "by-python"
        assert echoed["content-length"] == str(len(b"rerouted"))
    finally:
        await live.send(fetch.Disable())


async def test_a_request_can_be_failed_outright(live: Session, site: str) -> None:
    """Fetch.failRequest. The page sees a network error, which is how offline
    and connection-refused branches are exercised without unplugging anything.
    """
    await goto(live, f"{site}/xhr.html")
    await live.send(network.Enable())
    await live.send(fetch.Enable(patterns=[fetch.RequestPattern(url_pattern="*/api*")]))
    try:
        with live.queue(fetch.RequestPaused) as paused, live.queue(network.LoadingFailed) as failed:
            requesting = asyncio.ensure_future(evaluate(live, f"window.__try('{site}/api')"))
            held = await drain_until(paused, lambda event: True, timeout=15.0)
            await live.send(fetch.FailRequest(request_id=held.request_id, error_reason="ConnectionRefused"))
            answered = await requesting
            failure = await drain_until(failed, lambda event: True, timeout=15.0)

        assert answered["ok"] is False
        assert "Failed to fetch" in answered["error"]
        assert failure.error_text
    finally:
        await live.send(fetch.Disable())
        await live.send(network.Disable())


async def test_the_response_stage_hands_back_the_server_body(live: Session, site: str) -> None:
    """Fetch.getResponseBody, which is only valid at the ``Response`` stage --
    so the pattern that caught this request had to ask for it.

    This is the shape a spec uses to *assert on* a response without changing
    it: the body is read, checked, and then handed to the page unmodified.
    """
    await goto(live, f"{site}/xhr.html")
    await live.send(fetch.Enable(patterns=[fetch.RequestPattern(url_pattern="*/api*", request_stage="Response")]))
    try:
        with live.queue(fetch.RequestPaused) as paused:
            requesting = asyncio.ensure_future(evaluate(live, f"window.__get('{site}/api')"))
            held = await drain_until(paused, lambda event: True, timeout=15.0)

            assert held.response_status_code == 200, "paused at the response stage, so the status is known"
            assert held.response_headers is not None

            body = await live.send(fetch.GetResponseBody(request_id=held.request_id))
            # Fetch.getResponseBody base64-encodes unconditionally, even for
            # JSON -- unlike Network.getResponseBody, which returns text as
            # text. The two commands share a result shape and do not share this
            # behaviour, so the flag has to be honoured rather than assumed.
            assert body.base64_encoded is True
            assert json.loads(base64.b64decode(body.body))["from"] == "the server"

            await live.send(fetch.ContinueRequest(request_id=held.request_id))
            answered = await requesting
        assert json.loads(answered["body"])["from"] == "the server"
    finally:
        await live.send(fetch.Disable())


async def test_a_binary_response_body_comes_back_base64(live: Session, site: str) -> None:
    """The ``base64_encoded`` flag at the response stage, which is the only way
    to know whether ``body`` is text or bytes wearing a string."""
    await goto(live, f"{site}/xhr.html")
    await live.send(fetch.Enable(patterns=[fetch.RequestPattern(url_pattern="*/binary*", request_stage="Response")]))
    try:
        with live.queue(fetch.RequestPaused) as paused:
            requesting = asyncio.ensure_future(evaluate(live, f"window.__get('{site}/binary')"))
            held = await drain_until(paused, lambda event: True, timeout=15.0)
            body = await live.send(fetch.GetResponseBody(request_id=held.request_id))
            await live.send(fetch.ContinueRequest(request_id=held.request_id))
            await requesting
        assert body.base64_encoded is True
        assert base64.b64decode(body.body) == bytes(range(256))
    finally:
        await live.send(fetch.Disable())


async def test_a_basic_auth_challenge_can_be_answered(live: Session, site: str) -> None:
    """Fetch.authRequired and Fetch.continueWithAuth.

    Chrome asks the driver for credentials rather than showing the browser's
    own dialog -- which in headless nobody could answer, so an unhandled
    challenge is a request that hangs until the test times out.

    ``handle_auth_requests=True`` is what opts into this; without it the
    challenge is not offered and the request simply fails.
    """
    await goto(live, f"{site}/xhr.html")
    await live.send(
        fetch.Enable(
            patterns=[fetch.RequestPattern(url_pattern="*/auth*")],
            handle_auth_requests=True,
        )
    )
    try:
        with live.queue(fetch.RequestPaused) as paused, live.queue(fetch.AuthRequired) as challenges:
            requesting = asyncio.ensure_future(evaluate(live, f"window.__get('{site}/auth')"))

            first = await drain_until(paused, lambda event: True, timeout=15.0)
            await live.send(fetch.ContinueRequest(request_id=first.request_id))

            challenge = await drain_until(challenges, lambda event: True, timeout=15.0)
            assert challenge.auth_challenge.scheme.lower() == "basic"
            assert challenge.auth_challenge.realm == "wirespec"
            assert challenge.auth_challenge.source in {"Server", None}

            # The same request_id throughout: Chrome retries the request it
            # already has rather than making a new one, so an answer keyed on
            # "a different id from last time" waits for an event that never
            # comes.
            assert challenge.request_id == first.request_id

            await live.send(
                fetch.ContinueWithAuth(
                    request_id=challenge.request_id,
                    auth_challenge_response=fetch.AuthChallengeResponse(
                        response="ProvideCredentials", username=AUTH_USER, password=AUTH_PASSWORD
                    ),
                )
            )

            # No second pause: answering the challenge completes the request.
            answered = await asyncio.wait_for(requesting, 15.0)

        assert answered["status"] == 200
        assert json.loads(answered["body"])["authenticated"] is True
    finally:
        await live.send(fetch.Disable())


async def test_an_auth_challenge_can_be_declined(live: Session, site: str) -> None:
    """``CancelAuth`` is the other half: the spec that checks what the page does
    when the credentials are wrong."""
    await goto(live, f"{site}/xhr.html")
    await live.send(
        fetch.Enable(
            patterns=[fetch.RequestPattern(url_pattern="*/auth*")],
            handle_auth_requests=True,
        )
    )
    try:
        with live.queue(fetch.RequestPaused) as paused, live.queue(fetch.AuthRequired) as challenges:
            requesting = asyncio.ensure_future(evaluate(live, f"window.__try('{site}/auth')"))
            first = await drain_until(paused, lambda event: True, timeout=15.0)
            await live.send(fetch.ContinueRequest(request_id=first.request_id))

            challenge = await drain_until(challenges, lambda event: True, timeout=15.0)
            await live.send(
                fetch.ContinueWithAuth(
                    request_id=challenge.request_id,
                    auth_challenge_response=fetch.AuthChallengeResponse(response="CancelAuth"),
                )
            )
            answered = await asyncio.wait_for(requesting, 15.0)

        # Declining does not fail the request: the unauthenticated response is
        # delivered to the page as it stands, which is what the page would have
        # seen had nobody been intercepting at all.
        assert answered["ok"] is True
        assert answered["status"] == 401
    finally:
        await live.send(fetch.Disable())


async def test_disable_stops_the_pausing(live: Session, site: str) -> None:
    """Fetch.disable. Every paused request must be answered or the page waits
    forever, so a spec that turns this on must turn it off again."""
    await goto(live, f"{site}/xhr.html")
    await live.send(fetch.Enable(patterns=[fetch.RequestPattern(url_pattern="*/api*")]))
    await live.send(fetch.Disable())

    with live.queue(fetch.RequestPaused) as paused:
        answered = await evaluate(live, f"window.__get('{site}/api')")
        await asyncio.sleep(0.3)
    assert paused.qsize() == 0, "nothing should pause once Fetch is disabled"
    assert json.loads(answered["body"])["from"] == "the server"
