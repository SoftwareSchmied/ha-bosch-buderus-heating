"""Readings only the local access provides, as their own entities (P45).

The cloud does not offer these readings on every plant; the local access
reads them anyway with its polling plan. They become entities only for paths
the local access of this plant could actually read, and only when no cloud
resource of the same path exists (that one keeps its own entity). Without a
current local reading the entity is unavailable: there is no cloud value to
fall back to. Device states such as an open sensor are shown as unknown, never
as a number.

Reviewed readings have a fixed meaning, unit and default activation. The
controller's internal signals and the external inputs are offered generically
and disabled, only with numeric readings: their unit comes from the reading,
they get no statistics, and identifiers are never offered.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import lru_cache
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    UnitOfPower,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolumeFlowRate,
)

from .observations import ERROR_MARKERS, Observation, ValueStatus, canonical_unit

TEMPERATURE = "temperature"
POWER = "power"
FLOW = "flow"
PERCENT = "percent"
HUMIDITY = "humidity"
ROTATION = "rotation"
CONCENTRATION = "concentration"
DURATION = "duration"
# A reviewed name whose unit is not established: no device class, no statistics.
READING = "reading"
STATE = "state"
# C10-b: an on/off state reported as true/false, 1/0 or on/off.
FLAG = "flag"
# C10-b: the smart grid mode, a number or a declared state.
MODE = "mode"
GENERIC = "generic"
# Kinds shown as an enum sensor.
ENUM_KINDS = frozenset({STATE, FLAG, MODE})

# Canonical local unit -> Home Assistant unit and device class.
_UNITS: dict[str, tuple[str, SensorDeviceClass | None]] = {
    "°C": (UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE),
    "K": (UnitOfTemperature.KELVIN, SensorDeviceClass.TEMPERATURE),
    "W": (UnitOfPower.WATT, SensorDeviceClass.POWER),
    "kW": (UnitOfPower.KILO_WATT, SensorDeviceClass.POWER),
    "l/h": (UnitOfVolumeFlowRate.LITERS_PER_HOUR, SensorDeviceClass.VOLUME_FLOW_RATE),
    "l/min": (
        UnitOfVolumeFlowRate.LITERS_PER_MINUTE,
        SensorDeviceClass.VOLUME_FLOW_RATE,
    ),
    "%": (PERCENTAGE, None),
    "bar": (UnitOfPressure.BAR, SensorDeviceClass.PRESSURE),
    "s": (UnitOfTime.SECONDS, SensorDeviceClass.DURATION),
    "min": (UnitOfTime.MINUTES, SensorDeviceClass.DURATION),
    "h": (UnitOfTime.HOURS, SensorDeviceClass.DURATION),
    "1/min": ("rpm", None),
    "rpm": ("rpm", None),
    "ppm": ("ppm", None),
}
_KIND_CLASSES = {
    TEMPERATURE: SensorDeviceClass.TEMPERATURE,
    POWER: SensorDeviceClass.POWER,
    FLOW: SensorDeviceClass.VOLUME_FLOW_RATE,
    DURATION: SensorDeviceClass.DURATION,
}
# Error markers of temperature readings, also in their one-decimal form.
# C5: bounds for states a device declares in addition to the reviewed ones.
MAX_STATES = 64
_MAX_STATES = MAX_STATES
_MAX_STATE_LENGTH = 64
_TEMPERATURE_MARKERS = frozenset(
    Decimal(marker) for marker in ("32767", "-32768", "3276.7", "-3276.8")
)
# Kinds whose documented unit has no device class of its own.
_KIND_UNITS = {ROTATION: "rpm", CONCENTRATION: "ppm"}
# Identifiers, addresses and secrets of the controller or its links: never
# offered, whatever their value looks like.


@dataclass(frozen=True, slots=True)
class LocalOnlyDefinition:
    pattern: re.Pattern[str]
    key: str
    kind: str
    enabled: bool
    names: tuple[str, str]  # German, English; ``{}`` is filled from the path
    translation_key: str | None = None
    options: tuple[str, ...] = ()
    # C10-b: the unit used only while a reading declares none.
    default_unit: str | None = None
    # C10-b: the on/off form of a signal that may also be a temperature.
    alternate: LocalOnlyDefinition | None = None
    # C10-b: meaning not verified on a real system.
    unverified: bool = False
    # C7-4: the unit Home Assistant shows for a new entity (for example a
    # run time reported in seconds shown in hours).
    suggested_unit: str | None = None


def _define(
    pattern: str,
    key: str,
    kind: str,
    names: tuple[str, str],
    *,
    enabled: bool = False,
    translation_key: str | None = None,
    options: tuple[str, ...] = (),
) -> LocalOnlyDefinition:
    return LocalOnlyDefinition(
        re.compile(pattern), key, kind, enabled, names, translation_key, options
    )


_FLAG_OPTIONS = ("on", "off")
SMART_GRID_MODES = ("blocked", "normal", "preferred", "forced")
_SMART_GRID_OPTIONS = (*SMART_GRID_MODES, "not_used")
# LOE-3: the numbers of a smart grid mode without a state map, as the
# controller declares them with one (Not_used 0, EVU_block_active 1,
# Normal_operation 2, Smart_grid 3, Force_smart_grid 4). Not verified: no
# answer without a state map has been seen, on a controller or on an HM200.
_SMART_GRID_NUMBERS = ("not_used", *SMART_GRID_MODES)
# Declared state names of the smart grid mode and their SG-Ready meaning.
_SMART_GRID_NAMES = {
    "evu_block_active": "blocked",
    "blocked": "blocked",
    "normal_operation": "normal",
    "normal": "normal",
    "smart_grid": "preferred",
    "preferred": "preferred",
    "force_smart_grid": "forced",
    "forced": "forced",
    "not_used": "not_used",
}


def _hybrid(
    signal: str,
    key: str,
    kind: str,
    names: tuple[str, str],
    unit: str | None = None,
    *,
    flag: tuple[str, str] | None = None,
) -> LocalOnlyDefinition:
    """C10-b: one signal of a hybrid manager (HM200), disabled by default."""
    path = re.escape("/signals/HYBMAN." + signal)
    alternate = (
        LocalOnlyDefinition(
            re.compile(path),
            key + "_active",
            FLAG,
            False,
            flag,
            "on_off_state",
            _FLAG_OPTIONS,
            unverified=True,
        )
        if flag is not None
        else None
    )
    options = (
        _FLAG_OPTIONS if kind == FLAG else _SMART_GRID_OPTIONS if kind == MODE else ()
    )
    translation_key = (
        "on_off_state" if kind == FLAG else "smart_grid_mode" if kind == MODE else None
    )
    return LocalOnlyDefinition(
        re.compile(path),
        key,
        kind,
        False,
        names,
        translation_key,
        options,
        default_unit=unit,
        alternate=alternate,
        unverified=True,
    )


def _signal(
    signal: str,
    key: str,
    kind: str,
    names: tuple[str, str],
    *,
    enabled: bool = False,
    translation_key: str | None = None,
    options: tuple[str, ...] = (),
    suggested_unit: str | None = None,
    default_unit: str | None = None,
) -> LocalOnlyDefinition:
    """C7: one controller signal with a reviewed meaning."""
    if kind == FLAG:
        translation_key, options = translation_key or "on_off_state", _FLAG_OPTIONS
    if kind == MODE:
        translation_key, options = "smart_grid_mode", _SMART_GRID_OPTIONS
    return LocalOnlyDefinition(
        re.compile(re.escape("/signals/" + signal)),
        key,
        kind,
        enabled,
        names,
        translation_key,
        options,
        suggested_unit=suggested_unit,
        default_unit=default_unit,
    )


_HP1 = "SRC.CUHP.HP1."
CONTROLLER_SIGNALS: tuple[LocalOnlyDefinition, ...] = (
    # C7-1: on/off states reported as true/false.
    _signal(
        "SRC.CUHP.HMI.CompressorActive",
        "compressor_active",
        FLAG,
        ("Verdichter", "Compressor"),
        enabled=True,
    ),
    _signal(
        _HP1 + "CompressorStatus",
        "compressor_status",
        FLAG,
        ("Verdichter (Status)", "Compressor (status)"),
    ),
    _signal(
        _HP1 + "DHWValve",
        "dhw_valve",
        FLAG,
        ("3-Wege-Ventil Warmwasser (VW1)", "Hot water 3-way valve (VW1)"),
        enabled=True,
        translation_key="dhw_valve_position",
    ),
    _signal(
        _HP1 + "FourwayValve",
        "four_way_valve",
        FLAG,
        ("4-Wege-Ventil (VR4)", "4-way valve (VR4)"),
        enabled=True,
        translation_key="four_way_valve_position",
    ),
    *(
        _signal(
            _HP1 + f"ElectricalHeaterStep{step}",
            f"electric_heater_step_{step}",
            FLAG,
            (f"Heizstab Stufe {step}", f"Electric heater stage {step}"),
            enabled=step <= 3,
        )
        for step in (1, 2, 3, 4)
    ),
    _signal(
        _HP1 + "DripPanHeater",
        "drip_pan_heater",
        FLAG,
        ("Kondensatwannenheizung", "Drip pan heater"),
        enabled=True,
    ),
    _signal(
        _HP1 + "CrankcaseHeater",
        "crankcase_heater",
        FLAG,
        ("Kurbelwannenheizung", "Crankcase heater"),
        enabled=True,
    ),
    _signal(
        _HP1 + "CHPumpRelay",
        "heating_pump_relay",
        FLAG,
        ("Primäre Heizungspumpe (PC0)", "Primary heating pump (PC0)"),
        enabled=True,
    ),
    _signal(
        "SRC.CUHP.CH.HC1PumpRelay",
        "circuit_1_pump_relay",
        FLAG,
        ("Heizkreispumpe HK1 (PC1)", "Heating circuit 1 pump (PC1)"),
    ),
    _signal(
        "SRC.CUHP.Cooling.Active",
        "cooling_active",
        FLAG,
        ("Kühlung aktiv", "Cooling active"),
        enabled=True,
    ),
    _signal(
        "SRC.CUHP.SilentModeActive",
        "silent_mode_active",
        FLAG,
        ("Leiser Betrieb aktiv", "Low-noise mode active"),
        enabled=True,
    ),
    _signal(
        "SRC.CUHP.CH.HeatingSystemVolumeFlowLow",
        "heating_flow_low",
        FLAG,
        ("Volumenstrom Heizung zu niedrig", "Heating volume flow low"),
        enabled=True,
    ),
    _signal(
        _HP1 + "InverterOverheated",
        "inverter_overheated",
        FLAG,
        ("Inverter überhitzt", "Inverter overheated"),
        enabled=True,
    ),
    # C7-2: states declared as a name map.
    _signal(
        "SC.SeasonOpt.Mode",
        "season_mode",
        STATE,
        ("Saisonbetrieb", "Season mode"),
        enabled=True,
        translation_key="signal_season_mode",
        options=("heating", "idle", "cooling"),
    ),
    _signal(
        "SRC.CUHP.CH.GSOState",
        "heat_cool_state",
        STATE,
        ("Heiz-/Kühlzustand", "Heating/cooling state"),
        enabled=True,
        translation_key="signal_heat_cool",
        options=("idle", "heat", "cool"),
    ),
    _signal(
        "SRC.CUHP.SmartGridMode",
        "smart_grid_mode",
        MODE,
        ("Smart-Grid-Modus", "Smart grid mode"),
        enabled=True,
    ),
    # C7-4: measurements with a reviewed meaning.
    _signal(
        "SC.DampOutdTemp",
        "damped_outdoor_temperature",
        TEMPERATURE,
        ("Gedämpfte Außentemperatur", "Damped outdoor temperature"),
        enabled=True,
    ),
    _signal(
        _HP1 + "DripPanTemp",
        "drip_pan_temperature",
        TEMPERATURE,
        ("Kondensatwannentemperatur (TA4)", "Drain pan temperature (TA4)"),
        enabled=True,
    ),
    # O100: despite its name the heat output, not the electric power (it
    # matches the heat meter); existing entities keep their entity ID.
    _signal(
        _HP1 + "CompressorPower",
        "compressor_output",
        POWER,
        ("Wärmeleistung (Verdichter)", "Heat output (compressor)"),
        enabled=True,
    ),
    _signal(
        _HP1 + "ExpansionValveActual",
        "expansion_valve",
        PERCENT,
        ("Expansionsventil VR1", "Expansion valve VR1"),
        enabled=True,
    ),
    _signal(
        _HP1 + "ReceiverValveActual",
        "expansion_valve_vr0",
        PERCENT,
        ("Expansionsventil VR0", "Expansion valve VR0"),
        enabled=True,
    ),
    _signal(
        _HP1 + "Timer.CompressorRestart",
        # A waiting time, not a counter: generic, so it is read hourly while
        # disabled, as before (G3).
        "compressor_start_delay_signal",
        GENERIC,
        ("Verzögerung Kompressorstart", "Compressor start delay"),
    ),
    *(
        _signal(
            _HP1 + f"Stats.Compressor{mode}",
            f"compressor_run_time_{key}",
            DURATION,
            names,
            enabled=True,
            # Static, so the suggested hours are valid also when the entity
            # is registered before its first reading.
            default_unit="s",
            suggested_unit=UnitOfTime.HOURS,
        )
        for mode, key, names in (
            (
                "CH",
                "heating",
                ("Verdichterlaufzeit Heizen", "Compressor run time heating"),
            ),
            (
                "DHW",
                "hot_water",
                ("Verdichterlaufzeit Warmwasser", "Compressor run time hot water"),
            ),
            (
                "Cooling",
                "cooling",
                ("Verdichterlaufzeit Kühlen", "Compressor run time cooling"),
            ),
        )
    ),
    _signal(
        "SRC.HC1.FlowCtrl.PumpVolumeFlow",
        "circuit_1_volume_flow",
        FLOW,
        ("Volumenstrom Heizkreis 1 (PC1)", "Heating circuit 1 flow rate (PC1)"),
        enabled=True,
    ),
    # Second sources of readings other entities already show: named after the
    # controller, but generic and disabled like any other signal (C3).
    *(
        _signal(signal, key, GENERIC, names)
        for signal, key, names in (
            (
                _HP1 + "CompressorSpeedActual",
                "compressor_speed_signal",
                ("Verdichterdrehzahl (Signal)", "Compressor speed (signal)"),
            ),
            (
                _HP1 + "CHPumpSpeed",
                "heating_pump_speed_signal",
                (
                    "Drehzahl primäre Heizungspumpe (PC0, Signal)",
                    "Primary heating pump speed (PC0, signal)",
                ),
            ),
            (
                "SRC.IntFlowTemp",
                "primary_supply_signal",
                (
                    "Vorlauftemperatur primär (TC1, Signal)",
                    "Primary supply temperature (TC1, signal)",
                ),
            ),
            (
                _HP1 + "ReturnTemp",
                "return_temperature_signal",
                (
                    "Rücklauftemperatur (TC0, Signal)",
                    "Return temperature (TC0, signal)",
                ),
            ),
            (
                "SRC.SystemPumpVolumeFlow",
                "primary_flow_signal",
                (
                    "Volumenstrom primär (PC0, Signal)",
                    "Primary flow rate (PC0, signal)",
                ),
            ),
            (
                "SC.HC1.FlowTempSetp",
                "circuit_1_supply_setpoint_signal",
                ("Vorlauf-Sollwert HK1 (Signal)", "Supply setpoint HC1 (signal)"),
            ),
            (
                "SRC.HC1.FlowCtrl.ReqFlowTemp",
                "circuit_1_requested_supply_signal",
                (
                    "Angeforderte Vorlauftemperatur HK1 (Signal)",
                    "Requested supply temperature HC1 (signal)",
                ),
            ),
            (
                "SRC.RelayStatus.3WayValve",
                "dhw_valve_relay_signal",
                (
                    "Relais 3-Wege-Ventil Warmwasser (Signal)",
                    "Hot water 3-way valve relay (signal)",
                ),
            ),
            (
                "SRC.RelayStatus.CHPump",
                "heating_pump_relay_signal",
                (
                    "Relais primäre Heizungspumpe (Signal)",
                    "Primary heating pump relay (signal)",
                ),
            ),
        )
    ),
    # C7-3: the parts of the installation date (the date itself is derived).
    *(
        _signal(
            f"SC.InstallationDate.{part}",
            f"installation_date_{part.lower()}",
            GENERIC,
            names,
        )
        for part, names in (
            ("Day", ("Installationsdatum (Tag)", "Installation date (day)")),
            ("Month", ("Installationsdatum (Monat)", "Installation date (month)")),
            ("Year", ("Installationsdatum (Jahr)", "Installation date (year)")),
        )
    ),
)


_ODU = ("Außeneinheit Temperatur {}", "Outdoor unit temperature {}")
HYBRID_SIGNALS: tuple[LocalOnlyDefinition, ...] = (
    *(
        _hybrid(
            f"ODUMONITOR.{sensor}Temp",
            f"hybrid_odu_{sensor.lower()}",
            TEMPERATURE,
            (_ODU[0].format(sensor), _ODU[1].format(sensor)),
            "°C",
        )
        for sensor in (
            "JR0",
            "JR1",
            "TA4",
            "TC0",
            "TC3",
            "TL2",
            "TR1",
            "TR3",
            "TR4",
            "TR5",
            "TR6",
        )
    ),
    _hybrid(
        "TempFlowHeatPump",
        "hybrid_heat_pump_supply",
        TEMPERATURE,
        ("Vorlauftemperatur Wärmepumpe", "Heat pump supply temperature"),
        "°C",
    ),
    _hybrid(
        "TempReturnHeatPump",
        "hybrid_heat_pump_return",
        TEMPERATURE,
        ("Rücklauftemperatur Wärmepumpe", "Heat pump return temperature"),
        "°C",
    ),
    _hybrid(
        "TempSysReturn",
        "hybrid_system_return",
        TEMPERATURE,
        ("Rücklauftemperatur Anlage", "System return temperature"),
        "°C",
    ),
    _hybrid(
        "TempMaxHp",
        "hybrid_heat_pump_max_supply",
        TEMPERATURE,
        (
            "Höchste Vorlauftemperatur Wärmepumpe",
            "Heat pump maximum supply temperature",
        ),
        "°C",
    ),
    _hybrid(
        "HighestPermittedTemp",
        "hybrid_highest_permitted",
        TEMPERATURE,
        ("Höchste zulässige Temperatur", "Highest permitted temperature"),
        "°C",
        flag=(
            "Höchste zulässige Temperatur erreicht",
            "Highest permitted temperature reached",
        ),
    ),
    _hybrid(
        "LowOutdoorTempStopHeating",
        "hybrid_heating_stop_low",
        TEMPERATURE,
        ("Heizgrenze untere Außentemperatur", "Heating limit, low outdoor temperature"),
        "°C",
        flag=(
            "Heizstopp wegen niedriger Außentemperatur",
            "Heating stopped by low outdoor temperature",
        ),
    ),
    _hybrid(
        "HighOutdoorTempStopHeating",
        "hybrid_heating_stop_high",
        TEMPERATURE,
        ("Heizgrenze obere Außentemperatur", "Heating limit, high outdoor temperature"),
        "°C",
        flag=(
            "Heizstopp wegen hoher Außentemperatur",
            "Heating stopped by high outdoor temperature",
        ),
    ),
    _hybrid(
        "PumpSpeedSetp",
        "hybrid_pump_speed_setpoint",
        PERCENT,
        ("Pumpendrehzahl Sollwert", "Pump speed setpoint"),
        "%",
    ),
    _hybrid(
        "ODUMONITOR.CompressorSpeedCurr",
        "hybrid_odu_compressor_speed",
        PERCENT,
        ("Verdichterdrehzahl Außeneinheit", "Outdoor unit compressor speed"),
        "%",
    ),
    _hybrid(
        "CAN.CompressorSetp",
        "hybrid_compressor_setpoint",
        PERCENT,
        ("Verdichterdrehzahl Sollwert", "Compressor speed setpoint"),
        "%",
    ),
    _hybrid(
        "CAN.PositionVR1",
        "hybrid_valve_vr1",
        PERCENT,
        ("Ventilstellung VR1", "Valve position VR1"),
        "%",
    ),
    _hybrid(
        "TimeTillNextCompressorStart",
        "hybrid_time_to_compressor_start",
        DURATION,
        ("Zeit bis zum Verdichterstart", "Time until compressor start"),
        "min",
    ),
    _hybrid(
        "TimeTillNextCompressorStop",
        "hybrid_time_to_compressor_stop",
        DURATION,
        ("Zeit bis zum Verdichterstopp", "Time until compressor stop"),
        "min",
    ),
    _hybrid(
        "OpTimeCompressorHeating",
        "hybrid_compressor_hours_heating",
        DURATION,
        ("Verdichterlaufzeit Heizen", "Compressor run time heating"),
        "h",
    ),
    _hybrid(
        "OpTimeCompressorDHW",
        "hybrid_compressor_hours_hot_water",
        DURATION,
        ("Verdichterlaufzeit Warmwasser", "Compressor run time hot water"),
        "h",
    ),
    _hybrid(
        "SmartGridMode",
        "hybrid_smart_grid_mode",
        MODE,
        ("Smart-Grid-Modus", "Smart grid mode"),
    ),
    _hybrid(
        "EmergencyModeStatus",
        "hybrid_emergency_mode",
        READING,
        ("Notbetrieb", "Emergency mode"),
    ),
    _hybrid("PumpOn", "hybrid_pump", FLAG, ("Pumpe", "Pump")),
    _hybrid("CAN.DefrostActive", "hybrid_defrost", FLAG, ("Abtauen", "Defrosting")),
    _hybrid(
        "CAN.FourWayValveActive",
        "hybrid_four_way_valve",
        FLAG,
        ("Vierwegeventil", "Four-way valve"),
    ),
    _hybrid(
        "CAN.DripPanHeaterActive",
        "hybrid_drip_pan_heater",
        FLAG,
        ("Kondensatwannenheizung", "Drip pan heater"),
    ),
    _hybrid(
        "LowNoiseModeActive",
        "hybrid_low_noise",
        FLAG,
        ("Leiser Betrieb", "Low-noise operation"),
    ),
    _hybrid(
        "HeatUpSequence",
        "hybrid_heat_up_sequence",
        FLAG,
        ("Aufheizsequenz", "Heat-up sequence"),
    ),
    _hybrid(
        "CompSpeedLimitedDueToSilentMode",
        "hybrid_compressor_limited",
        FLAG,
        (
            "Verdichter durch leisen Betrieb begrenzt",
            "Compressor limited by low-noise operation",
        ),
    ),
)


HS = r"/heatSources/hs\d+"
REFRIGERANT = HS + "/refrigerant"
VENTILATION = r"/ventilation/zone\d+"
CATALOG: tuple[LocalOnlyDefinition, ...] = (
    # Enabled: readings useful in everyday operation.
    _define(
        "/heatSources/currentSupplySetpoint",
        "supply_setpoint",
        TEMPERATURE,
        ("Vorlauf-Sollwert aktuell", "Current supply setpoint"),
        enabled=True,
    ),
    _define(
        HS + "/pumpVolumeFlow",
        "volume_flow",
        FLOW,
        ("Volumenstrom", "Volume flow"),
        enabled=True,
    ),
    _define(
        "/heatSources/compressor/powerElecActual",
        "compressor_power",
        POWER,
        ("Elektrische Leistung Verdichter", "Compressor electric power"),
        enabled=True,
    ),
    _define(
        "/heatSources/eHeater/powerElecActual",
        "electric_heater_power",
        POWER,
        ("Elektrische Leistung Heizstab", "Electric heater power"),
        enabled=True,
    ),
    _define(
        REFRIGERANT + "/status",
        "refrigerant_status",
        STATE,
        ("Kältekreis-Status", "Refrigerant circuit status"),
        enabled=True,
        translation_key="refrigerant_status",
        options=(
            "idle",
            "startup",
            "heating",
            "cooling",
            "defrost",
            "passive_defrost",
            "shutoff",
            "alarm",
            "test_mode",
            # C5: also declared by the gateways (for example firmware 15.00.01).
            "off",
            "dhw",
            "pool",
            "pool_heat",
        ),
    ),
    _define(
        "/heatSources/dhw/cylinderTemperature",
        "cylinder_temperature",
        TEMPERATURE,
        ("Speichertemperatur", "Cylinder temperature"),
        # C3: the same temperature as the hot water temperature entity.
        enabled=False,
    ),
    _define(
        r"/heatingCircuits/hc\d+/pumpStatus",
        "circuit_pump",
        STATE,
        ("Heizkreispumpe", "Heating circuit pump"),
        enabled=True,
        translation_key="on_off_state",
        options=("on", "off"),
    ),
    _define(
        r"/dhwCircuits/dhw\d+/tdrunningStatus",
        "disinfection_running",
        STATE,
        ("Thermische Desinfektion läuft", "Thermal disinfection running"),
        enabled=True,
        translation_key="on_off_state",
        options=("on", "off"),
    ),
    _define(
        "/system/powerConstraints/silentMode/status",
        "silent_mode",
        STATE,
        ("Flüstermodus", "Silent mode"),
        enabled=True,
        translation_key="pv_contact_state",
        options=("active", "inactive"),
    ),
    _define(
        "/system/powerConstraints/externalInputs/appliancePowerBlock/status",
        "utility_block",
        STATE,
        ("EVU-Sperre", "Utility power block"),
        enabled=True,
        translation_key="pv_contact_state",
        options=("active", "inactive"),
    ),
    # Disabled: technical readings of the refrigerant circuit and drives.
    *(
        _define(REFRIGERANT + "/" + name, key, TEMPERATURE, names)
        for name, key, names in (
            (
                "compressorTemp",
                "compressor_temperature",
                ("Verdichtertemperatur", "Compressor temperature"),
            ),
            (
                "highPressureTemp",
                "high_pressure_temperature",
                ("Hochdrucktemperatur (JR1)", "High-pressure temperature (JR1)"),
            ),
            (
                "hotGasTemp",
                "hot_gas_temperature",
                ("Heißgastemperatur (TR6)", "Hot gas temperature (TR6)"),
            ),
            (
                "liquidPipeCoolingTemp",
                "liquid_line_cooling",
                (
                    "Verflüssigertemperatur Kühlen (TR4)",
                    "Condenser temperature cooling (TR4)",
                ),
            ),
            (
                "liquidPipeHeatingTemp",
                "liquid_line_heating",
                (
                    "Verflüssigertemperatur Heizen (TR3)",
                    "Condenser temperature heating (TR3)",
                ),
            ),
            (
                "lowPressureTemp",
                "low_pressure_temperature",
                ("Niederdrucktemperatur (JR0)", "Low-pressure temperature (JR0)"),
            ),
            (
                "suctionGasTemp",
                "suction_gas_temperature",
                ("Temperatur Saugleitung (TR5)", "Suction line temperature (TR5)"),
            ),
        )
    ),
    _define(
        "/heatSources/sensors/evaporatorTemp_tl2",
        "evaporator_temperature",
        TEMPERATURE,
        ("Luftansaugtemperatur (TL2)", "Air intake temperature (TL2)"),
    ),
    _define(
        HS + "/oduFanSpeed",
        "outdoor_fan_speed",
        PERCENT,
        ("Lüfterdrehzahl Außeneinheit (PL3)", "Outdoor unit fan speed (PL3)"),
    ),
    _define(
        REFRIGERANT + "/heatCarrierPumpSpeed",
        "heat_carrier_pump_speed",
        PERCENT,
        ("Drehzahl primäre Heizungspumpe (PC0)", "Primary heating pump speed (PC0)"),
    ),
    _define(
        REFRIGERANT + "/compressorActualSpeed",
        "compressor_speed",
        PERCENT,
        ("Verdichterdrehzahl", "Compressor speed"),
    ),
    _define(
        "/heatSources/additionalHeater/flowTemp",
        "additional_heater_flow",
        TEMPERATURE,
        ("Vorlauftemperatur primär (TC1)", "Primary supply temperature (TC1)"),
    ),
    # Second sources of readings above: offered, but disabled.
    _define(
        REFRIGERANT + "/compressorElecPowerActual",
        "compressor_power_refrigerant",
        POWER,
        (
            "Elektrische Leistung Verdichter (Kältekreis)",
            "Compressor electric power (refrigerant circuit)",
        ),
    ),
    _define(
        "/heatSources/eHeater/powerElecActualLowResolution",
        "electric_heater_power_coarse",
        POWER,
        ("Elektrische Leistung Heizstab (grob)", "Electric heater power (coarse)"),
    ),
    # Disabled: readings of a ventilation unit (P31). The units below are the
    # documented ones; a reading in another unit is not shown.
    *(
        _define(VENTILATION + "/" + name, key, kind, names)
        for name, key, kind, names in (
            (
                "sensors/exhaustTemp",
                "ventilation_exhaust_temperature",
                TEMPERATURE,
                ("Fortlufttemperatur", "Exhaust air temperature"),
            ),
            (
                "sensors/extractTemp",
                "ventilation_extract_temperature",
                TEMPERATURE,
                ("Ablufttemperatur", "Extract air temperature"),
            ),
            (
                "sensors/outdoorTemp",
                "ventilation_outdoor_temperature",
                TEMPERATURE,
                ("Außenlufttemperatur", "Outdoor air temperature"),
            ),
            (
                "sensors/supplyTemp",
                "ventilation_supply_temperature",
                TEMPERATURE,
                ("Zulufttemperatur", "Supply air temperature"),
            ),
            (
                "sensors/internalHumidity",
                "ventilation_indoor_humidity",
                HUMIDITY,
                ("Luftfeuchte innen", "Indoor humidity"),
            ),
            (
                "sensors/externalHumidity",
                "ventilation_outdoor_humidity",
                HUMIDITY,
                ("Luftfeuchte außen", "Outdoor humidity"),
            ),
            (
                "maxRelativeHumidity",
                "ventilation_highest_humidity",
                HUMIDITY,
                ("Höchste relative Luftfeuchte", "Highest relative humidity"),
            ),
            (
                "sensors/internalAirQuality",
                "ventilation_indoor_air_quality",
                CONCENTRATION,
                ("Luftqualität innen", "Indoor air quality"),
            ),
            (
                "sensors/externalAirQuality",
                "ventilation_outdoor_air_quality",
                CONCENTRATION,
                ("Luftqualität außen", "Outdoor air quality"),
            ),
            (
                "maxIndoorAirQuality",
                "ventilation_highest_air_quality",
                CONCENTRATION,
                ("Höchster Luftqualitätswert innen", "Highest indoor air quality"),
            ),
            (
                "exhaustFanPower",
                "ventilation_exhaust_fan_power",
                PERCENT,
                ("Leistung Abluftventilator", "Exhaust fan power"),
            ),
            (
                "supplyFanPower",
                "ventilation_supply_fan_power",
                PERCENT,
                ("Leistung Zuluftventilator", "Supply fan power"),
            ),
            (
                "exhaustFanSpeed",
                "ventilation_exhaust_fan_speed",
                ROTATION,
                ("Drehzahl Abluftventilator", "Exhaust fan speed"),
            ),
            (
                # The documented unit of this reading is malformed.
                "sensors/supplyFanRotation",
                "ventilation_supply_fan_speed",
                READING,
                ("Drehzahl Zuluftventilator", "Supply fan speed"),
            ),
            (
                "summerBypass/flapPower",
                "ventilation_bypass_flap",
                PERCENT,
                ("Sommer-Bypass Klappe", "Summer bypass flap"),
            ),
            (
                "elAuxHeaterPower",
                "ventilation_aux_heater_power",
                PERCENT,
                ("Leistung Zusatzheizregister", "Auxiliary heater power"),
            ),
            (
                "electricalAuxHeaterPower",
                "ventilation_aux_heater_power_alternative",
                PERCENT,
                (
                    "Leistung Zusatzheizregister (zweite Angabe)",
                    "Auxiliary heater power (second reading)",
                ),
            ),
            (
                "applianceRunTime",
                "ventilation_run_time",
                DURATION,
                ("Betriebszeit Lüftungsgerät", "Ventilation unit run time"),
            ),
            (
                "filter/remainingTime",
                "ventilation_filter_remaining",
                DURATION,
                ("Filter-Restlaufzeit", "Filter remaining time"),
            ),
        )
    ),
    _define(
        VENTILATION + "/exhaustFanLevel",
        "ventilation_exhaust_fan_level",
        STATE,
        ("Abluft-Lüfterstufe", "Exhaust fan level"),
        translation_key="ventilation_fan_level",
        options=("off", "min", "red", "nom", "max", "dem"),
    ),
    # Generic and disabled: meaning not documented in detail.
    _define(
        r"/heatSources/externalInputs/input(\d+)/state",
        "external_input",
        GENERIC,
        ("Externer Eingang {}", "External input {}"),
    ),
    # C10-b: before the generic signals, so these get their meaning.
    *HYBRID_SIGNALS,
    # C7: reviewed signals of the controller.
    *CONTROLLER_SIGNALS,
    # C7-6: which heating circuit a room controller is assigned to.
    _define(
        "/devices/uhc/assignedTo",
        "room_controller_assignment",
        GENERIC,
        ("Zuordnung Raumregler", "Room controller assignment"),
    ),
    _define(r"/signals/(.+)", "signal", GENERIC, ("Signal {}", "Signal {}")),
)


# Option sets with translated state texts (C7-2).
_MAP_TRANSLATIONS = {
    frozenset({"heating", "idle", "cooling"}): "signal_season_mode",
    frozenset({"idle", "heat", "cool"}): "signal_heat_cool",
    frozenset({"idle", "planned", "active"}): "signal_holiday_slot",
    frozenset({"true", "false"}): "signal_true_false",
    frozenset({"off", "on"}): "on_off_state",
}
_TRUE_FALSE = frozenset({"true", "false"})
_MARKER_STATE = re.compile(
    r"(?:^|_)(?:na|open|short|invalid\d*|overflow|underflow|low_?flow|error)(?:_|$)"
)


# Generic controller signals: the catch-all and the named ones.
_SIGNAL_KEYS = frozenset(
    {"signal"}
    | {
        definition.key
        for definition in CONTROLLER_SIGNALS
        if definition.kind == GENERIC and definition.key.endswith("_signal")
    }
)

# Definitions without a reviewed meaning of their own (C7): their entities
# are diagnostic, whatever form the answer gives them.
_UNREVIEWED_KEYS = (
    frozenset({"spec", "external_input", "room_controller_assignment"}) | _SIGNAL_KEYS
)


def diagnostic(definition: LocalOnlyDefinition) -> bool:
    """C7: an unreviewed reading is a diagnostic entity."""
    return definition.kind == GENERIC or definition.key in _UNREVIEWED_KEYS


def static_unit(
    definition: LocalOnlyDefinition,
) -> tuple[str | None, SensorDeviceClass | None]:
    """C7: the unit and class a definition has without a reading, if fixed."""
    if definition.default_unit is None:
        return None, None
    return unit_and_class(definition, definition.default_unit) or (None, None)


def is_marker_state(name: object) -> bool:
    """A declared state that marks an invalid reading (for example NA_OPEN)."""
    return bool(_MARKER_STATE.search(state_key(name) or ""))


# #73: the attribute of a flow reading below its sensor's measuring range.
MEASURING_RANGE = "measuring_range"
BELOW_MEASURING_RANGE = "below"
_LOW_FLOW_STATE = re.compile(r"(?:^|_)low_?flow(?:_|$)")


def below_measuring_range(observation: Observation) -> bool:
    """#73: the reading is the declared low-flow state ("lowflow",
    "LOW_FLOW"): the flow is too low for the sensor to measure, not zero."""
    return any(
        _LOW_FLOW_STATE.search(state_key(name) or "") for name in observation.matched
    )


# #76: a setpoint an energy manager uses. While none is connected, the
# controller reports a placeholder (0 °C) instead of a setpoint.
ENERGY_MANAGER_STATUS_PATH = "/heatSources/emStatus"
_ENERGY_MANAGER_SETPOINT = re.compile(r"/signals/(?:[^/.]+\.)*EM\.[^/]*Setp")


def energy_manager_setpoint(path: str) -> bool:
    """#76: a controller signal with a setpoint of the energy manager."""
    return _ENERGY_MANAGER_SETPOINT.fullmatch(path) is not None


def energy_manager_not_connected(status: object) -> bool:
    """#76: the energy management status reports no connected manager."""
    return (state_key(status) or "").replace("_", "") == "notconnected"


def generic_form(
    definition: LocalOnlyDefinition, observation: object
) -> LocalOnlyDefinition | None:
    """C7: the form of a reading without a reviewed meaning.

    Every reading becomes an entity (disabled), except free text: text with
    declared values is a state, text true/false an on/off state, other text
    is never shown (it could carry an identifier). A number with a state map
    of real names and no unit is a state; with a unit it stays a number,
    whose marker states (for example NA_OPEN) show no value.
    """
    data = getattr(observation, "data", None)
    if getattr(observation, "type", None) == "stringValue":
        declared = _declared(getattr(data, "allowed_values", ()))
        if declared and not _TRUE_FALSE.issuperset(declared):
            return replace(
                definition,
                kind=STATE,
                options=declared,
                translation_key=_MAP_TRANSLATIONS.get(frozenset(declared)),
            )
        if declared or state_key(getattr(data, "value", None)) in _TRUE_FALSE:
            # O107: declared or answered true/false is an on/off state.
            return replace(
                definition,
                kind=FLAG,
                options=_FLAG_OPTIONS,
                translation_key="on_off_state",
            )
        return None
    names = tuple(getattr(state, "name", "") for state in getattr(data, "states", ()))
    if (
        names
        and canonical_unit(getattr(data, "unit", None)) is None
        and not all(is_marker_state(name) for name in names)
    ):
        keys = _declared(names)
        if keys and _TRUE_FALSE.issuperset(keys):
            # O107: a TRUE/FALSE map is an on/off state, as a text answer is.
            return replace(
                definition,
                kind=FLAG,
                options=_FLAG_OPTIONS,
                translation_key="on_off_state",
            )
        return replace(
            definition,
            kind=STATE,
            options=keys,
            translation_key=_MAP_TRANSLATIONS.get(frozenset(keys)),
        )
    return definition


def for_reading(
    definition: LocalOnlyDefinition,
    observation: object,
    *,
    registered: str | None = None,
) -> LocalOnlyDefinition | None:
    """C10-b: the form of a hybrid manager signal, or None while undecided.

    A signal that is either a temperature or an on/off state keeps the form
    its registered entity has (``registered``: ``TEMPERATURE`` or ``FLAG``),
    so a restart never switches it. Otherwise only decisive evidence
    decides: a temperature unit means the temperature, a text answer or a
    state map with other than error-marker states the on/off form. An error
    marker or a unitless 0/1 decides nothing; until evidence arrives no
    entity is created.

    A text answer of any other hybrid signal behaves like a generic text
    signal: a state with its declared values, or no entity. The smart grid
    mode also accepts the SG-Ready names as text.
    """
    data = getattr(observation, "data", None)
    text = getattr(observation, "type", None) == "stringValue"
    alternate = definition.alternate
    if alternate is not None:
        if registered == FLAG:
            return alternate
        if registered == TEMPERATURE:
            return definition
        if canonical_unit(getattr(data, "unit", None)) in ("°C", "K"):
            return definition
        names = {getattr(state, "name", None) for state in getattr(data, "states", ())}
        if text or names - ERROR_MARKERS - {None}:
            return alternate
        return None
    if definition.kind == GENERIC:
        return generic_form(definition, observation)
    if not definition.unverified or not text or definition.kind == FLAG:
        return definition
    value = getattr(data, "value", None)
    if definition.kind == MODE and state_key(value) in _SMART_GRID_NAMES:
        return definition
    declared = _declared(getattr(data, "allowed_values", ()))
    if not declared:
        return None  # like a generic text signal without declared values
    return replace(definition, kind=STATE, options=declared, translation_key=None)


# Readings ask for the same few hundred paths again and again; the catalog is
# fixed, so the scan is cached (like the pair catalog).
@lru_cache(maxsize=4096)
def definition_for(path: str) -> LocalOnlyDefinition | None:
    """The local-only definition of a path, or None (also for identifiers)."""
    for definition in CATALOG:
        if (match := definition.pattern.fullmatch(path)) is None:
            continue
        if definition.key == "signal" and (
            "/" in match.group(1) or identifier_name(match.group(1))
        ):
            return None
        return definition
    return None


def identifier_name(name: str) -> bool:
    """A reading name that denotes an identifier, address or secret."""
    from .resource_catalog import identifier_name as shared

    return shared(name)


def entity_name(definition: LocalOnlyDefinition, path: str, language: str) -> str:
    match = definition.pattern.fullmatch(path)
    name = definition.names[0 if language.casefold().startswith("de") else 1]
    detail = match.group(1) if match is not None and match.groups() else ""
    return name.format(detail) if "{}" in name else name


def unit_and_class(
    definition: LocalOnlyDefinition, unit: str | None
) -> tuple[str | None, SensorDeviceClass | None] | None:
    """Home Assistant unit and device class of a reading, None if it does not fit."""
    if definition.kind in ENUM_KINDS:
        return None, SensorDeviceClass.ENUM
    canonical = canonical_unit(unit)
    if definition.unverified:
        # C10-b: the gateway's own unit decides; the expected one only
        # stands in while the answer declares none.
        if canonical is None:
            canonical = definition.default_unit
        if canonical is None:
            return None, None
        if (mapped := _UNITS.get(canonical)) is None:
            return canonical, None
        if definition.kind == READING:
            return mapped[0], None
        return mapped
    if definition.kind == READING:
        # Without an established unit: keep only a known unit, never a class.
        known = _UNITS.get(canonical) if canonical is not None else None
        return (known[0] if known is not None else None), None
    if canonical is None:
        return (None, None) if definition.kind == GENERIC else None
    mapped = _UNITS.get(canonical)
    if mapped is None:
        # A unit the integration does not map is kept as reported for generic
        # signals only (a unit the decoder does not know is never shown, C7).
        return (canonical, None) if definition.kind == GENERIC else None
    if definition.kind == GENERIC and canonical == "K":
        # A generic kelvin value is usually a difference, not a temperature.
        return "K", None
    if definition.kind == PERCENT:
        return mapped if mapped[0] == PERCENTAGE else None
    if definition.kind == HUMIDITY:
        return (
            (PERCENTAGE, SensorDeviceClass.HUMIDITY)
            if mapped[0] == PERCENTAGE
            else None
        )
    if definition.kind in _KIND_UNITS:
        return mapped if mapped[0] == _KIND_UNITS[definition.kind] else None
    if definition.kind in _KIND_CLASSES:
        return mapped if mapped[1] is _KIND_CLASSES[definition.kind] else None
    return mapped


# Units of a generic signal that make it a measurement (#80); run and
# remaining times stay without statistics, like the reviewed ones.
_MEASUREMENT_UNITS = frozenset(
    unit
    for unit, device_class in _UNITS.values()
    if device_class is not SensorDeviceClass.DURATION
)


def state_class(
    definition: LocalOnlyDefinition, unit: str | None = None
) -> SensorStateClass | None:
    """Statistics for measurements: reviewed ones, and controller signals
    whose reading has the unit of a measurement (``unit``, as Home Assistant
    shows it). States, text, times and other generic readings get none."""
    # Run and remaining times are counters, not measurements: no statistics.
    if definition.kind in (
        TEMPERATURE,
        POWER,
        FLOW,
        PERCENT,
        HUMIDITY,
        ROTATION,
        CONCENTRATION,
    ):
        return SensorStateClass.MEASUREMENT
    if (
        definition.kind == GENERIC
        and definition.key in _SIGNAL_KEYS
        and unit in _MEASUREMENT_UNITS
    ):
        return SensorStateClass.MEASUREMENT
    return None


def state_options(
    definition: LocalOnlyDefinition, observation: Observation | None
) -> tuple[str, ...]:
    """C5: the reviewed states, then those the device declares (bounded)."""
    if definition.kind == FLAG:
        return definition.options
    declared = _declared(observation.declared if observation is not None else ())
    if definition.kind == STATE and observation is not None:
        declared = (*declared, *_declared(observation.state_names))
    if definition.kind == MODE:
        names = _declared(observation.state_names if observation is not None else ())
        declared = tuple(_SMART_GRID_NAMES.get(name, name) for name in names)
    return tuple(dict.fromkeys((*definition.options, *declared)))[:_MAX_STATES]


def state_key(value: object) -> str | None:
    """C5: a state as an option key: casefolded, other characters as one
    ``_``, bounded; None when nothing usable remains."""
    if not isinstance(value, str):
        return None
    return _state_key(value)


_NOT_KEY = re.compile(r"[^a-z0-9_]+")
_REPEATED_UNDERSCORE = re.compile(r"_+")


@lru_cache(maxsize=4096)
def _state_key(value: str) -> str | None:
    if not value.isprintable():
        return None
    key = _NOT_KEY.sub("_", value.casefold())
    key = _REPEATED_UNDERSCORE.sub("_", key).strip("_")
    return key if 0 < len(key) <= _MAX_STATE_LENGTH else None


def _declared(values: Iterable[object]) -> tuple[str, ...]:
    """C5: declared states as option keys, deduplicated and bounded."""
    keys = (state_key(item) for item in values)
    return tuple(dict.fromkeys(key for key in keys if key is not None))[:_MAX_STATES]


def flag_state(value: object) -> str | None:
    """on/off of a true/false, on/off or 1/0 value; None for anything else."""
    return _flag(value)


def _flag(value: object) -> str | None:
    if isinstance(value, bool):
        return "on" if value else "off"
    raw = str(value) if isinstance(value, str | int | Decimal) else ""
    text = state_key(raw) or ""
    if text in ("true", "on", "1", "active", "yes"):
        return "on"
    if text in ("false", "off", "0", "inactive", "no"):
        return "off"
    return None


def _smart_grid(observation: Observation) -> str | None:
    """C10-b: the gateway's declared state if it has a state map, else 0..4.

    Without a state map 0 means not used and 1..4 the SG-Ready modes, like
    the controller's own declaration (LOE-3, not verified without a map).
    """
    if observation.state_names:
        if not observation.matched:
            return None
        name = state_key(observation.matched[0])
        return _SMART_GRID_NAMES.get(name, name) if name is not None else None
    value = observation.value
    if isinstance(value, str):
        # Text: only the SG-Ready names are a mode.
        return _SMART_GRID_NAMES.get(state_key(value) or "")
    if (
        observation.status is ValueStatus.VALID
        and isinstance(value, int | Decimal)
        and not isinstance(value, bool)
        and value == int(value)
        and 0 <= int(value) < len(_SMART_GRID_NUMBERS)
    ):
        return _SMART_GRID_NUMBERS[int(value)]
    return None


def native_value(
    definition: LocalOnlyDefinition, observation: Observation
) -> int | float | str | None:
    """The shown value; None for device states, markers and other shapes."""
    if definition.kind == MODE:
        # Shown only within the options, so a state is never outside them.
        mode = _smart_grid(observation)
        return mode if mode in state_options(definition, observation) else None
    if definition.kind == FLAG and observation.matched:
        # A declared state of an on/off signal.
        return _flag(observation.matched[0])
    if definition.kind == STATE and observation.matched:
        # C7-2: a declared state, shown by its name.
        key = state_key(observation.matched[0])
        return key if key in state_options(definition, observation) else None
    if observation.status is ValueStatus.UNKNOWN_UNIT and definition.kind == GENERIC:
        pass  # C7: the number is shown; its unknown unit never is
    elif observation.status is not ValueStatus.VALID or observation.parts:
        return None
    value: Any = observation.value
    if isinstance(value, bool) or value is None:
        return None
    if definition.kind == STATE:
        key = state_key(value)
        return key if key in state_options(definition, observation) else None
    if definition.kind == FLAG:
        return _flag(value)
    # Some documented examples report the error marker without declaring it as
    # a state; like the cloud readings, never show it as a value.
    if (
        definition.kind == TEMPERATURE
        and isinstance(value, int | float | Decimal)
        and Decimal(str(value)) in _TEMPERATURE_MARKERS
    ):
        return None
    if isinstance(value, Decimal):
        value = (
            int(value) if observation.resource_type == "integerValue" else float(value)
        )
    if isinstance(value, int | float):
        return value
    # Text is never shown: in a reviewed measurement it is another shape, and
    # in a generic signal it could carry an identifier.
    return None


def numeric(observation: Observation | None) -> bool:
    """A generic entry is offered only for a valid numeric reading."""
    if observation is None or observation.status is not ValueStatus.VALID:
        return False
    value = observation.value
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)
