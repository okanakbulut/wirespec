"""What ``DOMSnapshot.captureSnapshot`` calls rendered text, measured.

§12 carried "``inner_text`` is not ``element.innerText``" as open,
with this experiment named as the thing that closes it. It is closed: §9 now
records the numbers and §4.3 the divergence that survives.

These are not tests of wirespec -- ``inner_text`` is a step-4 reader and does
not exist yet. They pin the facts about *Chrome* that the decision rests on, so
that a Chrome upgrade which changes one of them fails here, naming it, rather
than quietly changing what a text assertion means.

Everything reads the snapshot through ``page.send`` rather than a wirespec
wrapper, because ``DOMSnapshot`` joins the subset when the resolution layer
needs it (§13 step 3) and not before.
"""

import pytest
import pytest_asyncio

from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: The three computed styles a faithful reconstruction needs, and no more.
STYLES = ["display", "visibility", "white-space"]


async def snapshot_of(page: Page) -> dict:
    return await page.send(
        "DOMSnapshot.captureSnapshot",
        {"computedStyles": STYLES, "includePaintOrder": False, "includeDOMRects": False},
    )


def index_by_id(snapshot: dict) -> dict[str, int]:
    strings = snapshot["strings"]
    nodes = snapshot["documents"][0]["nodes"]
    found: dict[str, int] = {}
    for index, attributes in enumerate(nodes["attributes"]):
        for name, value in zip(attributes[0::2], attributes[1::2], strict=True):
            if strings[name] == "id":
                found[strings[value]] = index
    return found


def runs_under(snapshot: dict, node_index: int) -> list[tuple[str, str, str, str]]:
    """Every text run under ``node_index``: (text, display, visibility, white-space)."""
    document = snapshot["documents"][0]
    strings, nodes, layout = snapshot["strings"], document["nodes"], document["layout"]
    subtree = {node_index}
    # parentIndex is in document order, so one forward pass closes the subtree.
    for index, parent in enumerate(nodes["parentIndex"]):
        if parent in subtree:
            subtree.add(index)
    collected = []
    for position, owner in enumerate(layout["nodeIndex"]):
        if owner not in subtree:
            continue
        text_index = layout["text"][position]
        if text_index < 0:
            continue
        style = [strings[v] if v >= 0 else "" for v in layout["styles"][position]]
        collected.append((strings[text_index], *style))
    return collected


def text_under(snapshot: dict, node_index: int) -> str:
    return "".join(run[0] for run in runs_under(snapshot, node_index))


@pytest_asyncio.fixture(loop_scope="session")
async def snapshot(page: Page):
    await page.goto("/text.html")
    return await snapshot_of(page), page


async def test_the_snapshot_gives_source_text_not_collapsed_text(snapshot) -> None:
    """The first surprise, and the reason the raw snapshot is not innerText:
    Chrome hands back each run's *source* text, whitespace and all."""
    taken, page = snapshot
    assert text_under(taken, index_by_id(taken)["collapse"]) == "   lots     of\n   space   "
    assert await page.evaluate("document.getElementById('collapse').innerText") == "lots of space"


async def test_a_block_boundary_produces_no_separator(snapshot) -> None:
    """The second, and the one that would silently corrupt an assertion:
    two block children run together into one word."""
    taken, page = snapshot
    assert text_under(taken, index_by_id(taken)["blocks"]) == "alphabeta"
    assert await page.evaluate("document.getElementById('blocks').innerText") == "alpha\nbeta"


async def test_computed_display_is_what_puts_the_boundary_back(snapshot) -> None:
    """And this is why the reconstruction asks for ``display``: the block boxes
    are in the snapshot, they just carry no text of their own."""
    taken, _ = snapshot
    document = taken["documents"][0]
    strings, layout = taken["strings"], document["layout"]
    subtree = index_by_id(taken)["blocks"]
    displays = [
        strings[layout["styles"][position][STYLES.index("display")]]
        for position, owner in enumerate(layout["nodeIndex"])
        if owner == subtree
    ]
    assert "block" in displays


async def test_visibility_hidden_text_is_present_and_flagged(snapshot) -> None:
    """innerText drops it; the snapshot keeps it and says it is hidden. That is
    the better arrangement -- the filter is one comparison, and the alternative
    would be text wirespec could not see at all."""
    taken, page = snapshot
    runs = runs_under(taken, index_by_id(taken)["visibility"])
    hidden = [text for text, _display, visibility, _ws in runs if visibility == "hidden"]
    assert hidden == ["gone"]
    assert await page.evaluate("document.getElementById('visibility').innerText") == "before  after"


async def test_a_whole_hidden_element_is_still_in_the_snapshot(snapshot) -> None:
    taken, page = snapshot
    runs = runs_under(taken, index_by_id(taken)["invisible"])
    assert all(visibility == "hidden" for _text, _display, visibility, _ws in runs)
    assert await page.evaluate("document.getElementById('invisible').innerText") == ""


async def test_a_br_arrives_as_a_run_holding_a_newline(snapshot) -> None:
    """Not as an empty box to infer a break from -- Chrome has already done it.
    The trap is that the newline is then *collapsible* whitespace to any naive
    normaliser, which turns the line break back into a space."""
    taken, _ = snapshot
    assert [run[0] for run in runs_under(taken, index_by_id(taken)["breaks"])] == ["one", "\n", "two"]


async def test_display_none_text_is_absent_altogether(snapshot) -> None:
    """The one thing both agree on, and the difference §4.3 said mattered."""
    taken, _ = snapshot
    assert "gone" not in text_under(taken, index_by_id(taken)["display"])


async def test_text_transform_is_already_applied(snapshot) -> None:
    """So there is no transform to reimplement. innerText does the same."""
    taken, page = snapshot
    assert text_under(taken, index_by_id(taken)["transform"]) == "SHOUTY"
    assert await page.evaluate("document.getElementById('transform').innerText") == "SHOUTY"


async def test_script_and_style_text_never_appears(snapshot) -> None:
    """They have no layout boxes, so the exclusion is free here -- unlike the
    text XPath in §8.3, where it is a predicate somebody has to
    have written on purpose."""
    taken, _ = snapshot
    assert text_under(taken, index_by_id(taken)["scripts"]) == "visible"


async def test_the_one_case_a_faithful_reconstruction_still_gets_wrong(snapshot) -> None:
    """The residual, measured and named (§4.3).

    Whitespace either side of a ``display:none`` element merges in innerText
    and does not in the snapshot: with no box there, Chrome's line layout joins
    the two runs, while the snapshot still reports the source runs separately.
    Telling this apart from the ``visibility:hidden`` case -- where innerText
    *also* keeps both spaces, and the snapshot is right -- needs the line-box
    structure rather than the text runs.

    It is a doubled space, never a wrong word, and every wirespec text matcher
    normalises whitespace before comparing (§4.2), so it cannot
    change the outcome of an assertion. It can only be seen by a spec reading
    ``inner_text()`` verbatim.
    """
    taken, page = snapshot
    both = index_by_id(taken)
    assert [run[0] for run in runs_under(taken, both["display"])] == ["before ", " after"]
    assert await page.evaluate("document.getElementById('display').innerText") == "before after"
    # Its twin, where the same two runs are correct.
    assert await page.evaluate("document.getElementById('visibility').innerText") == "before  after"
