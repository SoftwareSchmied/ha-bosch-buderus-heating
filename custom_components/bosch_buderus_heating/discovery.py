"""Bounded discovery of the PointT resource reference tree."""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import islice

from . import rrc_contracts
from .holidays import HOLIDAY_RESOURCE_PATHS
from .legacy_holidays import (
    ICOM_FIELDS,
    ICOM_HOLIDAY_ROOT,
    WDDW2_HOLIDAY_PATH,
    is_icom_slot,
)
from .pointt import (
    AuthenticationError,
    BatchItemResult,
    Gateway,
    PointTClient,
    PointTError,
    ProtocolError,
    RateLimited,
    RequestTimeout,
    Resource,
    ResourceError,
    ServiceUnavailable,
    TransportError,
    UnexpectedHttpStatus,
)
from .pointt.const import MAX_BULK_PATHS
from .pointt.redaction import resource_path_template
from .resource_catalog import is_never_read
from .rrc import (
    DEVICE_LIST,
    PRODUCT_TYPE_PATH,
    PROGRAM_LIST,
    RRC_ID,
    ZONE,
    ZONE_LIST,
    catalog_paths,
    device_firmware_paths,
    device_setting_paths,
    in_device_scope,
    is_product_type,
    listed_ids,
    listed_paths,
    profile_evidence,
)
from .startup_data import LICENSE_PATH, StartupGatewayData, is_startup_path
from .water_softener import (
    SOFTENER_CAPABILITIES,
    SOFTENER_CIRCUIT,
    SOFTENER_ROOT,
    is_water_softener,
)

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
# An HTTP 5xx answer to one individual fallback read concerns that path only.
# This many in a row, without any working answer in between, are treated as an
# outage of the cloud itself and end the discovery like a request-wide error.
MAX_CONSECUTIVE_FALLBACK_5XX = 3
# #113 (RRC-01): the RRC lists come first, before every other pending path.
# The zone list shows RRC zones early, and the program list is the only source
# of the program selection. On a large tree the budget would otherwise end
# before these catalog and optional paths. This changes only the order: both
# paths were requested before as well, within the same budget.
PRIORITY_PATHS = frozenset({ZONE_LIST, PROGRAM_LIST})
# C8 (CT-B02): on an RRC controller the device list joins them. It is the
# only source of the radio devices; nothing else below /devices is read
# except one firmware leaf per listed device, at most this many.
RRC_PRIORITY_PATHS = frozenset({*PRIORITY_PATHS, DEVICE_LIST})
MAX_RRC_DEVICE_PROBES = 64
# W6: the setting leaves the contracts declare per listed device type, at
# most this many, read after every other path.
MAX_RRC_DEVICE_SETTING_PROBES = 192


class DiscoveryPathSource(StrEnum):
    """How a path entered the bounded discovery queue."""

    ROOT = "root"
    REFERENCE = "reference"
    CATALOG = "catalog"
    OPTIONAL = "optional"


@dataclass(slots=True)
class DiscoveryPathDiagnostic:
    """Privacy-safe result for one requested resource path."""

    source: DiscoveryPathSource
    bulk_result: str = "not_attempted"
    fallback_reason: str | None = None
    fallback_result: str | None = None
    discovered: bool = False
    read_policy: str = "normal"

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
    device_profile: str = "generic"
    # C8: what showed an RRC controller (device_type, product_type, zones).
    rrc_evidence: tuple[str, ...] = ()

    def reset(self) -> None:
        """Clear a previous run before rediscovery."""
        self.paths.clear()
        self.completed = False
        self.stop_reason = "running"
        self.depth_limit_reached = False
        self.bulk_calls = 0
        self.device_profile = "generic"
        self.rrc_evidence = ()

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
        catalog = tuple(
            item for item in items if item.source is DiscoveryPathSource.CATALOG
        )
        optionals = tuple(
            item for item in items if item.source is DiscoveryPathSource.OPTIONAL
        )
        forbidden = sum(
            "http_403" in (item.bulk_result, item.fallback_result) for item in items
        )
        return {
            "device_profile": self.device_profile,
            "rrc_evidence": list(self.rrc_evidence),
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
            "catalog_paths": len(catalog),
            "catalog_paths_discovered": sum(item.discovered for item in catalog),
            "optional_paths": len(optionals),
            "optional_paths_discovered": sum(item.discovered for item in optionals),
            "fallback_attempts": len(fallbacks),
            "fallback_successes": sum(
                item.fallback_result == "success" for item in fallbacks
            ),
            "fallback_failures": sum(
                item.fallback_result not in (None, "success") for item in fallbacks
            ),
            # CT-A02 (E03): answers that refuse access, apart from errors.
            "forbidden_paths": forbidden,
            "access_reason": self._access_reason(forbidden),
            "paths_not_read": dict(
                sorted(
                    Counter(
                        item.read_policy
                        for item in items
                        if item.read_policy in _NOT_READ_POLICIES
                    ).items()
                )
            ),
        }

    def _access_reason(self, forbidden: int) -> str | None:
        """CT-A02 (E03): whether refused reads point to the sign-in's rights.

        A controller refuses some advertised paths as a matter of course
        (a TC100.2 about 35). Only a refused request, a discovery that found
        nothing but refusals, or refused zones point to the scope or the
        rights of the account.
        """
        if self.stop_reason == "http_403" or (
            forbidden
            and (
                not any(item.discovered for item in self.paths.values())
                or any(
                    "http_403" in (item.bulk_result, item.fallback_result)
                    for path, item in self.paths.items()
                    if path in ("/zones", ZONE_LIST)
                )
            )
        ):
            return "scope_or_permission_denied"
        return "some_paths_forbidden" if forbidden else None


# C8: read policies of paths that were scheduled but deliberately not read.
_NOT_READ_POLICIES = frozenset(
    {
        "never_read_private",
        "outside_rrc_device_scope",
        "rrc_5xx_assumed_missing",
        "rrc_device_setting_limit",
    }
)


# Some gateways omit stable public resources from their reference trees or
# advertise an unreadable container around them. Keep these fallbacks narrow:
# every listed path is a known public resource and remains optional when a
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

# Request these known resources for each advertised circuit independently of
# whether the circuit directory itself can be read.
_HEATING_CIRCUIT_CATALOG_SUFFIXES = (
    "/operationMode",
    "/manualRoomSetpoint",
    "/controlType",
    "/temperatureLevels",
    "/temperatureLevels/comfort2",
    "/temperatureLevels/eco",
)

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
    "/dhwType",
    "/switchPrograms/A",
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
    "/cooling/manualRoomSetpoint",
    "/cooling/operationMode",
    "/cooling/temporaryRoomSetpoint",
    "/heating/manualRoomSetpoint",
    "/heating/operationMode",
    "/heating/temporaryRoomSetpoint",
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
    startup_data: StartupGatewayData | None = None,
    gateway: Gateway | None = None,
    rediscovery: bool = False,
    rrc_evidence: tuple[str, ...] = (),
) -> dict[str, Resource]:
    """Follow PointT references without escaping configured safety bounds.

    ``rediscovery`` searches exactly the given subtrees again (P46): the device
    profile keeps its probes but does not widen the roots, and start-up values
    stay queued in the existing start-up state instead of resetting it.
    ``rrc_evidence`` is what earlier readings showed of an RRC controller;
    its profile then applies from the first request on.
    """
    if maximum_depth < 0 or maximum_resources < 1:
        raise ValueError("Discovery bounds must be positive")

    report = diagnostics or DiscoveryDiagnostics()
    report.reset()
    discovered: dict[str, Resource] = {}
    softener_profile = is_water_softener(gateway, discovered)
    # A known family changes probes, not the permission to follow advertised
    # references. Unknown gateways may advertise a softener circuit as well.
    allowed_roots = roots if rediscovery else (*roots, SOFTENER_ROOT)
    rrc_enabled = False
    # C8: an RRC 2.0 room controller (CT200, TC100.2 and their relatives).
    rrc_profile = False
    priority_paths = PRIORITY_PATHS
    if softener_profile:
        if not rediscovery:
            roots = ("/notifications", "/gateway", "/system", SOFTENER_ROOT)
        report.device_profile = "water_softener"
    if startup_data is not None and not rediscovery:
        startup_data.begin_discovery(
            maximum_resources=maximum_resources, maximum_depth=maximum_depth
        )
    priority_pending: dict[str, int] = {}
    referenced_pending: dict[str, int] = {}
    catalog_pending: dict[str, int] = {}
    optional_pending: dict[str, int] = {}
    # W6: device setting leaves come last, after every other path.
    device_pending: dict[str, int] = {}
    queues = (
        priority_pending,
        referenced_pending,
        catalog_pending,
        optional_pending,
        device_pending,
    )
    device_settings: set[str] = set()

    def outside_device_scope(path: str) -> bool:
        return _outside_rrc_device_scope(path) and path not in device_settings

    consecutive_5xx = 0
    processed: set[str] = set()
    depth_limited: set[str] = set()

    def schedule(path: str, depth: int, source: DiscoveryPathSource) -> None:
        report.scheduled(path, source)
        if is_never_read(path):
            # CT-E07, CT-F05: device keys and personal data, on any gateway.
            report.paths[path].read_policy = "never_read_private"
            return
        if rrc_profile and outside_device_scope(path):
            report.paths[path].read_policy = "outside_rrc_device_scope"
            return
        if path == LICENSE_PATH:
            report.paths[path].read_policy = "omitted_license"
            if startup_data is not None:
                startup_data.omit_license()
            return
        if startup_data is not None and is_startup_path(path):
            report.paths[path].read_policy = "startup_once"
            startup_data.schedule(
                path, depth, advertised=source is DiscoveryPathSource.REFERENCE
            )
            return
        if path in processed:
            return
        depth = min(depth, *(queue.get(path, depth) for queue in queues))
        if depth > maximum_depth:
            depth_limited.add(path)
            report.depth_limit_reached = True
            return
        depth_limited.discard(path)
        report.depth_limit_reached = bool(depth_limited)
        target = (
            priority_pending
            if path in priority_paths
            else device_pending
            if path in device_settings
            else queues[1 + _source_priority(report.paths[path].source)]
        )
        for queue in queues:
            if queue is not target:
                queue.pop(path, None)
        target[path] = depth

    def enable_rrc_lists(depth: int) -> None:
        nonlocal rrc_enabled, allowed_roots
        if rrc_enabled:
            return
        rrc_enabled = True
        allowed_roots = (*allowed_roots, "/programs")
        schedule("/programs", depth, DiscoveryPathSource.CATALOG)
        schedule(PROGRAM_LIST, depth, DiscoveryPathSource.CATALOG)

    def enable_rrc_profile(evidence: str, depth: int) -> None:
        """C8 (CT-A03, CT-B02): a room controller, recognised by resources."""
        nonlocal rrc_profile, priority_paths
        if evidence not in report.rrc_evidence:
            report.rrc_evidence = (*report.rrc_evidence, evidence)
        if rrc_profile:
            return
        rrc_profile = True
        priority_paths = RRC_PRIORITY_PATHS
        if not softener_profile:
            report.device_profile = "rrc2"
        # Probes queued before the controller was recognised (the device
        # tree comes before the zones) are dropped; none has been sent.
        for queue in queues:
            for pending_path in tuple(queue):
                if _outside_rrc_device_scope(pending_path):
                    del queue[pending_path]
                    report.paths[pending_path].read_policy = "outside_rrc_device_scope"
                    depth_limited.discard(pending_path)
        report.depth_limit_reached = bool(depth_limited)
        if startup_data is not None:
            for withdrawn in startup_data.withdraw(_outside_rrc_device_scope):
                if (item := report.paths.get(withdrawn)) is not None:
                    item.read_policy = "outside_rrc_device_scope"
        if not rediscovery or any(
            root == "/devices" or root.startswith("/devices/") for root in roots
        ):
            schedule(DEVICE_LIST, depth, DiscoveryPathSource.CATALOG)
        enable_contract_paths(depth)

    def enable_contract_paths(depth: int) -> None:
        """W2/W5: the roots and optional leaves the contracts add on an RRC
        controller; none without such contracts."""
        nonlocal allowed_roots
        for extra in rrc_contracts.extra_roots():
            if rediscovery and not any(
                root == extra or root.startswith(f"{extra}/") for root in roots
            ):
                continue
            if extra not in allowed_roots:
                allowed_roots = (*allowed_roots, extra)
            schedule(extra, 0, DiscoveryPathSource.CATALOG)
        # Containers read before the controller was recognised.
        for container in tuple(discovered):
            for child in rrc_contracts.optional_children(container):
                schedule(child, depth + 1, DiscoveryPathSource.OPTIONAL)

    def schedule_children(path: str, depth: int) -> None:
        if is_never_read(path) or (rrc_profile and _outside_rrc_device_scope(path)):
            return  # nothing below a path that is not read is probed either
        if ZONE.match(path):
            enable_rrc_profile("zones", depth + 1)
            enable_rrc_lists(depth + 1)
        if rrc_enabled:
            catalog_root = "/".join(path.split("/")[:3]) if ZONE.match(path) else path
            for child in catalog_paths(catalog_root):
                schedule(child, depth + 1, DiscoveryPathSource.CATALOG)
        if SOFTENER_CIRCUIT.fullmatch(path):
            for capability in SOFTENER_CAPABILITIES:
                schedule(
                    f"{path}/{capability.suffix}",
                    depth + 1,
                    DiscoveryPathSource.CATALOG,
                )
        if _HEATING_CIRCUIT_PATH.fullmatch(path):
            for suffix in _HEATING_CIRCUIT_CATALOG_SUFFIXES:
                schedule(f"{path}{suffix}", depth + 1, DiscoveryPathSource.CATALOG)
        if rrc_profile:
            for child in rrc_contracts.optional_children(path):
                schedule(child, depth + 1, DiscoveryPathSource.OPTIONAL)
        for fallback in _optional_children(path):
            if (
                softener_profile
                and path == "/system"
                and fallback
                not in {"/system/appliance/model", "/system/appliance/versionFirmware"}
            ):
                continue
            schedule(fallback, depth + 1, DiscoveryPathSource.OPTIONAL)

    for evidence in dict.fromkeys(
        (
            *rrc_evidence,
            *profile_evidence(gateway.device_type if gateway else None, {}),
        )
    ):
        enable_rrc_profile(evidence, 1)
    for root in roots:
        schedule(root, 0, DiscoveryPathSource.ROOT)

    if softener_profile and not rediscovery:
        schedule_children(f"{SOFTENER_ROOT}/sw1", 1)
    device_reads = 0
    while any(queues):
        # W6: the setting leaves of listed devices come after every other path
        # and have their own cap (MAX_RRC_DEVICE_SETTING_PROBES, applied when
        # they are scheduled) instead of a share of the overall budget, so a
        # large controller still reads the leaves of its last devices.
        device_frontier = not any(queues[:-1])
        if device_frontier:
            capacity = MAX_BULK_PATHS
        else:
            capacity = min(
                MAX_BULK_PATHS, maximum_resources - (len(processed) - device_reads)
            )
        if capacity <= 0:
            report.stop_reason = "resource_limit"
            break
        # Leave spare capacity unused so returned references are expanded before
        # catalog paths, and catalog paths before other optional probes. The
        # RRC lists join the next batch instead of needing a request of their own.
        frontier_entries = dict(islice(priority_pending.items(), capacity))
        pending = next((queue for queue in queues[1:] if queue), priority_pending)
        if pending is not priority_pending and pending is not device_pending:
            frontier_entries.update(
                islice(pending.items(), capacity - len(frontier_entries))
            )
        elif device_frontier:
            frontier_entries = dict(islice(device_pending.items(), capacity))
        if device_frontier:
            # Not charged to the shared distinct-path budget either.
            device_reads += len(frontier_entries)
        elif startup_data is not None:
            frontier_entries = {
                path: frontier_entries[path]
                for path in startup_data.reserve_discovery(tuple(frontier_entries))
            }
            if not frontier_entries:
                report.stop_reason = "resource_limit"
                break
        frontier = tuple(frontier_entries)
        for path in frontier:
            for queue in queues:
                queue.pop(path, None)
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
        if any(result.resource is not None for result in results):
            # A bulk answer with resources shows that the cloud is answering.
            consecutive_5xx = 0
        results, consecutive_5xx = await _recover_invalid_bulk_results(
            client,
            gateway_id,
            results,
            diagnostics=report,
            consecutive_5xx=consecutive_5xx,
            rrc_profile=rrc_profile,
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
            if resource.path == ZONE_LIST and listed_ids(resource) is not None:
                enable_rrc_profile("zones", depth + 1)
                enable_rrc_lists(depth + 1)
            if resource.path == PRODUCT_TYPE_PATH and is_product_type(resource.value):
                enable_rrc_profile("product_type", depth)
            if rrc_profile and resource.path == DEVICE_LIST:
                # CT-B05: device IDs only from the list, never guessed.
                for child in device_firmware_paths(resource, MAX_RRC_DEVICE_PROBES):
                    schedule(child, depth + 1, DiscoveryPathSource.CATALOG)
                # W6: the contract leaves of each listed device type, bounded.
                for position, child in enumerate(device_setting_paths(resource)):
                    if position >= MAX_RRC_DEVICE_SETTING_PROBES:
                        report.scheduled(child, DiscoveryPathSource.OPTIONAL)
                        report.paths[child].read_policy = "rrc_device_setting_limit"
                        continue
                    device_settings.add(child)
                    schedule(child, depth + 1, DiscoveryPathSource.OPTIONAL)
            if resource.path == ZONE_LIST or rrc_enabled:
                for child in listed_paths(resource):
                    schedule(child, depth + 1, DiscoveryPathSource.REFERENCE)
                    schedule_children(child, depth + 1)
            if (
                not softener_profile
                and not rediscovery
                and is_water_softener(gateway, discovered)
            ):
                softener_profile = True
                report.device_profile = "water_softener"
                schedule(SOFTENER_ROOT, 0, DiscoveryPathSource.CATALOG)
                schedule_children(f"{SOFTENER_ROOT}/sw1", 1)
            schedule_children(result.path, depth)
            for reference in resource.references:
                child = reference.path
                if not _is_allowed_reference(child, allowed_roots):
                    continue
                schedule(child, depth + 1, DiscoveryPathSource.REFERENCE)
                schedule_children(child, depth + 1)
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
    consecutive_5xx: int = 0,
    rrc_profile: bool = False,
) -> tuple[tuple[BatchItemResult, ...], int]:
    """Retry every recoverable discovery item once with an individual read.

    Like the regular poll, an HTTP status answer to one read stays local to that
    path. Only no answer at all, authentication, a rate limit, or
    ``MAX_CONSECUTIVE_FALLBACK_5XX`` 5xx answers in a row end the discovery.
    Returns the results and the updated count of consecutive 5xx answers.

    CT-B06: an RRC controller answers HTTP 500 for a path it does not have.
    On that profile a 5xx answer to a catalog or optional probe neither
    counts towards the outage series nor ends it; the path is taken as
    missing. Roots and advertised references keep the rule. An item 5xx in
    the bulk answer to an optional probe or a contract zone detail leaf is
    taken as missing at once, without an individual read: such leaves are
    often absent, and the next discovery probes them again.
    """
    recovered: list[BatchItemResult] = []
    for result in results:
        fallback_reason = result.fallback_reason
        if fallback_reason is None:
            recovered.append(result)
            continue
        if (
            rrc_profile
            and fallback_reason == "item_5xx"
            and _rrc_missing_without_retry(
                result.path, diagnostics.paths[result.path].source
            )
        ):
            diagnostics.paths[result.path].read_policy = "rrc_5xx_assumed_missing"
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
            status = _path_local_status(err)
            if status is None:
                category = _error_category(err)
                diagnostics.fallback_finished(result.path, category, discovered=False)
                diagnostics.stop_reason = category
                raise
            diagnostics.fallback_finished(
                result.path, f"http_{status}", discovered=False
            )
            if (
                status >= 500
                and rrc_profile
                and diagnostics.paths[result.path].source
                in (DiscoveryPathSource.CATALOG, DiscoveryPathSource.OPTIONAL)
            ):
                diagnostics.paths[result.path].read_policy = "rrc_5xx_assumed_missing"
                recovered.append(
                    BatchItemResult(gateway_id, result.path, status, error=err)
                )
                continue
            if status >= 500:
                consecutive_5xx += 1
                if consecutive_5xx >= MAX_CONSECUTIVE_FALLBACK_5XX:
                    _LOGGER.debug(
                        "Ending PointT discovery after %d individual reads in a "
                        "row answered HTTP 5xx",
                        consecutive_5xx,
                    )
                    diagnostics.stop_reason = _error_category(err)
                    raise
            else:
                consecutive_5xx = 0
            recovered.append(
                BatchItemResult(gateway_id, result.path, status, error=err)
            )
            continue
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
        # Any other answer ends a series of 5xx answers.
        consecutive_5xx = 0
    return tuple(recovered), consecutive_5xx


def _rrc_missing_without_retry(path: str, source: DiscoveryPathSource) -> bool:
    """CT-B06: an optional probe or a contract detail leaf of a zone (W1),
    which an RRC controller without it answers with HTTP 500. The known zone
    fields and the other catalog paths keep their individual read."""
    if source is DiscoveryPathSource.OPTIONAL:
        return True
    if source is not DiscoveryPathSource.CATALOG:
        return False
    match = re.fullmatch(rf"/zones/zn{RRC_ID}/(.+)", path)
    return match is not None and match.group(1) in rrc_contracts.zone_fields()


def _path_local_status(error: PointTError) -> int | None:
    """The HTTP status of an answer that concerns only the requested path.

    ``None`` means the request as a whole failed: no answer (timeout, network,
    5xx without status), authentication, or a rate limit.
    """
    if isinstance(error, UnexpectedHttpStatus):
        return error.status
    if isinstance(error, ServiceUnavailable):
        return error.status
    return None


def _outside_rrc_device_scope(path: str) -> bool:
    """C8: a ``/devices`` path an RRC controller does not need to read."""
    return path.startswith("/devices/") and not in_device_scope(path)


def _optional_children(path: str) -> tuple[str, ...]:
    """Return narrowly curated app paths below one discovered container."""
    fixed = OPAQUE_CONTAINER_CHILDREN.get(path, ())
    if path == "/system":
        fixed = (*fixed, ICOM_HOLIDAY_ROOT, WDDW2_HOLIDAY_PATH)
    if is_icom_slot(path):
        return tuple(f"{path}/{field}" for field in (*ICOM_FIELDS, "delete"))
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
    """Read advertised paths, then core catalog paths, then optional probes."""
    if source is DiscoveryPathSource.OPTIONAL:
        return 2
    return 1 if source is DiscoveryPathSource.CATALOG else 0


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


def error_category(error: PointTError) -> str:
    """Describe a request error by category, without message or payload."""
    return _error_category(error)
