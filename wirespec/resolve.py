"""Turning a chain of steps into node ids, with CDP and no JavaScript.

Every question this file asks the page has a CDP answer -- §3.4 is
the table of which. The chain is walked in Python, each step searching within
the previous step's matches, and a chain of pure refinements with no query in it
resolves to nothing rather than to the document (§4.1).
"""

import asyncio
import re
from typing import TYPE_CHECKING

from wirespec.cdp import accessibility as ax_domain
from wirespec.cdp import dom as dom_domain
from wirespec.cdp import domsnapshot as snapshot_domain
from wirespec.errors import NODE_GONE, CDPError, WirespecError
from wirespec.markup import AMBIGUOUS_ROOTS, INVISIBLE_TAGS, read
from wirespec.matching import matches, normalise
from wirespec.rendered import source_text_by_backend_id
from wirespec.roles import selector_for
from wirespec.steps import Attribute, Css, Frame, Label, Matcher, Nth, Or, Role, Step, Text, TextFilter

if TYPE_CHECKING:
    from wirespec.page import Page

__all__ = ["INVISIBLE_TAGS", "resolve", "text_contents"]

#: Text inside these is in the document and not on the screen (§8.3). There
#: is no query root left to enforce that accidentally, so it is a
#: predicate somebody has to have written on purpose -- twice, because it is
#: needed in the XPath *and* again in the confirm.
#:
#: Defined in ``wirespec/markup.py``, which is the other thing that has to
#: agree about it, and re-exported here because this is where it reads as
#: belonging.

#: ``Node.nodeType`` for a text node, as the DOM spells it.
_TEXT_NODE = 3

#: The XPath half of §8.5. It narrows and is allowed to be
#: generous; the Python confirm below is what is exact.
#:
#: The exclusion appears twice on purpose. Once to stop a ``<script>`` matching
#: itself, and once *inside* the innermost predicate -- without the second, an
#: element whose only matching descendant is excluded counts as innermost and
#: matches. Measured on a fixture whose ``<title>`` holds the search text: with
#: the inner exclusion missing, ``<head>`` is a hit.
_NOT_INVISIBLE = "[not(self::script or self::style or self::noscript or self::template or self::title)]"

#: Whitespace Python's ``str.split`` collapses and XPath's ``normalize-space``
#: does not -- the twenty-five characters where ``c.isspace()`` is true and
#: ``c`` is not one of XPath's four. The list is written down rather than
#: computed, because computing it means walking the whole Unicode range at
#: import time for an answer that changes once a decade.
#:
#: They matter because the narrowing and the confirm have to agree about what
#: "one space" is. ``&nbsp;`` is the everyday one: an element reading
#: ``a b\xa0c`` normalises to ``a b c`` in Python, stays ``a b\xa0c`` under
#: ``normalize-space``, and a spec searching for the words it can see on the
#: screen finds nothing -- the under-approximation ``_contains`` says must
#: never happen (§8.21).
#: The widest safe narrowing: every element with any text in it at all. What a
#: predicate falls back to when XPath cannot express the question.
_ANY_TEXT = '[normalize-space(.) != ""]'

_OTHER_SPACE = (
    "\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


#: The steps that *find* elements. The rest -- ``TextFilter`` and ``Nth`` --
#: only ever remove them.
_QUERIES = (Attribute, Css, Label, Role, Text)


def _has_query(chain: tuple[Step, ...]) -> bool:
    """Does this chain ever ask for anything?

    §4.1: a chain of pure refinements resolves to nothing rather
    than to the document. Without this, ``locator.filter(has_text="Acme")`` with
    nothing in front of it starts at the document, finds that the document does
    contain "Acme", and returns the whole page as one match -- which then reads
    as a passing assertion.
    """
    return any(isinstance(step, _QUERIES) or (isinstance(step, Or) and _has_query(step.chain)) for step in chain)


async def resolve(page: Page, chain: tuple[Step, ...]) -> list[int]:
    """The node ids ``chain`` currently names."""
    if not _has_query(chain):
        return []
    roots = [await page.document()]
    for position, step in enumerate(chain):
        roots = await _apply(page, step, roots)
        if not roots and not any(isinstance(later, Or) for later in chain[position + 1 :]):
            # Stopping early is right for every step but one. `or_` offers an
            # alternative to the *whole chain so far*, so "what came before
            # matched nothing" is the normal way to reach it -- and a
            # short-circuit there makes `or_` answer 0 whenever the outcome
            # that arrived was the second one, which is half the time and
            # silent (§15.4; found by the differential suite).
            return []
    return roots


async def _apply(page: Page, step: Step, roots: list[int]) -> list[int]:
    match step:
        case Css(selector):
            return await _css(page, selector, roots)
        case Role(role, name, exact):
            return await _role(page, role, name, exact, roots)
        case Text(wanted, exact):
            return await _text(page, wanted, exact, roots)
        case Label(wanted, exact):
            return await _label(page, wanted, exact, roots)
        case Attribute(name, wanted, exact, _):
            return await _attribute(page, name, wanted, exact, roots)
        case TextFilter(wanted, negate):
            return await _filter(page, wanted, negate, roots)
        case Nth(index):
            return [roots[index]] if -len(roots) <= index < len(roots) else []
        case Frame():
            return await _frame(page, roots)
        case Or(other):
            # Resolved from the document, not from the current matches: `or_`
            # offers an alternative to the whole chain so far, not a refinement
            # of it.
            alternative = await resolve(page, other)
            seen = set(roots)
            return roots + [node_id for node_id in alternative if node_id not in seen]
    # Not reachable while `Step` is exhaustively matched above -- and if a step
    # kind is ever added without a branch here, this says so instead of
    # silently matching nothing (§5, "nothing fails silently").
    raise NotImplementedError(f"no resolver for {type(step).__name__}")


async def _role(
    page: Page,
    role: str,
    name: Matcher | None,
    exact: bool,
    roots: list[int],
) -> list[int]:
    """Narrow with CSS, confirm with the accessibility tree, pipelined.

    §3.4, in three lines of code and one design decision. The
    selector over-approximates (``wirespec/roles.py``); ``getPartialAXTree``
    says what Chrome actually calls each candidate; and every one of those calls
    goes out before any of them is awaited, because the connection multiplexes
    by design (§3.3).

    **The pipelining is not an optimisation, it is what makes the design
    viable.** Measured on this machine, 78 candidates: 2.0 ms pipelined against
    17.8 ms sequential. At two hundred candidates the sequential version is the
    difference between a query and a pause.

    ``fetch_relatives=False`` because the reply is then exactly one node -- the
    candidate -- so there is nothing to search for in it and no backend id to
    resolve first.
    """
    candidates = await _css(page, selector_for(role), roots)
    if not candidates:
        return []
    # §8.30. Where the role has many candidates and the caller
    # named one, ask the document which of them could *possibly* bear that name
    # before asking Chrome about every one of them. The narrowing is a superset
    # or it is nothing, and an empty answer falls through to the full confirm
    # below rather than being believed.
    if name is not None and _worth_narrowing(len(candidates), page.node_count):
        narrowed = await _possibly_named(page, candidates, name, exact=exact)
        if narrowed:
            confirmed = await _confirm(page, narrowed, role, name, exact=exact)
            if confirmed:
                return confirmed
    return await _confirm(page, candidates, role, name, exact=exact)


async def _confirm(
    page: Page,
    candidates: list[int],
    role: str,
    name: Matcher | None,
    *,
    exact: bool,
) -> list[int]:
    """Which of these does Chrome actually call by this role and name?

    The exact half of §3.4, and the only thing that ever decides a
    match. Whatever narrowed the candidates is allowed to be generous and is
    never allowed to be the answer.
    """
    described = await page.session.pipeline(
        [ax_domain.GetPartialAXTree(node_id=node_id, fetch_relatives=False) for node_id in candidates]
    )
    confirmed: list[int] = []
    for node_id, reply in zip(candidates, described, strict=True):
        for node in reply.nodes:
            if node.role is None or node.role.value != role:
                continue
            if name is not None and not matches(name, _name_of(node), exact=exact):
                continue
            confirmed.append(node_id)
            break
    return confirmed


#: What one accessibility confirm costs, in microseconds, and what the
#: narrowing costs per node of the document. Both measured on this machine
#: (§8.30); both are only ever used to decide which of the two is
#: cheaper, so being roughly right is the whole requirement.
_CONFIRM_COST = 24.0
_NARROW_COST = 0.75

#: Never narrow below this many candidates, whatever the arithmetic says: the
#: confirm is already under two milliseconds there and always right, and the
#: narrowing has a fixed cost of several round trips.
_NARROW_FLOOR = 64


def _worth_narrowing(candidates: int, nodes: int) -> bool:
    """Is narrowing cheaper than confirming everything?

    The two scale against different things, which is why this is arithmetic
    rather than a constant: the confirm costs one message per **candidate**,
    and the narrowing costs a handful of XPath searches over the whole
    **document**. So a page with three hundred buttons in fourteen thousand
    nodes wants narrowing and the same three hundred buttons in a thousand
    nodes does not.

    ``nodes`` is read off the document map the resolver already holds, so
    deciding costs nothing.
    """
    if candidates < _NARROW_FLOOR:
        return False
    return candidates * _CONFIRM_COST > nodes * _NARROW_COST


#: Attributes that can supply an accessible name on their own. ``label`` is
#: here for ``<optgroup>`` and ``<option>``; ``value`` for the button-shaped
#: inputs, whose name is what is printed on them.
_NAMING_ATTRIBUTES = ("aria-label", "title", "alt", "placeholder", "value", "label")


async def _possibly_named(
    page: Page,
    candidates: list[int],
    wanted: Matcher,
    *,
    exact: bool,
) -> list[int] | None:
    """Which candidates could bear this accessible name, without asking Chrome.

    §8.30. **A superset or nothing**: every element whose name
    really does match must survive, or the query silently answers about fewer
    elements than it should -- and strict mode, which exists to report two
    matches where a spec expected one, would report the one it was allowed to
    see. ``None`` means "cannot say", and the caller confirms everything.

    An accessible name comes from a short list of places, and this covers the
    ones a document can be asked about:

    ==========================  ==============================================
    ``aria-label`` and friends  an attribute of the element (``_NAMING_ATTRIBUTES``)
    a nested ``alt``/``value``  the same attributes on a descendant
    contents                    the element's own text, which XPath already
                                folds over the subtree
    ``<label for>``, wrapping   found by searching for the *label*, then
    ``<label>``                 following it to what it labels
    ``aria-labelledby``         found by searching for the *referent*, then
                                following the reference back
    ==========================  ==============================================

    The last two are why this is four searches rather than one. Expressed as
    joins inside a single XPath they are quadratic -- **2 090 ms on a 3 600-node
    page and over 25 000 ms at 14 000**, because XPath 1.0 has no index for a
    cross-reference. Asked separately they are nearly free, because a page has
    few labels whose text matches anything.

    **One source is not covered and cannot be**: CSS generated content.
    ``::before { content: "Delete " }`` puts text in the name that is in no
    attribute and no text node, so no query over the document can see it. That
    is §12's newest known gap, and the fallback narrows it: an
    element named *only* that way makes this return nothing, and nothing means
    the caller confirms everything.
    """
    if isinstance(wanted, re.Pattern):
        # There is no XPath that expresses a Python regex, and a pattern that
        # matched fewer elements than it should would do it silently.
        return None
    needle = normalise(wanted)
    if not needle:
        return None
    holds = _holds(needle, ".", exact=exact)
    if holds is None:
        # The case fold is not one-to-one, so `translate` cannot express it.
        return None

    attributes = " or ".join(f"@{attribute}[{holds}]" for attribute in _NAMING_ATTRIBUTES)
    named, texted, labels, referents = await asyncio.gather(
        # An attribute of the element itself. Its *descendants'* attributes
        # matter too -- a button named by a nested `<img alt>` -- and those are
        # reached by walking up from the hit rather than by asking XPath for
        # `descendant-or-self`, which was measured at three times the cost.
        _search(page, f"//*[{attributes}]"),
        # Contents. An element's XPath string value already spans its subtree,
        # so this returns the ancestors as well and needs no walk.
        _search(page, f"//*[{holds}]"),
        _search(page, f"//label[{holds}]"),
        _search(page, f"//*[@id][{holds}]"),
    )

    possible = set(texted)
    for node_id in named:
        possible.add(node_id)
        # The walk up is what covers a name carried by a descendant's
        # attribute. A node the map has never heard of has no ancestry here,
        # and guessing at it is exactly the silent under-approximation this
        # function must not make -- so say nothing instead.
        if not page.knows_node(node_id):
            return None
        possible.update(page.ancestor_ids(node_id))

    if labels:
        possible.update(await _labelled_by(page, labels))
    if referents:
        possible.update(await _referred_to_by(page, referents))

    # Filtered from `candidates` rather than intersected as a set, so the
    # document order the confirm depends on survives.
    return [node_id for node_id in candidates if node_id in possible]


async def _labelled_by(page: Page, labels: list[int]) -> set[int]:
    """What these ``<label>`` elements name.

    Both spellings: ``for`` pointing at an id, and a label wrapped around its
    control. Reached from the label rather than from the control because that
    is the direction a document can be searched in cheaply.
    """
    attributes, wrapped = await asyncio.gather(
        page.session.pipeline([dom_domain.GetAttributes(node_id=node_id) for node_id in labels]),
        page.session.pipeline(
            [dom_domain.QuerySelectorAll(node_id=node_id, selector=_LABELLABLE) for node_id in labels]
        ),
    )
    found: set[int] = set()
    for reply in wrapped:
        found.update(reply.node_ids)
    targets = [
        value
        for reply in attributes
        if (value := dict(zip(reply.attributes[0::2], reply.attributes[1::2], strict=False)).get("for"))
    ]
    found.update(await _by_id(page, targets))
    return found


async def _referred_to_by(page: Page, referents: list[int]) -> set[int]:
    """Elements whose ``aria-labelledby`` points at one of these.

    ``~=`` and not ``=``: ``aria-labelledby`` is a space-separated list of ids,
    and an element named by two of them would be missed by an equality test.
    """
    attributes = await page.session.pipeline([dom_domain.GetAttributes(node_id=node_id) for node_id in referents])
    ids = [
        value
        for reply in attributes
        if (value := dict(zip(reply.attributes[0::2], reply.attributes[1::2], strict=False)).get("id"))
    ]
    if not ids:
        return set()
    selector = ",".join(f'[aria-labelledby~="{_css_string(identifier)}"]' for identifier in ids)
    return set(await _css(page, selector, [await page.document()]))


async def _by_id(page: Page, identifiers: list[str]) -> set[int]:
    """The elements carrying these ids, in one query."""
    if not identifiers:
        return set()
    selector = ",".join(f'[id="{_css_string(identifier)}"]' for identifier in identifiers)
    return set(await _css(page, selector, [await page.document()]))


def _css_string(value: str) -> str:
    """``value`` inside a double-quoted CSS string. An id is whatever the author
    put in the attribute, quotes and backslashes included."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _name_of(node: ax_domain.AXNode) -> str:
    """The accessible name Chrome computed, as a string.

    ``AXValue.value`` is typed ``Any`` because CDP uses one shape for names,
    booleans and numbers alike; a name that is not a string is a name of "".
    """
    if node.name is None or not isinstance(node.name.value, str):
        return ""
    return node.name.value


#: What a name may have come from for ``get_by_label`` to accept it. The
#: cascade Chrome returns also holds ``contents``, ``title`` and ``placeholder``
#: -- a button named by its own text is not a labelled control, and conflating
#: the two is what makes ``get_by_label("Save")`` find a Save button.
_LABEL_SOURCES = frozenset({"label", "labelfor", "labelwrapped"})

#: Anything that can carry a label, plus anything claiming a name outright. Over
#: -approximates, like every narrowing selector here (§3.4).
_LABELLABLE = (
    "input, textarea, select, button, meter, output, progress, [contenteditable], [aria-label], [aria-labelledby]"
)


async def _label(page: Page, wanted: Matcher, exact: bool, roots: list[int]) -> list[int]:
    """Named *by a label*, which is not the same as named at all.

    ``getPartialAXTree`` returns the whole name cascade -- every candidate
    source, which won, and which were superseded -- so this can ask where the
    name came from rather than guessing. That cascade is the same material a
    failure message wants (§3.4), and it is already in hand.
    """
    candidates = await _css(page, _LABELLABLE, roots)
    if not candidates:
        return []
    # The same narrowing the role query uses, and for the same reason: the
    # selector above is wide -- every input, every button, everything claiming a
    # name -- so on a form-heavy page this is a message per control
    # (§8.30). ``_possibly_named`` covers more sources than
    # ``_LABEL_SOURCES`` accepts, which is the harmless direction: the extra
    # candidates reach the confirm and it turns them down.
    if _worth_narrowing(len(candidates), page.node_count):
        narrowed = await _possibly_named(page, candidates, wanted, exact=exact)
        if narrowed:
            found = await _confirm_label(page, narrowed, wanted, exact=exact)
            if found:
                return found
    return await _confirm_label(page, candidates, wanted, exact=exact)


async def _confirm_label(page: Page, candidates: list[int], wanted: Matcher, *, exact: bool) -> list[int]:
    """Which of these does Chrome say is named by a label, and by this one?"""
    described = await page.session.pipeline(
        [ax_domain.GetPartialAXTree(node_id=node_id, fetch_relatives=False) for node_id in candidates]
    )
    found: list[int] = []
    for node_id, reply in zip(candidates, described, strict=True):
        for node in reply.nodes:
            if node.name is None or not _named_by_a_label(node):
                continue
            if matches(wanted, _name_of(node), exact=exact):
                found.append(node_id)
            break
    return found


def _named_by_a_label(node: ax_domain.AXNode) -> bool:
    for source in (node.name.sources if node.name else None) or ():
        if source.superseded or source.value is None:
            continue
        if source.native_source in _LABEL_SOURCES:
            return True
        if source.attribute in ("aria-label", "aria-labelledby"):
            return True
    return False


async def _attribute(page: Page, name: str, wanted: Matcher, exact: bool, roots: list[int]) -> list[int]:
    """Elements whose attribute matches. The CSS narrows to "has it at all"."""
    candidates = await _css(page, f"[{name}]", roots)
    if not candidates:
        return []
    described = await page.session.pipeline([dom_domain.GetAttributes(node_id=node_id) for node_id in candidates])
    found: list[int] = []
    for node_id, reply in zip(candidates, described, strict=True):
        pairs = dict(zip(reply.attributes[0::2], reply.attributes[1::2], strict=True))
        value = pairs.get(name)
        if value is not None and matches(wanted, value, exact=exact):
            found.append(node_id)
    return found


async def _filter(page: Page, wanted: Matcher, negate: bool, roots: list[int]) -> list[int]:
    """Keep the matches whose visible text does (or does not) contain something.

    Uses the same reading as the text query and as ``text_content()``, and the
    same exclusions, so the three cannot disagree about what an element's text
    is.
    """
    if not roots:
        return []
    return [
        node_id
        for node_id, text in zip(roots, await text_contents(page, roots), strict=True)
        if matches(wanted, text, exact=False) is not negate
    ]


async def _text(page: Page, wanted, exact: bool, roots: list[int]) -> list[int]:
    """§8.5, in two halves: XPath narrows, Python confirms.

    The XPath cannot be the whole answer, because an element's XPath
    string-value includes the text of *every* descendant -- so ``<head>``
    "contains" the title's text and a ``<div>`` holding a ``<script>`` that
    mentions the text "contains" it too. Both were measured, and both are false
    positives the confirm removes. Which is the same shape as the role query
    (§3.4): narrow with something cheap, confirm with something
    exact, and let the narrowing be generous.
    """
    # There is no XPath that expresses a Python regex, so a pattern narrows to
    # "every innermost element bearing any text" and lets the confirm decide --
    # the same fallback `_contains` already takes for a needle whose case
    # mapping is not one-to-one. Affordable only since the confirm became one
    # `DOMSnapshot` for the whole candidate set rather than a `describeNode`
    # each (§8.21); before that it was hundreds of round trips.
    holds = _ANY_TEXT if isinstance(wanted, re.Pattern) else _contains(normalise(wanted), exact=exact)
    query = f"//*{_NOT_INVISIBLE}{holds}[not(.//*{_NOT_INVISIBLE}{holds})]"
    candidates = await _search(page, query)
    # `performSearch` has no root: it searches every document in the target,
    # and that includes every same-process frame (§8.19 --
    # measured, four hits for one button on a page with three frames). So the
    # scoping below is not only for a text step part way down a chain; a text
    # step at the *head* of one has to be scoped to the main document too, or
    # `page.get_by_text` quietly answers for the frames as well.
    #
    # Skipped only when it provably cannot matter: a page with no frame in it
    # has nothing for the search to leak from, and `page.framed` is read off
    # the `getDocument` reply the resolver already had (§5.1). So
    # the common case still costs exactly one round trip.
    if roots != [await page.document()] or page.framed:
        candidates = await _within(page, candidates, roots)
    if not candidates:
        return []
    confirmed = [
        node_id
        for node_id, text in zip(candidates, await text_contents(page, candidates), strict=True)
        if matches(wanted, text, exact=exact)
    ]
    # Innermost again, now that the false positives are gone: an ancestor whose
    # only matching descendant survived the confirm is not the innermost match.
    return confirmed


def _holds(needle: str, expression: str = ".", *, exact: bool) -> str | None:
    """The XPath condition that ``expression``'s string value may hold
    ``needle``, or ``None`` where XPath 1.0 cannot express it.

    **It must over-approximate and must never under-approximate**
    (§3.4). A candidate the condition misses is an element the
    query silently cannot find; one it wrongly includes costs a confirm and is
    dropped by it.

    ``exact`` compares case-sensitively after whitespace normalisation, so a
    plain ``contains()`` is already a safe superset. The default -- a
    case-insensitive substring -- is not: XPath's ``contains()`` is
    case-sensitive, and searching for "acme" this way finds nothing on a page
    that says "Acme". It was measured doing exactly that.

    XPath 1.0's only case tool is ``translate()``, which folds character by
    character. Building its table from the *needle* rather than from the ASCII
    alphabet is what makes it work for Greek, Cyrillic and accented Latin as
    well. Where a character's case mapping is not one-to-one -- ``ﬁ`` uppercases
    to two characters -- ``translate`` cannot express it, and this says ``None``
    rather than guessing. Slower, correct, and rare.

    ``expression`` is a parameter because the same question is asked of three
    different things: an element's own string value, one of its attributes, and
    -- through the caller -- a ``<label>``'s text (§8.30).
    """
    if exact:
        return f"contains(normalize-space({_spaced(expression)}), {_xpath_literal(needle)})"
    folded = needle.lower()
    upper = "".join(sorted({character.upper() for character in folded}))
    if any(len(character) != 1 for character in upper) or len(upper) != len(set(upper)):
        return None
    lower = "".join(character.lower() for character in upper)
    if any(len(character) != 1 for character in lower):
        return None
    # The case fold and the whitespace fold in one `translate`, and the
    # `normalize-space` **outside** it: folding first turns `a \xa0 b` into
    # three spaces, and only a normalise after that collapses them to the one
    # Python's `normalise` produced.
    table = upper + _OTHER_SPACE
    return (
        f"contains(normalize-space(translate({expression}, {_xpath_literal(table)}, "
        f"{_xpath_literal(lower + ' ' * len(_OTHER_SPACE))})), {_xpath_literal(folded)})"
    )


def _contains(needle: str, *, exact: bool) -> str:
    """``_holds`` as a predicate, with the text query's own fallback.

    Where the case fold is not expressible, the text query narrows to "every
    innermost element bearing any text" and lets the confirm do the work; the
    role query has a different answer to the same problem, which is to stop
    narrowing at all (``_possibly_named``).
    """
    condition = _holds(needle, ".", exact=exact)
    return _ANY_TEXT if condition is None else f"[{condition}]"


def _spaced(expression: str) -> str:
    """``expression`` with the whitespace XPath does not know about folded to
    plain spaces, ready for ``normalize-space``."""
    return f"translate({expression}, {_xpath_literal(_OTHER_SPACE)}, {_xpath_literal(' ' * len(_OTHER_SPACE))})"


#: Above this many elements, read the document once instead of each subtree.
#:
#: The two readings agree exactly (``tests/driver/test_readers.py``), so this is
#: only ever about which is cheaper, and they scale against each other:
#: ``getOuterHTML`` costs one message and one parse per element, the snapshot
#: costs one message and one pass over the *whole document* however many
#: elements are asked about. Measured on a 14k-node page, one element: 0.13 ms
#: against 58.2 ms. The crossover is where the subtrees add up to the document,
#: which for a page whose elements nest -- and they all do -- comes well before
#: the element count does.
#:
#: Chosen from the measurement in §8.27 rather than derived: below
#: it the fan-out wins on every page shape tried, above it the pathological
#: shape (``locator("div").all_text_contents()`` on deeply nested divs, where
#: each subtree is most of the document) starts to matter.
SNAPSHOT_ABOVE = 64


async def text_contents(page: Page, node_ids: list[int]) -> list[str]:
    """``textContent`` for each node, minus the tags whose text is not on the
    screen (§8.3).

    **Whitespace-only text nodes are the whole constraint here.**
    ``DOM.describeNode`` does not report them at all, so a rebuild from the DOM
    tree reads ``<b>a</b> <b>b</b>`` as ``"ab"`` -- two words run into one, with
    no space left for normalisation to put back (§8.21). That left
    two sources that do report them, and they cost very differently:

    ``DOM.getOuterHTML``   one message per element, over its subtree
    ``DOMSnapshot``        one message, over the entire document

    The snapshot was the original answer and is the wrong shape for the
    question, which is almost always about one element: 58.2 ms against 0.13 ms
    on a 14k-node page. So the fan-out is the default and the snapshot is what
    a genuinely document-sized read falls back to (``SNAPSHOT_ABOVE``).

    One function for the reader, the filter and the text query, so the three
    cannot disagree about what an element's text is.
    """
    if not node_ids:
        return []
    if len(node_ids) > SNAPSHOT_ABOVE:
        return await _text_contents_by_snapshot(page, node_ids)
    markup = await page.session.pipeline(
        [dom_domain.GetOuterHTML(node_id=node_id) for node_id in node_ids], return_exceptions=True
    )
    found: list[str] = []
    for node_id, reply in zip(node_ids, markup, strict=True):
        if isinstance(reply, BaseException):
            # A node the resolver named a moment ago and Chrome no longer knows
            # has no text, which is what the snapshot path answers for it too --
            # it simply would not have found the backend id. Keeping the two
            # identical matters more here than raising would: `_filter` reads a
            # whole match set, and one element re-rendering mid-read must not
            # take the other answers down with it.
            if isinstance(reply, CDPError) and NODE_GONE in reply.message:
                found.append("")
                continue
            raise reply
        reading = read(reply.outer_html, INVISIBLE_TAGS)
        # The subtree's own root is the one tag the markup cannot classify: an
        # HTML `<title>` and an SVG one are both serialised `<title>`, and only
        # the first is invisible. Settled from the DOM's node name, which the
        # document map already holds (wirespec/markup.py).
        if reading.root_tag in AMBIGUOUS_ROOTS and await page.is_invisible_element(node_id):
            found.append("")
            continue
        found.append(reading.text)
    return found


async def _text_contents_by_snapshot(page: Page, node_ids: list[int]) -> list[str]:
    """The document-sized reading, for when the subtrees would add up to it."""
    backends = await page.backend_ids(node_ids)
    # No computed styles: this is the source text, and none of it depends on
    # how the element is painted.
    snapshot = await page.session.send(snapshot_domain.CaptureSnapshot(computed_styles=[]))
    found = source_text_by_backend_id(snapshot, set(backends), INVISIBLE_TAGS)
    return [found.get(backend, "") for backend in backends]


async def _search(page: Page, query: str) -> list[int]:
    """One XPath search, and the discard that must follow it.

    ``performSearch`` allocates, and a polling assertion that forgets to free it
    leaks one allocation per iteration for the life of the page
    (§8.5).
    """
    found = await page.session.send(dom_domain.PerformSearch(query=query))
    try:
        if not found.result_count:
            return []
        results = await page.session.send(
            dom_domain.GetSearchResults(search_id=found.search_id, from_index=0, to_index=found.result_count)
        )
        return results.node_ids
    finally:
        await page.session.send(dom_domain.DiscardSearchResults(search_id=found.search_id))


#: Elements that can hold a document. ``FRAME`` is the pre-HTML5 spelling and
#: costs nothing to accept; ``OBJECT`` and ``EMBED`` are deliberately absent,
#: because neither has been measured here and a guess that resolves to the wrong
#: document is exactly the silent failure §5 rules out.
_FRAME_TAGS = frozenset({"IFRAME", "FRAME"})


def _attributes(node: dom_domain.Node) -> dict[str, str]:
    """CDP's flat ``[name, value, name, value, …]`` as a mapping."""
    if not node.attributes:
        return {}
    return dict(zip(node.attributes[::2], node.attributes[1::2], strict=False))


def describe(node: dom_domain.Node) -> str:
    """The element as a person would point at it: ``IFRAME#widget``.

    Only for failure messages, and it has to be enough to find the element in
    the page's source -- a message naming "an iframe" on a page with four of
    them has not said anything.
    """
    identifier = _attributes(node).get("id") or _attributes(node).get("name")
    return f"{node.node_name}#{identifier}" if identifier else node.node_name


async def _frame(page: Page, roots: list[int]) -> list[int]:
    """The documents inside these frame elements, as query roots.

    One ``describeNode`` per frame, and no cache. The document map is not used
    on purpose: it is refreshed only when the *main* frame navigates, so a child
    frame that navigated on its own would leave a root that Chrome has already
    forgotten -- and this step is what everything after it searches, so a stale
    answer here is a whole chain resolved against the wrong document.

    ``contentDocument`` comes back on a plain ``describeNode``; ``pierce`` was
    measured and makes no difference to it, so it is not sent (§9).

    **Two of the three states a young frame passes through are not answers**
    (§8.19). Measured on a freshly inserted ``<iframe>``, over
    about five milliseconds: its ``contentDocument`` arrives with ``nodeId: 0``,
    then the frame element's *own* node id is briefly unbound while the real
    document commits, then it settles. Neither of those is an error and neither
    is "no such frame" -- they are "not yet", and the honest answer to "what
    does this chain name **now**" is nothing, which is what the wait loop is
    for. Raising instead turns a five-millisecond window into a failed test one
    run in eight, which is exactly how this was found.
    """
    described = await page.session.pipeline(
        [dom_domain.DescribeNode(node_id=node_id) for node_id in roots], return_exceptions=True
    )
    documents: list[int] = []
    for reply in described:
        if isinstance(reply, BaseException):
            # Only the transient above. A node id a query returned a moment ago
            # and Chrome no longer knows is a node that no longer matches, which
            # is a result and not a fault -- the same reading `_css` would have
            # given had it run an instant later. Anything else is a real fault
            # and is re-raised (§5, "nothing fails silently").
            if isinstance(reply, CDPError) and NODE_GONE in reply.message:
                continue
            raise reply
        node = reply.node
        if node.node_name not in _FRAME_TAGS:
            raise WirespecError(
                f"{describe(node)} has no document inside it: content_frame steps into an "
                f"<iframe>, and this element is a {node.node_name}."
            )
        if node.content_document is None:
            # Measured: the *only* way a frame element has no contentDocument at
            # all is that Chrome put it in another renderer process, which it
            # does per site. Its nodes are not in this session's node-id space
            # and nothing here can reach them (§8.19). A frame that
            # is merely young reports `nodeId: 0` instead, never `null`, which is
            # what keeps this from accusing a same-origin frame of being remote.
            source = _attributes(node).get("src", "an unknown URL")
            raise WirespecError(
                f"{describe(node)} is cross-origin: it loads {source}, which Chrome runs in its "
                f"own renderer process, and its document is not in this page's node-id space. "
                f"Out-of-process frames are a known gap (§8.19)."
            )
        if node.content_document.node_id:
            documents.append(node.content_document.node_id)
    return documents


async def _within(page: Page, candidates: list[int], roots: list[int]) -> list[int]:
    """Keep only the candidates inside one of ``roots``.

    ``performSearch`` has no root parameter -- it searches the whole document --
    so a text step that is not the first in its chain has to be scoped
    afterwards. One ``querySelectorAll(root, "*")`` per root answers it, and the
    common case (a text query at the head of a chain) never gets here at all.
    """
    inside: set[int] = set()
    for root in roots:
        result = await page.session.send(dom_domain.QuerySelectorAll(node_id=root, selector="*"))
        inside.update(result.node_ids)
    return [node_id for node_id in candidates if node_id in inside]


def _xpath_literal(text: str) -> str:
    """A string XPath 1.0 will accept, including one holding both quote kinds.

    XPath 1.0 has no escape character, so the only way to write both quotes is
    ``concat()``. A spec locating something by a quoted phrase is not exotic --
    page objects find things by real typographic quotes -- and getting this
    wrong is a malformed query, not a wrong answer, so it fails loudly. It is
    still worth not failing.
    """
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    pieces: list[str] = []
    for index, piece in enumerate(text.split("'")):
        if index:
            pieces.append('"\'"')
        if piece:
            pieces.append(f"'{piece}'")
    return "concat(" + ",".join(pieces) + ")"


async def _css(page: Page, selector: str, roots: list[int]) -> list[int]:
    found: list[int] = []
    seen: set[int] = set()
    for root in roots:
        result = await page.session.send(dom_domain.QuerySelectorAll(node_id=root, selector=selector))
        for node_id in result.node_ids:
            if node_id not in seen:
                seen.add(node_id)
                found.append(node_id)
    return found
