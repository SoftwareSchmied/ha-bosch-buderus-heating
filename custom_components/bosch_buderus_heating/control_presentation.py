"""Names, icons and categories shared by the device page and controls dialog."""

from __future__ import annotations

import re

from homeassistant.const import EntityCategory

from . import rrc_contracts
from .coordinator import BoschBuderusDataUpdateCoordinator
from .device import grouped_entity_name
from .i18n import localize

# Keep the meaning of each setting, especially DHW start temperatures.
_LABELS = {
    "operationMode": ("Betriebsart", "Operating mode"),
    "manualRoomSetpoint": ("Manueller Sollwert", "Manual setpoint"),
    "maxFlowTemp": ("Vorlaufgrenze", "Flow temperature limit"),
    "temperatureLevels/comfort2": ("Heiztemperatur", "Heating temperature"),
    "chargeDuration": ("Dauer Extra-Warmwasser", "Extra hot water duration"),
    "singleChargeSetpoint": ("Extra-Warmwasser Sollwert", "Extra hot water target"),
    "charge": ("Extra-Warmwasser", "Extra hot water"),
    "reduceTempOnAlarm": ("Absenkung bei Störung", "Reduce on alarm"),
    "silentMode/enabled": ("Flüstermodus", "Silent mode"),
    "awayMode/enabled": ("Abwesenheit", "Away mode"),
    "seasonOptimizer/mode": ("Saisonumschaltung", "Season switching"),
    "additionalHeater/operationMode": ("Zuheizer", "Auxiliary heater"),
}


def _heating_presentation(path: str) -> bool:
    return path.startswith(("/heatingCircuits/", "/dhwCircuits/")) or path in {
        "/system/awayMode/enabled",
        "/system/silentMode/enabled",
        "/system/seasonOptimizer/mode",
        "/heatSources/additionalHeater/operationMode",
    }


def control_label(
    path: str, fallback: str, language: str, *, contract: bool = False
) -> str:
    """Short localized function name without discarding its meaning.

    ``contract``: the control comes from an RRC contract, whose name wins.
    """
    if contract and (label := rrc_contracts.name(path, language)) is not None:
        return label
    if not _heating_presentation(path):
        return fallback
    if "/temperatureLevels/" in path:
        level = path.rsplit("/", 1)[-1]
        if path.startswith("/dhwCircuits/"):
            level_labels = {
                "eco": localize(language, "Startschwelle Eco+", "Start threshold Eco+"),
                "low": localize(language, "Startschwelle Eco", "Start threshold Eco"),
                "high": localize(
                    language, "Startschwelle Komfort", "Start threshold Comfort"
                ),
            }
            if level in level_labels:
                return level_labels[level]
        elif level == "eco":
            return localize(language, "Absenktemperatur", "Setback temperature")
    for suffix, labels in reversed(tuple(_LABELS.items())):
        if path.endswith("/" + suffix):
            return localize(language, *labels)
    return fallback


def control_name(
    coordinator: BoschBuderusDataUpdateCoordinator,
    path: str,
    fallback: str,
    *,
    contract: bool = False,
) -> str:
    """Shorten generic groups, preserving configured room and device names."""
    if not _heating_presentation(path):
        return grouped_entity_name(coordinator, path, fallback)
    language = coordinator.hass.config.language
    name = control_label(path, fallback, language, contract=contract)
    grouped = grouped_entity_name(coordinator, path, name)
    group = grouped.removesuffix(" \N{EN DASH} " + name)
    for generic, short in (
        (
            ("Heizkreis", "Heating circuit"),
            localize(language, "HK", "HC"),
        ),
        (("Warmwasser", "Hot water"), localize(language, "WW", "DHW")),
    ):
        names = {*generic, localize(language, *generic)}
        pattern = "|".join(re.escape(item) for item in sorted(names))
        match = re.fullmatch(rf"(?:{pattern}) (\d+)", group)
        if match is not None:
            group = f"{short} {match.group(1)}"
    return f"{group}: {name}"


def control_icon(
    path: str, *, numeric: bool = False, contract: bool = False
) -> str | None:
    if contract and (icon := rrc_contracts.icon(path)) is not None:
        return icon
    if not _heating_presentation(path):
        return None
    if path.endswith("/chargeDuration"):
        return "mdi:timer-outline"
    if numeric:
        return "mdi:thermometer"
    if path.startswith("/dhwCircuits/"):
        return "mdi:water-boiler"
    if "/awayMode/" in path:
        return "mdi:home-export-outline"
    if "/silentMode/" in path:
        return "mdi:volume-low"
    if "/seasonOptimizer/" in path:
        return "mdi:sun-snowflake"
    return "mdi:radiator"


def control_category(
    path: str, *, numeric: bool = False, contract: bool = False
) -> EntityCategory | None:
    if contract and (category := rrc_contracts.category(path)) is not None:
        return EntityCategory(category)
    if not _heating_presentation(path):
        return EntityCategory.CONFIG if numeric else None
    # Keep the original controls available for automations while combined
    # climate/water-heater entities provide everyday controls on the device page.
    if path.startswith("/heatingCircuits/") and path.endswith(
        ("/manualRoomSetpoint", "/operationMode")
    ):
        return EntityCategory.CONFIG
    if path.startswith("/dhwCircuits/") and path.endswith("/operationMode"):
        return EntityCategory.CONFIG
    if numeric and not path.endswith("/manualRoomSetpoint"):
        return EntityCategory.CONFIG
    if path in {
        "/heatSources/additionalHeater/operationMode",
        "/system/seasonOptimizer/mode",
    } or path.endswith("/reduceTempOnAlarm"):
        return EntityCategory.CONFIG
    return None
