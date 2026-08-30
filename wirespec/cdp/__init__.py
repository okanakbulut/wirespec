"""The subset of the Chrome DevTools Protocol wirespec speaks.

Every command and event is a msgspec ``Struct``. Domains are modules, so the
call site reads the way the protocol does::

    from wirespec.cdp import page, runtime

    await session.send(page.Navigate(url="https://example.test/"))
    result = await session.send(runtime.Evaluate(expression="1 + 1", return_by_value=True))

It is a *subset*, on purpose: what §6 needs and nothing more.
Adding a command means adding a struct, which is a few lines; until then
``Connection.send_raw`` will carry anything the protocol accepts.

Importing this package builds every decoder, so no first call pays for one.
"""

from wirespec.cdp import (
    accessibility,
    animation,
    browser,
    css,
    dom,
    domsnapshot,
    emulation,
    fetch,
    input,
    network,
    page,
    runtime,
    storage,
    target,
)
from wirespec.cdp.base import COMMANDS, EVENTS, CDPStruct, Command, Event, Headers, decoder_for, finalize

__all__ = [
    "COMMANDS",
    "EVENTS",
    "CDPStruct",
    "Command",
    "Event",
    "Headers",
    "accessibility",
    "animation",
    "browser",
    "css",
    "decoder_for",
    "dom",
    "domsnapshot",
    "emulation",
    "fetch",
    "finalize",
    "input",
    "network",
    "page",
    "runtime",
    "storage",
    "target",
]

finalize()
