"""Validated PointT holiday calendar writes with mandatory read-back."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import fields, replace
from datetime import date, datetime, time, timedelta, tzinfo

from .holidays import (
    HOLIDAY_CONFIGURATION_PATH,
    HOLIDAY_LIST_PATH,
    HOLIDAY_TIMEZONE_PATH,
    HolidayPeriod,
    HolidayWriteConfiguration,
    HolidayWriteValues,
    encode_holiday_name,
    holiday_period_id,
    parse_confirmed_holiday_list,
    parse_holiday_state,
)
from .pointt import (
    PointTClient,
    RequestTimeout,
    Resource,
    ServiceUnavailable,
    WriteNotConfirmed,
    WriteValidationError,
)

DEFAULT_HOLIDAY_READ_BACK_DELAY = 0.5
DEFAULT_HOLIDAY_READ_BACK_ATTEMPTS = 3
_DATE_TIME_STEP_MINUTES = 15


class HolidayWriteService:
    """Write a holiday exactly once and confirm the resulting holiday list."""

    def __init__(
        self,
        client: PointTClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        read_back_delay: float = DEFAULT_HOLIDAY_READ_BACK_DELAY,
        read_back_attempts: int = DEFAULT_HOLIDAY_READ_BACK_ATTEMPTS,
    ) -> None:
        if read_back_delay < 0:
            raise ValueError("Read-back delay must not be negative")
        if read_back_attempts < 1:
            raise ValueError("Read-back attempts must be positive")
        self._client = client
        self._sleep = sleep
        self._read_back_delay = read_back_delay
        self._read_back_attempts = read_back_attempts

    async def async_create(
        self,
        gateway_id: str,
        resources: Mapping[str, Resource],
        values: HolidayWriteValues,
        *,
        fallback_timezone: str,
    ) -> Resource:
        """Create one period and confirm a new matching numeric identifier."""
        periods = await self._async_current_periods(
            gateway_id, resources, fallback_timezone=fallback_timezone
        )
        previous_ids = {
            identifier
            for period in periods
            if (identifier := holiday_period_id(period)) is not None
        }
        with suppress(RequestTimeout):
            await self._client.create_holiday_period(gateway_id, values.as_payload())
        # The server may have applied a timed-out non-idempotent POST. Always
        # determine the result from the following read-back instead of retrying.

        def confirmed(periods: tuple[HolidayPeriod, ...]) -> bool:
            return any(
                (identifier := holiday_period_id(period)) is not None
                and identifier not in previous_ids
                and _period_matches_values(period, values)
                for period in periods
            )

        return await self._async_confirm(
            gateway_id,
            resources,
            confirmed,
            fallback_timezone=fallback_timezone,
        )

    async def async_update(
        self,
        gateway_id: str,
        resources: Mapping[str, Resource],
        holiday_id: int,
        values: HolidayWriteValues,
        *,
        fallback_timezone: str,
        expected: HolidayWriteValues | None = None,
    ) -> Resource:
        """Update one period and confirm all known fields by numeric ID."""
        _require_existing_id(resources, holiday_id, fallback_timezone)
        baseline = expected or next(
            period.write_values
            for period in parse_holiday_state(
                resources, fallback_timezone=fallback_timezone
            ).periods
            if holiday_period_id(period) == holiday_id
        )
        periods = await self._async_current_periods(
            gateway_id, resources, fallback_timezone=fallback_timezone
        )
        current = next(
            (
                period.write_values
                for period in periods
                if holiday_period_id(period) == holiday_id
            ),
            None,
        )
        if baseline is None or current is None:
            raise WriteValidationError("Holiday is no longer available for editing")
        values = _merge_holiday_changes(baseline, values, current)
        merged_resource = Resource(
            path=HOLIDAY_LIST_PATH,
            value=[{"id": holiday_id, **values.as_payload()}],
            has_value=True,
        )
        if (
            parse_confirmed_holiday_list(
                merged_resource, resources, fallback_timezone=fallback_timezone
            )
            is None
        ):
            raise WriteValidationError("Updated holiday no longer has a valid timespan")
        with suppress(RequestTimeout):
            await self._client.update_holiday_period(
                gateway_id, holiday_id, values.as_payload()
            )

        def confirmed(periods: tuple[HolidayPeriod, ...]) -> bool:
            return any(
                holiday_period_id(period) == holiday_id
                and _period_matches_values(period, values)
                for period in periods
            )

        return await self._async_confirm(
            gateway_id,
            resources,
            confirmed,
            fallback_timezone=fallback_timezone,
        )

    async def async_delete(
        self,
        gateway_id: str,
        resources: Mapping[str, Resource],
        holiday_id: int,
        *,
        fallback_timezone: str,
    ) -> Resource:
        """Delete one period and confirm that its numeric ID disappeared."""
        _require_existing_id(resources, holiday_id, fallback_timezone)
        with suppress(RequestTimeout):
            await self._client.delete_holiday_period(gateway_id, holiday_id)

        def confirmed(periods: tuple[HolidayPeriod, ...]) -> bool:
            return all(holiday_period_id(period) != holiday_id for period in periods)

        return await self._async_confirm(
            gateway_id,
            resources,
            confirmed,
            fallback_timezone=fallback_timezone,
        )

    async def _async_current_periods(
        self,
        gateway_id: str,
        resources: Mapping[str, Resource],
        *,
        fallback_timezone: str,
    ) -> tuple[HolidayPeriod, ...]:
        """Establish an authoritative baseline before a non-idempotent write."""
        resource = await self._client.get_resource(gateway_id, HOLIDAY_LIST_PATH)
        periods = parse_confirmed_holiday_list(
            resource, resources, fallback_timezone=fallback_timezone
        )
        if periods is None:
            raise WriteValidationError("Current holiday list is incomplete or invalid")
        return periods

    async def _async_confirm(
        self,
        gateway_id: str,
        resources: Mapping[str, Resource],
        confirmed: Callable[[tuple[HolidayPeriod, ...]], bool],
        *,
        fallback_timezone: str,
    ) -> Resource:
        last_temporary_error: RequestTimeout | ServiceUnavailable | None = None
        for attempt in range(self._read_back_attempts):
            await self._sleep(self._read_back_delay * (2**attempt))
            try:
                holiday_list = await self._client.get_resource(
                    gateway_id, HOLIDAY_LIST_PATH
                )
            except (RequestTimeout, ServiceUnavailable) as err:
                last_temporary_error = err
                continue
            current = dict(resources)
            current[HOLIDAY_LIST_PATH] = holiday_list
            periods = parse_confirmed_holiday_list(
                holiday_list, current, fallback_timezone=fallback_timezone
            )
            if periods is not None and confirmed(periods):
                return holiday_list
        if last_temporary_error is not None:
            raise WriteNotConfirmed("PointT holiday read-back timed out") from (
                last_temporary_error
            )
        raise WriteNotConfirmed("PointT holiday read-back did not confirm the change")


def _merge_holiday_changes(
    baseline: HolidayWriteValues,
    desired: HolidayWriteValues,
    current: HolidayWriteValues,
) -> HolidayWriteValues:
    """Apply only the user's changes and reject conflicting concurrent edits."""
    changes = {}
    for field in fields(HolidayWriteValues):
        before = getattr(baseline, field.name)
        after = getattr(desired, field.name)
        live = getattr(current, field.name)
        if before == after:
            continue
        if live != before and live != after:
            raise WriteValidationError(
                "Holiday changed concurrently; refresh before editing"
            )
        changes[field.name] = after
    return replace(current, **changes)


def create_holiday_values(
    start: date | datetime,
    end: date | datetime,
    summary: str,
    configuration: HolidayWriteConfiguration,
    timezone: tzinfo,
) -> HolidayWriteValues:
    """Build a new period using the same safe defaults as the official apps."""
    start_value, end_value = _format_timespan(
        start, end, configuration.date_time_mode, timezone
    )
    encoded_name = _encode_name(summary, configuration)
    return HolidayWriteValues(
        start_date=start_value,
        end_date=end_value,
        heating_mode=configuration.heating_mode,
        dhw_mode=configuration.dhw_mode,
        ventilation_mode=configuration.ventilation_mode,
        assigned_to=configuration.assigned_to,
        name=encoded_name,
        thermal_disinfection=configuration.thermal_disinfection,
        fix_temperature=configuration.fix_temperature,
    )


def update_holiday_values(
    period: HolidayPeriod,
    start: date | datetime,
    end: date | datetime,
    summary: str,
    configuration: HolidayWriteConfiguration,
    timezone: tzinfo,
    *,
    change_name: bool = True,
) -> HolidayWriteValues:
    """Change calendar fields while preserving every other PointT setting."""
    current = period.write_values
    if current is None or holiday_period_id(period) is None:
        raise WriteValidationError(
            "This holiday period does not have a complete writable PointT payload"
        )
    start_value, end_value = _format_timespan(
        start, end, configuration.date_time_mode, timezone
    )
    name = current.name
    if change_name:
        normalized_summary = _normalize_summary(summary)
        name = _encode_name(normalized_summary, configuration, require_storage=True)
    return replace(
        current,
        start_date=start_value,
        end_date=end_value,
        name=name,
    )


def configure_holiday_values(
    period: HolidayPeriod,
    configuration: HolidayWriteConfiguration,
    *,
    assigned_to: list[str],
    heating_mode: str | None,
    dhw_mode: str | None,
    ventilation_mode: str | None,
    thermal_disinfection: str | None,
    fix_temperature: float,
) -> HolidayWriteValues:
    """Validate and apply the PointT-specific settings of one holiday."""
    current = period.write_values
    if current is None or holiday_period_id(period) is None:
        raise WriteValidationError(
            "This holiday period does not have a complete writable PointT payload"
        )

    assignments = tuple(dict.fromkeys(assigned_to))
    if (
        not assignments
        or len(assignments) != len(assigned_to)
        or not set(assignments).issubset(configuration.assigned_to)
    ):
        raise WriteValidationError("Holiday assignments are not supported")

    _validate_configured_mode(
        heating_mode,
        current.heating_mode,
        configuration.heating_modes,
        "heating",
    )
    _validate_configured_mode(
        dhw_mode,
        current.dhw_mode,
        configuration.dhw_modes,
        "hot-water",
    )
    _validate_configured_mode(
        ventilation_mode,
        current.ventilation_mode,
        configuration.ventilation_modes,
        "ventilation",
    )
    _validate_configured_mode(
        thermal_disinfection,
        current.thermal_disinfection,
        configuration.thermal_disinfection_modes,
        "thermal-disinfection",
    )

    if not math.isfinite(fix_temperature):
        raise WriteValidationError("Holiday fixed temperature must be finite")
    if (
        configuration.fix_temperature_min is None
        or configuration.fix_temperature_max is None
    ):
        if not math.isclose(fix_temperature, current.fix_temperature):
            raise WriteValidationError(
                "The gateway does not advertise safe fixed-temperature limits"
            )
    elif not (
        configuration.fix_temperature_min
        <= fix_temperature
        <= configuration.fix_temperature_max
    ):
        raise WriteValidationError("Holiday fixed temperature is outside its limits")

    return replace(
        current,
        assigned_to=assignments,
        heating_mode=heating_mode,
        dhw_mode=dhw_mode,
        ventilation_mode=ventilation_mode,
        thermal_disinfection=thermal_disinfection,
        fix_temperature=fix_temperature,
    )


def _validate_configured_mode(
    value: str | None,
    current: str | None,
    allowed: tuple[str, ...],
    field: str,
) -> None:
    if allowed:
        if value not in allowed:
            raise WriteValidationError(f"Holiday {field} mode is not supported")
        return
    if value != current:
        raise WriteValidationError(f"Holiday {field} mode is not configurable")


def _format_timespan(
    start: date | datetime,
    end: date | datetime,
    date_time_mode: str,
    timezone: tzinfo,
) -> tuple[str, str]:
    start_is_datetime = isinstance(start, datetime)
    end_is_datetime = isinstance(end, datetime)
    if start_is_datetime != end_is_datetime:
        raise WriteValidationError("Holiday start and end must use the same type")

    if date_time_mode == "date":
        start_date = (
            _local_datetime(start, timezone).date() if start_is_datetime else start
        )
        end_date = _local_datetime(end, timezone).date() if end_is_datetime else end
        if start_is_datetime and (
            _local_datetime(start, timezone).time() != time()
            or _local_datetime(end, timezone).time() != time()
        ):
            raise WriteValidationError("This system supports all-day holidays only")
        if end_date <= start_date:
            raise WriteValidationError("Holiday end must be after its start")
        # PointT date end values are inclusive; Home Assistant ends are exclusive.
        return start_date.isoformat(), (end_date - timedelta(days=1)).isoformat()

    if date_time_mode != "dateTime":
        raise WriteValidationError("Unsupported PointT holiday date mode")
    start_datetime = (
        _local_datetime(start, timezone)
        if start_is_datetime
        else datetime.combine(start, time(), timezone)
    )
    end_datetime = (
        _local_datetime(end, timezone)
        if end_is_datetime
        else datetime.combine(end, time(), timezone)
    )
    if end_datetime <= start_datetime:
        raise WriteValidationError("Holiday end must be after its start")
    for value in (start_datetime, end_datetime):
        if value.minute % _DATE_TIME_STEP_MINUTES or value.second or value.microsecond:
            raise WriteValidationError(
                "Holiday times must use the 15-minute steps supported by PointT"
            )
    return _format_start(start_datetime), _format_end(end_datetime)


def _local_datetime(value: date | datetime, timezone: tzinfo) -> datetime:
    if not isinstance(value, datetime):
        return datetime.combine(value, time(), timezone)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _format_start(value: datetime) -> str:
    return value.replace(tzinfo=None).isoformat(timespec="seconds")


def _format_end(value: datetime) -> str:
    if value.time() == time():
        previous = value.date() - timedelta(days=1)
        return f"{previous.isoformat()}T24:00:00"
    return value.replace(tzinfo=None).isoformat(timespec="seconds")


def _normalize_summary(summary: str) -> str:
    value = " ".join(summary.split())
    if not value or len(value) > 80 or not value.isprintable():
        raise WriteValidationError(
            "Holiday name must contain 1 to 80 printable characters"
        )
    return value


def _encode_name(
    summary: str,
    configuration: HolidayWriteConfiguration,
    *,
    require_storage: bool = False,
) -> str | None:
    normalized = _normalize_summary(summary)
    codec = configuration.name_codec
    if codec is not None and len(normalized) > codec.maximum_length:
        raise WriteValidationError(
            f"Holiday name must not exceed {codec.maximum_length} characters"
        )
    encoded = encode_holiday_name(normalized, codec)
    if require_storage and encoded is None:
        raise WriteValidationError(
            "This gateway does not support storing holiday names in PointT"
        )
    return encoded


def _period_matches_values(period: HolidayPeriod, expected: HolidayWriteValues) -> bool:
    actual = period.write_values
    if actual is None:
        return False
    return (
        period.start
        == _value_datetime(expected.start_date, period.start.tzinfo, end=False)
        and period.end
        == _value_datetime(expected.end_date, period.end.tzinfo, end=True)
        and actual.heating_mode == expected.heating_mode
        and actual.dhw_mode == expected.dhw_mode
        and actual.ventilation_mode == expected.ventilation_mode
        and set(actual.assigned_to) == set(expected.assigned_to)
        and actual.name == expected.name
        and actual.thermal_disinfection == expected.thermal_disinfection
        and math.isclose(actual.fix_temperature, expected.fix_temperature)
    )


def _require_existing_id(
    resources: Mapping[str, Resource], holiday_id: int, fallback_timezone: str
) -> None:
    if not any(
        holiday_period_id(period) == holiday_id
        for period in parse_holiday_state(
            resources, fallback_timezone=fallback_timezone
        ).periods
    ):
        raise WriteValidationError(
            "Holiday ID is not present in the current PointT holiday list"
        )


def _value_datetime(
    value: str, timezone: tzinfo | None, *, end: bool
) -> datetime | None:
    if len(value) == 10:
        parsed_date = date.fromisoformat(value)
        if end:
            parsed_date += timedelta(days=1)
        return datetime.combine(parsed_date, time(), timezone)
    if value.endswith("T24:00:00"):
        return datetime.combine(
            date.fromisoformat(value[:10]) + timedelta(days=1), time(), timezone
        )
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed


def holiday_resources_from_snapshots(
    snapshots: Mapping[str, object],
) -> dict[str, Resource]:
    """Extract fresh-enough holiday resources without depending on coordinator types."""
    resources: dict[str, Resource] = {}
    for path in (
        HOLIDAY_LIST_PATH,
        HOLIDAY_CONFIGURATION_PATH,
        HOLIDAY_TIMEZONE_PATH,
    ):
        snapshot = snapshots.get(path)
        resource = getattr(snapshot, "resource", None)
        available = getattr(snapshot, "available", False)
        if available and isinstance(resource, Resource):
            resources[path] = resource
    return resources
