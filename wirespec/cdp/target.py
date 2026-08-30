"""``Target`` — browser contexts, pages, and the flat sessions that address them.

Attachment is always flat (§3.3): one connection carries every
page, and each message names its ``sessionId``. The alternative,
``Target.sendMessageToTarget``, wraps protocol inside protocol and is
deprecated.
"""

from typing import ClassVar

from wirespec.cdp.base import CDPStruct, Command, Event

__all__ = [
    "AttachToTarget",
    "AttachToTargetResult",
    "AttachedToTarget",
    "CloseTarget",
    "CloseTargetResult",
    "CreateBrowserContext",
    "CreateBrowserContextResult",
    "CreateTarget",
    "CreateTargetResult",
    "DetachFromTarget",
    "DetachedFromTarget",
    "DisposeBrowserContext",
    "GetTargets",
    "GetTargetsResult",
    "SetDiscoverTargets",
    "TargetCrashed",
    "TargetCreated",
    "TargetDestroyed",
    "TargetInfo",
    "TargetInfoChanged",
]


class TargetInfo(CDPStruct):
    target_id: str
    type: str
    title: str
    url: str
    attached: bool
    opener_id: str | None = None
    can_access_opener: bool = False
    browser_context_id: str | None = None


class CreateBrowserContextResult(CDPStruct):
    browser_context_id: str


class CreateBrowserContext(Command[CreateBrowserContextResult], omit_defaults=False):
    """A real browser context: its own cookies, storage and cache.

    ``omit_defaults=False`` because ``dispose_on_detach`` defaults to *false* in
    CDP and to *true* here -- a context that outlives the session that made it
    is a leaked profile. With defaults omitted, asking for the value we want
    would send nothing and get the value we do not.
    """

    __method__: ClassVar[str] = "Target.createBrowserContext"

    dispose_on_detach: bool = True


class DisposeBrowserContext(Command[None]):
    __method__: ClassVar[str] = "Target.disposeBrowserContext"

    browser_context_id: str


class CreateTargetResult(CDPStruct):
    target_id: str


class CreateTarget(Command[CreateTargetResult]):
    __method__: ClassVar[str] = "Target.createTarget"

    url: str
    browser_context_id: str | None = None
    width: int | None = None
    height: int | None = None


class CloseTargetResult(CDPStruct):
    success: bool = True


class CloseTarget(Command[CloseTargetResult]):
    __method__: ClassVar[str] = "Target.closeTarget"

    target_id: str


class AttachToTargetResult(CDPStruct):
    session_id: str


class AttachToTarget(Command[AttachToTargetResult], omit_defaults=False):
    """``omit_defaults=False``: CDP defaults ``flatten`` to false, and a non-flat
    session is not something wirespec knows how to route."""

    __method__: ClassVar[str] = "Target.attachToTarget"

    target_id: str
    flatten: bool = True


class DetachFromTarget(Command[None]):
    __method__: ClassVar[str] = "Target.detachFromTarget"

    session_id: str | None = None
    target_id: str | None = None


class GetTargetsResult(CDPStruct):
    target_infos: list[TargetInfo]


class GetTargets(Command[GetTargetsResult]):
    __method__: ClassVar[str] = "Target.getTargets"


class SetDiscoverTargets(Command[None]):
    """Turns the ``target*`` events on. Without it a page that crashes is simply
    a page that stops answering."""

    __method__: ClassVar[str] = "Target.setDiscoverTargets"

    discover: bool


class AttachedToTarget(Event):
    __method__: ClassVar[str] = "Target.attachedToTarget"

    session_id: str
    target_info: TargetInfo
    waiting_for_debugger: bool = False


class DetachedFromTarget(Event):
    __method__: ClassVar[str] = "Target.detachedFromTarget"

    session_id: str
    target_id: str | None = None


class TargetCreated(Event):
    __method__: ClassVar[str] = "Target.targetCreated"

    target_info: TargetInfo


class TargetDestroyed(Event):
    __method__: ClassVar[str] = "Target.targetDestroyed"

    target_id: str


class TargetCrashed(Event):
    __method__: ClassVar[str] = "Target.targetCrashed"

    target_id: str
    status: str
    error_code: int


class TargetInfoChanged(Event):
    __method__: ClassVar[str] = "Target.targetInfoChanged"

    target_info: TargetInfo
