"""Bounded discovery of the PointT resource reference tree."""

from __future__ import annotations

import logging
import re

from .holidays import HOLIDAY_RESOURCE_PATHS
from .pointt import (
    AuthenticationError,
    BatchItemResult,
    PointTClient,
    PointTError,
    ProtocolError,
    RateLimited,
    Resource,
    ResourceError,
)
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
MAX_DISCOVERY_FALLBACK_PATHS = 30

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
) -> dict[str, Resource]:
    """Follow PointT references without escaping configured safety bounds."""
    if maximum_depth < 0 or maximum_resources < 1:
        raise ValueError("Discovery bounds must be positive")

    pending = [(path, 0) for path in roots]
    queued = set(roots)
    discovered: dict[str, Resource] = {}
    fallback_remaining = MAX_DISCOVERY_FALLBACK_PATHS
    while pending:
        depth = pending[0][1]
        capacity = maximum_resources - len(discovered)
        if capacity <= 0:
            break
        frontier: list[str] = []
        while pending and pending[0][1] == depth and len(frontier) < capacity:
            frontier.append(pending.pop(0)[0])
        try:
            results = await client.get_resources_bulk(gateway_id, frontier)
        except AuthenticationError, RateLimited:
            raise
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
        results, fallback_used = await _recover_invalid_bulk_results(
            client,
            gateway_id,
            results,
            limit=fallback_remaining,
        )
        fallback_remaining -= fallback_used
        for result in results:
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
            if len(discovered) >= maximum_resources or depth >= maximum_depth:
                continue
            for fallback in _optional_children(result.path):
                if depth + 1 <= maximum_depth and fallback not in queued:
                    queued.add(fallback)
                    pending.append((fallback, depth + 1))
            for reference in resource.references:
                child = reference.path
                if child in queued or not _is_allowed_reference(child, roots):
                    continue
                queued.add(child)
                pending.append((child, depth + 1))
                for fallback in _optional_children(child):
                    if depth + 2 <= maximum_depth and fallback not in queued:
                        queued.add(fallback)
                        pending.append((fallback, depth + 2))
    return discovered


async def _recover_invalid_bulk_results(
    client: PointTClient,
    gateway_id: str,
    results: tuple[BatchItemResult, ...],
    *,
    limit: int,
) -> tuple[tuple[BatchItemResult, ...], int]:
    """Retry malformed bulk items individually within one discovery budget."""
    recovered: list[BatchItemResult] = []
    used = 0
    for result in results:
        if result.resource is not None or not isinstance(result.error, ProtocolError):
            recovered.append(result)
            continue
        if used >= limit:
            recovered.append(result)
            continue

        used += 1
        client.metrics.record_fallback_request()
        _LOGGER.debug(
            "Retrying malformed PointT discovery item %s with one individual GET: %s",
            resource_path_template(result.path),
            result.error,
        )
        try:
            resource = await client.get_resource(gateway_id, result.path)
        except AuthenticationError, RateLimited:
            raise
        except ResourceError as err:
            recovered.append(
                BatchItemResult(gateway_id, result.path, err.status, error=err)
            )
        except PointTError as err:
            recovered.append(BatchItemResult(gateway_id, result.path, None, error=err))
        else:
            recovered.append(
                BatchItemResult(gateway_id, result.path, 200, resource=resource)
            )
    return tuple(recovered), used


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
