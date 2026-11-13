"""Native HA dehumidifier services, translated states and registry continuity."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations

from custom_components.bosch_buderus_heating.const import DOMAIN
from tests.test_bacon_dhw import responding
from tests.test_bacon_dry import configured
from tests.test_bacon_ha import entry
from tests.test_bacon_session import emit

ROOT = "custom_components.bosch_buderus_heating"


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.usefixtures("legacy_platform_runtime")
async def test_native_dehumidifier_services_translations_and_reload(hass, language):
    hass.config.language = language
    config = entry(hass)
    account, unit, data = configured()
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
        identity = unit.unique_id.rsplit(":", 1)[0]

        def entity_id(domain, key):
            found = registry.async_get_entity_id(
                domain, DOMAIN, identity + ":dry_" + key
            )
            assert found is not None
            return found

        unit_id = entity_id("humidifier", "dehumidifier")
        registered = registry.async_get(unit_id)
        state = hass.states.get(unit_id)
        assert state.state == "on"
        assert state.attributes["device_class"] == "dehumidifier"
        assert state.attributes["current_humidity"] == 60
        assert state.attributes["humidity"] == 50
        assert (
            state.attributes["min_humidity"] == 30
            and state.attributes["max_humidity"] == 80
        )
        assert state.attributes["target_humidity_step"] == 10
        assert state.attributes["action"] == "drying"
        for domain, keys in (
            ("sensor", ("humidity", "temperature", "operating_status")),
            (
                "binary_sensor",
                (
                    "water_tank_full",
                    "defrosting",
                    "idle",
                    "condensate_pump",
                    "device_error",
                ),
            ),
            ("switch", ("child_lock", "display")),
        ):
            for key in keys:
                item = entity_id(domain, key)
                assert hass.states.get(item).state not in ("unknown", "unavailable")
                assert registry.async_get(item).device_id == registered.device_id
        for locale in ("de", "en"):
            texts = await async_get_translations(hass, locale, "entity", {DOMAIN})
            prefix = "component." + DOMAIN + ".entity."
            assert texts[prefix + "switch.bacon_dry_display.name"] == "Display"
            assert texts[prefix + "humidifier.bacon_dry_dehumidifier.name"] == (
                "Luftentfeuchter" if locale == "de" else "Dehumidifier"
            )
            assert texts[
                prefix + "humidifier.bacon_dry_dehumidifier."
                "state_attributes.mode.state.laundry"
            ] == ("Wäschetrocknung" if locale == "de" else "Laundry drying")
            assert texts[
                prefix + "sensor.bacon_dry_operating_status.state.water_tank_full"
            ] == ("Wasserbehälter voll" if locale == "de" else "Water tank full")
        commands = [
            ("humidifier", "dehumidifier", "turn_off", {}, {"powerEnabled": False}),
            ("humidifier", "dehumidifier", "turn_on", {}, {"powerEnabled": True}),
            (
                "humidifier",
                "dehumidifier",
                "set_humidity",
                {"humidity": 60},
                {"humiditySetpoint": 60},
            ),
            (
                "humidifier",
                "dehumidifier",
                "set_mode",
                {"mode": "night"},
                {"opMode": "night"},
            ),
            ("switch", "child_lock", "turn_on", {}, {"childLockEnabled": True}),
            ("switch", "child_lock", "turn_off", {}, {"childLockEnabled": False}),
            ("switch", "display", "turn_off", {}, {"displayEnabled": False}),
            ("switch", "display", "turn_on", {}, {"displayEnabled": True}),
        ]
        for domain, key, service, params, expected in commands:
            await hass.services.async_call(
                domain,
                service,
                {"entity_id": entity_id(domain, key), **params},
                blocking=True,
            )
            assert writes[-1] == expected
        for service, params in (
            ("set_humidity", {"humidity": 55}),
            ("set_mode", {"mode": "unknown"}),
        ):
            with pytest.raises(HomeAssistantError):
                await hass.services.async_call(
                    "humidifier",
                    service,
                    {"entity_id": unit_id, **params},
                    blocking=True,
                )
        assert len(writes) == len(commands)
        await hass.services.async_call(
            "humidifier", "turn_off", {"device_id": registered.device_id}, blocking=True
        )
        assert (
            writes[-1] == {"powerEnabled": False}
            and hass.states.get(unit_id).state == "off"
        )
        assert not [
            item
            for item in registry.entities.values()
            if item.platform == DOMAIN
            and item.domain in ("fan", "water_heater", "climate", "button", "select")
        ]
        # A partial measurement preserves the independently reported idle flag.
        emit(account, "topics/sensor", {"items": [{"timestamp": 3, "relHumidity": 45}]})
        await hass.async_block_till_done()
        assert hass.states.get(entity_id("binary_sensor", "idle")).state == "off"
        registry.async_update_entity(unit_id, name="Mein Entfeuchter")
        disabled = entity_id("sensor", "temperature")
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
            {"items": [{**data["sensor"]["items"][0], "timestamp": 4}]},
        )
        emit(account, "shadows/state/get/accepted", data["shadow"])
        assert await hass.config_entries.async_setup(config.entry_id)
        await hass.async_block_till_done()
        after = registry.async_get(unit_id)
        assert (
            after.id,
            after.device_id,
        ) == remembered and after.name == "Mein Entfeuchter"
        assert registry.async_get(disabled).disabled_by == er.RegistryEntryDisabler.USER
        assert await hass.config_entries.async_unload(config.entry_id)
        await hass.async_block_till_done()
        assert not account._listeners


def test_dry_translation_keys_and_states_match_in_both_languages():
    root = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"
    texts = [
        json.loads((root / path).read_text())
        for path in ("strings.json", "translations/en.json", "translations/de.json")
    ]

    def leaves(value, prefix=""):
        return {
            prefix + "." + key
            for key, item in value.items()
            if not isinstance(item, dict)
        } | set().union(
            *(
                leaves(item, prefix + "." + key)
                for key, item in value.items()
                if isinstance(item, dict)
            )
        )

    reference = texts[0]["entity"]
    count = 0
    for domain, entities in reference.items():
        for key, item in entities.items():
            if key.startswith("bacon_dry_"):
                count += 1
                assert item == texts[1]["entity"][domain][key]
                assert leaves(item) == leaves(texts[2]["entity"][domain][key])
    assert count == 11
