"""The steps a locator chain is made of.

A locator is an immutable list of these (§4.1). They are ordinary
frozen Python values and **are never serialised anywhere**: since §3.4 there is
no page side to send them to, so the resolver simply interprets them. That is
also why a ``re.Pattern`` can sit in one untouched -- no pattern is
reconstructed, escaped or translated, because nothing crosses a language
boundary (§4.2).
"""

import re
from dataclasses import dataclass

__all__ = ["Attribute", "Css", "Frame", "Label", "Matcher", "Nth", "Or", "Role", "Step", "Text", "TextFilter"]

#: What a spec may hand a query for a name or a piece of text.
type Matcher = str | re.Pattern[str]


@dataclass(frozen=True, slots=True)
class Css:
    """Elements matching a CSS selector, within the previous step's matches."""

    selector: str

    def __str__(self) -> str:
        return f"locator({self.selector!r})"


@dataclass(frozen=True, slots=True)
class Role:
    """Elements Chrome gives this role, optionally with this accessible name.

    The most-used query in the validating suite by a wide margin, and the one
    whose semantics are *not* wirespec's: the role and the name are computed by
    the browser under test, so there is no ARIA implementation here to drift
    from it (§4.2).
    """

    role: str
    name: Matcher | None = None
    exact: bool = False

    def __str__(self) -> str:
        parts = [repr(self.role)]
        if self.name is not None:
            parts.append(f"name={self.name!r}")
        if self.exact:
            parts.append("exact=True")
        return f"get_by_role({', '.join(parts)})"


@dataclass(frozen=True, slots=True)
class Text:
    """Elements whose whole normalised text matches, innermost winning.

    §8.5. Not an accessibility question, so unlike ``Role`` there is
    no browser computation to defer to -- this is the one query wirespec has to
    define itself.
    """

    text: Matcher
    exact: bool = False

    def __str__(self) -> str:
        return f"get_by_text({self.text!r}{', exact=True' if self.exact else ''})"


@dataclass(frozen=True, slots=True)
class Label:
    """Form controls named by their ``<label>``, ``aria-label`` or
    ``aria-labelledby``.

    The accessible name again -- but only where it *came from* a label. Chrome
    returns the whole name cascade, so "named by its label" and "named by its
    own contents" are distinguishable rather than conflated.
    """

    text: Matcher
    exact: bool = False

    def __str__(self) -> str:
        return f"get_by_label({self.text!r}{', exact=True' if self.exact else ''})"


@dataclass(frozen=True, slots=True)
class Attribute:
    """Elements whose attribute matches -- what ``get_by_placeholder`` and
    ``get_by_test_id`` both are underneath."""

    name: str
    value: Matcher
    exact: bool = False
    #: How the step names itself in a failure message.
    label: str = "attribute"

    def __str__(self) -> str:
        return f"get_by_{self.label}({self.value!r}{', exact=True' if self.exact else ''})"


@dataclass(frozen=True, slots=True)
class TextFilter:
    """Keep (or drop) the matches whose text contains something.

    A refinement, not a query: it never adds an element, so a chain made only of
    these resolves to nothing rather than to the document (§4.1).
    """

    text: Matcher
    negate: bool = False

    def __str__(self) -> str:
        return f"filter(has_{'not_' if self.negate else ''}text={self.text!r})"


@dataclass(frozen=True, slots=True)
class Nth:
    """One of the matches, by index. Negative counts from the end."""

    index: int

    def __str__(self) -> str:
        return {0: "first", -1: "last"}.get(self.index, f"nth({self.index})")


@dataclass(frozen=True, slots=True)
class Frame:
    """Step into the document inside the matched ``<iframe>``.

    The only step that changes which *document* the ones after it search. It
    carries nothing, because everything it needs is the node it is applied to
    -- and it can be a step at all, rather than a second `Page`, because a
    same-process frame's nodes live in the page's own node-id space
    (§8.19).
    """

    def __str__(self) -> str:
        return "content_frame"


@dataclass(frozen=True, slots=True)
class Or:
    """Either what came before, or this other chain.

    How a spec waits for one of two outcomes without knowing which.
    """

    chain: tuple[Step, ...]

    def __str__(self) -> str:
        return "or_(" + " -> ".join(str(step) for step in self.chain) + ")"


#: Every step kind. A union rather than a base class, so a resolver that forgets
#: one is a type error rather than a silent fall-through to "match nothing".
type Step = Attribute | Css | Frame | Label | Nth | Or | Role | Text | TextFilter
