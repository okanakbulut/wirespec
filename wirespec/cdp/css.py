"""``CSS`` — computed style, which is how wirespec decides what is visible.

Only the computed-style half of the domain is here. §5.2 defines
visible as a non-empty box model *and* not ``visibility: hidden`` -- and
deliberately **not** opacity and **not** in-viewport, both of which make correct
specs flake.

At 0.721 ms this is the most expensive per-node call in the resolution set, an
order above ``getBoxModel``'s 0.089 ms, which is why visibility is asked about
once per element and not once per check.
"""

from typing import ClassVar

from wirespec.cdp.base import CDPStruct, Command

__all__ = [
    "CSSComputedStyleProperty",
    "Disable",
    "Enable",
    "GetComputedStyleForNode",
    "GetComputedStyleForNodeResult",
]


class CSSComputedStyleProperty(CDPStruct):
    name: str
    value: str


class Enable(Command[None]):
    __method__: ClassVar[str] = "CSS.enable"


class Disable(Command[None]):
    __method__: ClassVar[str] = "CSS.disable"


class GetComputedStyleForNodeResult(CDPStruct):
    #: Every property, not a chosen subset: CDP has no way to ask for less.
    computed_style: list[CSSComputedStyleProperty]


class GetComputedStyleForNode(Command[GetComputedStyleForNodeResult]):
    __method__: ClassVar[str] = "CSS.getComputedStyleForNode"

    node_id: int
