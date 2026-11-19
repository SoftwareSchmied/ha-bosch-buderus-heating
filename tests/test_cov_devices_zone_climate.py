"""Zone climate: readings that go missing, stale or unusable, and refusals.

Built on the synthetic zone controller of ``test_zone_climate``. Every test
checks what the entity shows and that no PUT goes out when it must not.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.zone_climate import (
    ZoneClimate,
    track_zone_climates,
)
from tests import test_zone_climate
from tests.test_zone_climate import (
    CONTROL_TYPE,
    MANUAL,
    MODE,
    RESOURCES,
    SEASON,
    TEMPORARY,
    Z1,
    button,
    changed,
    climate,
)


@pytest.fixture(name="plant")
def zone_plant(hass):
    return test_zone_climate.zone_plant.__wrapped__(hass)


HUMIDITY = f"{Z1}/averageActualHumidity"
TARGET = ClimateEntityFeature.TARGET_TEMPERATURE


def stale(coordinator, path):
    coordinator.data[path] = replace(coordinator.data[path], freshness=Freshness.STALE)


@pytest.fixture(name="unloaders", autouse=True)
def unloaders():
    """Remove the discovery listeners again, as an unloading entry does."""
    callbacks = []
    yield callbacks
    for callback in callbacks:
        callback()


def tracked(coordinator, unloaders):
    added = []
    track_zone_climates(
        SimpleNamespace(async_on_unload=unloaders.append),
        coordinator,
        added.extend,
    )
    return {entity.root: entity for entity in added}


async def _refused(call, key, cloud):
    with pytest.raises(ServiceValidationError) as caught:
        await call
    assert caught.value.translation_key == key
    assert cloud.puts == []


# Creation from the stored inventory ---------------------------------------------------


async def test_a_zone_stored_before_the_first_reading_is_added_unavailable(
    plant, unloaders
):
    coordinator, _ = plant
    stale(coordinator, MODE)
    assert Z1 not in tracked(coordinator, unloaders)
    coordinator._stored_resources = {MODE: RESOURCES[MODE]}
    found = tracked(coordinator, unloaders)
    assert Z1 in found
    assert not found[Z1].available


async def test_a_stored_zone_that_would_not_be_created_normally_stays_out(
    plant, unloaders
):
    coordinator, _ = plant
    stale(coordinator, MODE)
    coordinator._stored_resources = {
        MODE: replace(RESOURCES[MODE], value="no-such-mode")
    }
    assert Z1 not in tracked(coordinator, unloaders)


# Season of the circuit ----------------------------------------------------------------


async def test_without_a_season_reading_the_zone_is_on_the_heating_side(hass, plant):
    coordinator, _ = plant
    del coordinator.data[SEASON]
    entity = climate(hass, coordinator)
    assert entity.hvac_mode == HVACMode.AUTO
    assert entity.extra_state_attributes["zone_side"] == "heating"


@pytest.mark.parametrize("how", ["stale", "not_text", "invalid_mode"])
async def test_an_unreadable_zone_shows_nothing_and_takes_no_command(hass, plant, how):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    if how == "stale":
        stale(coordinator, SEASON)
    elif how == "not_text":
        changed(coordinator, cloud, SEASON, value=7)
    else:
        changed(coordinator, cloud, MODE, value="unknown")
    assert not entity.available
    assert entity.hvac_mode is None
    assert entity.hvac_modes == []
    assert entity.hvac_action is None
    assert entity.native_target_temperature is None
    assert (entity.min_temp, entity.max_temp) == (0, 0)
    assert entity.target_temperature_step is None
    assert entity.extra_state_attributes == {}
    assert entity.supported_features == ClimateEntityFeature(0)
    await _refused(
        entity.async_set_hvac_mode(HVACMode.HEAT), "zone_climate_command", cloud
    )
    await _refused(
        entity.async_set_temperature(temperature=22), "zone_climate_command", cloud
    )


# Control type of the circuit ----------------------------------------------------------


async def test_a_stale_constants_control_type_keeps_the_zone_read_only(hass, plant):
    coordinator, cloud = plant
    changed(coordinator, cloud, CONTROL_TYPE, value="constants")
    stale(coordinator, CONTROL_TYPE)
    entity = climate(hass, coordinator)
    assert entity.hvac_modes == [HVACMode.AUTO]
    assert not entity.supported_features & TARGET
    await _refused(
        entity.async_set_hvac_mode(HVACMode.HEAT), "zone_climate_command", cloud
    )


async def test_a_control_type_missing_from_the_poll_keeps_its_last_constants(
    hass, plant
):
    coordinator, cloud = plant
    changed(coordinator, cloud, CONTROL_TYPE, value="constants")
    constants = coordinator.data[CONTROL_TYPE].resource
    del coordinator.data[CONTROL_TYPE]
    coordinator.resources[CONTROL_TYPE] = constants
    entity = climate(hass, coordinator)
    assert entity.hvac_modes == [HVACMode.AUTO]
    # Never reported at all: not read-only.
    del coordinator.resources[CONTROL_TYPE]
    assert HVACMode.HEAT in entity.hvac_modes


async def test_a_stale_guard_reading_blocks_the_write_but_not_the_display(hass, plant):
    coordinator, cloud = plant
    stale(coordinator, CONTROL_TYPE)
    entity = climate(hass, coordinator)
    assert HVACMode.HEAT in entity.hvac_modes
    await _refused(
        entity.async_set_hvac_mode(HVACMode.HEAT), "zone_climate_command", cloud
    )


async def test_a_circuit_without_control_type_reading_is_written_with_its_season(
    hass, plant
):
    coordinator, cloud = plant
    del coordinator.data[CONTROL_TYPE]
    entity = climate(hass, coordinator)
    await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert cloud.puts == [(MODE, "manual")]
    assert SEASON in cloud.bulk_calls[0]
    assert CONTROL_TYPE not in cloud.bulk_calls[0]


async def test_a_zone_without_a_circuit_is_written_without_guards(hass, plant):
    coordinator, cloud = plant
    changed(coordinator, cloud, "/devices/device1/assignedHC", value="hc2")
    entity = climate(hass, coordinator)
    await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert cloud.puts == [(MODE, "manual")]
    assert SEASON not in cloud.bulk_calls[0]
    assert CONTROL_TYPE not in cloud.bulk_calls[0]


# Manual setpoint ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "key"),
    [
        ({"used": False}, "control_unused"),
        ({"writable": False}, "zone_climate_command"),
    ],
)
async def test_an_unusable_manual_setpoint_offers_and_takes_no_target(
    hass, plant, change, key
):
    coordinator, cloud = plant
    changed(coordinator, cloud, MODE, value="manual")
    changed(coordinator, cloud, MANUAL, **change)
    entity = climate(hass, coordinator)
    assert not entity.supported_features & TARGET
    assert (entity.min_temp, entity.max_temp) == (0, 0)
    await _refused(entity.async_set_temperature(temperature=21), key, cloud)


async def test_a_manual_setpoint_without_a_range_has_no_limits(hass, plant):
    coordinator, cloud = plant
    changed(coordinator, cloud, MODE, value="manual")
    changed(coordinator, cloud, MANUAL, minimum=None)
    entity = climate(hass, coordinator)
    assert entity.min_temp == 0
    assert entity.target_temperature_step is None


async def test_the_mode_off_takes_no_target_temperature(hass, plant):
    coordinator, cloud = plant
    changed(coordinator, cloud, MODE, value="off")
    entity = climate(hass, coordinator)
    await _refused(
        entity.async_set_temperature(temperature=21), "zone_climate_command", cloud
    )


# Mode requests ------------------------------------------------------------------------


async def test_a_mode_the_zone_does_not_offer_is_refused(hass, plant):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    await _refused(
        entity.async_set_hvac_mode(HVACMode.COOL), "zone_climate_command", cloud
    )
    # Zone 2 offers only off and auto.
    zone2 = climate(hass, coordinator, "/zones/zone2")
    await _refused(
        zone2.async_set_hvac_mode(HVACMode.HEAT), "zone_climate_command", cloud
    )
    await _refused(
        zone2.async_set_temperature(temperature=20, hvac_mode=HVACMode.HEAT),
        "zone_climate_command",
        cloud,
    )


async def test_a_target_in_a_mode_whose_setpoint_is_unused_names_the_reason(
    hass, plant
):
    """The target's flags may follow the mode, so they are checked on the
    reading of the target's own write: the mode is sent, and a target the
    gateway still does not use is refused with the reason."""
    coordinator, cloud = plant
    changed(coordinator, cloud, MANUAL, used=False)
    entity = climate(hass, coordinator)
    with pytest.raises(ServiceValidationError) as caught:
        await entity.async_set_temperature(temperature=21, hvac_mode=HVACMode.HEAT)
    assert caught.value.translation_key == "control_unused"
    assert cloud.puts == [(MODE, "manual")]


async def test_a_mode_that_moved_again_before_the_target_sends_no_target(hass, plant):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    original = ZoneClimate._async_write_mode

    async def then_moved(self, command, hvac_mode):
        await original(self, command, hvac_mode)
        changed(coordinator, cloud, MODE, value="auto")

    with patch.object(ZoneClimate, "_async_write_mode", then_moved):
        await _refused_after_mode(entity, cloud)


async def _refused_after_mode(entity, cloud):
    with pytest.raises(ServiceValidationError) as caught:
        await entity.async_set_temperature(temperature=22, hvac_mode=HVACMode.HEAT)
    assert caught.value.translation_key == "zone_climate_command"
    assert cloud.puts == [(MODE, "manual")]


# Humidity -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"value": 55.5}, 55.5),
        ({"value": 101}, None),
        ({"value": -1}, None),
        ({"value": 0}, None),
        ({"value": 100}, 100.0),
        ({"value": float("nan")}, None),
        ({"value": True}, None),
        ({"value": "wet"}, None),
        ({"unit": "C"}, None),
    ],
)
async def test_humidity_is_shown_only_as_a_percentage_in_range(
    hass, plant, change, expected
):
    coordinator, cloud = plant
    changed(coordinator, cloud, HUMIDITY, **change)
    assert climate(hass, coordinator).current_humidity == expected


# Cloud down ---------------------------------------------------------------------------


async def test_cloud_down_is_named_for_a_temperature_and_the_clear_button(hass, plant):
    coordinator, cloud = plant
    changed(coordinator, cloud, TEMPORARY, value=22.5)
    coordinator.control_status = lambda: ("rate_limited", None)
    with pytest.raises(HomeAssistantError) as caught:
        await climate(hass, coordinator).async_set_temperature(temperature=22)
    assert caught.value.translation_key == "command_cloud_rate_limited"
    with pytest.raises(HomeAssistantError) as caught:
        await button(hass, coordinator).async_press()
    assert caught.value.translation_key == "command_cloud_rate_limited"
    assert cloud.puts == []


# Clear button -------------------------------------------------------------------------


async def test_clear_button_is_unavailable_when_the_side_is_unknown(hass, plant):
    coordinator, cloud = plant
    changed(coordinator, cloud, TEMPORARY, value=22.5)
    stale(coordinator, SEASON)
    entity = button(hass, coordinator)
    assert not entity.available
    await _refused(entity.async_press(), "zone_temporary_setpoint_not_active", cloud)
