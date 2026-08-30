"""``textContent`` read back out of a serialised subtree.

§8.21 settled *what* an element's text is and left one way to get
it: ``DOMSnapshot.captureSnapshot``, because ``DOM.describeNode`` does not
report whitespace-only text nodes and a rebuild from it reads ``<b>a</b>
<b>b</b>`` as ``"ab"``. The snapshot does report them -- but it reports them for
the **whole document**, and the question is almost always about one element.
Measured on a 14k-node page: 58.2 ms for the snapshot against 0.13 ms for a
``DOM.getOuterHTML`` of the element, which carries the same whitespace because
it is the serialiser's output rather than the DOM agent's summary.

So this module is the other direction: markup in, text out. It is the parsing
half of ``resolve.text_contents``, and it exists to agree with
``rendered.source_text_by_backend_id`` exactly --
``tests/driver/test_markup_text.py`` holds the two against each other on every
element of every fixture page in the suite.

**Three things it must get right**, each found by a case that broke a draft:

* **Skipped subtrees are skipped whole.** ``<script>``, ``<style>`` and the rest
  of §8.3 hold text that is in the document and not on the screen.
  Counting depth rather than setting a flag is what keeps a ``<style>`` inside a
  ``<template>`` from re-enabling the text when the inner tag closes.
* **Foreign content spells its tags differently, and the DOM knows.** SVG's
  ``<title>`` is a different element from HTML's, and the snapshot skips only
  the second: it reads node names as ``TITLE`` and ``title``. The serialiser
  writes both as ``<title>``, so inside the markup this tracks ``<svg>`` and
  ``<math>`` to tell them apart -- and for the *root* element, where there is no
  enclosing tag to look at, it declines to guess. See ``root_tag`` below.
* **Comments are not text.** ``convert_charrefs`` folds entities into the data
  callback, which is what makes ``&amp;`` arrive as ``&``; comments and
  declarations come in on callbacks of their own and are simply not collected.
"""

from html.parser import HTMLParser
from typing import NamedTuple

__all__ = ["AMBIGUOUS_ROOTS", "INVISIBLE_TAGS", "Reading", "read"]

#: Text inside these is in the document and not on the screen (§8.3).
#: Spelled as the DOM spells them -- upper case, which is what an HTML
#: element's ``nodeName`` is and what a foreign element's is not.
INVISIBLE_TAGS = frozenset({"SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "TITLE"})

#: The same names as the serialiser writes them. A subtree whose *root* carries
#: one of these cannot be classified from its markup alone -- ``<title>`` is
#: the document's title in HTML and a tooltip in SVG, and Chrome serialises
#: both identically -- so the caller settles it from the DOM's own node name.
AMBIGUOUS_ROOTS = frozenset(tag.lower() for tag in INVISIBLE_TAGS)

#: Elements that take no end tag, so the parser must not expect one. It matters
#: for the skip counter: a void element inside a skipped subtree would
#: otherwise decrement a level it never opened, and the text after it would be
#: collected.
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

#: Inside one of these, tag names are the author's own rather than HTML's, and
#: an element sharing a name with an HTML one is not that element.
_FOREIGN = frozenset({"svg", "math"})


class Reading(NamedTuple):
    """What a subtree's markup said.

    ``root_tag`` is carried out because the caller may have to disambiguate it
    against the DOM -- see ``AMBIGUOUS_ROOTS``. It is the serialiser's spelling,
    which is lower case for everything.
    """

    root_tag: str
    text: str


class _Text(HTMLParser):
    """Collects the character data of a serialised subtree."""

    def __init__(self, skip: frozenset[str]) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = skip
        self._depth = 0
        self._foreign = 0
        self._parts: list[str] = []
        self.root_tag = ""
        self._seen_root = False

    def handle_starttag(self, tag: str, attrs: object) -> None:
        first = not self._seen_root
        if first:
            self._seen_root = True
            self.root_tag = tag
        if tag in _VOID:
            return
        if tag in _FOREIGN:
            self._foreign += 1
        elif self._depth:
            # Already inside a skipped subtree. Every open tag still has to be
            # counted, or its end tag closes the skip instead of itself.
            self._depth += 1
        elif not first and not self._foreign and tag.upper() in self._skip:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if tag in _FOREIGN:
            self._foreign = max(0, self._foreign - 1)
        elif self._depth:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._depth:
            self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def read(markup: str, skip: frozenset[str] = INVISIBLE_TAGS) -> Reading:
    """The ``textContent`` of the element ``markup`` is the serialisation of.

    ``skip`` is the tag names -- upper case, as the DOM spells them -- whose
    subtrees hold text that is not on the screen (§8.3). The root
    element is never skipped here even when it is one of them: from the markup
    alone there is no telling an HTML ``<title>`` from an SVG one, so that call
    belongs to the caller, which has the DOM's node name.
    """
    parser = _Text(skip)
    parser.feed(markup)
    parser.close()
    return Reading(parser.root_tag, parser.text)
