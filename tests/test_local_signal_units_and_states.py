"""Pressure units, hybrid volume flows and gateway state texts of local signals.

Pressure spellings are recognized as reported and never converted. A hybrid
system is detected only from evidence; its volume flows are never rescaled.
Named gateway states get texts in every language; any other state is shown
as the gateway reports it. Entity names, unique IDs and entity IDs stay.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfPressure

from custom_components.bosch_buderus_heating.diagnostics import _hybrid_volume_flow
from custom_components.bosch_buderus_heating.local_api.models import DataIssue
from custom_components.bosch_buderus_heating.local_only import (
    GENERIC,
    STATE,
    hybrid_evidence,
    native_value,
    state_class,
    volume_flow_path,
)
from custom_components.bosch_buderus_heating.local_only_sensor import (
    local_only_unique_id,
)
from custom_components.bosch_buderus_heating.local_plant_view import (
    local_reading,
    local_sensor,
)
from custom_components.bosch_buderus_heating.observations import _unit_label
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from tests.test_c7_signals import answer, entity, signal
from tests.test_local_api import read, scalar

PACKAGE = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
PRESSURE = "SRC.SystemPumpPressureDifference"
PRESSURE_UNITS = {
    "mbar": UnitOfPressure.MBAR,
    "hPa": UnitOfPressure.HPA,
    "kPa": UnitOfPressure.KPA,
    "Pa": UnitOfPressure.PA,
}


# ------------------------------------------------------------ pressure units


@pytest.mark.parametrize("unit", sorted(PRESSURE_UNITS))
def test_the_decoder_knows_the_pressure_spellings(unit) -> None:
    result = read(scalar(unitOfMeasure=unit, value=150)).data
    assert result.unit == unit
    assert DataIssue.UNKNOWN_UNIT not in result.issues
    # The diagnostics name the unit instead of "other".
    assert _unit_label(unit) == unit


@pytest.mark.parametrize("unit", sorted(PRESSURE_UNITS))
def test_a_pressure_signal_shows_its_unit_without_conversion(unit) -> None:
    observation = signal(PRESSURE, 150, unit=unit)
    sensor, reason = local_sensor(observation)
    assert sensor is not None, reason
    assert sensor.definition.kind == GENERIC and not sensor.enabled
    assert (sensor.unit, sensor.device_class) == (
        PRESSURE_UNITS[unit],
        SensorDeviceClass.PRESSURE,
    )
    # The value as reported: 150 mbar stays 150, never 0.15 bar.
    assert native_value(sensor.definition, local_reading(observation)) == 150
    assert state_class(sensor.definition, sensor.unit) is SensorStateClass.MEASUREMENT


async def test_a_pressure_entity_is_a_disabled_diagnostic_pressure(hass) -> None:
    path = f"/signals/{PRESSURE}"
    sensor = entity(hass, path, [signal(PRESSURE, 85.5, unit="mbar")])
    assert sensor.native_value == 85.5
    assert sensor.native_unit_of_measurement == UnitOfPressure.MBAR
    assert sensor.device_class is SensorDeviceClass.PRESSURE
    assert sensor.state_class is SensorStateClass.MEASUREMENT
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.entity_registry_enabled_default is False


@pytest.mark.parametrize("unit", ["mBar", "psi", "mmH2O", "MPa"])
def test_other_pressure_spellings_stay_without_a_unit(unit) -> None:
    observation = signal(PRESSURE, 150, unit=unit)
    assert observation.data.issues == (DataIssue.UNKNOWN_UNIT,)
    sensor, _ = local_sensor(observation)
    assert sensor is not None
    assert (sensor.unit, sensor.device_class) == (None, None)
    assert _unit_label(unit) == "other"


# --------------------------------------------------------- hybrid volume flow


def test_hybrid_evidence_names_its_reasons() -> None:
    assert hybrid_evidence([]) == ()
    assert hybrid_evidence([("/signals/HYBMAN.PumpOn", "true")]) == (
        "hybrid_manager_signals",
    )
    assert hybrid_evidence([("/heatSources/hybrid/activeHeatSource", None)]) == (
        "hybrid_resources",
    )
    for value in ("eHybrid", "hybman", "hybridBoiler", "hybrid"):
        assert hybrid_evidence([("/system/type", value)]) == ("system_type",)
    for path in ("/heatSources/type", "/heatSources/hs1/type"):
        assert hybrid_evidence([(path, "Hybrid")]) == ("heat_source_type",)
    assert hybrid_evidence(
        [
            ("/system/type", "eHybrid"),
            ("/signals/HYBMAN.PumpOn", "true"),
            ("/signals/HYBMAN.CAN.DefrostActive", "false"),
        ]
    ) == ("hybrid_manager_signals", "system_type")


@pytest.mark.parametrize(
    "readings",
    [
        # The reference heat pump: a heat pump type, no hybrid manager.
        [
            ("/system/type", "heatpump_single"),
            ("/heatSources/type", "Heatpump"),
            ("/heatSources/hs1/pumpVolumeFlow", 800),
            ("/signals/SRC.SystemPumpVolumeFlow", 65535),
        ],
        # A type without a value, a boiler, a product name: no evidence.
        [("/system/type", None), ("/heatSources/type", "GasBoiler")],
        [("/system/info", "Hybrid heat pump"), ("/signals/SRC.HybridName", "x")],
        [("/heatSources/hybridity", "hybrid")],
    ],
)
def test_no_hybrid_without_evidence(readings) -> None:
    assert hybrid_evidence(readings) == ()


def test_volume_flow_readings() -> None:
    for path in (
        "/heatSources/hs1/pumpVolumeFlow",
        "/signals/SRC.SystemPumpVolumeFlow",
        "/signals/SRC.HC1.FlowCtrl.PumpVolumeFlow",
        "/dhwCircuits/dhw1/volumeFlow",
    ):
        assert volume_flow_path(path), path
    for path in (
        "/signals/SRC.CUHP.CH.HeatingSystemVolumeFlowLow",
        "/heatSources/hs1/flowTemp",
    ):
        assert not volume_flow_path(path), path


def test_a_hybrid_volume_flow_is_never_rescaled() -> None:
    # The documented example of a volume flow: 2 l/h stays 2 l/h, also next
    # to a hybrid manager's signals.
    observation = answer(
        "/heatSources/hs1/pumpVolumeFlow", 2, unit="l/h", kind="integerValue"
    )
    sensor, _ = local_sensor(observation)
    assert sensor is not None
    assert native_value(sensor.definition, local_reading(observation)) == 2


def _runtime(bindings, sources=None, local=None):
    state = SimpleNamespace(
        snapshot=SimpleNamespace(bindings=bindings),
        source_data=lambda key: (sources or {}).get(key),
        local_state=lambda key: (local or {}).get(key),
    )
    return SimpleNamespace(connections=state)


def _binding(target, adapter):
    return SimpleNamespace(target_id=target, adapter=adapter)


def test_diagnostics_note_a_hybrid_from_local_readings() -> None:
    observations = [
        answer("/signals/HYBMAN.PumpOn", "true"),
        answer("/heatSources/hs1/pumpVolumeFlow", 2, unit="l/h", kind="integerValue"),
        answer("/signals/SRC.SystemPumpVolumeFlow", 40, unit="l/h"),
    ]
    runtime = _runtime(
        {"local": _binding("plant", "k40_local"), "other": _binding("x", "pointt")},
        local={"local": SimpleNamespace(current_observations=observations)},
    )
    report = _hybrid_volume_flow(runtime, {"plant": 1, "x": 2})
    assert report == [
        {
            "target": 1,
            "hybrid_detected": True,
            "evidence": ["hybrid_manager_signals"],
            "volume_flow_readings": 2,
            "volume_flow": "unverified",
        }
    ]
    serialized = json.dumps(report)
    assert "/" not in serialized and "HYBMAN" not in serialized


async def test_diagnostics_note_a_hybrid_from_the_cloud(hass) -> None:
    from tests.test_validity import _coordinator

    def resource(path, value, unit=None, kind="stringValue"):
        return Resource(
            path,
            value=value,
            has_value=True,
            metadata=ResourceMetadata(resource_type=kind, unit=unit),
        )

    hybrid = _coordinator(
        hass,
        resource("/system/type", "eHybrid"),
        resource("/heatSources/hs1/pumpVolumeFlow", 2.0, "l/h", "floatValue"),
    )
    heat_pump = _coordinator(
        hass,
        resource("/system/type", "heatpump_single"),
        resource("/heatSources/hs1/pumpVolumeFlow", 800.0, "l/h", "floatValue"),
    )
    runtime = _runtime(
        {"a": _binding("one", "pointt"), "b": _binding("two", "pointt")},
        sources={"a": hybrid, "b": heat_pump},
    )
    report = _hybrid_volume_flow(runtime, {"one": 1, "two": 2, "three": 3})
    assert report == [
        {
            "target": 1,
            "hybrid_detected": True,
            "evidence": ["system_type"],
            "volume_flow_readings": 1,
            "volume_flow": "unverified",
        },
        {
            "target": 2,
            "hybrid_detected": False,
            "evidence": [],
            "volume_flow_readings": 1,
            "volume_flow": "as_reported",
        },
    ]
    # The value itself is never touched.
    assert hybrid.data["/heatSources/hs1/pumpVolumeFlow"].resource.value == 2.0
    await hybrid.async_shutdown()
    await heat_pump.async_shutdown()


# ------------------------------------------------------ gateway state texts

GATEWAY_STATES = {
    "GWEEBUS.Status": (
        "signal_eebus_status",
        (
            "NO_UPDATESERVER_CONNECTION",
            "POWER_LIMITATION_ACTIVE",
            "NO_EEBUS_CONNECTION",
            "OHPCF_ACTIVE",
            "NOT_COMMISSIONED",
            "NO_EEBUS_NO_USECASE_ACTIVE",
            "NO_EEBUS_NO_TRUST",
            "NO_HP_CONNECTION",
            "NO_LAN_CONNECTION",
            "INTERNAL_ERROR",
            "OK",
        ),
    ),
    "GWEEBUS.CommissioningStatus": (
        "signal_eebus_commissioning",
        ("COMMISSIONED", "NOT_COMMISSIONED"),
    ),
    "SRC.CUHP.StartUpCheck": (
        "signal_startup_check",
        ("STARTUP_COMPLETED", "STARTUP_INPROGRESS"),
    ),
}


def _languages() -> dict[str, dict]:
    files = {"strings": PACKAGE / "strings.json"}
    files.update(
        {path.stem: path for path in (PACKAGE / "translations").glob("*.json")}
    )
    return {
        name: json.loads(path.read_text("utf-8"))["entity"]["sensor"]
        for name, path in files.items()
    }


@pytest.mark.parametrize("name", sorted(GATEWAY_STATES))
@pytest.mark.parametrize("form", ["text", "map"])
def test_named_gateway_states_get_their_texts(name, form) -> None:
    key, declared = GATEWAY_STATES[name]
    if form == "text":
        observation = signal(name, declared[0], allowed=list(declared))
    else:
        observation = signal(
            name, 0, state={state: number for number, state in enumerate(declared)}
        )
    sensor, value = local_sensor(observation)[0], None
    assert sensor is not None
    value = native_value(sensor.definition, local_reading(observation))
    assert sensor.definition.kind == STATE
    assert sensor.definition.translation_key == key
    assert value == declared[0].lower()
    # Still the generic signal: disabled, diagnostic, the same unique ID.
    assert sensor.definition.key == "signal" and not sensor.enabled
    assert sensor.unique_suffix == f"local_only:signals:{name}"


def test_gateway_state_texts_in_every_language() -> None:
    languages = _languages()
    assert len(languages) == 20
    for key, declared in GATEWAY_STATES.values():
        expected = {state.lower() for state in declared}
        for language, sensors in languages.items():
            texts = sensors[key]["state"]
            assert set(texts) == expected, (language, key)
            assert all(text.strip() for text in texts.values()), (language, key)
            assert sensors[key]["name"].strip(), (language, key)
            # Brand-neutral texts.
            assert not any(
                brand in text.casefold()
                for text in texts.values()
                for brand in ("bosch", "buderus")
            ), (language, key)


def test_an_unknown_gateway_state_stays_as_reported() -> None:
    declared = ["OK", "NOT_COMMISSIONED", "FUTURE_STATE"]
    observation = signal("GWEEBUS.Status", "FUTURE_STATE", allowed=declared)
    sensor, _ = local_sensor(observation)
    assert sensor is not None
    assert sensor.definition.translation_key == "signal_eebus_status"
    assert "future_state" in sensor.options
    # Shown by its own key: no text exists, so the frontend shows the key.
    assert native_value(sensor.definition, local_reading(observation)) == (
        "future_state"
    )
    texts = _languages()["strings"]["signal_eebus_status"]["state"]
    assert "future_state" not in texts


def test_other_signals_keep_their_texts() -> None:
    # The holiday slot still finds its texts by its states; a state set
    # without texts gets none.
    sensor, _ = local_sensor(
        signal("SC.HolidayNew.1.State", 1, state={"IDLE": 0, "PLANNED": 1})
    )
    assert sensor.definition.translation_key is None
    sensor, _ = local_sensor(
        signal("SC.HC1.HolidayMode", "NONE", allowed=["NONE", "FIX_TEMP", "OFF"])
    )
    assert sensor.definition.translation_key is None
    # The same states under another path: no gateway texts.
    sensor, _ = local_sensor(
        signal("SRC.Other.Status", "OK", allowed=["OK", "INTERNAL_ERROR"])
    )
    assert sensor.definition.translation_key is None


async def test_gateway_state_entities_keep_their_entity_id(hass) -> None:
    from homeassistant.helpers import entity_registry as er
    from homeassistant.setup import async_setup_component

    from tests.ha_test_helpers import add_without_device

    assert await async_setup_component(hass, "sensor", {})
    path = "/signals/GWEEBUS.Status"
    observation = signal(
        "GWEEBUS.Status", "NOT_COMMISSIONED", allowed=["OK", "NOT_COMMISSIONED"]
    )
    sensor = entity(hass, path, [observation])
    assert sensor.translation_key == "signal_eebus_status"
    # The name stays the generic signal name, so does the entity ID.
    assert sensor.name.endswith("Signal GWEEBUS.Status")
    assert sensor.unique_id == local_only_unique_id("gateway-one", path)
    await add_without_device(hass, sensor)
    await hass.async_block_till_done()
    assert sensor.entity_id.endswith("signal_gweebus_status")
    registered = er.async_get(hass).async_get(sensor.entity_id)
    assert registered is not None
    assert registered.translation_key == "signal_eebus_status"
    assert registered.unique_id == local_only_unique_id("gateway-one", path)
    assert sensor.native_value == "not_commissioned"


def test_decimal_states_are_not_affected() -> None:
    # A state map with a unit stays a number, also on a named path.
    observation = signal(
        "SRC.CUHP.StartUpCheck", 3, unit="s", state={"STARTUP_COMPLETED": 0}
    )
    sensor, _ = local_sensor(observation)
    assert sensor is not None
    assert sensor.definition.kind == GENERIC
    assert sensor.definition.translation_key is None
    assert native_value(sensor.definition, local_reading(observation)) == Decimal(3)
