"""``alert``, ``confirm`` and ``prompt``: the page asking a question back.

A dialog is the one thing a page can do that stops the driver rather than the
other way round. Until it is answered the renderer is blocked, so the command
that opened it never returns and neither does anything sent afterwards -- a
suite that ignores dialogs does not misbehave, it stops (§8.20).

So the load-bearing test here is the first one, which registers no handler at
all: the default has to be an answer, because the alternative is a fifteen
second timeout that names the click and not the dialog.
"""

import pytest

from wirespec.dialogs import Dialog
from wirespec.errors import WirespecError
from wirespec.expect import expect
from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_dialog_nobody_is_listening_for_is_dismissed(page: Page) -> None:
    """The landmine. Without a default the click never returns, and the failure
    a spec sees is a timeout on ``#confirm`` that says nothing about a dialog.

    Dismissed rather than accepted, because "cancel" is the answer that changes
    least: a spec that never mentioned the dialog did not ask for the
    destructive branch of a confirm.
    """
    await page.goto("/dialogs.html")
    await page.locator("#confirm").click()
    await expect(page.locator("#answer")).to_have_text("cancelled")


async def test_a_dialog_before_the_load_event_does_not_hang_goto(page: Page) -> None:
    """The same trap one step earlier: this one blocks the navigation itself,
    so there is no click to blame and no element to wait for."""
    await page.goto("/dialog-on-load.html")
    await expect(page.locator("h1")).to_have_text("loaded anyway")


async def test_a_dialog_nothing_is_waiting_on_is_handled_too(page: Page) -> None:
    """Opened from a timer, so the click has long since returned. A driver that
    answered dialogs by watching the command in flight would miss this one and
    wedge the page for every test that followed."""
    await page.goto("/dialogs.html")
    await page.locator("#later").click()
    await expect(page.locator("#answer")).to_have_text("late cancelled")


async def test_a_handler_sees_what_the_dialog_says(page: Page) -> None:
    seen: list[Dialog] = []

    async def answer(dialog: Dialog) -> None:
        seen.append(dialog)
        await dialog.accept("Ada")

    await page.on("dialog", answer)
    await page.goto("/dialogs.html")
    await page.locator("#prompt").click()

    await expect(page.locator("#answer")).to_have_text("Ada")
    assert len(seen) == 1
    assert seen[0].type == "prompt"
    assert seen[0].message == "your name?"
    assert seen[0].default_value == "default name"
    assert seen[0].page is page


async def test_accepting_a_confirm_takes_the_other_branch(page: Page) -> None:
    """The reason a handler exists at all: the default answer is the safe one,
    and a spec testing the destructive path has to be able to say so."""

    async def answer(dialog: Dialog) -> None:
        await dialog.accept()

    await page.on("dialog", answer)
    await page.goto("/dialogs.html")
    await page.locator("#confirm").click()
    await expect(page.locator("#answer")).to_have_text("confirmed")


async def test_an_alert_has_no_answer_to_give(page: Page) -> None:
    """``alert`` takes accept and dismiss alike -- there is only one button. The
    thing worth asserting is that the page carries on either way."""

    async def answer(dialog: Dialog) -> None:
        assert dialog.type == "alert"
        assert dialog.default_value == ""
        await dialog.accept()

    await page.on("dialog", answer)
    await page.goto("/dialogs.html")
    await page.locator("#alert").click()
    await expect(page.locator("#answer")).to_have_text("alert dismissed")


async def test_dismissing_a_prompt_is_a_null_not_an_empty_string(page: Page) -> None:
    """Cancelling a prompt gives the page ``null``, and accepting a prompt with
    no text gives it ``""``. Distinguishable, and a driver that dismissed by
    accepting empty text would flatten the difference."""

    async def answer(dialog: Dialog) -> None:
        await dialog.dismiss()

    await page.on("dialog", answer)
    await page.goto("/dialogs.html")
    await page.locator("#prompt").click()
    await expect(page.locator("#answer")).to_have_text("cancelled")


async def test_accepting_a_prompt_with_no_text_is_an_empty_string(page: Page) -> None:
    async def answer(dialog: Dialog) -> None:
        await dialog.accept()

    await page.on("dialog", answer)
    await page.goto("/dialogs.html")
    await page.locator("#prompt").click()
    await expect(page.locator("#answer")).to_have_text("")


async def test_an_ordinary_function_may_handle_it(page: Page) -> None:
    """A handler that only wants to *see* dialogs has nothing to await, and
    should not have to be a coroutine to say so."""
    messages: list[str] = []

    await page.on("dialog", lambda dialog: messages.append(dialog.message))
    await page.goto("/dialogs.html")
    await page.locator("#confirm").click()

    await expect(page.locator("#answer")).to_have_text("cancelled")
    assert messages == ["really?"]


async def test_a_handler_that_decides_nothing_still_unblocks_the_page(page: Page) -> None:
    """Playwright freezes here, deliberately, so that the omission is noticed.
    wirespec answers instead and says so on the loop's exception handler: a
    frozen page is a silent failure, and every constraint in this project
    points the other way (§8.20)."""
    await page.on("dialog", lambda dialog: None)
    await page.goto("/dialogs.html")
    await page.locator("#confirm").click()
    await expect(page.locator("#answer")).to_have_text("cancelled")


async def test_a_handler_that_raises_still_unblocks_the_page(page: Page) -> None:
    def explode(dialog: Dialog) -> None:
        raise RuntimeError("the handler is broken")

    await page.on("dialog", explode)
    await page.goto("/dialogs.html")
    await page.locator("#confirm").click()
    await expect(page.locator("#answer")).to_have_text("cancelled")


async def test_answering_the_same_dialog_twice_refuses(page: Page) -> None:
    """The second answer would reach whatever dialog is open *next*, which is
    the kind of wrong that shows up three tests later."""
    failures: list[str] = []

    async def answer(dialog: Dialog) -> None:
        await dialog.accept()
        try:
            await dialog.dismiss()
        except WirespecError as exc:
            failures.append(str(exc))

    await page.on("dialog", answer)
    await page.goto("/dialogs.html")
    await page.locator("#confirm").click()
    await expect(page.locator("#answer")).to_have_text("confirmed")
    assert len(failures) == 1
    assert "already" in failures[0]
    assert "confirm" in failures[0]


async def test_unsubscribing_puts_the_default_back(page: Page) -> None:
    accepted: list[str] = []

    async def answer(dialog: Dialog) -> None:
        accepted.append(dialog.message)
        await dialog.accept()

    unsubscribe = await page.on("dialog", answer)
    await page.goto("/dialogs.html")
    await page.locator("#confirm").click()
    await expect(page.locator("#answer")).to_have_text("confirmed")

    unsubscribe()
    await page.locator("#confirm").click()
    await expect(page.locator("#answer")).to_have_text("cancelled")
    assert accepted == ["really?"]


async def test_a_dialog_on_one_page_is_not_a_dialog_on_another(page: Page) -> None:
    """Handlers are per page, and so is the subscription underneath them: a
    flat session delivers every target's events down one pipe, so a driver that
    subscribed without a session id would answer the wrong tab's question."""
    other = await page.context.new_page()
    seen: list[str] = []

    async def answer(dialog: Dialog) -> None:
        seen.append(dialog.message)
        await dialog.accept()

    await other.on("dialog", answer)
    await page.goto("/dialogs.html")
    await page.locator("#confirm").click()

    await expect(page.locator("#answer")).to_have_text("cancelled")
    assert seen == []


async def test_page_on_refuses_an_event_it_does_not_have(page: Page) -> None:
    with pytest.raises(ValueError, match="dialog"):
        await page.on("console", lambda event: None)
