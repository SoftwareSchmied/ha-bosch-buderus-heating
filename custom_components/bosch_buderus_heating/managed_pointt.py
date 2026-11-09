"""Run the existing PointT adapter in independently managed connection scopes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .configuration import ConfigurationError, JsonObject, StoredBinding
from .connection_runtime import (
    ConnectionPhase,
    RepeatedFailure,
    SourceContext,
    SourceFailure,
)
from .coordinator import (
    SOURCE_STOPPED_CATEGORY,
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
    SourceOwnedCoordinator,
)
from .counter_selection import CounterGuard, counter_source_store
from .discovery import error_category
from .discovery_inventory import DiscoveryInventory, discovery_inventory
from .local_selection import LocalSelection
from .managed_updates import SourceUpdates
from .managed_wallbox import ManagedWallboxCoordinator
from .pointt import Gateway, PointTClient
from .pointt.exceptions import (
    AuthenticationError,
    PointTError,
    RateLimited,
    SourceStopped,
)
from .wallbox import is_wallbox

if TYPE_CHECKING:
    from .faults import FaultTracker
    from .managed_cloud import CloudConnection

_LOGGER = logging.getLogger(__name__)
# P42: the local error list as a second fault source. A switch, so it can be
# turned off without touching the tracker if a device shows a mismatch.
LOCAL_FAULT_SOURCE = True
# P49b (S1): a switched-off access shows its stored inventory for up to 30
# days (P49), afterwards the plant's full local entities (read-only). Only for
# a switched-off access; a sign-in or start-up failure keeps the P40/P47 view.
# A switch, so it can be turned off without touching the views.
LOCAL_PLANT_VIEW = True
# The inventory has passed its age limit only after this margin.
INVENTORY_EXPIRY_MARGIN = timedelta(seconds=1)
# A timer that fires early (clock change) checks again after this time.
INVENTORY_RECHECK = timedelta(minutes=5)
# P49: an outdated inventory of a switched-off access is reported once.
_OLD_INVENTORY_REPORTED: set[str] = set()


class PointTSource:
    """Reuse normal discovery, polling and confirmed writes for one exact gateway."""

    def __init__(
        self, connection: CloudConnection, binding_id: str, binding: StoredBinding
    ) -> None:
        self.connection, self.binding_id, self.binding = connection, binding_id, binding
        self.context: SourceContext | None = None
        self.coordinator: (
            ManagedPointTCoordinator | ManagedWallboxCoordinator | None
        ) = None
        self._auth_failed = asyncio.Event()
        self._closed = False
        self._gateway_id: str | None = None
        self._inventory: DiscoveryInventory | None = None
        self._remove_listener: Callable[[], None] | None = None
        # P49b: the retained view that handed its fault baseline to this
        # attempt, and whether this attempt published (took over).
        self._faults_from: object | None = None
        # The fault tracker of this attempt's own coordinator (review P49b).
        self._attempt_faults: FaultTracker | None = None
        self._published = False

    def current(self) -> bool:
        return (
            not self._closed
            and not self._auth_failed.is_set()
            and self.context is not None
            and self.context.current
        )

    def ensure_current(self) -> None:
        if not self.current():
            raise SourceStopped()

    def authentication_failed(self) -> None:
        if self.current():
            self._auth_failed.set()

    async def run(self, context: SourceContext) -> None:
        from . import _start_from_inventory, _temporary

        self.context = context
        connection = self.connection
        locator = cast(JsonObject, self.binding.to_data()["locator"])
        gateway_id = cast(str, locator["value"])
        inventory = discovery_inventory(connection.hass, connection.entry.entry_id)
        await inventory.async_load()
        self._gateway_id, self._inventory = gateway_id, inventory
        # P40: the inventory of a gateway whose cloud access was removed stays
        # until its age limit, so adding the access again can start with it.
        # P47: a temporary cloud failure starts with the stored inventory, so
        # the source's entities exist (unavailable) instead of waiting for a
        # source retry while "not provided".
        inventory_reason: str | None = None
        rate_limit: RateLimited | None = None
        gateway_error: PointTError | None = None
        try:
            try:
                gateways = await connection.gateways()
            except SourceStopped:
                raise
            except AuthenticationError:
                # P40: with a local access, start from the stored inventory so
                # local readings are shown until the sign-in is renewed.
                await self._retain_from_inventory(context, gateway_id, inventory)
                raise
            except PointTError as err:
                stored = inventory.startup_gateways((gateway_id,))
                temporary = _temporary(error_category(err))
                if stored is None and temporary and LOCAL_PLANT_VIEW:
                    # 2A: without a usable inventory, the local readings show
                    # the plant until the cloud answers; retries go on.
                    await self._retain_local_plant(
                        context, gateway_id, inventory, startup=True
                    )
                if stored is None or not temporary:
                    raise
                gateways = stored
                inventory_reason = f"gateway_list_{error_category(err)}"
                rate_limit = err if isinstance(err, RateLimited) else None
                gateway_error = err
            self.ensure_current()
            gateway = next(
                (item for item in gateways if item.gateway_id == gateway_id), None
            )
            if gateway is None:
                if inventory_reason is None:
                    # P50: perhaps the gateway was exchanged; decided from
                    # this list alone, without another request.
                    await self._sync_exchange_issue(gateways, inventory)
                raise SourceFailure()
            if inventory_reason is None:
                inventory.remember_gateways((gateway,))
                from .gateway_exchange import clear_exchange_issue

                clear_exchange_issue(
                    connection.hass,
                    connection.entry,
                    connection.connection_id,
                    self.binding_id,
                )
            client = PointTClient(
                async_get_clientsession(connection.hass),
                connection.tokens,
                metrics=connection.resources.metrics,
                backoff=connection.backoff,
                request_semaphore=connection.resources.semaphore,
                request_guard=self.ensure_current,
                gateway_id=gateway_id,
            )
            coordinator: ManagedPointTCoordinator | ManagedWallboxCoordinator
            if is_wallbox(gateway):
                # Wallboxes have no heating discovery or supplementary-read queue.
                connection.startup_finished(self.binding_id)
                coordinator = ManagedWallboxCoordinator(self, client, gateway)
                self.coordinator = coordinator
            else:
                coordinator = ManagedPointTCoordinator(self, client, gateway)
                self.coordinator = coordinator
                # Reuse exact registry-path recovery without its cleanup paths.
                from . import _restore_registered_startup_paths

                _restore_registered_startup_paths(
                    connection.hass, connection.entry, coordinators=(coordinator,)
                )
                coordinator.inventory = inventory
                # A retained view of this binding saves its fault baseline
                # first, so the cloud continues from it (review P49b).
                self._faults_from = await connection.runtime.async_settle_faults(
                    self.binding_id, self
                )
                self._attempt_faults = coordinator.faults
                await coordinator.async_load_fault_state()
            self.ensure_current()
            if inventory_reason is not None and isinstance(
                coordinator, ManagedPointTCoordinator
            ):
                if not _start_from_inventory(
                    coordinator, inventory, inventory_reason, rate_limit=rate_limit
                ):
                    # Keep the runtime's handling of the original error, for
                    # example its Retry-After pause.
                    assert gateway_error is not None
                    raise gateway_error
            else:
                # Sources can start after the owner is already LOADED. The HA
                # first-setup helper is reserved for global entry setup.
                await coordinator.async_refresh()
            if not coordinator.last_update_success and (
                coordinator.startup_mode == "live"
                if isinstance(coordinator, ManagedPointTCoordinator)
                else True
            ):
                if self._auth_failed.is_set():
                    raise SourceFailure(ConnectionPhase.AUTH_REQUIRED)
                # A source stopped meanwhile is no start-up failure.
                self.ensure_current()
                reason = (
                    coordinator.discovery_diagnostics.stop_reason
                    if isinstance(coordinator, ManagedPointTCoordinator)
                    else None
                )
                if not (
                    isinstance(coordinator, ManagedPointTCoordinator)
                    and _temporary(reason)
                    and _start_from_inventory(
                        coordinator, inventory, f"discovery_{reason}"
                    )
                ):
                    connection.backoff.raise_if_active()
                    # An identified wallbox has a fixed read-only sensor set.
                    # Keep those sensors unavailable and let polling recover.
                    if not isinstance(coordinator, ManagedWallboxCoordinator):
                        raise SourceFailure()
        finally:
            connection.startup_finished(self.binding_id)
        self.ensure_current()

        from .gateway_exchange import module_fingerprints

        fingerprints = module_fingerprints(connection.hass, connection.entry.entry_id)
        await fingerprints.async_load()
        self.ensure_current()

        @callback
        def updated() -> None:
            if getattr(coordinator, "local_refreshing", False):
                # Only paired local readings changed: the runtime already
                # notified about them, and the cloud data is unchanged.
                return
            if self.current():
                if context.publish(coordinator):
                    self._published = True
                # P50: the device modules as evidence for a gateway exchange.
                snapshot = (
                    (coordinator.data or {}).get("/system/info")
                    if isinstance(coordinator, ManagedPointTCoordinator)
                    else None
                )
                if (
                    snapshot is not None
                    and snapshot.available
                    and coordinator.last_update_success
                ):
                    fingerprints.observe(
                        self.binding.target_id, snapshot.resource.values
                    )

        self._remove_listener = coordinator.async_add_listener(updated)
        updated()
        await self._auth_failed.wait()
        raise SourceFailure(ConnectionPhase.AUTH_REQUIRED)

    async def _sync_exchange_issue(
        self, gateways: tuple[Gateway, ...], inventory: DiscoveryInventory
    ) -> None:
        from .gateway_exchange import module_fingerprints, sync_exchange_issue

        connection = self.connection
        fingerprints = module_fingerprints(connection.hass, connection.entry.entry_id)
        await fingerprints.async_load()
        self.ensure_current()
        gateway_id = cast(
            str, cast(JsonObject, self.binding.to_data()["locator"])["value"]
        )
        stored = inventory.gateway(gateway_id)
        try:
            sync_exchange_issue(
                connection.hass,
                connection.entry,
                connection.runtime.snapshot,
                connection.connection_id,
                self.binding_id,
                gateways,
                fingerprints,
                old_gateway=stored.gateway if stored is not None else None,
            )
        except ConfigurationError:
            # Another entry's stored data cannot be read: no question now.
            _LOGGER.debug("Gateway exchange undecided: other entries unreadable")

    async def run_dormant(self, context: SourceContext) -> None:
        """P49: start a switched-off access from its stored inventory.

        Never requests anything: the connection is dormant and the context is
        never current. Kept only if a local access can show the readings.
        """
        self.context = context
        if not LocalSelection(
            self.connection.runtime, self.binding.target_id
        ).has_local_access:
            return  # nothing could show the readings; build nothing
        locator = cast(JsonObject, self.binding.to_data()["locator"])
        gateway_id = cast(str, locator["value"])
        inventory = discovery_inventory(
            self.connection.hass, self.connection.entry.entry_id
        )
        await inventory.async_load()
        if inventory.startup_gateways((gateway_id,)) is None:
            if LOCAL_PLANT_VIEW:
                await self._retain_local_plant(context, gateway_id, inventory)
                await self.close()
                return
            if gateway_id not in _OLD_INVENTORY_REPORTED:
                _OLD_INVENTORY_REPORTED.add(gateway_id)
                _LOGGER.warning(
                    "The cloud access is switched off and no inventory of the "
                    "last 30 days is stored; switch the cloud access on once to "
                    "show local readings after a restart"
                )
            return
        await self._retain_from_inventory(
            context, gateway_id, inventory, reason="cloud_switched_off"
        )
        # Like a stopped source: kept for local readings, or stopped entirely.
        await self.close()

    async def _retain_from_inventory(
        self,
        context: SourceContext,
        gateway_id: str,
        inventory: DiscoveryInventory,
        *,
        reason: str = "sign_in_required",
    ) -> None:
        from . import _announce_inventory_start, _start_from_inventory

        stored = inventory.startup_gateways((gateway_id,))
        gateway = next(
            (item for item in stored or () if item.gateway_id == gateway_id), None
        )
        if stored is None and reason != "cloud_switched_off" and LOCAL_PLANT_VIEW:
            # 2A: a sign-in is required and the inventory is older than its
            # age limit: the local readings show the plant until it is renewed.
            await self._retain_local_plant(context, gateway_id, inventory, startup=True)
            return
        if gateway is None or is_wallbox(gateway):
            return
        connection = self.connection
        client = PointTClient(
            async_get_clientsession(connection.hass),
            connection.tokens,
            metrics=connection.resources.metrics,
            backoff=connection.backoff,
            request_semaphore=connection.resources.semaphore,
            request_guard=self.ensure_current,
            gateway_id=gateway_id,
        )
        coordinator = ManagedPointTCoordinator(self, client, gateway)
        # Never polls: the cloud needs a new sign-in first.
        coordinator.update_interval = None
        # Shown from the stored inventory alone, until its age limit (2A).
        coordinator.inventory_view = True
        coordinator.inventory = inventory
        retain = (
            connection.runtime.retain_dormant
            if reason == "cloud_switched_off"
            else connection.runtime.retain_startup
        )
        if (
            not coordinator.retain_for_local()
            or not _start_from_inventory(coordinator, inventory, reason, announce=False)
            or not retain(context, coordinator)
        ):
            await coordinator.async_stop_source()
            return
        self.coordinator = coordinator
        if reason == "cloud_switched_off":
            # 5A: without a cloud, the local error list alone decides the
            # faults. Loaded once the view holds the binding's slot, so a
            # released predecessor has saved its baseline (review P49b).
            coordinator.faults.local_authority = True
            await coordinator.async_load_fault_state()
        # Announced only once the view is really kept (review P49).
        _announce_inventory_start(reason, coordinator.startup_age_seconds)

    async def run_local_plant(self, context: SourceContext) -> None:
        """2A: replace an expired inventory view of a cloud that is not
        available (sign-in required, outage) by the local plant view.

        Never requests anything: the connection is dormant.
        """
        self.context = context
        locator = cast(JsonObject, self.binding.to_data()["locator"])
        gateway_id = cast(str, locator["value"])
        inventory = discovery_inventory(
            self.connection.hass, self.connection.entry.entry_id
        )
        await inventory.async_load()
        await self._retain_local_plant(context, gateway_id, inventory, startup=True)
        await self.close()

    async def _retain_local_plant(
        self,
        context: SourceContext,
        gateway_id: str,
        inventory: DiscoveryInventory,
        *,
        startup: bool = False,
    ) -> None:
        """P49b: the plant's entities from its local readings alone.

        Never requests anything. Readings of paths the cloud offered (the
        recorded entities of this binding and the stored inventory) keep the
        cloud's IDs; registered local-only sensors keep theirs.
        """
        from .managed_local_plant import async_build_local_plant

        connection = self.connection
        if connection.runtime.retained_data(self.binding_id) is not None:
            return  # a view exists (for example from an earlier retry)
        coordinator = await async_build_local_plant(self, gateway_id, inventory)
        if coordinator is None:
            return
        retain = (
            connection.runtime.retain_startup
            if startup
            else connection.runtime.retain_local_plant
        )
        if not coordinator.retain_for_local() or not retain(context, coordinator):
            # Never writes the baseline another view of the binding keeps.
            coordinator.faults.discard()
            await coordinator.async_stop_source()
            return
        self.coordinator = coordinator
        if startup:
            _LOGGER.warning(
                "The cloud is not available and no inventory of the last 30 "
                "days is stored; showing the plant's local readings until it "
                "answers again"
            )
        else:
            _LOGGER.info(
                "The cloud access is switched off and no recent inventory is "
                "stored; showing the plant's local readings"
            )

    async def close(self) -> None:
        try:
            await self._stop()
        finally:
            await self._resume_faults()

    async def _resume_faults(self) -> None:
        """An attempt that never published gives the fault baseline back.

        Runs after this attempt's own coordinator saved its copy, so the
        retained view's save is the last write (review P49b).
        """
        view, self._faults_from = self._faults_from, None
        attempt, self._attempt_faults = self._attempt_faults, None
        resume = getattr(view, "async_resume_faults", None)
        if resume is None or self._published:
            return
        try:
            if attempt is not None:
                # Even if this attempt's stop failed or timed out: its copy
                # is saved now and never written again, so it cannot replace
                # the view's baseline later.
                await attempt.async_hand_over()
            await resume(self)
        except Exception:
            _LOGGER.error("A fault baseline could not be taken back")

    async def _stop(self) -> None:
        self._closed = True
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        coordinator = self.coordinator
        if (
            coordinator is None
            and self.context is not None
            and self._inventory is not None
            and self._gateway_id is not None
            and self.connection.runtime.connection_phase(self.connection.connection_id)
            is ConnectionPhase.AUTH_REQUIRED
        ):
            # A sibling stopped by the shared sign-in failure before it could
            # start from its inventory (review P40).
            await self._retain_from_inventory(
                self.context, self._gateway_id, self._inventory
            )
            coordinator = self.coordinator
        if coordinator is None:
            return
        if isinstance(
            coordinator, ManagedPointTCoordinator
        ) and self.connection.runtime.is_retained(self.binding_id, coordinator):
            await coordinator.async_stop_source(retain=True)
            if (
                LOCAL_PLANT_VIEW
                and coordinator.inventory_view
                and self.connection.runtime.is_retained(self.binding_id, coordinator)
            ):
                # S1/2A: after its 30 days the local plant view takes over.
                coordinator.schedule_inventory_expiry()
        else:
            await coordinator.async_stop_source()


class ManagedPointTCoordinator(
    BoschBuderusDataUpdateCoordinator,
    SourceOwnedCoordinator[dict[str, ResourceSnapshot]],
):
    """Scope publications and reauth without changing the legacy coordinator path.

    B04: the source stops it (``async_stop_source``), not the entry's unload.
    """

    def __init__(
        self, source: PointTSource, client: PointTClient, gateway: Gateway
    ) -> None:
        self.source = source
        self._updates = SourceUpdates(source)
        connection = source.connection
        super().__init__(
            connection.hass,
            client,
            gateway,
            connection.entry,
            connection.configuration.polling_profile(),
            startup_session=connection.startup,
            connection_id=connection.connection_id,
            target_id=source.binding.target_id,
        )
        # Existing entities may display paired local readings of the same target.
        self.local_selection = LocalSelection(
            connection.runtime, source.binding.target_id
        )
        self.counter_guard = CounterGuard(
            counter_source_store(connection.hass),
            connection.runtime.snapshot.owner_id,
            source.binding.target_id,
        )
        self._local_fault_failure = RepeatedFailure(
            _LOGGER, "The local error list could not be applied"
        )
        self._remove_local_listener: Callable[[], None] | None = (
            connection.runtime.subscribe(self._local_updated)
        )
        self._inventory_expiry: CALLBACK_TYPE | None = None
        # A retained view built from the stored inventory alone (never polls).
        self.inventory_view = False
        # A1: the stored fault baseline is loaded before the view holds.
        self._hold_loading = False
        self._hold_task: asyncio.Task[None] | None = None
        # True while entities refresh for new paired local readings.
        self.local_refreshing = False

    def schedule_inventory_expiry(self, *, recheck: bool = False) -> None:
        """P49b (S1): when the stored inventory of this dormant view passes
        its age limit, the binding's view is built again (then from the local
        readings). Nothing confirms the inventory while the access is off."""
        self._cancel_inventory_expiry()
        inventory = self.inventory
        expiry = (
            inventory.startup_expiry(self.gateway.gateway_id)
            if inventory is not None
            else None
        )
        if expiry is None:
            return
        due = expiry + INVENTORY_EXPIRY_MARGIN
        if recheck:
            # Never a tight loop, also if the clocks disagree.
            due = max(due, dt_util.utcnow() + INVENTORY_RECHECK)
        self._inventory_expiry = async_track_point_in_utc_time(
            self.hass, self._inventory_expired, due
        )

    @callback
    def _inventory_expired(self, _now: datetime) -> None:
        self._inventory_expiry = None
        source = self.source
        runtime = source.connection.runtime
        if self.inventory is None or not runtime.is_retained(source.binding_id, self):
            return
        if self.inventory.startup_gateways((self.gateway.gateway_id,)) is not None:
            self.schedule_inventory_expiry(recheck=True)  # still valid
            return
        if not runtime.expire_dormant(source.binding_id, self):
            # Not now (for example while a new source starts): later again.
            self.schedule_inventory_expiry(recheck=True)

    def _cancel_inventory_expiry(self) -> None:
        _cancel_inventory_expiry(self)

    @callback
    def _local_updated(self) -> None:
        """Refresh entities as soon as new eligible local readings arrive."""
        selection = self.local_selection
        if not (self.source.current() or self.local_retained) or selection is None:
            return
        faults_changed = False
        try:
            faults_changed = self._local_faults_updated(selection)
        except Exception as error:
            # Fault tracking must never stop local values from updating.
            # C61 (PRIV-2): logged once with the error class only.
            self._local_fault_failure.failed(error)
        else:
            self._local_fault_failure.succeeded()
        if selection.changed() or faults_changed:
            self.local_refreshing = True
            try:
                self.async_update_listeners()
            finally:
                self.local_refreshing = False

    def _local_faults_updated(self, selection: LocalSelection) -> bool:
        """P42: the local error list feeds the same fault tracker.

        True when the fault state changed, so the fault entities refresh.
        """
        if not LOCAL_FAULT_SOURCE:
            return False
        from .faults import local_device_errors, local_fault_resource

        before = self.faults.state_version
        if not selection.has_local_access:
            # Removed or switched off: the cloud decides alone again.
            self.faults.detach_local()
            self.faults.process_device_errors({})
        else:
            ready, observation, interval = selection.fault_reading()
            self.faults.process_local(
                local_fault_resource(observation) if observation is not None else None,
                reading=getattr(observation, "observed_monotonic", None),
                ready=ready,
                interval=interval,
            )
            # B3: the room devices' own error lists, shown next to the faults.
            self.faults.process_device_errors(
                dict(
                    parsed
                    for item in selection.device_error_lists()
                    if (parsed := local_device_errors(item)) is not None
                )
            )
        return self.faults.state_version != before

    async def async_hand_over_faults(self, owner: object | None = None) -> None:
        """A new source of the binding starts: it loads the fault baseline
        a view without a cloud (5A, A1) kept; local values stay until it
        publishes (P49b)."""
        if self.faults.local_authority:
            await self.faults.async_hand_over(owner)

    @callback
    def hold_without_cloud(self) -> None:
        """A1: the cloud access was switched off while this view is kept.

        Like the dormant view of a switched-off access (5A), the local error
        list alone decides the faults from now on: only faults it reported
        can end, a fault only the cloud knew stays pending, and the fault
        entities show a state only while the list is current. The tracker
        keeps its baseline, so switching the access on again continues it.
        """
        if self.faults.local_authority or self._hold_loading:
            return
        if not self.faults.baseline_known:
            # A view started from the inventory for a sign-in never loaded
            # the stored baseline: load it first; until then the local list
            # decides nothing (review A1).
            self._hold_loading = True
            self._hold_task = self.hass.async_create_background_task(
                self._async_hold_after_load(),
                "home_comfort_fault_baseline",
                eager_start=False,
            )
            return
        self._hold()

    async def _async_hold_after_load(self) -> None:
        try:
            await self.async_load_fault_state()
        except Exception:
            _LOGGER.error("The fault baseline could not be loaded")
            return
        finally:
            self._hold_loading = False
        runtime = self.source.connection.runtime
        connection_id = self.source.connection.connection_id
        if (
            self.faults.baseline_known
            and runtime.is_retained(self.source.binding_id, self)
            and connection_id is not None
            and runtime.connection_phase(connection_id) is ConnectionPhase.DISABLED
        ):
            self._hold()
            runtime.view_changed()  # the notification follows it now (A2)
        elif self.faults.baseline_known:
            # Switched on again (or released) meanwhile: the starting cloud
            # owns the baseline; this view never writes the copy it loaded.
            self.faults.discard()

    def _hold(self) -> None:
        self.faults.local_authority = True
        self.faults.mark_unavailable()  # the cloud's evidence ends here
        if self.local_retained:
            DataUpdateCoordinator.async_update_listeners(self)

    async def async_resume_faults(self, owner: object | None = None) -> None:
        """The new source did not take over: this view keeps the baseline."""
        await self.faults.async_resume(owner)

    def retain_for_local(self) -> bool:
        """P40: worth keeping after the cloud stopped (a local access exists)."""
        selection = self.local_selection
        return (
            self._remove_local_listener is not None
            and selection is not None
            and selection.has_local_access
        )

    def _request_reauthentication(self) -> None:
        self.source.authentication_failed()

    def _cloud_auth_required(self) -> bool:
        runtime = self.source.connection.runtime
        return (
            runtime.connection_phase(self.source.connection.connection_id)
            is ConnectionPhase.AUTH_REQUIRED
        )

    def _cloud_source_stopped(self) -> bool:
        return not self.source.current()

    async def _async_update_data(self) -> dict[str, ResourceSnapshot]:
        return await self._updates.run(super()._async_update_data)

    @callback
    def async_set_updated_data(self, data: dict[str, ResourceSnapshot]) -> None:
        if self.source.current():
            super().async_set_updated_data(data)

    @callback
    def async_update_listeners(self) -> None:
        if self.source.current() or self.local_retained:
            super().async_update_listeners()

    async def async_release_retention(self) -> None:
        """The retained view ends (cloud back, access removed or unloaded)."""
        _cancel_inventory_expiry(self)
        _cancel_hold(self)
        if not self.local_retained:
            return
        self.local_retained = False
        if self._remove_local_listener is not None:
            self._remove_local_listener()
            self._remove_local_listener = None
        DataUpdateCoordinator.async_update_listeners(self)
        await self.faults.async_close()

    async def async_stop_source(self, *, retain: bool = False) -> None:
        """Stop cloud work; ``retain`` keeps local readings flowing (P40)."""
        # Set before the first await, so a release during the stop is not lost.
        self.local_retained = retain
        _cancel_inventory_expiry(self)
        if not retain:
            _cancel_hold(self)
        if self._remove_local_listener is not None and not retain:
            self._remove_local_listener()
            self._remove_local_listener = None
        tasks = tuple(self._manual_setpoint_tasks.values())
        self.cancel_manual_setpoint_recovery()
        await self.async_shutdown()
        await self._updates.stop(tasks)
        self.startup_data.record_main_cycle(successful=False)
        self.faults.mark_unavailable()
        self.data = {
            key: replace(
                value,
                available=False,
                freshness=Freshness.STALE,
                last_error_category=SOURCE_STOPPED_CATEGORY,
                ended=True,
            )
            for key, value in (self.data or {}).items()
        }
        # A stopped source shows no counter, also not a previously decided one.
        self._counter_views = {}
        if self.counter_guard is not None:
            self.counter_guard.forget_shown()
        self.last_update_success = False
        self.last_exception = UpdateFailed("PointT source is unavailable")
        if retain and not self.local_retained:
            return  # released while stopping; the release closed everything
        DataUpdateCoordinator.async_update_listeners(self)
        if not retain:
            await self.faults.async_close()


def _cancel_hold(coordinator: BoschBuderusDataUpdateCoordinator) -> None:
    """The view ends: a fault baseline still loading for it is not needed.

    Safe for every coordinator, also one that never started such a load.
    """
    task = getattr(coordinator, "_hold_task", None)
    if task is not None:
        coordinator._hold_task = None  # type: ignore[attr-defined]
        if not task.done():
            task.cancel()


def _cancel_inventory_expiry(coordinator: BoschBuderusDataUpdateCoordinator) -> None:
    """End a pending inventory expiry timer (P49b), if the view has one."""
    unsubscribe = getattr(coordinator, "_inventory_expiry", None)
    if unsubscribe is not None:
        unsubscribe()
        coordinator._inventory_expiry = None  # type: ignore[attr-defined]
