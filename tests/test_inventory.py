"""Tests for the privacy-safe capability inventory exporter."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.bosch_buderus_heating.inventory import (
    InventoryError,
    build_anonymized_inventory,
    main,
)


def _diagnostics() -> dict[str, object]:
    return {
        "diagnostics_schema": 2,
        "privacy": {
            "contains_raw_resource_values": False,
            "contains_credentials": False,
            "contains_stable_identifiers": False,
            "contains_user_defined_names": False,
        },
        "config": {"secret": "must-not-survive"},
        "gateways": [
            {
                "label": "ignored-label",
                "device_class": "k40",
                "runtime": {"private": "ignored"},
                "inventory": {
                    "resource_count": 1,
                    "writable_count": 1,
                    "entity_supported_count": 1,
                    "resource_types": {"floatValue": 1},
                    "polling_groups": {"control": 1},
                    "maturity_levels": {"understood": 1},
                    "current_error_categories": {},
                },
                "capabilities": [
                    {
                        "path_template": ("/dhwCircuits/{dhw}/temperatureLevels/eco"),
                        "name": "must-not-survive",
                        "resource_type": "floatValue",
                        "unit": "C",
                        "poll_group": "control",
                        "entity_supported": True,
                        "maturity": "understood",
                        "entity_enabled_by_default": False,
                        "writable": True,
                        "has_value": True,
                        "value_shape": "number",
                        "values_count": 0,
                        "references_count": 0,
                        "allowed_values_count": 0,
                        "has_minimum": True,
                        "has_maximum": True,
                        "calls": {"ignored": "raw-runtime-data"},
                    }
                ],
            }
        ],
    }


def test_inventory_contains_structure_but_no_runtime_or_names() -> None:
    inventory = build_anonymized_inventory(
        _diagnostics(), captured_at=datetime(2026, 8, 16, 12, tzinfo=UTC)
    )
    rendered = repr(inventory)

    assert inventory["captured_at"] == "2026-08-16T12:00:00+00:00"
    assert inventory["gateway_count"] == 1
    assert "must-not-survive" not in rendered
    assert "raw-runtime-data" not in rendered
    capability = inventory["gateways"][0]["capabilities"][0]  # type: ignore[index]
    assert capability["path_template"] == ("/dhwCircuits/{dhw}/temperatureLevels/eco")
    assert capability["writable"] is True

    wrapped = build_anonymized_inventory(
        {"home_assistant": {"version": "ignored"}, "data": _diagnostics()},
        captured_at=datetime(2026, 8, 16, 12, tzinfo=UTC),
    )
    assert wrapped == inventory


def test_inventory_rejects_unsafe_privacy_and_concrete_dynamic_ids() -> None:
    unsafe = _diagnostics()
    unsafe["privacy"]["contains_credentials"] = True  # type: ignore[index]
    with pytest.raises(InventoryError, match="not declared safe"):
        build_anonymized_inventory(unsafe)

    concrete = _diagnostics()
    concrete["gateways"][0]["capabilities"][0]["path_template"] = (  # type: ignore[index]
        "/dhwCircuits/dhw7/temperatureLevels/eco"
    )
    with pytest.raises(InventoryError, match="installation identifier"):
        build_anonymized_inventory(concrete)


def test_inventory_accepts_central_heat_source_paths() -> None:
    diagnostics = _diagnostics()
    capability = diagnostics["gateways"][0]["capabilities"][0]  # type: ignore[index]
    capability["path_template"] = "/heatSources/emon/totalConsumption"

    inventory = build_anonymized_inventory(diagnostics)

    exported = inventory["gateways"][0]["capabilities"][0]  # type: ignore[index]
    assert exported["path_template"] == "/heatSources/emon/totalConsumption"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda data: data.pop("privacy"), "privacy declaration"),
        (lambda data: data.pop("diagnostics_schema"), "schema version"),
        (lambda data: data.pop("gateways"), "gateway list"),
        (lambda data: data["gateways"].append("invalid"), "Gateway inventory"),
        (
            lambda data: data["gateways"][0].pop("capabilities"),
            "capabilities or summary",
        ),
        (
            lambda data: data["gateways"][0]["capabilities"].append("invalid"),
            "Capability entry",
        ),
    ],
)
def test_inventory_rejects_incomplete_structures(change, message: str) -> None:
    diagnostics = _diagnostics()
    change(diagnostics)

    with pytest.raises(InventoryError, match=message):
        build_anonymized_inventory(diagnostics)


def test_inventory_requires_timezone_and_safe_tokens() -> None:
    with pytest.raises(InventoryError, match="timezone"):
        build_anonymized_inventory(
            _diagnostics(), captured_at=datetime(2026, 8, 16, 12)
        )
    diagnostics = _diagnostics()
    diagnostics["gateways"][0]["device_class"] = "unsafe private value"  # type: ignore[index]
    with pytest.raises(InventoryError, match="device class"):
        build_anonymized_inventory(diagnostics)


def test_inventory_cli_writes_and_protects_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "diagnostics.json"
    output = tmp_path / "inventory.json"
    source.write_text(json.dumps({"data": _diagnostics()}), encoding="utf-8")

    assert main([str(source), str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["inventory_schema"] == 1
    with pytest.raises(SystemExit):
        main([str(source), str(output)])
    assert main([str(source), str(output), "--force"]) == 0


def test_inventory_cli_rejects_invalid_json(tmp_path: Path) -> None:
    source = tmp_path / "invalid.json"
    source.write_text("not-json", encoding="utf-8")

    with pytest.raises(SystemExit):
        main([str(source), str(tmp_path / "output.json")])
