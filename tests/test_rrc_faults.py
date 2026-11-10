"""W5 (CT-F01): fault texts of RRC 2.0 room controllers and the numeric
classes of a notification.

Radio devices report display code A84 with a cause code; low water pressure
comes as cause code 207, 1017 or 2971 with any display code. The texts are
worded independently and expected, untested on a CT200.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import i18n
from custom_components.bosch_buderus_heating.faults import (
    FaultSeverity,
    FaultTracker,
    fault_summary,
    parse_fault_resource,
)
from custom_components.bosch_buderus_heating.pointt import Resource
from tests.test_faults import NOW

RADIO = {
    "1869": (
        "Funkverbindung zu einem Gerät verloren",
        "Radio connection to a device lost",
    ),
    "1870": ("Batterie eines Funkgeräts schwach", "Battery of a radio device low"),
    "1871": (
        "Ventiladaption eines Heizkörperthermostats steht aus",
        "Valve adaptation of a radiator thermostat pending",
    ),
    "1872": (
        "Ventil eines Heizkörperthermostats zu schwergängig",
        "Valve of a radiator thermostat moves too stiffly",
    ),
    "1873": (
        "Stellbereich eines Heizkörperthermostats zu groß",
        "Valve travel of a radiator thermostat too large",
    ),
    "1874": (
        "Stellbereich eines Heizkörperthermostats zu klein",
        "Valve travel of a radiator thermostat too small",
    ),
    "1876": (
        "Funkgerät hat die Verbindung zum Regler verloren",
        "Radio device lost its connection to the controller",
    ),
    "1879": (
        "Ventil eines Heizkörperthermostats in Fehlstellung",
        "Valve of a radiator thermostat in a wrong position",
    ),
}


def as_value(*items: object) -> Resource:
    """P40: the controller sends the list under ``value``."""
    return Resource(path="/notifications", value=list(items), has_value=True)  # type: ignore[arg-type]


def as_values(*items: object) -> Resource:
    return Resource(path="/notifications", values=items, has_values=True)  # type: ignore[arg-type]


def only(resource: Resource):
    parsed = parse_fault_resource(resource, observed_at=NOW)
    assert parsed.invalid_entries == 0
    (fault,) = parsed.faults
    return fault


@pytest.mark.parametrize("form", [as_value, as_values])
@pytest.mark.parametrize("code", sorted(RADIO))
def test_radio_device_codes_have_texts(form, code: str) -> None:
    fault = only(form({"ccd": int(code), "dcd": "A84", "fc": 4, "dlv": 8}))
    german, english = RADIO[code]
    assert fault_summary(fault, "de") == german
    assert fault_summary(fault, "en") == english
    # A lower case display code is the same code.
    lower = only(form({"ccd": code, "dcd": "a84"}))
    assert fault_summary(lower, "en") == english


@pytest.mark.parametrize("code", ["1875", "1877", "1878", "1880"])
def test_other_radio_device_codes_fall_back_to_a_radio_text(code: str) -> None:
    fault = only(as_value({"ccd": code, "dcd": "A84"}))
    assert fault_summary(fault, "de") == f"Störung eines Funkgeräts (Code {code})"
    assert fault_summary(fault, "en") == f"Radio device fault (code {code})"
    # The radio texts belong to A84 only.
    other = only(as_value({"ccd": "1869", "dcd": "A11"}))
    assert fault_summary(other, "en") == "Unknown fault (code 1869)"


@pytest.mark.parametrize("code", [207, 1017, 2971])
@pytest.mark.parametrize("display", ["H07", "A01", "", None])
def test_low_water_pressure_with_any_display_code(code: int, display) -> None:
    fault = only(as_value({"ccd": code, "dcd": display}))
    assert fault_summary(fault, "de") == "Wasserdruck zu niedrig"
    assert fault_summary(fault, "en-GB") == "Water pressure too low"


def test_a_language_without_a_translation_gets_english() -> None:
    fault = only(as_value({"ccd": 1870, "dcd": "A84"}))
    expected = i18n.CATALOGS["da"].get(
        "Battery of a radio device low", "Battery of a radio device low"
    )
    assert fault_summary(fault, "da") == expected


@pytest.mark.parametrize(
    ("item", "pointt_class", "severity"),
    [
        # The numeric classes: maintenance, and a fault at display level 8.
        ({"fc": 18, "dlv": 8}, "maintenance", FaultSeverity.MAINTENANCE),
        ({"fc": "20", "dlv": "4"}, "maintenance", FaultSeverity.MAINTENANCE),
        ({"fc": "4", "dlv": "8"}, "fault", FaultSeverity.FAULT),
        ({"fc": 17.0, "dlv": 8}, "fault", FaultSeverity.FAULT),
        # Anything else stays unknown and therefore a problem.
        ({"fc": "4", "dlv": "4"}, None, FaultSeverity.UNKNOWN),
        ({"fc": "19", "dlv": "8"}, None, FaultSeverity.UNKNOWN),
        ({"fc": "4"}, None, FaultSeverity.UNKNOWN),
        ({"fc": "-4", "dlv": "8"}, None, FaultSeverity.UNKNOWN),
        ({"fc": "4.5", "dlv": "8"}, None, FaultSeverity.UNKNOWN),
        # Known classes keep their meaning.
        ({"fc": "12", "dlv": "4"}, "blocking", FaultSeverity.FAULT),
        ({"fc": "WARNING", "dlv": "8"}, "warning", FaultSeverity.WARNING),
    ],
)
def test_numeric_classes_of_a_notification(item, pointt_class, severity) -> None:
    fault = only(as_value({"ccd": 1870, "dcd": "A84", **item}))
    assert fault.pointt_class == pointt_class
    assert fault.severity is severity


def test_a_maintenance_request_is_no_fault(hass: HomeAssistant) -> None:
    tracker = FaultTracker(hass, "entry", "gateway")
    notifications = as_value(
        {"ccd": 1870, "dcd": "A84", "fc": 18, "dlv": 8},
        {"ccd": 207, "dcd": "H07", "fc": 4, "dlv": 8},
    )
    tracker.process_resources({notifications.path: notifications})
    assert len(tracker.active) == 2
    (fault,) = tracker.active_faults
    assert fault.code == "207"
    assert tracker.highest_severity is FaultSeverity.FAULT
