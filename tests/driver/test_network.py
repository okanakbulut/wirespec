"""Watching traffic go by, and changing it in flight.

Two separate things, kept apart because enabling ``Fetch`` costs every request a
round trip: ``Network`` observes, ``Fetch`` intercepts, and both are enabled
lazily so a page that never asks pays nothing (§6.2).
"""

import pytest

from wirespec.expect import expect
from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_requests_and_responses_can_be_watched(page: Page) -> None:
    seen: list[str] = []
    await page.on("request", lambda request: seen.append(f"{request.method} {request.url}"))
    await page.goto("/network.html")
    assert any(entry.endswith("/network.html") for entry in seen)
    assert all(entry.startswith("GET ") for entry in seen)


async def test_a_response_carries_its_status_and_its_request(page: Page) -> None:
    responses: list = []
    await page.on("response", responses.append)
    await page.goto("/network.html")
    landing = next(r for r in responses if r.url.endswith("/network.html"))
    assert landing.status == 200
    assert landing.ok is True
    assert landing.request.method == "GET"


async def test_a_response_body_can_be_read(page: Page) -> None:
    await page.goto("/network.html")
    async with page.expect_response(lambda r: r.url.endswith("/api")) as caught:
        await page.locator("#go").click()
    response = caught.result()
    assert response.status == 200
    assert "the server" in await response.text()
    assert (await response.json())["from"] == "the server"


async def test_expect_request_catches_what_the_block_causes(page: Page) -> None:
    """An async context manager, so the subscription is in place before the
    block runs -- a request made by the very first line is still caught."""
    await page.goto("/network.html")
    async with page.expect_request(lambda r: r.url.endswith("/api")) as caught:
        await page.locator("#go").click()
    assert caught.result().method == "GET"


async def test_a_route_can_fulfil_a_request_without_a_server(page: Page) -> None:
    await page.route("**/api", lambda route: route.fulfill(body='{"from":"the route"}'))
    await page.goto("/network.html")
    async with page.expect_response(lambda r: r.url.endswith("/api")):
        await page.locator("#go").click()
    # A retrying assertion, not a read: `expect_response` fires when the
    # response arrives, and the page's own `.then()` that writes it into the
    # document has not run yet.
    await expect(page.locator("#out")).to_contain_text("the route")


async def test_a_route_can_abort(page: Page) -> None:
    await page.route("**/api", lambda route: route.abort())
    await page.goto("/network.html")
    await page.evaluate("() => window.__fetch('/api').catch(() => { window.__failed = true; })")
    await page.wait_for_selector("#out", state="attached")
    assert await page.evaluate("() => window.__failed === true") is True


async def test_a_route_can_let_it_through(page: Page) -> None:
    handled: list[str] = []

    async def pass_through(route) -> None:
        handled.append(route.request.url)
        await route.continue_()

    await page.route("**/api", pass_through)
    await page.goto("/network.html")
    async with page.expect_response(lambda r: r.url.endswith("/api")):
        await page.locator("#go").click()
    assert handled and handled[0].endswith("/api")
    await expect(page.locator("#out")).to_contain_text("the server")


async def test_a_route_only_sees_what_its_pattern_matches(page: Page) -> None:
    """A glob, not a regex: ``**/api`` is what a spec writes."""
    seen: list[str] = []

    async def watch(route) -> None:
        seen.append(route.request.url)
        await route.continue_()

    await page.route("**/api", watch)
    await page.goto("/network.html")
    assert seen == [], "the page itself does not match the pattern"


async def test_routing_can_be_set_on_the_whole_context(page: Page, browser, site: str) -> None:
    """§6.1: a context routes for every page opened in it, which is
    how a spec stubs an API once rather than per tab."""
    async with browser.new_context(base_url=site) as context:
        await context.route("**/api", lambda route: route.fulfill(body='{"from":"the context"}'))
        first = await context.new_page()
        await first.goto("/network.html")
        async with first.expect_response(lambda r: r.url.endswith("/api")):
            await first.locator("#go").click()
        assert "the context" in await first.locator("#out").text_content()


async def test_a_handler_that_raises_does_not_wedge_the_connection(page: Page) -> None:
    """The trap §16.2 names from the other side: a route handler
    runs off the read path, so it can await -- and if it throws, the request has
    to be let through rather than left paused for ever with the page hanging on
    it."""

    def broken(route) -> None:
        raise RuntimeError("the handler is wrong")

    await page.route("**/api", broken)
    await page.goto("/network.html")
    async with page.expect_response(lambda r: r.url.endswith("/api")):
        await page.locator("#go").click()
    await expect(page.locator("#out")).to_contain_text("the server")
