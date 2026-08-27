"""Translate PointT enum spellings at the Home Assistant boundary."""

from __future__ import annotations

_POINTT_TO_HA: dict[str, dict[str, str]] = {
    "compressor_status": {"poolHeat": "pool_heat"},
    "data_processing_status": {"inProgress": "in_progress"},
    "electric_auxiliary_heater_status": {"poolHeat": "pool_heat"},
    "energy_management_status": {"notConnected": "not_connected"},
    "heating_circuit_heat_cool_mode": {"heatCool": "heat_cool"},
    "heat_source_type": {
        "Heatpump": "heatpump",
        "Boiler": "boiler",
        "Hybrid": "hybrid",
        "gas_boiler": "boiler",
    },
    "hot_water_operation_mode": {"Off": "off"},
    "isrc_support_status": {
        "notSupportedIncompatibleController": ("not_supported_incompatible_controller"),
        "notSupportedPairingEnabled": "not_supported_pairing_enabled",
        "inEvaluation": "in_evaluation",
    },
    "season_optimizer_mode": {
        "forcedHeat": "forced_heat",
        "forcedCool": "forced_cool",
    },
}

_HA_TO_POINTT: dict[str, dict[str, str]] = {
    group: {translated: raw for raw, translated in aliases.items()}
    for group, aliases in _POINTT_TO_HA.items()
}


def enum_value_to_ha(translation_key: str, value: str) -> str:
    """Return a Home Assistant-safe enum option without changing PointT data."""
    return _POINTT_TO_HA.get(translation_key, {}).get(value, value)


def enum_value_to_pointt(translation_key: str, value: str) -> str:
    """Restore the exact PointT spelling before a write."""
    return _HA_TO_POINTT.get(translation_key, {}).get(value, value)
