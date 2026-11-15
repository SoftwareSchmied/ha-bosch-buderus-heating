"""Air-conditioner coordinator within a managed PointT source."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aircon import AirconCoordinator, AirconSnapshot
from .coordinator import SourceOwnedCoordinator
from .managed_updates import SourceUpdates
from .pointt import Gateway, PointTClient

if TYPE_CHECKING:
    from .managed_pointt import PointTSource


class ManagedAirconCoordinator(
    AirconCoordinator, SourceOwnedCoordinator[AirconSnapshot]
):
    """Keep air-conditioner polling separate from source ownership.

    The source stops it (``async_stop_source``), not the entry's unload.
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
            connection_id=connection.connection_id,
        )

    def current(self) -> bool:
        return self.source.current() and super().current()

    def _source_stopped(self) -> bool:
        return not self.source.current()

    async def _async_update_data(self) -> AirconSnapshot:
        return await self._updates.run(super()._async_update_data)

    @callback
    def async_set_updated_data(self, data: AirconSnapshot) -> None:
        if self.source.current():
            super().async_set_updated_data(data)

    @callback
    def async_update_listeners(self) -> None:
        if self.source.current():
            super().async_update_listeners()

    async def async_stop_source(self) -> None:
        await self.async_shutdown()
        await self._updates.stop()
        self.error = "source_stopped"
        self.last_update_success = False
        self.last_exception = UpdateFailed("Air conditioner source is unavailable")
        DataUpdateCoordinator.async_update_listeners(self)
