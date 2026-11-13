"""Names and defaults of the entity review of 0.9.1.

Names change only where the reviewed meaning is established:

- ``flameStatus`` says what it means on every heat source (a burner or a
  compressor): the heat source is active.
- The heating circuit sensors of the comfort and setback levels are named
  like their number controls.
- Controller signals whose meaning follows from their path and the recorded
  values lose the signal path in their name; open ones keep it.
- One term each for the silent mode and the season switching.
- The thermal heat output is not called a compressor figure, VR0 is the
  receiver valve, the gateway number is an ID, the variable tariff's support
  status says what it supports.

Unique IDs, translation keys, entity IDs and the activation of existing
entities stay; the defaults for new entities are pinned here as well.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating.control_presentation import (
    control_label,
)
from custom_components.bosch_buderus_heating.i18n import LANGUAGES
from custom_components.bosch_buderus_heating.local_only import (
    _SIGNAL_KEYS,
    CONTROLLER_SIGNALS,
    GENERIC,
    definition_for,
    diagnostic,
    entity_name,
)
from custom_components.bosch_buderus_heating.local_only_sensor import (
    BoschBuderusInstallationDateSensor,
    local_only_unique_id,
)
from custom_components.bosch_buderus_heating.pointt import Resource
from custom_components.bosch_buderus_heating.pointt.models import ResourceMetadata
from custom_components.bosch_buderus_heating.resource_catalog import (
    entity_enabled_by_default,
    resource_name,
)
from custom_components.bosch_buderus_heating.sensor import build_sensor_descriptions
from tests.ha_test_helpers import add_without_device
from tests.helpers.rrc_cloud import load_tree

COMPONENT = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
OTHER_LANGUAGES = [code for code in LANGUAGES if code not in ("en", "de")]
DASH = "\N{EN DASH}"

# (path, subkey, English, German) of the cloud names.
CLOUD_NAMES = [
    ("/heatSources/flameStatus", None, "Heat source active", "Wärmeerzeuger aktiv"),
    (
        "/heatingCircuits/hc1/temperatureLevels/comfort2",
        None,
        "Heating temperature",
        "Heiztemperatur",
    ),
    (
        "/heatingCircuits/hc1/temperatureLevels/eco",
        None,
        "Setback temperature",
        "Absenktemperatur",
    ),
    ("/gateway/uuid", None, "Gateway ID", "Gateway-ID"),
    ("/gateway/swPrefix", None, "Software family", "Softwarefamilie"),
    (
        "/system/variableTariff/supportStatus",
        None,
        f"Variable tariff {DASH} support",
        f"Variabler Tarif {DASH} Unterstützung",
    ),
    (
        "/system/iSRC/installationStatus",
        None,
        "iSRC installation status",
        "iSRC-Installationsstatus",
    ),
    ("/heatSources/hs1/type", None, "Heat source type", "Typ des Wärmeerzeugers"),
    ("/system/type", None, "System type", "Anlagentyp"),
    (
        "/system/silentMode/enabled",
        None,
        "Silent mode enabled",
        "Flüstermodus aktiviert",
    ),
    ("/system/seasonOptimizer/mode", None, "Season switching", "Saisonumschaltung"),
]

# (signal, English, German) of the local names.
LOCAL_NAMES = [
    (
        "SRC.CUHP.HP1.CompressorPower",
        "Heat output (thermal)",
        "Heizleistung (thermisch)",
    ),
    (
        "SRC.CUHP.HP1.ReceiverValveActual",
        "Receiver valve (VR0)",
        "Sammlerventil (VR0)",
    ),
    (
        "SRC.CUHP.HP1.ExpansionValveActual",
        "Expansion valve VR1",
        "Expansionsventil VR1",
    ),
    (
        "SRC.CUHP.SilentModeActive",
        "Silent mode active (signal)",
        "Flüstermodus aktiv (Signal)",
    ),
    (
        "SRC.CUHP.HMI.CompressorActive",
        "Compressor running (display signal)",
        "Kompressor läuft (Anzeigesignal)",
    ),
    (
        "SRC.CUHP.HP1.CompressorStatus",
        "Compressor running (heat pump signal)",
        "Kompressor läuft (Wärmepumpensignal)",
    ),
    (
        "SC.HC1.MaxFlowTempSetp",
        "Maximum supply temperature HC1 (signal)",
        "Maximale Vorlauftemperatur HK1 (Signal)",
    ),
    (
        "SC.HC1.RTSD.CurrentRoomTempSetp",
        "Current room setpoint HC1 (signal)",
        "Aktueller Raum-Sollwert HK1 (Signal)",
    ),
    ("SC.HC1.HolidayMode", "Holiday mode HC1", "Urlaubsmodus HK1"),
    (
        "SRC.CHPumpModulation",
        "Primary heating pump modulation (PC0, signal)",
        "Modulation primäre Heizungspumpe (PC0, Signal)",
    ),
    (
        "SRC.CUHP.HP1.CompressorSpeedMax",
        "Maximum compressor speed",
        "Maximale Kompressordrehzahl",
    ),
    (
        "SRC.CUHP.Additional.ExternBlocked",
        "Auxiliary heater blocked externally",
        "Zuheizer extern gesperrt",
    ),
    (
        "SRC.CUHP.CH.ExternBlocked",
        "Heating blocked externally",
        "Heizen extern gesperrt",
    ),
    (
        "SRC.CUHP.DHW.ExternBlocked",
        "Hot water blocked externally",
        "Warmwasser extern gesperrt",
    ),
    (
        "SRC.CUHP.HP1.CompressorExtBlocked",
        "Compressor blocked externally",
        "Kompressor extern gesperrt",
    ),
    ("SRC.CUHP.HMI.BuzzerActive", "Buzzer active", "Summer aktiv"),
    ("GWEEBUS.Status", "EEBus status", "EEBus-Status"),
    (
        "GWEEBUS.CommissioningStatus",
        "EEBus commissioning status",
        "EEBus-Inbetriebnahmestatus",
    ),
    (
        "SRC.CUHP.Stats.ControlUnit",
        "Controller operating time",
        "Betriebszeit Regler",
    ),
    (
        "SRC.CUHP.HP1.BrineTempHeatingLow",
        "Brine temperature too low",
        "Soletemperatur zu niedrig",
    ),
    (
        "SRC.CUHP.HP1.CollectorCircuitPressureSwitch",
        "Collector circuit pressure switch",
        "Druckschalter Kollektorkreis",
    ),
    ("SRC.RelayStatus.Fan", "Fan relay", "Relais Lüfter"),
    ("SRC.RelayStatus.GasValve1", "Gas valve 1 relay", "Relais Gasventil 1"),
    ("SRC.RelayStatus.GasValve2", "Gas valve 2 relay", "Relais Gasventil 2"),
    (
        "SRC.CUHP.HP1.Stats.CompStartsCooling",
        "Compressor starts cooling",
        "Kompressorstarts Kühlen",
    ),
    ("SC.HolidayNew.1.State", "Holiday slot 1 state", "Urlaubsplatz 1 Status"),
    ("SC.HolidayNew.8.State", "Holiday slot 8 state", "Urlaubsplatz 8 Status"),
]

# Signals whose meaning is open keep the signal path as their name.
OPEN_SIGNALS = (
    "SRC.CUHP.HP1.HighestPermittedAirTemp",
    "SRC.CUHP.HP1.HighestPermittedTemp",
    "SRC.CUHP.HP1.LowestPermittedAirTemp",
    "SRC.CUHP.HP1.LowestPermittedFlowTemp",
    "SRC.CUHP.HP1.HeatingCable",
    "SRC.CUHP.HP1.G3Relay",
    "SRC.CUHP.HP1.CompressorStartTemp",
)

# English texts that left the names, with their replacement.
OLD_ENGLISH = (
    "Burner status",
    "Gateway UUID",
    "Heat output (compressor)",
    "Expansion valve VR0",
    "Low-noise mode active",
    "Compressor (status)",
    "Reduced",
)


def _catalog(code: str) -> dict[str, str]:
    path = COMPONENT / "i18n" / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8"))["texts"]


def _local(signal: str, language: str) -> str:
    path = f"/signals/{signal}"
    definition = definition_for(path)
    assert definition is not None, signal
    return entity_name(definition, path, language)


# ---------------------------------------------------------------- the names


@pytest.mark.parametrize(("path", "subkey", "english", "german"), CLOUD_NAMES)
def test_cloud_names(path, subkey, english, german) -> None:
    assert resource_name(path, subkey, language="en") == english
    assert resource_name(path, subkey, language="de") == german


def test_the_two_system_types_are_told_apart() -> None:
    assert resource_name("/heatSources/hs1/type", language="en") != resource_name(
        "/system/type", language="en"
    )
    # The kind of the additional heater has its own name.
    assert resource_name(
        "/heatSources/additionalHeater/primary/type", language="en"
    ) == ("Primary auxiliary heater type")


@pytest.mark.parametrize(("signal", "english", "german"), LOCAL_NAMES)
def test_local_names(signal, english, german) -> None:
    assert _local(signal, "en") == english
    assert _local(signal, "de") == german


@pytest.mark.parametrize("signal", OPEN_SIGNALS)
def test_open_signals_keep_the_signal_path(signal) -> None:
    assert _local(signal, "en") == f"Signal {signal}"


def test_the_silent_mode_has_one_name() -> None:
    names = {
        control_label("/system/silentMode/enabled", "x", "en"),
        _local("SRC.CUHP.SilentModeActive", "en").removesuffix(" active (signal)"),
        resource_name("/system/silentMode/enabled", language="en").removesuffix(
            " enabled"
        ),
    }
    assert names == {"Silent mode"}
    german = {
        control_label("/system/silentMode/enabled", "x", "de"),
        _local("SRC.CUHP.SilentModeActive", "de").removesuffix(" aktiv (Signal)"),
        resource_name("/system/silentMode/enabled", language="de").removesuffix(
            " aktiviert"
        ),
    }
    assert german == {"Flüstermodus"}


def test_the_season_switching_has_one_name() -> None:
    path = "/system/seasonOptimizer/mode"
    for language in ("en", "de"):
        assert control_label(path, "x", language) == resource_name(
            path, language=language
        )


def test_the_names_of_the_translation_files() -> None:
    for name, labels in (
        ("strings.json", ("Heating temperature", "Setback temperature")),
        ("translations/en.json", ("Heating temperature", "Setback temperature")),
        ("translations/de.json", ("Heiztemperatur", "Absenktemperatur")),
    ):
        data = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        numbers = data["entity"]["number"]
        assert (
            numbers["heating_temperature"]["name"],
            numbers["reduced_temperature"]["name"],
        ) == labels, name
    for name, expected in (
        ("strings.json", f"Variable tariff {DASH} support"),
        ("translations/en.json", f"Variable tariff {DASH} support"),
        ("translations/de.json", f"Variabler Tarif {DASH} Unterstützung"),
    ):
        data = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        assert data["entity"]["sensor"]["support_status"]["name"] == expected


@pytest.mark.parametrize("code", OTHER_LANGUAGES)
def test_every_language_has_the_new_names(code) -> None:
    texts = _catalog(code)
    for path, subkey, english, _german in CLOUD_NAMES:
        assert english in texts, (code, english)
        assert resource_name(path, subkey, language=code) == texts[english]
    for signal, english, _german in LOCAL_NAMES:
        if "{}" in english or signal.startswith("SC.HolidayNew"):
            continue
        assert english in texts, (code, english)
        assert _local(signal, code) == texts[english], (code, signal)
    slot = _local("SC.HolidayNew.3.State", code)
    assert slot == texts["Holiday slot {} state"].format("3")
    assert slot != "Holiday slot 3 state"
    for old in OLD_ENGLISH:
        assert old not in texts, (code, old)
    assert (
        resource_name("/heatSources/flameStatus", language=code)
        == texts["Heat source active"]
    )
    assert resource_name("/gateway/uuid", language=code) == texts["Gateway ID"]
    assert (
        resource_name("/heatingCircuits/hc1/temperatureLevels/eco", language=code)
        == texts["Setback temperature"]
    )


# ---------------------------------------------------- the signals stay what they were


def test_named_signals_stay_generic_disabled_diagnostics() -> None:
    for signal, _english, _german in LOCAL_NAMES:
        definition = definition_for(f"/signals/{signal}")
        assert definition is not None
        if definition.key.endswith("_signal") or definition.kind == GENERIC:
            assert not definition.enabled, signal
        if definition.kind == GENERIC:
            assert diagnostic(definition), signal
            assert definition.key in _SIGNAL_KEYS, signal
    # Only the reviewed ones that were enabled before stay enabled.
    enabled = {
        definition.key
        for definition in CONTROLLER_SIGNALS
        if definition.enabled and definition.kind == GENERIC
    }
    assert not enabled


def test_unique_ids_of_renamed_signals_stay() -> None:
    assert local_only_unique_id("gw", "/signals/GWEEBUS.Status") == (
        "gw:local_only:signals:GWEEBUS.Status"
    )
    assert local_only_unique_id("gw", "/signals/SRC.CUHP.HP1.ReceiverValveActual") == (
        "gw:local_only:signals:SRC.CUHP.HP1.ReceiverValveActual"
    )


def test_the_keys_of_the_renamed_entities_stay() -> None:
    assert definition_for("/signals/SRC.CUHP.HP1.ReceiverValveActual").key == (
        "expansion_valve_vr0"
    )
    assert definition_for("/signals/SRC.CUHP.HP1.CompressorPower").key == (
        "compressor_output"
    )
    assert definition_for("/signals/SRC.CUHP.SilentModeActive").key == (
        "silent_mode_active"
    )
    assert definition_for("/signals/SRC.CUHP.HMI.CompressorActive").enabled
    assert not definition_for("/signals/SRC.CUHP.HP1.CompressorStatus").enabled


# ------------------------------------------------- registry: IDs and activation


def _flame_sensor(hass: HomeAssistant):
    from tests.test_sensor import _sensor

    return _sensor(
        hass,
        Resource(
            path="/heatSources/flameStatus",
            value="on",
            has_value=True,
            metadata=ResourceMetadata(resource_type="stringValue"),
        ),
    )


async def test_an_existing_flame_entity_keeps_id_and_activation(
    hass: HomeAssistant,
) -> None:
    assert await async_setup_component(hass, "sensor", {})
    sensor = _flame_sensor(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor",
        "sensor",
        sensor.unique_id,
        suggested_object_id="k40_heat_pump_burner_status",
        original_name=f"Heat pump {DASH} Burner status",
    )
    assert old.disabled_by is None
    await add_without_device(hass, sensor)
    await hass.async_block_till_done()
    assert sensor.entity_id == old.entity_id == "sensor.k40_heat_pump_burner_status"
    entry = registry.async_get(old.entity_id)
    assert entry is not None
    assert entry.unique_id == old.unique_id
    assert entry.disabled_by is None
    assert entry.original_name.endswith("Heat source active")
    assert hass.states.get(old.entity_id).state == "on"


@pytest.mark.parametrize(
    ("signal", "value", "old_name", "object_id", "new_name"),
    [
        (
            "GWEEBUS.Status",
            "not_commissioned",
            "System \N{EN DASH} Signal GWEEBUS.Status",
            "mx400_system_signal_gweebus_status",
            "EEBus status",
        ),
        (
            "SRC.RelayStatus.GasValve1",
            "false",
            "System \N{EN DASH} Signal SRC.RelayStatus.GasValve1",
            "mx400_system_signal_src_relaystatus_gasvalve1",
            "Gas valve 1 relay",
        ),
        (
            "SRC.CUHP.HP1.CompressorStatus",
            "true",
            "System \N{EN DASH} Compressor (status)",
            "mx400_system_compressor_status",
            "Compressor running (heat pump signal)",
        ),
    ],
)
async def test_an_existing_signal_entity_keeps_id_and_activation(
    hass: HomeAssistant, signal, value, old_name, object_id, new_name
) -> None:
    from tests.test_c7_signals import answer, entity

    assert await async_setup_component(hass, "sensor", {})
    allowed = ["not_commissioned", "commissioned"] if value != "true" else None
    sensor = entity(
        hass,
        f"/signals/{signal}",
        [answer(f"/signals/{signal}", value, allowed=allowed)],
    )
    # A new installation creates these disabled ...
    assert not sensor.entity_registry_enabled_default
    registry = er.async_get(hass)
    # ... an installation that enabled it keeps it enabled and keeps the ID.
    old = registry.async_get_or_create(
        "sensor",
        "sensor",
        sensor.unique_id,
        suggested_object_id=object_id,
        original_name=old_name,
    )
    assert old.disabled_by is None
    await add_without_device(hass, sensor)
    await hass.async_block_till_done()
    entry = registry.async_get(old.entity_id)
    assert sensor.entity_id == old.entity_id == f"sensor.{object_id}"
    assert entry is not None
    assert entry.unique_id == old.unique_id
    assert entry.disabled_by is None
    assert entry.original_name.endswith(new_name)


async def test_a_new_installation_creates_the_named_signals_disabled(
    hass: HomeAssistant,
) -> None:
    from tests.test_c7_signals import answer, entity

    assert await async_setup_component(hass, "sensor", {})
    path = "/signals/GWEEBUS.Status"
    sensor = entity(
        hass, path, [answer(path, "not_commissioned", allowed=["not_commissioned"])]
    )
    await add_without_device(hass, sensor)
    await hass.async_block_till_done()
    entry = er.async_get(hass).async_get(sensor.entity_id)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert entry.original_name.endswith("EEBus status")


# ------------------------------------------------ defaults for new installations


def _k40() -> dict[str, Resource]:
    return load_tree("captures/k40-cloud-2026-11-05.json")


@pytest.mark.parametrize(
    ("path", "count"),
    [("/gateway/update/status", 5), ("/heatSources/systemPressureRange", 6)],
)
def test_update_details_and_pressure_limits_are_created_disabled(path, count) -> None:
    """Every part of the software update (status, progress, step) and every
    permitted pressure limit (including the low system pressure) is created
    disabled; the pressure itself and its status stay enabled."""
    assert not entity_enabled_by_default(path)
    resources = _k40()
    if path not in resources:
        resources[path] = Resource(
            path=path,
            value={},
            has_value=True,
            metadata=ResourceMetadata(resource_type="stringValue"),
        )
    items = [
        item
        for item in build_sensor_descriptions(resources)
        if item.resource_path == path
    ]
    assert len(items) >= 1
    assert not any(item.entity_registry_enabled_default for item in items)
    assert entity_enabled_by_default("/heatSources/systemPressure")


def test_pool_starts_and_the_installation_date_parts_are_created_disabled() -> None:
    items = {
        item.key: item.entity_registry_enabled_default
        for item in build_sensor_descriptions(_k40())
    }
    pool = [key for key in items if key.endswith("numberOfStarts:pool")]
    assert pool
    assert not any(items[key] for key in pool)
    for part in ("Day", "Month", "Year"):
        definition = definition_for(f"/signals/SC.InstallationDate.{part}")
        assert definition is not None
        assert definition.kind == GENERIC and not definition.enabled


def test_the_gas_valve_signals_are_created_disabled() -> None:
    for signal in ("GasValve1", "GasValve2"):
        definition = definition_for(f"/signals/SRC.RelayStatus.{signal}")
        assert definition is not None and not definition.enabled
    # The fourth heating stage is not offered by default either.
    assert not definition_for("/signals/SRC.CUHP.HP1.ElectricalHeaterStep4").enabled
    assert definition_for("/signals/SRC.CUHP.HP1.ElectricalHeaterStep3").enabled


# ---------------------------------------------------------- installation date


def _date_sensor(day, month, year):
    parts = {"Day": day, "Month": month, "Year": year}

    def part(name):
        return parts[name]

    fake = SimpleNamespace(_part=part)
    return (
        BoschBuderusInstallationDateSensor.native_value.fget(fake),
        BoschBuderusInstallationDateSensor.available.fget(fake),
    )


def test_a_valid_past_installation_date_is_shown() -> None:
    assert _date_sensor(1, 8, 20) == (date(2020, 8, 1), True)
    today = dt_util.now().date()
    assert _date_sensor(today.day, today.month, today.year - 2000)[0] == today


def test_a_future_installation_date_is_unknown() -> None:
    # The placeholder of a controller that was never given a date.
    assert _date_sensor(1, 8, 60) == (None, True)
    future = dt_util.now().date() + timedelta(days=2)
    assert _date_sensor(future.day, future.month, future.year - 2000)[0] is None


def test_an_impossible_installation_date_is_unknown() -> None:
    assert _date_sensor(31, 2, 20) == (None, True)
    assert _date_sensor(1, 13, 20) == (None, True)
    assert _date_sensor(0, 8, 20) == (None, True)
    assert _date_sensor(1, 8, 120) == (None, True)


def test_missing_parts_stay_unavailable() -> None:
    for day, month, year in ((None, 8, 20), (1, None, 20), (1, 8, None)):
        assert _date_sensor(day, month, year) == (None, False)
