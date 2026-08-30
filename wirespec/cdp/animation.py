"""``Animation`` — which elements are moving, pushed rather than polled.

This domain exists here for exactly one caller: the "is it still moving" check
before an action (§5.2 step 6, §8.7).

That check has been asked three ways. The prototype asked
``document.getAnimations()``, which is permanently true on any page with one
indefinite animation anywhere and cost 64% of all protocol time. The
replacement read ``animation-name`` and ``transition-duration`` out of a
``DOMSnapshot``, which scoped the question correctly but paid for the whole
document to answer about one element -- 59.7 ms on a 14k-node page, on **every
action**.

Chrome will simply say. ``animationStarted`` names the animating element by
backend node id and carries how long it will run, so a page can keep the answer
as a set and the check costs no round trip at all. Two dividends beyond the
time:

* **A declared transition is not a running one.** The computed-style reading
  could not tell them apart -- ``transition-duration`` is non-zero on an element
  with a transition declared even when nothing is happening -- so every action
  on such an element waited an animation frame it had no reason to. This fires
  only when a transition actually runs.
* **It sees animations the styles do not.** ``element.animate()`` sets no
  ``animation-name``, so the snapshot reading missed the Web Animations API
  entirely. This domain reports every animation, however it was started.

**``animationCanceled`` names the animation, not the element**, which is why
the page has to keep the id and cannot keep only the node.
"""

from typing import ClassVar

from wirespec.cdp.base import CDPStruct, Command, Event

__all__ = ["Animation", "AnimationCanceled", "AnimationEffect", "AnimationStarted", "Disable", "Enable"]


class AnimationEffect(CDPStruct):
    """What the animation does, and for how long. Times are milliseconds.

    ``iterations`` is **absent for an animation that never ends** -- measured on
    ``animation: spin 1s linear infinite``, where every other field arrives and
    this one does not. So ``None`` is not "unknown", it is "forever", and
    treating it as a missing number would expire a spinner after one round.

    ``keyframesRule`` is deliberately not declared: it is the largest part of
    the message, nothing here reads it, and an undeclared field is not parsed.
    """

    backend_node_id: int | None = None
    delay: float = 0.0
    end_delay: float = 0.0
    iteration_start: float = 0.0
    #: None means it never ends. See above.
    iterations: float | None = None
    duration: float = 0.0
    direction: str = ""
    fill: str = ""
    easing: str = ""


class Animation(CDPStruct):
    """One running animation. ``type`` is ``CSSAnimation``, ``CSSTransition``
    or ``WebAnimation``; all three move an element and none is treated
    differently."""

    id: str
    name: str = ""
    paused_state: bool = False
    play_state: str = ""
    playback_rate: float = 1.0
    start_time: float = 0.0
    current_time: float = 0.0
    type: str = ""
    source: AnimationEffect | None = None


class Enable(Command[None]):
    """Start reporting animations. Measured at 0.21 ms, once per page."""

    __method__: ClassVar[str] = "Animation.enable"


class Disable(Command[None]):
    __method__: ClassVar[str] = "Animation.disable"


class AnimationStarted(Event):
    """An animation began. Carries the element and how long it will run."""

    __method__: ClassVar[str] = "Animation.animationStarted"

    animation: Animation


class AnimationCanceled(Event):
    """An animation stopped before it was due to.

    Fires when the rule is removed, when the element is, and when a transition
    is interrupted. **Only the animation's id is in the message**, so whatever
    is tracking elements has to have kept the mapping.
    """

    __method__: ClassVar[str] = "Animation.animationCanceled"

    id: str
