"""Generic controls whose icons come from ``icons.json``.

Home Assistant shows an entity's icon from its translation key (with an icon
per state) only while the entity sets no icon of its own. The selects named
here leave theirs out; every other control keeps its icon in code.
"""

from __future__ import annotations

from typing import Final

TRANSLATED_SELECT_ICONS: Final = frozenset(
    {
        "auxiliary_heater_operation_mode",
        "cooling_operation_mode",
        "heating_circuit_operation_mode",
        "hot_water_operation_mode",
        "season_switching",
        "silent_mode",
        "summer_winter_switching",
        "ventilation_operation_mode",
    }
)
