"""Device diagnostics: entity kinds and states, no readings, names or IDs."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.diagnostics import (
    DIAGNOSTICS_SCHEMA_VERSION,
    async_get_device_diagnostics,
)
from tests.ha_test_helpers import MockConfigEntry


async def _setup(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "private-device-id")},
        name="Private device name",
        model="private-model",
        sw_version="private-firmware",
        serial_number="private-serial",
    )
    registry = er.async_get(hass)
    for number, (domain, key, disabled) in enumerate(
        [
            ("sensor", "outdoor_temperature", None),
            ("sensor", "supply_temperature", None),
            ("sensor", "old_value", er.RegistryEntryDisabler.USER),
            ("switch", "Private user name!", None),
        ]
    ):
        registry.async_get_or_create(
            domain,
            DOMAIN,
            f"private-unique-id-{number}",
            config_entry=entry,
            device_id=device.id,
            translation_key=key,
            suggested_object_id=f"private_object_{number}",
            disabled_by=disabled,
        )
    hass.states.async_set("sensor.private_object_0", "21.5")
    hass.states.async_set("sensor.private_object_1", "unavailable")
    hass.states.async_set("switch.private_object_3", "unknown")
    return entry, device


async def test_the_report_counts_entities_and_states(hass: HomeAssistant) -> None:
    entry, device = await _setup(hass)
    report = await async_get_device_diagnostics(hass, entry, device)
    assert report["diagnostics_schema"] == DIAGNOSTICS_SCHEMA_VERSION == 48
    assert report["device"]["entity_count"] == 4
    assert report["device"]["disabled_entity_count"] == 1
    assert report["entities_by_domain"] == {"sensor": 3, "switch": 1}
    assert report["entities_by_state"] == {
        "disabled": 1,
        "unavailable": 1,
        "unknown": 1,
        "value": 1,
    }
    assert report["entities_truncated"] is False
    keys = {item["translation_key"] for item in report["entities"]}
    assert {"outdoor_temperature", "supply_temperature", "old_value"} <= keys


async def test_the_report_has_no_readings_names_or_identifiers(
    hass: HomeAssistant,
) -> None:
    entry, device = await _setup(hass)
    text = json.dumps(await async_get_device_diagnostics(hass, entry, device))
    for private in (
        "21.5",
        "private-",
        "Private",
        "private_object",
        entry.entry_id,
        device.id,
    ):
        assert private not in text
    # A translation key that is not a plain code is not copied.
    assert "null" in text


async def test_entities_of_other_entries_and_devices_are_left_out(
    hass: HomeAssistant,
) -> None:
    entry, device = await _setup(hass)
    other = MockConfigEntry(domain=DOMAIN)
    other.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        "other-entry-entity",
        config_entry=other,
        device_id=device.id,
        translation_key="other_entry_key",
    )
    report = await async_get_device_diagnostics(hass, entry, device)
    assert report["device"]["entity_count"] == 4
    assert "other_entry_key" not in json.dumps(report)


async def test_the_list_is_capped_but_the_counts_cover_everything(
    hass: HomeAssistant, monkeypatch
) -> None:
    from custom_components.bosch_buderus_heating import device_diagnostics

    monkeypatch.setattr(device_diagnostics, "MAX_LISTED_ENTITIES", 2)
    entry, device = await _setup(hass)
    report = await async_get_device_diagnostics(hass, entry, device)
    assert len(report["entities"]) == 2
    assert report["entities_truncated"] is True
    assert report["device"]["entity_count"] == 4
