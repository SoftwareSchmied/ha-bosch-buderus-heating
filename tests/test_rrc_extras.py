"""Further room controller settings through the real setup: automatic
absence, notification light, presence sensor setting, total solar yield,
hot water system and hydraulic balancing.

The fake cloud answers with ``ct200-shapes.json`` and the leaves of
``extras.json`` laid over it (synthetic; leaf names, types and units of the
TC100.2 tree of issue 122). Everything here is expected, untested on a
real plant beyond that tree.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import rrc_contracts
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
from custom_components.bosch_buderus_heating.rrc_contract_extras import (
    AUTO_AWAY_POLICY,
    NOTIFICATION_LIGHT_POLICY,
)
from custom_components.bosch_buderus_heating.rrc_extras_entities import (
    build_flags,
    build_sensors,
)
from custom_components.bosch_buderus_heating.switch import build_switch_descriptions
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    assess_control,
)
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches, load_tree
from tests.test_entry import _entry

PACKAGE = "custom_components.bosch_buderus_heating"
GW = "gateway-one"
AUTO_AWAY = "/system/autoAway/enabled"
LIGHT = "/gateway/notificationLight/enabled"
PIR = "/gateway/pirSensitivity"
COMING_HOME = "/system/autoAway/comingHome/active"
PREDICTION = "/system/autoAway/comingHome/prediction"
HYDRAULIC = "/system/hydraulicBalancing/enabled"
SOLAR = "/solarCircuits/sc1/totalSolarGain"
HOT_WATER = "/dhwCircuits/dhw1/hotWaterSystem"
USERS = "/system/autoAway/users/list"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

SWITCH_AWAY = ("switch", f"{GW}:system:autoAway:enabled:control")
SWITCH_LIGHT = ("switch", f"{GW}:gateway:notificationLight:enabled:control")
BINARY_HOME = ("binary_sensor", f"{GW}:system:autoAway:comingHome:active:flag")
BINARY_HYDRAULIC = (
    "binary_sensor",
    f"{GW}:system:hydraulicBalancing:enabled:flag",
)
SENSOR_PREDICTION = (
    "sensor",
    f"{GW}:system:autoAway:comingHome:prediction:value",
)
SENSOR_PIR = ("sensor", f"{GW}:gateway:pirSensitivity:value")
SENSOR_SOLAR = ("sensor", f"{GW}:solarCircuits:sc1:totalSolarGain:value")
SENSOR_HOT_WATER = ("sensor", f"{GW}:dhwCircuits:dhw1:hotWaterSystem:value")
ALL = (
    SWITCH_AWAY,
    SWITCH_LIGHT,
    BINARY_HOME,
    BINARY_HYDRAULIC,
    SENSOR_PREDICTION,
    SENSOR_PIR,
    SENSOR_SOLAR,
    SENSOR_HOT_WATER,
)


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def extras_tree() -> dict[str, Resource]:
    return {**load_tree("rrc/ct200-shapes.json"), **load_tree("rrc/extras.json")}


def without_extras() -> dict[str, Resource]:
    """The room controller tree as it is without any of the new leaves."""
    tree = load_tree("rrc/ct200-shapes.json")
    autoaway = tree["/system/autoAway"]
    tree["/system/autoAway"] = replace(
        autoaway,
        references=tuple(r for r in autoaway.references if r.path != AUTO_AWAY),
    )
    del tree[AUTO_AWAY]
    return tree


def _entries(hass: HomeAssistant, entry) -> dict[tuple[str, str], er.RegistryEntry]:
    return {
        (item.domain, item.unique_id): item
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


def _state(hass: HomeAssistant, entry, key: tuple[str, str]):
    return hass.states.get(_entries(hass, entry)[key].entity_id)


async def _setup(hass: HomeAssistant, cloud: RrcCloud, gateway: Gateway | None = None):
    entry = _entry(hass)
    clock = Clock()
    stack = cloud_patches(cloud, clock, gateway or Gateway(GW))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, clock, stack


def _no_wait(entry) -> list[float]:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    entry.runtime_data.coordinators[0]._write_service._sleep = sleep
    return sleeps


async def _teardown(hass: HomeAssistant, entry, stack) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stack.close()


async def _enable(hass: HomeAssistant, entry, *keys: tuple[str, str]) -> None:
    registry = er.async_get(hass)
    entries = _entries(hass, entry)
    for key in keys:
        registry.async_update_entity(entries[key].entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    _no_wait(entry)


async def test_the_leaves_become_entities_with_the_agreed_defaults(
    hass: HomeAssistant,
) -> None:
    cloud = RrcCloud(extras_tree())
    entry, _clock, stack = await _setup(hass, cloud)
    try:
        found = _entries(hass, entry)
        # (enabled by default, category)
        expected = {
            SWITCH_AWAY: (False, "config"),
            SWITCH_LIGHT: (False, "config"),
            BINARY_HOME: (True, None),
            BINARY_HYDRAULIC: (False, "diagnostic"),
            SENSOR_PREDICTION: (False, "diagnostic"),
            SENSOR_PIR: (False, "diagnostic"),
            SENSOR_SOLAR: (True, None),
            SENSOR_HOT_WATER: (False, "diagnostic"),
        }
        for key, (enabled, category) in expected.items():
            item = found[key]
            assert (item.disabled_by is None) is enabled, key
            assert (item.entity_category and item.entity_category.value) == category, (
                key
            )
            assert item.translation_key and item.translation_key.startswith("rrc_")

        solar = _state(hass, entry, SENSOR_SOLAR)
        assert float(solar.state) == 1234.5
        assert solar.attributes["unit_of_measurement"] == "kWh"
        assert solar.attributes["device_class"] == "energy"
        assert solar.attributes["state_class"] == "total_increasing"
        assert _state(hass, entry, BINARY_HOME).state == STATE_OFF
        # Nothing is written, and the user list is never requested.
        assert cloud.writes == []
        assert USERS not in cloud.all_requested()
        # The structure of the notification light is no control.
        assert not [k for k in found if k[1].endswith("notificationLight:activate")]
    finally:
        await _teardown(hass, entry, stack)


async def test_the_disabled_entities_show_their_values_once_enabled(
    hass: HomeAssistant,
) -> None:
    entry, _clock, stack = await _setup(hass, RrcCloud(extras_tree()))
    try:
        await _enable(
            hass,
            entry,
            BINARY_HYDRAULIC,
            SENSOR_PREDICTION,
            SENSOR_PIR,
            SENSOR_HOT_WATER,
            SWITCH_AWAY,
            SWITCH_LIGHT,
        )
        assert _state(hass, entry, BINARY_HYDRAULIC).state == STATE_ON
        prediction = _state(hass, entry, SENSOR_PREDICTION)
        assert float(prediction.state) == 25
        assert prediction.attributes["unit_of_measurement"] == "min"
        assert prediction.attributes["device_class"] == "duration"
        assert _state(hass, entry, SENSOR_PIR).state == "medium"
        assert _state(hass, entry, SENSOR_HOT_WATER).state == "storage"
        assert _state(hass, entry, SWITCH_AWAY).state == STATE_OFF
        assert _state(hass, entry, SWITCH_LIGHT).state == STATE_ON
    finally:
        await _teardown(hass, entry, stack)


async def test_the_switches_write_the_exact_strings_once(hass: HomeAssistant) -> None:
    """S2, S6, S7: one PUT each with "true"/"false", read back."""
    cloud = RrcCloud(extras_tree())
    entry, _clock, stack = await _setup(hass, cloud)
    try:
        await _enable(hass, entry, SWITCH_AWAY, SWITCH_LIGHT)
        away = _entries(hass, entry)[SWITCH_AWAY].entity_id
        light = _entries(hass, entry)[SWITCH_LIGHT].entity_id
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": away}, blocking=True
        )
        assert cloud.written() == [("PUT", AUTO_AWAY, "true")]
        assert hass.states.get(away).state == STATE_ON
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": light}, blocking=True
        )
        assert cloud.written()[-1] == ("PUT", LIGHT, "false")
        assert hass.states.get(light).state == STATE_OFF
        assert len(cloud.written()) == 2
        stats = entry.runtime_data.coordinators[0].rrc_write_stats.snapshot("extras")
        assert stats["writes_sent"] == 2
        assert stats["writes_confirmed"] == 2
    finally:
        await _teardown(hass, entry, stack)


async def test_a_write_the_controller_does_not_take_is_not_retried(
    hass: HomeAssistant,
) -> None:
    cloud = RrcCloud(extras_tree())
    cloud.ignore_writes.add(AUTO_AWAY)
    entry, clock, stack = await _setup(hass, cloud)
    try:
        await _enable(hass, entry, SWITCH_AWAY)
        away = _entries(hass, entry)[SWITCH_AWAY].entity_id
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": away}, blocking=True
            )
        assert cloud.written() == [("PUT", AUTO_AWAY, "true")]
        # Unconfirmed: never shown as on; the next regular cycle shows the
        # controller's value again, and nothing is sent a second time.
        assert hass.states.get(away).state != STATE_ON
        coordinator = entry.runtime_data.coordinators[0]
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(away).state == STATE_OFF
        assert cloud.written() == [("PUT", AUTO_AWAY, "true")]
    finally:
        await _teardown(hass, entry, stack)


async def test_values_follow_the_regular_cycle(hass: HomeAssistant) -> None:
    cloud = RrcCloud(extras_tree())
    entry, clock, stack = await _setup(hass, cloud)
    try:
        coordinator = entry.runtime_data.coordinators[0]
        cloud.tree[COMING_HOME] = replace(cloud.tree[COMING_HOME], value="true")
        cloud.tree[SOLAR] = replace(cloud.tree[SOLAR], value=1300.0)
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert _state(hass, entry, BINARY_HOME).state == STATE_ON
        assert float(_state(hass, entry, SENSOR_SOLAR).state) == 1300.0
        # An unexpected flag is unknown, a failed reading unavailable.
        cloud.tree[COMING_HOME] = replace(cloud.tree[COMING_HOME], value="maybe")
        cloud.down[SOLAR] = 403
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert _state(hass, entry, BINARY_HOME).state == "unknown"
        assert _state(hass, entry, SENSOR_SOLAR).state == STATE_UNAVAILABLE
    finally:
        await _teardown(hass, entry, stack)


async def test_without_the_leaves_nothing_is_created(hass: HomeAssistant) -> None:
    entry, _clock, stack = await _setup(hass, RrcCloud(without_extras()))
    try:
        found = _entries(hass, entry)
        for key in ALL:
            assert key not in found, key
        assert not [
            k for k in found if "autoAway" in k[1] or "notificationLight" in k[1]
        ]
    finally:
        await _teardown(hass, entry, stack)


async def test_other_gateways_get_nothing(hass: HomeAssistant) -> None:
    """S11: the entities belong to a recognised room controller only."""
    extras = load_tree("rrc/extras.json")
    tree = {
        **load_tree("captures/k40-cloud-2026-11-05.json"),
        SOLAR: extras[SOLAR],
        PIR: extras[PIR],
        "/solarCircuits/sc1": extras["/solarCircuits/sc1"],
        "/solarCircuits": extras["/solarCircuits"],
    }
    gateway = tree["/gateway"]
    tree["/gateway"] = replace(
        gateway, references=(*gateway.references, ResourceReference(PIR))
    )
    cloud = RrcCloud(tree, forbidden=set(), missing_as_500=False)
    entry, _clock, stack = await _setup(hass, cloud, Gateway(GW, device_type="k40"))
    try:
        coordinator = entry.runtime_data.coordinators[0]
        # Read there too, but no entity of this package.
        assert PIR in coordinator.resources
        found = _entries(hass, entry)
        for key in ALL:
            assert key not in found, key
    finally:
        await _teardown(hass, entry, stack)


# ------------------------------------------------------------ pure parts
def _wire(path: str, **fields: object) -> Resource:
    return parse_resource({"id": path, **fields})


def _switches(path: str, context, **fields: object) -> tuple:
    base = {"type": "stringValue", "writeable": 1, "value": "false"}
    resource = _wire(path, **{**base, **fields})
    return build_switch_descriptions({path: resource}, context)


def test_switch_policies_need_exactly_the_two_strings() -> None:
    for path in (AUTO_AWAY, LIGHT):
        (description,) = _switches(path, RRC_CONTEXT)
        assert (description.on_value, description.off_value) == ("true", "false")
        assert not description.entity_registry_enabled_default
        # Another wire format: no control (never guessed).
        assert not _switches(path, RRC_CONTEXT, value="off")
        assert not _switches(path, RRC_CONTEXT, value="on")
        # Not writable, or advertising other options: no control.
        assert not _switches(path, RRC_CONTEXT, writeable=0)
        assert not _switches(path, RRC_CONTEXT, allowedValues=["false", "true", "x"])
        # Only on a room controller (S11).
        assert not _switches(path, GENERIC_CONTEXT)
    for policy in (AUTO_AWAY_POLICY, NOTIFICATION_LIGHT_POLICY):
        assert not policy.enabled_by_default
        assert rrc_contracts.owns(policy)


def test_the_presence_setting_and_the_balancing_are_never_controls() -> None:
    for path, value in ((PIR, "medium"), (HYDRAULIC, "true")):
        resource = _wire(path, type="stringValue", writeable=1, value=value)
        assessment = assess_control(resource, RRC_CONTEXT)
        assert assessment.policy is None
        assert not assessment.supports_switch


def _changed(tree: dict[str, Resource], path: str, **changes: object) -> dict:
    resource = tree[path]
    meta = {
        name: changes.pop(name) for name in ("unit", "available") if name in changes
    }
    if meta:
        resource = replace(resource, metadata=replace(resource.metadata, **meta))
    return {path: replace(resource, **changes)}


def test_sensors_need_their_expected_shape() -> None:
    tree = extras_tree()
    assert len(build_sensors(tree, RRC_CONTEXT)) == 4
    assert len(build_flags(tree, RRC_CONTEXT)) == 2

    def sensors(path: str, **changes: object) -> tuple:
        return build_sensors(_changed(tree, path, **changes), RRC_CONTEXT)

    def flags(path: str, **changes: object) -> tuple:
        return build_flags(_changed(tree, path, **changes), RRC_CONTEXT)

    assert sensors(SOLAR, value=1.0)
    # A unit that is not kWh is never taken as kWh (P10).
    assert not sensors(SOLAR, unit="MWh")
    assert not sensors(SOLAR, unit=None)
    assert not sensors(SOLAR, value="1,234")
    assert not sensors(SOLAR, value=True)
    assert not sensors(PREDICTION, unit="h")
    # P41: a leaf the controller reports unavailable gets nothing.
    assert not sensors(SOLAR, available=False)
    assert not sensors(HOT_WATER, available=False)
    assert not sensors(HOT_WATER, value=5)
    # Flags read "true"/"false" only.
    assert flags(HYDRAULIC, value="false")
    assert not flags(HYDRAULIC, value="maybe")
    assert not flags(COMING_HOME, value=3)
    assert not flags(HYDRAULIC, available=False)


def test_several_circuits_each_get_their_own_entity() -> None:
    tree = extras_tree()
    tree["/solarCircuits/sc2/totalSolarGain"] = replace(
        tree[SOLAR], path="/solarCircuits/sc2/totalSolarGain"
    )
    tree["/dhwCircuits/dhw2/hotWaterSystem"] = replace(
        tree[HOT_WATER], path="/dhwCircuits/dhw2/hotWaterSystem"
    )
    sensors = build_sensors(tree, RRC_CONTEXT)
    assert len({d.key for d in sensors}) == len(sensors) == 6
    # Paths outside the pattern are ignored.
    tree["/solarCircuits/scX/totalSolarGain"] = tree[SOLAR]
    tree["/solarCircuits/sc1/other/totalSolarGain"] = tree[SOLAR]
    assert len(build_sensors(tree, RRC_CONTEXT)) == 6


def test_poll_groups_and_paths_have_no_conflict() -> None:
    tree = extras_tree()
    for path in (AUTO_AWAY, LIGHT):
        assert resource_poll_group(tree[path], rrc_plant=True) is PollGroup.CONTROL
    assert resource_poll_group(tree[PIR], rrc_plant=True) is PollGroup.SLOW
    rrc_contracts.registry().check_paths(tree)
