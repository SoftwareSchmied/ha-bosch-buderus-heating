"""Compare a new local API specification with the reviewed, pinned one (P44).

The integration only ever uses the pinned facts in ``local_api/_data.py``. This
tool reads a candidate specification with the same rules and lists what changed,
sorted by what it means for the integration:

- ``compatible``: additions that keep every known contract, such as new paths,
  new optional fields or new example variants;
- ``adapt``: a known contract changed (type, format, unit, allowed values,
  limits, markers, parameters, sign-in, a field that is no longer guaranteed,
  anything the extraction does not know); the adapters and tests need a
  review before the pin is moved;
- ``breaking``: a known path, schema or guaranteed field disappeared.

Each change names the affected read templates and whether the integration
shows (paired entity) or reads (polling plan) them. Nothing is changed, loaded
at runtime, reported or published; moving the pin stays a deliberate, reviewed
step with ``scripts/update_local_api_catalog.py``.

Only the read contract is compared: the successful (200) JSON response, the
parameters, the examples, the sign-in scheme and the server. Error responses
and descriptions are not compared.

Usage::

    python -m scripts.compare_local_api_spec path/to/k-40-rf.yaml [--json]

Exit code 0: nothing or only compatible changes; 3: changes need adaptation;
4: something breaks. A crash exits with 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # also when started as a file
    sys.path.insert(0, str(ROOT))

EXAMPLES = ROOT / "tests/fixtures/local_api_examples.json"
_MISSING = object()
_UNIT_KEYS = {"unitOfMeasure", "unit"}
LEVELS = ("breaking", "adapt", "compatible")
EXIT_CODES = {"breaking": 4, "adapt": 3}
TEMPERATURE_RANGE = (-60, 150)
GENERAL_MARKERS = {-32768, 32767}
# Reviewed sign-in and server of the pinned specification (without texts).
PINNED_ACCESS = {
    "security": [{"bearerAuth": []}],
    "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
    "servers": ["https://{gatewayAddress}:9443"],
}


@dataclass(frozen=True, slots=True)
class Change:
    level: str
    area: str
    where: str
    detail: str
    templates: tuple[str, ...] = field(default=())
    used: tuple[str, ...] = field(default=())


@dataclass(slots=True)
class Facts:
    schemas: dict[str, Any]
    operations: dict[str, Any]
    examples: list[dict[str, Any]]
    access: dict[str, Any] = field(
        default_factory=lambda: json.loads(json.dumps(PINNED_ACCESS))
    )


# ------------------------------------------------------------------- loading


def pinned_facts() -> Facts:
    """The reviewed facts the integration runs with."""
    from custom_components.bosch_buderus_heating.local_api import _data

    examples = json.loads(EXAMPLES.read_text(encoding="utf-8"))["examples"]
    return Facts(dict(_data.SCHEMAS), dict(_data.OPERATIONS), examples)


def _access(spec: Mapping[str, Any]) -> dict[str, Any]:
    schemes = (spec.get("components") or {}).get("securitySchemes") or {}
    return {
        "security": spec.get("security"),
        "securitySchemes": {
            name: (
                {k: v for k, v in scheme.items() if k != "description"}
                if isinstance(scheme, dict)
                else scheme
            )
            for name, scheme in schemes.items()
        },
        "servers": [
            server.get("url") if isinstance(server, dict) else server
            for server in spec.get("servers") or ()
        ],
    }


def candidate_facts(spec: Mapping[str, Any]) -> tuple[Facts, list[str]]:
    """Facts of a new specification and what the extraction does not know."""
    from scripts.update_local_api_catalog import extract

    unknown: list[str] = []
    schemas, operations, examples = extract(spec, unknown)
    return Facts(schemas, operations, examples, _access(spec)), sorted(set(unknown))


def used_templates() -> dict[str, str]:
    """Templates the integration shows (paired entity) or reads (polling plan)."""
    from custom_components.bosch_buderus_heating.local_api.catalog import (
        request_for_path,
    )
    from custom_components.bosch_buderus_heating.local_polling._data import (
        ANCHORS,
        CLASSES,
    )
    from custom_components.bosch_buderus_heating.local_polling.policy import (
        PERIODS,
        PollClass,
    )
    from custom_components.bosch_buderus_heating.local_probe import ProbePath
    from custom_components.bosch_buderus_heating.observations import PAIR_CATALOG

    used = {
        template: "read"
        for template, poll_class in CLASSES.items()
        if PollClass(poll_class) in PERIODS
    }
    for path in (*ANCHORS, *(item.value for item in ProbePath)):
        used[request_for_path(path).definition.template] = "read"
    for pair in PAIR_CATALOG:
        path = (
            pair.template.replace("{hc}", "hc1")
            .replace("{dhw}", "dhw1")
            .replace("{hs}", "hs1")
        )
        used[request_for_path(path).definition.template] = "shown"
    return used


# ------------------------------------------------------- example semantics


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _units(value: Any) -> list[str]:
    if isinstance(value, dict):
        found = [
            child
            for key, child in value.items()
            if key in _UNIT_KEYS and isinstance(child, str)
        ]
        for key, child in value.items():
            if key not in _UNIT_KEYS:
                found += _units(child)
        return found
    if isinstance(value, list):
        return [unit for child in value for unit in _units(child)]
    return []


def _states(response: Mapping[str, Any]) -> set[float]:
    return {
        number
        for state in response.get("state") or ()
        if isinstance(state, dict)
        for number in state.values()
        if _number(number)
    }


def example_deviations(examples: Iterable[Mapping[str, Any]]) -> set[tuple[str, ...]]:
    """Examples that contradict their meaning: (kind, template, detail).

    Kinds: ``unit`` (unknown or misspelled unit), ``allowed_value`` (value
    outside its own allowed values), ``limit`` (value outside its own limits),
    ``temperature`` (``tenth_of_marker``, ``undeclared_marker`` or
    ``implausible``).
    """
    from custom_components.bosch_buderus_heating.local_api.decoder import _UNITS

    found: set[tuple[str, ...]] = set()
    for row in examples:
        template, response = row["template"], row["response"]
        if not isinstance(response, dict):
            continue
        found |= {
            ("unit", template, unit) for unit in _units(response) if unit not in _UNITS
        }
        value = response.get("value")
        allowed = response.get("allowedValues")
        if isinstance(value, str) and allowed and value not in allowed:
            found.add(("allowed_value", template, value))
        low, high = response.get("minValue"), response.get("maxValue")
        states = _states(response)
        if (
            _number(value)
            and _number(low)
            and _number(high)
            and not low <= value <= high
            and value not in states
        ):
            found.add(("limit", template, str(value)))
        if response.get("unitOfMeasure") not in {"C", "°C"} or not _number(value):
            continue
        if TEMPERATURE_RANGE[0] <= value <= TEMPERATURE_RANGE[1] or value in states:
            continue
        if any(value == state / 10 for state in states):
            found.add(("temperature", template, "tenth_of_marker"))
        elif value in GENERAL_MARKERS:
            found.add(("temperature", template, "undeclared_marker"))
        else:
            found.add(("temperature", template, "implausible"))
    return found


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if _number(value):
        return "number"
    return type(value).__name__


def _example_facts(responses: Iterable[Any]) -> dict[str, set[str]]:
    """What the examples of one template state, regardless of example names."""
    facts: dict[str, set[str]] = {
        "unit": set(),
        "type": set(),
        "value kind": set(),
        "allowed value": set(),
        "state": set(),
        "limit": set(),
    }
    for response in responses:
        if not isinstance(response, dict):
            facts["value kind"].add(_kind(response))
            continue
        facts["unit"] |= {json.dumps(unit) for unit in _units(response)}
        if "type" in response:
            facts["type"].add(json.dumps(response["type"]))
        if "value" in response:
            facts["value kind"].add(_kind(response["value"]))
        for allowed in response.get("allowedValues") or ():
            facts["allowed value"].add(json.dumps(allowed))
        for state in response.get("state") or ():
            if isinstance(state, dict):
                facts["state"] |= {
                    json.dumps({k: v}, sort_keys=True) for k, v in state.items()
                }
        for key in ("minValue", "maxValue", "stepSize"):
            if key in response:
                facts["limit"].add(f"{key}={json.dumps(response[key])}")
    return facts


# --------------------------------------------------------------- comparison


def _refs(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = set()
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str):
                found.add(child.rsplit("/", 1)[-1])
            else:
                found |= _refs(child)
        return found
    if isinstance(value, list):
        return set().union(*(_refs(child) for child in value)) if value else set()
    return set()


def _closure(facts: Facts, start: set[str]) -> set[str]:
    direct = {name: _refs(schema) for name, schema in facts.schemas.items()}
    pending, seen = list(start), set()
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending.extend(direct.get(name, ()))
    return seen


def _users(facts: Facts) -> tuple[dict[str, set[str]], set[str], set[str]]:
    """Templates using each schema; schemas used by parameters and responses."""
    users: dict[str, set[str]] = {name: set() for name in facts.schemas}
    in_responses: set[str] = set()
    in_parameters: set[str] = set()
    for template, operation in facts.operations.items():
        response = _closure(facts, _refs(operation["response"]))
        parameters = _closure(facts, _refs(operation["parameters"]))
        in_responses |= response
        in_parameters |= parameters
        for name in response | parameters:
            users.setdefault(name, set()).add(template)
    return users, in_parameters, in_responses


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _shown(value: Any) -> str:
    return "missing" if value is _MISSING else json.dumps(value)


def _diff_schema(
    old: Any,
    new: Any,
    *,
    parameter: bool = False,
    response: bool = True,
    trail: str = "",
) -> Iterable[tuple[str, str]]:
    """Yield (level, detail) for one schema; ``trail`` names the nested place.

    ``parameter``/``response`` say where the schema is used: a new allowed
    value needs adaptation in a response, a removed one in a parameter.
    """
    if not isinstance(old, dict) or not isinstance(new, dict):
        if old != new:
            yield "adapt", f"{trail or 'schema'}: {_shown(old)} -> {_shown(new)}"
        return
    prefix = f"{trail}." if trail else ""
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key, _MISSING), new.get(key, _MISSING)
        if before == after:
            continue
        place = f"{prefix}{key}"
        if key == "properties":
            before = before if isinstance(before, dict) else {}
            after = after if isinstance(after, dict) else {}
            guaranteed = set(_as_list(old.get("required")))
            for name in sorted(set(before) | set(after)):
                if name not in before:
                    yield "compatible", f"field {prefix}{name} added"
                elif name not in after:
                    yield (
                        "breaking" if name in guaranteed else "adapt",
                        f"field {prefix}{name} removed",
                    )
                else:
                    yield from _diff_schema(
                        before[name],
                        after[name],
                        parameter=parameter,
                        response=response,
                        trail=f"{prefix}{name}",
                    )
        elif key == "required":
            was, now = set(_as_list(before)), set(_as_list(after))
            for name in sorted(was - now):
                yield "adapt", f"field {prefix}{name} no longer required"
            for name in sorted(now - was):
                yield "compatible", f"field {prefix}{name} now required"
        elif key == "enum" and isinstance(before, list) and isinstance(after, list):
            added = [v for v in after if v not in before]
            removed = [v for v in before if v not in after]
            if added:
                yield (
                    "adapt" if response else "compatible",
                    f"{place}: values added {added}",
                )
            if removed:
                yield (
                    "adapt" if parameter else "compatible",
                    f"{place}: values removed {removed}",
                )
        elif (
            key in {"allOf", "oneOf", "anyOf"}
            and isinstance(before, list)
            and isinstance(after, list)
            and len(before) == len(after)
        ):
            for index, (a, b) in enumerate(zip(before, after, strict=True)):
                yield from _diff_schema(
                    a,
                    b,
                    parameter=parameter,
                    response=response,
                    trail=f"{place}[{index}]",
                )
        elif key in {"items", "additionalProperties"} and (
            isinstance(before, dict) and isinstance(after, dict)
        ):
            yield from _diff_schema(
                before, after, parameter=parameter, response=response, trail=place
            )
        else:
            yield "adapt", f"{place}: {_shown(before)} -> {_shown(after)}"


def _shape(template: str) -> str:
    """A template without its parameter names: renaming them changes nothing."""
    return re.sub(r"\{[^}]*\}", "{}", template)


def compare(
    old: Facts, new: Facts, used: Mapping[str, str] | None = None
) -> list[Change]:
    """All differences between the pinned and a candidate specification."""
    used = used or {}
    changes: list[Change] = []

    def add(level, area, where, detail, templates=()):
        templates = tuple(sorted(templates))
        changes.append(
            Change(
                level,
                area,
                where,
                detail,
                templates,
                tuple(sorted({used[t] for t in templates if t in used})),
            )
        )

    removed = set(old.operations) - set(new.operations)
    added = set(new.operations) - set(old.operations)
    renamed: dict[str, str] = {}
    for template in sorted(removed):
        matches = [t for t in added if _shape(t) == _shape(template)]
        if len(matches) == 1:
            renamed[template] = matches[0]
            added.discard(matches[0])
    for template in sorted(removed - set(renamed)):
        add("breaking", "operation", template, "path removed", (template,))
    for template in sorted(added):
        add(
            "compatible",
            "operation",
            template,
            "path added; needs a polling class before it can be read",
            (template,),
        )
    for template, target in sorted(renamed.items()):
        add(
            "adapt",
            "operation",
            template,
            f"path parameters renamed to {target}; update the catalog references",
            (template,),
        )
    pairs = [(t, t) for t in sorted(set(old.operations) & set(new.operations))]
    for template, target in pairs + sorted(renamed.items()):
        before, after = old.operations[template], new.operations[target]
        if before.get("group") != after.get("group"):
            add("compatible", "operation", template, "group changed", (template,))
        for key in ("deprecated", "security"):
            if before.get(key) != after.get(key):
                add(
                    "adapt",
                    "operation",
                    template,
                    f"{key}: {json.dumps(before.get(key))} -> "
                    f"{json.dumps(after.get(key))}",
                    (template,),
                )
        _compare_parameters(add, template, before, after, template != target)
        for level, detail in _diff_schema(before["response"], after["response"]):
            add(level, "response", template, detail, (template,))

    users, in_parameters, in_responses = _users(old)
    for name in sorted(set(old.schemas) - set(new.schemas)):
        add("breaking", "schema", name, "schema removed", users.get(name, ()))
    for name in sorted(set(new.schemas) - set(old.schemas)):
        add("compatible", "schema", name, "schema added")
    for name in sorted(set(old.schemas) & set(new.schemas)):
        for level, detail in _diff_schema(
            old.schemas[name],
            new.schemas[name],
            parameter=name in in_parameters,
            response=name in in_responses or name not in in_parameters,
        ):
            add(level, "schema", name, detail, users.get(name, ()))

    _compare_examples(add, old, new, renamed)

    for key in sorted(set(old.access) | set(new.access)):
        if old.access.get(key) != new.access.get(key):
            add(
                "adapt",
                "sign-in",
                key,
                f"{json.dumps(old.access.get(key))} -> "
                f"{json.dumps(new.access.get(key))}; check the sign-in checklist",
            )

    order = {level: index for index, level in enumerate(LEVELS)}
    return sorted(
        changes, key=lambda c: (order[c.level], not c.used, c.where, c.detail)
    )


def _keyed_parameters(parameters, renamed_path: bool) -> dict[tuple, dict]:
    """Parameters by name and place; after a path rename, path ones by position."""
    result: dict[tuple, dict] = {}
    position = 0
    for parameter in parameters:
        if renamed_path and parameter.get("in") == "path":
            position += 1
            result[(f"#{position}", "path")] = parameter
        else:
            result[(parameter.get("name"), parameter.get("in"))] = parameter
    return result


def _compare_parameters(add, template, before, after, renamed_path) -> None:
    params_before = _keyed_parameters(before["parameters"], renamed_path)
    params_after = _keyed_parameters(after["parameters"], renamed_path)
    for item in sorted(set(params_before) | set(params_after), key=str):
        name = f"parameter {item[0]} ({item[1]})"
        if item not in params_before:
            level = "adapt" if params_after[item].get("required") else "compatible"
            add(level, "parameter", template, f"{name} added", (template,))
            continue
        if item not in params_after:
            add("adapt", "parameter", template, f"{name} removed", (template,))
            continue
        a, b = params_before[item], params_after[item]
        if bool(a.get("required")) != bool(b.get("required")):
            add(
                "adapt" if b.get("required") else "compatible",
                "parameter",
                template,
                f"{name} now {'required' if b.get('required') else 'optional'}",
                (template,),
            )
        for extra in ("style", "explode", "allowEmptyValue", "deprecated"):
            if a.get(extra) != b.get(extra):
                add(
                    "adapt",
                    "parameter",
                    template,
                    f"{name}: {extra} {json.dumps(a.get(extra))} -> "
                    f"{json.dumps(b.get(extra))}",
                    (template,),
                )
        for level, detail in _diff_schema(
            a["schema"], b["schema"], parameter=True, response=False
        ):
            add(level, "parameter", template, f"{name}: {detail}", (template,))


def _compare_examples(add, old: Facts, new: Facts, renamed) -> None:
    target_to_old = {target: template for template, target in renamed.items()}

    def grouped(examples, rename):
        result: dict[str, dict[str, Any]] = {}
        for row in examples:
            template = rename.get(row["template"], row["template"])
            result.setdefault(template, {})[row["name"]] = row["response"]
        return result

    before = grouped(old.examples, {})
    after = grouped(new.examples, target_to_old)
    for template in sorted(set(before) | set(after)):
        a, b = before.get(template, {}), after.get(template, {})
        for name in sorted(set(b) - set(a)):
            add("compatible", "example", template, f"example {name} added", (template,))
        for name in sorted(set(a) - set(b)):
            add(
                "compatible",
                "example",
                template,
                f"example {name} removed",
                (template,),
            )
        if not a or not b:
            continue
        # Compared per template, so a renamed example still shows its changes.
        facts_a, facts_b = _example_facts(a.values()), _example_facts(b.values())
        for fact in facts_a:
            gone = sorted(facts_a[fact] - facts_b[fact])
            new_values = sorted(facts_b[fact] - facts_a[fact])
            if not gone and not new_values:
                continue
            # Fewer documented allowed values keep every known one valid.
            level = (
                "compatible" if fact == "allowed value" and not new_values else "adapt"
            )
            add(
                level,
                "example",
                template,
                f"{fact}: {', '.join(gone) or '-'} -> {', '.join(new_values) or '-'}",
                (template,),
            )
    renamed_rows = (
        dict(row, template=target_to_old.get(row["template"], row["template"]))
        for row in new.examples
    )
    for kind, template, detail in sorted(
        example_deviations(renamed_rows) - example_deviations(old.examples)
    ):
        add(
            # On a new path it only matters once that path gets a contract.
            "adapt" if template in old.operations else "compatible",
            "example",
            template,
            f"example contradicts its meaning ({kind}: {detail})",
            (template,),
        )


# ------------------------------------------------------------------- report


def summary(changes: Iterable[Change]) -> dict[str, int]:
    changes = list(changes)
    result = {level: sum(c.level == level for c in changes) for level in LEVELS}
    result["affecting_used_templates"] = sum(
        bool(c.used) for c in changes if c.level != "compatible"
    )
    return result


def exit_code(changes: Iterable[Change]) -> int:
    levels = {c.level for c in changes}
    return max(
        (EXIT_CODES[level] for level in levels if level in EXIT_CODES), default=0
    )


def render(changes: list[Change], header: Mapping[str, Any]) -> str:
    lines = ["# Local API specification comparison", ""]
    lines += [f"- {key}: {value}" for key, value in header.items()]
    counts = summary(changes)
    lines += [
        f"- breaking: {counts['breaking']}, adapt: {counts['adapt']}, "
        f"compatible: {counts['compatible']} "
        f"({counts['affecting_used_templates']} non-compatible changes affect "
        "templates the integration shows or reads)",
        "",
    ]
    if not changes:
        lines.append("No differences to the pinned specification.")
    for level in LEVELS:
        selected = [c for c in changes if c.level == level]
        if not selected:
            continue
        lines += [f"## {level}", "", "| Where | Change | Used | Templates |"]
        lines.append("| --- | --- | --- | --- |")
        for c in selected:
            templates = ", ".join(c.templates[:5]) + (
                f" (+{len(c.templates) - 5})" if len(c.templates) > 5 else ""
            )
            lines.append(
                f"| {c.area} `{c.where}` | {c.detail} | "
                f"{', '.join(c.used) or '-'} | {templates or '-'} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def run(spec: Mapping[str, Any], raw: bytes) -> tuple[list[Change], dict[str, Any]]:
    """Compare one candidate document; also used by the tests."""
    from custom_components.bosch_buderus_heating.local_api._data import (
        SOURCE_COMMIT,
        SOURCE_SHA256,
    )

    new, unknown = candidate_facts(spec)
    changes = compare(pinned_facts(), new, used_templates())
    for item in unknown:
        where, _, detail = item.partition(": ")
        changes.append(Change("adapt", "extraction", where, f"unknown {detail}"))
    candidate_sha = hashlib.sha256(raw).hexdigest()
    header = {
        "pinned commit": SOURCE_COMMIT,
        "pinned sha256": SOURCE_SHA256,
        "candidate sha256": candidate_sha,
        "candidate info.version": (spec.get("info") or {}).get("version"),
        "identical to the pin": candidate_sha == SOURCE_SHA256,
    }
    return changes, header


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    import yaml

    raw = args.candidate.read_bytes()
    spec = yaml.safe_load(raw)
    if not isinstance(spec, dict):
        print("The candidate is not an OpenAPI document.", file=sys.stderr)
        return 1
    changes, header = run(spec, raw)
    if args.json:
        print(
            json.dumps(
                {
                    "header": header,
                    "summary": summary(changes),
                    "changes": [asdict(c) for c in changes],
                },
                indent=1,
                ensure_ascii=False,
            )
        )
    else:
        print(render(changes, header), end="")
    return exit_code(changes)


if __name__ == "__main__":
    raise SystemExit(main())
