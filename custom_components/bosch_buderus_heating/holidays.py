"""Tolerant, read-only interpretation of PointT holiday resources."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .pointt import Resource
from .pointt.models import JsonValue

HOLIDAY_LIST_PATH = "/holidayMode/list"
HOLIDAY_CONFIGURATION_PATH = "/holidayMode/configuration"
HOLIDAY_ACTIVE_MODES_PATH = "/holidayMode/activeModes"
HOLIDAY_RESOURCE_PATHS = (
    HOLIDAY_LIST_PATH,
    HOLIDAY_CONFIGURATION_PATH,
    HOLIDAY_ACTIVE_MODES_PATH,
)
HOLIDAY_PERIOD_PATHS = (HOLIDAY_LIST_PATH, HOLIDAY_CONFIGURATION_PATH)

_MAX_CANDIDATES = 64
_MAX_DEPTH = 6
_MIN_YEAR = 2000
_MAX_YEAR = 2100

_START_KEYS = frozenset(
    {
        "begin",
        "begindate",
        "begindatetime",
        "from",
        "fromdate",
        "start",
        "startdate",
        "startdatetime",
        "starttime",
        "starttimestamp",
        "fromdatetime",
        "validfrom",
    }
)
_END_KEYS = frozenset(
    {
        "end",
        "enddate",
        "enddatetime",
        "endtime",
        "endtimestamp",
        "to",
        "todate",
        "todatetime",
        "until",
        "validto",
    }
)
_ID_KEYS = ("id", "holidayid", "holidaymodeid", "identifier", "uuid")
_NAME_KEYS = ("name", "title", "label")
_TIME_ZONE_KEYS = (
    "offset",
    "timezone",
    "timezoneid",
    "timezoneoffset",
    "tz",
    "utcoffset",
)
_FALSE_TOKENS = frozenset(
    {
        "0",
        "disabled",
        "false",
        "inactive",
        "none",
        "notactive",
        "off",
        "stopped",
    }
)
_TRUE_TOKENS = frozenset({"1", "active", "enabled", "on", "running", "true"})
_UNKNOWN_TOKENS = frozenset({"notavailable", "notsupported", "unknown"})


@dataclass(frozen=True, slots=True)
class HolidayPeriod:
    """One validated PointT holiday period."""

    start: datetime
    end: datetime
    identifier: str | None = None
    name: str | None = None
    all_day: bool = False


@dataclass(frozen=True, slots=True)
class HolidayState:
    """Normalized holiday state without retaining arbitrary vendor fields."""

    periods: tuple[HolidayPeriod, ...]
    active: bool | None
    supported_paths: tuple[str, ...]
    invalid_period_count: int
    timezone_source: str

    @property
    def has_supported_source(self) -> bool:
        """Return whether at least one holiday resource was discovered."""
        return bool(self.supported_paths)


def parse_holiday_state(
    resources: Mapping[str, Resource],
    *,
    fallback_timezone: str = "UTC",
    now: datetime | None = None,
) -> HolidayState:
    """Parse all supported PointT holiday shapes without trusting their schema."""
    timezone, timezone_source = _select_timezone(resources, fallback_timezone)
    candidates: list[Mapping[str, JsonValue]] = []
    for path in HOLIDAY_PERIOD_PATHS:
        resource = resources.get(path)
        if resource is None:
            continue
        for payload in _resource_payloads(resource):
            candidates.extend(_period_candidates(payload))
            if len(candidates) >= _MAX_CANDIDATES:
                break

    invalid = 0
    periods: list[HolidayPeriod] = []
    seen: set[tuple[datetime, datetime, str | None]] = set()
    for candidate in candidates[:_MAX_CANDIDATES]:
        parsed = _parse_period(candidate, timezone)
        if parsed is None:
            invalid += 1
            continue
        identity = (parsed.start, parsed.end, parsed.identifier)
        if identity in seen:
            continue
        seen.add(identity)
        periods.append(parsed)
    periods.sort(key=lambda item: (item.start, item.end, item.identifier or ""))

    active_resource = resources.get(HOLIDAY_ACTIVE_MODES_PATH)
    active = (
        _active_modes_value(active_resource) if active_resource is not None else None
    )
    if active is None and periods:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone)
        active = any(period.start <= current < period.end for period in periods)
    elif active is None and HOLIDAY_LIST_PATH in resources and invalid == 0:
        active = False

    return HolidayState(
        periods=tuple(periods),
        active=active,
        supported_paths=tuple(
            path for path in HOLIDAY_RESOURCE_PATHS if path in resources
        ),
        invalid_period_count=invalid,
        timezone_source=timezone_source,
    )


def _resource_payloads(resource: Resource) -> Iterable[JsonValue]:
    if resource.has_value:
        yield resource.value
    yield from resource.values


def _period_candidates(
    payload: JsonValue, *, depth: int = 0
) -> list[Mapping[str, JsonValue]]:
    if depth > _MAX_DEPTH:
        return []
    if isinstance(payload, list):
        result: list[Mapping[str, JsonValue]] = []
        for item in payload:
            result.extend(_period_candidates(item, depth=depth + 1))
            if len(result) >= _MAX_CANDIDATES:
                break
        return result[:_MAX_CANDIDATES]
    if not isinstance(payload, dict):
        return []

    normalized = {_normalize_key(key): value for key, value in payload.items()}
    if (
        _find_value(normalized, _START_KEYS) is not None
        or _find_value(normalized, _END_KEYS) is not None
    ):
        return [payload]

    result = []
    for value in payload.values():
        if isinstance(value, list | dict):
            result.extend(_period_candidates(value, depth=depth + 1))
            if len(result) >= _MAX_CANDIDATES:
                break
    return result[:_MAX_CANDIDATES]


def _parse_period(
    candidate: Mapping[str, JsonValue], default_timezone: tzinfo
) -> HolidayPeriod | None:
    normalized = {_normalize_key(key): value for key, value in candidate.items()}
    start_raw = _find_value(normalized, _START_KEYS)
    end_raw = _find_value(normalized, _END_KEYS)
    if start_raw is None or end_raw is None:
        return None

    period_timezone = _timezone_from_mapping(normalized) or default_timezone
    start_result = _parse_datetime(start_raw, period_timezone)
    end_result = _parse_datetime(end_raw, period_timezone)
    if start_result is None or end_result is None:
        return None
    start, start_is_date = start_result
    end, end_is_date = end_result
    if start_is_date and end_is_date:
        # PointT/App end dates are inclusive; HA calendar end dates are exclusive.
        end += timedelta(days=1)
    if not (_MIN_YEAR <= start.year <= _MAX_YEAR):
        return None
    if not (_MIN_YEAR <= end.year <= _MAX_YEAR) or end <= start:
        return None

    return HolidayPeriod(
        start=start,
        end=end,
        identifier=_safe_text(_find_value(normalized, frozenset(_ID_KEYS)), 96),
        name=_safe_text(_find_value(normalized, frozenset(_NAME_KEYS)), 80),
        all_day=start_is_date and end_is_date,
    )


def _parse_datetime(value: JsonValue, timezone: tzinfo) -> tuple[datetime, bool] | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, UTC).astimezone(timezone), False
        except OverflowError, OSError, ValueError:
            return None
    if isinstance(value, dict):
        normalized = {_normalize_key(key): item for key, item in value.items()}
        try:
            year = _integer(normalized.get("year"))
            month = _integer(normalized.get("month"))
            day = _integer(normalized.get("day"))
            if year is None or month is None or day is None:
                return None
            hour = _integer(normalized.get("hour")) or 0
            minute = _integer(normalized.get("minute")) or 0
            second = _integer(normalized.get("second")) or 0
            parsed = datetime(year, month, day, hour, minute, second, tzinfo=timezone)
            date_only = not any(
                key in normalized for key in ("hour", "minute", "second")
            )
            return parsed, date_only
        except ValueError:
            return None
    if not isinstance(value, str):
        return None

    candidate = value.strip()
    if not candidate:
        return None
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate))
    try:
        if date_only:
            parsed = datetime.combine(date.fromisoformat(candidate), time(), timezone)
        else:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone)
    except ValueError:
        return None
    return parsed, date_only


def _active_modes_value(resource: Resource) -> bool | None:
    payloads = tuple(_resource_payloads(resource))
    if not payloads:
        # A successfully returned activeModes resource without entries is an
        # explicit empty active-mode list.
        return False
    results = tuple(_active_payload_value(payload) for payload in payloads)
    known = tuple(result for result in results if result is not None)
    return any(known) if known else None


def _active_payload_value(value: JsonValue) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        token = re.sub(r"[\s_-]+", "", value.strip().casefold())
        if token in _FALSE_TOKENS or not token:
            return False
        if token in _TRUE_TOKENS:
            return True
        if token in _UNKNOWN_TOKENS:
            return None
        return True
    if isinstance(value, list):
        if not value:
            return False
        results = tuple(_active_payload_value(item) for item in value)
        known = tuple(result for result in results if result is not None)
        return any(known) if known else True
    if isinstance(value, dict):
        normalized = {_normalize_key(key): item for key, item in value.items()}
        for key in ("active", "enabled", "isactive"):
            if key in normalized:
                return _active_payload_value(normalized[key])
        for key in ("activemodes", "items", "modes", "values"):
            if key in normalized:
                return _active_payload_value(normalized[key])
        return bool(value)
    return None


def _select_timezone(
    resources: Mapping[str, Resource], fallback_timezone: str
) -> tuple[tzinfo, str]:
    timezone_resource = resources.get("/gateway/tzInfo/timeZone")
    if timezone_resource is not None and isinstance(timezone_resource.value, str):
        timezone = _zoneinfo(timezone_resource.value)
        if timezone is not None:
            return timezone, "gateway"
    timezone = _zoneinfo(fallback_timezone)
    if timezone is not None:
        return timezone, "home_assistant"
    return UTC, "utc_fallback"


def _timezone_from_mapping(values: Mapping[str, JsonValue]) -> tzinfo | None:
    value = _find_value(values, frozenset(_TIME_ZONE_KEYS))
    return _zoneinfo(value) if isinstance(value, str) else None


def _zoneinfo(value: str) -> tzinfo | None:
    candidate = value.strip()
    match = re.fullmatch(r"(?:UTC)?([+-])(\d{2}):?(\d{2})", candidate, re.IGNORECASE)
    if match:
        hours, minutes = int(match.group(2)), int(match.group(3))
        if hours > 23 or minutes > 59:
            return None
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(delta if match.group(1) == "+" else -delta)
    try:
        return ZoneInfo(candidate)
    except ValueError, ZoneInfoNotFoundError:
        return None


def _find_value(
    values: Mapping[str, JsonValue], keys: frozenset[str]
) -> JsonValue | None:
    return next((values[key] for key in keys if key in values), None)


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _integer(value: JsonValue | None) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _safe_text(value: JsonValue | None, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = " ".join(value.split())
    if not candidate or len(candidate) > maximum or not candidate.isprintable():
        return None
    return candidate
