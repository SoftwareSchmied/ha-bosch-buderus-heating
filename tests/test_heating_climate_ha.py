"""Exercise real HA service routing, display units and existing registry entries."""

from unittest.mock import patch

import pytest
from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.const import EntityCategory, Platform
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.bosch_buderus_heating.const import DOMAIN
from tests.test_heating_controls import MODE, TARGET, setup


@pytest.mark.parametrize("fahrenheit", [False, True])
@pytest.mark.usefixtures("legacy_platform_runtime")
async def test_real_climate_services_and_old_control_registry(
    hass, enable_custom_integrations, fahrenheit
):
    if fahrenheit:
        hass.config.units = US_CUSTOMARY_SYSTEM
    entry, coordinator, expected_entity = setup(hass)
    runtime = entry.runtime_data
    registry = er.async_get(hass)
    original = registry.async_get_or_create(
        "number",
        DOMAIN,
        "gateway:heatingCircuits:hc2:manualRoomSetpoint:control",
        suggested_object_id="existing_target",
        config_entry=entry,
    )
    registry.async_update_entity(
        original.entity_id, name="Mein Sollwert", icon="mdi:star"
    )
    platforms = [Platform.CLIMATE, Platform.NUMBER, Platform.SELECT]

    async def start(hass, config):
        config.runtime_data = runtime
        await hass.config_entries.async_forward_entry_setups(config, platforms)
        return True

    async def stop(hass, config):
        result = await hass.config_entries.async_unload_platforms(config, platforms)
        await coordinator.async_shutdown()
        return result

    with (
        patch(
            "custom_components.bosch_buderus_heating.async_setup_entry",
            side_effect=start,
        ),
        patch(
            "custom_components.bosch_buderus_heating.async_unload_entry",
            side_effect=stop,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        climate_id = registry.async_get_entity_id(
            "climate", DOMAIN, expected_entity.unique_id
        )
        assert climate_id
        state = hass.states.get(climate_id)
        assert state.state == "heat"
        assert state.attributes["current_temperature"] == (68 if fahrenheit else 20)
        assert state.attributes["temperature"] == (66.2 if fahrenheit else 19)
        assert state.attributes["target_temp_step"] == (0.9 if fahrenheit else 0.5)
        assert state.attributes["min_temp"] == (41 if fahrenheit else 5)
        assert "hvac_action" not in state.attributes
        current = registry.async_get(original.entity_id)
        assert current.name == "Mein Sollwert" and current.icon == "mdi:star"
        assert current.entity_category == EntityCategory.CONFIG
        assert registry.async_get(climate_id).device_id == current.device_id
        assert hass.states.get(original.entity_id).attributes["mode"] == "box"
        for target in ({"entity_id": climate_id}, {"device_id": current.device_id}):
            await hass.services.async_call(
                "climate",
                "set_temperature",
                {**target, "temperature": 68.9 if fahrenheit else 20.5},
                blocking=True,
            )
            assert coordinator.data[TARGET].resource.value == pytest.approx(20.5)
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": climate_id, "hvac_mode": "auto"},
            blocking=True,
        )
        assert coordinator.data[MODE].resource.value == "auto"
        automatic = hass.states.get(climate_id)
        assert automatic.attributes["temperature"] == (69.8 if fahrenheit else 21)
        assert not (
            automatic.attributes["supported_features"]
            & ClimateEntityFeature.TARGET_TEMPERATURE
        )
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": climate_id, "temperature": 69.8 if fahrenheit else 21},
                blocking=True,
            )
        registry.async_update_entity(climate_id, name="Meine Heizung")
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get(original.entity_id).id == original.id
        assert registry.async_get(climate_id).name == "Meine Heizung"
        assert hass.states.get(climate_id).state == "auto"
        await hass.services.async_call(
            "climate", "turn_off", {"entity_id": climate_id}, blocking=True
        )
        assert coordinator.data[MODE].resource.value == "off"
        assert (
            registry.async_get_entity_id("climate", DOMAIN, expected_entity.unique_id)
            == climate_id
        )
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
