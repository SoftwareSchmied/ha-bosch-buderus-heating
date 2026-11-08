"""Actual HA platforms, translations, services and registry continuity."""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.bosch_buderus_heating.bacon import dhw
from custom_components.bosch_buderus_heating.const import DOMAIN
from tests.test_bacon_dhw import configured, responding
from tests.test_bacon_ha import entry
from tests.test_bacon_session import emit

ROOT = "custom_components.bosch_buderus_heating"


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("language,fahrenheit", [("de", False), ("en", True)])
@pytest.mark.usefixtures("legacy_platform_runtime")
async def test_ha_platform_services_translations_and_registry(
    hass, language, fahrenheit
):
    hass.config.language = language
    if fahrenheit:
        hass.config.units = US_CUSTOMARY_SYSTEM
    config = entry(hass)
    account, heater, data = configured()
    writes = responding(account, data)

    async def wait():
        await asyncio.Event().wait()

    with (
        patch(ROOT + ".BaconAccount", return_value=account),
        patch.object(account, "run", side_effect=wait),
    ):
        assert await hass.config_entries.async_setup(config.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id(
            "water_heater", DOMAIN, heater.unique_id
        )
        assert entity_id is not None
        registered = registry.async_get(entity_id)
        current = hass.states.get(entity_id)
        assert current.state == "electric"
        assert current.attributes["temperature"] == (122 if fahrenheit else 50)
        assert current.attributes["operation_list"] == ["off", *dhw.MODES["midea"]]
        assert current.attributes["target_temp_step"] == (9 if fahrenheit else 5)
        assert registered.translation_key == "bacon_dhw"
        for locale in ("de", "en"):
            texts = await async_get_translations(hass, locale, "entity", {DOMAIN})
            prefix = "component." + DOMAIN + ".entity.water_heater.bacon_dhw"
            for option in ("off", *dhw.MODES["midea"], *dhw.MODES["rwh"]):
                assert texts[prefix + ".state." + option]
                assert texts[
                    prefix + ".state_attributes.operation_mode.state." + option
                ]
            assert texts[prefix + ".state.electric"] == (
                "Elektroheizung" if locale == "de" else "Electric heater"
            )
            assert texts[prefix + ".state.comfort"] == (
                "Komfort" if locale == "de" else "Comfort"
            )
        await hass.services.async_call(
            "water_heater",
            "set_temperature",
            {"entity_id": entity_id, "temperature": 131 if fahrenheit else 55},
            blocking=True,
        )
        assert writes == [{"tempSetpoint": 55}]
        assert hass.states.get(entity_id).attributes["temperature"] == (
            131 if fahrenheit else 55
        )
        await hass.services.async_call(
            "water_heater",
            "set_operation_mode",
            {"entity_id": entity_id, "operation_mode": "hybrid"},
            blocking=True,
        )
        assert writes[-1] == {"powerEnabled": True, "opMode": "hybrid"}
        assert hass.states.get(entity_id).state == "hybrid"
        connectivity = registry.async_get_entity_id(
            "binary_sensor",
            DOMAIN,
            heater.unique_id.rsplit(":", 1)[0] + ":connectivity",
        )
        assert registry.async_get(connectivity).device_id == registered.device_id
        registry.async_update_entity(entity_id, name="Mein Warmwasser")
        remembered = (registered.id, registered.device_id)
        disabled = registry.async_get_entity_id(
            "binary_sensor",
            DOMAIN,
            heater.unique_id.rsplit(":", 1)[0] + ":dhw_vacation",
        )
        registry.async_update_entity(
            disabled, disabled_by=er.RegistryEntryDisabler.USER
        )
        assert await hass.config_entries.async_unload(config.entry_id)
        await hass.async_block_till_done()
        assert not account._listeners
        # Simulate the same identity and fresh streams after a new setup.
        account.connected = True
        account.generation += 1
        emit(account, "topics/info", {**data["info"], "timestamp": 3})
        emit(account, "topics/meta", data["metadata"], retained=True)
        emit(
            account,
            "topics/sensor",
            {"items": [{**data["sensor"]["items"][0], "timestamp": 3}]},
        )
        emit(account, "shadows/state/get/accepted", data["shadow"])
        assert await hass.config_entries.async_setup(config.entry_id)
        await hass.async_block_till_done()
        after = registry.async_get(entity_id)
        assert (after.id, after.device_id) == remembered
        assert after.name == "Mein Warmwasser"
        assert registry.async_get(disabled).disabled_by == er.RegistryEntryDisabler.USER
        assert hass.states.get(disabled) is None
        assert dr.async_get(hass).async_get(after.device_id) is not None
        assert await hass.config_entries.async_unload(config.entry_id)
        await hass.async_block_till_done()
        assert not account._listeners
