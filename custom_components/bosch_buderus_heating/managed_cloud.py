"""Shared cloud login and independent PointT and additional-cloud backends."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auth_lifecycle import CloudRequestResources, connection_resources, entry_auth
from .configuration import StoredBinding, StoredConnection
from .configuration_access import EntryConfiguration
from .configuration_changes import RequestGeneration
from .configuration_management import section
from .connection_runtime import (
    ConnectionPhase,
    ConnectionRuntime,
    SourceContext,
    SourceDriver,
    SourceFailure,
)
from .managed_bacon import BaconPool, BaconSource
from .pointt import Gateway, OAuthClient, PointTClient
from .pointt.exceptions import SourceStopped
from .pointt.transport import RateLimitBackoff
from .startup_data import StartupSession

if TYPE_CHECKING:
    from .pointt import TokenManager


class _DormantTokens:
    """The sign-in of a switched-off access is never used (P49)."""

    async def get_access_token(self, *args: object, **kwargs: object) -> str:
        raise SourceStopped()


class CloudDriverFactory:
    """Keep request accounting and concurrency shared across all account targets."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        runtime: Callable[[], ConnectionRuntime],
    ) -> None:
        self.hass, self.entry, self.runtime = hass, entry, runtime

    def __call__(self, connection_id: str, record: StoredConnection) -> CloudConnection:
        runtime = self.runtime()
        resources = connection_resources(self.hass, self.entry.entry_id, connection_id)
        return CloudConnection(self.hass, self.entry, runtime, connection_id, resources)

    async def dormant_view(
        self, binding_id: str, binding: StoredBinding, context: SourceContext
    ) -> None:
        """P49: the view of a switched-off access, built without any request."""
        if binding.adapter != "pointt":
            return
        runtime = self.runtime()
        connection_id = context.generation.connection_id
        resources = connection_resources(self.hass, self.entry.entry_id, connection_id)
        connection = CloudConnection(
            self.hass,
            self.entry,
            runtime,
            connection_id,
            resources,
            dormant=context.generation,
        )
        from .managed_pointt import PointTSource

        await PointTSource(connection, binding_id, binding).run_dormant(context)

    async def target_plant_view(self, target_id: str, gateway_id: str) -> Any:
        """R1: the local plant view of a system whose cloud access was
        removed. Detached from any connection: no request resources are
        registered, nothing is requested and no sign-in is used."""
        from .discovery_inventory import discovery_inventory
        from .managed_local_plant import TargetHost, async_build_local_plant

        inventory = discovery_inventory(self.hass, self.entry.entry_id)
        await inventory.async_load()
        runtime = self.runtime()
        host = TargetHost(self.hass, self.entry, runtime, target_id, gateway_id)
        target = runtime.snapshot.targets.get(target_id)
        details = target.to_data().get("local_details") if target else None
        details = details if isinstance(details, dict) else {}
        model, brand = details.get("model"), details.get("brand")
        return await async_build_local_plant(
            host,
            gateway_id,
            inventory,
            # Step 7: what the local setup read; never an identifier.
            model=model if isinstance(model, str) else None,
            brand=brand if isinstance(brand, str) else None,
        )

    async def local_plant_view(
        self, binding_id: str, binding: StoredBinding, context: SourceContext
    ) -> None:
        """2A: the local plant view of a cloud that is not available,
        built without any request, the sign-in or the backoff."""
        if binding.adapter != "pointt":
            return
        runtime = self.runtime()
        connection_id = context.generation.connection_id
        resources = connection_resources(self.hass, self.entry.entry_id, connection_id)
        connection = CloudConnection(
            self.hass,
            self.entry,
            runtime,
            connection_id,
            resources,
            dormant=context.generation,
        )
        from .managed_pointt import PointTSource

        await PointTSource(connection, binding_id, binding).run_local_plant(context)


class CloudConnection:
    """One shared cloud login; gateway-list failure stays in the PointT backend."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        runtime: ConnectionRuntime,
        connection_id: str,
        resources: CloudRequestResources,
        *,
        dormant: RequestGeneration | None = None,
    ) -> None:
        self.hass, self.entry, self.runtime = hass, entry, runtime
        self.connection_id, self.resources = connection_id, resources
        self.configuration = EntryConfiguration(entry, connection_id)
        # P49: a dormant connection of a switched-off access never requests.
        self.dormant = dormant is not None
        self.generation = (
            dormant
            if dormant is not None
            else runtime.capture_connection(connection_id)
        )
        session = async_get_clientsession(hass)
        if self.dormant:
            # Never touches the stored sign-in, its history or the shared
            # account backoff (review P49).
            # Duck-typed: the client only asks it for an access token.
            self.tokens = cast("TokenManager", _DormantTokens())
            self.backoff = RateLimitBackoff()
        else:
            self.tokens, self.backoff = entry_auth(
                hass, entry, OAuthClient(session), connection_id=connection_id
            )
        self.client = PointTClient(
            session,
            self.tokens,
            metrics=resources.metrics,
            backoff=self.backoff,
            request_guard=self.ensure_current,
            request_semaphore=resources.semaphore,
        )
        self.startup = StartupSession(
            tuple(self.configuration.cloud_data()["gateway_ids"]),
            options={"polling_profile": self.configuration.polling_profile().value},
        )
        options = section(runtime.snapshot.to_options(), "bindings")
        self._pending_startup = {
            key
            for key, binding in runtime.snapshot.bindings.items()
            if binding.connection_id == connection_id
            and binding.adapter == "pointt"
            and options[key]["enabled"]
        }
        self._gateways: asyncio.Task[tuple[Gateway, ...]] | None = None
        self._gateways_expiry = 0.0
        self._closed = False
        self.additional = BaconPool(self)
        self._remove_listener: Callable[[], None] | None = None

    def ensure_current(self) -> None:
        if (
            self.dormant
            or self._closed
            or not self.runtime.current_connection(self.generation)
        ):
            raise SourceStopped()

    async def connect(self) -> None:
        # Backend discovery belongs to source work; it cannot block other adapters.
        self.ensure_current()
        # C68: follows the configuration, not local readings.
        self._remove_listener = self.runtime.subscribe(
            self._update_startup, local_values=False
        )

    def source(self, binding_id: str, binding: StoredBinding) -> SourceDriver:
        if binding.adapter == "pointt":
            from .managed_pointt import PointTSource

            return PointTSource(self, binding_id, binding)
        if binding.adapter == "bacon":
            return BaconSource(self.additional, binding_id, binding)
        raise SourceFailure(ConnectionPhase.UNSUPPORTED)

    async def gateways(self) -> tuple[Gateway, ...]:
        self.ensure_current()
        if self._gateways is None or (
            self._gateways.done() and monotonic() >= self._gateways_expiry
        ):
            self._gateways_expiry = monotonic() + 60
            self._gateways = self.hass.async_create_background_task(
                self.client.get_gateways(),
                "home_comfort_gateway_list",
                eager_start=False,
            )
            # Retrieve errors even if every waiter was cancelled while auth rotated.
            self._gateways.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
        # Waiting must not cancel the shared request when one source stops.
        # Its explicit done callback retrieves errors after the last waiter exits.
        task = self._gateways
        await asyncio.wait((task,))
        result = task.result()
        self.ensure_current()
        return result

    def startup_finished(self, binding_id: str) -> None:
        self._pending_startup.discard(binding_id)
        self._update_startup()

    def _update_startup(self) -> None:
        options = section(self.runtime.snapshot.to_options(), "bindings")
        self._pending_startup.intersection_update(
            key
            for key, binding in self.runtime.snapshot.bindings.items()
            if binding.connection_id == self.connection_id
            and binding.adapter == "pointt"
            and options[key]["enabled"]
        )
        if not self._pending_startup:
            self.startup.mark_setup_complete()

    async def close(self) -> None:
        self._closed = True
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        self.startup.close()
        if self._gateways is not None and not self._gateways.done():
            self._gateways.cancel()
        try:
            await self.additional.close()
        finally:
            if self._gateways is not None:
                await asyncio.gather(self._gateways, return_exceptions=True)
