"""Synthetic climate profiles and adversarial command/metadata scenarios."""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.components.climate.const import ClimateEntityFeature as Feature
from homeassistant.components.climate.const import HVACMode
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.bacon import rac
from custom_components.bosch_buderus_heating.bacon.models import Device
from custom_components.bosch_buderus_heating.climate import (
    BaconClimate,
    async_setup_entry,
)
from custom_components.bosch_buderus_heating.ha_compat import temperature_unit
from tests.test_bacon_dhw import responding
from tests.test_bacon_session import emit, environment

FIXTURES = Path(__file__).parent / "fixtures/contracts/bacon-rac"


def configured(name="ac_186i3", serial="serial-A", account=None):
    account = account or environment()
    data = json.loads((FIXTURES / (name + ".json")).read_text())
    for suffix, key in (
        ("topics/info", "info"),
        ("topics/meta", "metadata"),
        ("topics/sensor", "sensor"),
        ("shadows/state/get/accepted", "shadow"),
    ):
        emit(account, suffix, data[key], serial)
    return account, BaconClimate(account, serial), data


def update(account, data, **patch):
    data["shadow"]["version"] += 1
    data["shadow"]["state"]["reported"].update(patch)
    emit(account, "shadows/state/get/accepted", data["shadow"])


@pytest.mark.parametrize("profile", ["ac_186i3", "cl_7000i"])
def test_family_profiles_have_individual_features_and_no_inferred_action(profile):
    account, entity, data = configured(profile)
    assert entity.available and entity.hvac_mode == HVACMode.COOL
    assert entity.native_current_temperature == 22.5
    assert entity.native_target_temperature == 22
    assert temperature_unit(entity) == "°C"
    assert entity.hvac_action is None
    assert entity.hvac_modes == (
        [HVACMode.OFF, *rac.MODES]
        if profile == "ac_186i3"
        else [HVACMode.OFF, HVACMode.COOL, HVACMode.DRY, HVACMode.FAN_ONLY]
    )
    assert entity.fan_modes == (
        list(rac.FANS) if profile == "ac_186i3" else ["auto", "low", "high"]
    )
    assert entity.min_temp == (17 if profile == "ac_186i3" else 16)
    assert entity.max_temp == 30
    assert entity.target_temperature_step == (1 if profile == "ac_186i3" else 2)
    assert entity.swing_mode == "off"
    assert entity.swing_horizontal_mode == ("on" if profile == "ac_186i3" else None)
    assert bool(entity.supported_features & Feature.SWING_HORIZONTAL_MODE) == (
        profile == "ac_186i3"
    )
    assert entity.supported_features & Feature.TARGET_TEMPERATURE
    assert not entity.supported_features & Feature.PRESET_MODE
    diagnostics = account.diagnostics()
    assert diagnostics["air_conditioners"]["midea_climate"] == {
        "identified": 1,
        "metadata_current": 1,
        "sensor_current": 1,
    }
    for private in (
        "serial-A",
        data["info"]["appliance"]["type"],
        "22.5",
        "customTitle",
        "tempSetpoint",
    ):
        assert private not in json.dumps(diagnostics)


@pytest.mark.parametrize("prefix", rac.PREFIXES)
def test_documented_type_prefixes(prefix):
    assert rac.supported(Device("test", model=prefix + " model"))


@pytest.mark.parametrize(
    "model",
    [
        None,
        "",
        "Midea",
        "Sharp",
        "RAC",
        "xClimate Series",
        "Hot Water Series",
        "hpwh_vw_v1",
        "climate Series",
    ],
)
def test_unproven_models_do_not_match(model):
    assert not rac.supported(Device("test", model=model))


@pytest.mark.parametrize(
    "unit,value,expected",
    [(u, 22.5, 22.5) for u in rac.CELSIUS]
    + [(u, 72.5, 22.5) for u in rac.FAHRENHEIT]
    + [(None, 20, None), ("K", 293, None), ("F", 72.5, None)],
)
def test_sensor_units_are_explicit(unit, value, expected):
    account, entity, data = configured()
    data["metadata"]["topics"]["sensor"]["roomTemperature"]["unit"] = unit
    emit(account, "topics/meta", data["metadata"])
    emit(
        account,
        "topics/sensor",
        {"items": [{"timestamp": 3, "roomTemperature": value}]},
    )
    assert entity.native_current_temperature == expected


@pytest.mark.parametrize(
    "value", [None, True, "22.5", [], {}, -32768, 32767, 10**1000, -51, 71]
)
def test_invalid_temperature_is_unknown(value):
    _, entity, _ = configured()
    # Direct contract check also covers values rejected by the JSON transport.
    entity.device.sensor["roomTemperature"] = value
    assert entity.native_current_temperature is None


@pytest.mark.parametrize(
    "patch",
    [
        {"min": 23},
        {"max": 22},
        {"min": True},
        {"max": "30"},
        {"min": 10**1000},
        {"min": 25, "max": 20},
    ],
)
def test_invalid_sensor_bounds(patch):
    account, entity, data = configured()
    data["metadata"]["topics"]["sensor"]["roomTemperature"].update(patch)
    emit(account, "topics/meta", data["metadata"])
    assert entity.native_current_temperature is None


@pytest.mark.parametrize(
    "patch",
    [
        {"unit": "degF"},
        {"unit": None},
        {"min": None},
        {"min": True},
        {"max": 51},
        {"min": 30},
        {"step": 0},
        {"step": 0.5},
        {"step": None},
        {"step": 100},
    ],
)
async def test_invalid_target_metadata_removes_control_and_blocks_writes(patch):
    account, entity, data = configured()
    data["metadata"]["shadows"]["state"]["tempSetpoint"].update(patch)
    emit(account, "topics/meta", data["metadata"])
    assert entity.native_target_temperature is None
    assert not entity.supported_features & Feature.TARGET_TEMPERATURE
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=24)
    account._mqtt.publish.assert_not_called()


@pytest.mark.parametrize(
    "value", [None, True, "24", 24.5, 15, 31, float("nan"), float("inf"), 10**1000]
)
async def test_bad_service_targets_do_not_publish(value):
    account, entity, _ = configured()
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=value)
    account._mqtt.publish.assert_not_called()


async def test_integer_grid_default_step_and_conversion_noise():
    account, entity, data = configured("cl_7000i")
    writes = responding(account, data)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=23)
    await entity.async_set_temperature(temperature=24.000000000000004)
    assert writes == [{"tempSetpoint": 24}]
    data["metadata"]["shadows"]["state"]["tempSetpoint"].pop("step")
    emit(account, "topics/meta", data["metadata"])
    assert entity.target_temperature_step == 1
    await entity.async_set_temperature(temperature=23)
    assert writes[-1] == {"tempSetpoint": 23}


@pytest.mark.parametrize("ha_mode", rac.MODES)
async def test_mode_and_target_semantics(ha_mode):
    account, entity, data = configured()
    update(account, data, powerEnabled=False)
    writes = responding(account, data)
    await entity.async_set_hvac_mode(ha_mode)
    assert writes == [{"powerEnabled": True, "opMode": rac.MODES[ha_mode]}]
    assert entity.hvac_mode == ha_mode and entity.hvac_action is None
    if ha_mode in (HVACMode.DRY, HVACMode.FAN_ONLY):
        assert not entity.supported_features & Feature.TARGET_TEMPERATURE
        with pytest.raises(ServiceValidationError):
            await entity.async_set_temperature(temperature=24)
    else:
        await entity.async_set_temperature(temperature=24)
        assert writes[-1] == {"tempSetpoint": 24}


async def test_narrow_commands_preserve_other_fields_and_other_device():
    account, entity, data = configured()
    _, other, _ = configured("cl_7000i", "serial-B", account)
    writes = responding(account, data)
    original = copy.deepcopy(entity.device.state.reported)
    await entity.async_turn_off()
    assert entity.hvac_mode == HVACMode.OFF
    await entity.async_turn_on()
    await entity.async_set_fan_mode("quiet")
    await entity.async_set_swing_mode("on")
    await entity.async_set_swing_horizontal_mode("off")
    await entity.async_set_temperature(temperature=24, hvac_mode=HVACMode.HEAT)
    assert writes == [
        {"powerEnabled": False},
        {"powerEnabled": True},
        {"fanSpeed": "quiet"},
        {"vSwingEnabled": True},
        {"hSwingEnabled": False},
        {"powerEnabled": True, "opMode": "heat", "tempSetpoint": 24},
    ]
    for field in ("ecoEnabled", "onTimestamp", "customTitle"):
        assert entity.device.state.reported[field] == original[field]
    assert other.hvac_mode == HVACMode.COOL and other.native_target_temperature == 22


@pytest.mark.parametrize(
    "method,value",
    [
        ("async_set_hvac_mode", HVACMode.HEAT),
        ("async_set_hvac_mode", "new-mode"),
        ("async_set_fan_mode", "quiet"),
        ("async_set_fan_mode", "new-fan"),
        ("async_set_swing_mode", "auto"),
        ("async_set_swing_horizontal_mode", "on"),
        ("async_set_swing_horizontal_mode", "auto"),
    ],
)
async def test_unoffered_commands_never_publish(method, value):
    account, entity, _ = configured("cl_7000i")
    with pytest.raises(ServiceValidationError):
        await getattr(entity, method)(value)
    account._mqtt.publish.assert_not_called()


async def test_invalid_combined_command_has_no_partial_effect():
    account, entity, _ = configured("cl_7000i")
    for kwargs in (
        {"temperature": 24, "hvac_mode": HVACMode.HEAT},
        {"temperature": 23, "hvac_mode": HVACMode.COOL},
        {"temperature": 24, "target_temp_high": 25},
        {"temperature": 24, "hvac_mode": HVACMode.FAN_ONLY},
    ):
        with pytest.raises(ServiceValidationError):
            await entity.async_set_temperature(**kwargs)
    account._mqtt.publish.assert_not_called()


@pytest.mark.parametrize(
    "field", ["powerEnabled", "opMode", "fanSpeed", "tempSetpoint", *rac.SWINGS]
)
async def test_missing_write_right_defaults_to_readonly(field):
    account, entity, data = configured()
    data["metadata"]["shadows"]["state"][field].pop("ro")
    emit(account, "topics/meta", data["metadata"])
    calls = {
        "powerEnabled": (entity.async_turn_off, ()),
        "opMode": (entity.async_set_hvac_mode, (HVACMode.HEAT,)),
        "fanSpeed": (entity.async_set_fan_mode, ("low",)),
        "tempSetpoint": (entity.async_set_temperature, ()),
        "vSwingEnabled": (entity.async_set_swing_mode, ("on",)),
        "hSwingEnabled": (entity.async_set_swing_horizontal_mode, ("off",)),
    }
    method, args = calls[field]
    with pytest.raises(ServiceValidationError):
        await method(*args, **({"temperature": 24} if field == "tempSetpoint" else {}))
    assert entity.hvac_mode == HVACMode.COOL
    account._mqtt.publish.assert_not_called()


@pytest.mark.parametrize(
    "change",
    ["limits", "rights", "options", "model", "offline", "missing_meta", "mode"],
)
async def test_fresh_read_revalidates_entire_combined_request(change):
    account, entity, data = configured()

    def mutate():
        meta = data["metadata"]["shadows"]["state"]
        if change == "limits":
            meta["tempSetpoint"]["max"] = 23
        elif change == "rights":
            meta["opMode"]["ro"] = True
        elif change == "options":
            meta["opMode"]["enum"] = ["cool"]
        elif change == "missing_meta":
            data["metadata"] = {}
        elif change == "mode":
            data["shadow"]["version"] += 1
            data["shadow"]["state"]["reported"]["opMode"] = "fan"
        else:
            info = copy.deepcopy(data["info"])
            info["timestamp"] += 1
            if change == "model":
                info["appliance"]["type"] = "cl_7000i replacement"
            else:
                info["online"] = False
            emit(account, "topics/info", info)
        emit(account, "topics/meta", data["metadata"])

    writes = responding(account, data, after_read=mutate)
    with pytest.raises(HomeAssistantError):
        await entity.async_set_temperature(
            temperature=24, **({} if change == "mode" else {"hvac_mode": HVACMode.HEAT})
        )
    assert writes == []


async def test_concurrent_app_change_is_preserved_and_commands_are_serialized():
    account, entity, data = configured()

    def app_change():
        data["shadow"]["version"] += 1
        data["shadow"]["state"]["reported"]["ecoEnabled"] = False

    writes = responding(account, data, after_read=app_change)
    await asyncio.gather(
        entity.async_set_temperature(temperature=24), entity.async_set_fan_mode("low")
    )
    assert writes == [{"tempSetpoint": 24}, {"fanSpeed": "low"}]
    assert entity.device.state.reported["ecoEnabled"] is False
    assert entity.native_target_temperature == 24 and entity.fan_mode == "low"


@pytest.mark.parametrize(
    "outcome", ["desired", "retained", "disconnect", "reject", "timeout"]
)
async def test_unconfirmed_command_becomes_unavailable_without_replay(outcome):
    account, entity, data = configured()
    writes = responding(account, data, outcome=outcome)
    with pytest.raises(HomeAssistantError) as error:
        await entity.async_set_temperature(temperature=24)
    assert error.value.translation_key == "bacon_command_failed"
    assert writes == [{"tempSetpoint": 24}]
    if outcome not in ("desired", "disconnect"):
        # C50: the immediate state read shows the actual, unchanged set point.
        assert entity.available and entity.native_target_temperature == 22
        assert writes == [{"tempSetpoint": 24}]
        return
    assert not entity.available and entity.hvac_mode is None
    assert entity.supported_features == 0
    if outcome != "disconnect":
        data["shadow"]["version"] += 2
        emit(account, "shadows/state/get/accepted", data["shadow"])
        assert entity.available and entity.native_target_temperature == 22
        assert writes == [{"tempSetpoint": 24}]


def test_unknown_modes_missing_fields_and_model_change():
    account, entity, data = configured()
    update(
        account,
        data,
        opMode="unknown",
        fanSpeed=None,
        hSwingEnabled=None,
        powerEnabled=None,
    )
    assert entity.hvac_mode is None and entity.fan_mode is None
    assert not entity.hvac_modes and not entity.fan_modes
    assert entity.swing_horizontal_mode is None
    update(account, data, powerEnabled=True)
    assert entity.hvac_mode is None
    emit(
        account,
        "topics/info",
        {"timestamp": 3, "online": True, "appliance": {"type": "Sharp"}},
    )
    assert not entity.available and entity.native_current_temperature is None
    assert entity.hvac_mode is None and entity.supported_features == 0


def test_freshness_ordering_and_reconnection_are_independent():
    account, entity, data = configured()
    identity = entity.unique_id
    entity.device.sensor_received_at -= 3601
    for key in entity.device.sensor_fields_received_at:
        entity.device.sensor_fields_received_at[key] -= 3601
    for stamp in (1, 2, 3):
        emit(
            account,
            "topics/sensor",
            {"items": [{"timestamp": stamp, "roomTemperature": 25}]},
            retained=stamp == 3,
        )
    assert (
        entity.native_current_temperature is None
        and entity.native_target_temperature == 22
    )
    emit(account, "topics/sensor", {"items": [{"timestamp": 3, "roomTemperature": 25}]})
    entity.device.state_received_at -= 901
    for key in entity.device.state_fields_received_at:
        entity.device.state_fields_received_at[key] -= 901
    assert (
        entity.native_current_temperature == 25
        and entity.native_target_temperature is None
    )
    assert entity.hvac_mode is None and entity.supported_features == 0
    assert entity.min_temp == entity.max_temp == 0
    assert entity.target_temperature_step is None
    account._disconnected()
    assert not entity.available
    account.connected = True
    account.generation += 1
    emit(account, "topics/info", {**data["info"], "timestamp": 3})
    emit(account, "shadows/state/get/accepted", data["shadow"])
    assert entity.native_target_temperature is None
    emit(account, "topics/meta", data["metadata"], retained=True)
    assert (
        entity.native_target_temperature == 22
        and entity.native_current_temperature is None
    )
    emit(account, "topics/sensor", {"items": [{"timestamp": 4, "roomTemperature": 26}]})
    assert entity.native_current_temperature == 26
    assert BaconClimate(account, entity.serial).unique_id == identity


async def test_late_discovery_and_recovery_do_not_duplicate_entities(hass):
    account = environment()
    entities, cleanup = [], []
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(bacon=account, coordinators=()),
        async_on_unload=cleanup.append,
    )
    await async_setup_entry(hass, entry, entities.extend)
    assert not entities
    _, _, data = configured(account=account)
    assert len(entities) == 1
    entity = entities[0]
    emit(account, "topics/meta", {})
    assert entity.supported_features == 0
    emit(account, "topics/meta", data["metadata"])
    assert entity.supported_features & Feature.SWING_MODE
    assert len(entities) == 1
    for remove in cleanup:
        remove()
    configured("cl_7000i", "serial-B", account)
    assert len(entities) == 1
    await async_setup_entry(
        hass,
        SimpleNamespace(runtime_data=SimpleNamespace(bacon=None, coordinators=())),
        Mock(),
    )


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"opMode": "cool"},
        {"opMode": "off", "powerEnabled": True},
        {"powerEnabled": 1},
        {"fanSpeed": "low", "vSwingEnabled": True},
        {"hSwingEnabled": 1},
        {"customTitle": "x"},
        {"ecoEnabled": True},
        {"tempSetpoint": True},
    ],
)
def test_contract_has_no_generic_write_escape(patch):
    _, entity, _ = configured()
    assert not rac.command_valid(entity.device, patch, entity.device.model)


async def test_unknown_stored_mode_cannot_be_powered_on():
    account, entity, data = configured()
    update(account, data, powerEnabled=False, opMode="unknown")
    assert not entity.supported_features & Feature.TURN_ON
    with pytest.raises(ServiceValidationError):
        await entity.async_turn_on()
    account._mqtt.publish.assert_not_called()


async def test_duplicate_or_desired_ack_does_not_complete_delayed_command():
    account, entity, data = configured()
    account._timeout = 2
    published = asyncio.Event()
    writes = []

    async def publish(topic, payload, **kwargs):
        if topic.endswith("/get"):
            emit(account, "shadows/state/get/accepted", data["shadow"])
            return
        writes.append(json.loads(payload)["state"]["desired"])
        emit(account, "shadows/state/update/accepted", data["shadow"])
        desired = copy.deepcopy(data["shadow"])
        desired["version"] = 2
        desired["state"]["desired"] = writes[-1]
        emit(account, "shadows/state/update/accepted", desired)
        published.set()

    account._mqtt.publish.side_effect = publish
    task = asyncio.create_task(entity.async_set_temperature(temperature=24))
    try:
        await published.wait()
        await asyncio.sleep(0)
        assert not task.done() and entity.native_target_temperature == 22
        response = copy.deepcopy(data["shadow"])
        response["version"] = 3
        response["state"]["reported"]["tempSetpoint"] = 24
        emit(account, "shadows/state/update/accepted", response)
        await task
        assert entity.native_target_temperature == 24
        assert writes == [{"tempSetpoint": 24}]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def with_eco(account, data, meta):
    """Publish metadata with or without an Eco declaration."""
    data["metadata"]["shadows"]["state"].pop("ecoEnabled", None)
    if meta is not None:
        data["metadata"]["shadows"]["state"]["ecoEnabled"] = meta
    emit(account, "topics/meta", data["metadata"])


@pytest.mark.parametrize("meta", [None, {}, {"ro": True}, {"ro": "false"}])
async def test_eco_preset_absent_unless_declared_writable(meta):
    account, entity, data = configured()
    with_eco(account, data, meta)
    assert entity.preset_modes is None and entity.preset_mode is None
    assert not entity.supported_features & Feature.PRESET_MODE
    assert not rac.command_valid(
        entity.device, {"ecoEnabled": False}, entity.device.model
    )
    with pytest.raises(ServiceValidationError):
        await entity.async_set_preset_mode("none")
    account._mqtt.publish.assert_not_called()


@pytest.mark.parametrize("value", [None, "on", 1])
async def test_eco_preset_absent_without_boolean_report(value):
    account, entity, data = configured()
    with_eco(account, data, {"ro": False})
    if value is None:
        data["shadow"]["state"]["reported"].pop("ecoEnabled")
        data["shadow"]["version"] += 1
        emit(account, "shadows/state/get/accepted", data["shadow"])
    else:
        update(account, data, ecoEnabled=value)
    assert entity.preset_modes is None and entity.preset_mode is None
    assert not entity.supported_features & Feature.PRESET_MODE
    with pytest.raises(ServiceValidationError):
        await entity.async_set_preset_mode("eco")
    account._mqtt.publish.assert_not_called()


async def test_eco_preset_sends_only_eco_and_reads_back():
    account, entity, data = configured()
    with_eco(account, data, {"ro": False})
    update(account, data, fanSpeed="turbo")
    writes = responding(account, data)
    assert entity.preset_modes == ["none", "eco"]
    assert entity.supported_features & Feature.PRESET_MODE
    assert entity.preset_mode == "eco" and entity.fan_mode == "turbo"
    await entity.async_set_preset_mode("none")
    assert entity.preset_mode == "none"
    await entity.async_set_preset_mode("eco")
    assert entity.preset_mode == "eco"
    assert writes == [{"ecoEnabled": False}, {"ecoEnabled": True}]
    # The fan speed named turbo is a separate setting and stays untouched.
    assert entity.fan_mode == "turbo" and "turbo" in entity.fan_modes
    with pytest.raises(ServiceValidationError):
        await entity.async_set_preset_mode("boost")
    assert len(writes) == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"ecoEnabled": 1},
        {"ecoEnabled": True, "fanSpeed": "turbo"},
        {"ecoEnabled": True, "powerEnabled": True},
    ],
)
def test_eco_command_is_a_single_boolean_field(patch):
    account, entity, data = configured()
    with_eco(account, data, {"ro": False})
    assert rac.command_valid(entity.device, {"ecoEnabled": True}, entity.device.model)
    assert not rac.command_valid(entity.device, patch, entity.device.model)


async def test_unconfirmed_eco_change_keeps_reported_value():
    account, entity, data = configured()
    with_eco(account, data, {"ro": False})
    writes = responding(account, data, outcome="desired")
    with pytest.raises(HomeAssistantError):
        await entity.async_set_preset_mode("none")
    assert writes == [{"ecoEnabled": False}]
    assert rac.reported(entity.device, "ecoEnabled") is True
    assert not entity.available
