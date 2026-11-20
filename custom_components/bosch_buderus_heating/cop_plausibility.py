"""When a current COP is not a heating COP, or not plausible.

The current COP is the heat output over the electrical power of the heat
pump. It is not worked out

- while the heat pump cools: a cooling run would be taken for a heating COP
  (the heat output is zero, or it is the cooling capacity and the quotient an
  EER), and the long-term statistics would mix both; `reason: cooling`;
- when it stays below `LOW_COP` for `LOW_COP_SECONDS` with the compressor
  running: no heat pump delivers less heat than it takes in electricity for
  that long; the heat output reading is then more likely an electrical power
  (its meaning differs between models and firmware versions);
  `reason: implausible_low`;
- above `MAX_COP`: readings taken at different moments do not fit together
  (`reason: implausible`).

An unknown value is not recorded in the statistics.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from homeassistant.util import dt as dt_util

from .derived_activity import CIRCUIT, cooling_signalled, heat_side
from .derived_inputs import Trace, read, state

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator

COOLING_REASON: Final = "cooling"
IMPLAUSIBLE_LOW: Final = "implausible_low"
# A COP below this value over LOW_COP_SECONDS is not plausible; above MAX_COP
# a current COP is not shown.
LOW_COP: Final = 1.0
LOW_COP_SECONDS: Final = 600.0
MAX_COP: Final = 10.0
_SEASON: Final = re.compile(r"^/heatingCircuits/[^/]+/currentSuWiMode$")


def cooling_operation(
    coordinator: BoschBuderusDataUpdateCoordinator, trace: Trace
) -> bool:
    """The heat pump cools now: its own cooling signals, or the cooling
    season while it delivers to the circuits (not to the hot water)."""
    if cooling_signalled(coordinator, trace):
        return True
    if heat_side(coordinator) != CIRCUIT:
        return False
    for path in coordinator.data or {}:
        if _SEASON.fullmatch(path):
            reading = read(coordinator, path)
            if state(reading) == "cooling":
                trace.note(reading)
                return True
    return False


class LowCopWatch:
    """How long the COP has stayed below `LOW_COP` without a break."""

    def __init__(self) -> None:
        self._since: datetime | None = None

    def reset(self) -> None:
        self._since = None

    def observe(self, cop: float, now: datetime | None = None) -> bool:
        """Take a COP; `True` once it was below the limit for the whole time."""
        if cop >= LOW_COP:
            self._since = None
            return False
        now = now or dt_util.utcnow()
        if self._since is None:
            self._since = now
        return now - self._since >= timedelta(seconds=LOW_COP_SECONDS)
