"""Translate PointT enum spellings at the Home Assistant boundary."""

from __future__ import annotations

_POINTT_TO_HA: dict[str, dict[str, str]] = {
    "energy_management_status": {"notConnected": "not_connected"},
    "heat_source_type": {
        "Heatpump": "heatpump",
        "Boiler": "boiler",
        "Hybrid": "hybrid",
    },
    "hot_water_operation_mode": {"Off": "off"},
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
