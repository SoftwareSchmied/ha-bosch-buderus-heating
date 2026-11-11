"""D42: a device the integration no longer provides can be deleted."""

from __future__ import annotations

from homeassistant.const import ATTR_RESTORED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import async_remove_config_entry_device
from custom_components.bosch_buderus_heating.const import DOMAIN
from tests.ha_test_helpers import MockConfigEntry


def _setup(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    other = MockConfigEntry(domain="other_domain", data={})
    other.add_to_hass(hass)
    devices = dr.async_get(hass)
    gateway = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "gw")}
    )
    circuit = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "gw:hc2")},
    )
    circuit = devices.async_update_device(circuit.id, via_device_id=gateway.id)
    return entry, other, gateway, circuit


def _entity(hass, entry, device, key, *, state=None, restored=False, disabled=False):
    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        key,
        config_entry=entry,
        device_id=device.id,
        disabled_by=er.RegistryEntryDisabler.USER if disabled else None,
    )
    if state is not None:
        hass.states.async_set(
            entity.entity_id, state, {ATTR_RESTORED: True} if restored else {}
        )
    return entity


async def test_a_device_with_a_live_entity_is_kept(hass: HomeAssistant) -> None:
    entry, _other, _gateway, circuit = _setup(hass)
    _entity(hass, entry, circuit, "gw:hc2:flow", state=STATE_UNAVAILABLE)
    # Unavailable but provided by a platform: still current.
    assert not await async_remove_config_entry_device(hass, entry, circuit)


async def test_a_device_without_any_registered_entity_can_go(
    hass: HomeAssistant,
) -> None:
    entry, _other, _gateway, circuit = _setup(hass)
    assert await async_remove_config_entry_device(hass, entry, circuit)


async def test_a_disabled_entity_keeps_the_device(hass: HomeAssistant) -> None:
    entry, _other, _gateway, circuit = _setup(hass)
    entity = _entity(hass, entry, circuit, "gw:hc2:disabled", disabled=True)
    # Disabled: no state at all, but its setting must not be lost.
    assert hass.states.get(entity.entity_id) is None
    assert not await async_remove_config_entry_device(hass, entry, circuit)
    er.async_get(hass).async_remove(entity.entity_id)
    assert await async_remove_config_entry_device(hass, entry, circuit)


async def test_a_restored_or_stateless_entity_keeps_the_device(
    hass: HomeAssistant,
) -> None:
    entry, _other, _gateway, circuit = _setup(hass)
    restored = _entity(
        hass, entry, circuit, "gw:hc2:flow", state=STATE_UNAVAILABLE, restored=True
    )
    assert not await async_remove_config_entry_device(hass, entry, circuit)
    er.async_get(hass).async_remove(restored.entity_id)
    stateless = _entity(hass, entry, circuit, "gw:hc2:other")
    assert not await async_remove_config_entry_device(hass, entry, circuit)
    er.async_get(hass).async_remove(stateless.entity_id)
    assert await async_remove_config_entry_device(hass, entry, circuit)


async def test_a_parent_of_a_device_with_entities_is_kept(
    hass: HomeAssistant,
) -> None:
    entry, _other, gateway, circuit = _setup(hass)
    child = _entity(hass, entry, circuit, "gw:hc2:flow", state="21.5")
    assert not await async_remove_config_entry_device(hass, entry, gateway)
    hass.states.async_set(child.entity_id, STATE_UNAVAILABLE, {ATTR_RESTORED: True})
    assert not await async_remove_config_entry_device(hass, entry, gateway)
    er.async_get(hass).async_update_entity(
        child.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    assert not await async_remove_config_entry_device(hass, entry, gateway)
    er.async_get(hass).async_remove(child.entity_id)
    assert await async_remove_config_entry_device(hass, entry, gateway)


async def test_entities_of_another_entry_do_not_keep_the_device(
    hass: HomeAssistant,
) -> None:
    entry, other, _gateway, circuit = _setup(hass)
    entity = er.async_get(hass).async_get_or_create(
        "sensor", "other_domain", "x", config_entry=other, device_id=circuit.id
    )
    hass.states.async_set(entity.entity_id, "1")
    assert await async_remove_config_entry_device(hass, entry, circuit)
