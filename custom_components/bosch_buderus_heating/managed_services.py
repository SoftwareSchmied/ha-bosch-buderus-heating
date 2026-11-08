"""Keep target notifications and source repairs independent of connection jobs."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.helpers import issue_registry as ir

from .compatibility import sync_firmware_compatibility_issue
from .configuration_management import section
from .const import DOMAIN, GATEWAY_EXCHANGE_ISSUE_PREFIX
from .coordinator import BoschBuderusDataUpdateCoordinator
from .entry_sources import notification_gateways
from .faults import FaultTracker
from .notifications import FaultNotifications

if TYPE_CHECKING:
    from .managed_entry import ManagedEntry

_LOGGER = logging.getLogger(__name__)


class ManagedServices:
    """Retain local notification decisions while replacing only their source."""

    def __init__(self, owner: ManagedEntry) -> None:
        self.owner = owner
        self.notifications: dict[str, FaultNotifications] = {}
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        self._unsubscribe = owner.connections.subscribe(self.changed)

    def _sources(self) -> dict[str, BoschBuderusDataUpdateCoordinator]:
        if self._closed:
            return {}
        state = self.owner.connections
        # A2: a plant shown without a cloud (dormant, local plant, target or
        # held view) whose local error list decides its faults. A view kept
        # for a cloud that is expected back (P42) is not a source: its
        # notification keeps the last faults as not confirmed.
        sources = {
            value.gateway.gateway_id: value
            for value in (
                *(state.retained_data(key) for key in state.snapshot.bindings),
                *state.target_views().values(),
            )
            if isinstance(value, BoschBuderusDataUpdateCoordinator)
            and value.faults.local_authority is True
        }
        sources.update(
            {
                value.gateway.gateway_id: value
                for key in state.snapshot.bindings
                if isinstance(
                    value := state.source_data(key), BoschBuderusDataUpdateCoordinator
                )
            }
        )
        return sources

    def changed(self) -> None:
        if self._closed:
            return
        sources = self._sources()
        for gateway, manager in self.notifications.items():
            if manager.coordinator is not sources.get(gateway):
                manager.detach_source()
            manager.async_update()
        try:
            gateways = notification_gateways(self.owner.entry)
        except ValueError:
            gateways = tuple(sources)
        for gateway in gateways:
            source = sources.get(gateway)
            existing = self.notifications.get(gateway)
            if existing is not None and existing.coordinator is source:
                continue
            if gateway in self._jobs:
                continue
            task = self.owner.hass.async_create_background_task(
                self._attach(gateway), "home_comfort_notifications", eager_start=False
            )
            self._jobs[gateway] = task
            task.add_done_callback(partial(self._finished, gateway))
        state = self.owner.connections
        for key, binding in state.snapshot.bindings.items():
            if isinstance(
                value := state.source_data(key), BoschBuderusDataUpdateCoordinator
            ):
                sync_firmware_compatibility_issue(
                    self.owner.hass,
                    self.owner.entry,
                    (value,),
                    connection_id=binding.connection_id,
                    binding_id=key,
                )
        self._remove_orphan_repairs()

    def _finished(self, gateway: str, task: asyncio.Task[None]) -> None:
        self._jobs.pop(gateway, None)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("Heating fault notification setup failed")
        manager = self.notifications.get(gateway)
        if (
            not self._closed
            and manager is not None
            and manager.coordinator is not self._sources().get(gateway)
        ):
            self.changed()

    async def _attach(self, gateway: str) -> None:
        while not self._closed:
            source = self._sources().get(gateway)
            current = partial(self._is_current, gateway, source)
            manager = self.notifications.get(gateway)
            if manager is None:
                tracker = None
                if source is None:
                    tracker = FaultTracker(
                        self.owner.hass, self.owner.entry.entry_id, gateway
                    )
                    await tracker.async_load()
                    if self._closed:
                        return
                manager = FaultNotifications(
                    self.owner.hass,
                    self.owner.entry,
                    source,
                    source_current=current,
                    gateway_id=gateway,
                    tracker=tracker,
                    flush_tracker=False,
                )
                self.notifications[gateway] = manager
                try:
                    await manager.async_start()
                except asyncio.CancelledError:
                    await manager.async_close()
                    raise
                except Exception:
                    await manager.async_close()
                    self.notifications.pop(gateway, None)
                    _LOGGER.error(
                        "Heating fault notifications could not be initialized"
                    )
                    return
            elif source is None:
                manager.detach_source()
                return
            elif manager.coordinator is not source:
                manager.replace_source(source, current)
            if source is None and not self._sources().get(gateway):
                manager.detach_source()
                return
            if current():
                return
            manager.detach_source()

    def _is_current(
        self, gateway: str, source: BoschBuderusDataUpdateCoordinator | None
    ) -> bool:
        return source is not None and self._sources().get(gateway) is source

    def _remove_orphan_repairs(self, *, remove_all: bool = False) -> None:
        state = self.owner.connections
        registry = ir.async_get(self.owner.hass)
        options = state.snapshot.to_options()
        for (domain, key), issue in tuple(registry.issues.items()):
            data = issue.data or {}
            if domain != DOMAIN or data.get("entry_id") != self.owner.entry.entry_id:
                continue
            connection, binding = data.get("connection_id"), data.get("binding_id")
            if connection is not None and (
                remove_all
                or connection not in state.snapshot.connections
                or (binding is not None and binding not in state.snapshot.bindings)
                or (
                    # P50: "Gateway replaced?" asks about a running system.
                    key.startswith(GATEWAY_EXCHANGE_ISSUE_PREFIX)
                    # Both are known keys of the snapshot here.
                    and not (
                        section(options, "connections")[str(connection)]["enabled"]
                        and (
                            section(options, "bindings").get(binding, {})
                            if isinstance(binding, str)
                            else {}
                        ).get("enabled")
                    )
                )
            ):
                ir.async_delete_issue(self.owner.hass, DOMAIN, key)

    async def close(self) -> None:
        self._closed = True
        self._unsubscribe()
        for task in tuple(self._jobs.values()):
            task.cancel()
        await asyncio.gather(*tuple(self._jobs.values()), return_exceptions=True)
        await asyncio.gather(
            *(manager.async_close() for manager in self.notifications.values())
        )

    def remove_owner(self) -> None:
        self._remove_orphan_repairs(remove_all=True)
