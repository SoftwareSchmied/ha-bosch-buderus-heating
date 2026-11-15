"""Air conditioners on a PointT gateway (``deviceType`` ``rac``).

A separate backend from the air conditioners of the second cloud
(``bacon``): the unit is a gateway of the PointT account list and is read
and written through normal PointT resources below ``/airConditioning``.
One bulk request per cycle reads a fixed list of paths; nothing runs the
heating discovery. Writes go through the shared write service with the slow
read-back schedule, one PUT per action and no retry. The weekly programs are
read along in the same bulk request and changed through the shared program
write (``switch_program_service``): fresh reading of the program and its
configuration, one PUT of the whole week, read-back, no retry.

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
    ACTIVE_PROGRAM,
    ADVANCED_FUNCTION_PATHS,
    ADVANCED_FUNCTIONS,
    POLL_PATHS,
    PROGRAM_IDS,
    SCHEDULE_BASE,
    SCHEDULE_CONFIGURATION,
    STANDARD_FUNCTION_PATHS,
    STANDARD_FUNCTIONS,
    WRITE_GUARDS,
)
from .configuration_access import EntryConfiguration
from .const import DOMAIN, PollingProfile
from .pointt import (
    AuthenticationError,
    Gateway,
    PointTClient,
    PointTError,
    Resource,
    WriteValidationError,
)
from .pointt.const import DEFAULT_TOTAL_TIMEOUT
from .pointt.exceptions import RateLimited, SourceStopped
from .switch_program_edit import edit_issue
from .switch_program_service import (
    SwitchProgramChanged,
    SwitchProgramPlan,
    SwitchProgramService,
)
from .switch_programs import SwitchPoint, program_kind
from .write_policies import RRC_READ_BACK_DELAYS
from .write_window import send_window
from .writes import (
    EnumWritePolicy,
    NumberWritePolicy,
    WriteBusy,
    WriteDeadlineExceeded,
    WriteService,
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
# A program change reads the program and its configuration, reads the program
# once more right before the PUT, then sends and reads back.
PROGRAM_WRITE_DEADLINE_SECONDS: Final = 3 * DEFAULT_TOTAL_TIMEOUT + sum(
    READ_BACK_DELAYS
)


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


DAYS: Final = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
MAX_SWITCH_POINTS: Final = 7 * 24


def program_path(program: object) -> str:
    return f"{SCHEDULE_BASE}/{program}"


def parse_switch_points(resource: Resource | None) -> list[dict[str, object]] | None:
    """Readable switch points of a program (``switchPoints`` of a
    ``switchProgram``): day, time and the parameters the unit reports.
    Anything malformed gives None instead of a partial program."""
    points = resource.program_fields.get("switchPoints") if resource else None
    if not isinstance(points, list) or len(points) > MAX_SWITCH_POINTS:
        return None
    parsed: list[dict[str, object]] = []
    for point in points:
        if not isinstance(point, dict):
            return None
        day, minutes = point.get("dayOfWeek"), point.get("time")
        parameters = point.get("parameters")
        if (
            day not in DAYS
            or isinstance(minutes, bool)
            or not isinstance(minutes, int)
            or not 0 <= minutes <= 1440
            or not isinstance(parameters, dict)
        ):
            return None
        mode, setpoint = (
            parameters.get("operationMode"),
            parameters.get("temperatureSetpoint"),
        )
        fan = parameters.get("fanSpeed")
        parsed.append(
            {
                "day": day,
                "time": f"{minutes // 60:02d}:{minutes % 60:02d}",
                "mode": mode if isinstance(mode, str) else None,
                "setpoint": float(setpoint)
                if isinstance(setpoint, int | float) and not isinstance(setpoint, bool)
                else None,
                "fan": fan if isinstance(fan, str) else None,
            }
        )
    return parsed


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
        self._write_service = WriteService(client, read_back_schedule=READ_BACK_DELAYS)
        self._program_service = SwitchProgramService(client)
        self.writes_sent = 0
        self.writes_refused = 0
        self.program_writes: dict[str, int] = {}

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
        """Whether the unit names the function in its function list.

        The standard functions (power, mode, setpoint, fan, louvres) are
        checked against ``standardFunctions``, the five extra functions
        against ``advancedFunctions``; without a readable list the readable
        leaf decides alone. Timers, schedules and the solar state are no
        listed functions.
        """
        if path in ADVANCED_FUNCTION_PATHS:
            listing = self.data.resources.get(ADVANCED_FUNCTIONS)
        elif path in STANDARD_FUNCTION_PATHS:
            listing = self.data.resources.get(STANDARD_FUNCTIONS)
        else:
            return True
        if listing is None or not listing.references:
            return True
        return any(reference.path == path for reference in listing.references)

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
            paths = (*POLL_PATHS, *self._program_paths())
            results = await self.client.get_resources_bulk(
                self.gateway.gateway_id, paths
            )
        except AuthenticationError as err:
            self.error = "authentication"
            raise ConfigEntryAuthFailed("Air conditioner sign-in failed") from err
        except SourceStopped:
            raise
        except PointTError as err:
            self.error = (
                "rate_limited" if isinstance(err, RateLimited) else ("request_failed")
            )
            raise UpdateFailed("Air conditioner request failed") from err
        resources: dict[str, Resource] = {}
        missing: dict[str, str] = {}
        for item in results:
            if item.status == 429 or isinstance(item.error, RateLimited):
                self.error = "rate_limited"
                raise UpdateFailed("Air conditioner requests are paused")
            if item.ok and item.resource is not None and item.path in paths:
                resources[item.path] = item.resource
            elif item.path in paths:
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

    def _program_paths(self) -> tuple[str, ...]:
        """The programs the unit offered in the last cycle (the allowed values
        of the active program, at most nine), read along in the same bulk
        request: no request of their own."""
        active = self.data.resources.get(ACTIVE_PROGRAM)
        if active is None:
            return ()
        named = [*active.metadata.allowed_values, active.value]
        return tuple(
            program_path(program) for program in sorted(PROGRAM_IDS) if program in named
        )

    def program_paths(self) -> tuple[str, ...]:
        """The programs with a current reading, sorted."""
        return tuple(
            sorted(
                path
                for path in self.data.resources
                if program_kind(path) is not None and self.resource(path) is not None
            )
        )

    def program_readings(self) -> dict[str, Resource]:
        """The current readings, for the program rules."""
        return dict(self.data.resources) if self.current() else {}

    def program_active(self, path: str) -> bool | None:
        """Whether ``path`` is the active program; ``None`` when unknown."""
        value = self.value(ACTIVE_PROGRAM)
        return program_path(value) == path if value in PROGRAM_IDS else None

    def switch_points(self) -> list[dict[str, object]] | None:
        """The switch points of the active program, or None when unknown."""
        value = self.value(ACTIVE_PROGRAM)
        if value not in PROGRAM_IDS:
            return None
        return parse_switch_points(self.resource(program_path(value)))

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
                item.path == BUS_PATH and item.status in (403, 404) for item in results
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
    async def _transaction(self, deadline: float | None = None) -> AsyncIterator[None]:
        if deadline is None:
            deadline = WRITE_DEADLINE_SECONDS
        try:
            async with asyncio.timeout(WRITE_LOCK_TIMEOUT):
                await self._lock.acquire()
        except TimeoutError as err:
            raise WriteBusy("A reading held the unit; the change was not sent") from err
        try:
            with send_window(deadline) as window:
                try:
                    async with asyncio.timeout(deadline) as scope:
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

    # Weekly programs

    def _count_program(self, outcome: str) -> None:
        self.program_writes[outcome] = self.program_writes.get(outcome, 0) + 1

    async def async_plan_switch_program(
        self, path: str, plan: SwitchProgramPlan
    ) -> tuple[Resource, Resource | None]:
        """The action ``set_switch_points``: read the program and its
        configuration fresh, let ``plan`` build the week from that reading
        and write it once; ``(fresh, confirmed)``, ``confirmed`` is ``None``
        when nothing was sent."""
        return await self._async_program_write(path, None, (), plan)

    async def async_write_switch_program(
        self, expected: Resource, points: tuple[SwitchPoint, ...]
    ) -> Resource:
        """The options dialog: write the week over the program as shown."""
        _fresh, confirmed = await self._async_program_write(
            expected.path, expected, points, None
        )
        assert confirmed is not None
        return confirmed

    async def _async_program_write(
        self,
        path: str,
        expected: Resource | None,
        points: tuple[SwitchPoint, ...],
        plan: SwitchProgramPlan | None,
    ) -> tuple[Resource, Resource | None]:
        kind = program_kind(path)
        if kind is None or kind.key != "aircon":
            raise WriteValidationError("Unsupported switch program path")
        if not self.current() or path not in self.data.resources:
            raise WriteValidationError("The program has no current reading")
        if expected is not None and self.data.resources[path] != expected:
            self._count_program("refused")
            raise SwitchProgramChanged
        gateway_id = self.gateway.gateway_id
        try:
            async with self._transaction(PROGRAM_WRITE_DEADLINE_SECONDS):
                fresh = await self._fresh(gateway_id, (path, SCHEDULE_CONFIGURATION))
                program = fresh.get(path)
                if program is None:
                    raise WriteValidationError("The program cannot be read now")
                self._publish(fresh)
                # The rules need the current configuration: one that cannot
                # be read now is not taken from the last cycle.
                readings = {
                    key: value
                    for key, value in self.data.resources.items()
                    if key != SCHEDULE_CONFIGURATION
                }
                readings.update(fresh)
                if plan is not None:
                    planned = plan(program, readings, None)
                    if planned is None:
                        return program, None
                    expected, points = program, planned
                assert expected is not None
                # The shared write checks the rules against these readings,
                # compares the program once more and sends it once.
                confirmed = await self._program_service.async_write(
                    gateway_id,
                    expected,
                    points,
                    readings,
                    delays=READ_BACK_DELAYS,
                    on_sent=self._program_sent,
                )
                self._publish({path: confirmed})
        except WriteValidationError:
            self.writes_refused += 1
            self._count_program("refused")
            raise
        except PointTError:
            self._count_program("failed")
            raise
        self._count_program("confirmed")
        return program, confirmed

    def _program_sent(self) -> None:
        self.writes_sent += 1
        self._count_program("sent")

    async def _fresh(
        self, gateway_id: str, paths: tuple[str, ...]
    ) -> dict[str, Resource]:
        results = await self.client.get_resources_bulk(
            gateway_id, paths, retryable=False
        )
        read: dict[str, Resource] = {}
        for item in results:
            if item.status == 429 or isinstance(item.error, RateLimited):
                raise (
                    item.error
                    if isinstance(item.error, RateLimited)
                    else (RateLimited(None))
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
        # Program IDs stay out of the report; only whether one was read.
        resources = {
            path: resource
            for path, resource in self.data.resources.items()
            if not path.startswith(f"{SCHEDULE_BASE}/program")
        }
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
            "missing_paths": {
                path: reason
                for path, reason in sorted(self.data.missing.items())
                if not path.startswith(f"{SCHEDULE_BASE}/program")
            },
            "writable_paths": sorted(
                path
                for path, resource in resources.items()
                if resource.metadata.writable
            ),
            "eligible_controls": sorted(
                path for path in resources if self.control_ready(path)
            ),
            "function_references": functions,
            "active_program_read": len(resources) < len(self.data.resources),
            "programs_read": len(self.program_paths()),
            "program_edit_issues": self._program_issues(),
            "program_writes": dict(sorted(self.program_writes.items())),
            "writes_sent": self.writes_sent,
            "writes_refused": self.writes_refused,
        }

    def _program_issues(self) -> list[str]:
        """Why each program cannot be changed (``editable`` otherwise);
        tokens only, no program IDs."""
        readings = self.program_readings()
        return sorted(
            {
                edit_issue(resource, readings) or "editable"
                for path in self.program_paths()
                if (resource := self.resource(path)) is not None
            }
        )
