"""Switch-on and switch-off timers of the additional-cloud air conditioners.

The device stores each timer as an absolute time in whole seconds since the
epoch (``onTimestamp`` and ``offTimestamp``); 0 means no timer. The entities
show the minutes that remain and send a minute-aligned absolute time.
"""

from __future__ import annotations

from math import ceil
from time import time
from typing import Any, Final

from .metadata import metadata, writable
from .models import Device
from .rac import reported, supported

FIELDS: Final = {"on": "onTimestamp", "off": "offTimestamp"}
STEP_SECONDS: Final = 60
# The least time between the command and the timer time.
MIN_LEAD_SECONDS: Final = 10
# The app offers today and tomorrow; one day is the smallest span within that.
MAX_MINUTES: Final = 24 * 60
# A reported time further away than this cannot come from a timer we can show.
_MAX_ABSOLUTE: Final = 4_102_444_800


def now() -> float:
    return time()


def stored(device: Device, field: str) -> int | None:
    """The reported timer time; None when absent or not a plausible time."""
    value = reported(device, field)
    if type(value) is not int or not 0 <= value <= _MAX_ABSOLUTE:
        return None
    return value


def declared(device: Device, field: str) -> bool:
    """The timer exists on this device: writable declaration, valid value.

    A declared step other than the 60 seconds the app uses is not offered.
    """
    meta = metadata(device, "state", field)
    if not writable(meta) or meta is None or stored(device, field) is None:
        return False
    step = meta.get("step")
    return step is None or (type(step) is int and step == STEP_SECONDS)


def remaining_minutes(
    device: Device, field: str, at: float | None = None
) -> int | None:
    """Whole minutes until the timer fires (rounded up); 0 without a timer."""
    value = stored(device, field)
    if value is None:
        return None
    current = now() if at is None else at
    if value <= current:
        return 0
    return min(MAX_MINUTES, ceil((value - current) / STEP_SECONDS))


def target_time(minutes: int, at: float | None = None) -> int:
    """The minute-aligned absolute time that lies ``minutes`` ahead, at least
    ``MIN_LEAD_SECONDS`` from now: the message needs time to reach the device,
    so a time that would come sooner moves to the next minute."""
    current = now() if at is None else at
    target = (int(current) // STEP_SECONDS + minutes) * STEP_SECONDS
    if target - current < MIN_LEAD_SECONDS:
        target += STEP_SECONDS
    return target


def other_field(field: str) -> str:
    return FIELDS["off"] if field == FIELDS["on"] else FIELDS["on"]


def command_valid(
    device: Device,
    desired: dict[str, Any],
    expected_model: str,
    at: float | None = None,
) -> bool:
    """One timer field, from fresh state, never equal to the other timer's minute.

    The app refuses two timers in the same minute and a time in the past.
    """
    if (
        not supported(device)
        or device.model != expected_model
        or device.state is None
        or len(desired) != 1
    ):
        return False
    field, value = next(iter(desired.items()))
    if field not in FIELDS.values() or not declared(device, field):
        return False
    if type(value) is not int:
        return False
    if value == 0:
        return True
    current = now() if at is None else at
    if not current < value <= current + (MAX_MINUTES + 1) * STEP_SECONDS:
        return False
    if value % STEP_SECONDS:
        return False
    other = stored(device, other_field(field))
    return not (
        other is not None
        and other > current
        and other // STEP_SECONDS == value // STEP_SECONDS
    )
