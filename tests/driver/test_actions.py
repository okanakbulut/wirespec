"""Actions, and the actionability checks that must pass before each one.

§5.2. Everything that must be true before a click is decided in one
pipelined batch which returns the point to press. Each refusal is tested
separately, because the *message* is the feature: "is disabled" and "is covered
at 100,60 by something else" send someone to two very different places.
"""

import time

import pytest

from wirespec.browser import BrowserContext
from wirespec.errors import WirespecTimeoutError
from wirespec.expect import expect
from wirespec.page import Page
from wirespec.pickers import PickerError

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_click_reaches_the_element(page: Page) -> None:
    await page.goto("/actions.html")
    await page.get_by_role("button", name="Plain").click()
    assert await page.evaluate("() => window.__log") == ["plain"]


async def test_a_click_on_a_page_that_is_not_in_front_is_not_slow(context: BrowserContext) -> None:
    """Every page wirespec opens believes it is the focused one, and this is why.

    A tab that is not in front stops producing frames, and Chrome does not
    answer a mouse *move* until it produces one -- measured, exactly 5.001 s of
    watchdog per move, so every click on any page but the newest cost five
    seconds and nothing failed (§8.26). A suite that opens a second
    page and goes back to the first is the ordinary shape of that, which is how
    the pilot suite found it.

    Two seconds is the bound because the failure is not marginal: it is either
    ~16 ms or ~5 s.
    """
    behind = await context.new_page()
    await behind.goto("/actions.html")
    front = await context.new_page()
    await front.goto("/actions.html")

    began = time.monotonic()
    await behind.get_by_role("button", name="Plain").click()
    took = time.monotonic() - began

    assert await behind.evaluate("() => window.__log") == ["plain"]
    assert took < 2.0, f"a click on the page behind took {took:.2f}s"


async def test_a_click_scrolls_to_what_is_below_the_fold(page: Page) -> None:
    """§5.2 step 5. In-viewport is deliberately not part of
    *visibility* -- an element below the fold is one scroll away, and making it
    invisible would make correct specs flake -- so the scroll happens here."""
    await page.goto("/actions.html")
    await page.get_by_role("button", name="Below the fold").click()
    assert await page.evaluate("() => window.__log") == ["below"]


async def test_a_covered_element_is_refused_by_name(page: Page) -> None:
    """Step 7, and the message it exists for. A sticky overlay sits over its own
    container without occupying room in it, so "in the viewport" is not the same
    as "reachable" (§8.6)."""
    await page.goto("/actions.html")
    with pytest.raises(WirespecTimeoutError) as raised:
        await page.locator("#covered").click(timeout=0.6)
    assert "covered" in str(raised.value)
    assert await page.evaluate("() => window.__log") == []


async def test_a_covered_element_is_clicked_once_the_cover_goes(page: Page) -> None:
    """Which is why it is a *retrying* check rather than a verdict."""
    await page.goto("/actions.html")
    await page.evaluate("() => window.__uncover(150)")
    await page.locator("#covered").click()
    assert await page.evaluate("() => window.__log") == ["covered"]


async def test_force_drops_the_hit_test(page: Page) -> None:
    """For the case the check exists to catch and the caller means anyway:
    hovering something a tooltip is deliberately sitting on."""
    await page.goto("/actions.html")
    await page.locator("#covered").click(force=True)
    # The press lands at the point, and the overlay is what receives it -- which
    # is exactly what `force` says the caller wants.
    assert await page.evaluate("() => window.__log") == []


async def test_a_disabled_control_is_refused_by_name(page: Page) -> None:
    await page.goto("/actions.html")
    with pytest.raises(WirespecTimeoutError, match="disabled"):
        await page.get_by_role("button", name="Disabled").click(timeout=0.6)


async def test_hovering_a_disabled_control_is_allowed(page: Page) -> None:
    """A person can hover a disabled button, and a spec checking its tooltip
    has to be able to."""
    await page.goto("/actions.html")
    await page.get_by_role("button", name="Disabled").hover()


async def test_a_click_on_a_label_reaches_the_input_it_wraps(page: Page) -> None:
    """The hit test accepts the element or a descendant: a click on a button's
    own text node is a click on the button.

    An **ancestor** is deliberately not accepted, and this test is why that
    costs nothing. It used to be the case the ancestor rule existed for -- but a
    label wrapping a rendered control does not cover it, so the point at the
    input's centre hits the ``INPUT`` itself and the direct check is what
    accepts it. Measured; see ``wirespec/actionable.py``'s ``_hits``.
    """
    await page.goto("/actions.html")
    await page.locator("#inside-label").click()
    assert "inside-label" in await page.evaluate("() => window.__log")


async def test_an_element_the_mouse_falls_straight_through_is_refused(page: Page) -> None:
    """§8.31, in the shape that is not about scrolling.

    ``#ghost`` is visible and laid out and ``pointer-events: none``, so a press
    at its centre reaches the page and never the button. The hit test used to
    accept exactly this -- the node at the point was ``<body>``, an *ancestor*
    of the target, and an ancestor counted -- so ``click()`` dispatched into
    nothing and returned success. Measured against the code before the fix: the
    call reported success and the button's own handler never ran.

    The two halves are asserted separately because only one of them was ever
    the visible symptom: the refusal is new, and the silent no-op is what it
    replaces.
    """
    await page.goto("/actions.html")
    with pytest.raises(WirespecTimeoutError) as raised:
        await page.locator("#ghost").click(timeout=0.6)
    assert "is not painted there" in str(raised.value)
    assert await page.evaluate("() => window.__log") == []


async def test_a_moving_element_is_waited_for(page: Page) -> None:
    """§5.2 step 6. The second box sample is free -- the checks
    above already spent the time -- so nothing is waited for unless the element
    was actually seen to move."""
    await page.goto("/actions.html")
    box_before = await page.locator("#sliding").bounding_box()
    await page.evaluate("() => window.__slide()")
    await page.locator("#sliding").hover()
    box_after = await page.locator("#sliding").bounding_box()
    assert box_before is not None and box_after is not None
    # It settles 300px along, and -- the part that matters -- the pointer is on
    # it when it gets there. Aiming where the box used to be would move the
    # mouse to empty space and no mouseenter would ever fire.
    assert box_after["x"] > box_before["x"]
    assert "entered-sliding" in await page.evaluate("() => window.__log")


async def test_an_element_moved_by_an_ancestor_is_waited_for(page: Page) -> None:
    """§8.7's named case, and the one a per-element check misses.

    A dialog sliding into place moves everything inside it without those
    children animating at all -- the button's own computed style says nothing.
    Scoping the question to the element, its subtree **and its ancestors** is
    what catches it.
    """
    await page.goto("/actions.html")
    before = await page.locator("#in-dialog").bounding_box()
    await page.evaluate("() => window.__slideDialog()")
    await page.locator("#in-dialog").click()
    after = await page.locator("#in-dialog").bounding_box()
    assert before is not None and after is not None
    assert after["x"] > before["x"], "the dialog should have moved"
    assert "in-dialog" in await page.evaluate("() => window.__log"), "the click landed where the button used to be"


async def test_a_still_page_pays_nothing_for_the_movement_check(page: Page) -> None:
    """The other half of §8.7, and the expensive half.

    The prototype asked ``document.getAnimations()``, true on any page with one
    indefinite animation anywhere, and every action then waited two frames it
    had no reason to wait for -- 25.2 ms instead of 17.0 ms, 64% of all protocol
    time. A frame is ~16 ms, so an action on a still page has to come in well
    under two of them.
    """
    import time

    await page.goto("/actions.html")
    await page.locator("#plain").click()  # warm: the first call fetches the document
    started = time.perf_counter()
    await page.locator("#plain").click()
    elapsed = time.perf_counter() - started
    assert elapsed < 0.030, f"a click on a still page took {elapsed * 1000:.1f} ms, which is frames being waited"


async def test_two_matches_is_a_spec_bug_reported_as_one(page: Page) -> None:
    """§5.2 step 1, strict."""
    await page.goto("/actions.html")
    with pytest.raises(WirespecTimeoutError) as raised:
        await page.locator("button").click(timeout=0.5)
    assert "matched" in str(raised.value)


async def test_fill_replaces_and_raises_the_input_event(page: Page) -> None:
    """Not an assignment: ``.value =`` does not raise the ``input`` event a
    controlled React field listens for, and the field visibly reverts on the
    next render (§8.4)."""
    await page.goto("/actions.html")
    await page.locator("#text").fill("new value")
    await expect(page.locator("#text")).to_have_value("new value")
    events = await page.evaluate("() => window.__log.filter(e => e[0] === 'input').map(e => e[1])")
    assert "text" in events


async def test_fill_works_on_a_textarea_too(page: Page) -> None:
    await page.goto("/actions.html")
    await page.locator("#area").fill("replaced")
    await expect(page.locator("#area")).to_have_value("replaced")


async def test_type_produces_a_keystroke_per_character(page: Page) -> None:
    await page.goto("/actions.html")
    await page.locator("#text").fill("")
    await page.locator("#text").type("abc")
    await expect(page.locator("#text")).to_have_value("abc")


async def test_press_sends_a_named_key_to_the_element(page: Page) -> None:
    await page.goto("/actions.html")
    await page.locator("#text").fill("abc")
    await page.locator("#text").press("Backspace")
    await expect(page.locator("#text")).to_have_value("ab")


async def test_scroll_into_view_if_needed(page: Page) -> None:
    await page.goto("/actions.html")
    assert await page.evaluate("() => window.scrollY") == 0
    await page.locator("#below").scroll_into_view_if_needed()
    assert await page.evaluate("() => window.scrollY") > 0


async def test_dblclick_reaches_the_page_as_one(page: Page) -> None:
    await page.goto("/actions.html")
    await page.locator("#plain").dblclick()
    assert await page.evaluate("() => window.__log") == ["plain", "plain"]


# -- pickers (§8.4) ---------------------------------------------


@pytest.mark.parametrize(
    ("element", "value"),
    [("#date", "2026-03-15"), ("#time", "14:30"), ("#week", "2026-W11")],
)
async def test_the_three_pickers_that_can_be_typed(page: Page, element: str, value: str) -> None:
    """Measured, all three, and the segment order is pinned by the locale
    wirespec launches Chrome in rather than inherited from the machine."""
    await page.goto("/actions.html")
    await page.locator(element).fill(value)
    await expect(page.locator(element)).to_have_value(value)


async def test_a_typed_picker_raises_the_events_a_framework_listens_for(page: Page) -> None:
    """Which is the whole reason typing is worth the trouble: no value tracker
    can be left stale by it, because nothing was assigned."""
    await page.goto("/actions.html")
    await page.locator("#date").fill("2026-03-15")
    kinds = await page.evaluate("() => window.__log.filter(e => e[1] === 'date').map(e => e[0])")
    assert "input" in kinds
    assert "change" in kinds


async def test_a_slider_is_moved_by_its_step(page: Page) -> None:
    """The only relative fill of the seven: the arrows move one ``step`` each,
    so it has to know where the slider started."""
    await page.goto("/actions.html")
    await page.locator("#slider").fill("75")
    await expect(page.locator("#slider")).to_have_value("75")
    await page.locator("#slider").fill("30")
    await expect(page.locator("#slider")).to_have_value("30")


@pytest.mark.parametrize("element", ["#month", "#colour"])
async def test_the_pickers_that_cannot_be_filled_say_so(page: Page, element: str) -> None:
    """Appearing to work is the one outcome worse than not supporting it
    (§8.4)."""
    await page.goto("/actions.html")
    with pytest.raises(PickerError) as raised:
        await page.locator(element).fill("anything")
    said = str(raised.value)
    assert element.lstrip("#") in said or "type=" in said, said
    assert "wirespec cannot fill" in said, said


@pytest.mark.parametrize(
    ("element", "value"),
    [
        ("#rebuilt", "2026-03-15"),
        # A month past September, whose first digit already spells a month: the
        # value is complete the instant `1` lands, so the `2` of December has
        # nowhere safe to go and the arrows have to finish the job.
        ("#rebuilt", "2027-12-31"),
        ("#rebuilt", "2028-02-29"),
        ("#rebuilt-time", "14:30"),
        ("#rebuilt-week", "2026-W53"),
        # The same trap one step worse: the field is not replaced, it is
        # *emptied* for a render and refilled on the next, because the state the
        # framework renders from is one event behind.
        ("#stale", "2026-03-15"),
        ("#stale", "2027-12-31"),
    ],
)
async def test_a_picker_the_framework_rebuilds_under_it_is_still_filled(page: Page, element: str, value: str) -> None:
    """The case that matters, because it is what a real application is.

    The field is torn down on its first ``input`` event -- replaced outright, or
    emptied and refilled from state a render behind, both of which React does
    when it reconciles a controlled input. The widget's idea of which segment is
    being typed into does not survive either, so every keystroke after the first
    one the page sees lands somewhere else (§8.24). Filling one is a
    matter of giving the page nothing to react to until the last segment, and
    walking that one with the arrows, where each press is aimed again and waited
    on."""
    await page.goto("/controlled.html")
    await page.locator(element).fill(value)
    await expect(page.locator(element)).to_have_value(value)


async def test_a_rebuilt_picker_can_be_filled_twice(page: Page) -> None:
    """A second fill starts on a field that already has a value, which is the
    state that makes every keystroke a change the page reacts to. Emptying the
    last segment first is what makes the second fill look like the first."""
    await page.goto("/controlled.html")
    await page.locator("#rebuilt").fill("2026-03-15")
    await page.locator("#rebuilt").fill("2029-11-02")
    await expect(page.locator("#rebuilt")).to_have_value("2029-11-02")


async def test_a_picker_that_only_reassigns_its_value_never_needed_the_care(page: Page) -> None:
    """The boundary of §8.24, measured: assigning ``.value``, the ``value``
    attribute or ``defaultValue`` from an ``input`` handler leaves the widget's
    editing state alone. Only replacing the element loses it."""
    await page.goto("/controlled.html")
    await page.locator("#assigned").fill("2026-03-15")
    await expect(page.locator("#assigned")).to_have_value("2026-03-15")


async def test_a_malformed_picker_value_is_rejected_before_anything_is_typed(page: Page) -> None:
    await page.goto("/actions.html")
    with pytest.raises(PickerError, match="YYYY-MM-DD"):
        await page.locator("#date").fill("15/03/2026")


# -- native drag (§8.2) -----------------------------------------


async def test_a_native_drag_reaches_the_drop_target(page: Page) -> None:
    """Not a sequence of mouse events: a ``draggable`` element is run by the
    browser's own drag session, which synthetic mouse input does not start."""
    await page.goto("/actions.html")
    await page.locator("#drag").drag_to(page.locator("#drop"))
    log = await page.evaluate("() => window.__log")
    assert "dragstart" in log
    assert "dragenter" in log
    assert "drop" in log


async def test_dragging_something_that_is_not_draggable_says_so(page: Page) -> None:
    """A pointer-based drag library needs mouse events instead, and the two look
    similar until they do not. The timeout says which this was."""
    await page.goto("/actions.html")
    with pytest.raises(WirespecTimeoutError, match="draggable"):
        await page.locator("#plain").drag_to(page.locator("#drop"))


async def test_an_element_that_never_settles_is_refused_by_name(page: Page) -> None:
    """§5.2 step 6, the other side of it.

    An element that is *still* moving when the deadline passes has to say so.
    The animation here is infinite, so no amount of patience makes this pass by
    accident -- which is the point: the previous test proves the wait happens
    and this one proves it ends.
    """
    await page.goto("/actions.html")
    with pytest.raises(WirespecTimeoutError, match="still moving"):
        await page.locator("#forever").click(timeout=1.0)


async def test_an_element_with_no_box_is_refused_by_name(page: Page) -> None:
    """``display: none``. Chrome refuses a box model for it, and that refusal is
    an *answer* -- "it is not rendered" -- rather than an error to swallow."""
    await page.goto("/actions.html")
    with pytest.raises(WirespecTimeoutError, match="not rendered"):
        await page.locator("#gone").click(timeout=0.6)


async def test_a_visibility_hidden_element_is_refused_by_its_own_name(page: Page) -> None:
    """Distinct from the one above and worth its own message: it has a box, it
    occupies space, and clicking where it is would hit whatever is behind it."""
    await page.goto("/actions.html")
    with pytest.raises(WirespecTimeoutError, match="visibility:hidden"):
        await page.locator("#see-through").click(timeout=0.6)


async def test_a_locator_matching_two_elements_is_refused_by_count(page: Page) -> None:
    """§5.3. An action on an ambiguous locator is a spec bug, and
    acting on whichever came first is how it stays hidden until a later test
    fails for an unrelated reason."""
    await page.goto("/actions.html")
    with pytest.raises(WirespecTimeoutError) as raised:
        await page.locator(".twin").click(timeout=0.6)
    assert "2 elements" in str(raised.value)


async def test_a_locator_matching_nothing_says_so_rather_than_timing_out(page: Page) -> None:
    await page.goto("/actions.html")
    with pytest.raises(WirespecTimeoutError, match="matched nothing"):
        await page.locator("#not-on-this-page").click(timeout=0.6)


async def test_select_option_by_value_fires_change(page: Page) -> None:
    """No CDP command sets a ``<select>``, and there is no JavaScript to do it
    with (§3.1), so this drives the widget the way a person does:
    focus, Home, and arrow keys. The events are the proof it is the same thing
    -- a value assigned from outside fires nothing, and an application listening
    for ``change`` would never hear it (§8.16)."""
    await page.goto("/actions.html")
    assert await page.locator("#fruit").select_option("d") == ["d"]
    assert await page.locator("#fruit").input_value() == "Damson"
    assert ["change", "fruit", "d"] in await page.evaluate("() => window.__log")


async def test_select_option_skips_a_disabled_option(page: Page) -> None:
    """The measurement this is built on: arrow keys **skip** disabled options,
    so a count over DOM order lands one short. ``d`` is at DOM index 3 and at
    enabled-index 2."""
    await page.goto("/actions.html")
    await page.locator("#fruit").select_option("d")
    assert await page.locator("#fruit").input_value() == "Damson"


async def test_select_option_by_label_and_by_index(page: Page) -> None:
    await page.goto("/actions.html")
    assert await page.locator("#fruit").select_option(label="Banana") == ["b"]
    assert await page.locator("#fruit").input_value() == "Banana"
    # `index` counts the options as they are written, disabled ones included --
    # Playwright's does, and it is the index a spec can see in the markup.
    assert await page.locator("#fruit").select_option(index=3) == ["d"]


async def test_select_option_works_when_the_first_option_is_disabled(page: Page) -> None:
    """Chrome starts on the first *enabled* option and ``Home`` goes there too,
    not to index zero. A count from zero would be off by one on every
    "Choose one..." placeholder there is."""
    await page.goto("/actions.html")
    assert await page.locator("#lead").select_option("two") == ["two"]
    assert await page.locator("#lead").input_value() == "Two"


async def test_select_option_counts_across_optgroups(page: Page) -> None:
    """Groups are presentation: the indices are flat, and arrow keys cross them
    without stopping."""
    await page.goto("/actions.html")
    assert await page.locator("#grouped").select_option("v") == ["v"]
    assert await page.locator("#grouped").input_value() == "Zucchini"


async def test_an_option_with_no_value_attribute_is_its_own_text(page: Page) -> None:
    """The HTML rule, and a real one: ``<option>Plain</option>`` has value
    "Plain". Treating a missing attribute as an empty value makes every such
    select unselectable."""
    await page.goto("/actions.html")
    assert await page.locator("#bare").select_option("Second") == ["Second"]
    assert await page.locator("#bare").input_value() == "Second"


async def test_selecting_an_option_that_is_not_there_says_so(page: Page) -> None:
    await page.goto("/actions.html")
    with pytest.raises(LookupError) as raised:
        await page.locator("#fruit").select_option("kumquat")
    assert "kumquat" in str(raised.value)
    assert "Apple" in str(raised.value)


async def test_selecting_a_disabled_option_says_so(page: Page) -> None:
    """Rather than arrowing past it and reporting whatever it landed on."""
    await page.goto("/actions.html")
    with pytest.raises(LookupError, match="disabled"):
        await page.locator("#fruit").select_option("c")


async def test_a_multiple_select_takes_several_values(page: Page) -> None:
    """``Home`` selects the first enabled option and clears the rest, which is
    the known state the walk starts from; ``Ctrl+ArrowDown`` then moves without
    selecting and ``Ctrl+Space`` toggles (§8.16)."""
    await page.goto("/actions.html")
    assert await page.locator("#many").select_option(["q", "t"]) == ["q", "t"]
    assert await page.locator("#many").evaluate("node => [...node.selectedOptions].map(o => o.value)") == ["q", "t"]


async def test_a_multiple_select_takes_one_value_too(page: Page) -> None:
    """The single-value spelling has to keep working on a list box, and it has
    to *replace* the selection rather than add to it."""
    await page.goto("/actions.html")
    await page.locator("#many").select_option(["p", "q"])
    assert await page.locator("#many").select_option("t") == ["t"]


async def test_a_multiple_select_selects_the_first_option_alone(page: Page) -> None:
    """The one the walk starts on. If the code forgot that ``Home`` already
    selected it, this would come back with it toggled off again."""
    await page.goto("/actions.html")
    assert await page.locator("#many").select_option("p") == ["p"]


async def test_a_multiple_select_can_be_emptied(page: Page) -> None:
    """An explicit empty list, which is not the same as passing nothing at all
    -- that stays a TypeError, because it is a mistake rather than a request."""
    await page.goto("/actions.html")
    await page.locator("#many").select_option(["p", "q"])
    assert await page.locator("#many").select_option([]) == []
    assert await page.locator("#many").evaluate("node => node.selectedOptions.length") == 0


async def test_a_multiple_select_skips_a_disabled_option(page: Page) -> None:
    """``Ctrl+ArrowDown`` skips it exactly as the plain arrows do, so a walk
    counting DOM positions would toggle Sage when asked for Thyme."""
    await page.goto("/actions.html")
    assert await page.locator("#many").select_option(["s", "t"]) == ["s", "t"]


async def test_selecting_several_by_label_and_by_index(page: Page) -> None:
    await page.goto("/actions.html")
    assert await page.locator("#many").select_option(label=["Pea", "Sage"]) == ["p", "s"]
    assert await page.locator("#many").select_option(index=[1, 4]) == ["q", "t"]


async def test_a_single_select_refuses_several(page: Page) -> None:
    """A dropdown holds one. Selecting the last of them and reporting success
    is the failure this exists to prevent."""
    await page.goto("/actions.html")
    with pytest.raises(NotImplementedError, match="one option") as raised:
        await page.locator("#fruit").select_option(["a", "b"])
    assert "multiple" in str(raised.value)


async def test_two_ways_of_asking_at_once_is_a_mistake(page: Page) -> None:
    """``value=`` and ``label=`` together used to let one win silently."""
    await page.goto("/actions.html")
    with pytest.raises(TypeError, match="one of"):
        await page.locator("#fruit").select_option("a", label="Banana")


async def test_set_input_files_puts_a_file_in_and_fires_change(page: Page, tmp_path) -> None:
    """The only way to fill a file input: the picker is browser chrome, so no
    click and no key event reaches it. ``DOM.setFileInputFiles`` is the whole
    mechanism, and the ``change`` event is what makes it the same thing an
    upload would have been."""
    payload = tmp_path / "invoice.pdf"
    payload.write_bytes(b"%PDF-1.4\n")

    await page.goto("/actions.html")
    await page.locator("#file").set_input_files(payload)
    assert ["files", "file", ["invoice.pdf"]] in await page.evaluate("() => window.__log")


async def test_set_input_files_takes_several_for_a_multiple_input(page: Page, tmp_path) -> None:
    one, two = tmp_path / "one.txt", tmp_path / "two.txt"
    one.write_text("1")
    two.write_text("2")

    await page.goto("/actions.html")
    await page.locator("#files").set_input_files([one, two])
    assert ["files", "files", ["one.txt", "two.txt"]] in await page.evaluate("() => window.__log")


async def test_clearing_a_file_input_is_refused_rather_than_pretended(page: Page, tmp_path) -> None:
    """Measured, §8.17: ``DOM.setFileInputFiles(files=[])`` is a
    **silent no-op**. The file that was attached is still attached, no
    ``change`` fires, and no error comes back -- so a spec that "cleared" the
    field and submitted would upload the old file and pass.

    Playwright clears it by assigning ``input.value`` from JavaScript, which is
    not available here. Refusing is the honest answer.
    """
    payload = tmp_path / "one.txt"
    payload.write_text("1")

    await page.goto("/actions.html")
    await page.locator("#file").set_input_files(payload)
    with pytest.raises(NotImplementedError, match="ignores an empty file list"):
        await page.locator("#file").set_input_files([])
    # And the measurement the refusal rests on, pinned: still attached.
    assert await page.evaluate("() => document.getElementById('file').files.length") == 1


async def test_a_file_that_is_not_there_is_refused_before_chrome_sees_it(page: Page, tmp_path) -> None:
    """Chrome reads the path in its **own** process, so a missing file comes
    back as a protocol error naming a node id and nothing else. Checked here,
    where the path is still in hand."""
    await page.goto("/actions.html")
    with pytest.raises(FileNotFoundError, match="nowhere.txt"):
        await page.locator("#file").set_input_files(tmp_path / "nowhere.txt")


async def test_setting_files_on_something_that_is_not_a_file_input_says_so(page: Page, tmp_path) -> None:
    payload = tmp_path / "one.txt"
    payload.write_text("1")

    await page.goto("/actions.html")
    with pytest.raises(ValueError, match="not a file input"):
        await page.locator("#text").set_input_files(payload)
