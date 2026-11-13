"""W5 (CT-F02 to CT-F04): system status, maintenance and sensors of an RRC
2.0 room controller through the real setup.

The fake cloud answers with ``ct200-shapes.json`` and the system leaves of
``system.json`` laid over it (synthetic, demo shapes and the leaf list of
the TC100.2 tree of issue 122). Everything is read only and expected,
untested on a CT200.
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import rrc_contracts
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceReference,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.poll_group import PollGroup
from custom_components.bosch_buderus_heating.resource_catalog import (
    poll_group as resource_poll_group,
)
from custom_components.bosch_buderus_heating.rrc_system_entities import (
    PRESSURE,
    SENSORS,
    build_problems,
    build_sensors,
    flag,
    sensor_value,
)
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
)
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches, load_tree
from tests.test_entry import _entry

PACKAGE = "custom_components.bosch_buderus_heating"
GW = "gateway-one"
APPLIANCE = "/system/appliance"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def system_tree() -> dict[str, Resource]:
    return {**load_tree("rrc/ct200-shapes.json"), **load_tree("rrc/system.json")}


def entries(hass: HomeAssistant, entry) -> dict[str, er.RegistryEntry]:
    registry = er.async_get(hass)
    return {
        item.unique_id: item
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }


async def setup(hass: HomeAssistant, cloud: RrcCloud, gateway: Gateway | None = None):
    entry = _entry(hass)
    clock = Clock()
    stack = cloud_patches(cloud, clock, gateway or Gateway(GW))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, clock, stack


async def teardown(hass: HomeAssistant, entry, stack) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stack.close()


def state_of(hass: HomeAssistant, entry, unique_id: str):
    item = entries(hass, entry)[unique_id]
    return hass.states.get(item.entity_id)


async def test_the_system_leaves_become_read_only_entities(hass: HomeAssistant) -> None:
    cloud = RrcCloud(system_tree())
    entry, _clock, stack = await setup(hass, cloud)
    try:
        found = entries(hass, entry)
        expected = {
            f"{GW}:system:appliance:displayCode:value": ("sensor", True),
            f"{GW}:system:appliance:causeCode:value": ("sensor", True),
            f"{GW}:system:appliance:systemPressure:value": ("sensor", True),
            f"{GW}:system:sensors:humidity:indoor_h1:value": ("sensor", True),
            f"{GW}:system:sensors:temperatures:indoorAirDigital:value": (
                "sensor",
                False,
            ),
            f"{GW}:system:sensors:temperatures:indoorChip:value": ("sensor", False),
            f"{GW}:system:sensors:temperatures:indoorPCB:value": ("sensor", False),
            f"{GW}:gateway:update:state:value": ("sensor", False),
            f"{GW}:system:appliance:lockingError:problem": ("binary_sensor", True),
            f"{GW}:system:appliance:blockingError:problem": ("binary_sensor", True),
            f"{GW}:system:appliance:maintenanceRequest:problem": (
                "binary_sensor",
                True,
            ),
            f"{GW}:heatSources:refillNeeded:problem": ("binary_sensor", True),
        }
        for unique_id, (domain, enabled) in expected.items():
            item = found[unique_id]
            assert item.domain == domain, unique_id
            assert (item.disabled_by is None) is enabled, unique_id
            assert item.translation_key and item.translation_key.startswith("rrc_")
        # "false" reads as a flag, so no raw maintenance sensor; no status
        # without the plant's own pressure limits.
        assert f"{GW}:system:appliance:maintenanceRequest:value" not in found
        assert f"{GW}:system:appliance:systemPressure:status" not in found

        pressure = state_of(hass, entry, f"{GW}:system:appliance:systemPressure:value")
        assert float(pressure.state) == 1.6
        assert pressure.attributes["unit_of_measurement"] == "bar"
        assert pressure.attributes["device_class"] == "pressure"
        humidity = state_of(
            hass, entry, f"{GW}:system:sensors:humidity:indoor_h1:value"
        )
        assert float(humidity.state) == 48.0
        assert humidity.attributes["device_class"] == "humidity"
        assert state_of(
            hass, entry, f"{GW}:system:appliance:displayCode:value"
        ).state == ("0H")
        assert state_of(
            hass, entry, f"{GW}:system:appliance:causeCode:value"
        ).state == ("203")
        assert (
            state_of(hass, entry, f"{GW}:system:appliance:lockingError:problem").state
            == STATE_OFF
        )
        assert (
            state_of(hass, entry, f"{GW}:system:appliance:blockingError:problem").state
            == STATE_ON
        )

        # The generic sensors of these paths stay disabled diagnostics, so
        # an existing entity keeps its ID and nothing shows twice.
        generic = found[f"{GW}:system:appliance:systemPressure"]
        assert generic.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        # Nothing is ever written.
        assert cloud.writes == []
    finally:
        await teardown(hass, entry, stack)


async def test_values_follow_the_regular_cycle(hass: HomeAssistant) -> None:
    cloud = RrcCloud(system_tree())
    entry, clock, stack = await setup(hass, cloud)
    try:
        coordinator = entry.runtime_data.coordinators[0]
        locking = f"{GW}:system:appliance:lockingError:problem"
        pressure = f"{GW}:system:appliance:systemPressure:value"
        cloud.tree[f"{APPLIANCE}/lockingError"] = replace(
            cloud.tree[f"{APPLIANCE}/lockingError"], value="true"
        )
        cloud.tree[f"{APPLIANCE}/systemPressure"] = replace(
            cloud.tree[f"{APPLIANCE}/systemPressure"], value=0.7
        )
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert state_of(hass, entry, locking).state == STATE_ON
        assert float(state_of(hass, entry, pressure).state) == 0.7
        # An unexpected value is unknown, a failed reading unavailable.
        cloud.tree[f"{APPLIANCE}/lockingError"] = replace(
            cloud.tree[f"{APPLIANCE}/lockingError"], value="maybe"
        )
        cloud.down[f"{APPLIANCE}/systemPressure"] = 403
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert state_of(hass, entry, locking).state == "unknown"
        assert state_of(hass, entry, pressure).state == STATE_UNAVAILABLE
    finally:
        await teardown(hass, entry, stack)


async def test_unknown_units_and_unavailable_leaves_give_no_entity(
    hass: HomeAssistant,
) -> None:
    tree = system_tree()
    # The demo shape: a pressure without a unit is never taken as bar (P10).
    tree[f"{APPLIANCE}/systemPressure"] = load_tree("rrc/ct200-shapes.json")[
        f"{APPLIANCE}/systemPressure"
    ]
    humidity = "/system/sensors/humidity/indoor_h1"
    tree[humidity] = parse_resource(
        {
            "id": humidity,
            "type": "floatValue",
            "writeable": 0,
            "unitOfMeasure": "%",
            "available": "false",
            "value": 40.0,
        }
    )
    entry, _clock, stack = await setup(hass, RrcCloud(tree))
    try:
        found = entries(hass, entry)
        assert f"{GW}:system:appliance:systemPressure:value" not in found
        assert f"{GW}:system:sensors:humidity:indoor_h1:value" not in found
        assert f"{GW}:system:appliance:displayCode:value" in found
    finally:
        await teardown(hass, entry, stack)


async def test_the_same_paths_at_another_gateway_get_nothing(
    hass: HomeAssistant,
) -> None:
    """S11: the W5 entities belong to a recognised room controller only."""
    tree = {
        **load_tree("captures/k40-cloud-2026-11-05.json"),
        **{
            path: resource
            for path, resource in load_tree("rrc/system.json").items()
            if path.startswith(APPLIANCE)
        },
    }
    system = tree["/system"]
    tree["/system"] = replace(
        system, references=(*system.references, ResourceReference(APPLIANCE))
    )
    cloud = RrcCloud(tree, forbidden=set(), missing_as_500=False)
    entry, _clock, stack = await setup(hass, cloud, Gateway(GW, device_type="k40"))
    try:
        coordinator = entry.runtime_data.coordinators[0]
        # The leaves are read there too, but give no W5 entity.
        assert f"{APPLIANCE}/lockingError" in coordinator.resources
        found = entries(hass, entry)
        assert not [key for key in found if key.endswith((":value", ":problem"))]
    finally:
        await teardown(hass, entry, stack)


async def test_leaves_the_controller_does_not_list_are_not_probed(
    hass: HomeAssistant,
) -> None:
    """The leaves come from the container's references only: a controller
    without them would answer every probe with HTTP 500 (P31)."""
    tree = system_tree()
    tree[APPLIANCE] = load_tree("rrc/ct200-shapes.json")[APPLIANCE]
    cloud = RrcCloud(tree)
    entry, _clock, stack = await setup(hass, cloud)
    try:
        coordinator = entry.runtime_data.coordinators[0]
        assert coordinator.discovery_diagnostics.stop_reason == "complete"
        requested = cloud.all_requested()
        for leaf in ("displayCode", "lockingError", "blockingError", "causeCode"):
            assert f"{APPLIANCE}/{leaf}" not in requested
        assert f"{GW}:system:appliance:systemPressure:value" in entries(hass, entry)
        assert f"{GW}:system:appliance:displayCode:value" not in entries(hass, entry)
    finally:
        await teardown(hass, entry, stack)


async def test_diagnostics_report_no_values(hass: HomeAssistant) -> None:
    entry, _clock, stack = await setup(hass, RrcCloud(system_tree()))
    try:
        report = await async_get_config_entry_diagnostics(hass, entry)
        features = report["gateways"][0]["rrc_features"]
        assert features["contracts"]["system"]["confirmation"] == "unconfirmed"
        energy = features["energy_history"]
        assert energy["confirmation"] == "unconfirmed"
        text = json.dumps(energy)
        assert "gateway-one" not in text
        assert "MARKER" not in json.dumps(report)
    finally:
        await teardown(hass, entry, stack)


# ------------------------------------------------------------ pure parts
def test_poll_groups_of_the_system_leaves() -> None:
    tree = system_tree()
    assert resource_poll_group(tree[f"{APPLIANCE}/systemPressure"], rrc_plant=True) is (
        PollGroup.FAST
    )
    for path in (
        f"{APPLIANCE}/displayCode",
        f"{APPLIANCE}/lockingError",
        "/system/sensors/humidity/indoor_h1",
        "/gateway/update/state",
    ):
        assert resource_poll_group(tree[path], rrc_plant=True) is PollGroup.SLOW, path
    rrc_contracts.registry().check_paths(tree)


def test_flags_and_values_take_only_the_expected_shapes() -> None:
    assert flag("true") is True and flag(" FALSE ") is False and flag(True) is True
    assert flag("yes") is None and flag(1) is None and flag(None) is None
    pressure = parse_resource(
        {"id": f"{APPLIANCE}/systemPressure", "type": "floatValue", "value": 1.2}
    )
    assert sensor_value(PRESSURE, pressure) is None  # no unit
    bar = replace(pressure, metadata=replace(pressure.metadata, unit="BAR"))
    assert sensor_value(PRESSURE, bar) == 1.2
    assert sensor_value(PRESSURE, replace(bar, value=True)) is None
    code = next(item for item in SENSORS if item.key == "rrc_cause_code")
    cause = parse_resource(
        {"id": f"{APPLIANCE}/causeCode", "type": "floatValue", "value": 203.0}
    )
    assert sensor_value(code, cause) == 203
    assert sensor_value(code, replace(cause, value=20.5)) is None
    maintenance = next(
        item for item in SENSORS if item.key == "rrc_maintenance_request"
    )
    raw = parse_resource(
        {"id": f"{APPLIANCE}/maintenanceRequest", "type": "stringValue", "value": "M1"}
    )
    assert sensor_value(maintenance, raw) == "M1"
    assert sensor_value(maintenance, replace(raw, value="true")) is None


def test_builders_follow_the_shapes() -> None:
    tree = system_tree()
    sensors = {item.key for item in build_sensors(tree, RRC_CONTEXT)}
    assert "rrc_maintenance_request" not in sensors
    tree[f"{APPLIANCE}/maintenanceRequest"] = replace(
        tree[f"{APPLIANCE}/maintenanceRequest"], value="M2"
    )
    sensors = {item.key for item in build_sensors(tree, RRC_CONTEXT)}
    problems = {item.key for item in build_problems(tree, GENERIC_CONTEXT)}
    assert "rrc_maintenance_request" in sensors
    assert "rrc_maintenance_due" not in problems
    tree["/heatSources/refillNeeded"] = replace(
        tree["/heatSources/refillNeeded"], value=True
    )
    assert "rrc_refill_needed" in {
        item.key for item in build_problems(tree, RRC_CONTEXT)
    }


def test_the_pressure_status_needs_valid_limits() -> None:
    tree = system_tree()
    assert "pressure_status" not in {
        item.key for item in build_sensors(tree, RRC_CONTEXT)
    }
    tree["/heatSources/systemPressureRange"] = parse_resource(
        {
            "id": "/heatSources/systemPressureRange",
            "type": "systemPressureRange",
            "value": {
                "lowSystemPressure": 0.5,
                "shutOfPressureThreshold": 0.6,
                "lowPressureThreshold": 1.0,
                "highSystemPressure": 2.5,
                "highPressureThreshold": 2.8,
                "absoluteHighPressure": 3.0,
            },
        }
    )
    assert "pressure_status" in {item.key for item in build_sensors(tree, RRC_CONTEXT)}
