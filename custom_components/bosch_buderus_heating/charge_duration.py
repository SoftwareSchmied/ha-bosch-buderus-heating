"""The unit of the extra hot water duration is not settled on every gateway.

A room controller on a KM200 reports the duration in "mins" with limits
such as 225 to 960 (15 minutes would be 225, one hour 900), which suggests
a raw value in units of a fifteenth of a minute. Nothing confirms the scale
and no conversion is guessed: the control is created disabled, so it is
never written by accident, and the documentation lists it as unclear.
"""

from __future__ import annotations

import re
from typing import Final

from .pointt import ResourceMetadata

_PATH: Final = re.compile(r"^/dhwCircuits/[^/]+/chargeDuration$")
# The shortest extra charge a plant offers is some minutes; a lower limit of
# two hours or more in "mins" cannot be a count of minutes.
UNCLEAR_MINIMUM: Final = 120.0


def scale_is_unclear(path: str, metadata: ResourceMetadata) -> bool:
    """The duration reports minutes with a lower limit of two hours or more."""
    return (
        _PATH.fullmatch(path) is not None
        and isinstance(metadata.unit, str)
        and metadata.unit.strip().casefold() in {"min", "mins", "minute", "minutes"}
        and metadata.minimum is not None
        and metadata.minimum >= UNCLEAR_MINIMUM
    )
