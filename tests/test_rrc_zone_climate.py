"""W1: the zone climate of an RRC 2.0 room controller and its zone details.

Real setups against the fake cloud of ``tests/helpers/rrc_cloud.py`` with the
synthetic ``zone_climate.json`` (six zones: manual, schedule with and without
a temporary setpoint, a locked mode, no temporary setpoint, no room
temperature). Everything except the manual setpoint and the mode is
expected, untested on hardware.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.bosch_buderus_heating import rrc, rrc_contract_zone
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.poll_group import PollGroup
from custom_components.bosch_buderus_heating.rrc_contracts import registry
from tests.helpers.rrc_cloud import (
    Clock,
    RrcCloud,
    cloud_patches,
    load_tree,
    with_containers,
)
from tests.test_entry import _entry
from tests.test_rrc_tc100_setup import plant as tc100_plant

PACKAGE = "custom_components.bosch_buderus_heating"
GATEWAY = Gateway("gateway-one", device_type="rrc2")
FIXTURE = "rrc/zone_climate.json"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    """Reach the format 3 setup without migrating, as test_startup_inventory."""
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def path(zone: int, leaf: str) -> str:
    return f"/zones/zn{zone}/{leaf}"


MODE = "userMode"
MANUAL = "manualTemperatureHeating"
OVERRIDE = "clockOverride/temperatureHeating"


class ZoneCloud(RrcCloud):
    """The fake cloud with two more answers to a PUT: an error after the
    request went out, and a controller that ends the temporary setpoint."""

    def __init__(self, resources, **kwargs) -> None:
        super().__init__(resources, **kwargs)
        self.put_errors: dict[str, Exception] = {}
        self.end_on_write: set[str] = set()

    async def put_resource_value(self, gateway_id, path, value, **kwargs):
        if path in self.put_errors:
            self._write("PUT", path, value)
            raise self.put_errors[path]
        if path in self.end_on_write:
            self._write("PUT", path, value)
            self._apply(path, 0.0)
            return None
        return await super().put_resource_value(gateway_id, path, value, **kwargs)


def zone_tree() -> dict[str, Resource]:
    return with_containers(load_tree(FIXTURE))


def zone_cloud(tree: dict[str, Resource] | None = None) -> ZoneCloud:
    return ZoneCloud(zone_tree() if tree is None else tree, forbidden=set())


async def setup(hass: HomeAssistant, cloud: RrcCloud, clock: Clock | None = None):
    clock = clock or Clock()
    entry = _entry(hass)
    stack = cloud_patches(cloud, clock, GATEWAY)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinators[0]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    coordinator._write_service._sleep = sleep
    return entry, coordinator, sleeps, stack, clock


async def teardown(hass: HomeAssistant, entry, stack) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stack.close()


def entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    found = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert found is not None, unique_id
    return found


def climate(hass: HomeAssistant, zone: int) -> str:
    return entity_id(hass, "climate", f"gateway-one:zones:zn{zone}:climate")


def registered(hass: HomeAssistant, entry, domain: str) -> dict[str, er.RegistryEntry]:
    return {
        item.unique_id: item
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if item.domain == domain
    }


async def set_temperature(hass: HomeAssistant, target: str, **data: object) -> None:
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": target, **data}, blocking=True
    )


async def set_mode(hass: HomeAssistant, target: str, mode: str) -> None:
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": target, "hvac_mode": mode},
        blocking=True,
    )


async def refresh(hass: HomeAssistant, coordinator, clock: Clock, seconds: float):
    clock.now += seconds
    await coordinator.async_refresh()
    await hass.async_block_till_done()


# --------------------------------------------------------------- entities


async def test_every_zone_with_a_mode_and_a_room_temperature_gets_a_climate(
    hass: HomeAssistant,
) -> None:
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        climates = registered(hass, entry, "climate")
        # Zone 6 has no room temperature: only its select and number.
        assert sorted(climates) == [
            f"gateway-one:zones:zn{n}:climate" for n in range(1, 6)
        ]
        assert all(item.translation_key == "rrc_zone" for item in climates.values())
        assert "gateway-one:zones:zn6:userMode:control" in registered(
            hass, entry, "select"
        )

        manual = hass.states.get(climate(hass, 1))
        assert manual.state == "heat"
        assert manual.attributes["hvac_modes"] == ["heat", "auto"]
        assert manual.attributes["current_temperature"] == 20.5
        assert manual.attributes["temperature"] == 21.0
        assert manual.attributes["min_temp"] == 5.0
        assert manual.attributes["max_temp"] == 30.0
        assert manual.attributes["target_temp_step"] == 0.5
        assert manual.attributes["hvac_action"] == "heating"
        assert manual.attributes["zone_status"] == "heat_request"
        assert manual.attributes["temporary_override"] is False
        assert "override_until_next_switch_point" not in manual.attributes
        # P07: 999 is no next setpoint.
        assert "next_setpoint" not in manual.attributes
        assert manual.attributes["supported_features"] == (
            ClimateEntityFeature.TARGET_TEMPERATURE
        )
        assert "Zone A" in manual.attributes["friendly_name"]

        override = hass.states.get(climate(hass, 2))
        assert override.state == "auto"
        assert override.attributes["temperature"] == 23.5
        assert override.attributes["temporary_override"] is True
        assert override.attributes["override_until_next_switch_point"] is True
        assert override.attributes["next_setpoint"] == 18.0
        assert override.attributes["minutes_to_next_setpoint"] == 45
        assert override.attributes["hvac_action"] == "idle"
        # The temporary setpoint's lower limit is the manual minimum, not 0.
        assert override.attributes["min_temp"] == 5.0

        schedule = hass.states.get(climate(hass, 3))
        assert schedule.state == "auto"
        assert schedule.attributes["temperature"] == 19.0  # the program's
        assert schedule.attributes["temporary_override"] is False
        # An unknown zone status stays unknown, also as an action.
        assert schedule.attributes.get("hvac_action") is None
        assert "zone_status" not in schedule.attributes

        # No temporary setpoint (older firmware): the target is shown only.
        old = hass.states.get(climate(hass, 5))
        assert old.state == "auto"
        assert old.attributes["temperature"] == 20.0
        assert old.attributes["supported_features"] == 0
        assert old.attributes.get("hvac_action") is None  # low battery

        # The value-free contract diagnostics.
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        zone = diagnostics["gateways"][0]["rrc_features"]["contracts"]["zone"]
        assert zone["confirmation"] == "partly_confirmed"
        assert zone["zones"] == 6
        assert zone["override_paths"] == 5
        assert zone["overrides_active"] == 1
        assert zone["next_setpoint_none"] == 1
        assert zone["status_unknown"] == 1
        assert zone["window_status_unknown"] == 1
        assert "Zone" not in str(zone)
    finally:
        await teardown(hass, entry, stack)


async def test_zone_details_switches_and_numbers(hass: HomeAssistant) -> None:
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        sensors = registered(hass, entry, "sensor")
        state = hass.states.get

        nxt = sensors["gateway-one:zones:zn2:nextSetpoint"]
        assert nxt.translation_key == "rrc_next_setpoint"
        assert nxt.disabled_by is None
        assert state(nxt.entity_id).state == "18.0"
        # P07: 999 with used/available false is unknown, not 999 °C.
        assert state(sensors["gateway-one:zones:zn1:nextSetpoint"].entity_id).state == (
            "unknown"
        )
        minutes = sensors["gateway-one:zones:zn2:timeToNextSetpoint"]
        assert minutes.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        status = sensors["gateway-one:zones:zn1:status"]
        assert status.disabled_by is None
        assert status.capabilities["options"] == [
            "idle",
            "heat_request",
            "zone_error",
            "comm_error",
            "comm_warning",
            "low_battery",
        ]
        assert state(status.entity_id).state == "heat_request"
        assert state(sensors["gateway-one:zones:zn3:status"].entity_id).state == (
            "unknown"
        )
        assert state(sensors["gateway-one:zones:zn4:status"].entity_id).state == (
            "comm_warning"
        )
        valve = sensors["gateway-one:zones:zn1:actualValvePosition"]
        assert valve.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert valve.unit_of_measurement == "%"
        for key in ("optimumStartState", "optimumStartHeatupRate"):
            item = sensors[f"gateway-one:zones:zn1:{key}"]
            assert item.entity_category is EntityCategory.DIAGNOSTIC
            assert item.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        # The zone details have no generic sensor besides their own one.
        assert not [key for key in sensors if key.endswith(":openWindowDetection")]
        assert "gateway-one:zones:zn1:openWindowDetection:enabled" not in sensors

        windows = registered(hass, entry, "binary_sensor")
        window = {
            n: state(
                windows[f"gateway-one:zones:zn{n}:openWindowDetection:status"].entity_id
            ).state
            for n in (1, 2, 3)
        }
        assert window == {1: "off", 2: "on", 3: "unknown"}
        assert (
            windows[
                "gateway-one:zones:zn2:openWindowDetection:status"
            ].original_device_class
            == "window"
        )

        switches = registered(hass, entry, "switch")
        zone_switch = switches[
            "gateway-one:zones:zn1:openWindowDetection:enabled:control"
        ]
        assert zone_switch.translation_key == "rrc_window_detection"
        assert zone_switch.entity_category is EntityCategory.CONFIG
        assert zone_switch.disabled_by is None
        assert state(zone_switch.entity_id).state == "on"
        system = switches["gateway-one:system:openWindowDetection:enabled:control"]
        assert system.translation_key == "rrc_system_window_detection"
        assert system.disabled_by is er.RegistryEntryDisabler.INTEGRATION

        numbers = registered(hass, entry, "number")
        duration = numbers["gateway-one:system:openWindowDetection:duration:control"]
        assert duration.translation_key == "rrc_window_duration"
        assert duration.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert duration.capabilities["step"] == 5.0
        # S1: writeable 0 gives no number.
        assert (
            "gateway-one:system:openWindowDetection:temperature:control" not in numbers
        )
        # The temporary setpoint has no number of its own.
        assert not [key for key in numbers if "clockOverride" in key]
        # The system readings stay as disabled diagnostic mirrors.
        mirror = sensors["gateway-one:system:openWindowDetection:duration"]
        assert mirror.entity_category is EntityCategory.DIAGNOSTIC
        assert mirror.disabled_by is er.RegistryEntryDisabler.INTEGRATION

        buttons = registered(hass, entry, "button")
        assert sorted(key for key in buttons if key.endswith(":clear")) == [
            f"gateway-one:zones:zn{n}:clockOverride:temperatureHeating:clear"
            for n in range(1, 7)
            if n != 5
        ]
        clear = buttons["gateway-one:zones:zn2:clockOverride:temperatureHeating:clear"]
        assert clear.translation_key == "rrc_override_clear"
        assert state(clear.entity_id).state != "unavailable"
        idle = buttons["gateway-one:zones:zn3:clockOverride:temperatureHeating:clear"]
        assert state(idle.entity_id).state == "unavailable"  # nothing to clear
    finally:
        await teardown(hass, entry, stack)


async def test_window_switch_writes_the_wire_value_once(hass: HomeAssistant) -> None:
    cloud = zone_cloud()
    entry, coordinator, sleeps, stack, _clock = await setup(hass, cloud)
    try:
        switch = entity_id(
            hass, "switch", "gateway-one:zones:zn2:openWindowDetection:enabled:control"
        )
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": switch}, blocking=True
        )
        assert cloud.written("PUT") == [
            ("PUT", path(2, "openWindowDetection/enabled"), "on")
        ]
        assert hass.states.get(switch).state == "on"
        assert sleeps == [1.0]
        stats = coordinator.rrc_write_stats.snapshot("zone")
        assert stats["writes_confirmed"] == 1
    finally:
        await teardown(hass, entry, stack)


# ------------------------------------------------------------------ writes


async def test_target_goes_to_the_path_of_the_mode(hass: HomeAssistant) -> None:
    """P23: manual writes the manual setpoint (confirmed on a TC100.2), clock
    the temporary setpoint; a mode is never changed on the side."""
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        await set_temperature(hass, climate(hass, 1), temperature=22.5)
        await set_temperature(hass, climate(hass, 2), temperature=22.0)
        await set_temperature(hass, climate(hass, 3), temperature=21.5)
        assert cloud.written("PUT") == [
            ("PUT", path(1, MANUAL), 22.5),
            ("PUT", path(2, OVERRIDE), 22.0),
            ("PUT", path(3, OVERRIDE), 21.5),
        ]
        assert hass.states.get(climate(hass, 1)).attributes["temperature"] == 22.5
        three = hass.states.get(climate(hass, 3))
        assert three.state == "auto"
        assert three.attributes["temperature"] == 21.5
        assert three.attributes["temporary_override"] is True

        # With a mode: the mode first, confirmed, then the target of it.
        cloud.writes.clear()
        await set_temperature(
            hass, climate(hass, 3), temperature=20.5, hvac_mode="heat"
        )
        assert cloud.written("PUT") == [
            ("PUT", path(3, MODE), "manual"),
            ("PUT", path(3, MANUAL), 20.5),
        ]
        assert hass.states.get(climate(hass, 3)).state == "heat"

        cloud.writes.clear()
        await set_mode(hass, climate(hass, 1), "auto")
        assert cloud.written("PUT") == [("PUT", path(1, MODE), "clock")]
        assert hass.states.get(climate(hass, 1)).state == "auto"

        # A refused mode stops before the target: nothing else is sent.
        cloud.writes.clear()
        cloud.forbidden.add(path(2, MODE))
        with pytest.raises(HomeAssistantError):
            await set_temperature(
                hass, climate(hass, 2), temperature=20.0, hvac_mode="heat"
            )
        assert cloud.written("PUT") == [("PUT", path(2, MODE), "manual")]
    finally:
        await teardown(hass, entry, stack)


@pytest.mark.parametrize(
    "data",
    [
        {"temperature": 4.5},  # below the manual minimum
        {"temperature": 0},  # never clears through the climate
        {"temperature": 31.0},
        {"temperature": float("nan")},
        {"temperature": 21.0, "hvac_mode": "off"},
        {"target_temp_low": 20.0, "target_temp_high": 22.0},
    ],
)
async def test_invalid_targets_send_nothing(hass: HomeAssistant, data) -> None:
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        with pytest.raises((ServiceValidationError, ValueError)):
            await set_temperature(hass, climate(hass, 2), **data)
        assert not cloud.writes
    finally:
        await teardown(hass, entry, stack)


async def test_off_grid_target_is_refused_not_rounded(hass: HomeAssistant) -> None:
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        for zone in (1, 2):
            with pytest.raises(ServiceValidationError) as refused:
                await set_temperature(hass, climate(hass, zone), temperature=21.3)
            assert refused.value.translation_key == "write_validation_failed"
        assert not cloud.writes
    finally:
        await teardown(hass, entry, stack)


async def test_locked_mode_is_shown_and_refused(hass: HomeAssistant) -> None:
    """P04: used but unavailable: the modes stay, writing the mode is refused."""
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        locked = hass.states.get(climate(hass, 4))
        assert locked.state == "auto"
        assert locked.attributes["hvac_modes"] == ["heat", "auto"]
        with pytest.raises(ServiceValidationError) as refused:
            await set_mode(hass, climate(hass, 4), "heat")
        assert refused.value.translation_key == "rrc_mode_locked"
        assert not cloud.writes
    finally:
        await teardown(hass, entry, stack)


async def test_mode_changed_on_the_gateway_is_refused_without_a_put(
    hass: HomeAssistant,
) -> None:
    """The mode guard: a mode changed since the last reading never lets a
    target reach the path of the old mode."""
    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        mode = path(2, MODE)
        cloud.tree[mode] = replace(cloud.tree[mode], value="manual")
        with pytest.raises(ServiceValidationError) as refused:
            await set_temperature(hass, climate(hass, 2), temperature=22.0)
        assert refused.value.translation_key == "write_validation_failed"
        assert not cloud.writes
        # The fresh reading of the guard is shown at once.
        assert coordinator.data[mode].resource.value == "manual"
        assert hass.states.get(climate(hass, 2)).state == "heat"
    finally:
        await teardown(hass, entry, stack)


async def test_fahrenheit_targets_land_on_the_grid(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        state = hass.states.get(climate(hass, 1))
        assert state.attributes["min_temp"] == 41
        assert state.attributes["max_temp"] == 86
        assert state.attributes["target_temp_step"] == pytest.approx(0.9)
        await set_temperature(hass, climate(hass, 1), temperature=72)
        await set_temperature(hass, climate(hass, 2), temperature=70)
        assert cloud.written("PUT") == [
            ("PUT", path(1, MANUAL), 22.0),
            ("PUT", path(2, OVERRIDE), 21.0),
        ]
    finally:
        await teardown(hass, entry, stack)


async def test_writes_need_a_successful_cycle(hass: HomeAssistant) -> None:
    """A failed cloud cycle keeps the readings shown (O101) but every change
    waits for current readings; the next good cycle allows it again."""
    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, clock = await setup(hass, cloud)
    try:
        cloud.bulk_error = ResourceError("/zones", 503)
        await refresh(hass, coordinator, clock, 61)
        assert not coordinator.last_update_success
        assert hass.states.get(climate(hass, 1)).state == "heat"
        # The command names the outage; nothing is sent.
        with pytest.raises(HomeAssistantError) as refused:
            await set_temperature(hass, climate(hass, 1), temperature=22.0)
        assert refused.value.translation_key == "command_cloud_unreachable"
        assert not cloud.written("PUT")
        cloud.bulk_error = None
        await refresh(hass, coordinator, clock, 61)
        await set_temperature(hass, climate(hass, 1), temperature=22.0)
        assert cloud.written("PUT") == [("PUT", path(1, MANUAL), 22.0)]
    finally:
        await teardown(hass, entry, stack)


async def test_room_temperature_outage_keeps_the_climate(
    hass: HomeAssistant,
) -> None:
    """ZONE-07: only the mode makes the climate available; an invalid room
    temperature is unknown."""
    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, clock = await setup(hass, cloud)
    try:
        actual = path(1, "temperatureActual")
        cloud.tree[actual] = replace(cloud.tree[actual], value=99.0)  # off range
        await refresh(hass, coordinator, clock, 61)
        state = hass.states.get(climate(hass, 1))
        assert state.state == "heat"
        assert state.attributes["current_temperature"] is None
        assert state.attributes["temperature"] == 21.0
        mode = path(1, MODE)
        cloud.tree[mode] = replace(cloud.tree[mode], value="eco")  # no mode
        await refresh(hass, coordinator, clock, 301)
        assert hass.states.get(climate(hass, 1)).state == "unavailable"
        cloud.tree[mode] = replace(cloud.tree[mode], value="manual")
        cloud.tree[actual] = replace(cloud.tree[actual], value=20.0)
        await refresh(hass, coordinator, clock, 301)
        state = hass.states.get(climate(hass, 1))
        assert state.state == "heat"
        assert state.attributes["current_temperature"] == 20.0
    finally:
        await teardown(hass, entry, stack)


# ---------------------------------------------------------- plant shapes


async def test_no_heating_circuit_climate_beside_the_zone_climates(
    hass: HomeAssistant,
) -> None:
    tree = zone_tree()
    mode = "/heatingCircuits/hc1/operationMode"
    tree[mode] = parse_resource(
        {
            "id": mode,
            "type": "stringValue",
            "writeable": 1,
            "recordable": 0,
            "value": "auto",
            "allowedValues": ["off", "manual", "auto"],
        }
    )
    cloud = zone_cloud(with_containers(tree))
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        climates = registered(hass, entry, "climate")
        assert "gateway-one:heatingCircuits:hc1:climate" not in climates
        assert len(climates) == 5
    finally:
        await teardown(hass, entry, stack)


async def test_ct200_and_tc100_get_a_climate_per_zone(hass: HomeAssistant) -> None:
    cloud = RrcCloud(load_tree("rrc/ct200-shapes.json"))
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        climates = registered(hass, entry, "climate")
        assert sorted(climates) == sorted(
            f"gateway-one:zones:zn{n}:climate" for n in range(1, 12)
        )
        first = hass.states.get(climates["gateway-one:zones:zn1:climate"].entity_id)
        assert first.state == "auto"
        assert first.attributes["temperature"] == 21.0
        assert first.attributes["temporary_override"] is False
        assert first.attributes["hvac_action"] == "idle"
    finally:
        await teardown(hass, entry, stack)

    cloud = RrcCloud(
        {**with_containers(load_tree("rrc/tc100-string-flags.json")), **tc100_plant()},
        forbidden=set(),
        missing_as_500=False,
    )
    with patch(f"{PACKAGE}.coordinator.fault_resource_candidates", return_value=()):
        entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        climates = registered(hass, entry, "climate")
        assert sorted(climates) == [
            "gateway-one:zones:zn1:climate",
            "gateway-one:zones:zn4:climate",
        ]
        living = climates["gateway-one:zones:zn1:climate"].entity_id
        assert "Wohnzimmer" in hass.states.get(living).attributes["friendly_name"]
        # Regression of the hardware-confirmed write: the manual setpoint.
        await set_mode(hass, living, "heat")
        await set_temperature(hass, living, temperature=21.5)
        assert cloud.written("PUT") == [
            ("PUT", "/zones/zn1/userMode", "manual"),
            ("PUT", "/zones/zn1/manualTemperatureHeating", 21.5),
        ]
        assert hass.states.get(living).attributes["temperature"] == 21.5
        locked = hass.states.get(climates["gateway-one:zones:zn4:climate"].entity_id)
        assert locked.state == "heat"
    finally:
        await teardown(hass, entry, stack)


# --------------------------------------------------------------- polling


def test_zone_detail_poll_groups() -> None:
    """Measurements and status every minute, settings every 15 minutes."""
    tree = load_tree(FIXTURE)
    groups = {
        leaf: registry().poll_group(path(2, leaf))
        for leaf in rrc_contract_zone.DETAIL_FIELDS
    }
    assert groups == {
        "nextSetpoint": PollGroup.FAST,
        "timeToNextSetpoint": PollGroup.FAST,
        "status": PollGroup.FAST,
        "openWindowDetection/status": PollGroup.FAST,
        "openWindowDetection/enabled": PollGroup.SLOW,
        "actualValvePosition": PollGroup.SLOW,
        "optimumStartState": PollGroup.SLOW,
        "optimumStartHeatupRate": PollGroup.SLOW,
    }
    for system in (
        rrc_contract_zone.SYSTEM_WINDOW_ENABLED,
        rrc_contract_zone.SYSTEM_WINDOW_DURATION,
        rrc_contract_zone.SYSTEM_WINDOW_TEMPERATURE,
    ):
        assert registry().poll_group(system) is PollGroup.SLOW
    registry().check_paths(tree)


async def test_no_zone_request_faster_than_a_minute(hass: HomeAssistant) -> None:
    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, clock = await setup(hass, cloud)
    try:
        zone_paths = {item for item in cloud.tree if item.startswith("/zones/")}
        cloud.requests.clear()
        cloud.single.clear()
        await refresh(hass, coordinator, clock, 30)
        assert not cloud.all_requested() & zone_paths
        await refresh(hass, coordinator, clock, 31)
        read = cloud.all_requested() & zone_paths
        assert path(1, "status") in read
        assert path(1, "temperatureActual") in read
        assert path(1, "openWindowDetection/enabled") not in read
        assert path(1, "actualValvePosition") not in read
    finally:
        await teardown(hass, entry, stack)


async def test_eleven_zones_with_every_leaf_keep_the_lists(
    hass: HomeAssistant,
) -> None:
    """The budget: 11 zones with the eight W1 leaves each must not push the
    program and device lists out of the discovery."""
    tree = load_tree("rrc/ct200-shapes.json")
    extra = {}
    for zone in range(1, 12):
        for leaf, fields in (
            ("nextSetpoint", {"type": "floatValue", "unitOfMeasure": "C"}),
            ("timeToNextSetpoint", {"type": "floatValue", "unitOfMeasure": "mins"}),
            ("status", {"type": "stringValue"}),
            ("openWindowDetection/enabled", {"type": "stringValue"}),
            ("openWindowDetection/status", {"type": "stringValue"}),
            ("actualValvePosition", {"type": "floatValue", "unitOfMeasure": "%"}),
            ("optimumStartState", {"type": "stringValue"}),
            ("optimumStartHeatupRate", {"type": "floatValue"}),
        ):
            item = path(zone, leaf)
            if item not in tree:
                value = "idle" if fields["type"] == "stringValue" else 10.0
                extra[item] = parse_resource(
                    {"id": item, "writeable": 0, "recordable": 0, "value": value}
                    | fields
                )
    cloud = RrcCloud({**tree, **extra})
    entry, coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        report = coordinator.discovery_diagnostics
        assert report.stop_reason == "complete"
        requested = cloud.all_requested()
        assert {"/zones/list", "/programs/list", "/devices/list"} <= requested
        assert set(extra) <= set(coordinator.resources)
        assert len(registered(hass, entry, "climate")) == 11
        selects = registered(hass, entry, "select")
        assert len(
            [key for key in selects if key.endswith(":clockProgram:control")]
        ) == (11)
    finally:
        await teardown(hass, entry, stack)


def test_detail_readings_are_checked() -> None:
    def detail(leaf: str, **fields: object) -> Resource:
        return parse_resource(
            {"id": path(1, leaf), "writeable": 0, "recordable": 0, **fields}
        )

    floats = {"type": "floatValue"}
    assert (
        rrc.detail_value(
            detail("nextSetpoint", **floats, unitOfMeasure="C", value=999.0)
        )
        is None
    )
    assert (
        rrc.detail_value(
            detail("nextSetpoint", **floats, unitOfMeasure="C", value=18.5)
        )
        == 18.5
    )
    assert (
        rrc.detail_value(
            detail(
                "nextSetpoint", **floats, unitOfMeasure="C", value=18.5, used="false"
            )
        )
        is None
    )
    assert (
        rrc.detail_value(
            detail("nextSetpoint", **floats, unitOfMeasure="K", value=18.5)
        )
        is None
    )
    assert (
        rrc.detail_value(
            detail("timeToNextSetpoint", **floats, unitOfMeasure="mins", value=-1.0)
        )
        is None
    )
    assert (
        rrc.detail_value(
            detail("timeToNextSetpoint", **floats, unitOfMeasure="s", value=60.0)
        )
        is None
    )
    assert (
        rrc.detail_value(
            detail("actualValvePosition", **floats, unitOfMeasure="%", value=101.0)
        )
        is None
    )
    assert (
        rrc.detail_value(
            detail("actualValvePosition", **floats, unitOfMeasure="%", value=55.0)
        )
        == 55.0
    )
    assert (
        rrc.detail_value(detail("status", type="stringValue", value="heat request"))
        == "heat_request"
    )
    assert rrc.detail_value(detail("status", type="stringValue", value=3)) is None
    assert (
        rrc.detail_value(
            detail("openWindowDetection/status", type="stringValue", value="open")
        )
        is True
    )
    assert (
        rrc.detail_value(
            detail("optimumStartState", type="stringValue", value="x" * 40)
        )
        is None
    )
    assert (
        rrc.detail_value(
            detail("optimumStartState", type="stringValue", value="heating up")
        )
        == "heating up"
    )
    # A wrong type gives no entity at all.
    assert not rrc.detail_shape(detail("status", type="floatValue", value=1.0))
    # The zone details are no generic zone scalars and no controls.
    status = detail("status", type="stringValue", value="idle")
    assert rrc.scalar_field(status.path) == "status"
    assert rrc.scalar_value(status) is None
    assert not rrc.is_control(status.path)
    assert rrc.name(status.path, "de") is None


async def test_zone_commands_during_a_protection_pause_name_it(
    hass: HomeAssistant,
) -> None:
    """A paused cloud refuses a zone command with the reason, before anything
    is read or sent."""
    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, clock = await setup(hass, cloud)
    try:
        coordinator._circuit_open_until = clock.now + 600
        for command in (
            lambda: set_mode(hass, climate(hass, 1), "auto"),
            lambda: set_temperature(hass, climate(hass, 1), temperature=21.5),
        ):
            with pytest.raises(HomeAssistantError) as refused:
                await command()
            assert refused.value.translation_key == "command_cloud_protection_pause"
            assert refused.value.translation_placeholders["until"]
        assert not cloud.writes
    finally:
        await teardown(hass, entry, stack)
