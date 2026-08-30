"""`page.request` -- a request through the application's own door.

The mechanism is §6.2: a navigation on a scratch page, paused by
`Fetch` and rewritten into whatever the caller asked for. No JavaScript is
involved anywhere, which is the whole reason it is built this way.

Everything worth asserting is on the *server's* side. A response body proves the
call happened; only the echo endpoint proves it happened as the right method,
with the right body, and carrying the right cookies.
"""

import json

import pytest

from wirespec.api import RequestError
from wirespec.errors import WirespecTimeoutError
from wirespec.page import Page


@pytest.mark.asyncio(loop_scope="session")
async def test_get_returns_the_response(page: Page) -> None:
    await page.goto("/index.html")

    response = await page.request.get("/echo")

    assert response.status == 200
    assert response.ok
    assert json.loads(await response.text())["method"] == "GET"


@pytest.mark.asyncio(loop_scope="session")
async def test_post_sends_the_method_and_the_body(page: Page) -> None:
    """The half `Network.loadNetworkResource` could not do (§6.2)."""
    await page.goto("/index.html")

    response = await page.request.post("/echo", data='{"hello":"world"}')

    seen = json.loads(await response.text())
    assert seen["method"] == "POST"
    assert seen["body"] == '{"hello":"world"}'


@pytest.mark.asyncio(loop_scope="session")
async def test_headers_arrive(page: Page) -> None:
    await page.goto("/index.html")

    response = await page.request.post(
        "/echo", data="{}", headers={"Content-Type": "application/json", "X-Custom": "yes"}
    )

    seen = json.loads(await response.text())
    assert seen["content-type"] == "application/json"
    assert seen["x-custom"] == "yes"


@pytest.mark.asyncio(loop_scope="session")
async def test_the_context_cookies_go_out(page: Page) -> None:
    """The reason the request is made from the browser at all.

    A spec asserting that a logged-in user may do something is asserting about a
    request that carries the session cookie. One composed in Python would not.
    """
    await page.context.add_cookies([{"name": "who", "value": "sam", "url": page.context.base_url}])
    await page.goto("/index.html")

    response = await page.request.get("/echo")

    assert "who=sam" in (json.loads(await response.text())["cookie"] or "")


@pytest.mark.asyncio(loop_scope="session")
async def test_a_set_cookie_comes_back_into_the_jar(page: Page) -> None:
    """The other direction: a login endpoint's cookie is usable afterwards."""
    await page.goto("/index.html")

    await page.request.get("/seed-cookie")
    response = await page.request.get("/echo")

    assert "seeded=yes" in (json.loads(await response.text())["cookie"] or "")


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("verb", ["PUT", "PATCH", "DELETE"])
async def test_every_method_arrives_as_itself(page: Page, verb: str) -> None:
    await page.goto("/index.html")

    response = await getattr(page.request, verb.lower())("/echo")

    assert json.loads(await response.text())["method"] == verb


@pytest.mark.asyncio(loop_scope="session")
async def test_a_404_is_a_response_not_an_error(page: Page) -> None:
    """4xx and 5xx are answers. Only "there is no answer" raises."""
    await page.goto("/index.html")

    response = await page.request.get("/teapot")

    assert response.status == 418
    assert not response.ok
    assert await response.text() == "short and stout"


@pytest.mark.asyncio(loop_scope="session")
async def test_redirects_are_followed(page: Page) -> None:
    await page.goto("/index.html")

    response = await page.request.get("/redirect")

    assert response.status == 200
    assert json.loads(await response.text())["path"] == "/echo?landed"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_redirect_can_be_left_unfollowed(page: Page) -> None:
    """The 3xx itself, which is what an assertion about a redirect wants.

    Its body is empty on purpose: a 3xx pause has no body to read, and asking
    for one is refused by Chrome (§6.2).
    """
    await page.goto("/index.html")

    response = await page.request.get("/redirect", follow_redirects=False)

    assert response.status == 302
    assert response.headers["Location"] == "/echo?landed"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_post_that_redirects_becomes_a_get(page: Page) -> None:
    """RFC 9110 §15.4: a 302 turns the next hop into a GET with no body."""
    await page.goto("/index.html")

    response = await page.request.post("/redirect", data="dropped")

    seen = json.loads(await response.text())
    assert seen["method"] == "GET"
    assert seen["body"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_a_request_that_never_connects_names_the_error(page: Page) -> None:
    """Measured (§6.2): a failed request pauses at the Request
    stage and never reaches the Response stage, so the navigation's own
    ``errorText`` is the only thing there is to say. Without the race against
    it, this waits out its whole timeout and then says nothing.
    """
    await page.goto("/index.html")

    with pytest.raises(RequestError) as raised:
        await page.request.get("http://127.0.0.1:9/nothing", timeout=5.0)

    assert "127.0.0.1:9/nothing" in str(raised.value)
    assert "net::" in str(raised.value)


@pytest.mark.asyncio(loop_scope="session")
async def test_exactly_one_request_reaches_the_server(page: Page) -> None:
    """The trap in §8.14, pinned from the only side that can see it.

    The first version enabled `Fetch` with `urlPattern: "*"`. The scratch page's
    own `/favicon.ico` paused too, was rewritten with this call's method and
    body, and the server received `POST /favicon.ico` carrying the payload. The
    caller still got the right response, because the right response also
    arrived; a server keeping a write log would have had two entries.
    """
    await page.goto("/index.html")

    await page.request.post("/echo?once", data="payload")

    log = json.loads(await (await page.request.get("/requests")).text())
    assert [entry for entry in log if "once" in entry] == ["POST /echo?once"]
    # A `GET /favicon.ico` is just what a browser does on a page load. The bug
    # is one carrying this call's *method*, which is what a catch-all pattern
    # produced.
    assert not [entry for entry in log if "favicon" in entry and not entry.startswith("GET")]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_scratch_page_is_not_one_of_the_context_pages(page: Page) -> None:
    """A test that counts tabs must not find wirespec's own."""
    await page.goto("/index.html")
    before = list(page.context.pages)

    await page.request.get("/echo")

    assert page.context.pages == before


@pytest.mark.asyncio(loop_scope="session")
async def test_the_calling_page_is_untouched(page: Page) -> None:
    """The navigation that carries the request is aborted before it commits."""
    await page.goto("/index.html")
    was_at = page.url

    await page.request.post("/echo", data="{}")

    assert page.url == was_at
    assert await page.evaluate("document.readyState") == "complete"


@pytest.mark.asyncio(loop_scope="session")
async def test_two_pages_in_one_context_share_the_jar(page: Page) -> None:
    """`request` is the context's, because the cookie jar is."""
    other = await page.context.new_page()
    await page.goto("/index.html")

    await page.request.get("/seed-cookie")
    response = await other.request.get("/echo")

    assert page.request is other.request
    assert "seeded=yes" in (json.loads(await response.text())["cookie"] or "")


@pytest.mark.asyncio(loop_scope="session")
async def test_a_relative_url_needs_a_base(browser, site: str) -> None:
    """The same rule as `page.goto`: raise rather than guess (§6.2)."""
    async with browser.new_context() as bare:
        page = await bare.new_page()
        with pytest.raises(ValueError, match="relative"):
            await page.request.get("/echo")


@pytest.mark.asyncio(loop_scope="session")
async def test_the_request_still_looks_like_a_navigation(page: Page) -> None:
    """The documented limit (§6.2).

    Chrome recomputes some `Sec-Fetch-*` headers after `continueRequest`, and
    `Sec-Fetch-Dest` still says `document` however it is overridden. An
    application that hard-checks it would reject the request -- loudly, with its
    own error, which is what makes this acceptable to document rather than hide.
    Pinned so that a Chrome that starts honouring the override is noticed.
    """
    await page.goto("/index.html")

    response = await page.request.get("/echo", headers={"Sec-Fetch-Dest": "empty"})

    assert json.loads(await response.text())["sec-fetch-dest"] == "document"


@pytest.mark.asyncio(loop_scope="session")
async def test_routes_do_not_intercept_it(page: Page) -> None:
    """Measured, and worth knowing before relying on either reading.

    `page.route` and `context.route` install `Fetch` on a *page's* session. The
    scratch page is deliberately not one of those pages -- it skips
    `new_page` precisely so nothing else subscribes to the session this owns
    (§6.2) -- so a route never sees an API call, and an API call
    never has to share a `requestPaused` with a route handler.

    The consequence a spec has to know: stubbing an endpoint does not stub it
    for `page.request`, which reaches the real server. Recorded in §12.
    """
    await page.goto("/index.html")
    intercepted: list[str] = []

    async def stub(route) -> None:
        intercepted.append(route.request.url)
        await route.fulfill(body='{"from":"the route"}')

    await page.route("**/echo*", stub)

    response = await page.request.get("/echo?routed")

    assert intercepted == []
    assert json.loads(await response.text())["path"] == "/echo?routed"


@pytest.mark.asyncio(loop_scope="session")
async def test_dispose_closes_the_scratch_page_and_the_next_call_reopens_it(page: Page) -> None:
    await page.goto("/index.html")
    await page.request.get("/echo")

    await page.request.dispose()

    assert (await page.request.get("/echo")).status == 200


@pytest.mark.asyncio(loop_scope="session")
async def test_a_mapping_body_is_sent_as_json(page: Page) -> None:
    """Playwright's rule, measured against 1.62: ``data`` that is not a string
    or bytes is serialised to JSON and the request carries
    ``Content-Type: application/json`` unless the caller set one."""
    answer = await page.request.post("/echo", data={"operations": [], "n": 1})
    echoed = await answer.json()
    assert echoed["body"] == '{"operations": [], "n": 1}'
    assert echoed["content-type"] == "application/json"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_sequence_body_is_json_too(page: Page) -> None:
    answer = await page.request.post("/echo", data=[1, 2])
    assert (await answer.json())["body"] == "[1, 2]"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_content_type_the_caller_set_wins(page: Page) -> None:
    """Serialising is a convenience; deciding what the request says it is
    belongs to whoever wrote it."""
    answer = await page.request.post("/echo", data={"a": 1}, headers={"Content-Type": "text/plain"})
    echoed = await answer.json()
    assert echoed["body"] == '{"a": 1}'
    assert echoed["content-type"] == "text/plain"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_string_body_is_still_sent_as_it_was_written(page: Page) -> None:
    """Unchanged, and no content type invented for it: a caller who wrote the
    bytes has already decided what they are."""
    answer = await page.request.post("/echo", data="plain text")
    assert (await answer.json())["body"] == "plain text"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_request_that_stalls_times_out_by_name(page: Page) -> None:
    """A bare ``TimeoutError`` names neither the method nor the URL, and is not
    a ``WirespecError`` -- so a suite wrapping its API calls in
    ``except WirespecError`` catches nothing (§1, goal 4)."""
    with pytest.raises(WirespecTimeoutError) as raised:
        await page.request.get("/slow", timeout=0.25)
    assert "GET" in str(raised.value)
    assert "/slow" in str(raised.value)
    assert "0.25s" in str(raised.value)
