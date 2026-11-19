"""Controls of a ventilation unit: operating mode and filter run time.

The operating mode is written the way the manufacturer's app writes it: one
string on ``/ventilation/<zone>/operationMode``, chosen from the values the
device advertises. The fan level is read from ``exhaustFanLevel`` (a sensor)
and changed through the operating mode, because the unit keeps the level
itself read-only.

Pure data without Home Assistant imports, like ``write_policies``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

from .write_policies import EnumWritePolicy, NumberWritePolicy

# The operating modes the app knows, in the order it lists them. Only the
# advertised ones are offered; ``exhaustOnly`` (exhaust air only, bypass) is
# left out because the app hides it from the list as well.
VENTILATION_OPERATION_MODES: Final = (
    "time",
    "off",
    "min",
    "red",
    "nom",
    "max",
    "dem",
    "party",
    "powerVent",
    "fireplace",
    "fallAsleep",
    "freefct",
)

# Created enabled, as an explicit exception to the disabled default of new
# controls. ``off`` and ``fireplace`` stop or change the air supply of the
# whole unit (see docs/heating-controls.md).
VENTILATION_OPERATION_MODE_POLICY: Final = EnumWritePolicy(
    r"^/ventilation/zone[^/]+/operationMode$",
    frozenset({"stringValue"}),
    frozenset(VENTILATION_OPERATION_MODES),
    known_only=True,
)

# The filter run time limit in months. Its limits are required (a number
# entity needs them); the unit is the one the device reports. Disabled by
# default. The filter itself is never reset.
FILTER_RUN_TIME_UNITS: Final = frozenset({"month", "months"})
FILTER_RUN_TIME_POLICY: Final = NumberWritePolicy(
    r"^/ventilation/zone[^/]+/filter/maxRunTime$",
    "month",
    1.0,
    enabled_by_default=False,
    live_step=True,
    accepted_units=FILTER_RUN_TIME_UNITS,
    ui_unit_override="mo",
)

# The select an earlier version created on the exhaust fan level. The unit
# keeps the level read-only, so it is a sensor only and the select is removed.
_RETIRED_FAN_SELECT: Final = re.compile(
    r"^(?P<gateway>[^:]+):ventilation:[^:]+:exhaustFanLevel:control$"
)


def is_retired_fan_level_select(
    domain: str, unique_id: str, gateway_ids: Iterable[str]
) -> bool:
    """Whether a registry entry is the former exhaust fan level select of one
    of these gateways; no other entity matches."""
    match = _RETIRED_FAN_SELECT.fullmatch(unique_id)
    return (
        domain == "select"
        and match is not None
        and match.group("gateway") in set(gateway_ids)
    )
