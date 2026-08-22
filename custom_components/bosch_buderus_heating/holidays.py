"""Tolerant interpretation and validation of PointT holiday resources."""

from __future__ import annotations

import base64
import binascii
import math
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
HOLIDAY_CREATE_PATH = "/holidayMode"
HOLIDAY_TIMEZONE_PATH = "/gateway/tzInfo/timeZone"
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
_MAX_HOLIDAY_ID = 2_147_483_647
_MAX_NAME_LENGTH = 80

_CH_MODES = frozenset({"SATURDAY", "FIX_TEMPERATURE", "OFF", "ECO"})
_DHW_MODES = frozenset({"SATURDAY", "OFF", "ECO", "LOW", "HIGH", "OFF_TD"})
_VENTILATION_MODES = frozenset({"SATURDAY", "OFF", "MIN", "RED", "NOM", "MAX", "DEM"})
_THERMAL_DISINFECTION_MODES = frozenset({"ON", "OFF"})
_CIRCUIT_PATTERN = re.compile(r"(?:hc|dhw|vent)\d+", re.IGNORECASE)

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
class HolidayNameCodec:
    """Cloud-advertised holiday-name encoding."""

    coding_type: str
    charset: str | None
    maximum_length: int


@dataclass(frozen=True, slots=True)
class HolidayWriteValues:
    """Known PointT fields required to safely preserve a holiday period."""

    start_date: str
    end_date: str
    heating_mode: str | None
    dhw_mode: str | None
    ventilation_mode: str | None
    assigned_to: tuple[str, ...]
    name: str | None
    thermal_disinfection: str | None
    fix_temperature: float

    def as_payload(self) -> dict[str, JsonValue]:
        """Return the exact payload shape used by MyBuderus/HomeCom Easy."""
        return {
            "startDate": self.start_date,
            "endDate": self.end_date,
            "heatingMode": self.heating_mode,
            "dhwMode": self.dhw_mode,
            "ventilationMode": self.ventilation_mode,
            "assignedTo": list(self.assigned_to),
            "name": self.name,
            # The misspelling is part of the PointT wire contract.
            "thermalDesinfection": self.thermal_disinfection,
            "fixTemperature": self.fix_temperature,
        }


@dataclass(frozen=True, slots=True)
class HolidayWriteConfiguration:
    """Validated capabilities needed for calendar writes."""

    date_time_mode: str
    assigned_to: tuple[str, ...]
    heating_mode: str
    dhw_mode: str
    ventilation_mode: str | None
    thermal_disinfection: str | None
    fix_temperature: float
    name_codec: HolidayNameCodec | None
    heating_modes: tuple[str, ...]
    dhw_modes: tuple[str, ...]
    ventilation_modes: tuple[str, ...]
    thermal_disinfection_modes: tuple[str, ...]
    fix_temperature_min: float | None
    fix_temperature_max: float | None


@dataclass(frozen=True, slots=True)
class HolidayPeriod:
    """One validated PointT holiday period."""

    start: datetime
    end: datetime
    identifier: str | None = None
    name: str | None = None
    all_day: bool = False
    write_values: HolidayWriteValues | None = None


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
    name_codec = _holiday_name_codec(resources)
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
        parsed = _parse_period(candidate, timezone, name_codec)
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


def parse_holiday_write_configuration(
    resources: Mapping[str, Resource],
) -> HolidayWriteConfiguration | None:
    """Return a strict write configuration for the dedicated holiday endpoint."""
    list_resource = resources.get(HOLIDAY_LIST_PATH)
    # PointT exposes the collection through the read-only ``/holidayMode/list``
    # resource, while the apps create and mutate entries through the separate
    # ``/holidayMode[/<id>]`` endpoint.  The list therefore does not need to
    # advertise scalar writes; a current list and a complete configuration are
    # the capability contract for calendar mutations.
    if list_resource is None:
        return None
    values = _holiday_configuration_values(resources)
    if values is None:
        return None

    date_modes = _allowed_strings(values.get("date"))
    if "date" in date_modes and "dateTime" not in date_modes:
        date_time_mode = "date"
    elif "dateTime" in date_modes:
        date_time_mode = "dateTime"
    else:
        return None

    assigned_to = tuple(
        value
        for value in _allowed_string_values(values.get("assignedto"))
        if _CIRCUIT_PATTERN.fullmatch(value)
    )
    if not assigned_to:
        return None

    heating_mode_values = _allowed_string_values(values.get("heatingmode"))
    dhw_mode_values = _allowed_string_values(values.get("dhwmode"))
    ventilation_mode_values = _allowed_string_values(values.get("ventilationmode"))
    thermal_mode_values = _allowed_string_values(
        values.get("thermaldesinfection") or values.get("thermaldisinfection")
    )
    heating_modes = frozenset(heating_mode_values)
    dhw_modes = frozenset(dhw_mode_values)
    ventilation_modes = frozenset(ventilation_mode_values)
    thermal_modes = frozenset(thermal_mode_values)
    if not heating_modes.issubset(_CH_MODES):
        return None
    if not dhw_modes.issubset(_DHW_MODES):
        return None
    if not ventilation_modes.issubset(_VENTILATION_MODES):
        return None
    if not thermal_modes.issubset(_THERMAL_DISINFECTION_MODES):
        return None

    # These are the defaults used by both official apps. PointT expects OFF
    # even when a circuit family is not installed and therefore has no list.
    heating_mode = "FIX_TEMPERATURE" if "FIX_TEMPERATURE" in heating_modes else "OFF"
    dhw_mode = "OFF"
    ventilation_mode = "OFF" if "OFF" in ventilation_modes else None
    thermal_disinfection = "ON" if "ON" in thermal_modes else None

    fix_temperature = 17.0
    fix_temperature_min: float | None = None
    fix_temperature_max: float | None = None
    fix_config = values.get("fixtemperature")
    if isinstance(fix_config, Mapping):
        normalized_fix = {
            _normalize_key(key): value for key, value in fix_config.items()
        }
        fix_temperature_min = _finite_number(normalized_fix.get("minvalue"))
        fix_temperature_max = _finite_number(normalized_fix.get("maxvalue"))
        if fix_temperature_min is not None and fix_temperature < fix_temperature_min:
            return None
        if fix_temperature_max is not None and fix_temperature > fix_temperature_max:
            return None
        if (
            fix_temperature_min is not None
            and fix_temperature_max is not None
            and fix_temperature_min > fix_temperature_max
        ):
            return None

    return HolidayWriteConfiguration(
        date_time_mode=date_time_mode,
        assigned_to=assigned_to,
        heating_mode=heating_mode,
        dhw_mode=dhw_mode,
        ventilation_mode=ventilation_mode,
        thermal_disinfection=thermal_disinfection,
        fix_temperature=fix_temperature,
        name_codec=_name_codec_from_values(values),
        heating_modes=heating_mode_values,
        dhw_modes=dhw_mode_values,
        ventilation_modes=ventilation_mode_values,
        thermal_disinfection_modes=thermal_mode_values,
        fix_temperature_min=fix_temperature_min,
        fix_temperature_max=fix_temperature_max,
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
    candidate: Mapping[str, JsonValue],
    default_timezone: tzinfo,
    name_codec: HolidayNameCodec | None,
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

    identifier = _identifier_text(_find_value(normalized, frozenset(_ID_KEYS)))
    raw_name = _safe_text(_find_value(normalized, frozenset(_NAME_KEYS)), 512)
    period_name_codec = _name_codec(normalized.get("nameconfig")) or name_codec
    full_day_timespan = (
        isinstance(start_raw, str)
        and isinstance(end_raw, str)
        and start_raw.endswith("T00:00:00")
        and end_raw.endswith("T24:00:00")
    )
    return HolidayPeriod(
        start=start,
        end=end,
        identifier=identifier,
        name=_decode_holiday_name(raw_name, period_name_codec),
        all_day=(start_is_date and end_is_date) or full_day_timespan,
        write_values=_period_write_values(candidate, identifier, raw_name),
    )


def holiday_period_id(period: HolidayPeriod) -> int | None:
    """Return a bounded numeric PointT identifier for a writable period."""
    if period.identifier is None or not period.identifier.isdecimal():
        return None
    value = int(period.identifier)
    return value if 0 <= value <= _MAX_HOLIDAY_ID else None


def holiday_timezone(
    resources: Mapping[str, Resource], fallback_timezone: str
) -> tzinfo:
    """Return the validated gateway or Home Assistant holiday time zone."""
    return _select_timezone(resources, fallback_timezone)[0]


def encode_holiday_name(name: str, codec: HolidayNameCodec | None) -> str | None:
    """Encode a holiday name exactly as supported by the official apps."""
    value = " ".join(name.split())
    if not value or len(value) > _MAX_NAME_LENGTH:
        return None
    if codec is None or len(value) > codec.maximum_length:
        return None
    if codec.coding_type == "ASCII":
        # The apps keep ASCII names locally and submit null to PointT.
        return None
    encoding = _python_encoding(codec.charset)
    if codec.coding_type != "BASE64" or encoding is None:
        return None
    try:
        return base64.b64encode(value.encode(encoding)).decode("ascii")
    except UnicodeEncodeError:
        return None


def _period_write_values(
    candidate: Mapping[str, JsonValue],
    identifier: str | None,
    raw_name: str | None,
) -> HolidayWriteValues | None:
    normalized = {_normalize_key(key): value for key, value in candidate.items()}
    if identifier is None or not identifier.isdecimal():
        return None
    holiday_id = int(identifier)
    if not 0 <= holiday_id <= _MAX_HOLIDAY_ID:
        return None
    start = normalized.get("startdate")
    end = normalized.get("enddate")
    if not isinstance(start, str) or not isinstance(end, str):
        return None

    heating_mode = _optional_mode(normalized.get("heatingmode"), _CH_MODES)
    dhw_mode = _optional_mode(normalized.get("dhwmode"), _DHW_MODES)
    ventilation_mode = _optional_mode(
        normalized.get("ventilationmode"), _VENTILATION_MODES
    )
    thermal_disinfection = _optional_mode(
        normalized.get("thermaldesinfection", normalized.get("thermaldisinfection")),
        _THERMAL_DISINFECTION_MODES,
    )
    if any(
        value is _INVALID_MODE
        for value in (
            heating_mode,
            dhw_mode,
            ventilation_mode,
            thermal_disinfection,
        )
    ):
        return None

    assigned_raw = normalized.get("assignedto")
    if not isinstance(assigned_raw, list) or not assigned_raw:
        return None
    assigned_to: list[str] = []
    for item in assigned_raw:
        if not isinstance(item, str) or not _CIRCUIT_PATTERN.fullmatch(item):
            return None
        assigned_to.append(item.casefold())

    fix_temperature = _finite_number(
        normalized.get("fixtemperature", normalized.get("fixtemp"))
    )
    if fix_temperature is None:
        return None
    return HolidayWriteValues(
        start_date=start,
        end_date=end,
        heating_mode=_mode_value(heating_mode),
        dhw_mode=_mode_value(dhw_mode),
        ventilation_mode=_mode_value(ventilation_mode),
        assigned_to=tuple(assigned_to),
        name=raw_name,
        thermal_disinfection=_mode_value(thermal_disinfection),
        fix_temperature=fix_temperature,
    )


_INVALID_MODE = object()


def _optional_mode(
    value: JsonValue | None, allowed: frozenset[str]
) -> str | object | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return _INVALID_MODE
    normalized = value.strip().upper()
    return normalized if normalized in allowed else _INVALID_MODE


def _mode_value(value: str | object | None) -> str | None:
    return value if isinstance(value, str) else None


def _holiday_configuration_values(
    resources: Mapping[str, Resource],
) -> Mapping[str, JsonValue] | None:
    resource = resources.get(HOLIDAY_CONFIGURATION_PATH)
    if resource is None:
        return None
    for payload in _resource_payloads(resource):
        if not isinstance(payload, Mapping):
            continue
        normalized = {_normalize_key(key): value for key, value in payload.items()}
        nested = normalized.get("values")
        if isinstance(nested, Mapping):
            normalized = {_normalize_key(key): value for key, value in nested.items()}
        if any(
            key in normalized
            for key in ("date", "assignedto", "heatingmode", "dhwmode")
        ):
            return normalized
    return None


def _allowed_strings(value: JsonValue | None) -> frozenset[str]:
    return frozenset(_allowed_string_values(value))


def _allowed_string_values(value: JsonValue | None) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    normalized = {_normalize_key(key): item for key, item in value.items()}
    allowed = normalized.get("allowedvalues")
    if not isinstance(allowed, list) or len(allowed) > 64:
        return ()
    return tuple(
        item.strip()
        for item in allowed
        if isinstance(item, str) and 0 < len(item.strip()) <= 64
    )


def _holiday_name_codec(
    resources: Mapping[str, Resource],
) -> HolidayNameCodec | None:
    values = _holiday_configuration_values(resources)
    return _name_codec_from_values(values) if values is not None else None


def _name_codec_from_values(
    values: Mapping[str, JsonValue],
) -> HolidayNameCodec | None:
    name = values.get("name")
    if not isinstance(name, Mapping):
        return None
    normalized_name = {_normalize_key(key): value for key, value in name.items()}
    config = normalized_name.get("stringconfig", name)
    return _name_codec(config)


def _name_codec(config: object) -> HolidayNameCodec | None:
    if not isinstance(config, Mapping):
        return None
    normalized = {_normalize_key(key): value for key, value in config.items()}
    coding_type = normalized.get("codingtype")
    charset = normalized.get("charset")
    maximum = _integer(normalized.get("maxlength"))
    if not isinstance(coding_type, str):
        return None
    coding_type = coding_type.strip().upper()
    if coding_type not in {"ASCII", "BASE64"}:
        return None
    normalized_charset = (
        re.sub(r"[^A-Z0-9]", "", charset.strip().upper())
        if isinstance(charset, str)
        else None
    )
    if coding_type == "BASE64" and _python_encoding(normalized_charset) is None:
        return None
    if maximum is None or not 1 <= maximum <= _MAX_NAME_LENGTH:
        maximum = 32
    return HolidayNameCodec(coding_type, normalized_charset, maximum)


def _decode_holiday_name(
    value: str | None, codec: HolidayNameCodec | None
) -> str | None:
    if value is None:
        return None
    if codec is None or codec.coding_type == "ASCII":
        return _safe_text(value, _MAX_NAME_LENGTH)
    encoding = _python_encoding(codec.charset)
    if encoding is None:
        return None
    try:
        decoded = base64.b64decode(value, validate=True).decode(encoding)
    except binascii.Error, UnicodeDecodeError, ValueError:
        return None
    return _safe_text(decoded, min(codec.maximum_length, _MAX_NAME_LENGTH))


def _python_encoding(charset: str | None) -> str | None:
    return {"UTF8": "utf-8", "UTF16": "utf-16-be", "UTF32": "utf-32-be"}.get(
        charset or ""
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
            end_of_day = re.fullmatch(
                r"(\d{4}-\d{2}-\d{2})T24:([0-5]\d):([0-5]\d)", candidate
            )
            if end_of_day is not None:
                parsed = datetime.combine(
                    date.fromisoformat(end_of_day.group(1)),
                    time(
                        minute=int(end_of_day.group(2)),
                        second=int(end_of_day.group(3)),
                    ),
                    timezone,
                ) + timedelta(days=1)
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
    timezone_resource = resources.get(HOLIDAY_TIMEZONE_PATH)
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


def _finite_number(value: JsonValue | None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _identifier_text(value: JsonValue | None) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= _MAX_HOLIDAY_ID:
        return str(value)
    return _safe_text(value, 96)


def _safe_text(value: JsonValue | None, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = " ".join(value.split())
    if not candidate or len(candidate) > maximum or not candidate.isprintable():
        return None
    return candidate
