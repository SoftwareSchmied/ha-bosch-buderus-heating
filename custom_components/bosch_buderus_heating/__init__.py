"""Bosch/Buderus Home Comfort integration package."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .auth_diagnostics import async_load_auth_histories
from .auth_lifecycle import (
    async_wait_for_entry_refresh,
    entry_auth,
    forget_entry_auth,
)
from .bacon.client import BaconClient
from .bacon.models import (
    CONF_BACON_ACCOUNT,
    CONF_BACON_IDS,
    BaconError,
    account_key,
    device_key,
    device_list,
    token_subject,
)
from .bacon.session import BaconAccount
from .cloud_pauses import cloud_pause_store
from .compatibility import sync_firmware_compatibility_issue
from .configuration_access import EntryConfiguration
from .configuration_migration import convert_legacy_configuration
from .const import (
    CONF_GATEWAY_IDS,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import BoschBuderusDataUpdateCoordinator
from .counter_selection import counter_source_store
from .device import device_info_for_resource
from .discovery import error_category
from .discovery_inventory import (
    DiscoveryInventory,
    async_remove_discovery_inventory,
    discovery_inventory,
    is_transient_result,
)
from .ha_compat import device_by_identifier, identifier_owned_elsewhere
from .holiday_ui import async_setup_controls_ui, async_setup_holiday_ui
from .local_budget import LocalBudgetError, local_budget_manager
from .local_enrollment import LocalEnrollmentError, enrollment_journal
from .managed_entry import ManagedEntry, async_setup_managed_entry
from .notifications import async_setup_notifications, async_update_notification_options
from .pointt import (
    AuthenticationError,
    OAuthClient,
    PointTClient,
    PointTError,
    RateLimited,
)
from .resource_catalog import (
    CapabilityMaturity,
    capability_maturity,
    configured_device_name,
    unknown_never_entity,
)
from .runtime import BoschBuderusRuntimeData
from .start_counters import is_start_counter_path, registered_start_counter_path
from .startup_data import STARTUP_BUDGET_SECONDS, StartupSession, is_startup_entity_path
from .storage_migration import migrate_entry_storage
from .switch_programs import is_switch_program_path
from .wallbox import WallboxCoordinator, is_wallbox

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

type BoschBuderusConfigEntry = (
    ConfigEntry[BoschBuderusRuntimeData] | ConfigEntry[ManagedEntry]
)
type LegacyConfigEntry = ConfigEntry[BoschBuderusRuntimeData]
_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the holiday form selector when the frontend is available."""
    # O64: the independent stores load side by side; each keeps its own error
    # handling. Saved cloud pauses must be known before any entry sends a cloud
    # request, last shown counters before an entity shows a counter.
    results = await asyncio.gather(
        cloud_pause_store(hass).async_initialize(),
        counter_source_store(hass).async_initialize(),
        _async_load_local_access_storage(hass),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    await async_setup_holiday_ui(hass)
    await async_setup_controls_ui(hass)
    from .local_identity import install_local_support
    from .update_load import install_update_load

    install_local_support(hass)
    install_update_load(hass)
    from .store_cleanup import async_sweep_orphaned_stores

    # STORE-5: files left by entries removed earlier, before any entry starts.
    await async_sweep_orphaned_stores(hass)
    return True


async def _async_load_local_access_storage(hass: HomeAssistant) -> None:
    """The enrollment journal reads pauses from the budget: one after the other."""
    try:
        await local_budget_manager(hass).initialize()
        await enrollment_journal(hass).records()
        await enrollment_journal(hass).initialize()
    except LocalEnrollmentError, LocalBudgetError:
        _LOGGER.warning("Local access storage could not be reconciled")


async def async_setup_entry(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> bool:
    """Dispatch the entry only after HA has checked its storage version."""
    # Sign-in histories must be loaded before any token manager starts.
    await async_load_auth_histories(hass, entry)
    if entry.version == 4:
        from .configuration import ConfigurationError, load_configuration
        from .gateway_exchange import async_resume_gateway_exchange

        try:
            load_configuration(
                entry.entry_id,
                entry.data,
                entry.options,
                version=entry.version,
                minor_version=entry.minor_version,
            )
        except ConfigurationError as error:
            # Static codes only; never data, credentials or identifiers.
            _LOGGER.error("The stored configuration cannot be read (%s)", error)
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="invalid_stored_configuration",
                translation_placeholders={"reason": str(error)},
            ) from None

        # P50: an interrupted gateway exchange is finished before any source
        # starts, so no source runs against half-changed identities.
        await async_resume_gateway_exchange(hass, entry)
        # Before any source starts: no entity of this entry is running yet.
        _remove_unconfigured_gateway_entries(hass, entry)
        await async_setup_managed_entry(hass, entry)
        return True
    return await _async_setup_legacy_entry(hass, cast(LegacyConfigEntry, entry))


async def _async_setup_legacy_entry(
    hass: HomeAssistant, entry: LegacyConfigEntry
) -> bool:
    """Support an explicit legacy entry without bypassing HA migration dispatch."""
    try:
        configuration = EntryConfiguration(entry)
        tokens = configuration.tokens()
        cloud_data = configuration.cloud_data()
        selected_ids = _selected_gateway_ids(cloud_data)
        bacon_ids = device_list(cloud_data.get(CONF_BACON_IDS, []))
    except (ValueError, BaconError) as err:
        raise ConfigEntryAuthFailed from err

    session = async_get_clientsession(hass)
    oauth_client = OAuthClient(session)
    previous_runtime = getattr(entry, "runtime_data", None)
    try:
        token_manager, account_backoff = entry_auth(hass, entry, oauth_client)
    except ValueError as err:
        raise ConfigEntryAuthFailed from err
    startup = StartupSession(tuple(sorted(selected_ids)), options=dict(entry.options))
    if (
        isinstance(previous_runtime, BoschBuderusRuntimeData)
        and previous_runtime.startup is not None
        and previous_runtime.startup.can_resume(tuple(selected_ids), entry.options)
        and previous_runtime.token_manager.tokens == tokens
    ):
        startup = previous_runtime.startup
    if (
        isinstance(previous_runtime, BoschBuderusRuntimeData)
        and previous_runtime.startup is not None
        and previous_runtime.startup is not startup
    ):
        previous_runtime.startup.detach()

    @callback
    def startup_state_changed() -> None:
        # HA skips the integration's unload hook when reloading setup_retry.
        # Observe that explicit reset while preserving automatic retries.
        if entry.state in (ConfigEntryState.NOT_LOADED, ConfigEntryState.SETUP_ERROR):
            startup.close()

    startup.attach_state_listener(
        lambda: entry.async_on_state_change(startup_state_changed)
    )
    client = PointTClient(
        session,
        token_manager,
        metrics=(
            previous_runtime.client.metrics
            if isinstance(previous_runtime, BoschBuderusRuntimeData)
            and previous_runtime.startup is startup
            else None
        ),
        backoff=account_backoff,
    )
    # Keep the account brake even when gateway discovery prevents setup.
    entry.runtime_data = BoschBuderusRuntimeData(
        client, token_manager, (), (), startup=startup
    )

    inventory = discovery_inventory(hass, entry.entry_id)
    await inventory.async_load()
    # P47: a temporary cloud failure starts with the stored inventory instead
    # of leaving every entity "not provided" until a setup retry succeeds.
    inventory_reason: str | None = None
    startup_rate_limit: RateLimited | None = None
    try:
        gateways = await client.get_gateways() if selected_ids else ()
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed from err
    except PointTError as err:
        stored = inventory.startup_gateways(selected_ids)
        if stored is None or not _temporary(error_category(err)):
            raise ConfigEntryNotReady from err
        gateways = stored
        inventory_reason = f"gateway_list_{error_category(err)}"
        if isinstance(err, RateLimited):
            startup_rate_limit = err

    selected_gateways = tuple(
        gateway for gateway in gateways if gateway.gateway_id in selected_ids
    )
    if len(selected_gateways) != len(selected_ids):
        raise ConfigEntryNotReady("A configured gateway is currently unavailable")
    if inventory_reason is None:
        inventory.remember_gateways(selected_gateways)

    coordinators = tuple(
        BoschBuderusDataUpdateCoordinator(
            hass,
            client,
            gateway,
            entry,
            configuration.polling_profile(),
            startup_session=startup,
        )
        for gateway in selected_gateways
        if not is_wallbox(gateway)
    )
    wallboxes = tuple(
        WallboxCoordinator(hass, client, gateway, entry)
        for gateway in selected_gateways
        if is_wallbox(gateway)
    )
    # Publish the safe, partially initialized runtime before the first refresh.
    # Home Assistant can then produce diagnostics when discovery itself keeps
    # the config entry in setup_retry.
    entry.runtime_data = BoschBuderusRuntimeData(
        client=client,
        token_manager=token_manager,
        gateways=selected_gateways,
        coordinators=coordinators,
        wallboxes=wallboxes,
        startup=startup,
    )
    _restore_registered_startup_paths(hass, entry)
    inventory.retain(selected_ids)
    for coordinator in coordinators:
        coordinator.inventory = inventory
        await coordinator.async_load_fault_state()
        if inventory_reason is None:
            try:
                await coordinator.async_config_entry_first_refresh()
                continue
            except ConfigEntryNotReady:
                # Only a temporary cloud failure; a plant answering 404
                # everywhere or an unexpected error is retried as before.
                reason = coordinator.discovery_diagnostics.stop_reason
                if not _temporary(reason) or not _start_from_inventory(
                    coordinator, inventory, f"discovery_{reason}"
                ):
                    raise
        elif not _start_from_inventory(
            coordinator, inventory, inventory_reason, rate_limit=startup_rate_limit
        ):
            raise ConfigEntryNotReady("No saved inventory for a configured gateway")

    # Start-up values only for gateways that answered; the others read them
    # through the regular start-up reader once the cloud answers again.
    if live := tuple(item for item in coordinators if item.startup_mode == "live"):
        await _async_load_startup_data(live)

    for wallbox in wallboxes:
        with suppress(ConfigEntryNotReady):
            await wallbox.async_config_entry_first_refresh()
        # Keep mixed accounts usable. Entities retry on their normal cadence.

    if bacon_ids:
        bacon_client = BaconClient(session, token_manager)
        bacon_startup_error: str | None = None
        try:
            bacon_client.subject = token_subject(await bacon_client.access_token())
            if account_key(
                bacon_client.subject or ""
            ) != configuration.cloud_data().get(CONF_BACON_ACCOUNT):
                raise BaconError("account_changed")
        except BaconError as err:
            if err.category in {"authentication", "account_changed"}:
                raise ConfigEntryAuthFailed from err
            if err.category not in {"connection", "rate_limited"}:
                raise ConfigEntryNotReady from err
            # A temporary failure must not hold back the whole entry (P47).
            # The account connects in the background with its own backoff and
            # still checks that the signed-in account is the configured one.
            # A missing account hash never matches, as in the check above.
            bacon_client.expected_account = entry.data.get(CONF_BACON_ACCOUNT, "")
            bacon_startup_error = err.category
            _LOGGER.warning(
                "Other cloud devices could not sign in during start-up (%s); "
                "connecting in the background",
                err.category,
            )

        def bacon_updated() -> None:
            account = entry.runtime_data.bacon
            if account and account.error in {"authentication", "account_changed"}:
                entry.async_start_reauth(hass)

        entry.runtime_data.bacon = BaconAccount(
            bacon_client, bacon_ids, notify=bacon_updated
        )
        # Diagnostics show the cause before the first background attempt.
        entry.runtime_data.bacon.error = bacon_startup_error

    sync_firmware_compatibility_issue(hass, entry, coordinators)

    _remove_unselected_gateway_entries(hass, entry, selected_ids)
    _enable_new_default_entities(hass, entry, coordinators)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if (bacon := entry.runtime_data.bacon) is not None:
        bacon.attach_task(
            entry.async_create_background_task(hass, bacon.run(), "bacon_account")
        )
    _consolidate_logical_devices(hass, entry, coordinators)
    _remove_retired_entities(hass, entry, coordinators)
    entry.runtime_data.notifications = await async_setup_notifications(hass, entry)
    setup_data = configuration.setup_parameters()
    setup_profile = configuration.polling_profile()
    reload_scheduled = False

    async def entry_updated(hass: HomeAssistant, updated: ConfigEntry[Any]) -> None:
        nonlocal reload_scheduled
        if reload_scheduled:
            return
        try:
            current = EntryConfiguration(updated)
            changed = (
                current.setup_parameters() != setup_data
                or current.polling_profile() != setup_profile
                or current.tokens() != token_manager.tokens
            )
        except ValueError:
            changed = True
        if changed:
            reload_scheduled = True
            hass.config_entries.async_schedule_reload(updated.entry_id)
            return
        # Ordinary token rotation has already updated this manager. It must
        # not restart discovery or disconnect the additional cloud devices.
        await async_update_notification_options(hass, updated)

    entry.async_on_unload(entry.add_update_listener(entry_updated))
    startup.mark_setup_complete()
    entry.async_on_unload(startup.detach)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> bool:
    """Unload a Bosch/Buderus Home Comfort config entry."""
    if isinstance(entry.runtime_data, ManagedEntry):
        return await entry.runtime_data.unload()
    unloaded = bool(await hass.config_entries.async_unload_platforms(entry, PLATFORMS))
    if unloaded:
        if entry.runtime_data.bacon is not None:
            await entry.runtime_data.bacon.close()
        if entry.runtime_data.startup is not None:
            entry.runtime_data.startup.close()
        for coordinator in entry.runtime_data.coordinators:
            coordinator.cancel_manual_setpoint_recovery()
        for manager in entry.runtime_data.notifications:
            await manager.async_close()
    return unloaded


async def async_remove_entry(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> None:
    """Forget the memory-only authentication state when its entry is deleted."""
    from .connection_inventory import ConnectionInventory

    try:
        await enrollment_journal(hass).remove_owner(entry.entry_id)
    except LocalEnrollmentError:
        _LOGGER.warning("Pending local setup storage could not be cleared")
    counter_source_store(hass).discard_owner(entry.entry_id)
    from homeassistant.data_entry_flow import UnknownFlow

    from .config_flow import LOCAL_FIRST_START
    from .managed_entry import delete_setup_issue

    # A prepared local setup of this entry ends with it (P49b step 7).
    for flow in hass.config_entries.options.async_progress_by_handler(entry.entry_id):
        if flow["context"].get(LOCAL_FIRST_START) is True:
            with suppress(UnknownFlow):
                hass.config_entries.options.async_abort(flow["flow_id"])
    delete_setup_issue(hass, entry.entry_id)  # also without a runtime
    from .configuration_management import clear_not_found_issues

    clear_not_found_issues(hass, entry.entry_id)
    _clear_entry_issues(hass, entry.entry_id)
    runtime = getattr(entry, "runtime_data", None)
    if isinstance(runtime, ManagedEntry):
        await runtime.remove()
    elif entry.version == 4:
        await ConnectionInventory(hass, entry).remove_owner()
    forget_entry_auth(hass, entry)
    await async_remove_discovery_inventory(hass, entry.entry_id)
    from .gateway_exchange import (
        async_remove_exchange_journal,
        async_remove_module_fingerprints,
        clear_entry_exchange_issues,
    )

    await async_remove_module_fingerprints(hass, entry.entry_id)
    await async_remove_exchange_journal(hass, entry.entry_id)
    clear_entry_exchange_issues(hass, entry.entry_id)
    from .store_cleanup import async_remove_entry_stores

    await async_remove_entry_stores(hass, entry)


def _clear_entry_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Every repair note of a removed entry, loaded or not (STORE-4).

    HA unloads an entry and drops its runtime before removing it, so the
    runtime's own cleanup never runs here. Firmware, rate-limit and
    certificate notes name their entry in their data.
    """
    registry = ir.async_get(hass)
    for domain, issue_id in tuple(registry.issues):
        issue = registry.issues[(domain, issue_id)]
        if domain == DOMAIN and (issue.data or {}).get("entry_id") == entry_id:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def _restore_registered_startup_paths(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    *,
    coordinators: tuple[BoschBuderusDataUpdateCoordinator, ...] | None = None,
) -> None:
    """Recover exact existing scalar IDs without guessing device identifiers."""
    registry = er.async_get(hass)
    if coordinators is None:
        # Managed sources pass their coordinators explicitly above.
        coordinators = cast(BoschBuderusRuntimeData, entry.runtime_data).coordinators
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.domain != "sensor" or entity.platform != DOMAIN:
            continue
        for coordinator in coordinators:
            counter_path = registered_start_counter_path(
                entity.unique_id, coordinator.gateway.gateway_id
            )
            if counter_path is not None:
                coordinator.restore_start_counter_path(
                    counter_path,
                    scalar=entity.unique_id.endswith(":numberOfStarts"),
                )
            prefix = f"{coordinator.gateway.gateway_id}:"
            if entity.unique_id.startswith(prefix):
                path = "/" + entity.unique_id.removeprefix(prefix).replace(":", "/")
                if is_startup_entity_path(path):
                    coordinator.startup_data.schedule(path, 0, registered=True)


def _temporary(category: str | None) -> bool:
    """A failure that says nothing about the plant itself."""
    return category == "rate_limited" or is_transient_result(category)


def _start_from_inventory(
    coordinator: BoschBuderusDataUpdateCoordinator,
    inventory: DiscoveryInventory,
    reason: str,
    *,
    rate_limit: RateLimited | None = None,
    announce: bool = True,
) -> bool:
    """Start one gateway with its stored resources; False if there are none."""
    gateway_id = coordinator.gateway.gateway_id
    stored = inventory.gateway(gateway_id)
    resources = inventory.resources(gateway_id)
    if stored is None or not resources:
        return False
    seen = inventory.seen(gateway_id)
    # Age of the newest confirmed resource, not of the last gateway listing.
    newest = max((seen[path] for path in resources if path in seen), default=None)
    age = (
        max(0, int((dt_util.utcnow() - newest).total_seconds()))
        if newest is not None
        else inventory.age_seconds(gateway_id)
    )
    coordinator.start_from_inventory(
        resources,
        reason=reason,
        age_seconds=age,
        seen=seen,
        rate_limit=rate_limit,
    )
    coordinator.startup_age_seconds = age
    if announce:
        _announce_inventory_start(reason, age)
    return True


def _announce_inventory_start(reason: str, age: int | None) -> None:
    if reason == "cloud_switched_off":
        # A deliberate choice, so no warning on every start (P49).
        _LOGGER.info(
            "The cloud access is switched off; showing local readings with the "
            "inventory confirmed %d hours ago",
            (age or 0) // 3600,
        )
        return
    if reason == "sign_in_required":
        _LOGGER.warning(
            "PointT needs a new sign-in; showing local readings with the "
            "inventory confirmed %d hours ago until the sign-in is renewed",
            (age or 0) // 3600,
        )
        return
    _LOGGER.warning(
        "PointT start-up failed temporarily (%s); starting with the inventory "
        "confirmed %d hours ago. Entities stay unavailable until the cloud "
        "answers again",
        reason,
        (age or 0) // 3600,
    )


async def _async_load_startup_data(
    coordinators: tuple[BoschBuderusDataUpdateCoordinator, ...],
) -> None:
    """Give all core gateways priority, then share one short optional deadline."""
    deadline = monotonic() + STARTUP_BUDGET_SECONDS
    while monotonic() < deadline:
        attempted = False
        for coordinator in coordinators:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            snapshots = dict(coordinator.data or {})
            if await coordinator.async_read_startup_batch(snapshots, budget=remaining):
                coordinator.data = snapshots
                attempted = True
        if not attempted:
            return
    for coordinator in coordinators:
        coordinator.startup_data.budget_expired(monotonic())


async def async_migrate_entry(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> bool:
    """Keep dispatch aligned with the version advertised by the setup flow."""
    from .config_flow import BoschBuderusConfigFlow

    if entry.version > BoschBuderusConfigFlow.VERSION:
        return False
    if BoschBuderusConfigFlow.VERSION == 4 and entry.version == 4:
        if entry.minor_version > BoschBuderusConfigFlow.MINOR_VERSION:
            _LOGGER.error(
                "The configuration was saved by a newer version of this integration"
            )
            return False
        try:
            await async_wait_for_entry_refresh(hass, entry.entry_id)
            migrate_entry_storage(
                hass, entry, minor_version=BoschBuderusConfigFlow.MINOR_VERSION
            )
        except Exception as error:
            # Not a migration failure: the setup reports the stored
            # configuration with a clear message and retries at every start.
            _LOGGER.warning(
                "The stored configuration could not be checked (%s)",
                _migration_reason(error),
            )
        return True
    if BoschBuderusConfigFlow.VERSION == 4:
        try:
            await async_wait_for_entry_refresh(hass, entry.entry_id)
            if entry.version < 3:
                # Validate the later storage shape before any preview registry
                # changes. A malformed preview must retain its original state.
                convert_legacy_configuration(
                    entry.entry_id,
                    entry.data,
                    entry.options,
                    version=3,
                    minor_version=entry.minor_version,
                )
            _migrate_preview_devices(hass, entry)
            migrate_entry_storage(
                hass, entry, minor_version=BoschBuderusConfigFlow.MINOR_VERSION
            )
        except Exception as error:
            # Neither credentials nor arbitrary transport exception text belongs
            # in logs, not even in a debug traceback: only a static code or the
            # error type (ERR-6). HA reports MIGRATION_ERROR and requires a
            # restart.
            _LOGGER.error(
                "Configuration migration could not be completed (%s)",
                _migration_reason(error),
            )
            return False
    else:
        _migrate_preview_devices(hass, entry)
    return True


def _migration_reason(error: Exception) -> str:
    """A static configuration code, otherwise only the type of the error."""
    from .configuration import ConfigurationError

    if isinstance(error, ConfigurationError):
        return str(error)
    return type(error).__name__


@callback
def _migrate_preview_devices(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> None:
    """Retain the earlier preview transition before converting version 3."""
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
                existing = device_by_identifier(
                    device_registry, replacement, entry.entry_id
                )
                if existing is not None and existing.id != device.id:
                    for entity in er.async_entries_for_device(
                        entity_registry, device.id
                    ):
                        entity_registry.async_update_entity(
                            entity.entity_id, device_id=existing.id
                        )
                    device_registry.async_remove_device(device.id)
                elif not identifier_owned_elsewhere(
                    device_registry, replacement, entry.entry_id
                ):
                    # On older releases a device of another entry already holds
                    # the identifier; the legacy device then stays unchanged.
                    device_registry.async_update_device(
                        device.id, new_identifiers={replacement}
                    )
                break
        hass.config_entries.async_update_entry(entry, version=3)


def _selected_gateway_ids(data: Any) -> frozenset[str]:
    value = data.get(CONF_GATEWAY_IDS)
    if (
        not isinstance(value, list)
        or (not value and not data.get(CONF_BACON_IDS))
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
    runtime = getattr(entry, "runtime_data", None)
    bacon = runtime.bacon if isinstance(runtime, BoschBuderusRuntimeData) else None
    # Device keys derive from the signed-in account. Before the first
    # sign-in (start-up during an outage) they are unknown: keep everything.
    bacon_keys: set[str] | None = (
        set()
        if bacon is None
        else {device_key(bacon.client.subject, serial) for serial in bacon.selected}
        if bacon.client.subject is not None
        else None
    )
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if entity.unique_id.startswith("bacon:"):
            if (
                bacon_keys is not None
                and entity.unique_id.rsplit(":", 1)[0] not in bacon_keys
            ):
                entity_registry.async_remove(entity.entity_id)
            continue
        if entity.unique_id.startswith(f"{entry.entry_id}:request_metrics:"):
            continue
        gateway_id = entity.unique_id.split(":", 1)[0]
        if gateway_id not in selected_ids:
            entity_registry.async_remove(entity.entity_id)

    for device in tuple(
        dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    ):
        bacon_identifiers = {
            identifier[1]
            for identifier in device.identifiers
            if identifier[0] == DOMAIN and identifier[1].startswith("bacon:")
        }
        if bacon_identifiers:
            if bacon_keys is not None and bacon_identifiers.isdisjoint(bacon_keys):
                device_registry.async_remove_device(device.id)
            continue
        gateway_ids = {
            identifier[1].split(":", 1)[0]
            for identifier in device.identifiers
            if identifier[0] == DOMAIN
        }
        if gateway_ids and gateway_ids.isdisjoint(selected_ids):
            device_registry.async_remove_device(device.id)


def _user_customised(entity: er.RegistryEntry) -> bool:
    """The user named, placed, labelled, hid or switched off this entry."""
    return bool(
        entity.name
        or entity.icon
        or entity.area_id
        or entity.labels
        or entity.disabled_by is er.RegistryEntryDisabler.USER
        or entity.hidden_by is er.RegistryEntryHider.USER
    )


def _remove_unconfigured_gateway_entries(
    hass: HomeAssistant, entry: BoschBuderusConfigEntry
) -> None:
    """Managed entries: remove the entries of gateways no system has any more.

    A deselected or forgotten system keeps its target, devices and entities
    (it may be selected again, is shown from its local access or moves to an
    exchanged gateway), so only a gateway that belongs to no stored system
    loses its entries, and only those the user never customised. Entries of
    other cloud devices and of the access statistics are never touched.
    Nothing happens when a system's gateway cannot be told.
    """
    from .configuration import load_configuration
    from .connection_runtime import plant_gateway

    try:
        snapshot = load_configuration(
            entry.entry_id,
            entry.data,
            entry.options,
            version=entry.version,
            minor_version=entry.minor_version,
        )
    except ValueError, TypeError, KeyError:  # also ConfigurationError
        return
    gateways: set[str] = set()
    for target in snapshot.targets.values():
        data = target.to_data()
        gateway = plant_gateway(target.kind, data)
        if gateway is not None:
            gateways.add(gateway)
            continue
        identities = data.get("identities")
        namespaces = {
            item.get("namespace")
            for item in (identities if isinstance(identities, list) else ())
            if isinstance(item, dict)
        }
        if not namespaces or not namespaces <= {"bacon_device"}:
            return  # a system without a known gateway: keep everything
    for binding in snapshot.bindings.values():
        locator = binding.to_data().get("locator")
        value = locator.get("value") if isinstance(locator, dict) else None
        if isinstance(value, str):
            gateways.add(value)
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = entity.unique_id
        if (
            unique_id.startswith(("bacon:", f"{entry.entry_id}:"))
            or ":" not in unique_id
            or unique_id.split(":", 1)[0] in gateways
            or _user_customised(entity)
        ):
            continue
        registry.async_remove(entity.entity_id)


@callback
def remove_never_entities(coordinator: BoschBuderusDataUpdateCoordinator) -> None:
    """Managed entries: the cleanup of the entry setup for one cloud view.

    Removes the entries of resources that can never be entities (private
    data, identifiers, structures and their former field entries) and the
    retired preview entries, only for resources this view currently has.
    Entries the user customised stay. A read-only local view never removes.
    """
    entry = getattr(coordinator, "config_entry", None)
    if (
        entry is None
        or entry.version != 4
        or getattr(coordinator, "read_only_view", False) is True
    ):
        return
    try:
        _remove_retired_entities(
            coordinator.hass,
            cast("BoschBuderusConfigEntry", entry),
            (coordinator,),
            keep_customised=True,
        )
    except Exception:
        # A cleanup never keeps the entities of a view from being added.
        _LOGGER.exception("Registry entries could not be cleaned up")


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
    *,
    keep_customised: bool = False,
) -> None:
    """Remove preview entities that never represented a distinct scalar value."""
    retired_unique_ids: set[str] = set()
    observed_resource_paths: dict[str, str] = {}
    # Semantic ID of a structure -> its resource path.
    field_structures: dict[str, str] = {}
    present: set[str] = set()
    for coordinator in coordinators:
        gateway_id = coordinator.gateway.gateway_id
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
            present.add(f"{gateway_id}:{resource.path.strip('/').replace('/', ':')}")
            if capability_maturity(resource.path) is CapabilityMaturity.OBSERVED:
                semantic_path = resource.path.strip("/").replace("/", ":")
                if unknown_never_entity(resource):
                    # Private data, a structure or configuration: never an
                    # entity. Any other unknown resource keeps its entity
                    # (create-all), also while its value is missing, invalid
                    # or of another type, and a resource missing from this
                    # cycle (a partial batch, a failed read) is not here.
                    observed_resource_paths[f"{gateway_id}:{semantic_path}"] = (
                        resource.path
                    )
                    if isinstance(resource.value, dict | list):
                        # Its fields had preview entities ("P:field"); a
                        # child resource of a reference node never does.
                        field_structures[f"{gateway_id}:{semantic_path}"] = (
                            resource.path
                        )
            empty_name = resource.path.endswith("/name") and (
                not isinstance(resource.value, str)
                or configured_device_name(resource.value) is None
            )
            if (
                resource.metadata.resource_type == "switchProgram"
                and not resource.has_value
                and not resource.values
                # A known program may be temporarily empty or malformed. Keep
                # its registry entry, including the user's name and activation.
                and not is_switch_program_path(resource.path)
            ) or empty_name:
                semantic_path = resource.path.strip("/").replace("/", ":")
                retired_unique_ids.add(f"{gateway_id}:{semantic_path}")

    entity_registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if keep_customised and _user_customised(entity):
            continue
        if (
            entity.unique_id in retired_unique_ids
            or entity.unique_id in observed_resource_paths
            or _former_field_entity(entity.unique_id, field_structures, present)
        ):
            entity_registry.async_remove(entity.entity_id)


def _former_field_entity(
    unique_id: str, field_structures: dict[str, str], present: set[str]
) -> bool:
    """A preview field entry ("P:field") of a structure, never a resource.

    A direct child resource can share the shape of a field ID: a catalogued
    child and any resource present now keep their entities, also while
    discovery temporarily omits a catalogued child's value.
    """
    if unique_id in present:
        return False
    structure, _, field = unique_id.rpartition(":")
    path = field_structures.get(structure)
    return (
        path is not None
        and capability_maturity(f"{path}/{field}") is CapabilityMaturity.OBSERVED
    )


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
                if is_start_counter_path(description.resource_path):
                    # Existing counter activation choices survive this update.
                    continue
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
