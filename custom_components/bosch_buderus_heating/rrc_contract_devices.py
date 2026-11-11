"""RRC 2.0 contract of the radio devices: details and settings.

Owned by work package W6 (CT-E04, CT-E05, CT-E09). See
``docs/dev/rrc-packages.md`` for what each table does and
``rrc_device_settings`` for the leaves and their shapes.

- Child lock: a generic switch over the exact strings "true"/"false" (S2).
- Temperature offset and open window sensitivity: numbers within the
  advertised limits and step; their entities live in
  ``rrc_device_setting_entities`` (the offset only while the device uses it,
  the sensitivity as a selection of the levels its limits allow).
- Valve opening, its maximum, the temperature at the valve, the valve state
  and the whisper mode: read-only generic sensors, disabled by default.
- Valve problem: a binary sensor from ``etrv/status``.

Not offered (maintainer decisions W6-E1, W6-E2): the valve calibration, and
any write of the whisper mode until its value format is known.
"""

from __future__ import annotations

from types import MappingProxyType

from .poll_group import PollGroup
from .rrc_contract_base import RRC_ID, RrcContract
from .rrc_device_settings import (
    CHILD_LOCK,
    DEVICE_LEAVES,
    MAX_VALVE_POSITION,
    OFFSET,
    SENSITIVITY,
    STATUS,
    VALVE_POSITION,
    VALVE_STATE,
    VALVE_TEMPERATURE,
    WHISPER_MODE,
    diagnostics,
    leaf_pattern,
    sensitivity_check,
    unused_check,
)
from .write_policies import EnumWritePolicy, NumberWritePolicy

CHILD_LOCK_PATTERN = leaf_pattern(CHILD_LOCK)
OFFSET_PATTERN = leaf_pattern(OFFSET)
SENSITIVITY_PATTERN = leaf_pattern(SENSITIVITY, valve_only=True)
STATUS_PATTERN = leaf_pattern(STATUS, valve_only=True)
VALVE_POSITION_PATTERN = leaf_pattern(VALVE_POSITION, valve_only=True)
MAX_VALVE_POSITION_PATTERN = leaf_pattern(MAX_VALVE_POSITION, valve_only=True)
VALVE_TEMPERATURE_PATTERN = leaf_pattern(VALVE_TEMPERATURE, valve_only=True)
VALVE_STATE_PATTERN = leaf_pattern(VALVE_STATE, valve_only=True)
WHISPER_MODE_PATTERN = leaf_pattern(WHISPER_MODE, valve_only=True)
# Every leaf this contract reads below a listed device.
DEVICE_SETTING_PATTERN = (
    rf"^/devices/device{RRC_ID}/(?:etrv|wth|bwth|thermostat)/"
    r"(?:childLock/enabled|offset|openWindowDetection/sensitivity|status"
    r"|valvePosition|maxValvePosition|temperatureActual|valveState"
    r"|whisperMode/enabled)$"
)

# S1, S2: only a writable string resource with exactly "true" and "false";
# without allowedValues the current value must be one of them.
CHILD_LOCK_POLICY = EnumWritePolicy(
    CHILD_LOCK_PATTERN,
    frozenset({"stringValue"}),
    frozenset({"true", "false"}),
    enabled_by_default=False,
    options_rule="exact",
    fallback_options=("true", "false"),
    on_value="true",
    off_value="false",
)
# S3, S4: limits and step only from the metadata (the step counted from
# minValue, off-grid values refused); no number entity of the generic
# platform, see rrc_device_setting_entities.
OFFSET_POLICY = NumberWritePolicy(
    OFFSET_PATTERN,
    "C",
    0.5,
    enabled_by_default=False,
    create_entity=False,
    strict_grid=True,
    value_check=unused_check,
)
SENSITIVITY_POLICY = NumberWritePolicy(
    SENSITIVITY_PATTERN,
    "C",
    0.5,
    enabled_by_default=False,
    create_entity=False,
    strict_grid=True,
    value_check=sensitivity_check,
)

CONTRACT = RrcContract(
    name="devices",
    enum_policies=(CHILD_LOCK_POLICY,),
    number_policies=(OFFSET_POLICY, SENSITIVITY_POLICY),
    blocked=MappingProxyType(
        {
            # W6-E1: a trigger without a readable state, never confirmed.
            rf"^/devices/device{RRC_ID}/etrv/valveCalibration$": (
                "rrc_valve_calibration_not_offered"
            ),
            # W6-E2: read only until the value format is known.
            WHISPER_MODE_PATTERN: "rrc_whisper_mode_read_only",
        }
    ),
    # P20, CT-H01: radio devices, batteries and valves at most every 15 min.
    poll_groups=MappingProxyType({DEVICE_SETTING_PATTERN: PollGroup.SLOW}),
    names=MappingProxyType(
        {
            CHILD_LOCK_PATTERN: ("Kindersicherung", "Child lock"),
            OFFSET_PATTERN: ("Temperatur-Offset", "Temperature offset"),
            SENSITIVITY_PATTERN: (
                "Empfindlichkeit Fenster-offen-Erkennung",
                "Open window detection sensitivity",
            ),
            STATUS_PATTERN: ("Ventilproblem", "Valve problem"),
            VALVE_POSITION_PATTERN: ("Ventilöffnung", "Valve opening"),
            MAX_VALVE_POSITION_PATTERN: (
                "Maximale Ventilöffnung",
                "Maximum valve opening",
            ),
            VALVE_TEMPERATURE_PATTERN: (
                "Temperatur am Ventil",
                "Temperature at the valve",
            ),
            VALVE_STATE_PATTERN: ("Ventilzustand", "Valve state"),
            WHISPER_MODE_PATTERN: ("Flüstermodus", "Whisper mode"),
        }
    ),
    entity_keys=MappingProxyType(
        {
            CHILD_LOCK_PATTERN: MappingProxyType({"switch": "rrc_child_lock"}),
            OFFSET_PATTERN: MappingProxyType({"number": "rrc_temperature_offset"}),
            SENSITIVITY_PATTERN: MappingProxyType({"select": "rrc_window_sensitivity"}),
            STATUS_PATTERN: MappingProxyType({"binary_sensor": "rrc_valve_problem"}),
            VALVE_POSITION_PATTERN: MappingProxyType({"sensor": "rrc_valve_position"}),
            MAX_VALVE_POSITION_PATTERN: MappingProxyType(
                {"sensor": "rrc_max_valve_position"}
            ),
            VALVE_TEMPERATURE_PATTERN: MappingProxyType(
                {"sensor": "rrc_valve_temperature"}
            ),
            VALVE_STATE_PATTERN: MappingProxyType({"sensor": "rrc_valve_state"}),
            WHISPER_MODE_PATTERN: MappingProxyType({"sensor": "rrc_whisper_mode"}),
        }
    ),
    # The generic sensor of each setting becomes a disabled diagnostic one.
    read_only_mirrors=(CHILD_LOCK_PATTERN, OFFSET_PATTERN, SENSITIVITY_PATTERN),
    device_leaves=DEVICE_LEAVES,
    diagnostics=diagnostics,
    # The status list stays "observed": it is a list, which no generic
    # entity reads; its binary sensor is the package's own.
    understood=(
        CHILD_LOCK_PATTERN,
        OFFSET_PATTERN,
        SENSITIVITY_PATTERN,
        VALVE_POSITION_PATTERN,
        MAX_VALVE_POSITION_PATTERN,
        VALVE_TEMPERATURE_PATTERN,
        VALVE_STATE_PATTERN,
        WHISPER_MODE_PATTERN,
    ),
    default_enabled=MappingProxyType(
        {
            VALVE_POSITION_PATTERN: False,
            MAX_VALVE_POSITION_PATTERN: False,
            VALVE_TEMPERATURE_PATTERN: False,
            VALVE_STATE_PATTERN: False,
            WHISPER_MODE_PATTERN: False,
        }
    ),
    categories=MappingProxyType(
        {
            CHILD_LOCK_PATTERN: "config",
            OFFSET_PATTERN: "config",
            SENSITIVITY_PATTERN: "config",
            MAX_VALVE_POSITION_PATTERN: "diagnostic",
            VALVE_STATE_PATTERN: "diagnostic",
            WHISPER_MODE_PATTERN: "diagnostic",
        }
    ),
    icons=MappingProxyType(
        {
            CHILD_LOCK_PATTERN: "mdi:lock",
            OFFSET_PATTERN: "mdi:thermometer-plus",
            SENSITIVITY_PATTERN: "mdi:window-open-variant",
            VALVE_POSITION_PATTERN: "mdi:valve",
            MAX_VALVE_POSITION_PATTERN: "mdi:valve-open",
            VALVE_STATE_PATTERN: "mdi:valve",
            WHISPER_MODE_PATTERN: "mdi:volume-low",
        }
    ),
)
