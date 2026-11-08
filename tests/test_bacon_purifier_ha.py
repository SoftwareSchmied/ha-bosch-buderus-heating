"""Actual HA fan/number/switch/select/sensor services and registry continuity."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations

from custom_components.bosch_buderus_heating.bacon import purifier
from custom_components.bosch_buderus_heating.const import DOMAIN
from tests.test_bacon_dhw import responding
from tests.test_bacon_ha import entry
from tests.test_bacon_purifier import configured
from tests.test_bacon_session import emit

ROOT = "custom_components.bosch_buderus_heating"


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.usefixtures("legacy_platform_runtime")
async def test_purifier_ha_services_translation_and_registry(hass, language):
    hass.config.language = language
    config = entry(hass)
    account, fan, data = configured()
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
        identity = fan.unique_id.rsplit(":", 1)[0]

        def entity_id(domain, key):
            found = registry.async_get_entity_id(
                domain, DOMAIN, identity + ":purifier_" + key
            )
            assert found is not None
            return found

        fan_id = entity_id("fan", "fan")
        registered = registry.async_get(fan_id)
        state = hass.states.get(fan_id)
        assert state.state == "on"
        assert state.attributes["preset_mode"] == "auto"
        assert (
            "percentage" not in state.attributes
            and "percentage_step" not in state.attributes
        )
        for key in purifier.SENSORS:
            sensor = hass.states.get(entity_id("sensor", key))
            assert sensor.state not in ("unavailable", "unknown")
            assert (
                registry.async_get(entity_id("sensor", key)).device_id
                == registered.device_id
            )
        assert (
            hass.states.get(entity_id("sensor", "filter_life")).attributes[
                "state_class"
            ]
            == "measurement"
        )
        assert (
            "unit_of_measurement"
            not in hass.states.get(entity_id("sensor", "tvoc")).attributes
        )
        assert (
            "unit_of_measurement"
            not in hass.states.get(entity_id("number", "fan_speed")).attributes
        )
        assert hass.states.get(entity_id("sensor", "air_quality")).state == "fair"
        for locale in ("de", "en"):
            texts = await async_get_translations(hass, locale, "entity", {DOMAIN})
            prefix = "component." + DOMAIN + ".entity."
            assert texts[prefix + "fan.bacon_purifier_fan.name"] == (
                "Luftreiniger" if locale == "de" else "Air purifier"
            )
            assert texts[
                prefix
                + "fan.bacon_purifier_fan.state_attributes.preset_mode.state.silent"
            ] == ("Ruhemodus" if locale == "de" else "Silent")
            assert texts[
                prefix + "sensor.bacon_purifier_filter_status.state.error"
            ] == ("Filter wechseln" if locale == "de" else "Replace filter")

        commands = [
            ("fan", "fan", "turn_off", {}, {"powerEnabled": False}),
            (
                "fan",
                "fan",
                "turn_on",
                {"preset_mode": "manual"},
                {"powerEnabled": True, "opMode": "manual"},
            ),
            (
                "fan",
                "fan",
                "set_preset_mode",
                {"preset_mode": "silent"},
                {"opMode": "silent"},
            ),
            ("number", "fan_speed", "set_value", {"value": 3}, {"fanSpeed": 3}),
            ("switch", "child_lock", "turn_on", {}, {"childLockEnabled": True}),
            ("switch", "child_lock", "turn_off", {}, {"childLockEnabled": False}),
            (
                "select",
                "light_color",
                "select_option",
                {"option": "blue"},
                {"lightMode": "blue"},
            ),
            (
                "number",
                "light_intensity",
                "set_value",
                {"value": 60},
                {"lightIntensity": 60},
            ),
        ]
        for domain, key, service, params, expected in commands:
            await hass.services.async_call(
                domain,
                service,
                {"entity_id": entity_id(domain, key), **params},
                blocking=True,
            )
            assert writes[-1] == expected
        before = len(writes)
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "fan", "turn_on", {"entity_id": fan_id, "percentage": 50}, blocking=True
            )
        assert len(writes) == before
        await hass.services.async_call(
            "fan", "turn_off", {"device_id": registered.device_id}, blocking=True
        )
        assert writes[-1] == {"powerEnabled": False}
        assert hass.states.get(fan_id).state == "off"
        assert not [
            item
            for item in registry.entities.values()
            if item.platform == DOMAIN
            and item.domain in ("water_heater", "climate", "button")
        ]
        registry.async_update_entity(fan_id, name="Mein Luftreiniger")
        disabled = entity_id("sensor", "tvoc")
        registry.async_update_entity(
            disabled, disabled_by=er.RegistryEntryDisabler.USER
        )
        await hass.async_block_till_done()
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
        after = registry.async_get(fan_id)
        assert (
            after.id,
            after.device_id,
        ) == remembered and after.name == "Mein Luftreiniger"
        assert registry.async_get(disabled).disabled_by == er.RegistryEntryDisabler.USER
        assert await hass.config_entries.async_unload(config.entry_id)
        await hass.async_block_till_done()
        assert not account._listeners


def test_purifier_translation_keys_and_options_match_exactly():
    folder = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"
    catalogs = [
        json.loads((folder / file).read_text())
        for file in ("strings.json", "translations/en.json", "translations/de.json")
    ]

    def keys(value, prefix=""):
        return (
            {prefix}
            if not isinstance(value, dict)
            else {
                k for key, item in value.items() for k in keys(item, prefix + "." + key)
            }
        )

    sections = []
    for catalog in catalogs:
        sections.append(
            {
                platform: {
                    key: value
                    for key, value in entries.items()
                    if key.startswith("bacon_purifier_")
                }
                for platform, entries in catalog["entity"].items()
                if any(key.startswith("bacon_purifier_") for key in entries)
            }
        )
    assert sections[0] == sections[1]
    assert keys(sections[0]) == keys(sections[2])
    assert len([key for platform in sections[0].values() for key in platform]) == 16
