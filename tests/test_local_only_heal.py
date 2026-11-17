"""Reviewed local-only entities take their reading back from the cloud.

An earlier candidate removed the local-only entry of the compressor speed
(and added a cloud entry of its own for the path) and handed the refrigerant
circuit status over to the cloud sensor. At the next setup the local-only
entities come back with their entity IDs and settings, and the cloud entries
the integration created disabled are removed. A cloud entry in use is never
removed: it continues as the local-only entity under its entity ID, whichever
was set up first. Generic local-only readings keep the hand-over, and
nothing else in the registry is touched.
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.bosch_buderus_heating import (
    _clear_entry_issues,
    local_only_heal,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_only_heal import (
    cloud_unique_id,
    heal_registered,
)
from custom_components.bosch_buderus_heating.local_only_sensor import (
    BoschBuderusLocalOnlySensor,
    local_only_unique_id,
)
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from tests.test_local_backed import GATEWAY
from tests.test_local_only import by_path, coordinator_with, number, text, track
from tests.test_local_only_upgrade import (
    CLOUD_SPEED_ID,
    CLOUD_STATES,
    CLOUD_STATUS_ID,
    LOCAL_STATES,
    SPEED,
    SPEED_ID,
    STATUS,
    STATUS_ID,
    RefusedRefrigerantBackend,
    local_ready,
    publish_local,
)
from tests.test_managed_platforms import native
from tests.test_managed_pointt import eventually, running, source_for
from tests.test_source_isolation import paired_target
from tests.test_validity import MockConfigEntry

UNIT_GATEWAY = "gateway-one"
# The separator of grouped entity names.
DASH = chr(0x2013)
# Offered by the local plant view without a reviewed definition.
GENERIC = "/heatingCircuits/hc1/mixerPosition"
MANAGED = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")


def speed_resource(value: int = 38) -> Resource:
    return Resource(
        SPEED,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(resource_type="integerValue", unit="%"),
    )


def status_resource() -> Resource:
    return Resource(
        STATUS,
        value="heating",
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue", allowed_values=tuple(CLOUD_STATES)
        ),
    )


def removed_local_speed(registry, entry, gateway) -> str:
    """The compressor speed as 0.9.1 registered it (enabled by the user,
    renamed), then removed by the faulty hand-over."""
    item = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id(gateway, SPEED),
        config_entry=entry,
        suggested_object_id="heat_pump_compressor_speed",
        original_name="Heat pump Compressor speed",
        capabilities={"state_class": "measurement"},
        unit_of_measurement="%",
        disabled_by=None,
    )
    registry.async_update_entity(item.entity_id, name="Verdichter")
    registry.async_update_entity_options(item.entity_id, DOMAIN, {"native_unit": "%"})
    registry.async_remove(item.entity_id)
    return item.entity_id


def cloud_speed(registry, entry, gateway) -> str:
    """The cloud entry the faulty candidate added for the path instead."""
    return registry.async_get_or_create(
        "sensor",
        DOMAIN,
        cloud_unique_id(gateway, SPEED),
        config_entry=entry,
        suggested_object_id="heat_pump_compressor_actual_speed",
        original_name="Heat pump Compressor Actual Speed",
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        entity_category=EntityCategory.DIAGNOSTIC,
    ).entity_id


def handed_over_status(registry, entry, gateway) -> str:
    """The status as 0.9.1 registered it, then handed over to the cloud
    sensor, which wrote its own name, category and fewer states."""
    entity_id = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id(gateway, STATUS),
        config_entry=entry,
        suggested_object_id="heat_pump_refrigerant_circuit_status",
        original_name="Heat pump Refrigerant circuit status",
        original_device_class="enum",
        translation_key="refrigerant_status",
        capabilities={"options": LOCAL_STATES},
    ).entity_id
    registry.async_update_entity(entity_id, icon="mdi:heat-pump")
    registry.async_update_entity(
        entity_id, new_unique_id=cloud_unique_id(gateway, STATUS)
    )
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        cloud_unique_id(gateway, STATUS),
        config_entry=entry,
        original_name="Heat pump - Status",
        translation_key=None,
        entity_category=EntityCategory.DIAGNOSTIC,
        capabilities={"options": CLOUD_STATES},
    )
    return entity_id


def counts(**values: int) -> dict[str, int]:
    return {
        "restored": 0,
        "taken_back": 0,
        "continued": 0,
        "cloud_entries_removed": 0,
        "cloud_entries_kept": 0,
        **values,
    }


def notice(hass, coordinator, kind: str) -> dict[str, str] | None:
    """The placeholders of a repair notice of the heal, if there is one."""
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN,
        local_only_heal.issue_id(
            f"local_only_entities_{kind}",
            coordinator.config_entry.entry_id,
            coordinator.gateway.gateway_id,
        ),
    )
    return None if issue is None else dict(issue.translation_placeholders or {})


def active_cloud_speed(registry, entry, gateway, entity_id: str | None) -> str:
    """A cloud entity the user has used for weeks: enabled, with statistics,
    and its entity ID changed by the user (Home Assistant keeps no record)."""
    item = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        cloud_unique_id(gateway, SPEED),
        config_entry=entry,
        suggested_object_id="heat_pump_compressor_actual_speed",
        original_name="Heat pump Compressor Actual Speed",
        capabilities={"state_class": "measurement"},
        unit_of_measurement="%",
        disabled_by=None,
    )
    if entity_id is None:
        return item.entity_id
    return registry.async_update_entity(
        item.entity_id, new_entity_id=entity_id
    ).entity_id


def registry_items(registry) -> dict[tuple[str, str], object]:
    return {
        (item.entity_id, item.unique_id): (
            item.disabled_by,
            item.name,
            item.icon,
            item.capabilities,
            item.previous_unique_id,
        )
        for item in registry.entities.values()
    }


# ------------------------------------------------- the user's registry shape


@MANAGED
@pytest.mark.parametrize(
    ("known_at_start", "local_values"), [(True, False), (False, True)]
)
async def test_the_entities_an_earlier_candidate_gave_away_come_back(
    hass, caplog, known_at_start, local_values
) -> None:
    hass.config.components.add(DOMAIN)
    registry = er.async_get(hass)
    old: dict[str, str] = {}

    def configure(data, options):
        target, _ = paired_target(data, options)
        options["targets"][target]["local_values"] = local_values
        options["bindings"]["local"]["enabled"] = True
        (entry,) = hass.config_entries.async_entries(DOMAIN)
        old["speed"] = removed_local_speed(registry, entry, GATEWAY)
        old["cloud_speed"] = cloud_speed(registry, entry, GATEWAY)
        old["status"] = handed_over_status(registry, entry, GATEWAY)

    def probed() -> bool:
        coordinator = source_for(owner, GATEWAY)
        return (
            coordinator is not None
            and SPEED in coordinator.resources
            and STATUS in coordinator.resources
        )

    caplog.set_level(logging.INFO, logger=local_only_heal.__name__)
    backend = RefusedRefrigerantBackend()
    async with running(hass, backend, configure=configure) as (_, _, owner, _):
        if known_at_start:
            # The stored discovery already offers both paths at setup.
            await eventually(probed)
            publish_local(owner, 41, "defrost")
        await owner.start_platforms()
        if not known_at_start:
            await local_ready(owner)
            publish_local(owner, 41, "defrost")
            await eventually(probed)
        await eventually(lambda: native(owner, "sensor", SPEED_ID) is not None)
        await eventually(lambda: native(owner, "sensor", STATUS_ID) is not None)
        coordinator = source_for(owner, GATEWAY)
        speed = native(owner, "sensor", SPEED_ID)
        status = native(owner, "sensor", STATUS_ID)
        assert isinstance(speed, BoschBuderusLocalOnlySensor)
        assert isinstance(status, BoschBuderusLocalOnlySensor)
        # (a) Restored by Home Assistant with its entity ID and settings.
        restored = registry.async_get(speed.entity_id)
        assert restored.entity_id == old["speed"]
        assert restored.unique_id == SPEED_ID
        assert restored.disabled_by is None
        assert restored.name == "Verdichter"
        assert restored.capabilities == {"state_class": "measurement"}
        assert registry.async_get(old["cloud_speed"]) is None
        # (b) The same entry again: entity ID and settings stay; name,
        # category and all 13 states are the local-only entity's again.
        taken = registry.async_get(status.entity_id)
        assert taken.entity_id == old["status"]
        assert taken.unique_id == STATUS_ID
        assert taken.icon == "mdi:heat-pump"
        assert taken.translation_key == "refrigerant_status"
        assert taken.entity_category is None
        assert taken.capabilities == {"options": LOCAL_STATES}
        assert len(LOCAL_STATES) == 13
        # No cloud entry or cloud sensor of these paths is left.
        for unique_id in (CLOUD_SPEED_ID, CLOUD_STATUS_ID):
            assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) is None
        expected = "local" if local_values else "cloud"
        await eventually(
            lambda: (
                (state := hass.states.get(old["speed"])) is not None
                and state.state == ("41" if local_values else str(backend.speed))
            )
        )
        assert hass.states.get(old["speed"]).attributes["source"] == expected
        assert hass.states.get(old["speed"]).attributes["unit_of_measurement"] == "%"
        status_state = hass.states.get(old["status"])
        assert set(LOCAL_STATES) <= set(status_state.attributes["options"])
        assert status_state.attributes["source"] == expected
        assert local_only_heal.diagnostics(coordinator) == counts(
            restored=1, taken_back=1, cloud_entries_removed=1
        )
        # The status entity in use is named in a notice.
        assert notice(hass, coordinator, "continued") == {
            "count": "1",
            "entities": old["status"],
        }
        healed = [
            record
            for record in caplog.records
            if record.name == local_only_heal.__name__
            and record.levelno == logging.INFO
        ]
        assert len(healed) == 2
        # Repeating it and further updates change nothing.
        before = registry_items(registry)
        heal_registered(coordinator)
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert registry_items(registry) == before
        assert local_only_heal.diagnostics(coordinator)["restored"] == 1


# ------------------------------------------------------------- unit level


async def test_a_removed_entry_comes_back_and_its_cloud_entry_goes(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(
        hass, [number(SPEED, 41, unit="%", kind="integerValue")], speed_resource()
    )
    registry = er.async_get(hass)
    entry = coordinator.config_entry
    removed_local_speed(registry, entry, UNIT_GATEWAY)
    cloud = cloud_speed(registry, entry, UNIT_GATEWAY)
    entities = by_path(track(coordinator))
    # The local-only entity is added (Home Assistant restores the entry).
    assert set(entities) == {SPEED}
    assert entities[SPEED].unique_id == local_only_unique_id(UNIT_GATEWAY, SPEED)
    assert registry.async_get(cloud) is None
    from custom_components.bosch_buderus_heating.diagnostics import (
        _gateway_diagnostics,
    )

    assert _gateway_diagnostics(1, coordinator)["local_only_healed"] == counts(
        restored=1, cloud_entries_removed=1
    )
    # Nothing in use changed: no notice.
    assert notice(hass, coordinator, "continued") is None
    await coordinator.async_shutdown()


async def test_a_removed_entry_comes_back_before_its_first_local_reading(
    hass: HomeAssistant,
) -> None:
    """Its form is fixed: added at once, it shows the cloud reading until the
    local one arrives."""
    coordinator = coordinator_with(hass, [], speed_resource())
    registry = er.async_get(hass)
    removed_local_speed(registry, coordinator.config_entry, UNIT_GATEWAY)
    cloud_speed(registry, coordinator.config_entry, UNIT_GATEWAY)
    assert set(by_path(track(coordinator))) == {SPEED}
    await coordinator.async_shutdown()


@pytest.mark.parametrize("customised", [True, False])
async def test_a_handed_over_entry_gets_its_local_unique_id_back(
    hass: HomeAssistant, customised
) -> None:
    coordinator = coordinator_with(
        hass, [text(STATUS, "heating", LOCAL_STATES[:9])], status_resource()
    )
    registry = er.async_get(hass)
    entity_id = handed_over_status(registry, coordinator.config_entry, UNIT_GATEWAY)
    if not customised:
        registry.async_update_entity(entity_id, icon=None)
    entities = by_path(track(coordinator))
    assert set(entities) == {STATUS}
    item = registry.async_get(entity_id)
    assert item.unique_id == local_only_unique_id(UNIT_GATEWAY, STATUS)
    assert item.icon == ("mdi:heat-pump" if customised else None)
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, cloud_unique_id(UNIT_GATEWAY, STATUS)
        )
        is None
    )
    assert local_only_heal.diagnostics(coordinator) == counts(taken_back=1)
    await coordinator.async_shutdown()


async def test_a_registered_entity_loses_a_leftover_cloud_entry(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(
        hass, [number(SPEED, 41, unit="%", kind="integerValue")], speed_resource()
    )
    registry = er.async_get(hass)
    local = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id(UNIT_GATEWAY, SPEED),
        config_entry=coordinator.config_entry,
    ).entity_id
    cloud = cloud_speed(registry, coordinator.config_entry, UNIT_GATEWAY)
    assert set(by_path(track(coordinator))) == {SPEED}
    assert registry.async_get(local) is not None
    assert registry.async_get(cloud) is None
    assert local_only_heal.diagnostics(coordinator) == counts(cloud_entries_removed=1)
    await coordinator.async_shutdown()


@pytest.mark.parametrize("in_use", [True, False])
async def test_a_handed_over_duplicate_of_a_registered_entity(
    hass: HomeAssistant, in_use
) -> None:
    """Next to a registered local-only entity only a duplicate the
    integration created disabled is removed; one in use stays, and a notice
    names it with the entity that shows the reading now."""
    coordinator = coordinator_with(hass, [], status_resource())
    registry = er.async_get(hass)
    handed = handed_over_status(registry, coordinator.config_entry, UNIT_GATEWAY)
    registry.async_update_entity(
        handed,
        icon=None,  # not customised
        disabled_by=None if in_use else er.RegistryEntryDisabler.INTEGRATION,
    )
    local = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id(UNIT_GATEWAY, STATUS),
        config_entry=coordinator.config_entry,
    ).entity_id
    heal_registered(coordinator)
    heal_registered(coordinator)  # the second setup call changes nothing
    assert (registry.async_get(handed) is not None) is in_use
    assert registry.async_get(local).unique_id == local_only_unique_id(
        UNIT_GATEWAY, STATUS
    )
    if in_use:
        assert registry.async_get(handed).unique_id == cloud_unique_id(
            UNIT_GATEWAY, STATUS
        )
        assert local_only_heal.diagnostics(coordinator) == counts(cloud_entries_kept=1)
        assert notice(hass, coordinator, "kept") == {
            "count": "1",
            "entities": f"{handed} → {local}",
        }
    else:
        assert local_only_heal.diagnostics(coordinator) == counts(
            cloud_entries_removed=1
        )
        assert notice(hass, coordinator, "kept") is None
    await coordinator.async_shutdown()


async def test_the_090_1_shape_is_left_as_it_is(hass: HomeAssistant) -> None:
    """(c) Registered local-only entities and no cloud entries: nothing to heal."""
    coordinator = coordinator_with(
        hass,
        [number(SPEED, 41, unit="%", kind="integerValue")],
        speed_resource(),
        status_resource(),
    )
    registry = er.async_get(hass)
    for path in (SPEED, STATUS):
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            local_only_unique_id(UNIT_GATEWAY, path),
            config_entry=coordinator.config_entry,
        )
    before = registry_items(registry)
    heal_registered(coordinator)
    assert registry_items(registry) == before
    assert set(local_only_heal.diagnostics(coordinator).values()) == {0}
    await coordinator.async_shutdown()


async def test_a_disabled_cloud_entry_that_was_never_local_gives_way(
    hass: HomeAssistant,
) -> None:
    """Not in use: removed, and the local-only entity is added as new, the
    same as with the local access set up first."""
    coordinator = coordinator_with(
        hass, [number(SPEED, 41, unit="%", kind="integerValue")], speed_resource()
    )
    registry = er.async_get(hass)
    cloud = cloud_speed(registry, coordinator.config_entry, UNIT_GATEWAY)
    entities = by_path(track(coordinator))
    assert set(entities) == {SPEED}
    assert entities[SPEED].unique_id == local_only_unique_id(UNIT_GATEWAY, SPEED)
    assert registry.async_get(cloud) is None
    assert local_only_heal.diagnostics(coordinator) == counts(cloud_entries_removed=1)
    await coordinator.async_shutdown()


async def test_a_cloud_entity_in_use_waits_for_an_enabled_local_access(
    hass: HomeAssistant,
) -> None:
    """Without any local-only entry and with the local access switched off,
    the cloud entity stays the entity of the reading."""
    coordinator = coordinator_with(hass, [], speed_resource(), binding_enabled=False)
    registry = er.async_get(hass)
    cloud = active_cloud_speed(registry, coordinator.config_entry, UNIT_GATEWAY, None)
    before = registry_items(registry)
    assert set(by_path(track(coordinator))) == set()
    assert registry_items(registry) == before
    assert registry.async_get(cloud).unique_id == cloud_unique_id(UNIT_GATEWAY, SPEED)
    await coordinator.async_shutdown()


async def test_generic_readings_keep_the_hand_over(hass: HomeAssistant) -> None:
    """(d) A reading without a reviewed definition stays with the cloud
    sensor it was handed over to (8C-c)."""
    coordinator = coordinator_with(
        hass, [number(GENERIC, 40, unit="%", kind="integerValue")]
    )
    registry = er.async_get(hass)
    entity_id = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id(UNIT_GATEWAY, GENERIC),
        config_entry=coordinator.config_entry,
    ).entity_id
    registry.async_update_entity(
        entity_id, new_unique_id=cloud_unique_id(UNIT_GATEWAY, GENERIC)
    )
    # And one whose local-only entry was removed, with a cloud entry.
    other = "/heatingCircuits/hc2/mixerPosition"
    removed = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id(UNIT_GATEWAY, other),
        config_entry=coordinator.config_entry,
    ).entity_id
    registry.async_remove(removed)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        cloud_unique_id(UNIT_GATEWAY, other),
        config_entry=coordinator.config_entry,
    )
    before = registry_items(registry)
    heal_registered(coordinator)
    assert registry_items(registry) == before
    await coordinator.async_shutdown()


async def test_nothing_is_healed_without_a_configured_local_access(
    hass: HomeAssistant,
) -> None:
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        keeps_local_entity,
    )

    coordinator = coordinator_with(hass, [], speed_resource(), target="other")
    registry = er.async_get(hass)
    handed_over_status(registry, coordinator.config_entry, UNIT_GATEWAY)
    removed_local_speed(registry, coordinator.config_entry, UNIT_GATEWAY)
    cloud_speed(registry, coordinator.config_entry, UNIT_GATEWAY)
    before = registry_items(registry)
    heal_registered(coordinator)
    assert registry_items(registry) == before
    assert not keeps_local_entity(coordinator, SPEED)
    await coordinator.async_shutdown()


async def test_entries_of_others_and_customised_ones_stay(
    hass: HomeAssistant,
) -> None:
    """Only the entry's own cloud entries of reviewed paths are removed; a
    customised one stays, and so does another integration's entity holding
    the old entity ID (the restored entity gets another one)."""
    coordinator = coordinator_with(
        hass, [number(SPEED, 41, unit="%", kind="integerValue")], speed_resource()
    )
    registry = er.async_get(hass)
    entry = coordinator.config_entry
    old = removed_local_speed(registry, entry, UNIT_GATEWAY)
    foreign = registry.async_get_or_create(
        "sensor", "other_integration", "x", suggested_object_id=old.split(".")[1]
    )
    assert foreign.entity_id == old
    cloud = cloud_speed(registry, entry, UNIT_GATEWAY)
    registry.async_update_entity(cloud, name="Mine")
    elsewhere = MockConfigEntry(domain=DOMAIN)
    elsewhere.add_to_hass(hass)
    other_entry = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        cloud_unique_id("gateway-two", SPEED),
        config_entry=elsewhere,
    ).entity_id
    # Another integration's entry, even if it names this config entry.
    alien = registry.async_get_or_create(
        "sensor",
        "other_integration",
        cloud_unique_id(UNIT_GATEWAY, SPEED),
        config_entry=entry,
    ).entity_id
    # Entries of another platform, or without this gateway's prefix.
    binary = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        cloud_unique_id(UNIT_GATEWAY, SPEED),
        config_entry=entry,
    ).entity_id
    unprefixed = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        SPEED.strip("/").replace("/", ":"),
        config_entry=entry,
    ).entity_id
    heal_registered(coordinator)
    # Customised: in use, so it continues as the local-only entity.
    assert registry.async_get(cloud).name == "Mine"
    assert registry.async_get(cloud).unique_id == local_only_unique_id(
        UNIT_GATEWAY, SPEED
    )
    for kept in (alien, binary, unprefixed):
        assert registry.async_get(kept) is not None
    assert registry.async_get(old).platform == "other_integration"
    assert registry.async_get(other_entry) is not None
    assert local_only_heal.diagnostics(coordinator) == counts(continued=1)
    await coordinator.async_shutdown()


async def test_a_removed_entry_of_another_config_entry_is_not_restored(
    hass: HomeAssistant,
) -> None:
    """It is no local-only entry of this entry: with the local access
    switched off the reading counts as new to the entry and waits."""
    coordinator = coordinator_with(hass, [], speed_resource(), binding_enabled=False)
    registry = er.async_get(hass)
    elsewhere = MockConfigEntry(domain=DOMAIN)
    elsewhere.add_to_hass(hass)
    removed_local_speed(registry, elsewhere, UNIT_GATEWAY)
    cloud = cloud_speed(registry, coordinator.config_entry, UNIT_GATEWAY)
    heal_registered(coordinator)
    assert registry.async_get(cloud) is not None
    assert set(local_only_heal.diagnostics(coordinator).values()) == {0}
    assert (
        "sensor",
        DOMAIN,
        local_only_unique_id(UNIT_GATEWAY, SPEED),
    ) in registry.deleted_entities
    await coordinator.async_shutdown()


async def test_a_new_reviewed_reading_waits_for_an_enabled_local_access(
    hass: HomeAssistant,
) -> None:
    """With the local access switched off, a reading new to the entry keeps
    the cloud sensor (a removed entry still comes back, see above)."""
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        keeps_local_entity,
    )

    coordinator = coordinator_with(hass, [], speed_resource(), binding_enabled=False)
    assert not keeps_local_entity(coordinator, SPEED)
    registry = er.async_get(hass)
    removed_local_speed(registry, coordinator.config_entry, UNIT_GATEWAY)
    assert keeps_local_entity(coordinator, SPEED)
    # Switched on: a new reading of a fixed form is taken before its first
    # local reading.
    enabled = coordinator_with(hass, [], status_resource())
    assert keeps_local_entity(enabled, STATUS)
    assert set(by_path(track(enabled))) == {STATUS}
    await coordinator.async_shutdown()
    await enabled.async_shutdown()


async def test_a_new_reading_without_a_fixed_form_waits_for_its_local_reading(
    hass: HomeAssistant,
) -> None:
    from custom_components.bosch_buderus_heating.local_only_pair import (
        fixed_definition,
        reviewed_definition,
    )
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        keeps_local_entity,
    )

    brine = "/heatSources/hs1/brineCircuit/pumpSpeed"
    assert reviewed_definition(brine) is not None
    assert fixed_definition(brine) is None
    cloud = Resource(
        brine,
        value=40,
        has_value=True,
        metadata=ResourceMetadata(resource_type="integerValue", unit="%"),
    )
    coordinator = coordinator_with(hass, [], cloud)
    assert not keeps_local_entity(coordinator, brine)
    later = coordinator_with(
        hass, [number(brine, 41, unit="%", kind="integerValue")], cloud
    )
    assert keeps_local_entity(later, brine)
    await coordinator.async_shutdown()
    await later.async_shutdown()


# ------------------------------------------- a cloud entity in use continues


async def test_a_renamed_cloud_entity_in_use_keeps_its_entity_id(
    hass: HomeAssistant,
) -> None:
    """The local-only entry was removed (for example with the local access)
    and the user has worked with the cloud entity since, under an entity ID
    of their own: it is never removed. It continues as the local-only
    entity, so its entity ID, settings and statistics stay; the removed
    entry stays removed, and a notice names the entity."""
    coordinator = coordinator_with(
        hass, [number(SPEED, 41, unit="%", kind="integerValue")], speed_resource()
    )
    registry = er.async_get(hass)
    entry = coordinator.config_entry
    old_local = removed_local_speed(registry, entry, UNIT_GATEWAY)
    mine = active_cloud_speed(
        registry, entry, UNIT_GATEWAY, "sensor.my_compressor_speed"
    )
    entities = by_path(track(coordinator))
    assert set(entities) == {SPEED}
    item = registry.async_get(mine)
    assert item is not None
    assert item.unique_id == local_only_unique_id(UNIT_GATEWAY, SPEED)
    assert item.previous_unique_id == cloud_unique_id(UNIT_GATEWAY, SPEED)
    assert item.disabled_by is None
    assert item.name is None  # not the removed entry's name
    assert entities[SPEED].unique_id == item.unique_id
    # The removed entry stays removed; its entity ID is not taken again.
    assert registry.async_get(old_local) is None
    assert (
        "sensor",
        DOMAIN,
        local_only_unique_id(UNIT_GATEWAY, SPEED),
    ) not in registry.deleted_entities
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, cloud_unique_id(UNIT_GATEWAY, SPEED)
        )
        is None
    )
    assert local_only_heal.diagnostics(coordinator) == counts(continued=1)
    assert notice(hass, coordinator, "continued") == {
        "count": "1",
        "entities": "sensor.my_compressor_speed",
    }
    # Repeating it changes nothing.
    before = registry_items(registry)
    heal_registered(coordinator)
    assert registry_items(registry) == before
    assert local_only_heal.diagnostics(coordinator)["continued"] == 1
    # Removing the entry removes its notice; another entry's stays.
    other = local_only_heal.issue_id(
        "local_only_entities_continued", "other-entry", UNIT_GATEWAY
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        other,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        data={"entry_id": "other-entry"},
        translation_key="local_only_entities_continued",
    )
    _clear_entry_issues(hass, entry.entry_id)
    assert notice(hass, coordinator, "continued") is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, other) is not None
    await coordinator.async_shutdown()


@pytest.mark.parametrize(
    "capabilities", [{"state_class": "total_increasing"}, {"state_class": None}]
)
async def test_a_cloud_entity_with_other_statistics_stays_as_it_is(
    hass: HomeAssistant, capabilities
) -> None:
    """Its long-term statistics would not continue under the local-only
    form: the cloud entity stays the entity of the reading (no state class
    is fine, the local-only entity may add one)."""
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        keeps_local_entity,
    )

    coordinator = coordinator_with(
        hass, [number(SPEED, 41, unit="%", kind="integerValue")], speed_resource()
    )
    registry = er.async_get(hass)
    removed_local_speed(registry, coordinator.config_entry, UNIT_GATEWAY)
    mine = active_cloud_speed(
        registry, coordinator.config_entry, UNIT_GATEWAY, "sensor.my_speed"
    )
    registry.async_update_entity(mine, capabilities=capabilities)
    entities = by_path(track(coordinator))
    continued = capabilities["state_class"] is None
    assert set(entities) == ({SPEED} if continued else set())
    assert registry.async_get(mine).unique_id == (
        local_only_unique_id(UNIT_GATEWAY, SPEED)
        if continued
        else cloud_unique_id(UNIT_GATEWAY, SPEED)
    )
    assert keeps_local_entity(coordinator, SPEED) is continued
    await coordinator.async_shutdown()


async def test_a_cloud_entity_with_another_unit_stays_as_it_is(
    hass: HomeAssistant,
) -> None:
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        keeps_local_entity,
    )

    cloud = Resource(
        SPEED,
        value=38,
        has_value=True,
        metadata=ResourceMetadata(resource_type="integerValue", unit="Hz"),
    )
    coordinator = coordinator_with(
        hass, [number(SPEED, 41, unit="%", kind="integerValue")], cloud
    )
    registry = er.async_get(hass)
    mine = active_cloud_speed(registry, coordinator.config_entry, UNIT_GATEWAY, None)
    assert set(by_path(track(coordinator))) == set()
    assert registry.async_get(mine).unique_id == cloud_unique_id(UNIT_GATEWAY, SPEED)
    assert not keeps_local_entity(coordinator, SPEED)
    await coordinator.async_shutdown()


async def test_a_removed_entry_without_a_fixed_form_waits_for_its_reading(
    hass: HomeAssistant,
) -> None:
    """Its local-only entity cannot be added before a local reading: the
    cloud sensor is not held back (the reading would have no entity)."""
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        keeps_local_entity,
    )

    brine = "/heatSources/hs1/brineCircuit/pumpSpeed"
    cloud = Resource(
        brine,
        value=40,
        has_value=True,
        metadata=ResourceMetadata(resource_type="integerValue", unit="%"),
    )
    coordinator = coordinator_with(hass, [], cloud, binding_enabled=False)
    registry = er.async_get(hass)
    item = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id(UNIT_GATEWAY, brine),
        config_entry=coordinator.config_entry,
    )
    registry.async_remove(item.entity_id)
    assert not keeps_local_entity(coordinator, brine)
    assert set(by_path(track(coordinator))) == set()
    # With its local reading it comes back.
    later = coordinator_with(
        hass, [number(brine, 41, unit="%", kind="integerValue")], cloud
    )
    assert keeps_local_entity(later, brine)
    assert set(by_path(track(later))) == {brine}
    await coordinator.async_shutdown()
    await later.async_shutdown()


async def test_a_handed_over_entry_without_a_reading_waits(
    hass: HomeAssistant,
) -> None:
    """A handed-over entry of a reading without a fixed form is taken back
    only with its local reading; until then the cloud sensor shows it."""
    brine = "/heatSources/hs1/brineCircuit/pumpSpeed"
    cloud = Resource(
        brine,
        value=40,
        has_value=True,
        metadata=ResourceMetadata(resource_type="integerValue", unit="%"),
    )
    coordinator = coordinator_with(hass, [], cloud)
    registry = er.async_get(hass)
    entity_id = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id(UNIT_GATEWAY, brine),
        config_entry=coordinator.config_entry,
    ).entity_id
    registry.async_update_entity(
        entity_id, new_unique_id=cloud_unique_id(UNIT_GATEWAY, brine)
    )
    before = registry_items(registry)
    heal_registered(coordinator)
    assert registry_items(registry) == before
    await coordinator.async_shutdown()


# ------------------------------------- the same result in either setup order


@MANAGED
@pytest.mark.parametrize("first", ["local", "cloud"])
async def test_the_setup_order_gives_the_same_entities(hass, first) -> None:
    """Local access first, or the cloud first (its entities in use) and the
    local access added later: the same unique IDs, names, categories,
    states and enablement; only a cloud entity's entity ID is kept."""
    hass.config.components.add(DOMAIN)
    registry = er.async_get(hass)
    cloud_ids: dict[str, str] = {}

    def configure(data, options):
        target, _ = paired_target(data, options)
        options["targets"][target]["local_values"] = True
        options["bindings"]["local"]["enabled"] = True
        if first == "local":
            return
        (entry,) = hass.config_entries.async_entries(DOMAIN)
        # What a cloud-only start registered for the two readings.
        cloud_ids["speed"] = active_cloud_speed(
            registry, entry, GATEWAY, "sensor.my_compressor_speed"
        )
        cloud_ids["status"] = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            cloud_unique_id(GATEWAY, STATUS),
            config_entry=entry,
            suggested_object_id="heat_pump_status",
            original_name="Heat pump - Status",
            entity_category=EntityCategory.DIAGNOSTIC,
            capabilities={"options": CLOUD_STATES},
        ).entity_id

    def probed() -> bool:
        coordinator = source_for(owner, GATEWAY)
        return (
            coordinator is not None
            and SPEED in coordinator.resources
            and STATUS in coordinator.resources
        )

    backend = RefusedRefrigerantBackend()
    async with running(hass, backend, configure=configure) as (_, _, owner, _):
        await owner.start_platforms()
        await local_ready(owner)
        publish_local(owner, 41, "defrost")
        await eventually(probed)
        # The compressor speed is disabled by default, the status enabled.
        await eventually(lambda: native(owner, "sensor", STATUS_ID) is not None)
        shapes = {}
        for name, unique_id in (("speed", SPEED_ID), ("status", STATUS_ID)):
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            item = registry.async_get(entity_id)
            if first == "cloud":
                assert entity_id == cloud_ids[name]
            shapes[name] = (
                item.original_name,
                item.translation_key,
                item.entity_category,
                item.capabilities,
            )
            # The enablement of an entity in use stays the user's.
            assert item.disabled_by is (
                er.RegistryEntryDisabler.INTEGRATION
                if first == "local" and name == "speed"
                else None
            )
        for unique_id in (CLOUD_SPEED_ID, CLOUD_STATUS_ID):
            assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) is None
        assert shapes == {
            "speed": (
                "Heat generator 1 " + DASH + " Compressor speed",
                None,
                None,
                {"state_class": "measurement"},
            ),
            "status": (
                "Heat generator 1 " + DASH + " Refrigerant circuit status",
                "refrigerant_status",
                None,
                {"options": LOCAL_STATES},
            ),
        }


@MANAGED
async def test_an_automation_on_a_renamed_cloud_entity_keeps_working(
    hass,
) -> None:
    """The user renamed the cloud entity's ID and an automation triggers on
    it; after the local-only entity of the reading took over, the automation
    still sees every new reading under the same entity ID."""
    from homeassistant.setup import async_setup_component

    hass.config.components.add(DOMAIN)
    registry = er.async_get(hass)
    seen: list[str] = []

    def configure(data, options):
        target, _ = paired_target(data, options)
        options["targets"][target]["local_values"] = True
        options["bindings"]["local"]["enabled"] = True
        (entry,) = hass.config_entries.async_entries(DOMAIN)
        removed_local_speed(registry, entry, GATEWAY)
        active_cloud_speed(registry, entry, GATEWAY, "sensor.my_compressor_speed")

    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "triggers": [
                    {"trigger": "state", "entity_id": "sensor.my_compressor_speed"}
                ],
                "actions": [
                    {
                        "event": "speed_seen",
                        "event_data": {"value": "{{ trigger.to_state.state }}"},
                    }
                ],
            }
        },
    )
    hass.bus.async_listen(
        "speed_seen", lambda event: seen.append(str(event.data["value"]))
    )

    def probed() -> bool:
        coordinator = source_for(owner, GATEWAY)
        return coordinator is not None and SPEED in coordinator.resources

    backend = RefusedRefrigerantBackend()
    async with running(hass, backend, configure=configure) as (_, _, owner, _):
        await owner.start_platforms()
        await local_ready(owner)
        publish_local(owner, 41, "defrost")
        await eventually(probed)
        await eventually(lambda: native(owner, "sensor", SPEED_ID) is not None)
        assert native(owner, "sensor", SPEED_ID).entity_id == (
            "sensor.my_compressor_speed"
        )
        await eventually(lambda: "41" in seen)
        await local_ready(owner)
        publish_local(owner, 44, "defrost")
        source_for(owner, GATEWAY).async_update_listeners()
        await eventually(lambda: "44" in seen)
        state = hass.states.get("sensor.my_compressor_speed")
        assert state.attributes["source"] == "local"
        coordinator = source_for(owner, GATEWAY)
        assert local_only_heal.diagnostics(coordinator) == counts(continued=1)


async def test_a_handed_over_entry_with_other_statistics_stays(
    hass: HomeAssistant,
) -> None:
    """The cloud's sensor gave the handed-over entry a state class the
    local-only form does not have: taking it back would drop it (and orphan
    its statistics), so the entry stays the cloud's."""
    coordinator = coordinator_with(
        hass, [text(STATUS, "heating", LOCAL_STATES[:9])], status_resource()
    )
    registry = er.async_get(hass)
    entity_id = handed_over_status(registry, coordinator.config_entry, UNIT_GATEWAY)
    registry.async_update_entity(entity_id, capabilities={"state_class": "measurement"})
    before = registry_items(registry)
    heal_registered(coordinator)
    assert registry_items(registry) == before
    assert registry.async_get(entity_id).unique_id == cloud_unique_id(
        UNIT_GATEWAY, STATUS
    )
    await coordinator.async_shutdown()
