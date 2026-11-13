"""P22: anonymized captures of real systems as regression references.

Each capture in tests/fixtures/contracts/captures is a discovery result of a
real installation with its firmware. The entities built from it are pinned in
the matching ``.expected.json``: a code or firmware change that alters them
shows up here and must be reviewed. Regenerate the expectation deliberately
with ``python -m tests.test_captures`` after such a review.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from custom_components.bosch_buderus_heating.binary_sensor import (
    build_binary_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.button import build_button_descriptions
from custom_components.bosch_buderus_heating.compatibility import (
    incompatible_capabilities,
)
from custom_components.bosch_buderus_heating.discovery_inventory import (
    resource_from_data,
)
from custom_components.bosch_buderus_heating.number import build_number_descriptions
from custom_components.bosch_buderus_heating.select import build_select_descriptions
from custom_components.bosch_buderus_heating.sensor import build_sensor_descriptions
from custom_components.bosch_buderus_heating.switch import build_switch_descriptions

CAPTURES = Path(__file__).parent / "fixtures" / "contracts" / "captures"
BUILDERS = {
    "sensor": build_sensor_descriptions,
    "binary_sensor": build_binary_sensor_descriptions,
    "number": build_number_descriptions,
    "select": build_select_descriptions,
    "switch": build_switch_descriptions,
    "button": build_button_descriptions,
}


def captures() -> list[Path]:
    return sorted(
        p for p in CAPTURES.glob("*.json") if not p.name.endswith(".expected.json")
    )


def load(path: Path) -> tuple[dict, dict]:
    capture = json.loads(path.read_text(encoding="utf-8"))
    resources = {
        key: resource_from_data(value) for key, value in capture["resources"].items()
    }
    return capture, resources


def entities(resources: dict) -> dict[str, list[str]]:
    result = {}
    for platform, build in BUILDERS.items():
        keys = sorted(
            getattr(item, "unique_key", None) or item.key for item in build(resources)
        )
        result[platform] = keys
    return result


@pytest.mark.parametrize("path", captures(), ids=lambda p: p.stem)
def test_capture_builds_the_pinned_entities(path: Path) -> None:
    capture, resources = load(path)
    assert capture["provenance"]["kind"] == "anonymized_capture"
    assert capture["provenance"]["firmware"] == capture["gateway_firmware"]
    assert capture["gateway_firmware"]
    expected = json.loads(
        path.with_name(path.stem + ".expected.json").read_text(encoding="utf-8")
    )
    assert entities(resources) == expected["entities"]


@pytest.mark.parametrize("path", captures(), ids=lambda p: p.stem)
def test_capture_matches_the_known_capability_shapes(path: Path) -> None:
    _, resources = load(path)
    assert incompatible_capabilities(resources) == ()


@pytest.mark.parametrize("path", captures(), ids=lambda p: p.stem)
def test_capture_contains_no_identifiers(path: Path) -> None:
    """The anonymization rules hold for every capture that gets committed."""
    import re

    text = path.read_text(encoding="utf-8")
    for pattern in (
        r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b",  # MAC
        r"\b(?:\d{1,3}\.){3}\d{1,3}\b",  # IPv4
        r"[\w.+-]+@[\w-]+\.[\w.]+",  # e-mail
        r"\"[0-9a-fA-F]{12,}\"",  # long hex codes
        r"\"\d{11,}\"",  # serial-number-like digits (TTN type numbers have 10)
    ):
        assert re.search(pattern, text) is None, pattern
    capture = json.loads(text)
    for path_key in ("/gateway/serialId", "/gateway/uuid"):
        value = capture["resources"].get(path_key, {}).get("value")
        assert value in (None, "redacted")


def test_captures_are_present() -> None:
    """An empty folder would otherwise skip every capture test silently."""
    assert captures()


if __name__ == "__main__":  # pragma: no cover - regenerate after a review
    for item in captures():
        _, resources = load(item)
        item.with_name(item.stem + ".expected.json").write_text(
            json.dumps({"entities": entities(resources)}, indent=1) + "\n",
            encoding="utf-8",
        )
        print("written", item.stem, file=sys.stderr)
