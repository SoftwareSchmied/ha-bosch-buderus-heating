"""Tests for config-entry setup and token persistence."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating import (
    _consolidate_logical_devices,
    _enable_new_default_entities,
    _remove_retired_entities,
    _remove_unselected_gateway_entries,
    async_migrate_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.bosch_buderus_heating.const import (
    CONF_ACCESS_TOKEN,
    CONF_BRAND,
    CONF_GATEWAY_IDS,
    DOMAIN,
)
from custom_components.bosch_buderus_heating.data import (
    tokens_from_data,
    tokens_to_data,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    Brand,
    Gateway,
    RefreshTokenRejected,
    Resource,
    ResourceMetadata,
    ServiceUnavailable,
)


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BRAND: Brand.BUDERUS.value,
            CONF_GATEWAY_IDS: ["gateway-one"],
            **tokens_to_data(AuthTokens("access", "refresh", expires_at=4000.0)),
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_entry_setup_selects_gateways_and_persists_rotated_tokens(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    refreshed = AuthTokens("new-access", "new-refresh", expires_at=9000.0)
    with (
        patch(
            "custom_components.bosch_buderus_heating.PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("gateway-one"), Gateway("not-selected"))),
        ),
        patch(
            "custom_components.bosch_buderus_heating.OAuthClient.refresh",
            AsyncMock(return_value=refreshed),
        ),
        patch(
            "custom_components.bosch_buderus_heating.coordinator."
            "BoschBuderusDataUpdateCoordinator.async_config_entry_first_refresh",
            AsyncMock(),
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(),
        ),
    ):
        assert await async_setup_entry(hass, entry)
        assert [item.gateway_id for item in entry.runtime_data.gateways] == [
            "gateway-one"
        ]
        assert (
            await entry.runtime_data.token_manager.get_access_token(force_refresh=True)
            == "new-access"
        )

    assert entry.data[CONF_ACCESS_TOKEN] == "new-access"
    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
    ):
        assert await async_unload_entry(hass, entry)


async def test_setup_retries_retain_account_rate_limit(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    response = AsyncMock()
    response.status = 429
    response.headers = {"Retry-After": "3600"}
    response.__aenter__.return_value = response
    session = Mock()
    session.request.return_value = response
    with (
        patch(
            "custom_components.bosch_buderus_heating.async_get_clientsession",
            return_value=session,
        ),
        patch(
            "custom_components.bosch_buderus_heating.OAuthClient.refresh",
            AsyncMock(
                return_value=AuthTokens("access", "refresh", expires_at=4_000_000_000)
            ),
        ),
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)
        backoff = entry.runtime_data.client.backoff
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)
        assert entry.runtime_data.client.backoff is backoff
    session.request.assert_called_once()


async def test_entry_setup_maps_authentication_failure(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    with (
        patch(
            "custom_components.bosch_buderus_heating.PointTClient.get_gateways",
            AsyncMock(side_effect=RefreshTokenRejected("rejected")),
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await async_setup_entry(hass, entry)


async def test_entry_setup_rejects_invalid_saved_data(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_GATEWAY_IDS: []})
    entry.add_to_hass(hass)
    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)


async def test_entry_setup_retries_missing_or_unavailable_gateway(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    with (
        patch(
            "custom_components.bosch_buderus_heating.PointTClient.get_gateways",
            AsyncMock(return_value=()),
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, entry)

    with (
        patch(
            "custom_components.bosch_buderus_heating.PointTClient.get_gateways",
            AsyncMock(side_effect=ServiceUnavailable()),
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, entry)


async def test_entry_publishes_runtime_before_first_refresh_failure(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    with (
        patch(
            "custom_components.bosch_buderus_heating.PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("gateway-one"),)),
        ),
        patch(
            "custom_components.bosch_buderus_heating.coordinator."
            "BoschBuderusDataUpdateCoordinator.async_config_entry_first_refresh",
            AsyncMock(side_effect=ConfigEntryNotReady("discovery failed")),
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, entry)

    assert entry.runtime_data.gateways == (Gateway("gateway-one"),)
    assert len(entry.runtime_data.coordinators) == 1


@pytest.mark.parametrize(
    "bad_data",
    [
        {},
        {CONF_ACCESS_TOKEN: "access", "expires_at": True},
        {
            CONF_ACCESS_TOKEN: "access",
            "expires_at": 1.0,
            "refresh_token": 3,
        },
        {
            CONF_ACCESS_TOKEN: "access",
            "expires_at": 1.0,
            "scope": ["openid", 3],
        },
        {
            CONF_ACCESS_TOKEN: "access",
            "expires_at": 1.0,
            "token_type": 3,
        },
    ],
)
def test_token_data_validation(bad_data: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        tokens_from_data(bad_data)


async def test_entry_migration_merges_preview_logical_devices(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    legacy = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "gateway-one", "heating_circuit", "hc1")},
        name="Heizkreis 1 alt",
    )
    replacement = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "gateway-one:heating_circuit:hc1")},
        name="Heizkreis 1",
    )
    entity = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatingCircuits:hc1:status",
        config_entry=entry,
        device_id=legacy.id,
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 3
    assert entity_registry.async_get(entity.entity_id).device_id == replacement.id
    assert device_registry.async_get(legacy.id) is None


async def test_entry_migration_updates_legacy_device_in_place(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    device_registry = dr.async_get(hass)
    legacy = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "gateway-one", "hot_water_circuit", "dhw1")},
        name="Warmwasser",
    )

    assert await async_migrate_entry(hass, entry)

    migrated = device_registry.async_get(legacy.id)
    assert migrated is not None
    assert migrated.identifiers == {(DOMAIN, "gateway-one:hot_water_circuit:dhw1")}


async def test_entry_migration_rejects_unknown_future_version(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=4)
    entry.add_to_hass(hass)

    assert not await async_migrate_entry(hass, entry)


def test_logical_devices_are_consolidated_without_changing_entities(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    logical = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "gateway-one:heating_circuit:hc1")},
        name="Heizkreis 1",
    )
    entity = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatingCircuits:hc1:operationMode",
        config_entry=entry,
        device_id=logical.id,
    )
    coordinator = SimpleNamespace(
        gateway=Gateway("gateway-one", device_type="k40"),
        config_entry=entry,
        resources={},
        data={},
    )

    _consolidate_logical_devices(hass, entry, (coordinator,))

    gateway = device_registry.async_get_device(identifiers={(DOMAIN, "gateway-one")})
    assert gateway is not None
    assert entity_registry.async_get(entity.entity_id).device_id == gateway.id
    assert device_registry.async_get(logical.id) is None


def test_deselected_gateway_registry_entries_are_removed(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    selected = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "gateway-one")},
        name="Selected",
    )
    removed = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "gateway-two")},
        name="Removed",
    )
    selected_entity = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:status",
        config_entry=entry,
        device_id=selected.id,
    )
    removed_entity = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-two:status",
        config_entry=entry,
        device_id=removed.id,
    )
    metrics = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}:request_metrics:total",
        config_entry=entry,
    )
    metrics = entity_registry.async_update_entity(
        metrics.entity_id,
        name="My request counter",
        icon="mdi:counter",
        disabled_by=None,
    )

    _remove_unselected_gateway_entries(hass, entry, frozenset({"gateway-one"}))

    assert entity_registry.async_get(selected_entity.entity_id) is not None
    assert entity_registry.async_get(removed_entity.entity_id) is None
    assert entity_registry.async_get(metrics.entity_id) == metrics
    assert device_registry.async_get(selected.id) is not None
    assert device_registry.async_get(removed.id) is None


def test_observed_vendor_entities_are_retired_from_registry(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    entity_registry = er.async_get(hass)
    observed = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatSources:vendorExtension:detail",
        config_entry=entry,
    )
    verified = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatSources:actualModulation",
        config_entry=entry,
    )
    coordinator = SimpleNamespace(
        gateway=Gateway("gateway-one"),
        resources={
            "/heatSources/vendorExtension": Resource(
                path="/heatSources/vendorExtension", value={"detail": 1}
            ),
            "/heatSources/actualModulation": Resource(
                path="/heatSources/actualModulation", value=20, has_value=True
            ),
        },
    )

    _remove_retired_entities(hass, entry, (coordinator,))

    assert entity_registry.async_get(observed.entity_id) is None
    assert entity_registry.async_get(verified.entity_id) is not None


def test_retired_empty_and_duplicate_entities_are_removed(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    entity_registry = er.async_get(hass)
    duplicate = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatSources:numberOfStarts",
        config_entry=entry,
    )
    empty_program = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatingCircuits:hc1:switchPrograms:A",
        config_entry=entry,
    )
    empty_name = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatingCircuits:hc1:name",
        config_entry=entry,
    )
    direct_electricity = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatSources:emon:totalConsumption:electricity",
        config_entry=entry,
    )
    retained = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:gateway:system_pressure",
        config_entry=entry,
    )
    program_resource = Resource(
        path="/heatingCircuits/hc1/switchPrograms/A",
        metadata=ResourceMetadata(resource_type="switchProgram", writable=True),
    )
    name_resource = Resource(path="/heatingCircuits/hc1/name", has_value=True)
    coordinator = SimpleNamespace(
        gateway=Gateway("gateway-one"),
        resources={
            program_resource.path: program_resource,
            name_resource.path: name_resource,
        },
    )

    _remove_retired_entities(hass, entry, (coordinator,))

    assert entity_registry.async_get(duplicate.entity_id) is None
    assert entity_registry.async_get(empty_program.entity_id) is None
    assert entity_registry.async_get(empty_name.entity_id) is None
    assert entity_registry.async_get(direct_electricity.entity_id) is None
    assert entity_registry.async_get(retained.entity_id) is not None


def test_new_defaults_enable_only_integration_disabled_entities(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    entity_registry = er.async_get(hass)
    promoted = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatSources:hs1:workingTime:total",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    user_disabled = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatSources:hs1:numberOfStarts:total",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    resources = {
        "/heatSources/hs1/workingTime": Resource(
            path="/heatSources/hs1/workingTime",
            values=({"total": 3600},),
        ),
        "/heatSources/hs1/numberOfStarts": Resource(
            path="/heatSources/hs1/numberOfStarts",
            values=({"total": 12},),
        ),
    }
    coordinator = SimpleNamespace(
        gateway=Gateway("gateway-one"),
        resources=resources,
    )

    _enable_new_default_entities(hass, entry, (coordinator,))

    assert entity_registry.async_get(promoted.entity_id).disabled_by is None
    assert (
        entity_registry.async_get(user_disabled.entity_id).disabled_by
        is er.RegistryEntryDisabler.USER
    )
