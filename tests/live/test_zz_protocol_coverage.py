"""The suite's own completeness check.

Named to sort last: it reads a ledger the other live modules fill in as they
run, so it is only meaningful once they have.

This is the test that makes "a test case for each command and event" a fact
rather than an intention. Adding a struct to ``wirespec.cdp`` without
exercising it against Chrome fails here, naming the member.
"""

import pytest

from tests.live.conftest import ProtocolLog
from wirespec.cdp import COMMANDS, EVENTS

#: Every live module must have run for the ledger to be complete.
LIVE_MODULES = {
    "test_accessibility.py",
    "test_animation.py",
    "test_browser.py",
    "test_css.py",
    "test_dom.py",
    "test_domsnapshot.py",
    "test_emulation.py",
    "test_fetch.py",
    "test_input.py",
    "test_network.py",
    "test_page.py",
    "test_runtime.py",
    "test_target.py",
}


def _require_full_run(collected: set[str]) -> None:
    missing = LIVE_MODULES - collected
    if missing:
        pytest.skip(f"partial run: {', '.join(sorted(missing))} not collected")


def test_every_command_was_sent_to_a_real_chrome(protocol_log: ProtocolLog, collected_live_modules: set[str]) -> None:
    _require_full_run(collected_live_modules)
    missing = sorted(set(COMMANDS) - protocol_log.commands)
    assert not missing, f"{len(missing)} commands never reached Chrome: {missing}"


def test_every_event_was_received_from_a_real_chrome(
    protocol_log: ProtocolLog, collected_live_modules: set[str]
) -> None:
    _require_full_run(collected_live_modules)
    missing = sorted(set(EVENTS) - protocol_log.events)
    assert not missing, f"{len(missing)} events never arrived: {missing}"


def test_no_event_field_is_misspelled_against_the_wire(
    protocol_log: ProtocolLog, collected_live_modules: set[str]
) -> None:
    """Every field the subset declares must match how Chrome actually spells it.

    The failure this exists for is silent. ``rename="camel"`` turns
    ``document_url`` into ``documentUrl``; CDP spells it ``documentURL``. The
    struct is then asking for a key that never arrives -- and where the field
    is optional, nothing fails at all, the value is just permanently None.

    Only events actually seen on the wire are checked, and only fields whose
    expected name is absent while a case-insensitive twin is present: Chrome
    sends plenty of keys the subset deliberately ignores, and omits plenty of
    optional ones, neither of which is a bug.
    """
    _require_full_run(collected_live_modules)

    mistakes = []
    for method, seen in sorted(protocol_log.wire_keys.items()):
        event = EVENTS[method]
        expected = {
            encoded
            for encoded in event.__struct_encode_fields__  # type: ignore[attr-defined]
        }
        lowered = {key.lower(): key for key in seen}
        for name in sorted(expected - seen):
            twin = lowered.get(name.lower())
            if twin is not None:
                mistakes.append(f"{method}: struct asks for {name!r}, Chrome sends {twin!r}")
    assert not mistakes, "field names do not match the protocol:\n  " + "\n  ".join(mistakes)


def test_nothing_chrome_sent_failed_to_decode(protocol_log: ProtocolLog) -> None:
    """A decode failure anywhere in the run, gathered in one place."""
    assert not protocol_log.decode_failures, "\n  ".join(protocol_log.decode_failures)
