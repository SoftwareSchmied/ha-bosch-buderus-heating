"""What counts as a humidity reading and what as a placeholder.

A zone, a radio device or a heating circuit without a humidity sensor can
still report the resource. The controller then sends a value that is no
humidity: the number it declares as its error marker, or one outside the
range of a relative humidity (for example ``0``, ``-1`` or ``255``). Such a
value is shown as unknown, and a sensor first seen with one is created
disabled. A sensor that exists already keeps its settings. Pure data without
Home Assistant imports.
"""

from __future__ import annotations

import math
import re
from typing import Final, TypeGuard

from .pointt import Resource

HUMIDITY_PATH: Final = re.compile(
    r"^(?:/heatingCircuits/[^/]+/actualHumidity"
    r"|/zones/zone[^/]+/averageActualHumidity"
    r"|/devices/(?!list$)[^/]+/actualHumidity)$"
)

VALID: Final = "valid"
ERROR_MARKER: Final = "error_marker"
IMPLAUSIBLE: Final = "implausible"
MISSING: Final = "missing"
NOT_NUMERIC: Final = "not_numeric"


def is_humidity_path(path: str) -> bool:
    """Whether a path is a measured relative humidity of a zone, a radio
    device or a heating circuit."""
    return HUMIDITY_PATH.fullmatch(path) is not None


def plausible_humidity(value: object) -> TypeGuard[int | float]:
    """A finite number above 0 and up to 100 (a relative humidity of 0 does
    not occur indoors; sensors report it when there is no measurement)."""
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 < value <= 100
    )


def humidity_state(resource: Resource) -> str | None:
    """The kind of the current reading of a humidity resource, never the
    value itself; ``None`` for any other resource."""
    if not is_humidity_path(resource.path):
        return None
    if not resource.has_value:
        return MISSING
    value = resource.value
    if isinstance(value, bool) or not isinstance(value, int | float):
        return NOT_NUMERIC
    if value in resource.metadata.numeric_error_values:
        return ERROR_MARKER
    return VALID if plausible_humidity(value) else IMPLAUSIBLE


def is_placeholder(resource: Resource) -> bool:
    """A humidity resource whose reading is an error marker or lies outside
    the range of a relative humidity."""
    return humidity_state(resource) in {ERROR_MARKER, IMPLAUSIBLE}
