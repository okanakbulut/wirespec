"""``Runtime`` — evaluation in the page, and the objects it hands back.

This is the domain wirespec leans on hardest: every locator resolution, probe
and actionability verdict is one ``Runtime.callFunctionOn`` against the in-page
runtime (§3.4).
"""

from typing import Any, ClassVar

from msgspec import UNSET, UnsetType, field

from wirespec.cdp.base import CDPStruct, Command, Event

__all__ = [
    "AddBinding",
    "BindingCalled",
    "CallArgument",
    "CallFrame",
    "CallFunctionOn",
    "ConsoleAPICalled",
    "Disable",
    "Enable",
    "Evaluate",
    "EvaluationResult",
    "ExceptionDetails",
    "ExceptionThrown",
    "ExecutionContextCreated",
    "ExecutionContextDescription",
    "ExecutionContextDestroyed",
    "ExecutionContextsCleared",
    "ReleaseObject",
    "ReleaseObjectGroup",
    "RemoteObject",
    "RemoveBinding",
    "StackTrace",
]


class RemoteObject(CDPStruct):
    """A value in the page: either serialised into ``value``, or referenced by
    ``object_id`` and left there. Leaving it there is the point of
    ``evaluate_all`` — two hundred rows cost one round trip because the array
    never crosses the wire."""

    type: str
    subtype: str | None = None
    class_name: str | None = None
    value: Any = None
    unserializable_value: str | None = None
    description: str | None = None
    object_id: str | None = None


class CallFrame(CDPStruct):
    function_name: str
    script_id: str
    url: str
    line_number: int
    column_number: int


class StackTrace(CDPStruct):
    call_frames: list[CallFrame]
    description: str | None = None
    # Quoted, and staying quoted: under PEP 649 the annotation is resolved
    # lazily, but msgspec builds the struct layout eagerly and the class does
    # not exist yet at that point. noqa: UP037 -- these quotes are load-bearing.
    parent: "StackTrace | None" = None  # noqa: UP037


class ExceptionDetails(CDPStruct):
    """A page-side throw. ``exception.description`` carries the JavaScript
    stack, which is the part worth putting in a Python traceback."""

    exception_id: int
    text: str
    line_number: int
    column_number: int
    script_id: str | None = None
    url: str | None = None
    stack_trace: StackTrace | None = None
    exception: RemoteObject | None = None
    execution_context_id: int | None = None


class CallArgument(CDPStruct):
    """One argument to ``callFunctionOn``.

    ``value`` defaults to ``UNSET`` rather than ``None`` because the two mean
    different things here: an omitted ``value`` passes JavaScript ``undefined``,
    while ``value=None`` passes ``null``. With a ``None`` default,
    ``omit_defaults`` would collapse the second into the first.
    """

    value: Any = UNSET
    unserializable_value: str | UnsetType = UNSET
    object_id: str | UnsetType = UNSET


class ExecutionContextDescription(CDPStruct):
    id: int
    origin: str
    name: str
    unique_id: str = ""
    aux_data: dict[str, Any] | None = None


class EvaluationResult(CDPStruct):
    """The reply shape of both ``evaluate`` and ``callFunctionOn``.

    ``exception_details`` being set is not a protocol error — the command
    succeeded and the *page* threw — so it comes back in the result rather than
    as a ``CDPError``, and the caller decides what to do about it.
    """

    result: RemoteObject
    exception_details: ExceptionDetails | None = None


class Enable(Command[None]):
    __method__: ClassVar[str] = "Runtime.enable"


class Disable(Command[None]):
    __method__: ClassVar[str] = "Runtime.disable"


class Evaluate(Command[EvaluationResult]):
    __method__: ClassVar[str] = "Runtime.evaluate"

    expression: str
    context_id: int | None = None
    unique_context_id: str | None = None
    object_group: str | None = None
    return_by_value: bool = False
    await_promise: bool = False
    user_gesture: bool = False
    silent: bool = False
    # "includeCommandLineAPI", with the acronym uppercase. Under the camel
    # rename this goes out as "includeCommandLineApi", which Chrome accepts
    # without complaint and ignores -- so $$ and friends are simply absent and
    # nothing says why.
    include_command_line_api: bool = field(default=False, name="includeCommandLineAPI")
    # CDP calls this "timeout" and counts milliseconds. wirespec counts seconds
    # everywhere else (§4.3), so the name says which this is and
    # the wire name stays correct.
    timeout_ms: float | None = field(default=None, name="timeout")


class CallFunctionOn(Command[EvaluationResult]):
    """Note that the handle arrives as ``this``, not as an argument — see
    §8.9."""

    __method__: ClassVar[str] = "Runtime.callFunctionOn"

    function_declaration: str
    object_id: str | None = None
    execution_context_id: int | None = None
    unique_context_id: str | None = None
    arguments: list[CallArgument] | None = None
    object_group: str | None = None
    return_by_value: bool = False
    await_promise: bool = False
    user_gesture: bool = False
    silent: bool = False


class ReleaseObject(Command[None]):
    __method__: ClassVar[str] = "Runtime.releaseObject"

    object_id: str


class ReleaseObjectGroup(Command[None]):
    __method__: ClassVar[str] = "Runtime.releaseObjectGroup"

    object_group: str


class AddBinding(Command[None]):
    __method__: ClassVar[str] = "Runtime.addBinding"

    name: str
    execution_context_name: str | None = None


class RemoveBinding(Command[None]):
    __method__: ClassVar[str] = "Runtime.removeBinding"

    name: str


class ExecutionContextCreated(Event):
    __method__: ClassVar[str] = "Runtime.executionContextCreated"

    context: ExecutionContextDescription


class ExecutionContextDestroyed(Event):
    __method__: ClassVar[str] = "Runtime.executionContextDestroyed"

    execution_context_id: int | None = None
    execution_context_unique_id: str | None = None


class ExecutionContextsCleared(Event):
    __method__: ClassVar[str] = "Runtime.executionContextsCleared"


class ConsoleAPICalled(Event):
    __method__: ClassVar[str] = "Runtime.consoleAPICalled"

    type: str
    args: list[RemoteObject]
    execution_context_id: int
    timestamp: float
    stack_trace: StackTrace | None = None
    context: str | None = None


class ExceptionThrown(Event):
    __method__: ClassVar[str] = "Runtime.exceptionThrown"

    timestamp: float
    exception_details: ExceptionDetails


class BindingCalled(Event):
    __method__: ClassVar[str] = "Runtime.bindingCalled"

    name: str
    payload: str
    execution_context_id: int
