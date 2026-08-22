"""Privacy, polling, naming, and logical-device rules for PointT resources."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from enum import StrEnum

from .pointt import Resource


class PollGroup(StrEnum):
    """Cloud polling cadence for one discovered resource."""

    FAST = "fast"
    NOTIFICATIONS = "notifications"
    CONTROL = "control"
    ENERGY = "energy"
    SLOW = "slow"
    STATIC = "static"


class CapabilityMaturity(StrEnum):
    """Evidence level for exposing a discovered PointT capability."""

    OBSERVED = "observed"
    UNDERSTOOD = "understood"
    VERIFIED = "verified"
    WRITE_VERIFIED = "write_verified"


@dataclass(frozen=True, slots=True)
class LogicalDevice:
    """Stable logical-device identity derived from a PointT path."""

    kind: str
    logical_id: str
    name: str


_PRIVATE_PATHS = frozenset(
    {
        "/gateway/serialId",
        "/gateway/thirdPartyLicenseInformation",
        "/gateway/tosAccepted",
        "/gateway/uuid",
        "/system/country",
        "/system/dateTime",
        "/system/info",
    }
)

_PRIVATE_PREFIXES = ("/gateway/wifi",)

_OPT_IN_DIAGNOSTIC_PATHS = frozenset(
    {
        "/gateway/serialId",
        "/gateway/uuid",
        "/system/country",
        "/system/info",
    }
)

_NO_ENTITY_PATHS = frozenset(
    {
        "/notifications",
        "/devices",
        "/devices/list",
        "/gateway",
        "/gateway/tzInfo",
        "/gateway/update",
        "/system",
        "/system/awayMode",
        "/system/sensors",
        "/system/sensors/temperatures",
        "/system/variableTariff",
        "/heatingCircuits",
        "/dhwCircuits",
        "/heatSources",
        "/heatSources/info",
        "/heatSources/numberOfStarts",
        "/holidayMode/list",
        "/holidayMode/configuration",
        "/holidayMode/activeModes",
    }
)

_VERIFIED_PATHS = frozenset(
    {
        "/heatSources/Source/eHeater/status",
        "/heatSources/actualHeatDemand",
        "/heatSources/actualModulation",
        "/heatSources/actualSupplyTemperature",
        "/heatSources/compressor/status",
        "/heatSources/returnTemperature",
        "/heatSources/systemPressure",
        "/system/sensors/temperatures/outdoor_t1",
    }
)

_WRITE_VERIFIED_PATTERNS = (re.compile(r"^/heatingCircuits/[^/]+/operationMode$"),)

_VERIFIED_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^/heatingCircuits/[^/]+/(?:currentRoomSetpoint|currentSuWiMode|heatCoolMode|operationMode|overallStatus)$",
        r"^/dhwCircuits/[^/]+/(?:actualTemp|chargeRemainingTime|currentSetpoint|currentTemperatureLevel|operationMode|overallStatus|tdMode)$",
        r"^/heatSources/emon/(?:totalConsumption|chConsumption|dhwConsumption)$",
    )
)

_UNDERSTOOD_PATHS = frozenset(
    {
        "/notifications",
        "/devices",
        "/devices/list",
        "/dhwCircuits",
        "/gateway",
        "/gateway/brand",
        "/gateway/dateTime",
        "/gateway/dataProcessing/status",
        "/gateway/serialId",
        "/gateway/swPrefix",
        "/gateway/tosAccepted",
        "/gateway/tzInfo",
        "/gateway/tzInfo/timeZone",
        "/gateway/update",
        "/gateway/update/status",
        "/gateway/uuid",
        "/gateway/versionFirmware",
        "/gateway/versionHardware",
        "/heatSources",
        "/heatSources/chStatus",
        "/heatSources/emStatus",
        "/heatSources/flameStatus",
        "/heatSources/info",
        "/heatSources/numberOfStarts",
        "/heatSources/systemPressureRange",
        "/holidayMode/list",
        "/holidayMode/configuration",
        "/holidayMode/activeModes",
        "/heatingCircuits",
        "/system",
        "/system/awayMode",
        "/system/awayMode/enabled",
        "/system/brand",
        "/system/bus",
        "/system/country",
        "/system/dateTime",
        "/system/globalSeasonOptimizer/currentMode",
        "/system/iSRC/supportStatus",
        "/system/info",
        "/system/sensors",
        "/system/sensors/temperatures",
        "/system/sensors/temperatures/outdoorTemperatureSource",
        "/system/type",
        "/system/variableTariff",
        "/system/variableTariff/supportStatus",
    }
)

_UNDERSTOOD_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^/heatingCircuits/hc\d+$",
        r"^/dhwCircuits/dhw\d+$",
        r"^/heatSources/hs\d+$",
        r"^/heatingCircuits/[^/]+/(?:activeSwitchProgram|controlType|heatingType|manualRoomSetpoint|maxFlowTemp|name|suWiSwitchMode|switchProgramMode)$",
        r"^/heatingCircuits/[^/]+/(?:switchPrograms|temperatureLevels)$",
        r"^/heatingCircuits/[^/]+/switchPrograms/(?:[^/]+|name[^/]+)$",
        r"^/heatingCircuits/[^/]+/temperatureLevels/[^/]+$",
        r"^/dhwCircuits/[^/]+/(?:charge|chargeDuration|name|reduceTempOnAlarm|singleChargeSetpoint)$",
        r"^/dhwCircuits/[^/]+/temperatureLevels$",
        r"^/dhwCircuits/[^/]+/temperatureLevels/[^/]+$",
        r"^/heatSources/[^/]+/(?:heatPumpType|numberOfStarts|supplyFlowCondenserTemp|type|workingTime)$",
        r"^/heatSources/emon/coolingConsumption$",
    )
)

_DEFAULT_ENABLED_PATHS = frozenset(
    {
        "/heatSources/Source/eHeater/status",
        "/heatSources/actualHeatDemand",
        "/heatSources/actualModulation",
        "/heatSources/actualSupplyTemperature",
        "/heatSources/chStatus",
        "/heatSources/compressor/status",
        "/heatSources/emStatus",
        "/heatSources/flameStatus",
        "/heatSources/returnTemperature",
        "/heatSources/systemPressure",
        "/system/sensors/temperatures/outdoor_t1",
    }
)

_DEFAULT_ENABLED_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^/heatingCircuits/[^/]+/(?:currentRoomSetpoint|currentSuWiMode|heatCoolMode|overallStatus)$",
        r"^/dhwCircuits/[^/]+/(?:actualTemp|chargeRemainingTime|currentSetpoint|currentTemperatureLevel|overallStatus|tdMode)$",
        r"^/heatSources/[^/]+/(?:numberOfStarts|supplyFlowCondenserTemp|workingTime)$",
        r"^/heatSources/emon/(?:totalConsumption|chConsumption|dhwConsumption|coolingConsumption)$",
    )
)

# A writable Home Assistant control already represents each of these resources.
# Keep the additional read-only mirror available for advanced users without
# showing the same setting twice on a fresh installation.
_READ_ONLY_CONTROL_MIRROR_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^/system/awayMode/enabled$",
        r"^/heatingCircuits/[^/]+/(?:manualRoomSetpoint|operationMode)$",
        r"^/heatingCircuits/[^/]+/temperatureLevels/(?:comfort2|eco)$",
        r"^/dhwCircuits/[^/]+/(?:charge|chargeDuration|operationMode|reduceTempOnAlarm|singleChargeSetpoint)$",
        r"^/dhwCircuits/[^/]+/temperatureLevels/(?:eco|high|low)$",
    )
)

_FAST_TOKENS = (
    "actual",
    "current",
    "status",
    "remaining",
    "demand",
    "modulation",
    "pressure",
    "temperature",
    "setpoint",
    "mode",
    "charge",
)

_STATIC_TOKENS = (
    "brand",
    "country",
    "heatingtype",
    "heatpumptype",
    "name",
    "swprefix",
    "type",
    "versionfirmware",
    "versionhardware",
)

_GERMAN_NAMES = {
    "activeSwitchProgram": "Aktives Zeitprogramm",
    "actualHeatDemand": "Aktuelle Wärmeanforderung",
    "actualModulation": "Aktuelle Modulation",
    "actualSupplyTemperature": "Vorlauftemperatur",
    "actualTemp": "Warmwasser-Isttemperatur",
    "brand": "Marke",
    "bus": "Systembus",
    "charge": "Extra-Warmwasser",
    "chargeDuration": "Dauer Extra-Warmwasser",
    "chargeRemainingTime": "Restzeit Extra-Warmwasser",
    "chStatus": "Heizbetrieb",
    "controlType": "Regelungsart",
    "country": "Land",
    "currentRoomSetpoint": "Wunschtemperatur",
    "currentSetpoint": "Aktueller Sollwert",
    "currentSuWiMode": "Sommer-/Winterbetrieb",
    "currentTemperatureLevel": "Aktuelles Temperaturniveau",
    "dateTime": "Datum und Uhrzeit",
    "emStatus": "Energiemanagement-Status",
    "enabled": "Abwesenheitsmodus",
    "flameStatus": "Brennerstatus",
    "heatCoolMode": "Heiz-/Kühlbetrieb",
    "heatingType": "Heizsystem",
    "heatPumpType": "Wärmepumpentyp",
    "manualRoomSetpoint": "Manueller Sollwert",
    "maxFlowTemp": "Maximale Vorlauftemperatur",
    "name": "Name",
    "numberOfStarts": "Anzahl der Starts",
    "operationMode": "Betriebsart",
    "overallStatus": "Betriebsstatus",
    "reduceTempOnAlarm": "Temperaturabsenkung bei Störung",
    "returnTemperature": "Rücklauftemperatur",
    "serialId": "Seriennummer",
    "singleChargeSetpoint": "Solltemperatur Extra-Warmwasser",
    "supplyFlowCondenserTemp": "Austrittstemperatur Kondensator (TC3)",
    "switchProgramMode": "Art des Zeitprogramms",
    "systemPressure": "Systemdruck",
    "systemPressureRange": "Zulässiger Druckbereich",
    "tdMode": "Thermische Desinfektion",
    "timeZone": "Zeitzone",
    "type": "Anlagentyp",
    "uuid": "Gateway-UUID",
    "versionFirmware": "Firmwareversion",
    "versionHardware": "Hardwareversion",
    "workingTime": "Betriebszeit",
    "outdoor_t1": "Außentemperatur",
    "outdoorTemperatureSource": "Quelle der Außentemperatur",
    "supportStatus": "Unterstützungsstatus",
    "info": "Systeminformationen",
}

_GERMAN_PATH_NAMES = {
    "/gateway/dataProcessing/status": "Datenverarbeitungsstatus",
    "/heatSources/Source/eHeater/status": "Status elektrischer Zuheizer",
    "/heatSources/compressor/status": "Status Kompressor",
    "/holidayMode/activeModes": "Aktive Urlaubsmodi",
    "/holidayMode/configuration": "Urlaubskonfiguration",
    "/holidayMode/list": "Urlaubszeiten",
    "/system/globalSeasonOptimizer/currentMode": "Saisonoptimierung",
    "/system/iSRC/supportStatus": "iSRC-Unterstützung",
}

_ENGLISH_NAMES = {
    "activeSwitchProgram": "Active schedule",
    "actualHeatDemand": "Current heat demand",
    "actualModulation": "Current modulation",
    "actualSupplyTemperature": "Supply temperature",
    "actualTemp": "Hot water temperature",
    "brand": "Brand",
    "bus": "System bus",
    "charge": "Extra hot water",
    "chargeDuration": "Extra hot water duration",
    "chargeRemainingTime": "Extra hot water remaining time",
    "chStatus": "Central heating status",
    "controlType": "Control type",
    "country": "Country",
    "currentRoomSetpoint": "Target temperature",
    "currentSetpoint": "Current setpoint",
    "currentSuWiMode": "Summer/winter mode",
    "currentTemperatureLevel": "Current temperature level",
    "dateTime": "Date and time",
    "emStatus": "Energy management status",
    "enabled": "Away mode",
    "flameStatus": "Burner status",
    "heatCoolMode": "Heating/cooling mode",
    "heatingType": "Heating system",
    "heatPumpType": "Heat pump type",
    "manualRoomSetpoint": "Manual setpoint",
    "maxFlowTemp": "Maximum supply temperature",
    "name": "Name",
    "numberOfStarts": "Number of starts",
    "operationMode": "Operation mode",
    "overallStatus": "Operating status",
    "reduceTempOnAlarm": "Temperature reduction during fault",
    "returnTemperature": "Return temperature",
    "serialId": "Serial number",
    "singleChargeSetpoint": "Extra hot water setpoint",
    "supplyFlowCondenserTemp": "Condenser outlet temperature (TC3)",
    "switchProgramMode": "Schedule type",
    "systemPressure": "System pressure",
    "systemPressureRange": "Permitted pressure range",
    "tdMode": "Thermal disinfection",
    "timeZone": "Time zone",
    "type": "System type",
    "uuid": "Gateway UUID",
    "versionFirmware": "Firmware version",
    "versionHardware": "Hardware version",
    "workingTime": "Operating time",
    "outdoor_t1": "Outdoor temperature",
    "outdoorTemperatureSource": "Outdoor temperature source",
    "supportStatus": "Support status",
    "info": "System information",
}

_ENGLISH_PATH_NAMES = {
    "/gateway/dataProcessing/status": "Data processing status",
    "/heatSources/Source/eHeater/status": "Auxiliary heater status",
    "/heatSources/compressor/status": "Compressor status",
    "/holidayMode/activeModes": "Active holiday modes",
    "/holidayMode/configuration": "Holiday configuration",
    "/holidayMode/list": "Holiday periods",
    "/system/globalSeasonOptimizer/currentMode": "Season optimization",
    "/system/iSRC/supportStatus": "iSRC support",
}

_SUBKEY_NAMES = {
    "absoluteHighPressure": "Absoluter Maximaldruck",
    "ch": "Heizung",
    "compressor": "Stromverbrauch Wärmepumpe",
    "cooling": "Kühlung",
    "cur_percent": "Aktueller Update-Fortschritt",
    "cur_step": "Aktueller Update-Schritt",
    "dhw": "Warmwasser",
    "eheater": "Stromverbrauch elektrischer Zuheizer",
    "electricity": "Stromverbrauch",
    "environmental_energy": "Umweltenergie (berechnet)",
    "highPressureThreshold": "Obere Druckgrenze",
    "highSystemPressure": "Hoher Systemdruck",
    "lowPressureThreshold": "Untere Druckgrenze",
    "lowSystemPressure": "Niedriger Systemdruck",
    "nsteps": "Update-Schritte gesamt",
    "outputProduced": "Erzeugte Wärme",
    "percent": "Update-Fortschritt",
    "progress.cur_percent": "Aktueller Fortschritt",
    "progress.cur_step": "Aktueller Schritt",
    "progress.nsteps": "Schritte gesamt",
    "progress.percent": "Fortschritt",
    "shutOfPressureThreshold": "Abschaltdruck",
    "status.value": "Status",
    "total": "Gesamt",
    "value": "Status",
}

_ENGLISH_SUBKEY_NAMES = {
    "absoluteHighPressure": "Absolute maximum pressure",
    "ch": "Heating",
    "compressor": "Heat pump electricity consumption",
    "cooling": "Cooling",
    "cur_percent": "Current update progress",
    "cur_step": "Current update step",
    "dhw": "Hot water",
    "eheater": "Electric auxiliary heater consumption",
    "electricity": "Electricity consumption",
    "environmental_energy": "Environmental energy (calculated)",
    "highPressureThreshold": "Upper pressure limit",
    "highSystemPressure": "High system pressure",
    "lowPressureThreshold": "Lower pressure limit",
    "lowSystemPressure": "Low system pressure",
    "nsteps": "Total update steps",
    "outputProduced": "Heat produced",
    "percent": "Update progress",
    "progress.cur_percent": "Current progress",
    "progress.cur_step": "Current step",
    "progress.nsteps": "Total steps",
    "progress.percent": "Progress",
    "shutOfPressureThreshold": "Shutdown pressure",
    "status.value": "Status",
    "total": "Total",
    "value": "Status",
}


def is_private_resource(path: str) -> bool:
    """Return whether a resource must never become an entity or diagnostic."""
    return path in _PRIVATE_PATHS or path.startswith(_PRIVATE_PREFIXES)


def is_opt_in_diagnostic_resource(path: str) -> bool:
    """Return whether a private value may be exposed only when explicitly enabled."""
    return path in _OPT_IN_DIAGNOSTIC_PATHS


def is_read_only_control_mirror(path: str) -> bool:
    """Return whether a read-only entity duplicates an available control."""
    return any(
        pattern.fullmatch(path) for pattern in _READ_ONLY_CONTROL_MIRROR_PATTERNS
    )


def supports_entity(resource: Resource) -> bool:
    """Return whether the resource can yield at least one safe HA state."""
    return (
        capability_maturity(resource.path) is not CapabilityMaturity.OBSERVED
        and not is_private_resource(resource.path)
        and resource.path not in _NO_ENTITY_PATHS
        and not resource.references
        and resource.metadata.resource_type != "licenseInformation"
    )


def capability_maturity(path: str) -> CapabilityMaturity:
    """Return the curated evidence level for a concrete PointT path."""
    if any(pattern.fullmatch(path) for pattern in _WRITE_VERIFIED_PATTERNS):
        return CapabilityMaturity.WRITE_VERIFIED
    if path in _VERIFIED_PATHS or any(
        pattern.fullmatch(path) for pattern in _VERIFIED_PATTERNS
    ):
        return CapabilityMaturity.VERIFIED
    if path in _UNDERSTOOD_PATHS or any(
        pattern.fullmatch(path) for pattern in _UNDERSTOOD_PATTERNS
    ):
        return CapabilityMaturity.UNDERSTOOD
    return CapabilityMaturity.OBSERVED


def entity_enabled_by_default(path: str) -> bool:
    """Apply the explicit user-facing default entity policy."""
    if is_opt_in_diagnostic_resource(path) or is_read_only_control_mirror(path):
        return False
    return path in _DEFAULT_ENABLED_PATHS or any(
        pattern.fullmatch(path) for pattern in _DEFAULT_ENABLED_PATTERNS
    )


def poll_group(resource: Resource) -> PollGroup:
    """Classify a discovered resource into a cloud-friendly cadence."""
    # Historical failure lists are probed during discovery, but are not polled
    # repeatedly until the integration exposes a history feature that uses them.
    if resource.path.endswith("/failurelist"):
        return PollGroup.STATIC
    if resource.path.startswith("/holidayMode/"):
        return PollGroup.CONTROL
    if resource.path == "/notifications" or resource.path.endswith(
        ("/activefailure", "/errors")
    ):
        return PollGroup.NOTIFICATIONS
    if resource.references or is_private_resource(resource.path):
        return PollGroup.STATIC
    if resource.path == "/heatSources/systemPressureRange":
        return PollGroup.STATIC
    if resource.path in {
        "/gateway/dataProcessing/status",
        "/system/iSRC/supportStatus",
    }:
        return PollGroup.STATIC
    if "/emon/" in resource.path:
        return PollGroup.ENERGY
    if resource.path.endswith(("/workingTime", "/numberOfStarts")):
        return PollGroup.SLOW
    tail = resource.path.rsplit("/", 1)[-1].lower()
    if tail in _STATIC_TOKENS:
        return PollGroup.STATIC
    if resource.metadata.writable:
        return PollGroup.CONTROL
    if any(token in tail for token in _FAST_TOKENS):
        return PollGroup.FAST
    return PollGroup.SLOW


def logical_device_for_path(path: str) -> LogicalDevice | None:
    """Map dynamic PointT object IDs to stable Home Assistant devices."""
    for root, kind, label in (
        ("heatingCircuits", "heating_circuit", "Heizkreis"),
        ("dhwCircuits", "hot_water_circuit", "Warmwasser"),
        ("heatSources", "heat_source", "Wärmeerzeuger"),
    ):
        prefix = {
            "heatingCircuits": "hc",
            "dhwCircuits": "dhw",
            "heatSources": "hs",
        }[root]
        match = re.match(rf"^/{root}/({prefix}\d+)(?:/|$)", path, re.IGNORECASE)
        if match:
            logical_id = match.group(1)
            number = re.search(r"(\d+)$", logical_id)
            suffix = number.group(1) if number else logical_id
            return LogicalDevice(kind, logical_id, f"{label} {suffix}")
    return None


def resource_name(path: str, subkey: str | None = None, *, language: str = "de") -> str:
    """Return a localized user-facing name without exposing technical IDs."""
    german = language.casefold().startswith("de")
    names = _GERMAN_NAMES if german else _ENGLISH_NAMES
    path_names = _GERMAN_PATH_NAMES if german else _ENGLISH_PATH_NAMES
    subkey_names = _SUBKEY_NAMES if german else _ENGLISH_SUBKEY_NAMES
    tail = path.rsplit("/", 1)[-1]
    if "/heatingCircuits/" in path and "/temperatureLevels/" in path:
        temperature_names = (
            {"comfort2": "Heizen", "eco": "Absenken"}
            if german
            else {"comfort2": "Heating", "eco": "Reduced"}
        )
        base = temperature_names.get(tail, _humanize(tail))
    elif "/dhwCircuits/" in path and "/temperatureLevels/" in path:
        temperature_names = (
            {
                "eco": "Eco+ Starttemperatur",
                "high": "Komfort Starttemperatur",
                "low": "Eco Starttemperatur",
                "off": "Aus",
            }
            if german
            else {
                "eco": "Eco+ start temperature",
                "high": "Comfort start temperature",
                "low": "Eco start temperature",
                "off": "Off",
            }
        )
        base = temperature_names.get(tail, _humanize(tail))
    elif re.fullmatch(r"name[A-Z0-9]+", tail):
        suffix = tail.removeprefix("name")
        base = (
            f"Name des Zeitprogramms {suffix}" if german else f"Schedule {suffix} name"
        )
    elif path == "/gateway/update/status":
        base = "Softwareupdate" if german else "Software update"
    else:
        base = path_names.get(path, names.get(tail, _humanize(tail)))
    if "/emon/" in path:
        energy_domains = (
            {
                "totalConsumption": "Gesamt",
                "chConsumption": "Heizung",
                "dhwConsumption": "Warmwasser",
                "coolingConsumption": "Kühlung",
            }
            if german
            else {
                "totalConsumption": "Total",
                "chConsumption": "Heating",
                "dhwConsumption": "Hot water",
                "coolingConsumption": "Cooling",
            }
        )
        domain = energy_domains.get(tail, base)
        if subkey is None:
            return domain
        subname = subkey_names.get(subkey, _humanize(subkey))
        if subkey == "outputProduced" and tail == "coolingConsumption":
            subname = "Erzeugte Kühlenergie" if german else "Cooling energy produced"
        return f"{domain} \N{EN DASH} {subname}"
    if subkey is None:
        return base
    subname = subkey_names.get(subkey, _humanize(subkey))
    return f"{base} {subname}"


def configured_device_name(value: str) -> str | None:
    """Decode an optional PointT circuit name and reject control characters."""
    candidate = value.strip()
    if not candidate:
        return None
    try:
        encoded = base64.b64decode(candidate, validate=True)
    except binascii.Error, ValueError:
        encoded = b""
    if encoded and len(encoded) % 2 == 0 and b"\x00" in encoded:
        for encoding in ("utf-16-be", "utf-16-le"):
            try:
                decoded = encoded.decode(encoding).strip()
            except UnicodeDecodeError:
                continue
            if _valid_device_name(decoded):
                return decoded
    return candidate if _valid_device_name(candidate) else None


def _humanize(value: str) -> str:
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    words = words.replace("_", " ").replace(".", " ").strip()
    return words[:1].upper() + words[1:] if words else "Wert"


def _valid_device_name(value: str) -> bool:
    return 1 <= len(value) <= 64 and all(character.isprintable() for character in value)
