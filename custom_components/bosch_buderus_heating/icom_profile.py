"""I1: the load profile of iCom gateways (KM200, KM100, KM50, MB LAN 2, ...).

The cloud passes every request of an iCom gateway on to the gateway, and
these gateways answer more slowly than the newer ones. During an outage a
request takes the full 30 s and then fails; the cloud counts it for its rate
limit all the same. This module holds the rules that apply only to gateways
whose reported device type is ``icom``; every other gateway keeps its
behaviour:

* a new cloud connection with an iCom gateway starts with the cloud-friendly
  polling profile (the user can change it in the options);
* after a cycle with a timeout the next cycle uses half the bulk size (at
  least 10 paths); after three cycles in a row without a failed batch the
  size doubles again, step by step, back to the normal 30;
* the circuit breaker pauses 5, 10, 20 and then 30 minutes while an outage
  lasts, and starts at 5 minutes again after the first usable cycle;
* a write is read back after 1, 2, 3 and 4 seconds (10 s in all).

Pure logic without Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from .pointt.const import MAX_BULK_PATHS
from .pointt.models import Gateway

ICOM_DEVICE_TYPE: Final = "icom"
ICOM_MIN_BULK_PATHS: Final = 10
ICOM_BULK_RECOVERY_CYCLES: Final = 3
# Minutes of each further opening of the breaker within one outage.
ICOM_CIRCUIT_OPEN_MINUTES: Final[tuple[int, ...]] = (5, 10, 20, 30)
ICOM_READ_BACK_DELAYS: Final[tuple[float, ...]] = (1.0, 2.0, 3.0, 4.0)


def is_icom_device_type(device_type: object) -> bool:
    """Only the reported device type counts, never a model name."""
    return (
        isinstance(device_type, str)
        and device_type.strip().casefold() == ICOM_DEVICE_TYPE
    )


def is_icom_gateway(gateway: Gateway | None) -> bool:
    return gateway is not None and is_icom_device_type(gateway.device_type)


def any_icom_gateway(gateways: Iterable[Gateway], selected: Iterable[str]) -> bool:
    """Whether a selection of a cloud listing contains an iCom gateway."""
    chosen = set(selected)
    return any(
        gateway.gateway_id in chosen and is_icom_gateway(gateway)
        for gateway in gateways
    )


@dataclass(slots=True)
class AdaptiveBulkSize:
    """Halve the bulk size after a timeout, recover it after good cycles."""

    normal: int = MAX_BULK_PATHS
    minimum: int = ICOM_MIN_BULK_PATHS
    recovery_cycles: int = ICOM_BULK_RECOVERY_CYCLES
    size: int = MAX_BULK_PATHS
    good_cycles: int = 0
    reductions: int = 0

    def record_timeout(self) -> None:
        """A cycle with a timeout: the next one uses half the size, once."""
        smaller = max(self.minimum, self.size // 2)
        if smaller < self.size:
            self.reductions += 1
        self.size = smaller
        self.good_cycles = 0

    def record_good_cycle(self) -> None:
        """A cycle in which every batch answered."""
        if self.size >= self.normal:
            self.good_cycles = 0
            return
        self.good_cycles += 1
        if self.good_cycles >= self.recovery_cycles:
            self.size = min(self.normal, self.size * 2)
            self.good_cycles = 0

    def diagnostics(self) -> dict[str, int]:
        return {
            "bulk_size": self.size,
            "bulk_size_normal": self.normal,
            "bulk_size_minimum": self.minimum,
            "good_cycles_towards_next_step": self.good_cycles,
            "bulk_size_reductions": self.reductions,
        }


@dataclass(slots=True)
class GrowingCircuitPause:
    """The pause of each further breaker opening within one outage."""

    minutes: tuple[int, ...] = ICOM_CIRCUIT_OPEN_MINUTES
    openings: int = 0

    def next_pause_seconds(self) -> float:
        pause = self.minutes[min(self.openings, len(self.minutes) - 1)] * 60.0
        self.openings += 1
        return pause

    def reset(self) -> None:
        self.openings = 0

    def diagnostics(self) -> dict[str, object]:
        return {
            "circuit_openings_in_outage": self.openings,
            "circuit_pause_minutes": list(self.minutes),
        }
