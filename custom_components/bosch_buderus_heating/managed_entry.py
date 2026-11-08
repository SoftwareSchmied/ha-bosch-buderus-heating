"""HA owner lifecycle for independently managed connections and retained inventory."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any, cast

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlowContext,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .configuration import CLOUD_KIND, LOCAL_KIND, ConfigurationError, JsonObject
from .configuration_management import section
from .connection_inventory import ConnectionInventory
from .connection_runtime import ConnectionPhase, ConnectionRuntime, DriverFactory
from .const import DOMAIN, PLATFORMS
from .managed_platforms import ManagedPlatforms
from .managed_services import ManagedServices

_LOGGER = logging.getLogger(__name__)


# P49b step 7: the repair note of an entry without any access.
SETUP_ISSUE_PREFIX = "setup_unfinished_"


def delete_setup_issue(hass: HomeAssistant, entry_id: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, SETUP_ISSUE_PREFIX + entry_id)


def sync_setup_issue(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    """Raise the note for an entry without any access; remove it otherwise."""
    connections = entry.data.get("connections") if entry.version == 4 else None
    if entry.version != 4 or connections:
        delete_setup_issue(hass, entry.entry_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        SETUP_ISSUE_PREFIX + entry.entry_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="setup_unfinished",
        translation_placeholders={"title": entry.title},
    )


class ManagedEntry:
    """Load known ownership before starting independently managed connections."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        factories: Mapping[str, DriverFactory] | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        if factories is None:
            from .managed_cloud import CloudDriverFactory
            from .managed_local import LocalDriverFactory

            factories = {
                CLOUD_KIND: CloudDriverFactory(hass, entry, lambda: self.connections),
                LOCAL_KIND: LocalDriverFactory(hass, entry, lambda: self.connections),
            }
        self.connections = ConnectionRuntime(hass, entry, factories)
        self.inventory = ConnectionInventory(hass, entry)
        self.platforms = ManagedPlatforms(self)
        self.services = ManagedServices(self)
        self._platforms_started = False
        self._unsubscribe: list[Callable[[], None]] = []
        self._unavailable: set[str] = set()
        self._closed = False

    async def start(self) -> None:
        await self.inventory.load()
        self.inventory.restore_unavailable()
        from .discovery_inventory import discovery_inventory

        await discovery_inventory(self.hass, self.entry.entry_id).async_load()
        self._keep_templates()
        self._unsubscribe.append(self.connections.subscribe(self._state_changed))
        self.connections.start()
        if not self.entry.data.get("connections") and any(
            flow["context"].get("source") == "user"
            for flow in self.hass.config_entries.flow.async_progress_by_handler(
                self.entry.domain
            )
        ):
            return  # a new entry: its setup dialog continues (P49b step 7)
        self._sync_setup_issue(create=True)

    async def start_platforms(self) -> None:
        if self._closed or self._platforms_started:
            raise RuntimeError("Native platforms cannot be started twice")
        self._platforms_started = True
        await self.hass.config_entries.async_forward_entry_setups(self.entry, PLATFORMS)

    @callback
    def _state_changed(self) -> None:
        # R1 (P49b): entities of a target shown alone stay with that view.
        shown = self.connections.target_views()
        unavailable = {
            item.binding_id
            for item in self.inventory.entities
            if item.target_id not in shown
            and self.connections.source_phase(item.binding_id)
            is not ConnectionPhase.READY
            and self.connections.display_data(item.binding_id) is None
        }
        if changed := unavailable - self._unavailable:
            self.inventory.restore_unavailable(bindings=changed)
        self._unavailable = unavailable
        self._sync_setup_issue()
        self.schedule_reauthentication()
        self._keep_templates()

    @callback
    def _keep_templates(self) -> None:
        """1A (P49b): descriptions only for selected systems' gateways."""
        from .discovery_inventory import discovery_inventory

        inventory = discovery_inventory(self.hass, self.entry.entry_id)
        if self._closed or not inventory.loaded:
            return
        try:
            snapshot = self.connections.snapshot
            bindings = section(snapshot.to_options(), "bindings")
            kept = {
                cast(str, cast(JsonObject, binding.to_data()["locator"])["value"])
                for key, binding in snapshot.bindings.items()
                if binding.adapter == "pointt" and bindings[key]["enabled"]
            }
            # 6B-a: also systems shown from their local readings alone (R1).
            kept.update(self.connections.target_view_gateways(marked=True).values())
            inventory.keep_templates(kept)
        except Exception:  # descriptions are optional; never block the entry
            _LOGGER.warning("Stored descriptions could not be tidied up", exc_info=True)

    @callback
    def _sync_setup_issue(self, *, create: bool = False) -> None:
        """P49b step 7: the repair note of an entry without any access. It is
        raised at a start without an access (not during the setup dialog of a
        new entry) and ends as soon as an access exists."""
        if self._closed:
            return
        if self.connections.snapshot.connections:
            delete_setup_issue(self.hass, self.entry.entry_id)
        elif create:
            sync_setup_issue(self.hass, self.entry)

    @callback
    def schedule_reauthentication(self) -> None:
        if self._closed:
            return
        pending = tuple(
            key
            for key in self.connections.pending_reauthentication
            if self.connections.snapshot.connections[key].kind == CLOUD_KIND
        )
        if not pending or any(
            self.entry.async_get_active_flows(
                self.hass, {SOURCE_REAUTH, SOURCE_RECONFIGURE}
            )
        ):
            return
        self.entry.async_start_reauth(
            self.hass, context=cast(ConfigFlowContext, {"connection_id": pending[0]})
        )

    async def unload(self) -> bool:
        """Report platform unload failure without discarding the live owner."""
        if self._platforms_started:
            if not await self.hass.config_entries.async_unload_platforms(
                self.entry, PLATFORMS
            ):
                return False
            self._platforms_started = False
        await self.close()
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        await self.connections.close()
        await self.services.close()
        await self.platforms.close()
        if self._platforms_started:
            await self.hass.config_entries.async_unload_platforms(self.entry, PLATFORMS)
        # Whole-entry deletion can precede the unload callback.
        with suppress(ConfigurationError):
            self.inventory.restore_unavailable()
        await self.inventory.save()

    async def remove(self) -> None:
        delete_setup_issue(self.hass, self.entry.entry_id)
        await self.close()
        self.services.remove_owner()
        await self.inventory.remove_owner()


async def async_setup_managed_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    factories: Mapping[str, DriverFactory] | None = None,
) -> ManagedEntry:
    """Start the owner without waiting for the cloud or a local appliance."""
    runtime = ManagedEntry(hass, entry, factories)
    entry.runtime_data = runtime
    try:
        await runtime.start()
        await runtime.start_platforms()
    except BaseException:
        await runtime.close()
        raise
    return runtime
