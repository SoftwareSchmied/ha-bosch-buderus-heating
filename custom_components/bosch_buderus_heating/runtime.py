"""Runtime data owned by one config entry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .coordinator import BoschBuderusDataUpdateCoordinator
from .pointt import Gateway, PointTClient, TokenManager
from .startup_data import StartupSession

if TYPE_CHECKING:
    from .aircon import AirconCoordinator
    from .bacon.session import BaconAccount
    from .notifications import FaultNotifications
    from .wallbox import WallboxCoordinator


@dataclass(slots=True)
class BoschBuderusRuntimeData:
    """Live clients and selected gateways for one account."""

    client: PointTClient
    token_manager: TokenManager
    gateways: tuple[Gateway, ...]
    coordinators: tuple[BoschBuderusDataUpdateCoordinator, ...]
    notifications: tuple[FaultNotifications, ...] = ()
    startup: StartupSession | None = None
    bacon: BaconAccount | None = None
    wallboxes: tuple[WallboxCoordinator, ...] = ()
    aircons: tuple[AirconCoordinator, ...] = ()
