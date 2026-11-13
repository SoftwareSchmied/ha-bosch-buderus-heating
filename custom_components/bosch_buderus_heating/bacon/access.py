"""The device access contract used by existing cloud entity families."""

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .client import BaconClient
from .models import Device, Shadow


class BaconAccess(Protocol):
    @property
    def client(self) -> BaconClient: ...

    @property
    def devices(self) -> Mapping[str, Device]: ...

    @property
    def selected(self) -> tuple[str, ...]: ...

    @property
    def generation(self) -> int: ...

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]: ...

    def available(self, serial: str) -> bool: ...

    def expire_values(self, device: Device) -> None: ...

    async def write_state(
        self,
        serial: str,
        desired: dict[str, Any],
        validator: Callable[[Device, dict[str, Any]], bool],
    ) -> Shadow: ...
