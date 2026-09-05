"""Bosch/Buderus Heating integration package."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .compatibility import sync_firmware_compatibility_issue
from .const import (
    CONF_GATEWAY_IDS,
    DOMAIN,
    PLATFORMS,
    polling_profile_from_options,
)
from .coordinator import BoschBuderusDataUpdateCoordinator
from .data import tokens_from_data, tokens_to_data
from .device import device_info_for_resource
from .pointt import (
    AuthenticationError,
    AuthTokens,
    OAuthClient,
    PointTClient,
    PointTError,
    TokenManager,
)
from .resource_catalog import (
    CapabilityMaturity,
    capability_maturity,
    configured_device_name,
)
from .runtime import BoschBuderusRuntimeData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

type BoschBuderusConfigEntry = ConfigEntry[BoschBuderusRuntimeData]


async def async_setup_entry(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> bool:
    """Set up a Bosch/Buderus Heating config entry."""
    try:
        tokens = tokens_from_data(entry.data)
        selected_ids = _selected_gateway_ids(entry.data)
    except ValueError as err:
        raise ConfigEntryAuthFailed from err

    session = async_get_clientsession(hass)
    oauth_client = OAuthClient(session)

    async def persist_tokens(updated_tokens: AuthTokens) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, **tokens_to_data(updated_tokens)},
        )

    token_manager = TokenManager(oauth_client, tokens, persist_tokens)
    previous_runtime = getattr(entry, "runtime_data", None)
    client = PointTClient(
        session,
        token_manager,
        backoff=(
            previous_runtime.client.backoff
            if isinstance(previous_runtime, BoschBuderusRuntimeData)
            else None
        ),
    )
    # Keep the account brake even when gateway discovery prevents setup.
    entry.runtime_data = BoschBuderusRuntimeData(client, token_manager, (), ())

    try:
        gateways = await client.get_gateways()
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed from err
    except PointTError as err:
        raise ConfigEntryNotReady from err

    selected_gateways = tuple(
        gateway for gateway in gateways if gateway.gateway_id in selected_ids
    )
    if len(selected_gateways) != len(selected_ids):
        raise ConfigEntryNotReady("A configured gateway is currently unavailable")

    coordinators = tuple(
        BoschBuderusDataUpdateCoordinator(
            hass,
            client,
            gateway,
            entry,
            polling_profile_from_options(entry.options),
        )
        for gateway in selected_gateways
    )
    # Publish the safe, partially initialized runtime before the first refresh.
    # Home Assistant can then produce diagnostics when discovery itself keeps
    # the config entry in setup_retry.
    entry.runtime_data = BoschBuderusRuntimeData(
        client=client,
        token_manager=token_manager,
        gateways=selected_gateways,
        coordinators=coordinators,
    )
    for coordinator in coordinators:
        await coordinator.async_load_fault_state()
        await coordinator.async_config_entry_first_refresh()

    sync_firmware_compatibility_issue(hass, entry, coordinators)

    _remove_unselected_gateway_entries(hass, entry, selected_ids)
    _enable_new_default_entities(hass, entry, coordinators)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _consolidate_logical_devices(hass, entry, coordinators)
    _remove_retired_entities(hass, entry, coordinators)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> bool:
    """Unload a Bosch/Buderus Heating config entry."""
    return bool(await hass.config_entries.async_unload_platforms(entry, PLATFORMS))


async def async_migrate_entry(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> bool:
    """Migrate development-preview logical-device identifiers."""
    if entry.version > 3:
        return False
    if entry.version < 3:
        device_registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        for device in devices:
            for identifier in device.identifiers:
                legacy = cast(tuple[str, ...], identifier)
                if len(legacy) != 4 or legacy[0] != DOMAIN:
                    continue
                replacement = (DOMAIN, f"{legacy[1]}:{legacy[2]}:{legacy[3]}")
                existing = device_registry.async_get_device(identifiers={replacement})
                if existing is not None and existing.id != device.id:
                    for entity in er.async_entries_for_device(
                        entity_registry, device.id
                    ):
                        entity_registry.async_update_entity(
                            entity.entity_id, device_id=existing.id
                        )
                    device_registry.async_remove_device(device.id)
                else:
                    device_registry.async_update_device(
                        device.id, new_identifiers={replacement}
                    )
                break
        hass.config_entries.async_update_entry(entry, version=3)
    return True


def _selected_gateway_ids(data: Any) -> frozenset[str]:
    value = data.get(CONF_GATEWAY_IDS)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError("Config entry gateway selection is invalid")
    return frozenset(value)


def _remove_unselected_gateway_entries(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    selected_ids: frozenset[str],
) -> None:
    """Remove registry remnants for gateways deliberately deselected by the user."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if entity.unique_id.startswith(f"{entry.entry_id}:request_metrics:"):
            continue
        gateway_id = entity.unique_id.split(":", 1)[0]
        if gateway_id not in selected_ids:
            entity_registry.async_remove(entity.entity_id)

    for device in tuple(
        dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    ):
        gateway_ids = {
            identifier[1].split(":", 1)[0]
            for identifier in device.identifiers
            if identifier[0] == DOMAIN
        }
        if gateway_ids and gateway_ids.isdisjoint(selected_ids):
            device_registry.async_remove_device(device.id)


def _consolidate_logical_devices(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    coordinators: tuple[BoschBuderusDataUpdateCoordinator, ...],
) -> None:
    """Move preview entities from logical devices onto their gateway device."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    devices = tuple(dr.async_entries_for_config_entry(device_registry, entry.entry_id))
    for coordinator in coordinators:
        gateway_id = coordinator.gateway.gateway_id
        gateway_device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            **device_info_for_resource(coordinator, "/gateway"),
        )
        prefixes = tuple(
            f"{gateway_id}:{kind}:"
            for kind in ("heating_circuit", "hot_water_circuit", "heat_source")
        )
        for device in devices:
            if device.id == gateway_device.id or not any(
                identifier[0] == DOMAIN
                and any(identifier[1].startswith(prefix) for prefix in prefixes)
                for identifier in device.identifiers
            ):
                continue
            for entity in er.async_entries_for_device(entity_registry, device.id):
                entity_registry.async_update_entity(
                    entity.entity_id, device_id=gateway_device.id
                )
            device_registry.async_remove_device(device.id)


def _remove_retired_entities(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    coordinators: tuple[BoschBuderusDataUpdateCoordinator, ...],
) -> None:
    """Remove preview entities that never represented a distinct scalar value."""
    retired_unique_ids: set[str] = set()
    retired_unique_id_prefixes: set[str] = set()
    for coordinator in coordinators:
        gateway_id = coordinator.gateway.gateway_id
        retired_unique_ids.add(f"{gateway_id}:heatSources:numberOfStarts")
        for domain in (
            "totalConsumption",
            "chConsumption",
            "dhwConsumption",
            "coolingConsumption",
        ):
            retired_unique_ids.add(
                f"{gateway_id}:heatSources:emon:{domain}:electricity"
            )
        for resource in coordinator.resources.values():
            if capability_maturity(resource.path) is CapabilityMaturity.OBSERVED:
                semantic_path = resource.path.strip("/").replace("/", ":")
                retired_unique_id_prefixes.add(f"{gateway_id}:{semantic_path}")
            empty_name = resource.path.endswith("/name") and (
                not isinstance(resource.value, str)
                or configured_device_name(resource.value) is None
            )
            if (
                resource.metadata.resource_type == "switchProgram"
                and not resource.has_value
                and not resource.values
            ) or empty_name:
                semantic_path = resource.path.strip("/").replace("/", ":")
                retired_unique_ids.add(f"{gateway_id}:{semantic_path}")

    entity_registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if entity.unique_id in retired_unique_ids or any(
            entity.unique_id == prefix or entity.unique_id.startswith(f"{prefix}:")
            for prefix in retired_unique_id_prefixes
        ):
            entity_registry.async_remove(entity.entity_id)


def _enable_new_default_entities(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    coordinators: tuple[BoschBuderusDataUpdateCoordinator, ...],
) -> None:
    """Enable newly promoted defaults without overriding user choices."""
    from .binary_sensor import build_binary_sensor_descriptions
    from .sensor import build_sensor_descriptions

    entity_registry = er.async_get(hass)
    for coordinator in coordinators:
        gateway_id = coordinator.gateway.gateway_id
        candidates = (
            (
                "sensor",
                build_sensor_descriptions(coordinator.resources),
            ),
            (
                "binary_sensor",
                build_binary_sensor_descriptions(coordinator.resources),
            ),
        )
        for platform, descriptions in candidates:
            for description in descriptions:
                if not description.entity_registry_enabled_default:
                    continue
                unique_id = f"{gateway_id}:{description.key}"
                entity_id = entity_registry.async_get_entity_id(
                    platform, DOMAIN, unique_id
                )
                if entity_id is None:
                    continue
                registered = entity_registry.async_get(entity_id)
                if (
                    registered is not None
                    and registered.config_entry_id == entry.entry_id
                    and registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
                ):
                    entity_registry.async_update_entity(entity_id, disabled_by=None)
