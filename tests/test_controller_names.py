"""Local-only readings carry the names and sensor codes of the controller.

The reference heat pump's controller (MX400, firmware 15.00.01) shows these
readings in its Info menu with sensor codes such as TL2 or VR4. The entities
use the same wording, so its display and Home Assistant can be compared line
by line. Only names and state texts change: the unique IDs depend on the path.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorStateClass

from custom_components.bosch_buderus_heating.local_only import (
    FLAG,
    GENERIC,
    PERCENT,
    definition_for,
    diagnostic,
    entity_name,
    for_reading,
    state_class,
)
from custom_components.bosch_buderus_heating.local_only_sensor import (
    local_only_unique_id,
)
from custom_components.bosch_buderus_heating.local_selection import (
    signal_slows_when_unused,
)

COMPONENT = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
HS = "/heatSources/hs1"
HP1 = "/signals/SRC.CUHP.HP1."

NAMES = {
    "/heatSources/additionalHeater/flowTemp": (
        "Vorlauftemperatur primär (TC1)",
        "Primary supply temperature (TC1)",
    ),
    "/heatSources/sensors/evaporatorTemp_tl2": (
        "Luftansaugtemperatur (TL2)",
        "Air intake temperature (TL2)",
    ),
    HS + "/refrigerant/liquidPipeHeatingTemp": (
        "Verflüssigertemperatur Heizen (TR3)",
        "Condenser temperature heating (TR3)",
    ),
    HS + "/refrigerant/liquidPipeCoolingTemp": (
        "Verflüssigertemperatur Kühlen (TR4)",
        "Condenser temperature cooling (TR4)",
    ),
    HS + "/refrigerant/suctionGasTemp": (
        "Temperatur Saugleitung (TR5)",
        "Suction line temperature (TR5)",
    ),
    HS + "/refrigerant/hotGasTemp": (
        "Heißgastemperatur (TR6)",
        "Hot gas temperature (TR6)",
    ),
    HS + "/refrigerant/lowPressureTemp": (
        "Niederdrucktemperatur (JR0)",
        "Low-pressure temperature (JR0)",
    ),
    HS + "/refrigerant/highPressureTemp": (
        "Hochdrucktemperatur (JR1)",
        "High-pressure temperature (JR1)",
    ),
    HS + "/refrigerant/heatCarrierPumpSpeed": (
        "Drehzahl primäre Heizungspumpe (PC0)",
        "Primary heating pump speed (PC0)",
    ),
    HS + "/oduFanSpeed": (
        "Lüfterdrehzahl Außeneinheit (PL3)",
        "Outdoor unit fan speed (PL3)",
    ),
    HP1 + "DripPanTemp": (
        "Kondensatwannentemperatur (TA4)",
        "Drip pan temperature (TA4)",
    ),
    HP1 + "DripPanHeater": ("Kondensatwannenheizung", "Drip pan heater"),
    HP1 + "ExpansionValveActual": ("Expansionsventil VR1", "Expansion valve VR1"),
    HP1 + "ReceiverValveActual": ("Expansionsventil VR0", "Expansion valve VR0"),
    HP1 + "FourwayValve": ("4-Wege-Ventil (VR4)", "4-way valve (VR4)"),
    HP1 + "DHWValve": (
        "3-Wege-Ventil Warmwasser (VW1)",
        "Hot water 3-way valve (VW1)",
    ),
    HP1 + "CHPumpRelay": (
        "Primäre Heizungspumpe (PC0)",
        "Primary heating pump (PC0)",
    ),
    "/signals/SRC.CUHP.CH.HC1PumpRelay": (
        "Heizkreispumpe HK1 (PC1)",
        "Heating circuit pump HC1 (PC1)",
    ),
    "/signals/SRC.HC1.FlowCtrl.PumpVolumeFlow": (
        "Volumenstrom HK1 (PC1)",
        "Flow rate HC1 (PC1)",
    ),
    HP1 + "Timer.CompressorRestart": (
        "Verzögerung Kompressorstart",
        "Compressor start delay",
    ),
    # Since 0.9.0 the controller's word "Kompressor" in every German name.
    "/signals/SRC.CUHP.HMI.CompressorActive": ("Kompressor", "Compressor"),
    HP1 + "CompressorStatus": ("Kompressor (Status)", "Compressor (status)"),
    HP1 + "CompressorPower": (
        "Wärmeleistung (Kompressor)",
        "Heat output (compressor)",
    ),
    HP1 + "Stats.CompressorDHW": (
        "Kompressorlaufzeit Warmwasser",
        "Compressor run time hot water",
    ),
    HS + "/refrigerant/compressorActualSpeed": (
        "Kompressordrehzahl",
        "Compressor speed",
    ),
    HS + "/refrigerant/compressorTemp": (
        "Kompressortemperatur",
        "Compressor temperature",
    ),
    "/heatSources/compressor/powerElecActual": (
        "Elektrische Leistung Kompressor",
        "Compressor electric power",
    ),
    "/signals/HYBMAN.CAN.FourWayValveActive": ("4-Wege-Ventil", "4-way valve"),
    # The flow of the primary heating pump PC0, as its second source.
    HS + "/pumpVolumeFlow": ("Volumenstrom primär", "Primary flow rate"),
    # The season signal repeats the cloud's current heating/cooling status.
    "/signals/SC.SeasonOpt.Mode": (
        "Aktueller Heiz-/Kühlstatus (Signal)",
        "Current heating/cooling status (signal)",
    ),
    "/signals/SRC.IntDHW.HeatUpActive": (
        "Warmwasserbereitung aktiv",
        "Hot water heat-up active",
    ),
}

# Second sources of readings other entities show: named, but still generic.
SECOND_SOURCES = {
    HP1 + "CompressorSpeedActual": "Compressor speed (signal)",
    HP1 + "CHPumpSpeed": "Primary heating pump speed (PC0, signal)",
    "/signals/SRC.IntFlowTemp": "Primary supply temperature (TC1, signal)",
    HP1 + "ReturnTemp": "Return temperature (TC0, signal)",
    "/signals/SRC.SystemPumpVolumeFlow": "Primary flow rate (PC0, signal)",
    "/signals/SC.HC1.FlowTempSetp": "Supply setpoint HC1 (signal)",
    "/signals/SRC.HC1.FlowCtrl.ReqFlowTemp": (
        "Requested supply temperature HC1 (signal)"
    ),
    "/signals/SRC.RelayStatus.3WayValve": "Hot water 3-way valve relay (signal)",
    "/signals/SRC.RelayStatus.CHPump": "Primary heating pump relay (signal)",
    "/signals/SRC.IntDHW.CylTemp": "Cylinder temperature (signal)",
    "/signals/SRC.OutdoorTemp": "Outdoor temperature (signal)",
}


@pytest.mark.parametrize(("path", "names"), NAMES.items())
def test_controller_names_in_both_languages(path, names):
    definition = definition_for(path)
    assert definition is not None
    assert entity_name(definition, path, "de") == names[0]
    assert entity_name(definition, path, "en-GB") == names[1]


def test_new_signals_have_a_reviewed_meaning():
    vr0 = definition_for(HP1 + "ReceiverValveActual")
    assert (vr0.kind, vr0.enabled) == (PERCENT, True)
    assert state_class(vr0) is SensorStateClass.MEASUREMENT
    delay = definition_for(HP1 + "Timer.CompressorRestart")
    # A waiting time, not a counter: generic as before, only named. It stays
    # diagnostic, gets no statistics and is read hourly while disabled (G3).
    assert (delay.kind, delay.enabled) == (GENERIC, False)
    assert diagnostic(delay)
    assert state_class(delay, "s") is None
    assert signal_slows_when_unused(HP1 + "Timer.CompressorRestart")


@pytest.mark.parametrize(
    ("path", "texts"),
    [
        # The relay of VW1 shows the valve's position like VW1 (since 0.9.0).
        ("/signals/SRC.RelayStatus.3WayValve", "dhw_valve_position"),
        ("/signals/SRC.RelayStatus.CHPump", "on_off_state"),
    ],
)
def test_named_relay_signals_stay_diagnostic(path, texts):
    # Their true/false answer makes them an on/off state; as before they stay
    # in the diagnostic section.
    answer = SimpleNamespace(
        type="stringValue", data=SimpleNamespace(value="true", allowed_values=())
    )
    resolved = for_reading(definition_for(path), answer)
    assert (resolved.kind, resolved.translation_key) == (FLAG, texts)
    assert diagnostic(resolved)


def test_a_relay_read_as_a_number_has_no_state_texts():
    # Should the relay answer with a plain number, it is a number: the valve
    # texts belong to its on/off form only.
    answer = SimpleNamespace(
        type="floatValue", data=SimpleNamespace(value=1, states=(), unit=None)
    )
    path = "/signals/SRC.RelayStatus.3WayValve"
    resolved = for_reading(definition_for(path), answer)
    assert (resolved.kind, resolved.translation_key) == (GENERIC, None)


@pytest.mark.parametrize(("path", "name"), SECOND_SOURCES.items())
def test_second_sources_stay_generic_and_disabled(path, name):
    definition = definition_for(path)
    assert definition.key != "signal"
    assert entity_name(definition, path, "en") == name
    assert (definition.kind, definition.enabled) == (GENERIC, False)
    assert diagnostic(definition)
    # Like any controller signal, a measurement unit keeps its statistics.
    assert state_class(definition, "°C") is SensorStateClass.MEASUREMENT
    assert state_class(definition, "s") is None


@pytest.mark.parametrize(
    ("path", "key", "states"),
    [
        (
            HP1 + "FourwayValve",
            "four_way_valve_position",
            {"en": ("Heating", "Cooling/defrost"), "de": ("Heizen", "Kühlen/Abtauen")},
        ),
        (
            HP1 + "DHWValve",
            "dhw_valve_position",
            {"en": ("Heating", "Hot water"), "de": ("Heizen", "Warmwasser")},
        ),
    ],
)
def test_valves_show_their_position(path, key, states):
    definition = definition_for(path)
    # The states stay on/off, so automations keep working; only the texts change.
    assert (definition.kind, definition.options) == (FLAG, ("on", "off"))
    assert definition.translation_key == key
    for name, language in (
        ("strings.json", "en"),
        ("translations/en.json", "en"),
        ("translations/de.json", "de"),
    ):
        texts = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        state = texts["entity"]["sensor"][key]["state"]
        assert (state["off"], state["on"]) == states[language]


def test_other_flags_keep_the_on_off_texts():
    definition = definition_for("/signals/SRC.CUHP.Cooling.Active")
    assert definition.translation_key == "on_off_state"


@pytest.mark.parametrize("path", [*NAMES, *SECOND_SOURCES])
def test_unique_ids_depend_on_the_path_only(path):
    # Renaming never creates a new entity: the unique ID is the path's.
    suffix = path.strip("/").replace("/", ":")
    assert local_only_unique_id("gw", path) == f"gw:local_only:{suffix}"
