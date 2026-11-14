"""0.9.2: named controller signals, low-flow binary sensors, the outdoor
unit's software version and the composed name parts as translation keys."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import i18n, name_translations, rrc
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.flow_range import (
    CIRCUIT_1,
    PRIMARY,
    BoschBuderusFlowRangeBinarySensor,
    current_state,
    flow_range_unique_id,
    track_flow_range_sensors,
)
from custom_components.bosch_buderus_heating.local_only import (
    _SIGNAL_KEYS,
    FLAG,
    GENERIC,
    STATE,
    definition_for,
    diagnostic,
    entity_name,
)
from custom_components.bosch_buderus_heating.local_only_sensor import (
    local_only_unique_id,
)
from custom_components.bosch_buderus_heating.local_plant_view import local_sensor
from custom_components.bosch_buderus_heating.outdoor_unit_software import (
    BoschBuderusOutdoorUnitSoftwareSensor,
    OutdoorUnitInputs,
    OutdoorUnits,
    choose,
    clean_version,
    from_cloud,
    from_data,
    from_local,
    outdoor_unit_software_unique_id,
)
from custom_components.bosch_buderus_heating.resource_catalog import resource_name
from custom_components.bosch_buderus_heating.rrc_energy import KEYS
from custom_components.bosch_buderus_heating.rrc_energy_statistics import (
    statistic_name,
)
from tests.test_c7_signals import shown, signal
from tests.test_cloud_low_flow import flow as cloud_flow
from tests.test_local_only import by_path, coordinator_with, number, track

COMPONENT = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
OTHER_LANGUAGES = [code for code in i18n.LANGUAGES if code not in ("en", "de")]
DASH = "\N{EN DASH}"

# Signal, English name, German name.
NAMED = (
    (
        "SC.DHW1.HolidayModeNew",
        "Hot water holiday mode",
        f"Warmwasser {DASH} Urlaubsmodus",
    ),
    ("SRC.AirPurgeModeActive", "Air purge mode active", "Entlüftungsprogramm aktiv"),
    (
        "SRC.CUHP.HP1.HeatUpSequence",
        "Compressor start-up sequence",
        f"Kompressor {DASH} Anlaufsequenz",
    ),
    *(
        (
            f"SRC.CUHP.PC{pump}ConnectedToLIN",
            f"Pump PC{pump} connected to LIN bus",
            f"Pumpe PC{pump} am LIN-Bus angeschlossen",
        )
        for pump in (0, 1, 2)
    ),
    (
        "SRC.CUHP.StartUpCheck",
        "Controller start-up check",
        f"Regler {DASH} Start-up-Prüfung",
    ),
    (
        "SRC.HC1.FlowCtrl.PumpRequest",
        "Heating circuit 1 pump request (PC1)",
        f"Heizkreis 1 {DASH} Pumpenanforderung (PC1)",
    ),
    (
        "SRC.RelativeTime",
        "Controller relative time (minute counter)",
        "Reglerzeit relativ (Minutenzähler)",
    ),
    (
        "SC.DHW1.SD.ExtraActive",
        "Hot water extra active (setpoint logic)",
        f"Warmwasser {DASH} Extra aktiv (Sollwertbildung)",
    ),
    (
        "SRC.CUHP.EM.UsedLowerFlowTempSetp",
        "Energy management lower supply setpoint",
        f"Energiemanagement {DASH} unterer Vorlaufsollwert",
    ),
    (
        "SRC.CUHP.EM.UsedUpperFlowTempSetp",
        "Energy management upper supply setpoint",
        f"Energiemanagement {DASH} oberer Vorlaufsollwert",
    ),
    (
        "SRC.CUHP.HMI.AddElectricActive",
        "Electric auxiliary heater active (display)",
        "Elektrischer Zuheizer aktiv (Anzeige)",
    ),
    (
        "SRC.CUHP.HP1.Additional.AlarmInput",
        "Auxiliary heater alarm input",
        f"Zuheizer {DASH} Alarmeingang",
    ),
    (
        "SRC.CUHP.HP1.CompressorStartTemp",
        "Compressor start temperature",
        f"Kompressor {DASH} Starttemperatur",
    ),
    (
        "SRC.CUHP.HP1.DHWHeater",
        "Hot water electric heater",
        f"Warmwasser {DASH} Elektroheizstab",
    ),
    ("SRC.CUHP.HP1.HeatingCable", "Heating cable", "Heizband"),
    (
        "SRC.CUHP.HP1.HighOutdoorTempStopCooling",
        "Cooling stopped: outdoor temperature too high",
        "Kühlen gestoppt: Außentemperatur zu hoch",
    ),
    (
        "SRC.CUHP.HP1.HighOutdoorTempStopHeating",
        "Heating stopped: outdoor temperature too high",
        "Heizen gestoppt: Außentemperatur zu hoch",
    ),
    (
        "SRC.CUHP.HP1.LowOutdoorTempStopCooling",
        "Cooling stopped: outdoor temperature too low",
        "Kühlen gestoppt: Außentemperatur zu niedrig",
    ),
    (
        "SRC.CUHP.HP1.LowOutdoorTempStopHeating",
        "Heating stopped: outdoor temperature too low",
        "Heizen gestoppt: Außentemperatur zu niedrig",
    ),
    (
        "SRC.CUHP.HP1.HighestPermittedAirTemp",
        "Outdoor temperature above permitted range (heat pump)",
        "Außentemperatur über zulässigem Bereich (Wärmepumpe)",
    ),
    (
        "SRC.CUHP.HP1.LowestPermittedAirTemp",
        "Outdoor temperature below permitted range (heat pump)",
        "Außentemperatur unter zulässigem Bereich (Wärmepumpe)",
    ),
    (
        "SRC.CUHP.HP1.MinCoolingTempSetpoint",
        "Minimum cooling supply setpoint",
        f"Kühlen {DASH} Mindest-Vorlaufsollwert",
    ),
    (
        "SRC.CUHP.HP1.Timer.TDWarmKeeping",
        "Thermal disinfection hold timer",
        f"Thermische Desinfektion {DASH} Haltezeit-Timer",
    ),
    (
        "SRC.IntDHW.CircPumpStatus",
        "Hot water circulation pump",
        f"Warmwasser {DASH} Zirkulationspumpe",
    ),
    (
        "SRC.IntDHW.ExtraActive",
        "Hot water extra active (generator)",
        f"Warmwasser {DASH} Extra aktiv (Wärmeerzeuger)",
    ),
    # Seen on a brine-to-water heat pump.
    ("SRC.CUHP.HP1.HighPressureSwitch", "High-pressure switch", "Hochdruckschalter"),
    ("SRC.CUHP.SummaryAlarm", "Summary alarm", "Sammelalarm"),
    (
        "SRC.CUHP.HMI.EVUActive",
        "Utility power block active (display)",
        "EVU-Sperre aktiv (Anzeige)",
    ),
    (
        "SRC.DigInput.ExtCutOff",
        "Digital input: external cut-off",
        "Digitaler Eingang: externe Abschaltung",
    ),
    (
        "SRC.DigInput.AirPressureSwitch",
        "Digital input: air pressure switch",
        "Digitaler Eingang: Luftdruckschalter",
    ),
    (
        "SRC.DigInput.OilTemperatureHigh",
        "Digital input: oil temperature high",
        "Digitaler Eingang: Öltemperatur hoch",
    ),
    ("SRC.CUHP.HP1.CompressorSetpoint", "Compressor setpoint", "Kompressor-Sollwert"),
    (
        "SRC.CUHP.CH.HC1PumpSpeed",
        "Heating circuit pump speed HC1 (PC1)",
        "Drehzahl Heizkreispumpe HK1 (PC1)",
    ),
    (
        "SRC.CUHP.CH.HeatingSystemVolumetricFlow",
        "Heating system flow rate",
        "Volumenstrom Heizungsanlage",
    ),
)
# Meaning open: they keep the signal path as their name.
RAW = (
    "SRC.CUHP.HP1.G3Relay",
    "SRC.IntDHW.DHURunning",
    "SC.HC1.FPD.Active",
    "SC.HC1.RTSD.PreventionEcoModeActive",
    "SC.DHW1.HolidayAutoTD",
    "SC.RTC.RelativeTimer",
    "SRC.CUHP.HP1.HighestPermittedTemp",
    "SRC.CUHP.HP1.LowestPermittedFlowTemp",
    "SRC.AutoFillingActive",
    "SC.SeasonOpt.TimerStopHeatingSeason",
    "SC.HC1.LocSeasonOpt.TimerStopHeatingSeason",
)
PERMITTED = (
    "SRC.CUHP.HP1.HighestPermittedAirTemp",
    "SRC.CUHP.HP1.LowestPermittedAirTemp",
    "SRC.CUHP.HP1.HighestPermittedTemp",
    "SRC.CUHP.HP1.LowestPermittedFlowTemp",
)


def _texts(code: str) -> dict[str, str]:
    path = COMPONENT / "i18n" / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8"))["texts"]


def _translations(code: str) -> dict:
    name = "strings.json" if code == "source" else f"translations/{code}.json"
    return json.loads((COMPONENT / name).read_text(encoding="utf-8"))


def _name(signal_name: str, language: str) -> str:
    path = f"/signals/{signal_name}"
    definition = definition_for(path)
    assert definition is not None, signal_name
    return entity_name(definition, path, language)


# ------------------------------------------------------------ the signal names


@pytest.mark.parametrize(("name", "english", "german"), NAMED)
def test_the_named_signals(name, english, german) -> None:
    assert _name(name, "en") == english
    assert _name(name, "de") == german


@pytest.mark.parametrize("code", OTHER_LANGUAGES)
def test_every_language_has_the_new_signal_names(code) -> None:
    texts = _texts(code)
    for name, english, _german in NAMED:
        assert _name(name, code) == texts[english] != english, (code, name)
    brine = "/heatSources/hs1/brineCircuit/pumpSpeed"
    definition = definition_for(brine)
    assert definition is not None
    assert entity_name(definition, brine, code) == texts["Brine pump speed"]


@pytest.mark.parametrize("name", RAW)
def test_open_signals_keep_their_path(name) -> None:
    for language in ("en", "de"):
        assert _name(name, language) == f"Signal {name}"


@pytest.mark.parametrize(("name", "_english", "_german"), NAMED)
def test_the_named_signals_stay_generic_disabled_diagnostics(
    name, _english, _german
) -> None:
    definition = definition_for(f"/signals/{name}")
    assert definition is not None
    assert definition.kind == GENERIC
    assert not definition.enabled
    assert diagnostic(definition)
    assert definition.key in _SIGNAL_KEYS
    # The unique ID comes from the path, so existing entities keep it.
    assert local_only_unique_id("gw", f"/signals/{name}") == (
        f"gw:local_only:signals:{name}"
    )


def test_the_brine_pump_speed_is_named_and_stays_generic() -> None:
    path = "/heatSources/hs1/brineCircuit/pumpSpeed"
    definition = definition_for(path)
    assert definition is not None
    assert definition.kind == GENERIC and not definition.enabled
    assert entity_name(definition, path, "en") == "Brine pump speed"
    assert entity_name(definition, path, "de") == "Drehzahl Solepumpe"
    sensor, value = shown(number(path, 55, unit="%", kind="integerValue"))
    assert value == 55
    assert sensor.unit == "%"
    assert sensor.definition.key == "brine_pump_speed"


# ------------------------------------------------------- the permitted flags


@pytest.mark.parametrize("name", PERMITTED)
@pytest.mark.parametrize(("raw", "expected"), [("true", "on"), ("false", "off")])
def test_the_permitted_signals_are_on_off_states(name, raw, expected) -> None:
    sensor, value = shown(signal(name, raw))
    assert sensor.definition.kind == FLAG
    assert sensor.device_class is SensorDeviceClass.ENUM
    assert sensor.unit is None
    assert value == expected


@pytest.mark.parametrize("name", PERMITTED)
def test_a_permitted_signal_is_never_a_temperature(name) -> None:
    """Even an answer with a number and a temperature unit stays an on/off
    state: unknown, without a unit or a temperature class."""
    sensor, value = shown(signal(name, 25, unit="C"))
    assert sensor.definition.kind == FLAG
    assert sensor.device_class is SensorDeviceClass.ENUM
    assert sensor.unit is None
    assert value is None
    # 0 and 1 are off and on.
    assert shown(signal(name, 1))[1] == "on"


def test_another_generic_signal_with_a_unit_stays_a_temperature() -> None:
    sensor, value = shown(signal("SRC.CUHP.HP1.CompressorStartTemp", 11, unit="C"))
    assert value == 11
    assert sensor.device_class is SensorDeviceClass.TEMPERATURE


# ------------------------------------------------------------- state texts


def test_the_hot_water_holiday_mode_has_state_texts() -> None:
    observation = signal(
        "SC.DHW1.HolidayModeNew",
        0,
        state={
            "OFF": 2,
            "ON_LOW": 4,
            "NONE": 0,
            "ON_ECO": 3,
            "ON_HIGH": 5,
            "AUTO_SAT": 1,
        },
    )
    sensor, value = shown(observation)
    assert sensor.definition.kind == STATE
    assert sensor.definition.translation_key == "signal_dhw_holiday_mode"
    assert value == "none"
    for code in ("source", *i18n.LANGUAGES):
        states = _translations(code)["entity"]["sensor"]["signal_dhw_holiday_mode"]
        assert set(states["state"]) == set(sensor.options or ()), code


def test_the_frost_demand_is_named_neutrally() -> None:
    for code, frost in (("source", "Frost range"), ("de", "Frostbereich")):
        states = _translations(code)["entity"]["sensor"]["heat_demand"]["state"]
        assert states["frost"] == frost
        assert all("rost protection" not in text for text in states.values())
        assert all("Frostschutz" not in text for text in states.values())
    old = {
        "sv": "Frostskydd",
        "fr": "Protection antigel",
        "cs": "Ochrana proti zamrznutí",
        "sk": "Ochrana proti mrazu",
    }
    for code, text in old.items():
        states = _translations(code)["entity"]["sensor"]["heat_demand"]["state"]
        assert all(text.casefold() not in value.casefold() for value in states.values())


# ------------------------------------------------- the low-flow binary sensors

HS_FLOW = "/heatSources/hs1/pumpVolumeFlow"
SYSTEM_FLOW = "/signals/SRC.SystemPumpVolumeFlow"
CIRCUIT_FLOW = "/signals/SRC.HC1.FlowCtrl.PumpVolumeFlow"


def _flow(path, value, marker=65535):
    if path.startswith("/signals/"):
        return signal(
            path.removeprefix("/signals/"),
            value,
            unit="l/h",
            state={"LOW_FLOW": marker},
        )
    return number(
        path, value, unit="l/h", kind="integerValue", state=[{"lowflow": marker}]
    )


def _binary(coordinator) -> dict[str, BoschBuderusFlowRangeBinarySensor]:
    added: list = []
    entry = SimpleNamespace(async_on_unload=Mock())
    track_flow_range_sensors(entry, coordinator, added.extend)
    return {entity._flow.key: entity for entity in added}


@pytest.mark.parametrize(
    ("value", "on"),
    [(65535, True), (1180, False), (0, False)],
)
async def test_the_primary_flow_below_range(hass: HomeAssistant, value, on) -> None:
    """65535 with its declared state is on; a measured value, a real 0
    included, is off."""
    coordinator = coordinator_with(hass, [_flow(HS_FLOW, value)])
    sensors = _binary(coordinator)
    assert set(sensors) == {PRIMARY.key}
    sensor = sensors[PRIMARY.key]
    assert sensor.available
    assert sensor.is_on is on
    assert sensor.extra_state_attributes == {
        "derived_from": HS_FLOW,
        "source": "local",
    }
    # The flow sensor itself: unknown with the reported state, or the value.
    flow_sensor = by_path(track(coordinator))[HS_FLOW]
    assert flow_sensor.native_value == (None if on else value)
    if on:
        assert flow_sensor.extra_state_attributes == {
            "measuring_range": "below",
            "reported_state": "low_flow",
        }
    else:
        assert flow_sensor.extra_state_attributes is None
    await coordinator.async_shutdown()


async def test_the_circuit_flow_and_the_signal_fallback(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(
        hass, [_flow(SYSTEM_FLOW, 65535), _flow(CIRCUIT_FLOW, 1362)]
    )
    sensors = _binary(coordinator)
    assert set(sensors) == {PRIMARY.key, CIRCUIT_1.key}
    # Without the heat source's flow, the controller signal decides.
    assert sensors[PRIMARY.key].is_on is True
    assert sensors[PRIMARY.key].extra_state_attributes["derived_from"] == SYSTEM_FLOW
    assert sensors[CIRCUIT_1.key].is_on is False
    await coordinator.async_shutdown()


async def test_the_heat_source_flow_comes_first(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(
        hass, [_flow(HS_FLOW, 900), _flow(SYSTEM_FLOW, 65535)]
    )
    state = current_state(coordinator, PRIMARY)
    assert state is not None and state.path == HS_FLOW and state.below is False
    await coordinator.async_shutdown()


async def test_another_marker_says_nothing_about_the_range(
    hass: HomeAssistant,
) -> None:
    open_sensor = number(
        HS_FLOW, -32768, unit="l/h", kind="integerValue", state=[{"open": -32768}]
    )
    coordinator = coordinator_with(hass, [open_sensor])
    sensor = _binary(coordinator)[PRIMARY.key]
    assert not sensor.available
    assert sensor.is_on is None
    assert sensor.extra_state_attributes is None
    await coordinator.async_shutdown()


async def test_without_a_flow_path_no_binary_sensor(hass: HomeAssistant) -> None:
    """A plant without these flow readings (for example a brine-to-water heat
    pump without a primary flow) gets no entity."""
    coordinator = coordinator_with(
        hass,
        [number("/heatSources/hs1/oduFanSpeed", 40, unit="%", kind="integerValue")],
    )
    assert _binary(coordinator) == {}
    await coordinator.async_shutdown()


async def test_a_registered_binary_sensor_is_created_without_a_reading(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(hass, [])
    er.async_get(hass).async_get_or_create(
        "binary_sensor",
        DOMAIN,
        flow_range_unique_id(coordinator.gateway.gateway_id, CIRCUIT_1),
    )
    sensors = _binary(coordinator)
    assert set(sensors) == {CIRCUIT_1.key}
    assert not sensors[CIRCUIT_1.key].available
    await coordinator.async_shutdown()


async def test_the_binary_sensors_are_disabled_diagnostics(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, [_flow(HS_FLOW, 1), _flow(CIRCUIT_FLOW, 1)])
    for key, sensor in _binary(coordinator).items():
        assert sensor.entity_registry_enabled_default is False
        assert sensor.entity_category is EntityCategory.DIAGNOSTIC
        assert sensor.translation_key == key
        assert sensor.unique_id == f"gateway-one:{key}"
    for code in ("source", *i18n.LANGUAGES):
        binary = _translations(code)["entity"]["binary_sensor"]
        assert binary[PRIMARY.key]["name"] and binary[CIRCUIT_1.key]["name"], code
    await coordinator.async_shutdown()


async def test_a_cloud_flow_below_the_range(hass: HomeAssistant) -> None:
    below = cloud_flow(65535, [{"lowflow": 65535}])
    coordinator = coordinator_with(hass, [], below)
    sensor = _binary(coordinator)[PRIMARY.key]
    assert sensor.is_on is True
    assert sensor.extra_state_attributes["source"] == "cloud"
    await coordinator.async_shutdown()
    measured = coordinator_with(hass, [], cloud_flow(12.5, [{"lowflow": 65535}]))
    assert _binary(measured)[PRIMARY.key].is_on is False
    await measured.async_shutdown()


# ---------------------------------------------------- outdoor unit software


def _row(name, hardware, version, **extra):
    return {
        "ProductName": name,
        "ModuleHwIdentStr": hardware,
        "Ver": version,
        **extra,
    }


CLOUD_ROWS = [
    _row("Logatherm WLW176i-12 TP70", "XCU_THH", "12.11.1-2fc97779"),
    {"ModuleHwIdentStr": "HMI_module_02", "Ver": "47.12-FIELD"},
    _row(None, "MX400", "15.00.01"),
    _row("WLW-10 MB AR", "XCU_SRH", "9.16.0-dab9f463", ModuleSerialNumber="12345"),
]


def test_the_outdoor_unit_version_from_the_cloud() -> None:
    units = from_cloud(CLOUD_ROWS)
    assert units is not None
    assert units.state == "9.16.0-dab9f463"
    assert units.attributes() == {
        "product_name": "WLW-10 MB AR",
        "hardware_type": "XCU_SRH",
        "source": "cloud",
    }


def test_no_outdoor_unit_no_version() -> None:
    assert from_cloud(CLOUD_ROWS[:3]) is None
    assert from_cloud([]) is None
    assert from_cloud(["text", 3]) is None


@pytest.mark.parametrize(
    "version", ["12345", "", "  ", "v e r", "abc", None, 9.16, "x" * 65]
)
def test_identifiers_and_other_shapes_are_never_a_version(version) -> None:
    row = _row("WLW-10 MB AR", "XCU_SRH", version, ModuleSerialNumber="12345")
    assert from_cloud([row]) is None


def test_versions_are_bounded() -> None:
    assert clean_version("9.16.0-dab9f463") == "9.16.0-dab9f463"
    assert clean_version("9.16.0", frozenset({"9.16.0"})) is None


def test_several_outdoor_units_are_listed() -> None:
    rows = [
        _row("WLW-10 MB AR", "XCU_SRH", "9.16.0"),
        _row("WLW-10 MB AR", "XCU_SRH", "9.15.2"),
    ]
    units = from_cloud(rows)
    assert units is not None
    assert len(units.attributes()["outdoor_units"]) == 2


def test_the_local_module_list() -> None:
    modules = [
        SimpleNamespace(
            token="t",
            id="1",
            version="9.16.0-dab9f463",
            serial_number="999",
            hardware="XCU_SRH",
            product_serial=None,
            product_name="WLW-10 MB AR",
        ),
        SimpleNamespace(
            token="t",
            id="2",
            version="999",
            serial_number="999",
            hardware="XCU_SRH",
            product_serial=None,
            product_name="Other",
        ),
    ]
    units = from_local(modules)
    assert units is not None
    assert units.state == "9.16.0-dab9f463"
    assert units.source == "local"


def test_a_restart_keeps_the_version() -> None:
    units = from_cloud(CLOUD_ROWS)
    assert units is not None
    assert from_data(units.to_data()) == OutdoorUnits(units.units, "cloud")
    assert from_data({"source": "cloud", "units": [["a", "b"]]}) is None
    assert from_data({"source": "other", "units": []}) is None
    assert from_data(None) is None


def test_the_newer_answer_is_shown() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    cloud = OutdoorUnits(from_cloud(CLOUD_ROWS).units, "cloud", now)
    local = OutdoorUnits(cloud.units, "local", now - timedelta(minutes=5))
    assert choose(cloud, local) is cloud
    assert choose(None, local) is local
    assert choose(cloud, None) is cloud
    assert choose(None, None) is None


def test_the_inputs_read_the_cloud_module_list() -> None:
    snapshot = SimpleNamespace(
        available=True,
        resource=SimpleNamespace(values=CLOUD_ROWS),
        last_success=None,
    )
    coordinator = SimpleNamespace(data={"/system/info": snapshot}, local_selection=None)
    assert OutdoorUnitInputs(coordinator).current().state == "9.16.0-dab9f463"
    snapshot.available = False
    assert OutdoorUnitInputs(coordinator).current() is None


async def test_the_outdoor_unit_sensor_is_an_enabled_diagnostic(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(hass, [])
    sensor = BoschBuderusOutdoorUnitSoftwareSensor(coordinator)
    assert sensor.entity_registry_enabled_default is True
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.translation_key == "outdoor_unit_software_version"
    await coordinator.async_shutdown()


def test_the_outdoor_unit_sensor_has_a_translated_name() -> None:
    assert outdoor_unit_software_unique_id("gw") == "gw:outdoor_unit_software_version"
    for code in ("source", *i18n.LANGUAGES):
        sensor = _translations(code)["entity"]["sensor"]
        assert sensor["outdoor_unit_software_version"]["name"], code


# ------------------------------------------- composed name parts as keys


def test_every_name_part_exists_in_every_language() -> None:
    source = name_translations.NAMES["en"]
    assert len(source) >= 40
    for code in i18n.LANGUAGES:
        assert set(name_translations.NAMES[code]) == set(source), code


def test_a_name_part_falls_back_to_english() -> None:
    assert name_translations.name_part("eo", "name_part_low_salt") == "Low salt"
    assert name_translations.name_part("de-AT", "name_part_low_salt") == (
        "Salzvorrat niedrig"
    )
    assert (
        name_translations.name_part("en", "name_part_schedule_name", suffix="A")
        == "Schedule A name"
    )


def test_a_part_missing_in_a_language_shows_english(monkeypatch) -> None:
    names = dict(name_translations.NAMES)
    names["sv"] = {}
    monkeypatch.setattr(name_translations, "NAMES", names)
    assert name_translations.name_part("sv", "name_part_low_salt") == "Low salt"


def test_a_new_translation_file_is_found(tmp_path) -> None:
    source = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    (tmp_path / "en.json").write_text(json.dumps(source), encoding="utf-8")
    loaded = name_translations.load_names(tmp_path)
    assert set(loaded) == {"en"}
    assert loaded["en"] == name_translations.NAMES["en"]


def test_the_room_controller_and_statistic_names_come_from_the_keys() -> None:
    assert rrc.name("/zones/zn1/userMode", "de", control=True) == (
        "Heizungsbetriebsart einstellen"
    )
    assert rrc.name("/zones/zn1/userMode", "en") == "Heating mode"
    sv = _translations("sv")["entity"]["sensor"]
    assert (
        rrc.name("/zones/zn1/userMode", "sv")
        == sv["name_part_rrc_heating_mode"]["name"]
    )
    for key in KEYS:
        assert (
            statistic_name(key, "en")
            == (_translations("source")["entity"]["sensor"][key.name_key]["name"])
        )
    assert statistic_name(KEYS[0], "de") == "Wärmepumpe Strom Heizen (kWh erwartet)"


def test_no_name_part_is_left_in_the_built_texts() -> None:
    """The moved texts are no longer built texts of ``i18n``."""
    moved = {
        "Set room temperature",
        "Heat pump electricity heating (kWh expected)",
        "Low salt",
        "Schedule {suffix} name",
    }
    for code in OTHER_LANGUAGES:
        assert not moved & set(_texts(code)), code


def test_resource_names_with_name_parts() -> None:
    assert resource_name("/gateway/update/status", language="de") == "Softwareupdate"
    assert resource_name(
        "/heatSources/emon/coolingConsumption", "outputProduced", language="en"
    ) == (f"Cooling {DASH} Cooling energy produced")
    assert (
        resource_name("/dhwCircuits/dhw1/temperatureLevels/high", language="en")
        == "Start threshold Comfort"
    )


def test_a_local_sensor_of_a_named_signal_is_offered(hass: HomeAssistant) -> None:
    sensor, reason = local_sensor(signal("SRC.CUHP.SummaryAlarm", "false"))
    assert sensor is not None, reason
    assert sensor.definition.key == "summary_alarm_signal"
    assert sensor.definition.kind == FLAG
