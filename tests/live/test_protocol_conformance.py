"""Every field name in the subset, checked against Chrome's own protocol.

This is the test the acronym bugs were hiding from. The rest of the live suite
proves that what wirespec *exercised* works; this proves that what it
*declares* matches the protocol -- including the fields no test happens to
touch, which is exactly where a generated name goes wrong unnoticed.

Five real bugs were found by this check, and all five were silent:

* ``Network.requestWillBeSent.documentURL`` -- required, so the whole event
  failed to decode and simply never arrived.
* ``Network.Response.remoteIPAddress`` and ``Input.DragDataItem.baseURL`` --
  optional, so they decoded to None for ever.
* ``Runtime.evaluate`` and ``Page.addScriptToEvaluateOnNewDocument``'s
  ``includeCommandLineAPI`` -- outgoing, so Chrome accepted the misspelled
  parameter, ignored it, and reported nothing.

None of them raised. Only comparing against the protocol finds this class.
"""

import inspect

import msgspec.inspect as mi
import pytest

from tests.live.protocol import Protocol, fetch_protocol
from wirespec import cdp
from wirespec.cdp.base import CDPStruct, Command, Event

#: Which domain each module describes. Written out rather than derived, because
#: the derivation is exactly the thing under test: ``dom`` is ``DOM`` and
#: ``domsnapshot`` is ``DOMSnapshot``, and a rule that capitalises the first
#: letter gets both wrong. Every module in the subset is here, and
#: ``test_every_module_is_checked`` fails if one is added and this is not.
DOMAIN_OF_MODULE = {
    "accessibility": "Accessibility",
    "animation": "Animation",
    "browser": "Browser",
    "css": "CSS",
    "dom": "DOM",
    "domsnapshot": "DOMSnapshot",
    "emulation": "Emulation",
    "fetch": "Fetch",
    "input": "Input",
    "network": "Network",
    "page": "Page",
    "runtime": "Runtime",
    "storage": "Storage",
    "target": "Target",
}

#: The protocol type a struct of wirespec's own name stands in for. ``dom``
#: declares its own trimmed ``RemoteObject`` rather than importing ``runtime``
#: for the one field it reads, and the fields it does declare still have to be
#: the protocol's.
STANDS_IN_FOR = {("DOM", "_Resolved"): ("Runtime", "RemoteObject")}


@pytest.fixture(scope="session")
def protocol() -> Protocol:
    raw = fetch_protocol()
    if raw is None:
        pytest.skip("could not fetch the protocol definition from Chrome")
    return Protocol(raw)


def compare(struct: type[CDPStruct], authoritative: dict[str, dict], where: str) -> list[str]:
    """Field names this struct puts on the wire that the protocol does not use.

    Only a *case-insensitive twin* counts as a mistake. A name the protocol
    does not have at all would be a different bug and is reported separately;
    a protocol field the struct omits is not a bug, because the subset is a
    subset on purpose.
    """
    lowered = {name.lower(): name for name in authoritative}
    mistakes = []
    for field in struct.__struct_encode_fields__:
        if field in authoritative:
            continue
        twin = lowered.get(field.lower())
        mistakes.append(
            f"{where}: struct sends {field!r}, protocol says {twin!r}"
            if twin is not None
            else f"{where}: struct sends {field!r}, which the protocol does not define"
        )
    return mistakes


def result_structs() -> set[type[CDPStruct]]:
    """Every struct that is some command's reply.

    CDP declares a command's result inline rather than as a named type, so
    ``GetBoxModelResult`` is wirespec's own container and there is nothing of
    that name to look it up by. They are checked against ``command_returns``
    instead, by the two tests above -- so they are excluded here rather than
    skipped, which is the difference between "checked elsewhere" and "not
    checked".
    """
    return {
        command.__returns__
        for command in cdp.COMMANDS.values()
        if isinstance(command.__returns__, type) and issubclass(command.__returns__, CDPStruct)
    }


def nested_structs() -> list[tuple[str, str, type[CDPStruct]]]:
    """The structs that are neither commands, events nor replies -- ``Response``,
    ``DragDataItem``, ``TargetInfo`` and the rest.

    These are where the check earns its keep: a command is exercised by the
    suite and an event arrives or does not, but a nested optional field can be
    wrong for years without anything looking different.
    """
    replies = result_structs()
    found = []
    for module_name, domain in DOMAIN_OF_MODULE.items():
        module = getattr(cdp, module_name)
        for name, value in vars(module).items():
            if (
                inspect.isclass(value)
                and issubclass(value, CDPStruct)
                and not issubclass(value, Command | Event)
                and value is not CDPStruct
                and value.__module__ == module.__name__
                and value not in replies
            ):
                found.append((*STANDS_IN_FOR.get((domain, name), (domain, name)), value))
    return found


def commands_with_results() -> list[str]:
    """The commands whose reply carries something.

    The rest answer ``{}``, and a test parametrised over them would be checking
    that nothing matches nothing. ``test_a_void_command_is_void_in_the_protocol``
    is what keeps that honest.
    """
    return sorted(
        method
        for method, command in cdp.COMMANDS.items()
        if isinstance(command.__returns__, type) and issubclass(command.__returns__, CDPStruct)
    )


@pytest.mark.parametrize("method", sorted(cdp.COMMANDS), ids=str)
def test_command_parameters_match_the_protocol(protocol: Protocol, method: str) -> None:
    """Outgoing names. A misspelled parameter is accepted and ignored, so the
    feature silently does not happen."""
    authoritative = protocol.command_parameters(method)
    assert authoritative is not None, f"{method} is not in this Chrome's protocol"
    assert not compare(cdp.COMMANDS[method], authoritative, method)


@pytest.mark.parametrize("method", commands_with_results(), ids=str)
def test_command_results_match_the_protocol(protocol: Protocol, method: str) -> None:
    """Incoming names on a reply. A misspelled required field fails the decode;
    a misspelled optional one is quietly never populated."""
    returns = cdp.COMMANDS[method].__returns__
    assert isinstance(returns, type) and issubclass(returns, CDPStruct)
    authoritative = protocol.command_returns(method)
    assert authoritative is not None, f"{method} is not in this Chrome's protocol"
    assert not compare(returns, authoritative, f"{method} (result)")


@pytest.mark.parametrize("method", sorted(cdp.EVENTS), ids=str)
def test_event_parameters_match_the_protocol(protocol: Protocol, method: str) -> None:
    authoritative = protocol.event_parameters(method)
    assert authoritative is not None, f"{method} is not in this Chrome's protocol"
    assert not compare(cdp.EVENTS[method], authoritative, method)


def test_every_module_is_checked() -> None:
    """A domain module added without a line in ``DOMAIN_OF_MODULE`` would have
    its nested types checked by nothing at all, and nothing would say so."""
    modules = {
        name
        for name, value in vars(cdp).items()
        if inspect.ismodule(value) and value.__name__.startswith("wirespec.cdp.") and name != "base"
    }
    assert modules == set(DOMAIN_OF_MODULE)


@pytest.mark.parametrize("method", sorted(set(cdp.COMMANDS) - set(commands_with_results())), ids=str)
def test_a_void_command_is_void_in_the_protocol(protocol: Protocol, method: str) -> None:
    """The other half of the result check: a command wirespec declares
    ``Command[None]`` whose reply *does* carry something is a result thrown away
    silently, and no decode ever fails to say so."""
    assert protocol.command_returns(method) == {}, f"{method} returns something wirespec discards"


@pytest.mark.parametrize(
    ("domain", "name", "struct"), nested_structs(), ids=lambda value: value if isinstance(value, str) else ""
)
def test_nested_type_properties_match_the_protocol(
    protocol: Protocol, domain: str, name: str, struct: type[CDPStruct]
) -> None:
    authoritative = protocol.type_properties(domain, name)
    assert authoritative is not None, f"{domain}.{name} is not a type this Chrome's protocol defines"
    assert not compare(struct, authoritative, f"{domain}.{name}")


# msgspec's name for a type, per protocol type. A CDP "number" accepts an int
# field because every integer is a valid number; the reverse is not true, and
# an int field fed 1.5 fails the decode.
ACCEPTS = {
    "string": {"StrType", "EnumType"},
    "integer": {"IntType"},
    "number": {"FloatType", "IntType"},
    "boolean": {"BoolType"},
    "array": {"ListType", "TupleType", "VarTupleType"},
    "object": {"DictType", "StructType", "AnyType", "RawType"},
}


def msgspec_type_names(field: mi.Field) -> set[str]:
    """The concrete types a field accepts, with the ``None`` of an optional
    stripped -- ``str | None`` is a string field, not a union of two things."""
    union = getattr(field.type, "types", None)
    if union is None:
        return {type(field.type).__name__}
    return {type(member).__name__ for member in union} - {"NoneType"}


def wrong_types(
    struct: type[CDPStruct], authoritative: dict[str, dict], where: str, protocol: Protocol, domain: str
) -> list[str]:
    mistakes = []
    for field in mi.type_info(struct).fields:  # type: ignore[union-attr]
        declaration = authoritative.get(field.encode_name)
        if declaration is None:
            continue
        declared = protocol.base_type(declaration, domain)
        names = msgspec_type_names(field)
        if declared in ACCEPTS and names and not (names & ACCEPTS[declared]):
            mistakes.append(f"{where}.{field.encode_name}: protocol {declared!r}, struct {sorted(names)}")
    return mistakes


def over_required(struct: type[CDPStruct], authoritative: dict[str, dict], where: str) -> list[str]:
    """Fields the struct insists on that the protocol says may be absent.

    Only meaningful for things wirespec *receives*. When Chrome omits one, the
    decode fails -- and a decode failure on the read path is reported rather
    than raised, so the event does not arrive and nothing says why. That is
    exactly how ``documentURL`` hid.

    Going the other way is not a bug: a *command* may require more than the
    protocol does, which is wirespec narrowing its own API on purpose.
    """
    return [
        f"{where}.{field.encode_name}: protocol says optional, struct requires it"
        for field in mi.type_info(struct).fields  # type: ignore[union-attr]
        if field.required and authoritative.get(field.encode_name, {}).get("optional")
    ]


@pytest.mark.parametrize("method", sorted(cdp.EVENTS), ids=str)
def test_events_do_not_require_what_the_protocol_calls_optional(protocol: Protocol, method: str) -> None:
    authoritative = protocol.event_parameters(method)
    assert authoritative is not None
    assert not over_required(cdp.EVENTS[method], authoritative, method)


@pytest.mark.parametrize("method", commands_with_results(), ids=str)
def test_results_do_not_require_what_the_protocol_calls_optional(protocol: Protocol, method: str) -> None:
    returns = cdp.COMMANDS[method].__returns__
    assert isinstance(returns, type) and issubclass(returns, CDPStruct)
    authoritative = protocol.command_returns(method)
    assert authoritative is not None
    assert not over_required(returns, authoritative, f"{method} (result)")


@pytest.mark.parametrize(
    ("domain", "name", "struct"), nested_structs(), ids=lambda value: value if isinstance(value, str) else ""
)
def test_nested_types_do_not_require_what_the_protocol_calls_optional(
    protocol: Protocol, domain: str, name: str, struct: type[CDPStruct]
) -> None:
    authoritative = protocol.type_properties(domain, name)
    assert authoritative is not None, f"{domain}.{name} is not a type this Chrome's protocol defines"
    assert not over_required(struct, authoritative, f"{domain}.{name}")


@pytest.mark.parametrize("method", sorted(cdp.COMMANDS), ids=str)
def test_command_parameter_types_match_the_protocol(protocol: Protocol, method: str) -> None:
    domain = method.partition(".")[0]
    authoritative = protocol.command_parameters(method)
    assert authoritative is not None
    assert not wrong_types(cdp.COMMANDS[method], authoritative, method, protocol, domain)


@pytest.mark.parametrize("method", sorted(cdp.EVENTS), ids=str)
def test_event_parameter_types_match_the_protocol(protocol: Protocol, method: str) -> None:
    domain = method.partition(".")[0]
    authoritative = protocol.event_parameters(method)
    assert authoritative is not None
    assert not wrong_types(cdp.EVENTS[method], authoritative, method, protocol, domain)


@pytest.mark.parametrize(
    ("domain", "name", "struct"), nested_structs(), ids=lambda value: value if isinstance(value, str) else ""
)
def test_nested_type_field_types_match_the_protocol(
    protocol: Protocol, domain: str, name: str, struct: type[CDPStruct]
) -> None:
    authoritative = protocol.type_properties(domain, name)
    assert authoritative is not None, f"{domain}.{name} is not a type this Chrome's protocol defines"
    assert not wrong_types(struct, authoritative, f"{domain}.{name}", protocol, domain)
