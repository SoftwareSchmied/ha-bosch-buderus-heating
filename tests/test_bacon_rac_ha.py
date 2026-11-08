"""Real Home Assistant climate services, units, translations and registry."""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.bosch_buderus_heating.const import DOMAIN
from tests.test_bacon_dhw import responding
from tests.test_bacon_ha import entry
from tests.test_bacon_rac import configured
from tests.test_bacon_session import emit

ROOT = "custom_components.bosch_buderus_heating"


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("language,fahrenheit", [("de", False), ("en", True)])
@pytest.mark.usefixtures("legacy_platform_runtime")
async def test_climate_services_units_translations_and_registry(
    hass, language, fahrenheit
):
    hass.config.language = language
    if fahrenheit:
        hass.config.units = US_CUSTOMARY_SYSTEM
    config = entry(hass)
    account, climate, data = configured()
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
        entity_id = registry.async_get_entity_id("climate", DOMAIN, climate.unique_id)
        assert entity_id is not None
        registered = registry.async_get(entity_id)
        state = hass.states.get(entity_id)
        assert state.state == "cool"
        assert state.attributes["current_temperature"] == (72.5 if fahrenheit else 22.5)
        assert state.attributes["temperature"] == (71.6 if fahrenheit else 22)
        assert state.attributes["target_temp_step"] == (1.8 if fahrenheit else 1)
        assert state.attributes["min_temp"] == (62.6 if fahrenheit else 17)
        assert "hvac_action" not in state.attributes
        for locale in ("de", "en"):
            texts = await async_get_translations(hass, locale, "entity", {DOMAIN})
            prefix = "component." + DOMAIN + ".entity.climate.bacon_rac"
            assert texts[prefix + ".name"] == (
                "Klimagerät" if locale == "de" else "Air conditioner"
            )
            assert texts[prefix + ".state_attributes.fan_mode.state.quiet"] == (
                "Leise" if locale == "de" else "Quiet"
            )
            for field in ("swing_mode", "swing_horizontal_mode"):
                assert texts[prefix + ".state_attributes." + field + ".state.on"] == (
                    "Ein" if locale == "de" else "On"
                )
        commands = [
            (
                "set_temperature",
                {"temperature": 75.2 if fahrenheit else 24, "hvac_mode": "heat"},
                {"powerEnabled": True, "opMode": "heat", "tempSetpoint": 24},
            ),
            ("set_fan_mode", {"fan_mode": "low"}, {"fanSpeed": "low"}),
            ("set_swing_mode", {"swing_mode": "on"}, {"vSwingEnabled": True}),
            (
                "set_swing_horizontal_mode",
                {"swing_horizontal_mode": "off"},
                {"hSwingEnabled": False},
            ),
            ("turn_off", {}, {"powerEnabled": False}),
            ("turn_on", {}, {"powerEnabled": True}),
        ]
        for service, params, expected in commands:
            await hass.services.async_call(
                "climate", service, {"entity_id": entity_id, **params}, blocking=True
            )
            assert writes[-1] == expected
        assert hass.states.get(entity_id).state == "heat"
        assert hass.states.get(entity_id).attributes["temperature"] == (
            75.2 if fahrenheit else 24
        )
        connectivity = registry.async_get_entity_id(
            "binary_sensor",
            DOMAIN,
            climate.unique_id.rsplit(":", 1)[0] + ":connectivity",
        )
        assert registry.async_get(connectivity).device_id == registered.device_id
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {
                "device_id": registered.device_id,
                "temperature": 77 if fahrenheit else 25,
            },
            blocking=True,
        )
        assert writes[-1] == {"tempSetpoint": 25}
        registry.async_update_entity(entity_id, name="Mein Klimagerät")
        remembered = registered.id, registered.device_id
        assert await hass.config_entries.async_unload(config.entry_id)
        await hass.async_block_till_done()
        assert not account._listeners
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
        assert after.name == "Mein Klimagerät"
        registry.async_update_entity(
            entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )
        await hass.async_block_till_done()
        assert (
            registry.async_get(entity_id).disabled_by == er.RegistryEntryDisabler.USER
        )
        assert await hass.config_entries.async_unload(config.entry_id)
        await hass.async_block_till_done()
        assert not account._listeners


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.usefixtures("legacy_platform_runtime")
@pytest.mark.parametrize("declared", [True, False])
async def test_eco_preset_service_and_translations(hass, declared):
    config = entry(hass)
    account, climate, data = configured()
    if declared:
        data["metadata"]["shadows"]["state"]["ecoEnabled"] = {"ro": False}
        emit(account, "topics/meta", data["metadata"])
    writes = responding(account, data)

    async def wait():
        await asyncio.Event().wait()

    with (
        patch(ROOT + ".BaconAccount", return_value=account),
        patch.object(account, "run", side_effect=wait),
    ):
        assert await hass.config_entries.async_setup(config.entry_id)
        await hass.async_block_till_done()
        entity_id = er.async_get(hass).async_get_entity_id(
            "climate", DOMAIN, climate.unique_id
        )
        state = hass.states.get(entity_id)
        if not declared:
            assert "preset_modes" not in state.attributes
            assert "preset_mode" not in state.attributes
        else:
            assert state.attributes["preset_modes"] == ["none", "eco"]
            assert state.attributes["preset_mode"] == "eco"
            await hass.services.async_call(
                "climate",
                "set_preset_mode",
                {"entity_id": entity_id, "preset_mode": "none"},
                blocking=True,
            )
            assert writes == [{"ecoEnabled": False}]
            state = hass.states.get(entity_id)
            assert state.attributes["preset_mode"] == "none"
            assert state.attributes["fan_mode"] == "auto"
        for locale in ("de", "en"):
            texts = await async_get_translations(hass, locale, "entity", {DOMAIN})
            prefix = "component." + DOMAIN + ".entity.climate.bacon_rac"
            preset = prefix + ".state_attributes.preset_mode.state."
            assert texts[preset + "eco"] == "Eco"
            assert texts[preset + "none"] == ("Keine" if locale == "de" else "None")
        assert await hass.config_entries.async_unload(config.entry_id)
        await hass.async_block_till_done()
