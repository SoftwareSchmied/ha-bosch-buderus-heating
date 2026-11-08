"""Extract reviewed K40 API facts and examples from the pinned OpenAPI source."""

from __future__ import annotations

import argparse
import hashlib
import json
import pprint
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "78e36b3b57e11ac6266558acba96c13bee88ceff"
SOURCE_SHA256 = "ee7f17d9b783e4722b82e9c141dd63f1bc717c13c04bb435bfb2a7989323fb93"
ANNOTATIONS = {"description", "example", "readOnly", "title"}
FACETS = {
    "$ref",
    "type",
    "required",
    "properties",
    "items",
    "enum",
    "allOf",
    "oneOf",
    "anyOf",
    "additionalProperties",
    "minimum",
    "maximum",
    "minProperties",
    "maxProperties",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "pattern",
    "format",
}


def schema_facts(schema, unknown=None, where="schema"):
    """Keep the contract facets of a schema; annotations are dropped.

    With ``unknown`` (a list) anything a new specification uses that the
    reviewed extraction does not know is recorded there with its place and
    kept, instead of stopping (P44). Without it, the pinned rules are asserted.
    """
    if unknown is None:
        _check_known(schema)
    elif not isinstance(schema, dict):
        unknown.append(f"{where}: schema {json.dumps(schema)} is not an object")
        return schema
    else:
        kind, form = schema.get("type"), schema.get("format")
        if isinstance(kind, list) or kind not in _TYPES:
            unknown.append(f"{where}: type {json.dumps(kind)}")
        if isinstance(form, list) or form not in _FORMATS:
            unknown.append(f"{where}: format {json.dumps(form)}")
        unknown.extend(
            f"{where}: keyword {key!r}"
            for key in sorted(set(schema) - FACETS - ANNOTATIONS)
        )
    result = {}
    for key, value in schema.items():
        if key in ANNOTATIONS:
            continue
        if key == "properties" and isinstance(value, dict):
            value = {
                name: schema_facts(child, unknown, f"{where}.{name}")
                for name, child in value.items()
            }
        elif key in {"items", "additionalProperties"} and isinstance(value, dict):
            value = schema_facts(value, unknown, f"{where}.{key}")
        elif key in {"allOf", "oneOf", "anyOf"} and isinstance(value, list):
            value = [
                schema_facts(child, unknown, f"{where}.{key}[{index}]")
                for index, child in enumerate(value)
            ]
        result[key] = value
    return result


_TYPES = {None, "object", "array", "string", "integer", "number", "boolean"}
_FORMATS = {None, "float", "double", "int32", "int64", "date", "date-time", "uri"}
# Path item and operation keys that carry no contract of a read.
_PATH_ITEM_NOTES = {"summary", "description", "servers"}
_OPERATION_KEYS = {"operationId", "description", "summary", "tags", "responses"}
_OPERATION_KEYS |= {"parameters", "externalDocs"}
# Kept only when present, so the pinned facts stay unchanged.
_OPTIONAL_PARAMETER_FACTS = ("style", "explode", "allowEmptyValue", "deprecated")
_OPTIONAL_OPERATION_FACTS = ("deprecated", "security")


def _check_known(schema):
    assert schema.get("type") in _TYPES
    assert schema.get("format") in _FORMATS
    assert set(schema) <= FACETS | ANNOTATIONS, set(schema) - FACETS - ANNOTATIONS


def extract(spec, unknown=None):
    """Schemas, read operations and examples of one specification.

    Without ``unknown`` this is the strict extraction for the pin. With it, a
    candidate is read as far as possible and every deviation is recorded.
    """
    if unknown is None:
        return _extract(spec, None)
    try:
        return _extract(spec, unknown)
    except (AttributeError, KeyError, IndexError, TypeError) as err:  # pragma: no cover
        unknown.append(f"specification: not readable ({type(err).__name__})")
        return {}, {}, []


def _extract(spec, unknown):
    tolerant = unknown is not None
    components = (spec.get("components") or {}) if tolerant else spec["components"]
    raw_schemas = (
        (components.get("schemas") or {}) if tolerant else components["schemas"]
    )
    if tolerant and not raw_schemas:
        unknown.append("specification: no components.schemas")
    schemas = {
        name: schema_facts(value, unknown, f"schema {name}")
        for name, value in raw_schemas.items()
    }
    operations = {}
    examples = []
    paths = (spec.get("paths") or {}) if tolerant else spec["paths"]
    for path, item in paths.items():
        if not tolerant:
            assert set(item) <= {"get", "parameters"} and "get" in item
        else:
            others = set(item) - {"get", "parameters"} - _PATH_ITEM_NOTES
            if others or "get" not in item:
                unknown.append(
                    f"path {path}: operations {sorted(set(item) - {'parameters'})}"
                )
            if not isinstance(item.get("get"), dict):
                continue
        get = item["get"]
        if tolerant:
            unknown.extend(
                f"path {path}: operation keyword {key!r}"
                for key in sorted(
                    set(get) - _OPERATION_KEYS - set(_OPTIONAL_OPERATION_FACTS)
                )
            )
        parameters = []
        for parameter in item.get("parameters", []) + get.get("parameters", []):
            if "$ref" in parameter:
                name = parameter["$ref"].split("/")[-1]
                if tolerant and name not in (components.get("parameters") or {}):
                    unknown.append(f"path {path}: parameter reference {name!r} missing")
                    continue
                parameter = components["parameters"][name]
            facts = {
                k: parameter[k] for k in ("name", "in", "required") if k in parameter
            }
            if tolerant:
                facts |= {
                    k: parameter[k] for k in _OPTIONAL_PARAMETER_FACTS if k in parameter
                }
                if "schema" not in parameter:
                    unknown.append(
                        f"path {path}: parameter {parameter.get('name')!r} "
                        "without schema"
                    )
                    parameters.append(facts | {"schema": {}})
                    continue
            facts["schema"] = schema_facts(
                parameter["schema"],
                unknown,
                f"path {path} parameter {facts.get('name')}",
            )
            parameters.append(facts)
        if tolerant:
            content = (
                ((get.get("responses") or {}).get("200") or {}).get("content") or {}
            ).get("application/json")
            if not isinstance(content, dict) or "schema" not in content:
                unknown.append(f"path {path}: no JSON schema for status 200")
                content = {"schema": {}}
            tags = get.get("tags") or [None]
            if tags == [None]:
                unknown.append(f"path {path}: no tags")
        else:
            content = get["responses"]["200"]["content"]["application/json"]
            tags = get["tags"]
        operations[path] = {
            "group": tags[0],
            "parameters": parameters,
            "response": schema_facts(
                content["schema"], unknown, f"path {path} response"
            ),
        }
        if tolerant:
            operations[path] |= {
                k: get[k] for k in _OPTIONAL_OPERATION_FACTS if k in get
            }
        if "example" in content:
            examples.append(
                {"template": path, "name": "default", "response": content["example"]}
            )
        for name, example in content.get("examples", {}).items():
            if tolerant and not (isinstance(example, dict) and "value" in example):
                unknown.append(f"path {path}: example {name!r} without inline value")
                continue
            examples.append(
                {"template": path, "name": name, "response": example["value"]}
            )
    return schemas, operations, examples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    raw = args.source.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SOURCE_SHA256, (
        "Review a new source before updating the pin"
    )
    spec = yaml.safe_load(raw)
    schemas, operations, examples = extract(spec)
    assert len(operations) == 261
    header = (
        '"""Reviewed API facts. Rebuild with scripts/update_local_api_catalog.py.\n\n'
        "Adapted from Bosch Thermotechnik GmbH (2026), Apache-2.0.\n"
        "See LICENSE in this directory. Descriptions and examples are omitted.\n"
        '"""\n\nfrom typing import Any\n\n'
    )
    data = header + f'SOURCE_COMMIT = "{COMMIT}"\nSOURCE_SHA256 = "{SOURCE_SHA256}"\n\n'
    data += (
        "SCHEMAS: dict[str, Any] = "
        + pprint.pformat(schemas, sort_dicts=False, width=85)
        + "\n\n"
    )
    data += (
        "OPERATIONS: dict[str, Any] = "
        + pprint.pformat(operations, sort_dicts=False, width=85)
        + "\n"
    )
    fixture = {
        "source_commit": COMMIT,
        "source_sha256": SOURCE_SHA256,
        "copyright": "Bosch Thermotechnik GmbH 2026",
        "license": "Apache-2.0",
        "templates": list(operations),
        "examples": examples,
    }
    targets = {
        ROOT / "custom_components/bosch_buderus_heating/local_api/_data.py": data,
        ROOT / "tests/fixtures/local_api_examples.json": json.dumps(
            fixture, indent=2, ensure_ascii=False
        )
        + "\n",
    }
    # Formatting is deterministic and also checked by the repository's Ruff gate.
    import subprocess
    import sys

    for target, text in targets.items():
        if target.suffix == ".py":
            text = subprocess.check_output(
                [
                    sys.executable,
                    "-m",
                    "ruff",
                    "format",
                    "--stdin-filename",
                    str(target),
                    "-",
                ],
                input=text,
                text=True,
            )
        if args.check:
            assert target.read_text(encoding="utf-8") == text, target
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    print(f"Verified {len(operations)} operations and {len(examples)} examples")


if __name__ == "__main__":
    main()
