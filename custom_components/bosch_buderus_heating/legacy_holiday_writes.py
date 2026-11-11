"""Confirmed ICOM transactions; every changed leaf is sent at most once."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import date
from functools import partial

from .holiday_writes import (
    DEFAULT_HOLIDAY_READ_BACK_ATTEMPTS,
    DEFAULT_HOLIDAY_READ_BACK_DELAY,
    HolidayChangedError,
    HolidayWriteNotConfirmed,
    validate_holiday_values,
)
from .holidays import HolidayWriteValues, icom_write_configuration, icom_write_values
from .legacy_holidays import (
    HMI800_FIXED_FIELDS,
    ICOM_EMPTY_PERIODS,
    ICOM_FIELDS,
    SYSTEM_INFO_PATH,
    icom_components,
    icom_dates,
    icom_empty,
    is_icom_slot,
)
from .pointt import (
    PointTClient,
    RateLimited,
    RequestTimeout,
    Resource,
    ServiceUnavailable,
    WriteValidationError,
)
from .pointt.models import JsonValue


class IcomHolidayWriteService:
    """Keep the last authoritative read and invalidate any uncertain resources."""

    def __init__(
        self,
        client: PointTClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        read_back_delay: float = DEFAULT_HOLIDAY_READ_BACK_DELAY,
        read_back_attempts: int = DEFAULT_HOLIDAY_READ_BACK_ATTEMPTS,
        read_back_delays: tuple[float, ...] | None = None,
    ) -> None:
        if read_back_delay < 0 or any(delay < 0 for delay in read_back_delays or ()):
            raise ValueError("Read-back delay must not be negative")
        if read_back_attempts < 1 or read_back_delays == ():
            raise ValueError("Read-back attempts must be positive")
        self.client = client
        self.confirmed: dict[str, Resource] = {}
        self.invalidated: set[str] = set()
        self._sleep = sleep
        # I1: an iCom gateway reads back after 1, 2, 3 and 4 seconds; the
        # default stays 0.5, 1 and 2 seconds.
        self._read_back_delays = read_back_delays or tuple(
            read_back_delay * (2**attempt) for attempt in range(read_back_attempts)
        )

    async def async_write(
        self,
        gateway_id: str,
        resources: Mapping[str, Resource],
        operation: str,
        source_path: str,
        values: HolidayWriteValues | None,
        expected: HolidayWriteValues | None,
    ) -> Resource:
        """Recheck each step, commit dates last and stop on any conflict or failure."""
        self.confirmed = {}
        self.invalidated = set()
        if not is_icom_slot(source_path) or operation not in (
            "create",
            "update",
            "delete",
        ):
            raise WriteValidationError("Invalid legacy holiday request")
        paths = tuple(
            f"{source_path}/{field}"
            for field in (*ICOM_FIELDS, "delete")
            if f"{source_path}/{field}" in resources
        )
        if not paths:
            raise WriteValidationError("The legacy holiday was not discovered")
        current = await self._read(gateway_id, paths)
        # I2: the component list is not read again; the last fresh reading
        # of the coordinator applies its rules to this transaction.
        system_info = resources.get(SYSTEM_INFO_PATH)
        components = icom_components(system_info)
        configuration = icom_write_configuration(
            {**current, SYSTEM_INFO_PATH: system_info}
            if system_info is not None
            else current,
            source_path,
        )
        baseline = icom_write_values(current, source_path)
        if configuration is None or baseline is None:
            raise WriteValidationError("Legacy holiday capabilities are incomplete")
        if expected is not None and baseline != expected:
            raise HolidayChangedError("The legacy holiday changed")
        start_path = f"{source_path}/startStop"
        if operation == "create":
            if not icom_empty(current.get(start_path)):
                raise HolidayChangedError("The legacy holiday slot is no longer free")
        elif icom_dates(current.get(start_path)) is None:
            raise HolidayChangedError("The legacy holiday no longer exists")
        if operation == "delete":
            if not configuration.can_delete or expected is None:
                raise WriteValidationError(
                    "Deleting this legacy holiday is unavailable"
                )
            self.invalidated.update(paths)
            self.confirmed.clear()
            await self._async_put(gateway_id, f"{source_path}/delete", "TRUE")
            actual = await self._async_read_back(
                gateway_id,
                paths,
                start_path,
                lambda read: icom_empty(read.get(start_path)),
            )
            return actual[start_path]
        if values is None or values.source_path != source_path:
            raise WriteValidationError("Legacy holiday source changed")
        validate_holiday_values(values, configuration)
        # C37: send the circuits in the order the slot advertises them, so
        # that the order of the form never matters for the gateway.
        values = replace(
            values,
            assigned_to=tuple(
                circuit
                for circuit in configuration.assigned_to
                if circuit in values.assigned_to
            ),
        )
        self._validate_dates(values)
        if (
            values.fix_temperature != baseline.fix_temperature
            and values.heating_mode not in ("FIX_TEMP", "FIX_TEMPERATURE")
        ):
            raise WriteValidationError(
                "Fixed temperature requires the fixed-temperature holiday mode"
            )
        if (
            values.name is not None
            or values.ventilation_mode is not None
            or values.thermal_disinfection is not None
        ):
            raise WriteValidationError("Legacy holidays do not support these fields")
        if (
            values.fix_temperature != baseline.fix_temperature
            and configuration.fix_temperature_min is None
        ):
            raise WriteValidationError("Legacy temperature is not writable")
        desired: dict[str, JsonValue] = {
            "dhwMode": values.dhw_mode,
            "hcMode": values.heating_mode,
            "fixTemperature": values.fix_temperature,
            "assignedTo": list(values.assigned_to),
            "startStop": f"{values.start_date}/{values.end_date}",
        }
        # I2: every field to change is checked before the first PUT, so a
        # field that may not be written never leaves half a holiday behind.
        steps = [
            (field, f"{source_path}/{field}", value)
            for field, value in desired.items()
            if f"{source_path}/{field}" in current
            and not _field_matches(field, current[f"{source_path}/{field}"], value)
        ]
        if any(not current[path].metadata.writable for _field, path, _ in steps):
            raise WriteValidationError("Legacy holiday field is not writable")
        if components.hmi800_hp_light and any(
            field in HMI800_FIXED_FIELDS for field, _path, _value in steps
        ):
            # Expected, untested: the app never writes these fields there.
            raise WriteValidationError(
                "This controller keeps the holiday circuits and heating mode"
            )
        for field, path, value in steps:
            if _field_matches(field, current[path], value):
                continue
            # C38: ``current`` is the complete read just before this step: the
            # initial one or the previous step's read-back, without any wait.
            # Each read-back compares every other field, so a change in the
            # app between steps stops the transaction. PointT provides no
            # atomic compare-and-set operation.
            self.invalidated.update(paths)
            self.confirmed.clear()
            if field == "assignedTo":
                await self._async_put_assignments(
                    gateway_id, source_path, values.assigned_to
                )
            else:
                await self._async_put(gateway_id, path, value)
            before = current
            current = await self._async_read_back(
                gateway_id,
                paths,
                start_path,
                partial(_field_confirmed, field, path, value),
                conflict=partial(_other_field_changed, path, before),
            )
        # A full final comparison also covers no-op submissions and slot identity.
        actual_values = icom_write_values(current, source_path)
        if actual_values is None or not _same_values(actual_values, values):
            raise HolidayWriteNotConfirmed(current.get(start_path))
        return current[start_path]

    async def _async_put(self, gateway_id: str, path: str, value: JsonValue) -> None:
        """Send one leaf once; resolve an unknown outcome only by reading."""
        try:
            await self.client.put_resource_value(gateway_id, path, value)
        except RateLimited:
            raise
        except RequestTimeout, ServiceUnavailable:
            # C38: a lost connection or server error may follow an applied
            # PUT. The read-back decides; the PUT is never sent again.
            pass

    async def _async_put_assignments(
        self, gateway_id: str, source_path: str, circuits: tuple[str, ...]
    ) -> None:
        """Send the circuit list once; resolve an unknown outcome by reading."""
        try:
            await self.client.put_icom_holiday_assignments(
                gateway_id, int(source_path.rsplit("hm", 1)[-1]), circuits
            )
        except RateLimited:
            raise
        except RequestTimeout, ServiceUnavailable:
            pass

    async def _async_read_back(
        self,
        gateway_id: str,
        paths: tuple[str, ...],
        start_path: str,
        confirmed: Callable[[dict[str, Resource]], bool],
        *,
        conflict: Callable[[dict[str, Resource]], bool] = lambda _read: False,
    ) -> dict[str, Resource]:
        """C38: read back like the scalar writes, with a short wait and retries.

        A change of another field is a conflict and ends at once; a value not
        yet updated is read again after 0.5, 1 and 2 seconds (iCom gateways:
        1, 2, 3 and 4 seconds, I1).
        """
        last_temporary_error: RequestTimeout | ServiceUnavailable | None = None
        actual: dict[str, Resource] | None = None
        for delay in self._read_back_delays:
            await self._sleep(delay)
            try:
                actual = await self._read(gateway_id, paths)
            except (RequestTimeout, ServiceUnavailable) as err:
                last_temporary_error = err
                continue
            if conflict(actual):
                break
            if confirmed(actual):
                return actual
        raise HolidayWriteNotConfirmed(
            actual.get(start_path) if actual is not None else None
        ) from last_temporary_error

    async def _read(
        self, gateway_id: str, paths: tuple[str, ...]
    ) -> dict[str, Resource]:
        result: dict[str, Resource] = {}
        self.invalidated.update(paths)
        for path in paths:
            self.confirmed.pop(path, None)
        for path in paths:
            resource = await self.client.get_resource(gateway_id, path)
            if resource.path != path:
                raise WriteValidationError("Legacy holiday response has the wrong path")
            result[path] = resource
            self.confirmed[path] = resource
            self.invalidated.discard(path)
        return result

    @staticmethod
    def _validate_dates(values: HolidayWriteValues) -> None:
        try:
            start, end = (
                date.fromisoformat(values.start_date),
                date.fromisoformat(values.end_date),
            )
        except (TypeError, ValueError) as err:
            raise WriteValidationError("Invalid legacy holiday dates") from err
        if (
            start.isoformat() != values.start_date
            or end.isoformat() != values.end_date
            or not 2000 <= start.year <= end.year <= 2100
            or end < start
            or f"{values.start_date}/{values.end_date}" in ICOM_EMPTY_PERIODS
        ):
            raise WriteValidationError("Invalid legacy holiday date range")


def _field_matches(field: str, resource: Resource, value: JsonValue) -> bool:
    """C37: compare circuit assignments as a set, every other field exactly."""
    if field == "assignedTo":
        circuits = resource.values
        return (
            isinstance(value, list)
            and all(isinstance(circuit, str) for circuit in circuits)
            and len(circuits) == len(set(circuits))
            and set(circuits) == set(value)
        )
    return bool(resource.value == value)


def _field_confirmed(
    field: str, path: str, value: JsonValue, read: Mapping[str, Resource]
) -> bool:
    return _field_matches(field, read[path], value)


def _other_field_changed(
    path: str, before: Mapping[str, Resource], read: Mapping[str, Resource]
) -> bool:
    """C38: whether the app changed another field of the slot meanwhile."""
    return any(read[other] != before[other] for other in before if other != path)


def _same_values(actual: HolidayWriteValues, expected: HolidayWriteValues) -> bool:
    """C37: equal values, with the circuit assignments compared as a set."""
    return set(actual.assigned_to) == set(expected.assigned_to) and replace(
        actual, assigned_to=()
    ) == replace(expected, assigned_to=())
