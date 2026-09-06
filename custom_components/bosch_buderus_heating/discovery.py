"""Bounded discovery of the PointT resource reference tree."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import islice

from .holidays import HOLIDAY_RESOURCE_PATHS
from .pointt import (
    AuthenticationError,
    BatchItemResult,
    PointTClient,
    PointTError,
    ProtocolError,
    RateLimited,
    RequestTimeout,
    Resource,
    ResourceError,
    ServiceUnavailable,
    TransportError,
)
from .pointt.const import MAX_BULK_PATHS
from .pointt.redaction import resource_path_template

_LOGGER = logging.getLogger(__name__)

ROOT_RESOURCE_PATHS: tuple[str, ...] = (
    "/notifications",
    "/gateway",
    "/system",
    "/heatingCircuits",
    "/dhwCircuits",
    "/heatSources",
    "/devices",
    "/solarCircuits",
    "/pool",
    "/ventilation",
    "/zones",
    "/pv",
    *HOLIDAY_RESOURCE_PATHS,
)

MAX_DISCOVERY_DEPTH = 8
MAX_DISCOVERY_RESOURCES = 512


class DiscoveryPathSource(StrEnum):
    """How a path entered the bounded discovery queue."""

    ROOT = "root"
    REFERENCE = "reference"
    OPTIONAL = "optional"


@dataclass(slots=True)
class DiscoveryPathDiagnostic:
    """Privacy-safe result for one requested resource path."""

    source: DiscoveryPathSource
    bulk_result: str = "not_attempted"
    fallback_reason: str | None = None
    fallback_result: str | None = None
    discovered: bool = False

    @property
    def failed(self) -> bool:
        """Whether a finished bulk read remains unrecovered."""
        return (
            self.bulk_result not in ("not_attempted", "pending") and not self.discovered
        )


@dataclass(slots=True)
class DiscoveryDiagnostics:
    """Collect bounded discovery decisions for Home Assistant diagnostics."""

    paths: dict[str, DiscoveryPathDiagnostic] = field(default_factory=dict)
    completed: bool = False
    stop_reason: str = "not_started"
    depth_limit_reached: bool = False
    bulk_calls: int = 0

    def reset(self) -> None:
        """Clear a previous run before rediscovery."""
        self.paths.clear()
        self.completed = False
        self.stop_reason = "running"
        self.depth_limit_reached = False
        self.bulk_calls = 0

    def scheduled(self, path: str, source: DiscoveryPathSource) -> None:
        """Record the highest-priority source assigned to one path."""
        current = self.paths.get(path)
        if current is None:
            self.paths[path] = DiscoveryPathDiagnostic(source=source)
        elif _source_priority(source) < _source_priority(current.source):
            current.source = source

    def bulk_started(self, paths: tuple[str, ...]) -> None:
        """Record one logical batch call, separately from HTTP retries."""
        self.bulk_calls += 1
        for path in paths:
            self.paths[path].bulk_result = "pending"

    def bulk_failed(self, paths: tuple[str, ...], error: PointTError) -> None:
        """Record a request-wide error for exactly the submitted batch."""
        self.stop_reason = _error_category(error)
        for path in paths:
            self.paths[path].bulk_result = self.stop_reason

    def bulk_result(self, result: BatchItemResult) -> None:
        """Record one parsed bulk-item result."""
        item = self.paths[result.path]
        item.bulk_result = _result_category(result)
        item.discovered = result.resource is not None

    def fallback_started(self, path: str, reason: str) -> None:
        """Record one logical individual read, excluding transport retries."""
        self.paths[path].fallback_reason = reason

    def fallback_finished(self, path: str, result: str, *, discovered: bool) -> None:
        """Record the outcome of one individual fallback attempt."""
        item = self.paths[path]
        item.fallback_result = result
        item.discovered = discovered

    def snapshot(self) -> dict[str, object]:
        """Return aggregate counters; callers add selectively redacted paths."""
        items = tuple(self.paths.values())
        fallbacks = tuple(item for item in items if item.fallback_reason is not None)
        references = tuple(
            item for item in items if item.source is DiscoveryPathSource.REFERENCE
        )
        optionals = tuple(
            item for item in items if item.source is DiscoveryPathSource.OPTIONAL
        )
        return {
            "completed": self.completed,
            "stop_reason": self.stop_reason,
            "depth_limit_reached": self.depth_limit_reached,
            "attempts_scope": "logical_resource_reads",
            "bulk_calls": self.bulk_calls,
            "paths_scheduled": len(items),
            "paths_requested": sum(
                item.bulk_result != "not_attempted" for item in items
            ),
            "resources_discovered": sum(item.discovered for item in items),
            "paths_failed": sum(item.failed for item in items),
            "advertised_references": len(references),
            "advertised_references_discovered": sum(
                item.discovered for item in references
            ),
            "optional_paths": len(optionals),
            "optional_paths_discovered": sum(item.discovered for item in optionals),
            "fallback_attempts": len(fallbacks),
            "fallback_successes": sum(
                item.fallback_result == "success" for item in fallbacks
            ),
            "fallback_failures": sum(
                item.fallback_result not in (None, "success") for item in fallbacks
            ),
        }


# Some gateways omit stable public resources from their reference trees or
# advertise an unreadable container around them. Keep these fallbacks narrow:
# every listed path is used by the vendor apps and remains optional when a
# particular installation does not provide it.
OPAQUE_CONTAINER_CHILDREN: dict[str, tuple[str, ...]] = {
    "/gateway": ("/gateway/dataProcessing/status",),
    "/system": (
        "/system/appliance/enabled",
        "/system/appliance/model",
        "/system/appliance/versionFirmware",
        "/system/awayMode/temperature",
        "/system/energyTariff/electricity",
        "/system/energyTariff/gas",
        "/system/energyTariff/oil",
        "/system/energyTariff/pv",
        "/system/globalSeasonOptimizer/currentMode",
        "/system/healthStatus",
        "/system/iSRC/installationStatus",
        "/system/iSRC/supportStatus",
        "/system/lowNoise/duration",
        "/system/lowNoise/mode",
        "/system/powerGuard/active",
        "/system/powerLimitation/active",
        "/system/seasonOptimizer/coolingThreshold",
        "/system/seasonOptimizer/heatingThreshold",
        "/system/seasonOptimizer/mode",
        "/system/sensors/temperatures/chimney",
        "/system/sensors/temperatures/return",
        "/system/sensors/temperatures/supply_t1",
        "/system/sensors/temperatures/supply_t1_setpoint",
        "/system/silentMode/enabled",
        "/system/silentMode/powerReduction",
        "/system/silentMode/startTime",
        "/system/silentMode/stopTime",
        "/system/systemOfUnits",
    ),
    "/system/variableTariff": (
        "/system/variableTariff/ch/currentSetpoint",
        "/system/variableTariff/ch/highPriceDelta",
        "/system/variableTariff/ch/lowPriceDelta",
        "/system/variableTariff/ch/midPriceSetpoint",
        "/system/variableTariff/ch/optimization",
        "/system/variableTariff/ch/status",
        "/system/variableTariff/currentPriceCatagorization",
        "/system/variableTariff/dhw/currentOpmode",
        "/system/variableTariff/dhw/highPriceEnable",
        "/system/variableTariff/dhw/lowPriceEnable",
        "/system/variableTariff/dhw/optimization",
        "/system/variableTariff/dhw/status",
        "/system/variableTariff/priceInfo",
        "/system/variableTariff/supportStatus",
        "/system/variableTariff/tariffId",
    ),
    "/heatSources": (
        "/heatSources/additionalHeater/operationMode",
        "/heatSources/additionalHeater/primary/status",
        "/heatSources/additionalHeater/primary/type",
        "/heatSources/chStatus",
        "/heatSources/compressor/status",
        "/heatSources/currentEmergencyMode",
        "/heatSources/electricityTotalConsumption",
        "/heatSources/gasTotalConsumption",
        "/heatSources/hybrid/activeHeatSource",
        "/heatSources/hybrid/bivalentSetpoint",
        "/heatSources/hybrid/controlStrategy",
        "/heatSources/hybrid/outdoorStatus",
        "/heatSources/hybrid/outdoorVariant",
        "/heatSources/hybrid/reminderDate",
        "/heatSources/hybrid/reminderEnable",
        "/heatSources/hybrid/reminderLapsed",
        "/heatSources/numberOfRefrigerantCircuitsInstalled",
        "/heatSources/Source/eHeater/status",
        "/heatSources/passiveCooling/inflowTemp",
        "/heatSources/poolSetpointTemperature",
        "/heatSources/poolStatus",
        "/heatSources/poolTemperature",
        "/heatSources/pvContactState",
        "/heatSources/smartFunction/active",
        "/heatSources/smartFunction/enabled",
        "/heatSources/standbyMode",
        "/heatSources/type",
        "/heatSources/workingTime/totalSystem",
    ),
    "/heatSources/emon": (
        "/heatSources/emon/totalConsumption",
        "/heatSources/emon/chConsumption",
        "/heatSources/emon/dhwConsumption",
        "/heatSources/emon/coolingConsumption",
        "/heatSources/emon/poolConsumption",
    ),
    "/dhwCircuits": ("/dhwCircuits/waterTotalConsumption",),
    "/solarCircuits": ("/solarCircuits/sc1",),
    "/pool": (
        "/pool/additionalHeater/poolMode",
        "/pool/currentTemp",
        "/pool/enabled",
        "/pool/setpointTemp",
    ),
    "/ventilation": (
        "/ventilation/operationModes/manual/fanSetpoint",
        "/ventilation/zone1",
    ),
    "/zones": (
        "/zones/configuration",
        "/zones/list",
    ),
    "/pv": (
        "/pv/enable",
        "/pv/list",
        "/pv/surplusAvailable",
    ),
}

_HEATING_CIRCUIT_PATH = re.compile(r"^/heatingCircuits/hc[^/]+$", re.IGNORECASE)
_DHW_CIRCUIT_PATH = re.compile(r"^/dhwCircuits/dhw[^/]+$", re.IGNORECASE)
_HEAT_SOURCE_PATH = re.compile(r"^/heatSources/hs[^/]+$", re.IGNORECASE)
_SOLAR_CIRCUIT_PATH = re.compile(r"^/solarCircuits/[^/]+$", re.IGNORECASE)
_VENTILATION_ZONE_PATH = re.compile(r"^/ventilation/zone[^/]+$", re.IGNORECASE)
_ZONE_PATH = re.compile(r"^/zones/zone[^/]+$", re.IGNORECASE)
_DEVICE_PATH = re.compile(r"^/devices/(?!list$)[^/]+$", re.IGNORECASE)

_HEATING_CIRCUIT_OPTIONAL_SUFFIXES = (
    "/actualHumidity",
    "/actualSupplyTemperature",
    "/awayTemperature",
    "/boostDuration",
    "/boostMode",
    "/boostRemainingTime",
    "/boostTemperature",
    "/cooling/controlType",
    "/cooling/manualRoomSetpoint",
    "/cooling/operationMode",
    "/cooling/outdoorThreshold",
    "/cooling/roomTempSetpoint",
    "/cooling/temperatureLevels/on",
    "/cooling/temporaryRoomSetpoint",
    "/openWindowDetection/enabled",
    "/openWindowDetection/status",
    "/operationSetpoints",
    "/pumpModulation",
    "/roomtemperature",
    "/setpointOptimization",
    "/suWiCoolingThreshold",
    "/suWiThreshold",
    "/temporaryRoomSetpoint",
)

_DHW_CIRCUIT_OPTIONAL_SUFFIXES = (
    "/currentFriwaSupplyTemperature",
    "/friwaPrimaryPumpModulation",
    "/inletTemperature",
    "/learningWeek",
    "/manualsetpoint",
    "/monitorValues",
    "/numberOfShowersAvailable",
    "/operationSetpoints",
    "/outletTemperature",
    "/outTemp",
    "/recirculation/enabled",
    "/safetyTemperature",
    "/sensor/airBoxTemperature",
    "/sensor/atmosphericPressure",
    "/sensor/exhaustFlueGasTemperature",
    "/sensor/externalTankTemperature",
    "/sensor/fanSpeed",
    "/sensor/gasFlow",
    "/sensor/heatExchangerFlueGasTemperature",
    "/sensor/heatExchangerTemperature",
    "/sensor/waterFlow",
    "/volumeFlow",
    "/waterTotalConsumption",
)

_HEAT_SOURCE_OPTIONAL_SUFFIXES = (
    "/actualPower",
    "/brineCircuit/collectorInflowTemp",
    "/brineCircuit/collectorOutflowTemp",
    "/defrostActive",
    "/electricityTotalConsumption",
    "/emon/totalConsumption",
    "/operationHours",
    "/powerPercentage",
)

_SOLAR_CIRCUIT_OPTIONAL_SUFFIXES = (
    "/collectorTemperature",
    "/dhwTankBottomTemperature",
    "/maxCylinderTemperature",
    "/maxTemperatureReached",
    "/pumpModulation",
    "/solarYield",
)

_VENTILATION_ZONE_OPTIONAL_SUFFIXES = (
    "/exhaustFanLevel",
    "/filter/maxRunTime",
    "/filter/remainingTime",
    "/maxIndoorAirQuality",
    "/maxRelativeHumidity",
    "/operationMode",
    "/sensors/supplyTemp",
    "/ventilationLevels",
)

_ZONE_OPTIONAL_SUFFIXES = (
    "/averageActualHumidity",
    "/averageCurrentTemperature",
    "/childLock",
    "/currentRoomSetpoint",
    "/cool/manualRoomSetpoint",
    "/cool/operationMode",
    "/cool/temporaryRoomSetpoint",
    "/heat/manualRoomSetpoint",
    "/heat/operationMode",
    "/heat/temporaryRoomSetpoint",
    "/heatCool/manualRoomSetpoint",
    "/heatCool/operationMode",
    "/heatCool/temporaryRoomSetpoint",
    "/icon",
    "/name",
)

_DEVICE_OPTIONAL_SUFFIXES = (
    "/actualHumidity",
    "/assignedHC",
    "/battery",
    "/errors",
    "/name",
    "/productName",
    "/rfTimeofConnectionLost",
    "/roomtemperature",
    "/signal",
    "/type",
    "/versionFirmware",
    "/zoneId",
)


async def async_discover_resources(
    client: PointTClient,
    gateway_id: str,
    *,
    roots: tuple[str, ...] = ROOT_RESOURCE_PATHS,
    maximum_depth: int = MAX_DISCOVERY_DEPTH,
    maximum_resources: int = MAX_DISCOVERY_RESOURCES,
    diagnostics: DiscoveryDiagnostics | None = None,
) -> dict[str, Resource]:
    """Follow PointT references without escaping configured safety bounds."""
    if maximum_depth < 0 or maximum_resources < 1:
        raise ValueError("Discovery bounds must be positive")

    report = diagnostics or DiscoveryDiagnostics()
    report.reset()
    referenced_pending: dict[str, int] = {}
    optional_pending: dict[str, int] = {}
    processed: set[str] = set()
    depth_limited: set[str] = set()

    def schedule(path: str, depth: int, source: DiscoveryPathSource) -> None:
        report.scheduled(path, source)
        if path in processed:
            return
        depth = min(
            depth,
            referenced_pending.get(path, depth),
            optional_pending.get(path, depth),
        )
        if depth > maximum_depth:
            depth_limited.add(path)
            report.depth_limit_reached = True
            return
        depth_limited.discard(path)
        report.depth_limit_reached = bool(depth_limited)
        target = (
            optional_pending
            if report.paths[path].source is DiscoveryPathSource.OPTIONAL
            else referenced_pending
        )
        if target is referenced_pending:
            optional_pending.pop(path, None)
        target[path] = depth

    for root in roots:
        schedule(root, 0, DiscoveryPathSource.ROOT)

    discovered: dict[str, Resource] = {}
    while referenced_pending or optional_pending:
        capacity = min(MAX_BULK_PATHS, maximum_resources - len(processed))
        if capacity <= 0:
            report.stop_reason = "resource_limit"
            break
        # Leave spare reference-batch capacity unused: newly returned references
        # must be expanded before optional probes can spend the path budget.
        pending = referenced_pending or optional_pending
        frontier_entries = dict(islice(pending.items(), capacity))
        frontier = tuple(frontier_entries)
        for path in frontier:
            del pending[path]
        processed.update(frontier)
        report.bulk_started(frontier)
        try:
            results = await client.get_resources_bulk(gateway_id, frontier)
        except ProtocolError as err:
            _LOGGER.debug(
                "PointT discovery bulk envelope was unusable for %d paths: %s",
                len(frontier),
                type(err).__name__,
            )
            results = tuple(
                BatchItemResult(
                    gateway_id=gateway_id,
                    path=path,
                    status=None,
                    error=err,
                )
                for path in frontier
            )
        except PointTError as err:
            report.bulk_failed(frontier, err)
            raise
        for result in results:
            report.bulk_result(result)
        if any(result.status == 429 for result in results):
            report.stop_reason = "rate_limited"
            raise RateLimited(retry_after=None)
        results = await _recover_invalid_bulk_results(
            client,
            gateway_id,
            results,
            diagnostics=report,
        )
        for result in results:
            depth = frontier_entries[result.path]
            resource = result.resource
            if resource is None:
                _LOGGER.debug(
                    "Ignoring PointT discovery item %s: status=%s, error=%s (%s)",
                    resource_path_template(result.path),
                    result.status,
                    type(result.error).__name__ if result.error else "none",
                    result.error or "no parsed resource",
                )
                continue
            discovered[result.path] = resource
            for fallback in _optional_children(result.path):
                schedule(fallback, depth + 1, DiscoveryPathSource.OPTIONAL)
            for reference in resource.references:
                child = reference.path
                if not _is_allowed_reference(child, roots):
                    continue
                schedule(child, depth + 1, DiscoveryPathSource.REFERENCE)
                for fallback in _optional_children(child):
                    schedule(fallback, depth + 2, DiscoveryPathSource.OPTIONAL)
    else:
        report.completed = not report.depth_limit_reached
        report.stop_reason = "complete" if report.completed else "depth_limit"
    return discovered


async def _recover_invalid_bulk_results(
    client: PointTClient,
    gateway_id: str,
    results: tuple[BatchItemResult, ...],
    *,
    diagnostics: DiscoveryDiagnostics,
) -> tuple[BatchItemResult, ...]:
    """Retry every recoverable discovery item once with an individual read."""
    recovered: list[BatchItemResult] = []
    for result in results:
        fallback_reason = result.fallback_reason
        if fallback_reason is None:
            recovered.append(result)
            continue

        diagnostics.fallback_started(result.path, fallback_reason)
        _LOGGER.debug(
            "Retrying recoverable PointT discovery item %s with one individual "
            "GET: reason=%s, server_status=%s, gateway_status=%s, error=%s",
            resource_path_template(result.path),
            fallback_reason,
            result.server_status,
            result.gateway_status,
            result.error,
        )
        try:
            resource = await client.get_resource(
                gateway_id, result.path, fallback_reason=fallback_reason
            )
        except (AuthenticationError, TransportError) as err:
            category = _error_category(err)
            diagnostics.fallback_finished(result.path, category, discovered=False)
            diagnostics.stop_reason = category
            raise
        except ResourceError as err:
            diagnostics.fallback_finished(
                result.path, f"http_{err.status}", discovered=False
            )
            recovered.append(
                BatchItemResult(gateway_id, result.path, err.status, error=err)
            )
        except PointTError as err:
            diagnostics.fallback_finished(
                result.path, _error_category(err), discovered=False
            )
            recovered.append(BatchItemResult(gateway_id, result.path, None, error=err))
        else:
            diagnostics.fallback_finished(result.path, "success", discovered=True)
            recovered.append(
                BatchItemResult(gateway_id, result.path, 200, resource=resource)
            )
    return tuple(recovered)


def _optional_children(path: str) -> tuple[str, ...]:
    """Return narrowly curated app paths below one discovered container."""
    fixed = OPAQUE_CONTAINER_CHILDREN.get(path, ())
    suffixes: tuple[str, ...] = ()
    if _HEATING_CIRCUIT_PATH.fullmatch(path):
        suffixes = _HEATING_CIRCUIT_OPTIONAL_SUFFIXES
    elif _DHW_CIRCUIT_PATH.fullmatch(path) and path != "/dhwCircuits/list":
        suffixes = _DHW_CIRCUIT_OPTIONAL_SUFFIXES
    if _HEAT_SOURCE_PATH.fullmatch(path):
        suffixes = _HEAT_SOURCE_OPTIONAL_SUFFIXES
    elif _SOLAR_CIRCUIT_PATH.fullmatch(path):
        suffixes = _SOLAR_CIRCUIT_OPTIONAL_SUFFIXES
    elif _VENTILATION_ZONE_PATH.fullmatch(path):
        suffixes = _VENTILATION_ZONE_OPTIONAL_SUFFIXES
    elif _ZONE_PATH.fullmatch(path):
        suffixes = _ZONE_OPTIONAL_SUFFIXES
    elif _DEVICE_PATH.fullmatch(path):
        suffixes = _DEVICE_OPTIONAL_SUFFIXES
    return (*fixed, *(f"{path}{suffix}" for suffix in suffixes))


def _is_allowed_reference(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in roots)


def _source_priority(source: DiscoveryPathSource) -> int:
    """Keep advertised paths ahead of speculative app-path probes."""
    return 1 if source is DiscoveryPathSource.OPTIONAL else 0


def _result_category(result: BatchItemResult) -> str:
    """Return a stable, payload-free discovery result category."""
    if result.resource is not None:
        return "success"
    if isinstance(result.error, ProtocolError):
        return "malformed"
    if result.status is not None:
        return f"http_{result.status}"
    if result.error is not None:
        return _error_category(result.error)
    return "unavailable"


def _error_category(error: PointTError) -> str:
    """Describe an error without its message, URL, or response body."""
    if isinstance(error, AuthenticationError):
        return "authentication_error"
    if isinstance(error, RateLimited):
        return "rate_limited"
    if isinstance(error, RequestTimeout):
        return "timeout"
    if isinstance(error, ServiceUnavailable):
        return "service_unavailable"
    if isinstance(error, TransportError):
        return "transport_error"
    if isinstance(error, ProtocolError):
        return "malformed"
    if isinstance(error, ResourceError):
        return f"http_{error.status}"
    return "request_failed"
