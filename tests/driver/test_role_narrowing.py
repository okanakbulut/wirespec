"""The narrowing that makes a role query cheap, and the rule it must obey.

§8.30. A role query confirms each candidate against the
accessibility tree, which is one message per candidate; where the caller also
gave a name, the document is asked first which candidates could possibly bear
it.

**That narrowing is a superset or it is nothing.** An element it misses is an
element the query silently cannot find -- and worse than a miss, strict mode
exists to report two matches where a spec expected one, so a narrowing that
drops the second match turns a failure into a pass. So the test that matters
here is not that narrowing is faster. It is that for every name on a page built
out of every way a name can be arrived at, the narrowed answer and the
exhaustive answer are the same list.
"""

import pytest

from wirespec import resolve
from wirespec.cdp import accessibility as ax_domain
from wirespec.cdp import dom as dom_domain
from wirespec.page import Page
from wirespec.roles import NARROWING

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def always_narrow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the narrowing on regardless of page size.

    It is off on a small page for a good reason -- the exhaustive confirm is
    already under two milliseconds there -- but that would leave every fixture
    in the suite testing the path that has not changed.
    """
    monkeypatch.setattr(resolve, "_NARROW_FLOOR", 0)
    monkeypatch.setattr(resolve, "_NARROW_COST", 0.0)


@pytest.fixture
def never_narrow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolve, "_NARROW_FLOOR", 1_000_000)


async def _every_role_and_name(page: Page) -> list[tuple[str, str]]:
    """What Chrome calls everything on the page, straight from the AX tree.

    The oracle is Chrome rather than the fixture's own markup: the question is
    what the *driver* must find, and that is whatever Chrome computed.
    """
    root = await page.document()
    node_ids = (await page.session.send(dom_domain.QuerySelectorAll(node_id=root, selector="*"))).node_ids
    described = await page.session.pipeline(
        [ax_domain.GetPartialAXTree(node_id=node_id, fetch_relatives=False) for node_id in node_ids]
    )
    found: list[tuple[str, str]] = []
    for reply in described:
        for node in reply.nodes:
            role = node.role.value if node.role else None
            name = node.name.value if node.name else None
            # Only the roles `get_by_role` can express. An `img` on this page
            # is Chrome's answer and not a query anybody can write here, and
            # `roles.py` refuses it by name rather than matching nothing.
            if isinstance(role, str) and role in NARROWING and isinstance(name, str) and name.strip():
                found.append((role, name))
            break
    return sorted(set(found))


async def test_the_narrowing_finds_exactly_what_the_full_scan_finds(
    page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole contract, over every name on the page.

    Each name is queried twice -- once with the narrowing on, once with it off
    -- and the two lists must be identical, in order. A page where several
    elements share a name and take it from different sources is the case this
    exists for.
    """
    await page.goto("/naming.html")
    pairs = await _every_role_and_name(page)
    assert len(pairs) > 15, f"the fixture should exercise many names, saw {len(pairs)}"

    for role, name in pairs:
        for exact in (True, False):
            locator = page.get_by_role(role, name=name, exact=exact)

            monkeypatch.setattr(resolve, "_NARROW_FLOOR", 1_000_000)
            everything = await page.resolve(locator.chain)

            monkeypatch.setattr(resolve, "_NARROW_FLOOR", 0)
            monkeypatch.setattr(resolve, "_NARROW_COST", 0.0)
            narrowed = await page.resolve(locator.chain)

            assert narrowed == everything, (
                f"get_by_role({role!r}, name={name!r}, exact={exact}) "
                f"narrowed to {narrowed} but the full scan found {everything}"
            )


async def test_a_name_shared_by_four_sources_finds_all_four(page: Page, always_narrow: None) -> None:
    """The concrete case, spelled out. Four textboxes named "Field name" -- by
    ``aria-label``, by ``<label for>``, by a wrapping ``<label>`` and by
    ``aria-labelledby``. A narrowing that reads only attributes finds the first
    and stops, and strict mode then passes where it should refuse."""
    await page.goto("/naming.html")
    found = await page.resolve(page.get_by_role("textbox", name="Field name", exact=True).chain)
    assert len(found) == 4


async def test_a_name_carried_by_a_nested_attribute_is_found(page: Page, always_narrow: None) -> None:
    """``<button><img alt="Save changes"></button>``. The attribute is on the
    descendant and the name is on the button, so the narrowing walks up from
    the hit -- asking XPath for ``descendant-or-self`` instead was measured at
    three times the cost."""
    await page.goto("/naming.html")
    found = await page.resolve(page.get_by_role("button", name="Save changes", exact=True).chain)
    ids = [await page.locator(f"#{name}").count() for name in ("b-nested-alt",)]
    assert ids == [1]
    assert len(found) >= 7


async def test_two_references_at_once_are_followed(page: Page, always_narrow: None) -> None:
    """``aria-labelledby`` is a space-separated *list*, which is why the
    narrowing looks for it with ``~=`` and not ``=``."""
    await page.goto("/naming.html")
    found = await page.resolve(page.get_by_role("textbox", name="Contract value", exact=True).chain)
    assert len(found) == 1


async def test_an_id_needing_escaping_is_followed(page: Page, always_narrow: None) -> None:
    """An id is whatever the author put in the attribute, quotes and
    backslashes included, and it reaches the narrowing as a CSS selector."""
    await page.goto("/naming.html")
    found = await page.resolve(page.get_by_role("textbox", name="Escaped label", exact=True).chain)
    assert len(found) == 1


async def test_a_regex_name_is_never_narrowed(page: Page, monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no XPath that expresses a Python regex, and a pattern matching
    fewer elements than it should would do it silently. So the narrowing
    declines and the confirm sees everything."""
    import re

    await page.goto("/naming.html")
    locator = page.get_by_role("textbox", name=re.compile(r"Field name"))

    monkeypatch.setattr(resolve, "_NARROW_FLOOR", 1_000_000)
    everything = await page.resolve(locator.chain)

    monkeypatch.setattr(resolve, "_NARROW_FLOOR", 0)
    monkeypatch.setattr(resolve, "_NARROW_COST", 0.0)
    assert await page.resolve(locator.chain) == everything
    assert len(everything) == 4


async def test_matching_is_case_insensitive_and_whitespace_folded(page: Page, always_narrow: None) -> None:
    """The narrowing and the confirm have to agree about what a match is, or
    the narrowing drops elements the confirm would have accepted."""
    await page.goto("/naming.html")
    assert len(await page.resolve(page.get_by_role("button", name="mixed case button").chain)) == 1
    assert len(await page.resolve(page.get_by_role("button", name="Wrapped across lines", exact=True).chain)) == 1


async def test_the_arithmetic_prefers_whichever_is_cheaper() -> None:
    """Narrowing costs a handful of searches over the whole document and the
    confirm costs one message per candidate, so which is cheaper depends on
    both numbers rather than on a threshold."""
    assert resolve._worth_narrowing(candidates=1440, nodes=14000) is True
    assert resolve._worth_narrowing(candidates=360, nodes=3500) is True
    # Few candidates on a big page: confirming them all is cheaper.
    assert resolve._worth_narrowing(candidates=80, nodes=14000) is False
    # Never below the floor, whatever the arithmetic says.
    assert resolve._worth_narrowing(candidates=10, nodes=10) is False


async def test_get_by_label_narrows_the_same_way(page: Page, monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_by_label`` narrows to a selector that is wider still -- every
    input, every button, everything claiming a name -- so it takes the same
    treatment, and owes the same superset guarantee."""
    await page.goto("/naming.html")
    for name in ("Field name", "Contract value", "Escaped label", "Search matters"):
        for exact in (True, False):
            locator = page.get_by_label(name, exact=exact)

            monkeypatch.setattr(resolve, "_NARROW_FLOOR", 1_000_000)
            everything = await page.resolve(locator.chain)

            monkeypatch.setattr(resolve, "_NARROW_FLOOR", 0)
            monkeypatch.setattr(resolve, "_NARROW_COST", 0.0)
            assert await page.resolve(locator.chain) == everything, f"{name!r} exact={exact}"


async def test_get_by_label_still_refuses_a_name_from_contents(page: Page, always_narrow: None) -> None:
    """The narrowing offers a button named by its own text as a candidate and
    the *confirm* is what turns it down -- which is the division of labour the
    whole design rests on. Conflating the two is what makes
    ``get_by_label("Save")`` find a Save button (§3.4).

    ``aria-label`` and ``aria-labelledby`` are label sources and are kept; the
    button whose name is simply its own text is not, and neither is the one
    named by a nested ``alt``.
    """
    await page.goto("/naming.html")
    found = await page.resolve(page.get_by_label("Save changes", exact=True).chain)
    kept = {await _id_of(page, node_id) for node_id in found}
    assert kept == {"b-aria", "b-labelledby"}, kept


async def _id_of(page: Page, node_id: int) -> str:
    reply = await page.session.send(dom_domain.GetAttributes(node_id=node_id))
    return dict(zip(reply.attributes[0::2], reply.attributes[1::2], strict=False)).get("id", "?")


# -- the known gap -----------------------------------------------------------


async def test_generated_content_is_the_gap_and_the_fallback_covers_the_common_half(
    page: Page, always_narrow: None, never_narrow: None
) -> None:
    """§12. ``::before { content: "Save " }`` puts text in the
    accessible name that is in no attribute and no text node, so no query over
    the document can see it.

    The fallback covers the half of it that matters: an element named *only*
    that way narrows to nothing, and nothing means confirm everything. This
    test pins both halves so the gap is recorded rather than discovered.
    """
    await page.goto("/naming.html")
    await page.evaluate(
        """
        () => {
          const rule = document.createElement('style');
          rule.textContent = '#generated::before { content: "Reset "; }';
          document.head.appendChild(rule);
          const button = document.createElement('button');
          button.id = 'generated';
          button.textContent = 'filters';
          document.body.appendChild(button);
        }
        """
    )
    page.invalidate()
    locator = page.get_by_role("button", name="Reset filters", exact=True)

    # Chrome computes the name, so the exhaustive confirm finds it.
    import wirespec.resolve as module

    module._NARROW_FLOOR = 1_000_000
    assert len(await page.resolve(locator.chain)) == 1

    # And so does the narrowing, because narrowing to nothing falls through.
    module._NARROW_FLOOR = 0
    module._NARROW_COST = 0.0
    assert len(await page.resolve(locator.chain)) == 1
