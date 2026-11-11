"""Explicit RRC zone contracts, separate from ConnectKey room zones."""

from __future__ import annotations

import base64
import binascii
import math
import re
from collections.abc import Iterable, Mapping
from itertools import zip_longest
from types import MappingProxyType
from typing import Final, Literal

from . import rrc_contract_absence, rrc_contract_zone, rrc_contracts
from .i18n import localize
from .pointt import Resource
from .pointt.models import JsonScalar
from .rrc_contract_base import RRC_ID as RRC_ID
from .unit_contracts import canonical_unit

ZONE = re.compile(rf"^/zones/zn({RRC_ID})(?:/|$)")
PROGRAM = re.compile(rf"^/programs/pg({RRC_ID})(?:/|$)")
ZONE_LIST = "/zones/list"
PROGRAM_LIST = "/programs/list"
# AWAY-01: the single event entries and the active event are write targets
# only; their content comes from the event lists. They are never read.
_EVENT_ENTRY = re.compile(rrc_contract_absence.EVENT_ENTRIES)
# C8 (CT-B02, CT-E01): the radio devices of an RRC controller, one list.
DEVICE_LIST = "/devices/list"
RADIO_DEVICE = re.compile(rf"^/devices/device({RRC_ID})(?:/|$)")
RADIO_FIRMWARE = re.compile(rf"^/devices/device{RRC_ID}/versionFirmware$")
# CT-A03: the product type of an RRC 2.0 room controller.
PRODUCT_TYPE_PATH = "/gateway/productType"
PRODUCT_TYPE = "RRC 2.0"
# The device type the cloud's gateway list reports for these controllers.
GATEWAY_DEVICE_TYPE = "rrc2"
MODE_PATTERN = rf"^/zones/zn{RRC_ID}/userMode$"
MANUAL_PATTERN = rf"^/zones/zn{RRC_ID}/manualTemperatureHeating$"
MODES = frozenset({"manual", "clock"})
# #113: the TC100.2 advertises no allowedValues for userMode; the controller
# has exactly these two modes.
FALLBACK_MODES: tuple[JsonScalar, ...] = ("manual", "clock")
TEMPERATURES = (
    "temperatureActual",
    "temperatureHeatingSetpoint",
    "manualTemperatureHeating",
    "clockOverride/temperatureHeating",
)
# W1 adds zone leaves through its contract (``rrc_contract_zone.zone_fields``).
ZONE_FIELDS = (*TEMPERATURES, "userMode", "clockProgram", *rrc_contracts.zone_fields())
# W1: the zone details. Only the zone entities (``rrc_zone_entities``) read
# them, through ``detail_value``; they are no generic zone scalars.
DETAIL_FIELDS = frozenset(rrc_contracts.zone_fields())
OVERRIDE_FIELD = rrc_contract_zone.OVERRIDE_LEAF
# The writable zone scalars: the mode and the manual setpoint (hardware
# confirmed on the TC100.2) and the temporary setpoint (W1, expected).
CONTROL_FIELDS = frozenset({"userMode", "manualTemperatureHeating", OVERRIDE_FIELD})


def scalar_field(path: str) -> str | None:
    """Match only canonical numeric zone identifiers and documented leaves."""
    match = ZONE.match(path)
    field = path[match.end() :] if match else ""
    return field if field in ZONE_FIELDS else None


def program_list_expected(paths: Iterable[str]) -> bool:
    """Whether a zone selects a program, which needs the program list."""
    return any(scalar_field(path) == "clockProgram" for path in paths)


def is_control(path: str) -> bool:
    """A zone scalar written with the RRC checks (fresh read, metadata grid).

    The temporary setpoint is written only through its contract policy, so
    only on an RRC controller (S11).
    """
    return scalar_field(path) in CONTROL_FIELDS


def available(resource: Resource) -> bool:
    """Missing optional flags are allowed; false or malformed flags are not."""
    metadata = resource.metadata
    return (
        metadata.availability_flags_valid
        and metadata.used is not False
        and metadata.available is not False
    )


type FlagState = Literal["usable", "unused", "unavailable", "invalid"]


def flag_state(resource: Resource) -> FlagState:
    """What the ``used`` and ``available`` flags say about a setting.

    - ``unavailable``: ``available`` false (with any ``used``); the setting
      is not offered.
    - ``unused``: ``available`` not false and ``used`` false; the setting is
      shown, but the controller does not use it now.
    - ``invalid``: a flag that is neither true nor false.
    - ``usable``: everything else; a missing flag counts as true.

    The write rules of the contract settings build on it
    (``writes._contract_availability_issue``). Readings decide on their own
    whether a flag matters to them.
    """
    metadata = resource.metadata
    if not metadata.availability_flags_valid:
        return "invalid"
    if metadata.available is False:
        return "unavailable"
    if metadata.used is False:
        return "unused"
    return "usable"


def is_unread_event_entry(path: str) -> bool:
    """AWAY-01: an event entry (``/events/evN``) or ``/events/activeEvent``.

    An RRC controller lists its events in ``/events/list1`` to ``list3``;
    single entries are only written. Reading them costs a path each and may
    answer HTTP 500, so they are never read.
    """
    return _EVENT_ENTRY.fullmatch(path) is not None


def mode_locked(resource: Resource) -> bool:
    """#113: a used but unavailable mode keeps its value and is not writable.

    Such a mode is locked on the controller, for example while the system
    cools.
    Only ``userMode`` is read this way; other fields stay unavailable.
    """
    metadata = resource.metadata
    return (
        scalar_field(resource.path) == "userMode"
        and metadata.availability_flags_valid
        and metadata.used is True
        and metadata.available is False
    )


def program_unused(resource: Resource) -> bool:
    """#113 (RRC-03): an assigned program that does not act at the moment.

    The TC100.2 sends ``clockProgram`` with ``used`` false while its zone runs
    manually. The program stays assigned and can be changed in this state,
    so the value, its sensor and the selection stay. A hardware confirmation
    is still pending.
    """
    metadata = resource.metadata
    return (
        scalar_field(resource.path) == "clockProgram"
        and metadata.availability_flags_valid
        and metadata.used is False
        and metadata.available is not False
    )


def allowed_values(resource: Resource) -> tuple[JsonScalar, ...]:
    """The advertised values; a mode without any allowedValues key falls back.

    An empty or different list stays as advertised, so it keeps the strict
    rules.
    """
    metadata = resource.metadata
    if (
        scalar_field(resource.path) == "userMode"
        and metadata.allowed_values_missing
        and not metadata.allowed_values
    ):
        return FALLBACK_MODES
    return metadata.allowed_values


def scalar_value(resource: Resource) -> str | int | float | None:
    """Keep native precision and reject sentinels and malformed metadata."""
    field = scalar_field(resource.path)
    metadata = resource.metadata
    value = resource.value
    if (
        field is None
        or field in DETAIL_FIELDS
        or not (
            available(resource) or mode_locked(resource) or program_unused(resource)
        )
        or not resource.has_value
        or resource.has_values
        or resource.references
    ):
        return None
    if field == "userMode":
        return (
            value
            if (
                metadata.resource_type == "stringValue"
                and metadata.unit in (None, "")
                and isinstance(value, str)
                and value in MODES
            )
            else None
        )
    if (
        metadata.resource_type != "floatValue"
        or isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not metadata.numeric_error_states_valid
        or value in metadata.numeric_error_values
    ):
        return None
    for bound in (metadata.minimum, metadata.maximum):
        if bound is not None and not math.isfinite(bound):
            return None
    if (metadata.minimum is not None and value < metadata.minimum) or (
        metadata.maximum is not None and value > metadata.maximum
    ):
        return None
    if field == "clockProgram":
        return (
            int(value)
            if (
                metadata.unit in (None, "")
                and value == int(value)
                and 0 <= value <= 9999
            )
            else None
        )
    return (
        value
        if canonical_unit(metadata.unit) in {"C", "°C", "ºC"} and -50 <= value <= 100
        else None
    )


def display_value(resource: Resource) -> str | int | float | None:
    """The state shown in Home Assistant for a valid reading.

    #113 (RRC-06): a temporary setpoint of 0 means that no override is
    active. It is shown as unknown rather than as 0 °C. The reading itself
    stays valid, so its sensor exists before the first override. A hardware
    confirmation is pending.
    """
    value = scalar_value(resource)
    if scalar_field(resource.path) == "clockOverride/temperatureHeating" and (
        value == 0
    ):
        return None
    return value


def control_issue(resource: Resource) -> str | None:
    """Share read, write and diagnostic checks without creating permissions."""
    if scalar_value(resource) is None:
        return "invalid_rrc_reading"
    metadata = resource.metadata
    if scalar_field(resource.path) == "userMode":
        if resource.value not in allowed_values(resource):
            return "current_option_not_advertised"
    elif not metadata.step_size_valid or (
        metadata.step_size is not None
        and (not math.isfinite(metadata.step_size) or metadata.step_size <= 0)
    ):
        return "invalid_step"
    return None


_DETAIL_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        rrc_contract_zone.NEXT_SETPOINT: "floatValue",
        rrc_contract_zone.MINUTES_TO_NEXT: "floatValue",
        rrc_contract_zone.STATUS: "stringValue",
        rrc_contract_zone.WINDOW_ENABLED: "stringValue",
        rrc_contract_zone.WINDOW_STATUS: "stringValue",
        rrc_contract_zone.VALVE_POSITION: "floatValue",
        rrc_contract_zone.OPTIMUM_STATE: "stringValue",
        rrc_contract_zone.OPTIMUM_RATE: "floatValue",
    }
)
# A state token of the controller (optimum start); other text is not shown.
_STATE_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9 _-]{0,31}")
_MINUTE_UNITS = frozenset({"m", "min", "mins", "minutes"})


def detail_field(path: str) -> str | None:
    """The W1 zone detail leaf of a path, or ``None``."""
    field = scalar_field(path)
    return field if field in DETAIL_FIELDS else None


def detail_shape(resource: Resource) -> bool:
    """Whether a zone detail has the type its entity reads (CT-C06).

    The entity exists for such a resource; its state is ``detail_value``.
    """
    field = detail_field(resource.path)
    return (
        field is not None
        and resource.metadata.resource_type == _DETAIL_TYPES[field]
        and not resource.references
        and not resource.has_values
    )


def detail_value(resource: Resource) -> str | float | bool | None:
    """The state of a zone detail; ``None`` shows it as unknown.

    - ``nextSetpoint``: °C; 999 (P07) or an unused value is none.
    - ``timeToNextSetpoint``: minutes, never negative.
    - ``status``: the option key of a known zone status (CT-G07).
    - ``openWindowDetection/status``: ``True`` for open, ``False`` for
      closed; other values are unknown (the values are expected only).
    - ``actualValvePosition``: percent from 0 to 100.
    - ``optimumStartState``: a short state token; ``optimumStartHeatupRate``
      a non-negative number in the unit the controller names.
    """
    field = detail_field(resource.path)
    metadata = resource.metadata
    if (
        field is None
        or not detail_shape(resource)
        or not resource.has_value
        or not metadata.availability_flags_valid
        or metadata.used is False
        or metadata.available is False
    ):
        return None
    value = resource.value
    if _DETAIL_TYPES[field] == "stringValue":
        if not isinstance(value, str):
            return None
        if field == rrc_contract_zone.STATUS:
            return rrc_contract_zone.ZONE_STATUS.get(value)
        if field == rrc_contract_zone.WINDOW_STATUS:
            return rrc_contract_zone.WINDOW_OPEN.get(value)
        if field == rrc_contract_zone.WINDOW_ENABLED:
            return value if value in ("on", "off") else None
        return value if _STATE_TOKEN.fullmatch(value) else None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not metadata.numeric_error_states_valid
        or value in metadata.numeric_error_values
    ):
        return None
    number = float(value)
    unit = canonical_unit(metadata.unit)
    if field == rrc_contract_zone.NEXT_SETPOINT:
        if number == rrc_contract_zone.NO_NEXT_SETPOINT or unit not in {
            "C",
            "°C",
            "ºC",
        }:
            return None
        return number if -50 <= number <= 100 else None
    if field == rrc_contract_zone.MINUTES_TO_NEXT:
        if (metadata.unit or "") not in _MINUTE_UNITS:
            return None
        return number if 0 <= number <= 7 * 24 * 60 else None
    if field == rrc_contract_zone.VALVE_POSITION:
        return number if unit == "%" and 0 <= number <= 100 else None
    return number if number >= 0 else None


def zone_roots(paths: Iterable[str]) -> tuple[str, ...]:
    """The ``/zones/znN`` roots with a mode, in order of their numbers."""
    roots = {
        path.removesuffix("/userMode")
        for path in paths
        if re.fullmatch(MODE_PATTERN, path)
    }
    return tuple(sorted(roots, key=lambda root: int(root.rsplit("zn", 1)[1])))


def climate_roots(paths: Iterable[str]) -> tuple[str, ...]:
    """The zones that get a climate entity: a mode and a room temperature
    (W1, CT-C03). A zone without its room temperature keeps its select and
    number only."""
    known = set(paths)
    return tuple(
        root for root in zone_roots(known) if f"{root}/temperatureActual" in known
    )


def list_issue(resource: Resource) -> str | None:
    """Why an RRC list supplies no IDs, or ``None`` when it does.

    An empty list is valid and supplies no IDs. Only the value array is an ID
    source, never a guessed ID range.
    """
    parsed = _parse_list(resource)
    return parsed if isinstance(parsed, str) else None


def listed_ids(resource: Resource) -> tuple[int, ...] | None:
    """Only the RRC value array is an ID source, never a guessed ID range."""
    parsed = _parse_list(resource)
    return None if isinstance(parsed, str) else parsed


def _parse_list(resource: Resource) -> tuple[int, ...] | str:
    resource_type = {
        ZONE_LIST: "zoneArray",
        PROGRAM_LIST: "programArray",
        DEVICE_LIST: "deviceArray",
    }.get(resource.path)
    if resource_type is None:
        return "not_a_list"
    if resource.metadata.resource_type != resource_type:
        return "type"
    # #113 (RRC-04): only the value of the program list counts; its used and
    # available flags are ignored. The zone list keeps the rules.
    if resource.path != PROGRAM_LIST and not available(resource):
        return "flags"
    if (
        not resource.has_value
        or not isinstance(resource.value, list)
        or len(resource.value) > 128
    ):
        return "value"
    identifiers: list[int] = []
    for item in resource.value:
        if not isinstance(item, dict):
            return "ids"
        identifier = item.get("id")
        if type(identifier) is not int or not 0 <= identifier <= 9999:
            return "ids"
        if identifier in identifiers:
            return "ids"
        identifiers.append(identifier)
    return tuple(identifiers)


def listed_paths(resource: Resource) -> tuple[str, ...]:
    """The zones and programs of a list; a device list supplies no paths."""
    if resource.path == DEVICE_LIST:
        return ()
    identifiers = listed_ids(resource)
    if identifiers is None:
        return ()
    root = "/zones/zn" if resource.path == ZONE_LIST else "/programs/pg"
    return tuple(f"{root}{identifier}" for identifier in identifiers)


def is_product_type(value: object) -> bool:
    """CT-A03: the product type of an RRC 2.0 controller, in any case."""
    return isinstance(value, str) and value.strip().casefold() == (
        PRODUCT_TYPE.casefold()
    )


def profile_evidence(
    device_type: str | None, resources: Mapping[str, Resource]
) -> tuple[str, ...]:
    """C8: what shows that a gateway is an RRC 2.0 room controller.

    Only resources and the reported device type count, never a model name;
    the evidence selects probes and device names, it offers nothing.
    """
    evidence: list[str] = []
    if isinstance(device_type, str) and (
        device_type.strip().casefold() == GATEWAY_DEVICE_TYPE
    ):
        evidence.append("device_type")
    product = resources.get(PRODUCT_TYPE_PATH)
    if product is not None and is_product_type(product.value):
        evidence.append("product_type")
    if any(ZONE.match(path) for path in resources) or (
        (zones := resources.get(ZONE_LIST)) is not None
        and listed_ids(zones) is not None
    ):
        evidence.append("zones")
    return tuple(evidence)


def in_device_scope(path: str, device_list: Resource | None = None) -> bool:
    """C8 (CT-B02): the only ``/devices`` paths read on an RRC controller.

    The device list supplies type, zone, battery and signal of every radio
    device; per device only its firmware version is read, and the leaves a
    contract declares for its type (``device_leaves``, W6) when the given
    ``device_list`` lists the device. Everything else below ``/devices`` (the
    device keys, settings and valve details) is not read: it costs most of
    the discovery budget and feeds no entity.
    """
    return (
        path in ("/devices", DEVICE_LIST)
        or RADIO_FIRMWARE.fullmatch(path) is not None
        or (device_list is not None and path in device_setting_paths(device_list))
    )


def device_setting_paths(
    resource: Resource, limit: int | None = None
) -> tuple[str, ...]:
    """The contract leaves of the listed devices by their type (W6), at most
    ``limit`` of them; none without such contracts.

    Leaf by leaf: the first leaf of every device (the status of a valve), then
    the second of every device, and so on, each round in list order. A limit
    then drops the least important leaves of all devices, not every leaf of
    the last ones.
    """
    if resource.path != DEVICE_LIST or (identifiers := listed_ids(resource)) is None:
        return ()
    assert isinstance(resource.value, list)  # checked by listed_ids
    per_device: list[tuple[str, ...]] = []
    for identifier, item in zip(identifiers, resource.value, strict=True):
        kind = item.get("type") if isinstance(item, dict) else None
        if not isinstance(kind, str):
            continue
        per_device.append(
            tuple(
                f"/devices/device{identifier}/{leaf}"
                for leaf in rrc_contracts.device_leaves(kind)
            )
        )
    paths = [
        path
        for leaves in zip_longest(*per_device)
        for path in leaves
        if path is not None
    ]
    return tuple(paths if limit is None else paths[:limit])


def device_firmware_paths(resource: Resource, limit: int) -> tuple[str, ...]:
    """The firmware leaf of each listed device, at most ``limit`` of them."""
    if resource.path != DEVICE_LIST:
        return ()
    identifiers = listed_ids(resource) or ()
    return tuple(
        f"/devices/device{identifier}/versionFirmware"
        for identifier in identifiers[:limit]
    )


def catalog_paths(path: str) -> tuple[str, ...]:
    if re.fullmatch(rf"/zones/zn{RRC_ID}", path):
        return tuple(f"{path}/{field}" for field in ZONE_FIELDS)
    if re.fullmatch(rf"/programs/pg{RRC_ID}", path):
        return (f"{path}/week",)
    return ()


def display_name(value: object) -> str | None:
    """#113: a zone or program name, which the controller sends in Base64.

    Canonical Base64 that decodes to printable UTF-8 text is shown decoded;
    any other text as it is. Empty, overlong or unprintable names are none.
    """
    if not isinstance(value, str) or not (candidate := value.strip()):
        return None
    try:
        encoded = base64.b64decode(candidate, validate=True)
        if base64.b64encode(encoded).decode("ascii") == candidate:
            decoded = encoded.decode("utf-8").strip()
            if _printable_name(decoded):
                return decoded
    except binascii.Error, ValueError:  # UnicodeDecodeError is a ValueError
        pass
    return candidate if _printable_name(candidate) else None


def _printable_name(value: str) -> bool:
    return 1 <= len(value) <= 64 and value.isprintable()


# German and English names of the zone leaves; other languages translate the
# English text in their i18n data file.
_LABELS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "temperatureActual": ("Raumtemperatur", "Room temperature"),
        "temperatureHeatingSetpoint": (
            "Aktiver Heizsollwert",
            "Active heating setpoint",
        ),
        "manualTemperatureHeating": (
            "Manueller Heizsollwert",
            "Manual heating setpoint",
        ),
        "clockOverride/temperatureHeating": (
            "Vorübergehender Heizsollwert",
            "Temporary heating setpoint",
        ),
        "userMode": ("Heizungsbetriebsart", "Heating mode"),
        "clockProgram": ("Gewähltes Zeitprogramm", "Selected heating schedule"),
    }
)
# The names of the controls, as whole texts so that a translation need not
# follow the German or English word order.
_CONTROL_LABELS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        field: (f"{german} einstellen", f"Set {english.lower()}")
        for field, (german, english) in _LABELS.items()
    }
)


def name(path: str, language: str, *, control: bool = False) -> str | None:
    field = scalar_field(path)
    if field is None or field not in _LABELS:
        # The zone details are named by their own entities (W1).
        return None
    return localize(language, *(_CONTROL_LABELS if control else _LABELS)[field])
