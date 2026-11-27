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
from .holidays import (
    HolidayWriteValues,
    icom_cached_reset_allowed,
    icom_delete_command_supported,
    icom_reset_reference,
    icom_write_configuration,
    icom_write_values,
)
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
    ResourceNotFound,
    ServiceUnavailable,
    WriteValidationError,
)
from .pointt.models import JsonValue
from .write_window import ensure_send_window


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
        # Technical assignment fields only; no dates or gateway IDs.
        self.last_write_diagnostics: dict[str, object] = {}
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
        *,
        reset_marker: str | None = None,
    ) -> Resource:
        """Recheck each step; initialize dates before assigning new holidays."""
        self.confirmed = {}
        self.invalidated = set()
        self.last_write_diagnostics = {
            "operation": operation
            if operation in ("create", "update", "delete")
            else "invalid",
            "slot": source_path.rsplit("/", 1)[-1]
            if is_icom_slot(source_path)
            else None,
            "stage": "preflight",
            "planned_fields": (),
            "confirmed_fields": (),
            "current_field": None,
            "read_back_attempts": 0,
            "read_back_not_found_count": 0,
            "last_missing_field": None,
            "last_readback_status": None,
            "date_initialization_changes": [],
            "skipped_already_matching": [],
            "partial_creation_status": "not_started",
        }
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
        reset_reference = None
        reset_resources: dict[str, Resource] = {}
        if operation == "delete" and f"{source_path}/delete" not in resources:
            reference = icom_reset_reference(resources, source_path)
            if reference is not None:
                reference_path, marker = reference
                fresh = await self.client.get_resource(gateway_id, reference_path)
                if (
                    fresh.path != reference_path
                    or not icom_empty(fresh)
                    or fresh.value != marker
                ):
                    raise HolidayChangedError("The empty holiday reference changed")
                reset_reference = marker
                reset_resources[reference_path] = fresh
            elif icom_cached_reset_allowed(resources, reset_marker):
                reset_reference = reset_marker
        current = await self._read(gateway_id, paths)
        # I2: the component list is not read again; the last fresh reading
        # of the coordinator applies its rules to this transaction.
        system_info = resources.get(SYSTEM_INFO_PATH)
        components = icom_components(system_info)
        configuration = icom_write_configuration(
            {**resources, **reset_resources, **current, SYSTEM_INFO_PATH: system_info}
            if system_info is not None
            else {**resources, **reset_resources, **current},
            source_path,
            reset_marker=reset_reference
            if icom_cached_reset_allowed(resources, reset_reference)
            else None,
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
            explicit = icom_delete_command_supported(
                current.get(f"{source_path}/delete")
            )
            if not explicit and reset_reference is None:
                raise WriteValidationError("No verified empty holiday reference")
            field = "delete" if explicit else "startStop"
            self.last_write_diagnostics["delete_method"] = (
                "explicit" if explicit else "reset_start_stop"
            )
            self.last_write_diagnostics["planned_fields"] = (field,)
            self.last_write_diagnostics["current_field"] = field
            self.last_write_diagnostics["stage"] = "put"
            await self._async_put(
                gateway_id,
                f"{source_path}/{field}",
                "TRUE" if explicit else reset_reference,
            )
            self.last_write_diagnostics["stage"] = "read_back"
            actual = await self._async_read_back(
                gateway_id,
                paths,
                start_path,
                lambda read: (
                    icom_empty(read.get(start_path))
                    and (
                        explicit
                        or (
                            not read[f"{source_path}/assignedTo"].has_value
                            and not read[f"{source_path}/assignedTo"].values
                        )
                    )
                ),
            )
            self.last_write_diagnostics["confirmed_fields"] = (field,)
            self.last_write_diagnostics["stage"] = "confirmed"
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
        if operation == "create":
            # Initialize dates before assigning circuits: iCom can populate
            # assignments automatically. Updates retain the original order.
            steps.sort(
                key=lambda step: {"startStop": 1, "assignedTo": 2}.get(step[0], 0)
            )
        self.last_write_diagnostics["planned_fields"] = tuple(
            field for field, _path, _value in steps
        )
        if any(not current[path].metadata.writable for _field, path, _ in steps):
            raise WriteValidationError("Legacy holiday field is not writable")
        if components.hmi800_hp_light and any(
            field in HMI800_FIXED_FIELDS for field, _path, _value in steps
        ):
            # Expected, untested: these fields are not changeable there.
            raise WriteValidationError(
                "This controller keeps the holiday circuits and heating mode"
            )
        for field, path, value in steps:
            if _field_matches(field, current[path], value):
                skipped = self.last_write_diagnostics["skipped_already_matching"]
                if isinstance(skipped, list):
                    skipped.append(field)
                previous = self.last_write_diagnostics["confirmed_fields"]
                if isinstance(previous, tuple):
                    self.last_write_diagnostics["confirmed_fields"] = (*previous, field)
                continue
            if not current[path].metadata.writable:
                raise WriteValidationError("Legacy holiday field is no longer writable")
            # C38: ``current`` is the complete read just before this step: the
            # initial one or the previous step's read-back, without any wait.
            # Each read-back compares every other field, so a change in the
            # app between steps stops the transaction. PointT provides no
            # atomic compare-and-set operation.
            self.invalidated.update(paths)
            self.confirmed.clear()
            self.last_write_diagnostics["current_field"] = field
            self.last_write_diagnostics["stage"] = "put"
            if operation == "create" and field == "startStop":
                self.last_write_diagnostics["partial_creation_status"] = (
                    "date_write_attempted"
                )
            if field == "assignedTo":
                await self._async_put_assignments(
                    gateway_id, source_path, values.assigned_to
                )
            else:
                await self._async_put(gateway_id, path, value)
            self.last_write_diagnostics["stage"] = "read_back"
            before = current
            current = await self._async_read_back(
                gateway_id,
                paths,
                start_path,
                partial(_field_confirmed, field, path, value),
                conflict=(
                    partial(self._date_initialization_conflict, path, before, value)
                    if operation == "create" and field == "startStop"
                    else partial(_other_field_changed, path, before)
                ),
            )
            if operation == "create" and field == "startStop":
                self.last_write_diagnostics["partial_creation_status"] = (
                    "date_confirmed"
                )
            previous = self.last_write_diagnostics["confirmed_fields"]
            if isinstance(previous, tuple):
                self.last_write_diagnostics["confirmed_fields"] = (*previous, field)
        # Re-read every discovered leaf after creation.
        if operation == "create":
            self.last_write_diagnostics["stage"] = "final_read"
            current = await self._read(gateway_id, paths)
        # A full final comparison also covers no-op submissions and slot identity.
        actual_values = icom_write_values(current, source_path)
        if actual_values is None or not _same_values(actual_values, values):
            self.last_write_diagnostics["stage"] = "final_mismatch"
            self.last_write_diagnostics["mismatched_fields"] = tuple(
                field
                for field, wanted in desired.items()
                if f"{source_path}/{field}" in current
                and not _field_matches(field, current[f"{source_path}/{field}"], wanted)
            )
            raise HolidayWriteNotConfirmed(current.get(start_path))
        self.last_write_diagnostics["stage"] = "confirmed"
        if operation == "create":
            self.last_write_diagnostics["partial_creation_status"] = "complete"
        return current[start_path]

    def _date_initialization_conflict(
        self,
        path: str,
        before: Mapping[str, Resource],
        wanted: JsonValue,
        read: Mapping[str, Resource],
    ) -> bool:
        """Permit only assignment initialization when the requested dates match."""
        changed = tuple(
            other for other in before if other != path and read[other] != before[other]
        )
        self.last_write_diagnostics["date_initialization_changes"] = [
            other.rsplit("/", 1)[-1] for other in changed
        ]
        assignment_path = path.rsplit("/", 1)[0] + "/assignedTo"
        if not changed:
            return False
        if changed != (assignment_path,) or not _field_matches(
            "startStop", read[path], wanted
        ):
            return True
        old, new = before[assignment_path], read[assignment_path]
        # Only accept initialization of a previously empty assignment. A
        # changed nonempty assignment could be a concurrent user edit.
        if old.values or old.has_value or not new.values:
            return True
        allowed = old.metadata.allowed_values
        return not (
            all(isinstance(item, str) and item in allowed for item in new.values)
            and len(new.values) == len(set(new.values))
        )

    async def _async_put(self, gateway_id: str, path: str, value: JsonValue) -> None:
        """Send one leaf once; resolve an unknown outcome only by reading."""
        ensure_send_window(sum(self._read_back_delays))
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
        ensure_send_window(sum(self._read_back_delays))
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
        # A real iCom gateway has returned HTTP 404 for assignedTo directly
        # after accepting a holiday-mode PUT (204), even though its preflight
        # GETs succeeded. Treat missing *read-back* leaves as potentially
        # transient; never reinterpret an initial missing resource as writable
        # and never retry the PUT.
        last_temporary_error: (
            RequestTimeout | ResourceNotFound | ServiceUnavailable | None
        ) = None
        actual: dict[str, Resource] | None = None
        for attempt, delay in enumerate(self._read_back_delays, 1):
            self.last_write_diagnostics["read_back_attempts"] = attempt
            await self._sleep(delay)
            try:
                actual = await self._read(gateway_id, paths)
            except (RequestTimeout, ResourceNotFound, ServiceUnavailable) as err:
                last_temporary_error = err
                if isinstance(err, ResourceNotFound):
                    count = self.last_write_diagnostics["read_back_not_found_count"]
                    if isinstance(count, int):
                        self.last_write_diagnostics["read_back_not_found_count"] = (
                            count + 1
                        )
                    leaf = err.path.rsplit("/", 1)[-1]
                    self.last_write_diagnostics["last_missing_field"] = (
                        leaf if leaf in (*ICOM_FIELDS, "delete") else "other"
                    )
                    self.last_write_diagnostics["last_readback_status"] = (
                        "resource_not_found"
                    )
                elif isinstance(err, RequestTimeout):
                    self.last_write_diagnostics["last_readback_status"] = "timeout"
                else:
                    self.last_write_diagnostics["last_readback_status"] = "unavailable"
                continue
            if self.last_write_diagnostics["operation"] == "create":
                date_resource = actual.get(start_path)
                self.last_write_diagnostics["last_observed_date_state"] = (
                    "valid"
                    if icom_dates(date_resource) is not None
                    else "empty"
                    if icom_empty(date_resource)
                    else "unknown"
                )
            if conflict(actual):
                self.last_write_diagnostics["last_readback_status"] = (
                    "other_field_changed"
                )
                break
            if confirmed(actual):
                self.last_write_diagnostics["last_readback_status"] = "field_confirmed"
                return actual
            self.last_write_diagnostics["last_readback_status"] = "field_mismatch"
        self.last_write_diagnostics["stage"] = "unconfirmed"
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
