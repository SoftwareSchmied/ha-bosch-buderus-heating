"""The child lock of a zone as a switch, and its state texts (0.9.3).

The switch exists only for a writable path that advertises exactly the two
values ``active`` and ``inactive`` and reads one of them. Every shape is
constructed (nothing here confirms behaviour on a real installation).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating import zone_child_lock
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.sensor import build_sensor_descriptions
from custom_components.bosch_buderus_heating.switch import (
    BoschBuderusSwitch,
    build_switch_descriptions,
)
from custom_components.bosch_buderus_heating.writes import assess_control
from tests.circuit_control_helpers import Cloud, coordinator_for

LOCK = "/zones/zone1/childLock"
OTHER = "/zones/zone2/childLock"
COMPONENT = Path(zone_child_lock.__file__).parent


def _lock(path: str = LOCK, value: str = "inactive", **fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": path,
        "type": "stringValue",
        "value": value,
        "writeable": 1,
        "allowedValues": ["active", "inactive"],
        **fields,
    }
    return item


def _resources(*items: dict[str, Any]) -> dict[str, Any]:
    return {item["id"]: parse_resource(item) for item in items}


def _switches(resources: dict[str, Any]) -> dict[str, Any]:
    return {item.resource_path: item for item in build_switch_descriptions(resources)}


def _entity(hass, coordinator, path=LOCK) -> BoschBuderusSwitch:
    entity = BoschBuderusSwitch(coordinator, _switches(coordinator.resources)[path])
    entity.hass = hass
    return entity


def test_the_switch_exists_for_a_writable_two_value_path_and_starts_disabled():
    description = _switches(_resources(_lock(), _lock(OTHER, "active")))[LOCK]
    assert description.write_policy is zone_child_lock.CHILD_LOCK_POLICY
    assert (description.on_value, description.off_value) == ("active", "inactive")
    assert description.translation_key == "zone_child_lock"
    assert description.entity_category is EntityCategory.CONFIG
    # A new write control stays disabled until the user enables it.
    assert description.entity_registry_enabled_default is False


def test_without_the_path_nothing_is_created():
    unrelated = _resources(
        _lock("/zones/zone1/name", "Living room", allowedValues=None),
        _lock("/heatingCircuits/hc1/childLock"),
        _lock("/devices/device1/childLock"),
    )
    assert _switches(unrelated) == {}
    assert all(
        item.write_policy is not zone_child_lock.CHILD_LOCK_POLICY
        for item in build_switch_descriptions(unrelated)
    )


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        (_lock(writeable=0), "not_writable"),
        (_lock(allowedValues=None), "missing_options"),
        (_lock(allowedValues=[]), "missing_options"),
        (_lock(allowedValues=["active"]), "unsupported_options"),
        (
            _lock(allowedValues=["active", "inactive", "unknown"]),
            "unsupported_options",
        ),
        (_lock(allowedValues=["on", "off"]), "unsupported_options"),
        (_lock(value="broken"), "current_option_not_advertised"),
    ],
)
def test_the_gate_refuses_every_other_shape(item, reason):
    resources = _resources(item)
    assert assess_control(resources[LOCK]).rejection_reason == reason
    assert LOCK not in _switches(resources)


def test_a_wrong_resource_type_gives_no_switch():
    item = _lock()
    item["type"] = "floatValue"
    resources = _resources(item)
    assert LOCK not in _switches(resources)


async def test_turning_it_on_writes_once_and_confirms(hass):
    coordinator = coordinator_for(hass, _resources(_lock()))
    cloud = Cloud(coordinator)
    entity = _entity(hass, coordinator)
    assert entity.is_on is False
    await entity.async_turn_on()
    assert cloud.puts == [(LOCK, "active")]
    # The setting is read live in one request before the one PUT.
    assert cloud.bulk_calls[0] == (LOCK,)
    assert entity.is_on is True


async def test_turning_it_off_writes_the_other_value(hass):
    coordinator = coordinator_for(hass, _resources(_lock(value="active")))
    cloud = Cloud(coordinator)
    entity = _entity(hass, coordinator)
    assert entity.is_on is True
    await entity.async_turn_off()
    assert cloud.puts == [(LOCK, "inactive")]
    assert entity.is_on is False


async def test_a_list_that_changed_live_sends_nothing(hass):
    from dataclasses import replace

    coordinator = coordinator_for(hass, _resources(_lock()))
    cloud = Cloud(coordinator)
    entity = _entity(hass, coordinator)
    metadata = coordinator.data[LOCK].resource.metadata
    cloud.change(LOCK, metadata=replace(metadata, allowed_values=("active",)))
    with pytest.raises((ServiceValidationError, HomeAssistantError)):
        await entity.async_turn_on()
    assert cloud.puts == []


async def test_a_value_the_gateway_does_not_show_stays_one_put(hass):
    coordinator = coordinator_for(hass, _resources(_lock()))
    cloud = Cloud(coordinator)
    cloud.stuck[LOCK] = "inactive"
    entity = _entity(hass, coordinator)
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()
    assert cloud.puts == [(LOCK, "active")]


async def test_a_read_only_path_refuses_the_write(hass):
    from dataclasses import replace

    coordinator = coordinator_for(hass, _resources(_lock()))
    cloud = Cloud(coordinator)
    entity = _entity(hass, coordinator)
    metadata = coordinator.data[LOCK].resource.metadata
    cloud.change(LOCK, metadata=replace(metadata, writable=False))
    with pytest.raises((ServiceValidationError, HomeAssistantError)):
        await entity.async_turn_on()
    assert cloud.puts == []


# The states of the reading -----------------------------------------------------


def test_the_reading_has_translated_states_in_every_language():
    descriptions = {
        item.resource_path: item
        for item in build_sensor_descriptions(_resources(_lock()))
    }
    assert descriptions[LOCK].translation_key == "on_off_state"
    assert {"active", "inactive"} <= set(descriptions[LOCK].options or ())
    files = [
        COMPONENT / "strings.json",
        *sorted((COMPONENT / "translations").glob("*.json")),
    ]
    assert len(files) == 20
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        states = data["entity"]["sensor"]["on_off_state"]["state"]
        assert states["active"] and states["inactive"], path.name
        assert states["active"] != "active" or path.name in {"en.json", "strings.json"}
        assert data["entity"]["switch"]["zone_child_lock"]["name"], path.name
