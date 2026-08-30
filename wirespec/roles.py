"""The role narrowing table: a CSS selector that over-approximates each role.

§3.4. A role query is answered in two halves -- a CSS selector
collects candidates in one call, and ``Accessibility.getPartialAXTree`` confirms
each one's role and accessible name **as Chrome computed them**. This file is
the first half, and it exists because ``Accessibility.queryAXTree``, which would
have been the whole answer, costs 16.6 ms a call against 0.253 ms for a
per-node confirm.

**Every selector here must over-approximate, and must never under-approximate.**
The asymmetry is the design. A candidate a selector misses is an element the
query silently cannot find -- the failure mode this project keeps returning to.
A candidate it wrongly includes costs 0.253 ms and is then dropped by the
confirm. So every judgement call in this table has a safe direction, and when in
doubt the entry is too generous on purpose.

That is not a promise anyone should take on trust, which is why
``tests/driver/test_role_table.py`` checks it: for each role, a fixture holding
every element that can carry it, and an assertion that this selector's candidate
set is a *superset* of what ``Accessibility.getFullAXTree`` reports. Superset,
not equality -- a test demanding equality would fail for the harmless direction
and teach people to loosen the table.

**Why these roles and not the other hundred.** Because these are the ones the
229-spec suite that validates the design actually queries, in its own order of
use: button 24, radio 13, heading 6, textbox 5, listitem 5, list 5, group 5,
checkbox 5, and a tail of ones and twos. Adding a role is one line plus a
fixture element; adding all of ARIA on spec would be a hundred lines nothing
tests (§2).
"""

__all__ = ["NARROWING", "selector_for"]

#: Role -> the CSS that collects its candidates. ``[role=...]`` is in every
#: entry because any element may claim any role, and an explicit ``role``
#: attribute always wins over the tag's own mapping.
NARROWING: dict[str, str] = {
    # <summary> is here because Chrome maps it to a button-like role and the
    # table is allowed to be generous; the confirm decides.
    "button": "button, input[type=button], input[type=submit], input[type=reset], input[type=image], summary",
    "checkbox": "input[type=checkbox]",
    # <select> is a combobox only when it is not a listbox (no multiple, no
    # size>1), and <input list=...> is one too. Both are included unconditionally.
    "combobox": "select, input[list]",
    "heading": "h1, h2, h3, h4, h5, h6",
    # An <a> without href is not a link -- it is generic -- but including it
    # costs one confirm and excluding it would be an under-approximation the
    # moment Chrome changes its mind.
    "link": "a, area",
    "list": "ul, ol, menu",
    "listitem": "li",
    "main": "main",
    "option": "option",
    "radio": "input[type=radio]",
    # A <section> is a region only when it has an accessible name; an unnamed
    # one is generic. The selector cannot know, so it takes all of them.
    "region": "section",
    "searchbox": "input[type=search]",
    "status": "output",
    # No type= at all is a text field, which is why the bare `input` is here;
    # the typed ones that are *not* textboxes are dropped by the confirm.
    "textbox": "input, textarea, [contenteditable]",
    "complementary": "aside",
    "dialog": "dialog",
    "group": "fieldset, details, optgroup, address",
    # Roles with no native HTML element at all. The `[role=...]` the builder
    # adds is the whole selector.
    "alertdialog": "",
    "radiogroup": "",
    "tab": "",
    "tooltip": "",
}


def selector_for(role: str) -> str:
    """The narrowing selector for ``role``.

    Raises on a role the table does not cover, naming it. Returning "match
    nothing" would turn an unsupported role into an element that is simply
    never found, which is the same failure this whole table is arranged to
    avoid (§5, "nothing fails silently").
    """
    if role not in NARROWING:
        known = ", ".join(sorted(NARROWING))
        raise NotImplementedError(
            f"get_by_role({role!r}) is not supported yet -- wirespec has no narrowing selector for it. "
            f"Supported roles: {known}. Adding one is a line in wirespec/roles.py plus a fixture "
            f"element in tests/driver/pages/roles.html (§3.4)."
        )
    native = NARROWING[role]
    explicit = f'[role~="{role}"]'
    return f"{native}, {explicit}" if native else explicit
