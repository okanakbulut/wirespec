"""The failure messages, for **every** assertion and both its directions.

§11.2 lists these as their own obligation, and the reason is that
they are a feature that regresses without failing anything: an assertion whose
message stops saying what it last saw still passes when it should and still
fails when it should. The only thing that changes is how long the next person
spends in the browser.

A table rather than thirty near-identical tests, so adding an assertion to
``expect.py`` and forgetting it here is a gap that is visible by reading. Each
row says what to assert, what it should have wanted, and what it should report
seeing instead. Both are checked, because a message quoting only the
expectation is the "timed out" it exists to replace.
"""

import re

import pytest

from wirespec.errors import WirespecTimeoutError
from wirespec.expect import expect
from wirespec.page import Page

#: The subject is a CSS selector, or ``PAGE`` for the two page-level ones.
PAGE = None

#: ``(id, subject, method, args, wanted, saw, setup)``. ``setup`` is JavaScript
#: the *caller* supplies -- what a spec would write -- run before the assertion.
FAILURES = [
    ("count", "#rows li", "to_have_count", (9,), "to have count 9", "3", ""),
    ("count-not", "#rows li", "not_to_have_count", (3,), "not .* to have count 3", "3", ""),
    ("visible", "#invisible", "to_be_visible", (), "to be visible", "that it was not", ""),
    ("visible-not", "#one", "not_to_be_visible", (), "not .* to be visible", "that it was", ""),
    ("hidden", "#one", "to_be_hidden", (), "to be hidden", "that it was not", ""),
    ("hidden-not", "#invisible", "not_to_be_hidden", (), "not .* to be hidden", "that it was", ""),
    ("enabled", "#disabled", "to_be_enabled", (), "to be enabled", "that it was not", ""),
    ("enabled-not", "#value", "not_to_be_enabled", (), "not .* to be enabled", "that it was", ""),
    ("disabled", "#value", "to_be_disabled", (), "to be disabled", "that it was", ""),
    ("disabled-not", "#disabled", "not_to_be_disabled", (), "not .* to be disabled", "that it was not", ""),
    ("checked", "#unchecked", "to_be_checked", (), "to be checked", "that it was not", ""),
    ("checked-not", "#checked", "not_to_be_checked", (), "not .* to be checked", "that it was", ""),
    ("editable", "#readonly", "to_be_editable", (), "to be editable", "that it was not", ""),
    ("editable-not", "#value", "not_to_be_editable", (), "not .* to be editable", "that it was", ""),
    ("focused", "#focusable", "to_be_focused", (), "to be focused", "that it was not", ""),
    (
        "focused-not",
        "#focusable",
        "not_to_be_focused",
        (),
        "not .* to be focused",
        "that it was",
        "() => document.getElementById('focusable').focus()",
    ),
    ("empty", "#one", "to_be_empty", (), "to be empty", "the only one", ""),
    ("empty-not", "#empty", "not_to_be_empty", (), "not .* to be empty", "''", ""),
    ("text", "#one", "to_have_text", ("Nope",), "to have text 'Nope'", "the only one", ""),
    ("text-not", "#one", "not_to_have_text", ("the only one",), "not .* to have text", "the only one", ""),
    ("contains", "#one", "to_contain_text", ("Nope",), "to contain text 'Nope'", "the only one", ""),
    ("contains-not", "#one", "not_to_contain_text", ("only",), "not .* to contain text", "the only one", ""),
    ("value", "#value", "to_have_value", ("nope",), "to have value 'nope'", "in the field", ""),
    ("value-not", "#value", "not_to_have_value", ("in the field",), "not .* to have value", "in the field", ""),
    (
        "attribute",
        "#link",
        "to_have_attribute",
        ("data-kind", "nope"),
        "to have data-kind='nope'",
        "nav",
        "",
    ),
    (
        "attribute-not",
        "#link",
        "not_to_have_attribute",
        ("data-kind", "nav"),
        "not .* to have data-kind='nav'",
        "nav",
        "",
    ),
    ("css", "#swatch", "to_have_css", ("color", "rgb(1, 2, 3)"), "to have css color=", "rgb\\(20, 40, 60\\)", ""),
    (
        "css-not",
        "#swatch",
        "not_to_have_css",
        ("color", "rgb(20, 40, 60)"),
        "not .* to have css color=",
        "rgb\\(20, 40, 60\\)",
        "",
    ),
    ("url", PAGE, "to_have_url", ("/nowhere.html",), "to be at .*nowhere.html", "assertions.html", ""),
    ("url-not", PAGE, "not_to_have_url", ("/assertions.html",), "not the page to be at", "assertions.html", ""),
    ("title", PAGE, "to_have_title", ("not this",), "to be titled 'not this'", "assertions", ""),
    ("title-not", PAGE, "not_to_have_title", ("assertions",), "not the page to be titled", "assertions", ""),
]


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("subject", "method", "args", "wanted", "saw", "setup"),
    [case[1:] for case in FAILURES],
    ids=[case[0] for case in FAILURES],
)
async def test_a_failing_assertion_says_what_it_wanted_and_what_it_saw(
    page: Page, subject: str | None, method: str, args: tuple, wanted: str, saw: str, setup: str
) -> None:
    await page.goto("/assertions.html")
    if setup:
        await page.evaluate(setup)
    # `subject is None`, not `subject is PAGE`: the two are the same object and
    # only the first narrows the type, which is the difference between this
    # reading well and this needing an ignore comment.
    target = expect(page, timeout=0.3) if subject is None else expect(page.locator(subject), timeout=0.3)

    with pytest.raises(WirespecTimeoutError) as raised:
        await getattr(target, method)(*args)

    message = str(raised.value)
    # `re.search`, because the negated form puts the locator's repr between
    # "expected not" and the description and a plain substring cannot span it.
    assert re.search(wanted, message), f"wanted {wanted!r} in:\n{message}"
    assert re.search(saw, message), f"saw {saw!r} missing from:\n{message}"
    assert "last saw" in message
    assert "waited 0.3s" in message


def test_the_table_covers_every_assertion_expect_offers() -> None:
    """The gap this file is most likely to develop: a new assertion in
    ``expect.py`` and no row here. Nothing else would notice -- the new
    assertion's *semantics* get a test, and its message quietly gets none."""
    from wirespec.expect import LocatorAssertions, PageAssertions

    offered = {
        name
        for holder in (LocatorAssertions, PageAssertions)
        for name in vars(holder)
        if name.startswith(("to_", "not_to_"))
    }
    covered = {case[2] for case in FAILURES}
    assert offered - covered == set(), f"no message test for: {sorted(offered - covered)}"
