"""E17: the room controller functions confirmed on a real controller.

The diagnostics report every RRC contract as ``unconfirmed`` unless one of
its functions is listed here. A listed function was confirmed with this
integration on a TC100.2 (firmware 05.04.00) with 0.9.0-alpha.4,
0.9.0-alpha.5 or 0.9.0-alpha.6 (``docs/room-controllers.md``, "What is
confirmed"). Only
writes are listed, no readings, and a confirmation on the TC100.2 says
nothing about a CT200. Pure: tokens only, no Home Assistant import.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

UNCONFIRMED: Final = "unconfirmed"
PARTLY_CONFIRMED: Final = "partly_confirmed"
# The controller the functions were confirmed on, as a token.
CONFIRMED_ON: Final = "tc100_2"

# Contract name (``rrc_contract_*`` and ``core``) -> its confirmed functions.
CONFIRMED: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "core": frozenset(
            {
                # alpha.4
                "zone_mode",
                "manual_setpoint",
                # alpha.5
                "program_selection",
                "mode_with_target",
            }
        ),
        "zone": frozenset(
            {
                "temporary_setpoint",
                "zone_window_detection",
                # alpha.6
                "advance_switch_point",
            }
        ),
        "absence": frozenset({"away_mode", "absence_calendar_day", "boost_on_off"}),
        "dhw": frozenset({"operation_mode"}),
    }
)


def report(contract: str) -> dict[str, object]:
    """The confirmation fields of one contract's diagnostics."""
    functions = CONFIRMED.get(contract)
    if not functions:
        return {"confirmation": UNCONFIRMED}
    return {
        "confirmation": PARTLY_CONFIRMED,
        "confirmed_functions": sorted(functions),
        "confirmed_on": CONFIRMED_ON,
    }


def overall() -> str:
    """The confirmation of the room controller functions as a whole."""
    return PARTLY_CONFIRMED if any(CONFIRMED.values()) else UNCONFIRMED
