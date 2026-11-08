"""P44: a new local API specification is compared before the pin moves.

The pinned facts are turned back into an OpenAPI document, so the comparison
runs through the same extraction as a real new specification. Each case then
changes one thing, as a new specification version could.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
import yaml

from custom_components.bosch_buderus_heating.local_api._data import OPERATIONS
from scripts.compare_local_api_spec import (
    Change,
    candidate_facts,
    compare,
    main,
    pinned_facts,
    run,
    summary,
    used_templates,
)

RETURN = "/heatSources/returnTemperature"
RECORDING = "/recordings/heatSources/actualSupplyTemperature"
WIFI = "/gateway/wifi/apList"


def spec_from_pinned() -> dict[str, Any]:
    """An OpenAPI document equivalent to the pinned facts."""
    facts = pinned_facts()
    examples: dict[str, dict[str, Any]] = {}
    for row in facts.examples:
        examples.setdefault(row["template"], {})[row["name"]] = row["response"]
    paths = {}
    for template, operation in facts.operations.items():
        content: dict[str, Any] = {"schema": copy.deepcopy(operation["response"])}
        named = examples.get(template, {})
        if "default" in named:
            content["example"] = named["default"]
        others = {k: {"value": v} for k, v in named.items() if k != "default"}
        if others:
            content["examples"] = others
        paths[template] = {
            "get": {
                "tags": [operation["group"]],
                "parameters": copy.deepcopy(operation["parameters"]),
                "responses": {"200": {"content": {"application/json": content}}},
            }
        }
    return {
        "openapi": "3.0.3",
        "info": {"version": "synthetic"},
        "servers": [{"url": "https://{gatewayAddress}:9443"}],
        "security": [{"bearerAuth": []}],
        "paths": paths,
        "components": {
            "schemas": copy.deepcopy(facts.schemas),
            "parameters": {},
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "description": "x"}
            },
        },
    }


@pytest.fixture(scope="module")
def used() -> dict[str, str]:
    return used_templates()


def changes_for(spec, used) -> list[Change]:
    facts, unknown = candidate_facts(spec)
    assert unknown == []
    return compare(pinned_facts(), facts, used)


def _example(spec, template, name="default"):
    content = spec["paths"][template]["get"]["responses"]["200"]["content"][
        "application/json"
    ]
    return content["example"] if name == "default" else content["examples"][name]


def _float_value(spec) -> dict[str, Any]:
    return spec["components"]["schemas"]["FloatValue"]["allOf"][1]


def test_the_pinned_facts_round_trip_without_differences(used) -> None:
    assert changes_for(spec_from_pinned(), used) == []


# ------------------------------------------------------------ need adaptation


def _type_change(spec):
    _float_value(spec)["properties"]["value"]["type"] = "string"


def _unit_change(spec):
    _example(spec, RETURN)["unitOfMeasure"] = "K"


def _parameter_value_removed(spec):
    spec["components"]["schemas"]["RecordingSampleRate"]["enum"].remove("PT1H")


def _required_parameter_added(spec):
    spec["paths"][RETURN]["get"]["parameters"].append(
        {"name": "scale", "in": "query", "required": True, "schema": {"type": "string"}}
    )


def _field_no_longer_guaranteed(spec):
    _float_value(spec)["required"] = []


def _new_response_value(spec):
    _float_value(spec)["properties"]["type"]["enum"].append("decimalValue")


def _limit_changed(spec):
    spec["components"]["schemas"]["FloatValue"]["allOf"][1]["properties"]["value"][
        "maximum"
    ] = 100


def _example_value_kind(spec):
    _example(spec, RETURN)["value"] = "-3276.8"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (_type_change, "value.type"),
        (_unit_change, 'unit: "C" -> "K"'),
        (_parameter_value_removed, "values removed ['PT1H']"),
        (_required_parameter_added, "parameter scale (query) added"),
        (_field_no_longer_guaranteed, "value no longer required"),
        (_new_response_value, "values added ['decimalValue']"),
        (_limit_changed, "value.maximum: missing -> 100"),
        (_example_value_kind, "value kind: number -> str"),
    ],
    ids=lambda item: getattr(item, "__name__", None) or "",
)
def test_a_changed_contract_is_reported_for_adaptation(change, expected, used) -> None:
    spec = spec_from_pinned()
    change(spec)
    changes = changes_for(spec, used)
    assert changes, "the change went unnoticed"
    assert {c.level for c in changes} == {"adapt"}
    assert any(expected in c.detail for c in changes), [c.detail for c in changes]
    if change is _parameter_value_removed:
        # Recordings are not read on a schedule; the template is still named.
        assert RECORDING in changes[0].templates
    else:
        assert summary(changes)["affecting_used_templates"] >= 1


def test_a_shared_schema_change_names_the_used_templates(used) -> None:
    spec = spec_from_pinned()
    _type_change(spec)
    (change,) = changes_for(spec, used)
    assert RETURN in change.templates
    assert "shown" in change.used  # the return temperature is a paired entity
    assert len(change.templates) > 50


def test_a_removed_path_breaks(used) -> None:
    spec = spec_from_pinned()
    del spec["paths"][RETURN]
    changes = changes_for(spec, used)
    assert [(c.level, c.detail) for c in changes if c.area == "operation"] == [
        ("breaking", "path removed")
    ]
    assert changes[0].level == "breaking" and changes[0].used == ("shown",)


# --------------------------------------------------------------- compatible


def _field_added(spec):
    _float_value(spec)["properties"]["quality"] = {"type": "integer"}


def _path_added(spec):
    spec["paths"]["/heatSources/newSensor"] = copy.deepcopy(spec["paths"][RETURN])


def _example_added(spec):
    content = spec["paths"][RETURN]["get"]["responses"]["200"]["content"][
        "application/json"
    ]
    content["examples"] = {"open": {"value": dict(content["example"], value=1.0)}}


def _parameter_value_added(spec):
    spec["components"]["schemas"]["RecordingSampleRate"]["enum"].append("PT15M")


def _optional_parameter_added(spec):
    spec["paths"][WIFI]["get"]["parameters"].append(
        {"name": "band", "in": "query", "schema": {"type": "string"}}
    )


def _response_value_removed(spec):
    spec["components"]["schemas"]["BaseResource"]["properties"]["writeable"][
        "enum"
    ].remove(1)


@pytest.mark.parametrize(
    "change",
    [
        _field_added,
        _path_added,
        _example_added,
        _parameter_value_added,
        _optional_parameter_added,
        _response_value_removed,
    ],
)
def test_additions_stay_compatible(change, used) -> None:
    spec = spec_from_pinned()
    change(spec)
    changes = changes_for(spec, used)
    assert changes, "the addition went unnoticed"
    assert {c.level for c in changes} == {"compatible"}


def test_a_new_path_needs_a_polling_class_before_it_is_read(used) -> None:
    spec = spec_from_pinned()
    _path_added(spec)
    (change,) = [c for c in changes_for(spec, used) if c.area == "operation"]
    assert "polling class" in change.detail
    assert change.used == ()


def test_an_unknown_schema_keyword_is_recorded_instead_of_stopping() -> None:
    spec = spec_from_pinned()
    spec["components"]["schemas"]["FloatValue"]["discriminator"] = {"x": 1}
    _float_value(spec)["properties"]["value"]["type"] = "decimal"
    _, unknown = candidate_facts(spec)
    assert unknown == [
        'schema FloatValue.allOf[1].value: type "decimal"',
        "schema FloatValue: keyword 'discriminator'",
    ]
    changes, _ = run(spec, b"")
    extraction = [c for c in changes if c.area == "extraction"]
    assert {(c.level, c.where) for c in extraction} == {
        ("adapt", "schema FloatValue"),
        ("adapt", "schema FloatValue.allOf[1].value"),
    }


# ---------------------------------------------------------------------- CLI


def test_command_line_report_and_exit_code(tmp_path, capsys) -> None:
    same = tmp_path / "same.yaml"
    same.write_text(yaml.safe_dump(spec_from_pinned()), encoding="utf-8")
    assert main([str(same)]) == 0
    assert "No differences to the pinned specification." in capsys.readouterr().out

    changed = spec_from_pinned()
    _unit_change(changed)
    _field_added(changed)
    path = tmp_path / "changed.yaml"
    path.write_text(yaml.safe_dump(changed), encoding="utf-8")
    assert main([str(path)]) == 3  # needs adaptation
    text = capsys.readouterr().out
    assert "## adapt" in text and "## compatible" in text
    assert "identical to the pin: False" in text
    assert main([str(path), "--json"]) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["adapt"] == 1
    assert report["header"]["candidate info.version"] == "synthetic"


def test_used_templates_are_documented_operations(used) -> None:
    assert set(used) <= set(OPERATIONS)
    assert used[RETURN] == "shown"
    assert used["/gateway/versionFirmware"] in {"read", "shown"}
    assert set(used.values()) == {"read", "shown"}
    # Administration paths (network, updates) are never read on a schedule.
    assert "/gateway/wifi/apList" not in used


# ------------------------------------------------- cases from the P44 review

SUWI = "/heatingCircuits/{heatingCircuitId}/currentSuWiMode"


def test_a_whole_required_list_can_disappear_or_appear(used) -> None:
    spec = spec_from_pinned()
    del _float_value(spec)["required"]
    string = spec["components"]["schemas"]["StringValue"]["allOf"][1]
    string["required"] = ["value", "allowedValues"]
    details = {(c.level, c.where, c.detail) for c in changes_for(spec, used)}
    assert ("adapt", "FloatValue", "field allOf[1].value no longer required") in (
        details
    )
    assert (
        "compatible",
        "StringValue",
        "field allOf[1].allowedValues now required",
    ) in details


def test_a_removed_guaranteed_field_breaks(used) -> None:
    spec = spec_from_pinned()
    del _float_value(spec)["properties"]["value"]
    levels = {c.detail: c.level for c in changes_for(spec, used)}
    assert levels["field allOf[1].value removed"] == "breaking"
    spec = spec_from_pinned()
    del _float_value(spec)["properties"]["stepSize"]  # optional field
    levels = {c.detail: c.level for c in changes_for(spec, used)}
    assert levels["field allOf[1].stepSize removed"] == "adapt"


def test_a_renamed_example_still_shows_its_unit_change(used) -> None:
    spec = spec_from_pinned()
    content = spec["paths"][RETURN]["get"]["responses"]["200"]["content"][
        "application/json"
    ]
    example = content.pop("example")
    content["examples"] = {"standard": {"value": dict(example, unitOfMeasure="K")}}
    changes = changes_for(spec, used)
    assert any(
        c.level == "adapt" and c.detail.startswith("unit:") and c.where == RETURN
        for c in changes
    ), [c.detail for c in changes]


@pytest.mark.parametrize(
    ("template", "edit", "expected"),
    [
        (SUWI, lambda e: e["allowedValues"].append("auto"), "allowed value"),
        (RETURN, lambda e: e["state"].append({"open": -1}), "state"),
        (RETURN, lambda e: e.update(minValue=-20), "limit"),
    ],
    ids=["allowed_value", "state", "limit"],
)
def test_facts_stated_only_in_examples_are_compared(
    template, edit, expected, used
) -> None:
    spec = spec_from_pinned()
    edit(_example(spec, template))
    changes = changes_for(spec, used)
    assert any(c.level == "adapt" and c.detail.startswith(expected) for c in changes), [
        c.detail for c in changes
    ]


def test_a_parameter_that_becomes_optional_stays_compatible(used) -> None:
    spec = spec_from_pinned()
    for parameter in spec["paths"][RECORDING]["get"]["parameters"]:
        if parameter["name"] == "sampleRate":
            parameter["required"] = False
    (change,) = changes_for(spec, used)
    assert (change.level, change.detail) == (
        "compatible",
        "parameter sampleRate (query) now optional",
    )


def test_a_renamed_path_parameter_is_not_reported_as_a_removal(used) -> None:
    spec = spec_from_pinned()
    old = "/heatingCircuits/{heatingCircuitId}/currentRoomSetpoint"
    new = "/heatingCircuits/{hcId}/currentRoomSetpoint"
    item = spec["paths"].pop(old)
    for parameter in item["get"]["parameters"]:
        if parameter["in"] == "path":
            parameter["name"] = "hcId"
    spec["paths"][new] = item
    changes = changes_for(spec, used)
    assert {c.level for c in changes} == {"adapt"}
    assert any("renamed" in c.detail and c.used == ("shown",) for c in changes)


def test_changed_sign_in_or_server_needs_adaptation(used) -> None:
    spec = spec_from_pinned()
    spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] = "basic"
    spec["servers"] = [{"url": "https://{gatewayAddress}:9444"}]
    changes = changes_for(spec, used)
    assert {(c.level, c.area, c.where) for c in changes} == {
        ("adapt", "sign-in", "securitySchemes"),
        ("adapt", "sign-in", "servers"),
    }


def test_a_new_example_contradicting_its_meaning_needs_adaptation(used) -> None:
    spec = spec_from_pinned()
    _example(spec, RETURN)["unitOfMeasure"] = "°K"
    changes = changes_for(spec, used)
    assert any("contradicts its meaning (unit: °K)" in c.detail for c in changes)


def _broken(spec, kind):
    get = spec["paths"][RETURN]["get"]
    if kind == "nullable_type":
        _float_value(spec)["properties"]["value"]["type"] = ["number", "null"]
    elif kind == "boolean_schema":
        _float_value(spec)["properties"]["extra"] = True
    elif kind == "no_200":
        get["responses"] = {"default": get["responses"]["200"]}
    elif kind == "no_tags":
        del get["tags"]
    elif kind == "missing_parameter_reference":
        get["parameters"].append({"$ref": "#/components/parameters/Gone"})
    elif kind == "parameter_content":
        get["parameters"].append({"name": "q", "in": "query", "content": {}})
    elif kind == "example_reference":
        content = get["responses"]["200"]["content"]["application/json"]
        content["examples"] = {"ref": {"$ref": "#/components/examples/x"}}
    elif kind == "no_schemas":
        del spec["components"]["schemas"]
    elif kind == "path_summary":
        spec["paths"][RETURN]["summary"] = "text"


@pytest.mark.parametrize(
    "kind",
    [
        "nullable_type",
        "boolean_schema",
        "no_200",
        "no_tags",
        "missing_parameter_reference",
        "parameter_content",
        "example_reference",
        "no_schemas",
    ],
)
def test_unexpected_shapes_are_recorded_with_their_place(kind) -> None:
    spec = spec_from_pinned()
    _broken(spec, kind)
    changes, _ = run(spec, b"")
    assert [c for c in changes if c.level != "compatible"], kind
    extraction = [c for c in changes if c.area == "extraction"]
    assert extraction, kind
    assert all(c.where for c in extraction)
    # Each shape is handled where it occurs, not by the last-resort guard.
    assert not any("not readable" in c.detail for c in extraction), kind


def test_a_path_summary_is_no_contract_change(used) -> None:
    spec = spec_from_pinned()
    _broken(spec, "path_summary")
    assert changes_for(spec, used) == []


def test_breaking_has_its_own_exit_code(tmp_path) -> None:
    spec = spec_from_pinned()
    del spec["paths"][RETURN]
    path = tmp_path / "removed.yaml"
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    assert main([str(path)]) == 4


def test_the_script_also_runs_as_a_file(tmp_path) -> None:
    import subprocess
    import sys
    from pathlib import Path

    path = tmp_path / "same.yaml"
    path.write_text(yaml.safe_dump(spec_from_pinned()), encoding="utf-8")
    script = Path(__file__).parents[1] / "scripts" / "compare_local_api_spec.py"
    result = subprocess.run(
        [sys.executable, str(script), str(path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "No differences" in result.stdout
