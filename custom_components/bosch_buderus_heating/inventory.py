"""Create a shareable structural inventory from Home Assistant diagnostics."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

INVENTORY_SCHEMA_VERSION = 1
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.%/{\}-]{1,96}")
_CAPABILITY_FIELDS = (
    "path_template",
    "resource_type",
    "unit",
    "poll_group",
    "entity_supported",
    "maturity",
    "entity_enabled_by_default",
    "writable",
    "has_value",
    "value_shape",
    "values_count",
    "references_count",
    "allowed_values_count",
    "has_minimum",
    "has_maximum",
)
_INVENTORY_FIELDS = (
    "resource_count",
    "writable_count",
    "entity_supported_count",
    "resource_types",
    "polling_groups",
    "maturity_levels",
    "current_error_categories",
)


class InventoryError(ValueError):
    """Raised when a diagnostic export is not safe or structurally valid."""


def build_anonymized_inventory(
    diagnostics: Mapping[str, object], *, captured_at: datetime | None = None
) -> dict[str, object]:
    """Reduce integration diagnostics to a value-free, shareable inventory."""
    wrapped = diagnostics.get("data")
    if "privacy" not in diagnostics and isinstance(wrapped, Mapping):
        diagnostics = wrapped
    _validate_privacy(diagnostics.get("privacy"))
    diagnostics_schema = diagnostics.get("diagnostics_schema")
    if not isinstance(diagnostics_schema, int) or isinstance(diagnostics_schema, bool):
        raise InventoryError("Diagnostics schema version is missing")
    gateways = diagnostics.get("gateways")
    if not isinstance(gateways, list):
        raise InventoryError("Diagnostics do not contain a gateway list")
    timestamp = captured_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise InventoryError("Capture time must include a timezone")
    safe_gateways = [
        _safe_gateway(number, gateway)
        for number, gateway in enumerate(gateways, start=1)
    ]
    return {
        "inventory_schema": INVENTORY_SCHEMA_VERSION,
        "source_diagnostics_schema": diagnostics_schema,
        "captured_at": timestamp.astimezone(UTC).replace(microsecond=0).isoformat(),
        "privacy": {
            "contains_credentials": False,
            "contains_stable_identifiers": False,
            "contains_user_defined_names": False,
            "contains_raw_resource_values": False,
        },
        "gateway_count": len(safe_gateways),
        "gateways": safe_gateways,
    }


def _validate_privacy(value: object) -> None:
    if not isinstance(value, Mapping):
        raise InventoryError("Diagnostics privacy declaration is missing")
    required = (
        "contains_credentials",
        "contains_stable_identifiers",
        "contains_user_defined_names",
        "contains_raw_resource_values",
    )
    if any(value.get(key) is not False for key in required):
        raise InventoryError("Diagnostics are not declared safe for sharing")


def _safe_gateway(number: int, value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise InventoryError("Gateway inventory is invalid")
    capabilities = value.get("capabilities")
    inventory = value.get("inventory")
    if not isinstance(capabilities, list) or not isinstance(inventory, Mapping):
        raise InventoryError("Gateway capabilities or summary are missing")
    device_class = _token(value.get("device_class"), "device class")
    return {
        "label": f"gateway_{number}",
        "device_class": device_class,
        "summary": {
            key: _safe_structure(inventory.get(key), key) for key in _INVENTORY_FIELDS
        },
        "capabilities": [_safe_capability(item) for item in capabilities],
    }


def _safe_capability(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise InventoryError("Capability entry is invalid")
    path = _token(value.get("path_template"), "capability path")
    for root, placeholder in (
        ("heatingCircuits", "{hc}"),
        ("dhwCircuits", "{dhw}"),
    ):
        match = re.match(rf"^/{root}/([^/]+)", path)
        if match is not None and match.group(1) != placeholder:
            raise InventoryError("Capability path contains an installation identifier")
    if re.match(r"^/heatSources/hs\d+(?:/|$)", path, re.IGNORECASE):
        raise InventoryError("Capability path contains an installation identifier")
    return {key: _safe_structure(value.get(key), key) for key in _CAPABILITY_FIELDS}


def _safe_structure(value: object, field: str) -> object:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _token(value, field)
    if isinstance(value, Mapping):
        return {
            _token(key, field): _safe_structure(item, field)
            for key, item in value.items()
            if isinstance(key, str)
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_safe_structure(item, field) for item in value]
    raise InventoryError(f"Unsupported value in {field}")


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or _SAFE_TOKEN.fullmatch(value) is None:
        raise InventoryError(f"Unsafe or invalid {field}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a privacy-safe PointT capability inventory from a downloaded "
            "Bosch/Buderus Heating diagnostics JSON file."
        )
    )
    parser.add_argument("diagnostics", type=Path, help="Downloaded diagnostics JSON")
    parser.add_argument("output", type=Path, help="Inventory JSON to create")
    parser.add_argument(
        "--force", action="store_true", help="Replace an existing output file"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local inventory exporter."""
    args = _parser().parse_args(argv)
    if args.output.exists() and not args.force:
        _parser().error("output already exists; use --force to replace it")
    try:
        source = json.loads(args.diagnostics.read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            raise InventoryError("Diagnostics root must be an object")
        inventory = build_anonymized_inventory(source)
    except (OSError, json.JSONDecodeError, InventoryError) as err:
        _parser().error(str(err))
    args.output.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
