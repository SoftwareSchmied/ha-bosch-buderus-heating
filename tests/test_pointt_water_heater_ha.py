"""Native HA services, translations and registry continuity for PointT hot water."""

from unittest.mock import patch

import pytest
from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.bosch_buderus_heating.const import DOMAIN
from tests.test_pointt_water_heater import MODE, setup


@pytest.mark.parametrize("fahrenheit", [False, True])
@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.usefixtures("legacy_platform_runtime")
async def test_native_water_heater_services_and_preserved_controls(
    hass, enable_custom_integrations, fahrenheit, language
):
    hass.config.language = language
    if fahrenheit:
        hass.config.units = US_CUSTOMARY_SYSTEM
    entry, coordinator, expected, _ = setup(hass)
    runtime = entry.runtime_data
    registry = er.async_get(hass)
    original = registry.async_get_or_create(
        "select",
        DOMAIN,
        "gateway:dhwCircuits:dhw2:operationMode:control",
        suggested_object_id="existing_hot_water_mode",
        config_entry=entry,
    )
    registry.async_update_entity(
        original.entity_id, name="Meine Betriebsart", icon="mdi:star"
    )
    platforms = [Platform.WATER_HEATER, Platform.SELECT]

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
        heater_id = registry.async_get_entity_id(
            "water_heater", DOMAIN, expected.unique_id
        )
        assert heater_id
        state = hass.states.get(heater_id)
        assert state.state == "ownprogram"
        assert state.attributes["current_temperature"] == (
            119.3 if fahrenheit else 48.5
        )
        assert state.attributes["temperature"] == (125.6 if fahrenheit else 52)
        assert (
            state.attributes["supported_features"]
            == WaterHeaterEntityFeature.OPERATION_MODE
        )
        assert state.attributes["operation_list"] == [
            "off",
            "low",
            "high",
            "ownprogram",
            "eco",
        ]
        assert (
            not {"min_temp", "max_temp", "target_temp_step"} & state.attributes.keys()
        )
        current = registry.async_get(original.entity_id)
        assert current.name == "Meine Betriebsart" and current.icon == "mdi:star"
        assert current.entity_category == EntityCategory.CONFIG
        assert registry.async_get(heater_id).device_id == current.device_id
        translations = await async_get_translations(hass, language, "entity", {DOMAIN})
        prefix = f"component.{DOMAIN}.entity.water_heater.pointt_dhw"
        assert translations[prefix + ".state.high"] == (
            "Komfort" if language == "de" else "Comfort"
        )
        assert translations[prefix + ".state.eco"] == "Eco+"
        assert (
            translations[prefix + ".state_attributes.operation_mode.state.low"] == "Eco"
        )
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "water_heater",
                "set_temperature",
                {"entity_id": heater_id, "temperature": 45},
                blocking=True,
            )
        coordinator.client.put_resource_value.assert_not_awaited()
        await hass.services.async_call(
            "water_heater",
            "set_operation_mode",
            {"entity_id": heater_id, "operation_mode": "high"},
            blocking=True,
        )
        coordinator.client.put_resource_value.assert_awaited_once_with(
            "gateway", MODE, "high"
        )
        assert hass.states.get(heater_id).state == "high"
        assert hass.states.get(heater_id).attributes["temperature"] is None
        await coordinator.async_refresh()
        assert hass.states.get(heater_id).attributes["temperature"] == (
            125.6 if fahrenheit else 52
        )
        registry.async_update_entity(heater_id, name="Mein Warmwasser")
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get(original.entity_id).id == original.id
        assert registry.async_get(heater_id).name == "Mein Warmwasser"
        assert hass.states.get(heater_id).state == "high"
        await hass.services.async_call(
            "water_heater",
            "set_operation_mode",
            {"device_id": current.device_id, "operation_mode": "off"},
            blocking=True,
        )
        assert coordinator.data[MODE].resource.value == "Off"
        assert await hass.config_entries.async_unload(entry.entry_id)
        registry.async_update_entity(
            heater_id, disabled_by=er.RegistryEntryDisabler.USER
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert (
            registry.async_get(heater_id).disabled_by is er.RegistryEntryDisabler.USER
        )
        disabled_state = hass.states.get(heater_id)
        if hasattr(WaterHeaterEntity, "native_temperature_unit"):
            # Home Assistant 2026.11 shows no state for a disabled entity.
            assert disabled_state is None
        else:  # older releases keep a restored placeholder
            assert disabled_state.state == "unavailable"
            assert disabled_state.attributes["restored"] is True
        assert registry.async_get(original.entity_id).disabled_by is None
        assert await hass.config_entries.async_unload(entry.entry_id)
