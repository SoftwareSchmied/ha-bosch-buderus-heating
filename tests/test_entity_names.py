"""Every entity has a name of its own, never only the device name.

An entity with an empty name shows the device name alone (for example
"Buderus MX400"). The integration names every entity itself; an empty name
in the entity registry can only come from the entity settings in Home
Assistant, and such a setting is kept.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from tests.helpers.rrc_cloud import Clock, cloud_patches
from tests.test_entry import _entry
from tests.test_rrc_w0_baseline import PACKAGE, PLANTS

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("plant", sorted(PLANTS))
async def test_every_entity_resolves_to_a_name_of_its_own(
    hass: HomeAssistant, entity_registry_enabled_by_default, plant, language
) -> None:
    from contextlib import nullcontext
    from unittest.mock import patch

    hass.config.language = language
    build, gateway = PLANTS[plant]
    cloud = build()
    entry = _entry(hass)
    faults = (
        patch(f"{PACKAGE}.coordinator.fault_resource_candidates", return_value=())
        if plant == "tc100"
        else nullcontext()
    )
    with (
        patch(f"{PACKAGE}.async_migrate_entry", return_value=True),
        cloud_patches(cloud, Clock(), gateway),
        faults,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        devices = dr.async_get(hass)
        entities = er.async_entries_for_config_entry(registry, entry.entry_id)
        assert entities
        nameless = []
        for item in entities:
            state = hass.states.get(item.entity_id)
            device = devices.async_get(item.device_id) if item.device_id else None
            device_name = device.name if device is not None else None
            friendly = state.attributes.get("friendly_name") if state else None
            if (
                not (item.original_name or "").strip()
                or item.name is not None
                or (friendly is not None and friendly == device_name)
            ):
                nameless.append((item.entity_id, item.original_name, friendly))
        assert nameless == []
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
