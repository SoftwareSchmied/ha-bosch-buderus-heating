"""Translate PointT enum spellings at the Home Assistant boundary."""

from __future__ import annotations

_POINTT_TO_HA: dict[str, dict[str, str]] = {
    "compressor_status": {"poolHeat": "pool_heat"},
    "data_processing_status": {
        "IN_PROGRESS": "in_progress",
        "inProgress": "in_progress",
        "COMPLETE": "completed",
        "complete": "completed",
    },
    "electric_auxiliary_heater_status": {"poolHeat": "pool_heat"},
    "energy_management_status": {
        "notConnected": "not_connected",
        "activeCh": "active_ch",
        "activeDhw": "active_dhw",
        "activeEm": "active_em",
    },
    "heating_circuit_control_type": {
        "roomflowtemp": "room_flow_temperature",
        "roompower": "room_power",
        "constants": "constant",
        "valvefbcntrl": "valve_feedback_control",
        "ISRC": "isrc",
    },
    "heating_circuit_heat_cool_mode": {"heatCool": "heat_cool"},
    "heat_source_type": {
        "No_Appliance": "no_appliance",
        "Heatpump": "heatpump",
        "OilBoiler": "boiler_oil",
        "GasBoiler": "boiler_gas",
        "unknownBoiler": "boiler_unknown",
        "Boiler": "boiler",
        "Hybrid": "hybrid",
        "gas_boiler": "boiler",
    },
    "heat_pump_type": {
        "liquid_water": "brine_water",
        "exhaustAir_water": "exhaust_air",
    },
    "hot_water_operation_mode": {
        "Off": "off",
        "HCprogram": "follow_heating_program",
    },
    "isrc_support_status": {
        "notSupportedIncompatibleController": ("not_supported_incompatible_controller"),
        "notSupportedPairingEnabled": "not_supported_pairing_enabled",
        "inEvaluation": "in_evaluation",
    },
    "season_optimizer_mode": {
        "forcedHeat": "forced_heat",
        "forcedCool": "forced_cool",
    },
    "pv_contact_state": {"off": "inactive", "on": "active"},
    "system_type": {
        "boiler": "boiler_single",
        "eHybrid": "hybrid",
        "hybman": "hybrid",
        "hybridBoiler": "hybrid",
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
