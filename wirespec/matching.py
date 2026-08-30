"""How a wanted string is compared with what Chrome said.

Matching happens **here**, in Python, against names and text the browser has
already computed (§4.2). That is the clearest dividend of resolving
outside the page: a ``re.Pattern`` is simply used -- full Python semantics,
every flag, no ``new RegExp`` reconstruction, no flag subset to document, and no
escaping of plain strings into patterns they were never meant to be.

| the author writes | how it is matched |
|---|---|
| ``name="Save"`` | case-insensitive substring |
| ``name="Save", exact=True`` | equality, after whitespace normalisation |
| ``name=re.compile(...)`` | ``pattern.search``, with the author's own flags |
"""

import re

from wirespec.steps import Matcher

__all__ = ["asserted", "matches", "normalise"]

_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Runs of whitespace to one space, ends trimmed.

    What "after whitespace normalisation" means everywhere in wirespec. Markup
    wraps and indents; a spec's author wrote one line.
    """
    return _WHITESPACE.sub(" ", text).strip()


def matches(wanted: Matcher, actual: str, *, exact: bool) -> bool:
    """Does ``actual`` satisfy ``wanted``?

    ``exact`` is ignored for a pattern, because a pattern already says exactly
    what it wants: honouring ``exact`` there would mean anchoring somebody
    else's regex, which is a different regex.
    """
    if isinstance(wanted, re.Pattern):
        return wanted.search(actual) is not None
    if exact:
        return normalise(actual) == normalise(wanted)
    # ``lower``, not ``casefold``. The two disagree on a handful of characters
    # -- ``casefold`` maps ``ß`` to ``ss`` -- and the text query narrows with an
    # XPath ``translate()``, which can only do simple one-to-one case mapping.
    # If this were stricter than the narrowing, matching would be correct; if it
    # were looser, the narrowing would silently drop elements this would have
    # accepted. Agreeing exactly is the only arrangement with no gap in it.
    return normalise(wanted).lower() in normalise(actual).lower()


def asserted(wanted: Matcher, actual: str, *, whole: bool, ignore_case: bool) -> bool:
    """How an *assertion* compares, which is not how a *query* matches.

    A query's default is a case-insensitive substring, because a spec locating
    "save" should find a button labelled "Save". An assertion's default is
    case-sensitive, because a spec claiming the heading reads "Save" is claiming
    exactly that -- and Playwright draws the line in the same place, so
    diverging here would be a silent difference on the two most-used assertions
    in any suite.

    ``whole`` is the difference between ``to_have_text`` and ``to_contain_text``.
    Both normalise whitespace first: markup wraps and indents, and the author
    wrote one line.
    """
    if isinstance(wanted, re.Pattern):
        return wanted.search(actual) is not None
    left, right = normalise(wanted), normalise(actual)
    if ignore_case:
        left, right = left.lower(), right.lower()
    return left == right if whole else left in right
