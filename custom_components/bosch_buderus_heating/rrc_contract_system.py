"""RRC 2.0 contract of the system: status, maintenance, energy history.

Owned by work package W5. Empty tables change nothing; see
``docs/dev/rrc-packages.md`` for what each table does.

The system values are read only; nothing here is written (S9). The
entities of these paths live in ``rrc_system_entities``; the generic sensor
of such a path stays a disabled diagnostic one (``read_only_mirrors``), so
an existing entity keeps its ID and nothing shows twice. The energy history
is no discovered resource at all: ``rrc_energy_statistics`` reads it with
its own bounded budget (P39), so ``/energy`` is no extra root here.

No optional leaves are probed below ``/system/appliance``: the TC100.2 tree
of issue 122 lists them as references, and a controller without them would
answer each probe with HTTP 500 at every discovery (P31).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from .poll_group import PollGroup
from .rrc_contract_base import RrcContract
from .rrc_energy import statistic_names

APPLIANCE: Final = "/system/appliance"
DISPLAY_CODE: Final = f"{APPLIANCE}/displayCode"
CAUSE_CODE: Final = f"{APPLIANCE}/causeCode"
LOCKING_ERROR: Final = f"{APPLIANCE}/lockingError"
BLOCKING_ERROR: Final = f"{APPLIANCE}/blockingError"
MAINTENANCE_REQUEST: Final = f"{APPLIANCE}/maintenanceRequest"
FIRST_MAINTENANCE_REQUEST: Final = f"{APPLIANCE}/firstMaintenanceRequest"
SYSTEM_PRESSURE: Final = f"{APPLIANCE}/systemPressure"
INDOOR_HUMIDITY: Final = "/system/sensors/humidity/indoor_h1"
INDOOR_AIR: Final = "/system/sensors/temperatures/indoorAirDigital"
INDOOR_CHIP: Final = "/system/sensors/temperatures/indoorChip"
INDOOR_BOARD: Final = "/system/sensors/temperatures/indoorPCB"
UPDATE_STATE: Final = "/gateway/update/state"
REFILL_NEEDED: Final = "/heatSources/refillNeeded"

# The paths that get an entity of their own in ``rrc_system_entities``.
OWN_ENTITY_PATHS: Final = (
    DISPLAY_CODE,
    CAUSE_CODE,
    LOCKING_ERROR,
    BLOCKING_ERROR,
    MAINTENANCE_REQUEST,
    SYSTEM_PRESSURE,
    INDOOR_HUMIDITY,
    INDOOR_AIR,
    INDOOR_CHIP,
    INDOOR_BOARD,
    UPDATE_STATE,
    REFILL_NEEDED,
)

CONTRACT = RrcContract(
    name="system",
    poll_groups=MappingProxyType(
        {
            # The pressure like every other measurement (at least 60 s), the
            # status, maintenance and sensor leaves with the slow group.
            r"/system/appliance/systemPressure": PollGroup.FAST,
            r"/system/appliance/(?:displayCode|causeCode|lockingError"
            r"|blockingError|maintenanceRequest|firstMaintenanceRequest)": (
                PollGroup.SLOW
            ),
            r"/system/sensors/humidity/indoor_h1": PollGroup.SLOW,
            r"/system/sensors/temperatures/(?:indoorAirDigital|indoorChip"
            r"|indoorPCB|offset)": PollGroup.SLOW,
            r"/gateway/update/state": PollGroup.SLOW,
        }
    ),
    read_only_mirrors=OWN_ENTITY_PATHS,
    texts=statistic_names(),
)
