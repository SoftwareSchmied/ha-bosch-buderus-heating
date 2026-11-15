"""Air conditioners on a PointT gateway (``deviceType`` ``rac``).

A separate backend from the air conditioners of the second cloud
(``bacon``): the unit is a gateway of the PointT account list and is read
and written through normal PointT resources below ``/airConditioning``.
One bulk request per cycle reads a fixed list of paths; nothing runs the
heating discovery. Writes go through the shared write service with the slow
read-back schedule, one PUT per action and no retry.

Expected, untested on a real installation: the paths and their meaning come
from the vendor app.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Final, Literal

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .aircon_policies import (
    ADVANCED_FUNCTIONS,
    POLL_PATHS,
    STANDARD_FUNCTIONS,
    WRITE_GUARDS,
)
from .configuration_access import EntryConfiguration
from .const import DOMAIN, PollingProfile
from .pointt import AuthenticationError, Gateway, PointTClient, PointTError, Resource
from .pointt.const import DEFAULT_TOTAL_TIMEOUT
from .pointt.exceptions import RateLimited, SourceStopped
from .write_policies import RRC_READ_BACK_DELAYS
from .write_window import send_window
from .writes import (
    EnumWritePolicy,
    NumberWritePolicy,
    WriteBusy,
    WriteDeadlineExceeded,
    WriteService,
    WriteValidationError,
    assess_control,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import BoschBuderusConfigEntry

_LOGGER = logging.getLogger(__name__)

AIRCON_DEVICE_TYPE: Final = "rac"
BUS_PATH: Final = "/system/bus"
MODEL_PATH: Final = "/system/appliance/model"
DETECTION_PATHS: Final = (BUS_PATH, MODEL_PATH)
MIDEA_GATEWAY_CONNECTOR_MODEL: Final = "rac-midea-gatewayconnector"

type SystemType = Literal["midea", "midea_gateway_connector", "sharp"]
SYSTEM_TYPES: Final[tuple[SystemType, ...]] = (
    "midea",
    "midea_gateway_connector",
    "sharp",
)

# The unit applies a change late (the app discards its first reading after
# a command): read back after 1, 2, 4 and 8 seconds, never write twice.
READ_BACK_DELAYS: Final = RRC_READ_BACK_DELAYS
# A write waits so long for a running poll, then is reported busy unsent.
WRITE_LOCK_TIMEOUT: Final = 60.0
# The fresh reading, the PUT and the read-back of one action.
WRITE_DEADLINE_SECONDS: Final = 2 * DEFAULT_TOTAL_TIMEOUT + sum(READ_BACK_DELAYS)


def is_aircon_gateway(gateway: Gateway | None) -> bool:
    """Use the reported transport family only, never a retail name."""
    return (
        gateway is not None
        and isinstance(gateway.device_type, str)
        and gateway.device_type.strip().casefold() == AIRCON_DEVICE_TYPE
    )


def system_type(bus: object, model: object) -> SystemType | None:
    """The unit family from ``/system/bus`` and ``/system/appliance/model``.

    ``Midea`` with the connector model is the gateway connector, any other
    Midea model (also an empty one) a Midea unit, ``Echonet`` a Sharp unit;
    ``No_Bus`` or anything else is no air conditioner (nothing is guessed).
    """
    if bus == "Midea":
        return (
            "midea_gateway_connector"
            if model == MIDEA_GATEWAY_CONNECTOR_MODEL
            else "midea"
        )
    if bus == "Echonet":
        return "sharp"
    return None


@dataclass(frozen=True, slots=True)
class AirconSnapshot:
    """One cycle: the readable resources and why the others are missing."""

    resources: Mapping[str, Resource] = field(default_factory=dict)
    missing: Mapping[str, str] = field(default_factory=dict)
    received: float = 0.0


class AirconCoordinator(DataUpdateCoordinator[AirconSnapshot]):
    """Poll one air conditioner and write its controls."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PointTClient,
        gateway: Gateway,
        entry: BoschBuderusConfigEntry,
        *,
        connection_id: str | None = None,
    ) -> None:
        self.connection_id = connection_id
        friendly = (
            EntryConfiguration(entry, connection_id).polling_profile()
            is PollingProfile.CLOUD_FRIENDLY
        )
        self.interval = 120 if friendly else 60
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} air conditioner",
            update_interval=timedelta(seconds=self.interval),
            config_entry=entry,
        )
        self.client = client
        self.gateway = gateway
        self.system: SystemType | None = None
        # "pending" until both detection paths answered, then "detected" or
        # "no_air_conditioner" (kept until the next start, as the app).
        self.detection = "pending"
        self.error: str | None = None
        self.data = AirconSnapshot()
        self._lock = asyncio.Lock()
        self._write_service = WriteService(
            client, read_back_schedule=READ_BACK_DELAYS
        )
        self.writes_sent = 0
        self.writes_refused = 0

    # Readings

    def current(self) -> bool:
        """Whether the last cycle succeeded and is not older than two."""
        return bool(
            self.last_update_success
            and self.system is not None
            and self.data.received
            and monotonic() - self.data.received <= 2 * self.interval
        )

    def resource(self, path: str) -> Resource | None:
        """The current reading of ``path``; None when missing or outdated."""
        return self.data.resources.get(path) if self.current() else None

    def value(self, path: str) -> object:
        resource = self.resource(path)
        return resource.value if resource is not None and resource.has_value else None

    def referenced(self, path: str) -> bool:
        """Whether the unit names the function (or does not list any).

        Without a readable function list the readable leaf decides alone.
        """
        lists = [
            listing
            for listing in (
                self.data.resources.get(STANDARD_FUNCTIONS),
                self.data.resources.get(ADVANCED_FUNCTIONS),
            )
            if listing is not None and listing.references
        ]
        if not lists:
            return True
        return any(
            reference.path == path for listing in lists for reference in listing.references
        )

    def control_ready(self, path: str) -> bool:
        """A current, referenced and writable control with a released policy."""
        resource = self.resource(path)
        return (
            resource is not None
            and self.referenced(path)
            and assess_control(resource).eligible
        )

    async def _async_update_data(self) -> AirconSnapshot:
        async with self._lock:
            return await self._read()

    async def _read(self) -> AirconSnapshot:
        try:
            if self.detection == "pending":
                await self._detect()
            if self.system is None:
                self.error = None
                return AirconSnapshot(received=monotonic())
            results = await self.client.get_resources_bulk(
                self.gateway.gateway_id, POLL_PATHS
            )
        except AuthenticationError as err:
            self.error = "authentication"
            raise ConfigEntryAuthFailed("Air conditioner sign-in failed") from err
        except SourceStopped:
            raise
        except PointTError as err:
            self.error = "rate_limited" if isinstance(err, RateLimited) else (
                "request_failed"
            )
            raise UpdateFailed("Air conditioner request failed") from err
        resources: dict[str, Resource] = {}
        missing: dict[str, str] = {}
        for item in results:
            if item.status == 429 or isinstance(item.error, RateLimited):
                self.error = "rate_limited"
                raise UpdateFailed("Air conditioner requests are paused")
            if item.ok and item.resource is not None and item.path in POLL_PATHS:
                resources[item.path] = item.resource
            elif item.path in POLL_PATHS:
                missing[item.path] = (
                    f"http_{item.status}" if item.status else "invalid_payload"
                )
        if not resources:
            # Offline or unreachable: every entity becomes unavailable and the
            # unit is not detected again (as the app).
            self.error = "no_reading"
            raise UpdateFailed("The air conditioner answered no path")
        self.error = None
        return AirconSnapshot(resources, missing, monotonic())

    async def _detect(self) -> None:
        results = await self.client.get_resources_bulk(
            self.gateway.gateway_id, DETECTION_PATHS
        )
        read = {
            item.path: item.resource.value
            for item in results
            if item.ok and item.resource is not None and item.resource.has_value
        }
        if any(item.status == 429 for item in results):
            raise RateLimited(None)
        if BUS_PATH not in read:
            # Not answered: detected again in the next cycle.
            if any(
                item.path == BUS_PATH and item.status in (403, 404)
                for item in results
            ):
                self.detection = "no_air_conditioner"
            return
        self.system = system_type(read.get(BUS_PATH), read.get(MODEL_PATH))
        self.detection = "detected" if self.system else "no_air_conditioner"

    # Writes

    def control_status(self) -> tuple[str | None, datetime | None]:
        """Why a command cannot be sent now (``control.cloud_down_error``)."""
        if self._source_stopped():
            return "cloud_stopped", None
        remaining = getattr(getattr(self.client, "backoff", None), "remaining", 0.0)
        if isinstance(remaining, int | float) and remaining > 0:
            return "rate_limited", dt_util.utcnow() + timedelta(seconds=remaining)
        if not self.last_update_success:
            return "cloud_unreachable", None
        return None, None

    def _source_stopped(self) -> bool:
        return False

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        try:
            async with asyncio.timeout(WRITE_LOCK_TIMEOUT):
                await self._lock.acquire()
        except TimeoutError as err:
            raise WriteBusy("A reading held the unit; the change was not sent") from err
        try:
            with send_window(WRITE_DEADLINE_SECONDS) as window:
                try:
                    async with asyncio.timeout(WRITE_DEADLINE_SECONDS) as scope:
                        yield
                except TimeoutError as err:
                    if not scope.expired():
                        raise
                    if window.sent:
                        raise WriteDeadlineExceeded(
                            "The change was not confirmed in time; not sent again"
                        ) from err
                    raise WriteBusy("The change could not start in time") from err
        finally:
            self._lock.release()

    async def async_write(
        self,
        path: str,
        value: str | float,
        policy: EnumWritePolicy | NumberWritePolicy,
    ) -> Resource:
        """Read the control (and what it depends on) fresh, send it once and
        confirm it by reading back. Nothing is sent again."""
        if not self.current() or path not in self.data.resources:
            raise WriteValidationError("The control has no current reading")
        gateway_id = self.gateway.gateway_id
        async with self._transaction():
            guards = WRITE_GUARDS.get(path, ())
            fresh = await self._fresh(gateway_id, (path, *guards))
            resource = fresh.get(path)
            if resource is None:
                raise WriteValidationError("The control cannot be read now")
            self._publish(fresh)
            try:
                if isinstance(policy, EnumWritePolicy) and isinstance(value, str):
                    result = await self._write_service.async_write_enum(
                        gateway_id,
                        resource,
                        value,
                        policy,
                        current=resource,
                        related=fresh,
                        on_sent=self._sent,
                    )
                elif (
                    isinstance(policy, NumberWritePolicy)
                    and isinstance(value, int | float)
                    and not isinstance(value, bool)
                ):
                    result = await self._write_service.async_write_number(
                        gateway_id,
                        resource,
                        float(value),
                        policy,
                        current=resource,
                        related=fresh,
                        on_sent=self._sent,
                    )
                else:
                    raise WriteValidationError("Value does not match the control")
            except WriteValidationError:
                self.writes_refused += 1
                raise
            self._publish({path: result.resource})
        # Other values may follow a change (a mode resets the fan, for
        # example): read them again instead of trusting the cache.
        await self.async_request_refresh()
        return result.resource

    def _sent(self) -> None:
        self.writes_sent += 1

    async def _fresh(
        self, gateway_id: str, paths: tuple[str, ...]
    ) -> dict[str, Resource]:
        results = await self.client.get_resources_bulk(
            gateway_id, paths, retryable=False
        )
        read: dict[str, Resource] = {}
        for item in results:
            if item.status == 429 or isinstance(item.error, RateLimited):
                raise item.error if isinstance(item.error, RateLimited) else (
                    RateLimited(None)
                )
            if item.ok and item.resource is not None and item.path in paths:
                read[item.path] = item.resource
        return read

    def _publish(self, fresh: Mapping[str, Resource]) -> None:
        if not fresh:
            return
        resources = {**self.data.resources, **fresh}
        missing = {
            path: reason
            for path, reason in self.data.missing.items()
            if path not in fresh
        }
        self.async_set_updated_data(
            AirconSnapshot(resources, missing, self.data.received)
        )

    # Diagnostics

    def diagnostics(self) -> dict[str, object]:
        """Shape and freshness only: no values, names, serials or programs."""
        resources = self.data.resources
        functions = {
            key: len(listing.references) if listing is not None else None
            for key, listing in (
                ("standard", resources.get(STANDARD_FUNCTIONS)),
                ("advanced", resources.get(ADVANCED_FUNCTIONS)),
            )
        }
        return {
            "profile": "pointt_air_conditioner",
            "write_support": True,
            "confirmed_on_hardware": False,
            "system_type": self.system,
            "detection": self.detection,
            "error": self.error,
            "fresh": self.current(),
            "interval_seconds": self.interval,
            "readable_path_count": len(resources),
            "missing_paths": dict(sorted(self.data.missing.items())),
            "writable_paths": sorted(
                path
                for path, resource in resources.items()
                if resource.metadata.writable
            ),
            "eligible_controls": sorted(
                path for path in resources if self.control_ready(path)
            ),
            "function_references": functions,
            "writes_sent": self.writes_sent,
            "writes_refused": self.writes_refused,
        }
