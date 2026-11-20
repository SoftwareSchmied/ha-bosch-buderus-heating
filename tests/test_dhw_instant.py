"""An instantaneous water heater reports 0 as hot water temperature: unknown."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.pointt_water_heater import (
    PointTWaterHeater,
)
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from tests.ha_test_helpers import MockConfigEntry

ROOT = "/dhwCircuits/dhw1"
ACTUAL = ROOT + "/actualTemp"
SYSTEM = ROOT + "/hotWaterSystem"
MODE = ROOT + "/operationMode"


def _number(path: str, value: float) -> Resource:
    return Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="floatValue", unit="C", minimum=0, maximum=100
        ),
    )


def _text(path: str, value: str) -> Resource:
    return Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(resource_type="stringValue"),
    )


def _plant(hass: HomeAssistant, system: str | None, actual: float):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one", device_type="RRC"), entry
    )
    items = [_number(ACTUAL, actual), _text(MODE, "high")]
    if system is not None:
        items.append(_text(SYSTEM, system))
    coordinator.resources = {item.path: item for item in items}
    coordinator.update_interval = None
    coordinator.data = {
        item.path: ResourceSnapshot(item, True, datetime.now(UTC)) for item in items
    }
    coordinator.last_update_success = True
    description = next(
        item
        for item in build_sensor_descriptions(coordinator.resources)
        if item.resource_path == ACTUAL
    )
    sensor = BoschBuderusSensor(coordinator, description)
    heater = PointTWaterHeater(coordinator, ROOT)
    heater.hass = hass
    return sensor, heater


@pytest.mark.parametrize("spelling", ["instant", "Instant", " instant "])
def test_a_zero_on_an_instantaneous_heater_is_unknown(
    hass: HomeAssistant, spelling: str
) -> None:
    sensor, heater = _plant(hass, spelling, 0.0)
    assert sensor.native_value is None
    assert heater.native_current_temperature is None


def test_a_real_reading_on_an_instantaneous_heater_stays(hass: HomeAssistant) -> None:
    sensor, heater = _plant(hass, "instant", 41.5)
    assert sensor.native_value == 41.5
    assert heater.native_current_temperature == 41.5


@pytest.mark.parametrize("system", ["storage", None])
def test_a_zero_is_unknown_on_every_system(
    hass: HomeAssistant, system: str | None
) -> None:
    """Issue 83: a tank behind a CT200 reports 0.0 as well, flagged available;
    a tank is never at 0 degrees, so no system keeps the 0."""
    sensor, heater = _plant(hass, system, 0.0)
    assert sensor.native_value is None
    assert heater.native_current_temperature is None


@pytest.mark.parametrize("system", ["storage", None])
def test_a_real_reading_on_other_systems_stays(
    hass: HomeAssistant, system: str | None
) -> None:
    sensor, heater = _plant(hass, system, 0.5)
    assert sensor.native_value == 0.5
    assert heater.native_current_temperature == 0.5


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize(("system", "expected"), [("instant", None), ("storage", None)])
async def test_the_entities_of_a_room_controller_plant(
    hass: HomeAssistant, system: str, expected: float | None
) -> None:
    """Setup with the controller tree: the water heater shows no 0 either."""
    from dataclasses import replace
    from unittest.mock import patch

    from tests.helpers.rrc_cloud import RrcCloud, load_tree
    from tests.test_rrc_extras import PACKAGE, _setup, _teardown

    tree = {**load_tree("rrc/ct200-shapes.json"), **load_tree("rrc/dhw.json")}
    tree[SYSTEM] = replace(tree[SYSTEM], value=system)
    tree[ACTUAL] = replace(tree[ACTUAL], value=0.0)
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        entry, _clock, stack = await _setup(hass, RrcCloud(tree))
    try:
        registry = er.async_get(hass)
        items = er.async_entries_for_config_entry(registry, entry.entry_id)
        heater = next(
            item for item in items if item.unique_id.endswith("dhw1:water_heater")
        )
        attributes = hass.states.get(heater.entity_id).attributes
        assert attributes.get("current_temperature") == expected
    finally:
        await _teardown(hass, entry, stack)
