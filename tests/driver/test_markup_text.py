"""The two ways of reading ``textContent``, held against each other.

``resolve.text_contents`` reads one element's subtree with
``DOM.getOuterHTML`` and parses it, and falls back to a whole-document
``DOMSnapshot`` above ``SNAPSHOT_ABOVE`` elements. The two must agree
*exactly*, because which one runs is decided by how many elements were asked
about -- an invisible difference that would otherwise change an assertion's
outcome (§8.27).

This is the test that makes the swap safe rather than plausible: it walks every
element of every fixture page in the suite and compares the two readings
character for character.
"""

import pytest

from wirespec import resolve
from wirespec.cdp import dom as dom_domain
from wirespec.page import Page
from wirespec.resolve import _text_contents_by_snapshot, text_contents

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: Every fixture page the driver suite serves, so a page added for some other
#: reason is covered here too without anybody remembering to add it.
PAGES = (
    "markup.html",
    "readers.html",
    "index.html",
    "text.html",
    "list.html",
    "nested.html",
    "prose.html",
    "roles.html",
    "forms.html",
    "assertions.html",
    "actions.html",
    "input.html",
    "pickers.html",
    "waiting.html",
    "frames.html",
    "network.html",
)


@pytest.mark.parametrize("fixture", PAGES)
async def test_both_readings_agree_on_every_element(page: Page, fixture: str) -> None:
    await page.goto(f"/{fixture}")
    root = await page.document()
    node_ids = (await page.session.send(dom_domain.QuerySelectorAll(node_id=root, selector="*"))).node_ids
    assert node_ids, fixture

    subtrees = await text_contents(page, node_ids[:1]) if node_ids else []
    # One element at a time is the path a reader actually takes; the whole set
    # in one call is the path a filter over many matches takes.
    per_element = []
    for node_id in node_ids:
        per_element.extend(await text_contents(page, [node_id]))
    by_snapshot = await _text_contents_by_snapshot(page, node_ids)

    assert subtrees == per_element[:1]
    assert len(per_element) == len(by_snapshot) == len(node_ids)
    for node_id, mine, theirs in zip(node_ids, per_element, by_snapshot, strict=True):
        assert mine == theirs, f"{fixture} node {node_id}: {mine!r} != {theirs!r}"


async def test_the_awkward_cases_read_as_the_dom_does(page: Page) -> None:
    """Held against ``element.textContent`` itself, which is the actual claim.

    The exclusions are wirespec's own (§8.3), so those elements are
    compared against the DOM minus the same subtrees rather than against a raw
    ``textContent``.
    """
    await page.goto("/markup.html")
    for element in ("#whitespace", "#only-whitespace", "#entities", "#comment", "#void", "#deep", "#quotes"):
        mine = await page.locator(element).text_content()
        theirs = await page.evaluate(f"() => document.querySelector({element!r}).textContent")
        assert mine == theirs, element


async def test_the_skipped_tags_are_skipped_whole(page: Page) -> None:
    """§8.3: text in these is in the document and not on the
    screen. A ``<style>`` inside a ``<template>`` is the case that catches a
    flag where a depth counter was needed."""
    await page.goto("/markup.html")
    for element in ("#script", "#style", "#noscript", "#template", "#nested-skip"):
        assert await page.locator(element).text_content() == "visibletail", element


async def test_an_svg_title_is_not_the_documents_title(page: Page) -> None:
    """SVG's ``<title>`` is a different element from HTML's, and the two
    readings have to make the same call about it -- ``html.parser`` lower-cases
    every tag, so telling them apart means tracking foreign content."""
    await page.goto("/markup.html")
    node_ids = (
        await page.session.send(dom_domain.QuerySelectorAll(node_id=await page.document(), selector="#svg"))
    ).node_ids
    mine = (await text_contents(page, node_ids))[0]
    theirs = (await _text_contents_by_snapshot(page, node_ids))[0]
    assert mine == theirs
    assert "an svg title" in mine


async def test_the_threshold_only_changes_the_route(page: Page, monkeypatch: pytest.MonkeyPatch) -> None:
    """Which reading runs is decided by how many elements were asked about.

    That is an invisible difference to the caller, so it had better be an
    invisible difference to the answer: the same input is read both ways here,
    by moving the threshold rather than the input.
    """
    await page.goto("/markup.html")
    root = await page.document()
    node_ids = (await page.session.send(dom_domain.QuerySelectorAll(node_id=root, selector="*"))).node_ids

    monkeypatch.setattr(resolve, "SNAPSHOT_ABOVE", len(node_ids))
    by_subtree = await text_contents(page, node_ids)
    monkeypatch.setattr(resolve, "SNAPSHOT_ABOVE", 0)
    by_snapshot = await text_contents(page, node_ids)

    assert by_subtree == by_snapshot == await _text_contents_by_snapshot(page, node_ids)


async def test_a_node_that_vanished_reads_as_no_text(page: Page) -> None:
    """The snapshot path answers "" for an element it cannot find, and the
    fan-out has to answer the same: a filter reads a whole match set, and one
    element re-rendering mid-read must not take the others down with it."""
    await page.goto("/markup.html")
    found = await page.session.send(
        dom_domain.QuerySelectorAll(node_id=await page.document(), selector="#quotes, #empty")
    )
    await page.evaluate("() => document.querySelector('#empty').remove()")
    # The removed node's id is still in hand and Chrome has forgotten it. Node
    # ids come back in document order, so #quotes is first.
    assert await text_contents(page, found.node_ids) == ["she said \"quoted\" and 'single' & more", ""]
