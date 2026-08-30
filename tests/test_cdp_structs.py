"""The protocol subset itself: names, wire shapes, and the defaults rule."""

import re

import msgspec
import pytest

from wirespec import cdp
from wirespec.cdp import accessibility, browser, dom, fetch, network, page, runtime, target
from wirespec.cdp import input as input_domain
from wirespec.cdp.base import Command, Event, decoder_for

METHOD = re.compile(r"^[A-Z][A-Za-z]*\.[a-z][A-Za-z0-9]*$")

#: Command fields whose default is deliberately not falsy, because CDP's own
#: default for that parameter is not falsy either. Anything not on this list
#: with a truthy default is a bug: `omit_defaults` would drop it, and Chrome
#: would apply its own default instead of the one the struct promises.
NON_FALSY_DEFAULTS_MATCHING_CDP = {
    (page.CaptureScreenshot, "from_surface"),
    # CDP defaults both of these depths to 1, and both structs say so.
    (dom.GetDocument, "depth"),
    (dom.DescribeNode, "depth"),
    # CDP defaults fetchRelatives to true. The resolver passes False explicitly,
    # which is sent precisely because it differs from the default.
    (accessibility.GetPartialAXTree, "fetch_relatives"),
}


def all_commands() -> list[type[Command]]:
    return list(cdp.COMMANDS.values())


def all_events() -> list[type[Event]]:
    return list(cdp.EVENTS.values())


@pytest.mark.parametrize("command", all_commands(), ids=lambda c: c.__method__)
def test_command_method_is_well_formed(command: type[Command]) -> None:
    assert METHOD.match(command.__method__), command.__method__


@pytest.mark.parametrize("event", all_events(), ids=lambda e: e.__method__)
def test_event_method_is_well_formed(event: type[Event]) -> None:
    assert METHOD.match(event.__method__), event.__method__


@pytest.mark.parametrize("command", all_commands(), ids=lambda c: c.__method__)
def test_every_command_has_a_usable_decoder(command: type[Command]) -> None:
    """A command either declares a result type that msgspec can decode, or
    declares None and gets no decoder at all."""
    decoder = decoder_for(command)
    if command.__returns__ is type(None):
        assert decoder is None
    else:
        assert isinstance(decoder, msgspec.json.Decoder)


@pytest.mark.parametrize("event", all_events(), ids=lambda e: e.__method__)
def test_every_event_decodes_or_has_no_parameters(event: type[Event]) -> None:
    decoder = decoder_for(event)
    if event.__struct_fields__:
        assert isinstance(decoder, msgspec.json.Decoder)
    else:
        assert decoder is None, "a parameterless event should not build a decoder it never uses"


@pytest.mark.parametrize("command", all_commands(), ids=lambda c: c.__method__)
def test_omitted_defaults_are_cdp_defaults(command: type[Command]) -> None:
    """The rule from ``cdp.base``: with ``omit_defaults`` on, a field still
    holding its default is not sent at all, so the default had better be the one
    Chrome would have applied anyway."""
    if not command.__struct_config__.omit_defaults:
        return
    for name, default in zip(command.__struct_fields__, command.__struct_defaults__, strict=False):
        if (command, name) in NON_FALSY_DEFAULTS_MATCHING_CDP:
            continue
        assert not default, (
            f"{command.__method__}.{name} defaults to {default!r}, which omit_defaults will drop. "
            f"Either the default is CDP's too (add it to NON_FALSY_DEFAULTS_MATCHING_CDP) "
            f"or the struct needs omit_defaults=False."
        )


def test_registries_have_no_duplicates() -> None:
    assert len(cdp.COMMANDS) == len({c.__method__ for c in cdp.COMMANDS.values()})
    assert len(cdp.EVENTS) == len({e.__method__ for e in cdp.EVENTS.values()})
    assert not (set(cdp.COMMANDS) & set(cdp.EVENTS))


def test_field_names_go_out_as_camel_case() -> None:
    assert msgspec.json.encode(target.CreateTarget(url="about:blank", browser_context_id="B")) == (
        b'{"url":"about:blank","browserContextId":"B"}'
    )
    assert msgspec.json.encode(network.SetExtraHTTPHeaders(headers={"X-A": "1"})) == b'{"headers":{"X-A":"1"}}'


def test_a_command_with_no_parameters_encodes_to_nothing() -> None:
    assert browser.GetVersion.__struct_fields__ == ()
    assert msgspec.json.encode(browser.GetVersion()) == b"{}"


def test_flatten_is_always_on_the_wire() -> None:
    """§3.3: a non-flat session is not something wirespec can route,
    and CDP's own default for flatten is false."""
    assert msgspec.json.encode(target.AttachToTarget(target_id="T")) == b'{"targetId":"T","flatten":true}'


def test_dispose_on_detach_is_always_on_the_wire() -> None:
    assert msgspec.json.encode(target.CreateBrowserContext()) == b'{"disposeOnDetach":true}'


def test_set_cookie_sends_the_flags_it_was_asked_for() -> None:
    encoded = msgspec.json.encode(network.SetCookie(name="a", value="b", url="http://x/"))
    assert msgspec.json.decode(encoded) == {
        "name": "a",
        "value": "b",
        "url": "http://x/",
        "path": "/",
        "secure": False,
        "httpOnly": False,
    }


def test_call_argument_tells_null_apart_from_nothing() -> None:
    """JavaScript null and JavaScript undefined are different arguments, and only
    UNSET keeps them apart under omit_defaults."""
    assert msgspec.json.encode(runtime.CallArgument()) == b"{}"
    assert msgspec.json.encode(runtime.CallArgument(value=None)) == b'{"value":null}'
    assert msgspec.json.encode(runtime.CallArgument(value=0)) == b'{"value":0}'
    assert msgspec.json.encode(runtime.CallArgument(object_id="O")) == b'{"objectId":"O"}'


def test_runtime_timeout_keeps_its_protocol_name() -> None:
    """wirespec counts seconds and CDP counts milliseconds here, so the field is
    named for what it holds and renamed for what the wire expects."""
    assert msgspec.json.encode(runtime.Evaluate(expression="1", timeout_ms=250.0)) == (
        b'{"expression":"1","timeout":250.0}'
    )


def test_mouse_move_can_name_the_held_button() -> None:
    """§8.1 -- without this a drag never starts."""
    encoded = msgspec.json.decode(
        msgspec.json.encode(input_domain.DispatchMouseEvent(type="mouseMoved", x=1, y=2, button="left", buttons=1))
    )
    assert encoded == {"type": "mouseMoved", "x": 1.0, "y": 2.0, "button": "left", "buttons": 1}


def test_events_decode_from_realistic_payloads() -> None:
    loaded = msgspec.json.decode(b'{"timestamp":123.5}', type=page.LoadEventFired)
    assert loaded.timestamp == 123.5

    attached = msgspec.json.decode(
        b'{"sessionId":"S","targetInfo":{"targetId":"T","type":"page","title":"t",'
        b'"url":"about:blank","attached":true,"browserContextId":"B"},"waitingForDebugger":false}',
        type=target.AttachedToTarget,
    )
    assert attached.session_id == "S"
    assert attached.target_info.browser_context_id == "B"

    paused = msgspec.json.decode(
        b'{"requestId":"R","request":{"url":"http://x/","method":"GET","headers":{"Accept":"*/*"}},'
        b'"frameId":"F","resourceType":"Document"}',
        type=fetch.RequestPaused,
    )
    assert paused.request.headers == {"Accept": "*/*"}
    assert paused.response_status_code is None


def test_unknown_fields_are_ignored() -> None:
    """Chrome adds fields to events across versions; a driver that refuses them
    breaks on the next browser update rather than on a code change."""
    frame = msgspec.json.decode(
        b'{"frame":{"id":"F","url":"u","somethingChromeAddedIn160":true},"type":"Navigation"}',
        type=page.FrameNavigated,
    )
    assert frame.frame.id == "F"


def test_box_model_quads_are_eight_numbers() -> None:
    model = msgspec.json.decode(
        b'{"model":{"content":[0,0,1,0,1,1,0,1],"padding":[0,0,1,0,1,1,0,1],'
        b'"border":[0,0,2,0,2,2,0,2],"margin":[0,0,3,0,3,3,0,3],"width":2,"height":2}}',
        type=dom.GetBoxModelResult,
    )
    assert len(model.model.border) == 8
    assert model.model.border[2] == 2.0
