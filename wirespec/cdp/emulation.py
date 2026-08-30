"""``Emulation`` — viewport size, and nothing else.

Mobile emulation beyond viewport size is deliberately out of scope
(§2). The viewport itself is not optional: a headless window's
default size is not a promise, and every measured box is relative to it
(§8.9).
"""

from typing import ClassVar

from wirespec.cdp.base import Command

__all__ = ["ClearDeviceMetricsOverride", "SetDeviceMetricsOverride", "SetFocusEmulationEnabled"]


class SetDeviceMetricsOverride(Command[None]):
    __method__: ClassVar[str] = "Emulation.setDeviceMetricsOverride"

    width: int
    height: int
    device_scale_factor: float
    mobile: bool


class ClearDeviceMetricsOverride(Command[None]):
    __method__: ClassVar[str] = "Emulation.clearDeviceMetricsOverride"


class SetFocusEmulationEnabled(Command[None]):
    __method__: ClassVar[str] = "Emulation.setFocusEmulationEnabled"

    enabled: bool
