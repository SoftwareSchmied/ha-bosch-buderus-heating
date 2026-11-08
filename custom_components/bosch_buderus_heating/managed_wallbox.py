"""Read-only wallbox coordinator within a managed PointT source."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .managed_updates import SourceUpdates
from .pointt import Gateway, PointTClient
from .wallbox import SECTIONS, Section, SectionSnapshot, WallboxCoordinator

if TYPE_CHECKING:
    from .managed_pointt import PointTSource


class ManagedWallboxCoordinator(WallboxCoordinator):
    """Keep wallbox parsing and polling separate from source ownership."""

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

    def section_fresh(self, section: Section) -> bool:
        return self.source.current() and super().section_fresh(section)

    async def _async_update_data(self) -> dict[Section, SectionSnapshot]:
        return await self._updates.run(super()._async_update_data)

    @callback
    def async_set_updated_data(self, data: dict[Section, SectionSnapshot]) -> None:
        if self.source.current():
            super().async_set_updated_data(data)

    @callback
    def async_update_listeners(self) -> None:
        if self.source.current():
            super().async_update_listeners()

    async def async_stop_source(self) -> None:
        await self.async_shutdown()
        await self._updates.stop()
        self.data = {
            section: SectionSnapshot(error="source_stopped") for section in SECTIONS
        }
        self.last_update_success = False
        self.last_exception = UpdateFailed("Wallbox source is unavailable")
        DataUpdateCoordinator.async_update_listeners(self)
