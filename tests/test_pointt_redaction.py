from __future__ import annotations

import pytest

from custom_components.bosch_buderus_heating.pointt import anonymize_identifier
from custom_components.bosch_buderus_heating.pointt.redaction import (
    diagnostic_resource_path,
    resource_path_template,
)


def test_identifier_hash_is_stable_and_hides_the_value() -> None:
    hashed = anonymize_identifier("gateway-123", salt=b"installation salt")
    assert hashed.startswith("id:") and "gateway-123" not in hashed
    assert hashed == anonymize_identifier("gateway-123", salt=b"installation salt")
    assert hashed != anonymize_identifier("gateway-123", salt=b"other salt")


def test_no_denylist_redaction_helpers_are_offered() -> None:
    """PRIV-3: diagnostics use allowlists; no incomplete generic helper."""
    from custom_components.bosch_buderus_heating import pointt
    from custom_components.bosch_buderus_heating.pointt import redaction

    for name in ("redact_mapping", "redact_text"):
        assert not hasattr(pointt, name)
        assert not hasattr(redaction, name)
        assert name not in pointt.__all__


def test_resource_path_template_removes_installation_identifiers() -> None:
    assert resource_path_template("/heatingCircuits/private-id/status") == (
        "/heatingCircuits/{hc}/status"
    )
    assert resource_path_template("/devices/private-device/errors") == (
        "/devices/{device}/errors"
    )


def test_diagnostic_resource_path_keeps_only_safe_logical_identifiers() -> None:
    assert diagnostic_resource_path("/heatingCircuits/hc2/operationMode") == (
        "/heatingCircuits/hc2/operationMode"
    )
    assert diagnostic_resource_path("/dhwCircuits/dhw7/actualTemp") == (
        "/dhwCircuits/dhw7/actualTemp"
    )
    assert diagnostic_resource_path("/heatSources/hs3/type") == (
        "/heatSources/hs3/type"
    )
    assert diagnostic_resource_path("/heatSources/emon/totalConsumption") == (
        "/heatSources/emon/totalConsumption"
    )
    assert diagnostic_resource_path("/zones/list") == "/zones/list"
    assert diagnostic_resource_path("/heatingCircuits/private-id/status") == (
        "/heatingCircuits/{hc}/status"
    )
    assert diagnostic_resource_path("/devices/private-device/errors") == (
        "/devices/{device}/errors"
    )


@pytest.mark.parametrize(
    ("root", "canonical", "placeholder"),
    [
        ("heatingCircuits", "hc2", "{hc}"),
        ("dhwCircuits", "dhw7", "{dhw}"),
        ("heatSources", "hs3", "{hs}"),
        ("solarCircuits", "sc4", "{solar}"),
        ("ventilation", "zone5", "{zone}"),
        ("zones", "zone6", "{zone}"),
    ],
)
def test_diagnostic_paths_hide_unknown_ids_without_hiding_logical_circuits(
    root: str, canonical: str, placeholder: str
) -> None:
    path = f"/{root}/{canonical}/status"
    assert diagnostic_resource_path(path) == path
    for identifier in ("private-id", f"{canonical}-private"):
        assert diagnostic_resource_path(f"/{root}/{identifier}/status") == (
            f"/{root}/{placeholder}/status"
        )


@pytest.mark.parametrize(
    "path",
    [
        "/heatingCircuits/list",
        "/dhwCircuits/waterTotalConsumption",
        "/heatSources/emon/totalConsumption",
        "/heatSources/hybrid/activeHeatSource",
        "/heatSources/Source/eHeater/status",
        "/ventilation/operationModes/manual/fanSetpoint",
        "/zones/configuration",
        "/zones/list",
    ],
)
def test_diagnostic_paths_preserve_known_static_resources(path: str) -> None:
    assert diagnostic_resource_path(path) == path
