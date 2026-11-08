"""A common, read-only observation format for cloud and local measurements.

Observations keep their origin, raw path, interpretation and timing. They carry
no write rights or command metadata: controls keep using the cloud resource
metadata of the access that actually writes. Combining two sources requires an
explicit pair from the versioned catalog below; an equal path name alone is
not sufficient, and type and unit must match at runtime as well.

This module neither selects a source nor feeds entities. Source selection,
freshness rules and entity supply belong to later steps.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import lru_cache
from typing import Any

from .local_api.models import (
    LocalObservation,
    NamedValues,
    ScalarValue,
    ValueArray,
)
from .resource_catalog import function_group_for_path
from .unit_contracts import TEMPERATURE_UNITS
from .unit_contracts import canonical_unit as contract_unit

type Number = int | Decimal
type Value = Number | str | bool


class SourceKind(StrEnum):
    CLOUD = "cloud"
    LOCAL = "local"


class ValueStatus(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    EMPTY = "empty"
    DEVICE_ERROR = "device_error"
    STATE_MARKER = "state_marker"
    UNKNOWN_UNIT = "unknown_unit"
    UNSUPPORTED = "unsupported"


class PairResult(StrEnum):
    AGREE = "agree"
    DIFFER = "differ"
    INCOMPARABLE = "incomparable"
    # O91: a counter whose later reading is higher, read at least one poll
    # period apart: it most likely counted on between the two reads.
    CHANGED_BETWEEN_READS = "changed_between_reads"


# The shortest poll period of either access; reads closer together than this
# that still differ are reported as differing.
CHANGE_GAP_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ObservationTiming:
    """Times are kept per source and never compared as one shared clock.

    ``requested_at`` is the start of the attempt when the source knows it.
    ``received_at`` is when the integration accepted the answer. A device time
    reported inside a payload is kept separately and is not a receive time.
    """

    received_at: datetime
    requested_at: datetime | None = None
    received_monotonic: float | None = None
    device_time: datetime | None = None


@dataclass(frozen=True, slots=True, repr=False)
class Observation:
    """One interpreted reading from one source, without write semantics."""

    source: SourceKind
    connection_id: str
    target_id: str
    path: str
    group_kind: str | None
    group_id: str | None
    resource_type: str | None
    value: Value | None
    parts: tuple[tuple[str, Number], ...]
    unit: str | None
    status: ValueStatus
    current: bool
    timing: ObservationTiming = field(compare=False)
    # B2: the entries of a list of texts (``arrayData``); ``None`` for every
    # other shape. An empty list is a valid reading (nothing active).
    items: tuple[str, ...] | None = None
    # C5: the texts a local reading declares as its allowed values.
    declared: tuple[str, ...] = ()
    # C10-b: the names of a local reading's state map, and those it matches.
    state_names: tuple[str, ...] = ()
    matched: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            f"Observation(source={self.source.value}, status={self.status.value}, "
            f"type={self.resource_type}, unit={self.unit}, current={self.current})"
        )


# ----------------------------------------------------------------- catalog

PAIR_CATALOG_VERSION = 1
_EVIDENCE = (
    "official local OpenAPI k-40-rf at commit 78e36b3: same documented path",
    "PointT and local responses on the reference installation: same path, type, "
    "unit and value",
)


@dataclass(frozen=True, slots=True)
class PairDefinition:
    """A verified equivalence of one cloud and one local resource template."""

    measurement: str
    template: str
    counter: bool = False
    evidence: tuple[str, ...] = _EVIDENCE
    # B2: a list whose entries count regardless of their order (the entity
    # combines them into one state); otherwise the order is part of it.
    unordered: bool = False


def _pair(
    measurement: str, template: str, *, counter: bool = False, unordered: bool = False
) -> PairDefinition:
    return PairDefinition(measurement, template, counter, unordered=unordered)


# Variant A: only identical paths with matching type, unit and observed value.
# Network, administration and configuration objects are deliberately excluded.
PAIR_CATALOG: tuple[PairDefinition, ...] = (
    _pair("outdoor_temperature", "/system/sensors/temperatures/outdoor_t1"),
    _pair("system_type", "/system/type"),
    _pair("system_bus", "/system/bus"),
    _pair("power_limitation_active", "/system/powerLimitation/active"),
    _pair("isrc_installation_status", "/system/iSRC/installationStatus"),
    _pair("isrc_support_status", "/system/iSRC/supportStatus"),
    _pair("variable_tariff_support_status", "/system/variableTariff/supportStatus"),
    _pair("gateway_brand", "/gateway/brand"),
    _pair("gateway_firmware", "/gateway/versionFirmware"),
    _pair("gateway_hardware", "/gateway/versionHardware"),
    _pair("room_setpoint_current", "/heatingCircuits/{hc}/currentRoomSetpoint"),
    _pair("summer_winter_mode", "/heatingCircuits/{hc}/currentSuWiMode"),
    _pair("max_flow_temperature", "/heatingCircuits/{hc}/maxFlowTemp"),
    _pair("heating_circuit_status", "/heatingCircuits/{hc}/overallStatus"),
    _pair("hot_water_temperature", "/dhwCircuits/{dhw}/actualTemp"),
    _pair("hot_water_charge_remaining", "/dhwCircuits/{dhw}/chargeRemainingTime"),
    _pair("hot_water_setpoint_current", "/dhwCircuits/{dhw}/currentSetpoint"),
    _pair("hot_water_temperature_level", "/dhwCircuits/{dhw}/currentTemperatureLevel"),
    _pair("hot_water_status", "/dhwCircuits/{dhw}/overallStatus"),
    _pair("heat_demand", "/heatSources/actualHeatDemand", unordered=True),
    _pair("modulation", "/heatSources/actualModulation"),
    _pair("supply_temperature", "/heatSources/actualSupplyTemperature"),
    _pair("return_temperature", "/heatSources/returnTemperature"),
    _pair("system_pressure", "/heatSources/systemPressure"),
    _pair(
        "additional_heater_flow_temperature", "/heatSources/additionalHeater/flowTemp"
    ),
    _pair("compressor_electric_power", "/heatSources/compressor/powerElecActual"),
    _pair("electric_heater_power", "/heatSources/eHeater/powerElecActual"),
    _pair("electric_heater_status", "/heatSources/Source/eHeater/status"),
    _pair("energy_manager_status", "/heatSources/emStatus"),
    _pair("flame_status", "/heatSources/flameStatus"),
    _pair("heat_pump_type", "/heatSources/{hs}/heatPumpType"),
    _pair("compressor_speed", "/heatSources/{hs}/refrigerant/compressorActualSpeed"),
    _pair("refrigerant_status", "/heatSources/{hs}/refrigerant/status"),
    _pair("condenser_supply_temperature", "/heatSources/{hs}/supplyFlowCondenserTemp"),
    _pair("energy_heating", "/heatSources/emon/chConsumption", counter=True),
    _pair("energy_cooling", "/heatSources/emon/coolingConsumption", counter=True),
    _pair("energy_hot_water", "/heatSources/emon/dhwConsumption", counter=True),
    _pair("energy_total", "/heatSources/emon/totalConsumption", counter=True),
    _pair("starts_system", "/heatSources/numberOfStarts", counter=True),
    _pair("starts_heat_source", "/heatSources/{hs}/numberOfStarts", counter=True),
    _pair("working_time_system", "/heatSources/workingTime/totalSystem", counter=True),
    _pair("working_time_heat_source", "/heatSources/{hs}/workingTime", counter=True),
)

WORKING_TIME_MEASUREMENT = "working_time_heat_source"

_PLACEHOLDERS = {"{hc}": r"(hc\d+)", "{dhw}": r"(dhw\d+)", "{hs}": r"(hs\d+)"}


def _template_pattern(template: str) -> re.Pattern[str]:
    pattern = re.escape(template)
    for placeholder, group in _PLACEHOLDERS.items():
        pattern = pattern.replace(re.escape(placeholder), group)
    return re.compile(f"^{pattern}$")


_PATTERNS = tuple((pair, _template_pattern(pair.template)) for pair in PAIR_CATALOG)


# Entity state and diagnostics ask for the same few hundred paths again and
# again; the catalog is fixed, so the regular-expression scan is cached.
@lru_cache(maxsize=4096)
def pair_for_path(path: str) -> PairDefinition | None:
    """Return the catalog pair for one concrete path, if any."""
    for pair, pattern in _PATTERNS:
        if pattern.match(path):
            return pair
    return None


# ---------------------------------------------------------------- adapters


def from_cloud_snapshot(
    snapshot: Any, *, connection_id: str, target_id: str
) -> Observation:
    """Interpret a PointT resource snapshot without its write metadata."""
    resource = snapshot.resource
    metadata = resource.metadata
    status = ValueStatus.VALID
    value: Value | None = None
    parts: tuple[tuple[str, Number], ...] = ()
    items: tuple[str, ...] | None = None
    if (array := cloud_items(resource)) is not None:
        items = array
    elif resource.values:
        parts = _cloud_parts(resource.values)
        status = ValueStatus.VALID if parts else ValueStatus.UNSUPPORTED
    elif not resource.has_value:
        status = ValueStatus.MISSING
    else:
        value = _normalize(resource.value)
        if value is None or value == "":
            status = ValueStatus.EMPTY
            value = None
        elif _is_number(value) and any(
            _number_equal(value, error) for error in metadata.numeric_error_values
        ):
            status = ValueStatus.DEVICE_ERROR
            value = None
    group = function_group_for_path(resource.path)
    return Observation(
        source=SourceKind.CLOUD,
        connection_id=connection_id,
        target_id=target_id,
        path=resource.path,
        group_kind=group.kind if group else None,
        group_id=group.logical_id if group else None,
        resource_type=metadata.resource_type,
        value=value,
        parts=parts,
        unit=metadata.unit or None,
        status=status,
        current=bool(snapshot.available and snapshot.freshness.value == "fresh"),
        timing=ObservationTiming(
            received_at=snapshot.last_success, requested_at=snapshot.last_attempt
        ),
        items=items,
    )


def cloud_items(resource: Any) -> tuple[str, ...] | None:
    """B2: the entries of a cloud list of texts (``arrayData``), else None."""
    if (
        resource.metadata.resource_type != "arrayData"
        or resource.has_value
        or not (resource.has_values or resource.values)
        or not all(isinstance(item, str) for item in resource.values)
    ):
        return None
    return tuple(resource.values)


def from_local_observation(
    observation: LocalObservation,
    *,
    connection_id: str,
    target_id: str,
    current: bool,
) -> Observation:
    """Interpret a local observation; reported write flags are ignored."""
    data = observation.data
    status = ValueStatus.VALID
    value: Value | None = None
    parts: tuple[tuple[str, Number], ...] = ()
    items: tuple[str, ...] | None = None
    declared: tuple[str, ...] = ()
    state_names: tuple[str, ...] = ()
    matched: tuple[str, ...] = ()
    unit: str | None = None
    if isinstance(data, ScalarValue):
        unit = data.unit
        state_names = tuple(item.name for item in data.states)
        matched = tuple(data.matched_states)
        if _local_error_marker(data):
            status = ValueStatus.STATE_MARKER
        elif any(issue.value == "unknown_unit" for issue in data.issues):
            status = ValueStatus.UNKNOWN_UNIT
            # C7: kept for readings shown without their unknown unit.
            value = data.value if data.value != "" else None
        elif data.value == "":
            status = ValueStatus.EMPTY
        else:
            value = data.value
        declared = tuple(
            item for item in data.allowed_values if isinstance(item, str) and item
        )
    elif isinstance(data, NamedValues):
        unit = data.unit
        parts = tuple((item.name, item.value) for item in data.values)
        status = ValueStatus.VALID if parts else ValueStatus.EMPTY
    elif isinstance(data, ValueArray) and all(
        isinstance(item, str) for item in data.values
    ):
        # B2: like the cloud, an empty list is a valid reading.
        items = tuple(data.values)
    else:
        status = ValueStatus.UNSUPPORTED
    path = observation.request.path
    group = function_group_for_path(path)
    return Observation(
        source=SourceKind.LOCAL,
        connection_id=connection_id,
        target_id=target_id,
        path=path,
        group_kind=group.kind if group else None,
        group_id=group.logical_id if group else None,
        resource_type=observation.type,
        value=value,
        parts=parts,
        unit=unit,
        status=status,
        current=current,
        timing=ObservationTiming(
            received_at=observation.observed_at,
            received_monotonic=observation.observed_monotonic,
        ),
        items=items,
        declared=declared,
        state_names=state_names,
        matched=matched,
    )


# ------------------------------------------------------------- comparison


@dataclass(frozen=True, slots=True)
class PairComparison:
    """Both observations are kept; this records only how they relate."""

    pair: PairDefinition
    result: PairResult
    reason: str
    cloud: Observation
    local: Observation
    receive_gap_seconds: float | None


def compare(cloud: Observation, local: Observation) -> PairComparison | None:
    """Compare one cloud and one local observation of the same catalog pair."""
    pair = pair_for_path(cloud.path)
    if (
        pair is None
        or cloud.source is not SourceKind.CLOUD
        or local.source is not SourceKind.LOCAL
        or local.path != cloud.path
        or local.target_id != cloud.target_id
    ):
        return None
    gap = abs((cloud.timing.received_at - local.timing.received_at).total_seconds())

    def result(kind: PairResult, reason: str) -> PairComparison:
        return PairComparison(pair, kind, reason, cloud, local, round(gap, 3))

    if cloud.status is not ValueStatus.VALID or local.status is not ValueStatus.VALID:
        return result(PairResult.INCOMPARABLE, "not_valid")
    if cloud.resource_type != local.resource_type:
        return result(PairResult.INCOMPARABLE, "type_differs")
    if not units_match(pair, cloud, local):
        return result(PairResult.INCOMPARABLE, "unit_differs")
    if bool(cloud.parts) != bool(local.parts) or (cloud.items is None) != (
        local.items is None
    ):
        return result(PairResult.INCOMPARABLE, "shape_differs")
    if cloud.items is not None and local.items is not None:
        same = (
            Counter(cloud.items) == Counter(local.items)
            if pair.unordered
            else cloud.items == local.items
        )
    elif cloud.parts:
        same = _parts_map(cloud.parts) == _parts_map(local.parts)
    elif _is_number(cloud.value) and _is_number(local.value):
        same = _number_equal(cloud.value, local.value)
    else:
        same = cloud.value == local.value
    if same:
        return result(PairResult.AGREE, "compared")
    if pair.counter and gap > CHANGE_GAP_SECONDS and _counted_on(cloud, local):
        return result(PairResult.CHANGED_BETWEEN_READS, "compared")
    return result(PairResult.DIFFER, "compared")


def _counted_on(cloud: Observation, local: Observation) -> bool:
    """The later reading of a counter is higher, never lower in any part."""
    if cloud.timing.received_at == local.timing.received_at:
        return False
    earlier, later = (
        (cloud, local)
        if cloud.timing.received_at < local.timing.received_at
        else (local, cloud)
    )
    before, after = _numbers(earlier), _numbers(later)
    if not before or before.keys() != after.keys():
        return False
    return all(after[key] >= before[key] for key in before) and any(
        after[key] > before[key] for key in before
    )


def is_working_time(path: str) -> bool:
    """The working time of one heat source (``/heatSources/{hs}/workingTime``).

    Its cloud value has no unit and counts seconds, the local value reports
    ``s``: the pair agreed in scale on a reference installation (O90).
    """
    pair = pair_for_path(path)
    return pair is not None and pair.measurement == WORKING_TIME_MEASUREMENT


def units_match(pair: PairDefinition, cloud: Observation, local: Observation) -> bool:
    """Same unit, or an established unitless cloud contract of a counter: kWh
    for energy counters, seconds for the working time of a heat source."""
    if canonical_unit(cloud.unit) == canonical_unit(local.unit):
        return True
    if not pair.counter or cloud.unit is not None:
        return False
    if cloud.resource_type == "emonValue" and "/emon/" in cloud.path:
        return canonical_unit(local.unit) == "kWh"
    return (
        pair.measurement == WORKING_TIME_MEASUREMENT
        and canonical_unit(local.unit) == "s"
    )


def _numbers(item: Observation) -> dict[str, Decimal]:
    if item.parts:
        return _parts_map(item.parts)
    if _is_number(item.value):
        return {"value": Decimal(str(item.value))}
    return {}


def _step(numbers: Iterable[Decimal]) -> Decimal | None:
    """Finest decimal step seen in the reported numbers, e.g. 0.1 or 1."""
    exponents = [
        min(exponent, 0)
        for number in numbers
        if number.is_finite()
        and isinstance(exponent := number.normalize().as_tuple().exponent, int)
    ]
    return Decimal(1).scaleb(min(exponents)) if exponents else None


def counter_evidence(comparisons: Iterable[PairComparison]) -> dict[str, Any]:
    """Equality, largest relative difference and resolution of counter pairs.

    Only per catalog measurement and without absolute counter values.
    """
    evidence: dict[str, Any] = {}
    for item in comparisons:
        if not item.pair.counter or item.result is PairResult.INCOMPARABLE:
            continue
        cloud, local = _numbers(item.cloud), _numbers(item.local)
        differences = [
            abs(cloud[key] - local[key]) / abs(cloud[key]) * 100
            if cloud[key] != 0
            else Decimal(0 if local[key] == 0 else 100)
            for key in cloud.keys() & local.keys()
        ]
        cloud_step, local_step = _step(cloud.values()), _step(local.values())
        evidence[item.pair.measurement] = {
            "result": item.result.value,
            "max_difference_percent": float(round(max(differences), 4))
            if differences
            else 0.0,
            "cloud_step": float(cloud_step) if cloud_step is not None else None,
            "local_step": float(local_step) if local_step is not None else None,
            "receive_gap_seconds": item.receive_gap_seconds,
        }
    return dict(sorted(evidence.items()))


def pair_observations(
    cloud: Iterable[Observation], local: Iterable[Observation]
) -> tuple[PairComparison, ...]:
    """Match observations of the same target and path, one pair per path."""
    local_by_key = {
        (item.target_id, item.path): item
        for item in local
        if pair_for_path(item.path) is not None
    }
    results = []
    for item in cloud:
        partner = local_by_key.get((item.target_id, item.path))
        if partner is not None and (comparison := compare(item, partner)) is not None:
            results.append(comparison)
    return tuple(sorted(results, key=lambda item: item.cloud.path))


def pair_summary(comparisons: Iterable[PairComparison]) -> dict[str, Any]:
    """Aggregate comparisons for diagnostics, without values or paths."""
    items = tuple(comparisons)
    gaps = [
        item.receive_gap_seconds
        for item in items
        if item.receive_gap_seconds is not None
    ]
    return {
        "catalog_version": PAIR_CATALOG_VERSION,
        "catalog_pairs": len(PAIR_CATALOG),
        "observed_pairs": len(items),
        "results": dict(sorted(Counter(item.result.value for item in items).items())),
        "incomparable_reasons": dict(
            sorted(
                Counter(
                    item.reason
                    for item in items
                    if item.result is PairResult.INCOMPARABLE
                ).items()
            )
        ),
        "incomparable_units": dict(
            sorted(
                Counter(
                    f"{_unit_label(item.cloud.unit)}|{_unit_label(item.local.unit)}"
                    for item in items
                    if item.reason == "unit_differs"
                ).items()
            )
        ),
        # O91: the catalog names of pairs whose values differ, never values.
        "differing_measurements": sorted(
            {
                item.pair.measurement
                for item in items
                if item.result is PairResult.DIFFER
            }
        ),
        "counter_pairs": sum(item.pair.counter for item in items),
        "counter_evidence": counter_evidence(items),
        "both_current": sum(
            item.cloud.current and item.local.current for item in items
        ),
        "maximum_receive_gap_seconds": max(gaps) if gaps else None,
        # Rarely read static values would hide the gaps of live readings.
        "maximum_receive_gap_seconds_by_class": _gaps_by_class(items),
    }


def _gaps_by_class(items: Iterable[PairComparison]) -> dict[str, float]:
    """The largest receive gap per local poll class of the paired path."""
    from .local_api.catalog import request_for_path
    from .local_api.errors import LocalResourceError
    from .local_polling.policy import category

    gaps: dict[str, float] = {}
    for item in items:
        if item.receive_gap_seconds is None:
            continue
        try:
            name = category(request_for_path(item.cloud.path)).value
        except LocalResourceError, KeyError, ValueError:
            name = "other"
        gaps[name] = max(gaps.get(name, 0.0), item.receive_gap_seconds)
    return dict(sorted(gaps.items()))


# ---------------------------------------------------------------- helpers


# Error markers of both interfaces. Other named states (for example a setpoint
# reported as ``off: 0``) are ordinary values that the cloud shows as numbers.
ERROR_MARKERS = frozenset({"short", "open", "overflow", "underflow", "invalid"})


def canonical_unit(unit: str | None) -> str | None:
    """Unit with equivalent spellings unified; empty means no unit.

    Uses the reviewed alias tables shared with the cloud contracts. Only
    spellings are unified, never a conversion between different scales.
    """
    if not unit:
        return None
    if unit in TEMPERATURE_UNITS:
        return TEMPERATURE_UNITS[unit]
    return contract_unit(unit)


def _local_error_marker(data: ScalarValue) -> bool:
    """Error marker, also as the one-decimal value some devices report.

    The documented example of a return temperature reports an open sensor as
    -3276.8 next to the marker -32768: a float reading equal to a tenth of an
    error marker is a marker, never a temperature.
    """
    if not (type(data.value) is int or isinstance(data.value, Decimal)):
        return False
    if ERROR_MARKERS.intersection(data.matched_states):
        return True
    value = Decimal(str(data.value))
    return any(
        state.name in ERROR_MARKERS and value == Decimal(str(state.value)) / Decimal(10)
        for state in data.states
    )


# Unit spellings reported by either interface; anything else stays anonymous.
_KNOWN_UNITS = frozenset(
    {
        "%",
        "1/min",
        "bar",
        "C",
        "°C",
        "K",
        "count",
        "db",
        "dB",
        "kW",
        "kWh",
        "l/h",
        "l/min",
        "min",
        "mins",
        "s",
        "h",
        "W",
        "Wh",
        "wh",
        "ppm",
        "rpm",
    }
)


def _unit_label(unit: str | None) -> str:
    """Diagnostic label for a unit: known spellings only, never device text."""
    if not unit:
        return "-"
    return unit if unit in _KNOWN_UNITS else "other"


def _normalize(value: Any) -> Value | None:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        try:
            number = Decimal(repr(value))
        except InvalidOperation:  # pragma: no cover - repr of finite floats is valid
            return None
        return int(number) if number == number.to_integral_value() else number
    if isinstance(value, Decimal):
        return value
    return None


def _is_number(value: object) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(
        value, Decimal
    )


def _number_equal(left: object, right: object) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except InvalidOperation:
        return False


def _cloud_parts(values: tuple[Any, ...]) -> tuple[tuple[str, Number], ...]:
    parts: list[tuple[str, Number]] = []
    for item in values:
        if not isinstance(item, Mapping):
            return ()
        for name, raw in item.items():
            number = _normalize(raw)
            if not isinstance(name, str) or not _is_number(number):
                return ()
            parts.append((name, number))  # type: ignore[arg-type]
    names = [name for name, _ in parts]
    return tuple(parts) if len(names) == len(set(names)) else ()


def _parts_map(parts: tuple[tuple[str, Number], ...]) -> dict[str, Decimal]:
    return {name: Decimal(str(value)) for name, value in parts}
