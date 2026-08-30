"""Retrying assertions, and the messages they fail with.

``expect(locator).to_have_count(3)`` reads the page until the claim is true or
the deadline passes. The loop is §5.1's, shared with the readers:
read first so an assertion already true costs exactly one round trip, wake on a
DOM mutation rather than on a timer, and **keep the last reading** so the
failure says ``expected 9, last saw 3`` rather than ``timed out``.

Two conventions are contract rather than accident (§5.3):

* **Single-element assertions are strict.** ``to_be_visible`` on a locator
  matching three elements fails and says so. A locator matching more than it
  meant to is a bug that otherwise surfaces much later, in a different test.
* **Text assertions over many elements are satisfied by any of them**, and their
  negations by none of them. Pass a list instead and the comparison becomes
  positional, which is how a spec asserts an order.

Every assertion has a ``not_to_*`` counterpart, which polls until the condition
is *false* rather than asserting once — an important difference: it waits for
something to go away rather than checking that it has not arrived yet.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, TypeIs, overload

from wirespec.errors import WirespecTimeoutError
from wirespec.locator import Locator
from wirespec.matching import asserted, normalise
from wirespec.retry import poll
from wirespec.steps import Matcher
from wirespec.timeouts import DEFAULT_ASSERTION_TIMEOUT

if TYPE_CHECKING:
    from wirespec.page import Page

__all__ = ["LocatorAssertions", "PageAssertions", "expect"]


@overload
def expect(subject: Locator, *, timeout: float = ...) -> LocatorAssertions: ...


@overload
def expect(subject: Page, *, timeout: float = ...) -> PageAssertions: ...


def expect(
    subject: Locator | Page,
    *,
    timeout: float = DEFAULT_ASSERTION_TIMEOUT,
) -> LocatorAssertions | PageAssertions:
    """Assertions about a locator or a page, retried until they hold.

    Overloaded rather than returning a union, so a type checker knows that
    ``expect(page)`` has ``to_have_url`` and ``expect(locator)`` has the other
    thirty. A union here would make every assertion an error at every call site
    -- which is what it did, before these two lines.
    """
    if isinstance(subject, Locator):
        return LocatorAssertions(subject, timeout)
    return PageAssertions(subject, timeout)


class _Assertions:
    """The shared half: one poll, and one way of saying what went wrong."""

    __slots__ = ("_negated", "_timeout")

    def __init__(self, timeout: float, negated: bool = False) -> None:
        self._timeout = timeout
        self._negated = negated

    @property
    def _page(self) -> Page:
        raise NotImplementedError

    async def _check[T](self, read, accept, describe: str, *, saw=repr) -> None:
        """Poll ``read`` until ``accept`` (or, negated, until it stops).

        ``describe`` is written positively -- "to have count 3" -- and the
        negation flips the sentence rather than needing a second message, so the
        two can never drift apart.
        """
        negated = self._negated
        wanted = f"expected {'not ' if negated else ''}{describe}"

        def satisfied(reading) -> bool:
            return accept(reading) is not negated

        # The one place every assertion goes through, which is why the
        # recording hooks in here rather than on thirty methods. An assertion
        # earns its row: it is usually the call the artefact was kept for, and
        # the interesting number is how long it waited before giving up.
        with self._page.acting("expect", wanted):
            await poll(
                self._page,
                read,
                satisfied,
                lambda reading: f"{wanted}\nlast saw {saw(reading)}",
                self._timeout,
            )


def _is_sequence(value: Matcher | Sequence[Matcher]) -> TypeIs[Sequence[Matcher]]:
    """A list of expectations, rather than one expectation.

    A ``str`` is a ``Sequence`` and a ``re.Pattern`` is not, so neither can be
    told apart by protocol alone -- and getting it wrong would turn
    ``to_have_text("Acme")`` into a positional assertion about four characters.

    ``TypeIs`` rather than ``TypeGuard`` because the *negative* branch is the
    one that needs narrowing: it is where a single matcher is used as one.
    """
    return isinstance(value, list | tuple)


def _readable(reading: object) -> str:
    """``True``/``False`` read badly in a sentence about visibility."""
    if reading is True:
        return "that it was"
    if reading is False:
        return "that it was not"
    return repr(reading)


class LocatorAssertions(_Assertions):
    """Assertions about what a locator matches."""

    __slots__ = ("_locator",)

    def __init__(self, locator: Locator, timeout: float, negated: bool = False) -> None:
        super().__init__(timeout, negated)
        self._locator = locator

    @property
    def _page(self) -> Page:
        return self._locator.page

    def _negate(self) -> LocatorAssertions:
        return LocatorAssertions(self._locator, self._timeout, not self._negated)

    def _named(self, description: str) -> str:
        return f"{self._locator!r} {description}"

    # -- counting ------------------------------------------------------------

    async def to_have_count(self, count: int) -> None:
        await self._check(
            self._locator.count,
            lambda seen: seen == count,
            self._named(f"to have count {count}"),
        )

    async def not_to_have_count(self, count: int) -> None:
        await self._negate().to_have_count(count)

    # -- being there ---------------------------------------------------------

    async def to_be_visible(self) -> None:
        """Strict: exactly one element, and it is on the screen."""
        await self._check(
            self._one_where(self._locator.page.is_visible),
            lambda seen: seen is True,
            self._named("to be visible"),
            saw=_readable,
        )

    async def not_to_be_visible(self) -> None:
        await self._negate().to_be_visible()

    async def to_be_hidden(self) -> None:
        """Absent counts as hidden (§6.4).

        Not the strict reading, and deliberately: an element that never
        rendered is not on screen, and making a spec say which of the two it
        meant would be asking it about the implementation. Two matches is still
        a bug, though, so it is only *zero* that is forgiven.
        """

        async def read() -> object:
            found = await self._locator.page.resolve(self._locator.chain)
            if not found:
                return "no such element"
            if len(found) > 1:
                return f"{len(found)} matches"
            # Inverted here rather than in the acceptance test, because the
            # reading is also what the *message* quotes. Reporting visibility
            # under a sentence that says "to be hidden" produces "expected ...
            # to be hidden, last saw that it was" -- which reads as "it was
            # hidden", the exact opposite of the truth. The assertion was
            # right and only the explanation was wrong, which is the failure
            # §11.2 keeps a suite of message tests for.
            return not await self._locator.page.is_visible(found[0])

        await self._check(
            read,
            lambda seen: seen is True or seen == "no such element",
            self._named("to be hidden"),
            saw=_readable,
        )

    async def not_to_be_hidden(self) -> None:
        await self._negate().to_be_hidden()

    # -- what the control says -----------------------------------------------

    async def to_be_enabled(self) -> None:
        await self._check(
            self._locator.is_enabled,
            lambda seen: seen is True,
            self._named("to be enabled"),
            saw=_readable,
        )

    async def not_to_be_enabled(self) -> None:
        await self._negate().to_be_enabled()

    async def to_be_disabled(self) -> None:
        await self._check(
            self._locator.is_enabled,
            lambda seen: seen is False,
            self._named("to be disabled"),
            saw=_readable,
        )

    async def not_to_be_disabled(self) -> None:
        await self._negate().to_be_disabled()

    async def to_be_checked(self) -> None:
        await self._check(
            self._locator.is_checked,
            lambda seen: seen is True,
            self._named("to be checked"),
            saw=_readable,
        )

    async def not_to_be_checked(self) -> None:
        await self._negate().to_be_checked()

    async def to_be_editable(self) -> None:
        await self._check(
            self._locator.is_editable,
            lambda seen: seen is True,
            self._named("to be editable"),
            saw=_readable,
        )

    async def not_to_be_editable(self) -> None:
        await self._negate().to_be_editable()

    async def to_be_focused(self) -> None:
        await self._check(
            self._locator.is_focused,
            lambda seen: seen is True,
            self._named("to be focused"),
            saw=_readable,
        )

    async def not_to_be_focused(self) -> None:
        await self._negate().to_be_focused()

    async def to_be_empty(self) -> None:
        """No text, for an element; no value, for a control. A spec asserting a
        field is empty and a spec asserting a cell is empty mean the same thing
        and should not have to spell it differently."""

        async def read() -> str:
            node_id = await self._locator.one(timeout=self._timeout)
            value = await self._locator.page.control_value(node_id)
            if value is not None:
                return value
            return (await self._locator._text_contents([node_id]))[0]

        await self._check(read, lambda seen: not seen.strip(), self._named("to be empty"))

    async def not_to_be_empty(self) -> None:
        await self._negate().to_be_empty()

    # -- what it says --------------------------------------------------------

    async def to_have_text(
        self,
        expected: Matcher | Sequence[Matcher],
        *,
        ignore_case: bool = False,
        use_inner_text: bool = False,
    ) -> None:
        """The whole text, after whitespace normalisation."""
        await self._text(expected, whole=True, ignore_case=ignore_case, use_inner_text=use_inner_text)

    async def not_to_have_text(
        self,
        expected: Matcher | Sequence[Matcher],
        *,
        ignore_case: bool = False,
        use_inner_text: bool = False,
    ) -> None:
        await self._negate().to_have_text(expected, ignore_case=ignore_case, use_inner_text=use_inner_text)

    async def to_contain_text(
        self,
        expected: Matcher | Sequence[Matcher],
        *,
        ignore_case: bool = False,
        use_inner_text: bool = False,
    ) -> None:
        """A substring of the text, after whitespace normalisation."""
        await self._text(expected, whole=False, ignore_case=ignore_case, use_inner_text=use_inner_text)

    async def not_to_contain_text(
        self,
        expected: Matcher | Sequence[Matcher],
        *,
        ignore_case: bool = False,
        use_inner_text: bool = False,
    ) -> None:
        await self._negate().to_contain_text(expected, ignore_case=ignore_case, use_inner_text=use_inner_text)

    async def _text(
        self,
        expected: Matcher | Sequence[Matcher],
        *,
        whole: bool,
        ignore_case: bool,
        use_inner_text: bool,
    ) -> None:
        """§5.3's rule, in one place for all four spellings."""
        read = self._locator.all_inner_texts if use_inner_text else self._locator.all_text_contents
        positional = _is_sequence(expected)
        wanted: list[Matcher] = list(expected) if _is_sequence(expected) else [expected]

        def accept(seen: list[str]) -> bool:
            if positional:
                # The count has to match too, or "the first three of many"
                # quietly passes and the spec's claim about an order is not the
                # claim being checked.
                return len(seen) == len(wanted) and all(
                    asserted(one, other, whole=whole, ignore_case=ignore_case)
                    for one, other in zip(wanted, seen, strict=True)
                )
            # Any of them satisfies it; the negation therefore means none of
            # them, which is what flipping this whole predicate gives.
            return any(asserted(wanted[0], one, whole=whole, ignore_case=ignore_case) for one in seen)

        description = "to have" if whole else "to contain"
        await self._check(
            read,
            accept,
            self._named(f"{description} text {expected!r}"),
            saw=lambda seen: repr([normalise(one) for one in seen]),
        )

    async def to_have_value(self, expected: Matcher, *, ignore_case: bool = False) -> None:
        async def read() -> str:
            return await self._locator.input_value(timeout=self._timeout)

        await self._check(
            read,
            lambda seen: asserted(expected, seen, whole=True, ignore_case=ignore_case),
            self._named(f"to have value {expected!r}"),
        )

    async def not_to_have_value(self, expected: Matcher, *, ignore_case: bool = False) -> None:
        await self._negate().to_have_value(expected, ignore_case=ignore_case)

    async def to_have_attribute(self, name: str, expected: Matcher, *, ignore_case: bool = False) -> None:
        async def read() -> str | None:
            return await self._locator.get_attribute(name, timeout=self._timeout)

        await self._check(
            read,
            lambda seen: seen is not None and asserted(expected, seen, whole=True, ignore_case=ignore_case),
            self._named(f"to have {name}={expected!r}"),
        )

    async def not_to_have_attribute(self, name: str, expected: Matcher, *, ignore_case: bool = False) -> None:
        await self._negate().to_have_attribute(name, expected, ignore_case=ignore_case)

    async def to_have_css(self, name: str, expected: Matcher) -> None:
        """The *computed* value, which is the only one comparable at all: a
        colour declared as a keyword comes back as ``rgb(...)``."""

        async def read() -> str:
            node_id = await self._locator.one(timeout=self._timeout)
            return await self._locator.page.computed_style(node_id, name)

        await self._check(
            read,
            lambda seen: asserted(expected, seen, whole=True, ignore_case=False),
            self._named(f"to have css {name}={expected!r}"),
        )

    async def not_to_have_css(self, name: str, expected: Matcher) -> None:
        await self._negate().to_have_css(name, expected)

    def _one_where(self, probe):
        """Read a single-element property, keeping the strictness message.

        ``Locator.one`` already raises with "should match exactly one element,
        but matched 3", and that is the message a strict assertion wants -- so
        it is left to propagate rather than being caught and re-worded.

        With one exception, and it is the same carve-out ``to_be_hidden`` makes
        for the same reason: **negated, zero matches is an answer rather than a
        refusal.** ``not_to_be_visible`` on an element the page has removed is
        asserting that it is not on screen, and it is not on screen -- so it
        passes, as Playwright's does (§4.3).

        *Two* matches still refuses, in both directions. Only zero is forgiven,
        which is exactly the asymmetry [§6.4] already documents: an element that
        never rendered is not on screen, and two elements is a spec bug either
        way.
        """

        async def read():
            try:
                return await probe(await self._locator.one(timeout=self._timeout))
            except WirespecTimeoutError as exc:
                if self._negated and "matched none" in str(exc):
                    # The same reading `to_be_hidden` uses, so the two agree in
                    # the message as well as in the verdict. It fails every
                    # `seen is True` test, which is what makes the negation hold.
                    return "no such element"
                raise

        return read


class PageAssertions(_Assertions):
    """Assertions about the page itself."""

    __slots__ = ("_subject",)

    def __init__(self, page: Page, timeout: float, negated: bool = False) -> None:
        super().__init__(timeout, negated)
        self._subject = page

    @property
    def _page(self) -> Page:
        return self._subject

    def _negate(self) -> PageAssertions:
        return PageAssertions(self._subject, self._timeout, not self._negated)

    async def to_have_title(self, expected: Matcher) -> None:
        """What the tab says the page is called.

        Polled rather than read once, unlike ``to_have_url``: the url is kept
        current by an event and the title is not, and an application that sets
        it when the data arrives sets it well after the document did.
        """
        await self._check(
            self._subject.title,
            lambda seen: asserted(expected, seen, whole=True, ignore_case=False),
            f"the page to be titled {expected!r}",
        )

    async def not_to_have_title(self, expected: Matcher) -> None:
        await self._negate().to_have_title(expected)

    async def to_have_url(self, expected: Matcher) -> None:
        """Where the page is.

        A relative path is resolved against the context's ``base_url`` first, so
        a spec can write the path it navigated to rather than reassembling the
        fixture server's port.

        The reading is pushed, not polled: ``page.url`` is kept current by
        ``frameNavigated`` and ``navigatedWithinDocument`` (§8.12).
        But the *loop* still runs, because a navigation started by the
        application has to be waited for like anything else.
        """
        wanted = expected
        if isinstance(expected, str) and not _looks_absolute(expected):
            wanted = self._subject.resolve_url(expected)

        async def read() -> str:
            return self._subject.url

        await self._check(
            read,
            lambda seen: asserted(wanted, seen, whole=True, ignore_case=False),
            f"the page to be at {wanted!r}",
        )

    async def not_to_have_url(self, expected: Matcher) -> None:
        await self._negate().to_have_url(expected)


def _looks_absolute(url: str) -> bool:
    return "://" in url or url.startswith("about:")
