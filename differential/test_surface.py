"""One spec file, run under Playwright and under wirespec, results compared.

§15.4. Not a wirespec test suite: it is written the way a
Playwright suite is written, in milliseconds and dict viewports, and it must
pass under Playwright first — a test that only wirespec passes proves nothing,
and a test that neither passes is a bug in this file.

Run it with ``differential/compare.py``, which runs both and diffs the
outcomes. Running it directly under either interpreter also works and is how a
disagreement gets investigated.
"""

import re

import pytest
from playwright.async_api import expect

pytestmark = pytest.mark.asyncio(loop_scope="session")


# -- locators ---------------------------------------------------------------


async def test_a_css_locator_finds_and_counts(page) -> None:
    await page.goto("/list.html")
    assert await page.locator("#packs li").count() == 3
    assert await page.locator("#packs li").first.text_content() == "Acme"
    assert await page.locator("#packs li").last.text_content() == "Gamma"
    assert await page.locator("#packs li").nth(1).text_content() == "Beta"


async def test_locators_chain_and_scope(page) -> None:
    await page.goto("/list.html")
    assert await page.locator("#packs").locator("li").count() == 3
    assert await page.locator("#empty").locator("li").count() == 0


async def test_get_by_role_uses_the_browsers_own_computation(page) -> None:
    await page.goto("/roles.html")
    await expect(page.get_by_role("button", name="A button")).to_have_count(1)
    await expect(page.get_by_role("heading", name="Headings")).to_have_count(1)
    await expect(page.get_by_role("link", name="A real link")).to_have_count(1)
    await expect(page.get_by_role("checkbox", name="A checkbox")).to_have_count(1)


async def test_get_by_role_matches_the_name_loosely_unless_told_not_to(page) -> None:
    await page.goto("/roles.html")
    await expect(page.get_by_role("button", name="a butt")).to_have_count(1)
    await expect(page.get_by_role("button", name="a butt", exact=True)).to_have_count(0)


async def test_get_by_text_takes_a_string_or_a_pattern(page) -> None:
    await page.goto("/list.html")
    assert await page.get_by_text("Acme").count() == 1
    assert await page.get_by_text(re.compile(r"^(Acme|Beta)$")).count() == 2


async def test_get_by_label_and_placeholder_and_test_id(page) -> None:
    await page.goto("/roles.html")
    await expect(page.get_by_label("A text input")).to_have_count(1)
    await page.goto("/assertions.html")
    await expect(page.get_by_test_id("the-link")).to_have_count(1)


async def test_filter_keeps_and_drops_by_text(page) -> None:
    await page.goto("/list.html")
    assert await page.locator("#packs li").filter(has_text="Beta").count() == 1
    assert await page.locator("#packs li").filter(has_not_text="Beta").count() == 2


async def test_or_matches_either_side(page) -> None:
    await page.goto("/list.html")
    either = page.locator("#nothing-here").or_(page.locator("#outside"))
    assert await either.text_content() == "not in a list"


# -- readers ----------------------------------------------------------------


async def test_text_content_is_the_source_text(page) -> None:
    await page.goto("/readers.html")
    assert await page.locator("#lines").text_content() == "onetwo"
    assert await page.locator("#rows").all_text_contents() == ["\n  Row one\n  Row two\n  Row three\n"]


async def test_inner_text_is_the_rendered_text(page) -> None:
    await page.goto("/readers.html")
    assert await page.locator("#lines").inner_text() == "one\ntwo"
    assert await page.locator("#blocks").inner_text() == "alpha\nbeta"


async def test_attributes_and_values(page) -> None:
    await page.goto("/readers.html")
    assert await page.locator("#rows li").first.get_attribute("data-kind") == "a"
    assert await page.locator("#rows li").first.get_attribute("data-missing") is None
    assert await page.locator("#value").input_value() == "in the field"


async def test_state_readers(page) -> None:
    await page.goto("/readers.html")
    assert await page.locator("#checked").is_checked() is True
    assert await page.locator("#unchecked").is_checked() is False
    assert await page.locator("#disabled").is_enabled() is False
    assert await page.locator("#readonly").is_editable() is False
    assert await page.locator("#lines").is_visible() is True
    assert await page.locator("#hidden-by-display").is_visible() is False


async def test_bounding_box_is_in_viewport_coordinates(page) -> None:
    await page.goto("/readers.html")
    box = await page.locator("#padded").bounding_box()
    assert box is not None
    # 50px left plus an 8px margin, and the box includes border and padding.
    assert box["x"] == pytest.approx(58, abs=1)
    assert box["width"] == pytest.approx(150, abs=1)


async def test_evaluate_on_the_page_and_on_an_element(page) -> None:
    await page.goto("/readers.html")
    assert await page.evaluate("() => 1 + 1") == 2
    assert await page.evaluate("x => x * 2", 21) == 42
    assert await page.locator("#lines").evaluate("node => node.tagName") == "DIV"
    assert await page.locator("#rows li").evaluate_all("nodes => nodes.length") == 3


# -- actions ----------------------------------------------------------------


async def test_click_reaches_the_element(page) -> None:
    await page.goto("/actions.html")
    await page.locator("#plain").click()
    assert "plain" in await page.evaluate("() => window.__log.join(',')")


async def test_click_waits_for_the_element_to_arrive(page) -> None:
    await page.goto("/waiting.html")
    await page.evaluate("() => window.__appear(150)")
    await expect(page.locator("#appeared")).to_be_visible()


async def test_fill_replaces_and_fires_input(page) -> None:
    await page.goto("/actions.html")
    await page.locator("#text").fill("typed in")
    assert await page.locator("#text").input_value() == "typed in"


async def test_type_and_press(page) -> None:
    await page.goto("/actions.html")
    await page.locator("#text").fill("")
    await page.locator("#text").type("abc")
    assert await page.locator("#text").input_value() == "abc"
    await page.locator("#text").press("Backspace")
    assert await page.locator("#text").input_value() == "ab"


async def test_hover_and_focus_and_scroll(page) -> None:
    await page.goto("/actions.html")
    await page.locator("#plain").hover()
    await page.locator("#text").focus()
    await expect(page.locator("#text")).to_be_focused()
    await page.locator("#below").scroll_into_view_if_needed()


async def test_select_option_by_value_label_and_index(page) -> None:
    await page.goto("/actions.html")
    assert await page.locator("#fruit").select_option("d") == ["d"]
    assert await page.locator("#fruit").select_option(label="Banana") == ["b"]
    assert await page.locator("#fruit").select_option(index=0) == ["a"]


async def test_select_option_on_a_multiple_select(page) -> None:
    await page.goto("/actions.html")
    assert await page.locator("#many").select_option(["q", "t"]) == ["q", "t"]
    assert await page.locator("#many").evaluate("node => node.selectedOptions.length") == 2


async def test_set_input_files(page, tmp_path) -> None:
    payload = tmp_path / "invoice.pdf"
    payload.write_bytes(b"%PDF-1.4\n")
    await page.goto("/actions.html")
    await page.locator("#file").set_input_files(str(payload))
    assert await page.evaluate("() => document.getElementById('file').files[0].name") == "invoice.pdf"


# -- assertions -------------------------------------------------------------


async def test_the_counting_and_visibility_assertions(page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#rows li")).to_have_count(3)
    await expect(page.locator("#one")).to_be_visible()
    await expect(page.locator("#invisible")).to_be_hidden()
    await expect(page.locator("#nothing-at-all")).to_be_hidden()


async def test_the_text_assertions(page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#one")).to_have_text("the only one")
    await expect(page.locator("#one")).to_contain_text("only")
    await expect(page.locator("#one")).not_to_contain_text("Beta")
    await expect(page.locator("#rows li")).to_have_text(["Acme", "Beta", "Gamma"])


async def test_the_value_and_state_assertions(page) -> None:
    await page.goto("/assertions.html")
    await expect(page.locator("#value")).to_have_value("in the field")
    await expect(page.locator("#empty")).to_be_empty()
    await expect(page.locator("#checked")).to_be_checked()
    await expect(page.locator("#unchecked")).not_to_be_checked()
    await expect(page.locator("#disabled")).to_be_disabled()
    await expect(page.locator("#value")).to_be_enabled()
    await expect(page.locator("#link")).to_have_attribute("data-kind", "nav")


async def test_the_page_url_assertion(page) -> None:
    await page.goto("/index.html")
    await expect(page).to_have_url(re.compile(r"/index\.html$"))


async def test_an_assertion_waits_for_the_page_to_catch_up(page) -> None:
    await page.goto("/assertions.html")
    await page.evaluate("() => window.__grow(200)")
    await expect(page.locator(".grown")).to_have_count(1)


async def test_an_assertion_that_cannot_be_satisfied_times_out(page) -> None:
    await page.goto("/assertions.html")
    with pytest.raises(AssertionError):
        # Playwright's timeout goes on the assertion, not on `expect`, and what
        # it raises is an `AssertionError`. Both halves of that are the
        # contract a suite is written against.
        await expect(page.locator("#one")).to_have_text("something else", timeout=300)


# -- waiting and navigation -------------------------------------------------


async def test_wait_for_selector_in_every_state(page) -> None:
    await page.goto("/waiting.html")
    await page.wait_for_selector("#by-stylesheet", state="attached")
    await page.wait_for_selector("#by-stylesheet", state="hidden")
    await page.wait_for_selector("#vanishing", state="visible")
    await page.evaluate("() => window.__remove(100)")
    await page.wait_for_selector("#doomed", state="detached")


async def test_goto_and_reload_keep_the_url_current(page) -> None:
    await page.goto("/index.html")
    assert page.url.endswith("/index.html")
    await page.reload()
    assert page.url.endswith("/index.html")
    await page.goto("/list.html")
    assert page.url.endswith("/list.html")


async def test_the_viewport_can_be_resized(page) -> None:
    await page.goto("/index.html")
    await page.set_viewport_size({"width": 400, "height": 300})
    assert await page.evaluate("() => window.innerWidth") == 400


# -- network ----------------------------------------------------------------


async def test_a_route_can_fulfil_a_request(page) -> None:
    await page.route("**/api", lambda route: route.fulfill(body='{"from": "the route"}'))
    await page.goto("/network.html")
    await page.locator("#go").click()
    await expect(page.locator("#out")).to_have_text('{"from": "the route"}')


async def test_a_route_can_abort(page) -> None:
    await page.route("**/api", lambda route: route.abort())
    await page.goto("/network.html")
    await page.evaluate("() => window.__fetch('/api').catch(() => { document.title = 'blocked'; })")
    await expect(page).to_have_title("blocked")


async def test_expect_response_catches_the_answer(page) -> None:
    await page.goto("/network.html")
    async with page.expect_response(lambda response: response.url.endswith("/api")) as caught:
        await page.locator("#go").click()
    response = await caught.value
    assert response.status == 200
    assert response.request.method == "GET"
    assert (await response.json())["from"] == "the server"


async def test_page_on_request_sees_what_went_out(page) -> None:
    seen = []
    page.on("request", lambda request: seen.append(request.url))
    await page.goto("/network.html")
    assert any(url.endswith("/network.html") for url in seen)


# -- frames, tabs, dialogs --------------------------------------------------


async def test_a_frame_locator_reaches_into_a_frame(page) -> None:
    await page.goto("/frames.html")
    frame = page.frame_locator("#widget")
    assert await frame.locator("h1").text_content() == "inside the frame"
    await expect(frame.get_by_role("button", name="Press me")).to_be_visible()


async def test_a_query_in_a_frame_does_not_escape_it(page) -> None:
    await page.goto("/frames.html")
    await expect(page.frame_locator("#widget").get_by_text("Press me")).to_have_count(1)
    await expect(page.get_by_text("Press me")).to_have_count(1)


async def test_content_frame_and_owner(page) -> None:
    await page.goto("/frames.html")
    assert await page.locator("#second").content_frame.locator("h1").text_content() == "inside the frame"
    assert await page.frame_locator("#widget").owner.get_attribute("id") == "widget"


async def test_expect_popup_catches_a_new_tab(page) -> None:
    await page.goto("/popup.html")
    async with page.expect_popup() as popup:
        await page.locator("#open").click()
    opened = await popup.value
    await expect(opened.locator("h1")).to_be_visible()
    assert opened.url.endswith("/index.html")


async def test_a_dialog_is_dismissed_when_nobody_listens(page) -> None:
    await page.goto("/dialogs.html")
    await page.locator("#confirm").click()
    await expect(page.locator("#answer")).to_have_text("cancelled")


async def test_a_dialog_handler_can_accept(page) -> None:
    async def answer(dialog):
        await dialog.accept("Ada")

    page.on("dialog", answer)
    await page.goto("/dialogs.html")
    await page.locator("#prompt").click()
    await expect(page.locator("#answer")).to_have_text("Ada")
