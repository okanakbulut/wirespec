"""Machinery shared by every protocol struct.

Every CDP command and event wirespec speaks is a msgspec ``Struct``, never a
dict. Three things follow from that, in order of how much they matter:

* **The result type travels with the command.** ``Command[R]`` is generic, so
  ``await session.send(browser.GetVersion())`` is typed ``GetVersionResult``
  with no cast at the call site and no stringly-typed ``result["userAgent"]``
  anywhere downstream.
* **Encoding is one C-level pass.** msgspec walks the struct's own layout; there
  is no intermediate dict built per call, and ``rename="camel"`` is resolved
  once at class creation, not per message.
* **Decoding is targeted.** Each command and event owns a prebuilt
  ``msgspec.json.Decoder``. Building one is expensive and using one is cheap, so
  they are built once and reused for the life of the process.

**The one rule for defaults.** ``omit_defaults=True`` drops any field still
holding its default, so *a struct's defaults must be CDP's own defaults* --
otherwise asking for the default explicitly silently sends nothing and Chrome
applies its own. Where wirespec wants a value CDP does not default to, as with
``Target.attachToTarget(flatten=True)``, declare that struct
``omit_defaults=False`` and give it no optional fields, so every field is always
on the wire.

Use ``msgspec.UNSET`` for an optional field whose ``null`` is meaningful: a
``Runtime.CallArgument`` passing JavaScript ``null`` is not the same as one
passing nothing, and only ``UNSET`` keeps them apart.
"""

import typing
from typing import Any, ClassVar, TypeVar

import msgspec
from msgspec import Struct

__all__ = ["COMMANDS", "EVENTS", "CDPStruct", "Command", "Event", "Headers", "decoder_for", "finalize"]

Headers = dict[str, str]

_NONE = type(None)

#: Distinguishes "no decoder built yet" from "built, and there is nothing to
#: decode" -- a void command's ``{}``, or an event with no parameters.
_UNBUILT: Any = object()

#: Every command and event class, by protocol method name. Populated at import
#: by ``__init_subclass__``, and used for duplicate detection, for the raw
#: escape hatch, and by the tests that check the subset against real Chrome.
COMMANDS: dict[str, type[Command[Any]]] = {}
EVENTS: dict[str, type[Event]] = {}


class CDPStruct(Struct, rename="camel", omit_defaults=True):
    """A struct whose fields are snake_case here and camelCase on the wire."""


class Command[R](CDPStruct):
    """One CDP command. The fields are its parameters; ``R`` is its result.

    Subclasses set ``__method__`` to the protocol name verbatim, so it greps
    against the CDP reference. ``__returns__`` is derived from the
    ``Command[...]`` parametrisation and is never written by hand.
    """

    __method__: ClassVar[str] = ""
    __returns__: ClassVar[Any] = _NONE
    __decoder__: ClassVar[Any] = _UNBUILT

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for base in cls.__dict__.get("__orig_bases__", ()):
            if typing.get_origin(base) is Command:
                (returns,) = typing.get_args(base)
                if not isinstance(returns, TypeVar):
                    cls.__returns__ = returns
        method = cls.__dict__.get("__method__")
        if not method:
            return
        if method in COMMANDS:
            raise RuntimeError(f"{method} is already defined by {COMMANDS[method].__name__}")
        COMMANDS[method] = cls


class Event(CDPStruct):
    """One CDP event. The fields are its ``params``.

    An event with no parameters is a struct with no fields, and gets no decoder
    at all: the dispatcher constructs it directly rather than parsing ``{}``.
    """

    __method__: ClassVar[str] = ""
    __decoder__: ClassVar[Any] = _UNBUILT

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        method = cls.__dict__.get("__method__")
        if not method:
            return
        if method in EVENTS:
            raise RuntimeError(f"{method} is already defined by {EVENTS[method].__name__}")
        EVENTS[method] = cls


def decoder_for(cls: type[Command[Any] | Event]) -> msgspec.json.Decoder[Any] | None:
    """The decoder for what ``cls`` puts on the wire, or ``None`` if nothing.

    Deferred rather than done in ``__init_subclass__`` for two reasons: msgspec
    refuses to build a ``Decoder`` for a Struct from inside that struct's own
    ``__init_subclass__``, because the class object is not finished yet; and a
    command's result type is free to be defined after the command itself.
    """
    decoder = cls.__dict__.get("__decoder__", _UNBUILT)
    if decoder is not _UNBUILT:
        return decoder
    if issubclass(cls, Command):
        target = cls.__returns__
        decoder = None if target is _NONE else msgspec.json.Decoder(target)
    else:
        decoder = msgspec.json.Decoder(cls) if cls.__struct_fields__ else None
    cls.__decoder__ = decoder
    return decoder


def finalize() -> None:
    """Build every decoder up front, so no first call pays for one.

    Idempotent. ``wirespec.cdp`` calls this once its domains are imported;
    anything defining a command or event afterwards should call it again.
    """
    for command in COMMANDS.values():
        decoder_for(command)
    for event in EVENTS.values():
        decoder_for(event)
