"""Tolerant interpretation and validation of PointT holiday resources."""

from __future__ import annotations

import base64
import binascii
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from functools import lru_cache

from .legacy_holidays import (
    SYSTEM_INFO_PATH,
    icom_assignments,
    icom_components,
    icom_dates,
    icom_empty,
    icom_mode,
    icom_modes,
    icom_slots,
    icom_temperature,
    is_icom_slot,
    is_legacy_holiday_path,
    legacy_active,
)
from .pointt import Resource
from .pointt.models import JsonValue

HOLIDAY_LIST_PATH = "/holidayMode/list"
HOLIDAY_CONFIGURATION_PATH = "/holidayMode/configuration"
HOLIDAY_ACTIVE_MODES_PATH = "/holidayMode/activeModes"
HOLIDAY_ACTIVATED_PATH = "/holidayMode/activated"
HOLIDAY_ENABLED_PATH = "/holidayMode/enabled"
HOLIDAY_CREATE_PATH = "/holidayMode"
HOLIDAY_TIMEZONE_PATH = "/gateway/tzInfo/timeZone"
HOLIDAY_RESOURCE_PATHS = (
    HOLIDAY_LIST_PATH,
    HOLIDAY_CONFIGURATION_PATH,
    HOLIDAY_ACTIVE_MODES_PATH,
    HOLIDAY_ACTIVATED_PATH,
    HOLIDAY_ENABLED_PATH,
)
HOLIDAY_PERIOD_PATHS = (HOLIDAY_LIST_PATH, HOLIDAY_CONFIGURATION_PATH)

_MAX_CANDIDATES = 64
_MAX_DEPTH = 6
_MIN_YEAR = 2000
_MAX_YEAR = 2100
_MAX_HOLIDAY_ID = 2_147_483_647
_MAX_NAME_LENGTH = 80
_CIRCUIT_PATTERN = re.compile(r"(?:hc|dhw|vent)\d+", re.IGNORECASE)

# Synonyms in a fixed priority, so a row with several of them is always read
# the same way (D28): full date-times first, then dates, then the bare words;
# a separate time of day comes last.
_START_KEYS = (
    "startdatetime",
    "begindatetime",
    "fromdatetime",
    "starttimestamp",
    "startdate",
    "begindate",
    "fromdate",
    "start",
    "begin",
    "from",
    "validfrom",
    "starttime",
)
_END_KEYS = (
    "enddatetime",
    "todatetime",
    "endtimestamp",
    "enddate",
    "todate",
    "end",
    "to",
    "until",
    "validto",
    "endtime",
)
_ID_KEYS = ("id", "holidayid", "holidaymodeid", "identifier", "uuid")
_NAME_KEYS = ("name", "title", "label")
# A named zone (with its daylight saving time) before a fixed offset.
_TIME_ZONE_KEYS = (
    "timezone",
    "timezoneid",
    "tz",
    "timezoneoffset",
    "utcoffset",
    "offset",
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
    source_path: str = HOLIDAY_CREATE_PATH

    def as_payload(self) -> dict[str, JsonValue]:
        """Return the exact payload shape the cloud expects."""
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
    heating_mode: str | None
    dhw_mode: str | None
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
    source_path: str = HOLIDAY_CREATE_PATH
    can_delete: bool = True
    fix_temperature_step: float | None = None


@dataclass(frozen=True, slots=True)
class HolidayPeriod:
    """One validated PointT holiday period."""

    start: datetime
    end: datetime
    identifier: str | None = None
    name: str | None = None
    all_day: bool = False
    write_values: HolidayWriteValues | None = None
    source_path: str = HOLIDAY_CREATE_PATH


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
    fallback_timezone: tzinfo | None = UTC,
    timezones: Mapping[str, tzinfo | None] | None = None,
    now: datetime | None = None,
) -> HolidayState:
    """Parse all supported PointT holiday shapes without trusting their schema."""
    timezone, timezone_source = _select_timezone(
        resources, fallback_timezone, timezones
    )
    name_codec = _holiday_name_codec(resources)
    invalid = 0
    periods: list[HolidayPeriod] = []
    seen: set[tuple[datetime, datetime, str | None]] = set()
    for candidate in _holiday_candidates(resources):
        parsed = _parse_period(candidate, timezone, name_codec, timezones)
        if parsed is None:
            invalid += 1
            continue
        identity = (parsed.start, parsed.end, parsed.identifier)
        if identity in seen:
            continue
        seen.add(identity)
        periods.append(parsed)
    for source_path in icom_slots(resources):
        resource = resources.get(f"{source_path}/startStop")
        if dates := icom_dates(resource):
            start, end = dates
            periods.append(
                HolidayPeriod(
                    start=datetime.combine(start, time.min, timezone),
                    end=datetime.combine(end + timedelta(days=1), time.min, timezone),
                    identifier=source_path.rsplit("hm", 1)[-1],
                    all_day=True,
                    write_values=icom_write_values(resources, source_path),
                    source_path=source_path,
                )
            )
        elif resource is not None and not icom_empty(resource):
            invalid += 1
    periods.sort(
        key=lambda item: (item.start, item.end, item.source_path, item.identifier or "")
    )

    active = next(
        (
            active_value
            for path in (
                HOLIDAY_ACTIVE_MODES_PATH,
                HOLIDAY_ACTIVATED_PATH,
                HOLIDAY_ENABLED_PATH,
            )
            if (resource := resources.get(path)) is not None
            if (active_value := _active_modes_value(resource)) is not None
        ),
        None,
    )
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone)
    modern_periods = [
        item for item in periods if item.source_path == HOLIDAY_CREATE_PATH
    ]
    if active is None and modern_periods:
        active = any(period.start <= current < period.end for period in modern_periods)
    elif active is None and HOLIDAY_LIST_PATH in resources and invalid == 0:
        active = False
    icom_active = legacy_active(
        {
            path: resource
            for path, resource in resources.items()
            if path != "/system/holidayMode"
        }
    )
    icom_periods = [item for item in periods if item.source_path != HOLIDAY_CREATE_PATH]
    if icom_active is None and icom_periods:
        icom_active = any(
            period.start <= current < period.end for period in icom_periods
        )
    slots = icom_slots(resources)
    if (
        icom_active is None
        and slots
        and all(icom_empty(resources.get(f"{slot}/startStop")) for slot in slots)
    ):
        icom_active = False
    wddw_active = legacy_active(
        {
            path: resource
            for path, resource in resources.items()
            if path == "/system/holidayMode"
        }
    )
    states = [
        value for value in (active, icom_active, wddw_active) if value is not None
    ]
    active = any(states) if states else None

    return HolidayState(
        periods=tuple(periods),
        active=active,
        supported_paths=tuple(
            path for path in resources if is_holiday_resource_path(path)
        ),
        invalid_period_count=invalid,
        timezone_source=timezone_source,
    )


def parse_confirmed_holiday_list(
    resource: Resource,
    resources: Mapping[str, Resource],
    *,
    fallback_timezone: tzinfo,
    timezones: Mapping[str, tzinfo | None] | None = None,
) -> tuple[HolidayPeriod, ...] | None:
    """Require a complete explicit list before confirming a calendar mutation."""
    if resource.path != HOLIDAY_LIST_PATH:
        return None
    if resource.has_value and isinstance(resource.value, list):
        rows = resource.value
    elif resource.has_values or resource.values:
        rows = list(resource.values)
    else:
        return None
    if len(rows) > _MAX_CANDIDATES:
        return None
    timezone, _source = _select_timezone(resources, fallback_timezone, timezones)
    codec = _holiday_name_codec(resources)
    periods: list[HolidayPeriod] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        period = _parse_period(row, timezone, codec, timezones)
        if period is None or (identifier := holiday_period_id(period)) is None:
            return None
        if identifier in seen:
            return None
        seen.add(identifier)
        periods.append(period)
    return tuple(periods)


def parse_holiday_write_configuration(
    resources: Mapping[str, Resource],
    *,
    source_path: str | None = None,
) -> HolidayWriteConfiguration | None:
    """Return a strict write configuration for the dedicated holiday endpoint."""
    if source_path is not None and source_path != HOLIDAY_CREATE_PATH:
        return icom_write_configuration(resources, source_path)
    if source_path is None and HOLIDAY_LIST_PATH not in resources:
        return next(
            (
                configuration
                for slot in icom_slots(resources)
                if icom_empty(resources.get(f"{slot}/startStop"))
                if (configuration := icom_write_configuration(resources, slot))
                is not None
            ),
            None,
        )
    list_resource = resources.get(HOLIDAY_LIST_PATH)
    # PointT exposes the collection through the read-only ``/holidayMode/list``
    # resource, while entries are created and mutated through the separate
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

    for field in (
        "heatingmode",
        "dhwmode",
        "ventilationmode",
        "thermaldesinfection",
        "thermaldisinfection",
    ):
        description = values.get(field)
        if description is None:
            continue
        if not isinstance(description, Mapping):
            return None
        mode_description = {
            _normalize_key(key): value for key, value in description.items()
        }
        options = mode_description.get("allowedvalues")
        if (
            not isinstance(options, list)
            or len(options) > 64
            or any(
                not isinstance(option, str)
                or not 0 < len(option) <= 64
                or not option.isprintable()
                for option in options
            )
        ):
            return None

    heating_mode_values = _allowed_string_values(values.get("heatingmode"))
    dhw_mode_values = _allowed_string_values(values.get("dhwmode"))
    ventilation_mode_values = _allowed_string_values(values.get("ventilationmode"))
    thermal_mode_values = _allowed_string_values(
        values.get("thermaldesinfection") or values.get("thermaldisinfection")
    )
    heating_modes = frozenset(heating_mode_values)
    ventilation_modes = frozenset(ventilation_mode_values)
    thermal_modes = frozenset(thermal_mode_values)
    # These are conservative defaults for a new period. PointT expects OFF
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
        if any(
            key in normalized_fix and _finite_number(normalized_fix[key]) is None
            for key in ("minvalue", "maxvalue")
        ):
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


@lru_cache(maxsize=4096)
def is_holiday_resource_path(path: str) -> bool:
    """Include all three contracts without treating timezone metadata as state."""
    return path in HOLIDAY_RESOURCE_PATHS or is_legacy_holiday_path(path)


@lru_cache(maxsize=4096)
def is_holiday_period_path(path: str) -> bool:
    """WDDW2 has no dates and therefore cannot create a calendar."""
    return path in HOLIDAY_PERIOD_PATHS or (
        path.endswith("/startStop") and is_icom_slot(path.rsplit("/", 1)[0])
    )


def icom_write_values(
    resources: Mapping[str, Resource], source_path: str
) -> HolidayWriteValues | None:
    """Preserve one slot's exact values separately from the modern payload."""
    if not is_icom_slot(source_path):
        return None
    resource = resources.get(f"{source_path}/startStop")
    if icom_dates(resource) is None and not icom_empty(resource):
        return None
    assert resource is not None and isinstance(resource.value, str)
    assignments = icom_assignments(resources.get(f"{source_path}/assignedTo"))
    if assignments is None:
        return None
    modes: dict[str, str | None] = {}
    for field, prefix in (("hcMode", "hc"), ("dhwMode", "dhw")):
        mode_resource = resources.get(f"{source_path}/{field}")
        modes[field] = icom_mode(mode_resource)
        if modes[field] is None and (
            mode_resource is not None
            or any(item.startswith(prefix) for item in assignments)
        ):
            return None
    temperature_resource = resources.get(f"{source_path}/fixTemperature")
    temperature = icom_temperature(temperature_resource)
    if temperature_resource is not None and temperature is None:
        return None
    start, end = resource.value.split("/")
    return HolidayWriteValues(
        start_date=start,
        end_date=end,
        heating_mode=modes["hcMode"],
        dhw_mode=modes["dhwMode"],
        ventilation_mode=None,
        assigned_to=assignments,
        name=None,
        thermal_disinfection=None,
        fix_temperature=temperature or 0.0,
        source_path=source_path,
    )


def icom_write_configuration(
    resources: Mapping[str, Resource], source_path: str
) -> HolidayWriteConfiguration | None:
    """Derive each slot's capabilities from its own current metadata."""
    current = icom_write_values(resources, source_path)
    if current is None:
        return None
    start = resources[f"{source_path}/startStop"]
    assigned = resources.get(f"{source_path}/assignedTo")
    allowed = icom_assignments(assigned, allowed=True)
    if (
        not start.metadata.writable
        or assigned is None
        or not assigned.metadata.writable
        or not allowed
        or not set(current.assigned_to).issubset(allowed)
    ):
        return None
    hc = resources.get(f"{source_path}/hcMode")
    dhw = resources.get(f"{source_path}/dhwMode")
    heating_modes, dhw_modes = icom_modes(hc), icom_modes(dhw)
    for prefix, mode, options in (
        ("hc", current.heating_mode, heating_modes),
        ("dhw", current.dhw_mode, dhw_modes),
    ):
        if any(item.startswith(prefix) for item in allowed) and (
            not options or mode not in options
        ):
            return None
    # I2 (expected, untested on a real device): the rules for the
    # components of ``/system/info``. They only remove choices.
    components = icom_components(resources.get(SYSTEM_INFO_PATH))
    if hidden := components.hidden_circuits:
        # Token 16: these circuits are not offered; one already assigned stays.
        allowed = tuple(
            item
            for item in allowed
            if item not in hidden or item in current.assigned_to
        )
    if components.hmi800_hp_light:
        # HMI800 HP Light: circuits and heating mode stay as the slot has them.
        allowed = tuple(item for item in allowed if item in current.assigned_to)
        heating_modes = tuple(
            mode for mode in heating_modes if mode == current.heating_mode
        )
    if not allowed:
        return None
    fixed = resources.get(f"{source_path}/fixTemperature")
    if fixed is None and any(
        mode in ("FIX_TEMP", "FIX_TEMPERATURE") for mode in heating_modes
    ):
        return None
    minimum = maximum = step = None
    if fixed is not None and fixed.metadata.writable:
        minimum, maximum = fixed.metadata.minimum, fixed.metadata.maximum
        if (
            minimum is None
            or maximum is None
            or not math.isfinite(minimum)
            or not math.isfinite(maximum)
            or minimum > maximum
            or not fixed.metadata.step_size_valid
        ):
            return None
        step = fixed.metadata.step_size
        if step is not None and (not math.isfinite(step) or step <= 0):
            return None
    delete = resources.get(f"{source_path}/delete")
    return HolidayWriteConfiguration(
        date_time_mode="date",
        assigned_to=allowed,
        heating_mode=current.heating_mode,
        dhw_mode=current.dhw_mode,
        ventilation_mode=None,
        thermal_disinfection=None,
        fix_temperature=current.fix_temperature,
        name_codec=None,
        heating_modes=heating_modes,
        dhw_modes=dhw_modes,
        ventilation_modes=(),
        thermal_disinfection_modes=(),
        fix_temperature_min=minimum,
        fix_temperature_max=maximum,
        source_path=source_path,
        can_delete=bool(
            delete is not None
            and delete.metadata.writable
            and delete.metadata.resource_type == "stringValue"
            and delete.metadata.unit in (None, "")
            and not delete.has_values
            and (
                not delete.metadata.allowed_values
                or "TRUE" in delete.metadata.allowed_values
            )
        ),
        fix_temperature_step=step,
    )


def _resource_payloads(resource: Resource) -> Iterable[JsonValue]:
    if resource.has_value:
        yield resource.value
    yield from resource.values


def _holiday_candidates(
    resources: Mapping[str, Resource],
) -> list[Mapping[str, JsonValue]]:
    """Use the same bounded candidates for parsing and timezone preparation."""
    candidates: list[Mapping[str, JsonValue]] = []
    for path in HOLIDAY_PERIOD_PATHS:
        resource = resources.get(path)
        if resource is None:
            continue
        for payload in _resource_payloads(resource):
            candidates.extend(_period_candidates(payload))
            if len(candidates) >= _MAX_CANDIDATES:
                return candidates[:_MAX_CANDIDATES]
    return candidates


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
    timezones: Mapping[str, tzinfo | None] | None,
) -> HolidayPeriod | None:
    normalized = {_normalize_key(key): value for key, value in candidate.items()}
    start_raw = _find_value(normalized, _START_KEYS)
    end_raw = _find_value(normalized, _END_KEYS)
    if start_raw is None or end_raw is None:
        return None

    zone_name = _timezone_name_from_mapping(normalized)
    period_timezone = (
        _resolved_timezone(zone_name, timezones) if zone_name is not None else None
    ) or default_timezone
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

    identifier = _identifier_text(_find_value(normalized, _ID_KEYS))
    raw_name = _safe_text(_find_value(normalized, _NAME_KEYS), 512)
    period_name_codec = _name_codec(normalized.get("nameconfig")) or name_codec
    # C39: an exclusive end at midnight (T00:00:00), as written for an all-day
    # calendar event, is as much a whole day as the legacy T24:00:00 form.
    full_day_timespan = (
        isinstance(start_raw, str)
        and isinstance(end_raw, str)
        and start_raw.endswith("T00:00:00")
        and end_raw.endswith(("T24:00:00", "T00:00:00"))
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
    resources: Mapping[str, Resource],
    fallback_timezone: tzinfo,
    *,
    timezones: Mapping[str, tzinfo | None] | None = None,
) -> tzinfo:
    """Return the validated gateway or Home Assistant holiday time zone."""
    return _select_timezone(resources, fallback_timezone, timezones)[0]


def encode_holiday_name(name: str, codec: HolidayNameCodec | None) -> str | None:
    """Encode a holiday name in the format PointT expects."""
    value = " ".join(name.split())
    if not value or len(value) > _MAX_NAME_LENGTH:
        return None
    if codec is None or len(value) > codec.maximum_length:
        return None
    if codec.coding_type == "ASCII":
        # ASCII-coded names are not stored in PointT; submit null instead.
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

    heating_mode = _optional_mode(normalized.get("heatingmode"))
    dhw_mode = _optional_mode(normalized.get("dhwmode"))
    ventilation_mode = _optional_mode(normalized.get("ventilationmode"))
    thermal_disinfection = _optional_mode(
        normalized.get("thermaldesinfection", normalized.get("thermaldisinfection")),
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
        assigned_to.append(item)

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


def _optional_mode(value: JsonValue | None) -> str | object | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return _INVALID_MODE
    return (
        value if value and len(value) <= 64 and value.isprintable() else _INVALID_MODE
    )


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
        item
        for item in allowed
        if isinstance(item, str) and 0 < len(item) <= 64 and item.isprintable()
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
    resources: Mapping[str, Resource],
    fallback_timezone: tzinfo | None,
    timezones: Mapping[str, tzinfo | None] | None,
) -> tuple[tzinfo, str]:
    timezone_resource = resources.get(HOLIDAY_TIMEZONE_PATH)
    if timezone_resource is not None and isinstance(timezone_resource.value, str):
        timezone = _resolved_timezone(timezone_resource.value, timezones)
        if timezone is not None:
            return timezone, "gateway"
    if fallback_timezone is not None:
        return fallback_timezone, "home_assistant"
    return UTC, "utc_fallback"


def _timezone_name_from_mapping(values: Mapping[str, JsonValue]) -> str | None:
    value = _find_value(values, _TIME_ZONE_KEYS)
    return value.strip() if isinstance(value, str) else None


def _resolved_timezone(
    value: str, timezones: Mapping[str, tzinfo | None] | None
) -> tzinfo | None:
    """Use prepared named zones or construct a fixed offset without file I/O."""
    candidate = value.strip()
    match = re.fullmatch(r"(?:UTC)?([+-])(\d{2}):?(\d{2})", candidate, re.IGNORECASE)
    if match:
        hours, minutes = int(match.group(2)), int(match.group(3))
        if hours > 23 or minutes > 59:
            return None
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(delta if match.group(1) == "+" else -delta)
    return timezones.get(candidate) if timezones is not None else None


def holiday_timezone_names(resources: Mapping[str, Resource]) -> frozenset[str]:
    """Collect only zones used by the bounded holiday parser, without loading them."""
    names: set[str] = set()
    gateway_zone = resources.get(HOLIDAY_TIMEZONE_PATH)
    if gateway_zone is not None and isinstance(gateway_zone.value, str):
        names.add(gateway_zone.value.strip())
    for candidate in _holiday_candidates(resources):
        normalized = {_normalize_key(key): value for key, value in candidate.items()}
        if (name := _timezone_name_from_mapping(normalized)) is not None:
            names.add(name)
    # Fixed offsets are handled entirely in memory. Reject oversized/unprintable
    # input before passing a cloud-supplied name to the timezone database.
    return frozenset(
        name
        for name in names
        if 0 < len(name) <= 255
        and name.isprintable()
        and not re.fullmatch(r"(?:UTC)?[+-]\d{2}:?\d{2}", name, re.IGNORECASE)
    )


def _find_value(
    values: Mapping[str, JsonValue], keys: tuple[str, ...]
) -> JsonValue | None:
    """The value of the first synonym present, in the order of ``keys``."""
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
