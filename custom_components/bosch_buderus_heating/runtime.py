"""Runtime data owned by one config entry."""

from __future__ import annotations

from dataclasses import dataclass

from .coordinator import BoschBuderusDataUpdateCoordinator
from .pointt import Gateway, PointTClient, TokenManager


@dataclass(slots=True)
class BoschBuderusRuntimeData:
    """Live clients and selected gateways for one account."""

    client: PointTClient
    token_manager: TokenManager
    gateways: tuple[Gateway, ...]
    coordinators: tuple[BoschBuderusDataUpdateCoordinator, ...]
