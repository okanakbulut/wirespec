"""``page.evaluate`` -- the one door the caller's own JavaScript goes through.

wirespec ships, injects and evaluates no JavaScript of its own (
§3.1). Everything here is the *caller's* code, passed through and never
authored by wirespec, which is why the calling rule in §8.8 applies uniformly:
there is no internal caller to exempt.
"""

import pytest

from wirespec.errors import JavaScriptError
from wirespec.page import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_plain_expression_is_evaluated(page: Page) -> None:
    assert await page.evaluate("1 + 1") == 2


async def test_a_function_expression_is_called(page: Page) -> None:
    """§8.8. ``() => 1 + 1`` means "run this", not "give me a
    function object" -- evaluating it literally returns {} and the assertion
    fails far from the cause."""
    assert await page.evaluate("() => 1 + 1") == 2
    assert await page.evaluate("function () { return 1 + 1; }") == 2
    assert await page.evaluate("x => 41") == 41
    assert await page.evaluate("(a, b) => 7") == 7


async def test_an_async_function_is_awaited(page: Page) -> None:
    """``async () => {...}`` returns a promise. Returning the promise object
    would be a value no assertion can read."""
    assert await page.evaluate("async () => { await 0; return 'done'; }") == "done"


async def test_a_parenthesised_expression_is_not_mistaken_for_a_function(page: Page) -> None:
    """§8.8's warning, exactly: ``(a || b).c`` opens with a
    parenthesis and is not callable. Wrapping and calling it would throw."""
    await page.goto("/index.html")
    assert await page.evaluate("(null || document).title") == "wirespec driver fixture"


async def test_a_page_side_throw_becomes_a_python_exception(page: Page) -> None:
    """Not a None that fails somewhere else later. The JavaScript stack is the
    part worth carrying into the traceback."""
    with pytest.raises(JavaScriptError) as raised:
        await page.evaluate("() => { throw new Error('from the page'); }")
    assert "from the page" in str(raised.value)


async def test_a_function_wirespec_did_not_recognise_says_so(page: Page) -> None:
    """The detection in §8.8 is deliberately narrow, so it will sometimes fail
    to spot a function. That must be an error naming itself, not a silent {}.
    """
    with pytest.raises(JavaScriptError, match="function"):
        await page.evaluate("(a = (1)) => a")


async def test_an_argument_is_passed_to_the_function(page: Page) -> None:
    """Playwright's second parameter. The value is serialised by Chrome, not by
    wirespec: it goes out as ``Runtime.callFunctionOn``'s ``arguments``, so a
    string with a quote in it is a string and not a splicing accident."""
    assert await page.evaluate("x => x * 2", 21) == 42
    assert await page.evaluate("s => s + '!'", "it's") == "it's!"


async def test_an_argument_may_be_any_json_shape(page: Page) -> None:
    assert await page.evaluate("o => o.rows.length", {"rows": [1, 2, 3]}) == 3
    assert await page.evaluate("xs => xs[1]", ["a", "b"]) == "b"
    assert await page.evaluate("x => x === null", None) is True
    assert await page.evaluate("x => x", True) is True


async def test_the_argument_reaches_the_page_not_the_source(page: Page) -> None:
    """The reason this is an argument rather than string interpolation: a value
    spliced into the source would be *code*, and a spec passing a piece of the
    page's own text would be one apostrophe away from a syntax error -- or from
    running something it did not write."""
    await page.goto("/index.html")
    await page.evaluate("text => { document.title = text; }", "');alert(1);//")
    assert await page.evaluate("() => document.title") == "');alert(1);//"


async def test_an_argument_with_no_function_to_take_it_says_so(page: Page) -> None:
    """``1 + 1`` has nowhere to put an argument. Chrome would answer with a
    TypeError about ``2 is not a function``, which names neither the expression
    nor the argument."""
    with pytest.raises(JavaScriptError, match="argument"):
        await page.evaluate("1 + 1", 3)


async def test_a_page_side_throw_still_becomes_a_python_exception(page: Page) -> None:
    with pytest.raises(JavaScriptError, match="nope"):
        await page.evaluate("x => { throw new Error('nope ' + x); }", 1)


async def test_an_async_function_with_an_argument_is_awaited(page: Page) -> None:
    assert await page.evaluate("async x => { await 0; return x + 1; }", 41) == 42
