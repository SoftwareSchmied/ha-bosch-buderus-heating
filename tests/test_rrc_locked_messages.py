"""S8: the message of a locked room controller setting names what is locked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.bosch_buderus_heating.control import locked_translation_key

STRINGS = (
    Path(__file__).parents[1]
    / "custom_components"
    / "bosch_buderus_heating"
    / "strings.json"
)


@pytest.mark.parametrize(
    ("path", "key"),
    [
        ("/zones/zn1/userMode", "rrc_mode_locked"),
        ("/zones/zn12/manualTemperatureHeating", "rrc_zone_setting_locked"),
        ("/zones/zn1/clockOverride/temperatureHeating", "rrc_zone_setting_locked"),
        ("/heatingCircuits/hc1/boostShortcut", "rrc_boost_locked"),
        ("/heatingCircuits/hc1/heatCurveMax", "rrc_circuit_setting_locked"),
        ("/dhwCircuits/dhw1/extraDhw", "rrc_dhw_setting_locked"),
        ("/devices/device3/etrv/offset", "rrc_device_setting_locked"),
        ("/system/awayMode/enabled", "rrc_setting_locked"),
        ("/zones", "rrc_setting_locked"),
    ],
)
def test_each_locked_setting_has_its_own_message(path: str, key: str) -> None:
    assert locked_translation_key(path) == key
    exceptions = json.loads(STRINGS.read_text("utf-8"))["exceptions"]
    assert exceptions[key]["message"]
