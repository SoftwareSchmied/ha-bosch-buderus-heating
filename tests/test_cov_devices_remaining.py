"""Device groups: the last branches (availability, discovery, unexpected errors).

Wallbox entity availability follows the same rules as the commands; errors
that are not part of the PointT error family must not be swallowed or turned
into a wrong outcome, and a held lock must always be released.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.climate.const import HVACMode
from homeassistant.helpers.entity import Entity
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.bosch_buderus_heating import zone_radio_devices
from custom_components.bosch_buderus_heating.aircon import AirconCoordinator
from custom_components.bosch_buderus_heating.aircon_policies import FAN_SPEED_POLICY
from custom_components.bosch_buderus_heating.configuration_migration import (
    legacy_cloud_connection_id,
)
from custom_components.bosch_buderus_heating.managed_wallbox import (
    ManagedWallboxCoordinator,
)
from custom_components.bosch_buderus_heating.pointt import Gateway
from custom_components.bosch_buderus_heating.wallbox import WallboxCoordinator
from custom_components.bosch_buderus_heating.wallbox_control import async_start
from custom_components.bosch_buderus_heating.wallbox_entities import (
    WallboxNumber,
    WallboxSelect,
    WallboxSwitch,
    WallboxText,
)
from custom_components.bosch_buderus_heating.wallbox_flow import wallboxes_of
from custom_components.bosch_buderus_heating.zone_controls import (
    is_zone_control,
    is_zone_temporary,
)
from custom_components.bosch_buderus_heating.zone_radio_devices import (
    ZoneRadioBatteryBinarySensor,
    track_zone_radio_devices,
)
from tests import test_zone_climate, test_zone_radio_devices
from tests.aircon_helpers import (  # noqa: F401
    FakeUnit,
    aircon_cleanup,
    make_coordinator,
)
from tests.test_cov_devices_wallbox_entities import _Source
from tests.test_wallbox_controls import (
    FakeWallbox,
    _spec,
    config,
    fixture,
    sleeper,
    wallbox,
)
from tests.test_zone_climate import MANUAL, MODE, TEMPORARY, Z1, changed, climate
from tests.test_zone_radio_devices import battery, show


@pytest.fixture(name="plant")
def zone_plant(hass):
    return test_zone_climate.zone_plant.__wrapped__(hass)


@pytest.fixture(name="coordinator")
def radio_coordinator(hass):
    return test_zone_radio_devices.radio_coordinator.__wrapped__(hass)


CHILD_LOCK = f"{Z1}/childLock"


async def _now(coordinator):
    coordinator._next.clear()
    await coordinator.async_refresh()


# Wallbox entity availability follows the command rules --------------------------------


async def test_wallbox_entities_are_unavailable_where_their_command_would_be_refused(
    hass,
):
    coordinator, fake = await wallbox(hass)
    price = WallboxNumber(coordinator, _spec("electricity_price"))
    boost = WallboxSwitch(coordinator, _spec("boost"))
    strategy = WallboxSelect(coordinator, _spec("charging_strategy"))
    label = WallboxText(coordinator, _spec("label"))
    assert price.available and boost.available and strategy.available
    assert label.available
    fake.telemetry(energymanager="emma")
    fake.leaf("telemetry/extmeterState", "DISCONNECTED")
    coordinator.leaves.pop("telemetry/extmeterState")
    for _ in range(3):
        await _now(coordinator)
    # An energy manager controls charging: price, boost and mode are shown only.
    assert not price.available
    assert not boost.available
    assert not strategy.available
    # The display name is not part of charging control.
    assert label.available
    fake.conf(wallboxMode="leader")
    await _now(coordinator)
    assert not label.available


# Errors outside the PointT family -----------------------------------------------------


async def test_an_unexpected_timeout_passes_through_and_frees_the_wallbox(hass):
    coordinator, fake = await wallbox(hass)
    fake.errors["telemetry"] = TimeoutError()
    with pytest.raises(TimeoutError):
        await async_start(coordinator, sleep=sleeper())
    assert fake.writes() == []
    assert not coordinator.poll_lock.locked()
    assert coordinator.write_counts == {"start_failed": 1}


async def test_an_unexpected_timeout_passes_through_and_frees_the_aircon(hass):
    coordinator, unit, _ = await make_coordinator(hass)

    async def broken(*_args, **_kwargs):
        raise TimeoutError

    unit.get_resources_bulk = broken
    with pytest.raises(TimeoutError):
        await coordinator.async_write(
            "/airConditioning/fanSpeed", "high", FAN_SPEED_POLICY
        )
    assert unit.puts == []
    assert not coordinator._lock.locked()
    assert isinstance(coordinator, AirconCoordinator)


# Managed wallbox ----------------------------------------------------------------------


async def test_a_live_source_publishes_new_wallbox_data(hass):
    fake = FakeWallbox(fixture())
    entry = config(hass)
    source = _Source(hass, entry)
    source.connection.connection_id = legacy_cloud_connection_id(entry.entry_id)
    coordinator = ManagedWallboxCoordinator(
        source, fake, Gateway("wallbox-A", device_type="commodule")
    )
    await coordinator.async_refresh()
    published = []
    remove = coordinator.async_add_listener(lambda: published.append(coordinator.data))
    snapshot = coordinator.data["telemetry"]
    coordinator.async_set_updated_data({"telemetry": snapshot})
    assert published == [{"telemetry": snapshot}]
    remove()
    assert isinstance(coordinator, WallboxCoordinator)


# Options menu helper ------------------------------------------------------------------


def test_the_wallboxes_of_an_entry_come_from_its_runtime_only_when_they_are_wallboxes():
    assert wallboxes_of(SimpleNamespace(runtime_data=None)) == ()
    assert wallboxes_of(SimpleNamespace(runtime_data=SimpleNamespace())) == ()
    legacy = SimpleNamespace(wallboxes=("one",))
    assert wallboxes_of(SimpleNamespace(runtime_data=legacy)) == ("one",)

    class Connections:
        snapshot = SimpleNamespace(bindings={"a": 1, "b": 2})

        def display_data(self, key):
            return {"a": object(), "b": None}[key]

    connected = SimpleNamespace(connections=Connections())
    assert wallboxes_of(SimpleNamespace(runtime_data=connected)) == ()


# Zone paths and radio devices ---------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "temporary"),
    [
        ("/zones/zone1/heating/temporaryRoomSetpoint", True),
        ("/zones/zone2/cooling/temporaryRoomSetpoint", True),
        ("/zones/zone1/heating/manualRoomSetpoint", False),
        ("/zones/zone1/heating/temporaryRoomSetpoint/x", False),
        ("/zones/zone1/other/temporaryRoomSetpoint", False),
        ("/heatingCircuits/hc1/temporaryRoomSetpoint", False),
    ],
)
def test_only_zone_temporary_setpoints_belong_to_the_zone_climate(path, temporary):
    assert is_zone_temporary(path) is temporary
    if temporary:
        # A temporary setpoint is never a plain zone control.
        assert not is_zone_control(path)


async def test_a_battery_without_a_reading_shows_no_state(coordinator):
    entity = ZoneRadioBatteryBinarySensor(coordinator, "device1")
    assert not entity.available
    assert entity.is_on is None
    assert entity.extra_state_attributes == {}


async def test_the_devices_of_a_room_controller_get_no_zone_entities(coordinator):
    show(coordinator, battery())
    added: list[Entity] = []
    unload = []
    with patch.object(
        zone_radio_devices,
        "context_of",
        return_value=SimpleNamespace(rrc_plant=True),
    ):
        track_zone_radio_devices(
            SimpleNamespace(async_on_unload=unload.append),
            coordinator,
            added.extend,
            ZoneRadioBatteryBinarySensor,
        )
    assert added == []
    track_zone_radio_devices(
        SimpleNamespace(async_on_unload=unload.append),
        coordinator,
        added.extend,
        ZoneRadioBatteryBinarySensor,
    )
    assert [entity.unique_id for entity in added] == ["gateway:devices:device1:battery"]
    for callback in unload:
        callback()


# Zone climate details -----------------------------------------------------------------


async def test_manual_mode_shows_the_limits_of_the_manual_setpoint(hass, plant):
    coordinator, cloud = plant
    changed(coordinator, cloud, MODE, value="manual")
    entity = climate(hass, coordinator)
    assert (entity.min_temp, entity.max_temp, entity.target_temperature_step) == (
        5,
        30,
        0.5,
    )
    assert entity.hvac_mode == HVACMode.HEAT
    assert entity.native_target_temperature == 21.0


async def test_attributes_leave_out_what_the_zone_does_not_report(hass, plant):
    coordinator, cloud = plant
    del coordinator.data[TEMPORARY]
    changed(coordinator, cloud, CHILD_LOCK, value="maybe")
    attributes = climate(hass, coordinator).extra_state_attributes
    assert "temporary_override" not in attributes
    assert "child_lock" not in attributes
    assert attributes["zone_side"] == "heating"


async def test_a_manual_setpoint_without_a_reported_step_uses_the_default_grid(
    hass, plant
):
    coordinator, cloud = plant
    hass.config.units = US_CUSTOMARY_SYSTEM
    changed(coordinator, cloud, MODE, value="manual")
    changed(coordinator, cloud, MANUAL, step_size=None)
    entity = climate(hass, coordinator)
    await entity.async_set_temperature(temperature=(72 - 32) * 5 / 9)
    # The default grid is used: 72 F (22.22 C) becomes the nearest 22.0 C.
    assert cloud.puts == [(MANUAL, 22.0)]
