"""Synthetic purifier profiles, semantic boundaries and confirmed narrow writes."""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.components.fan import FanEntityFeature as Feature
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.bacon import purifier
from custom_components.bosch_buderus_heating.bacon.models import Device
from custom_components.bosch_buderus_heating.bacon.purifier_entities import (
    BaconPurifierColor,
    BaconPurifierNumber,
    BaconPurifierSensor,
    BaconPurifierSwitch,
    track_entities,
)
from custom_components.bosch_buderus_heating.fan import (
    BaconPurifierFan,
    async_setup_entry,
)
from tests.test_bacon_dhw import responding
from tests.test_bacon_session import emit, environment

FIXTURES = Path(__file__).parent / "fixtures/contracts/bacon-purifier"


def configured(name="air_6001i", serial="serial-A", account=None):
    account = account or environment()
    data = json.loads((FIXTURES / (name + ".json")).read_text())
    for suffix, key in (
        ("topics/info", "info"),
        ("topics/meta", "metadata"),
        ("topics/sensor", "sensor"),
        ("shadows/state/get/accepted", "shadow"),
    ):
        emit(account, suffix, data[key], serial)
    return account, BaconPurifierFan(account, serial), data


def update(account, data, **patch):
    data["shadow"]["version"] += 1
    data["shadow"]["state"]["reported"].update(patch)
    emit(account, "shadows/state/get/accepted", data["shadow"])


@pytest.mark.parametrize(
    "name,prefix", [(name, p) for name, ps in purifier.FAMILIES.items() for p in ps]
)
def test_only_proven_family_prefixes(name, prefix):
    assert purifier.family(Device("test", model=prefix + " model")) == name


@pytest.mark.parametrize(
    "model",
    [
        None,
        "",
        "Air 4000",
        "air_1001i",
        "Air 1000i",
        "xAir 1001i",
        "Air 6000i",
        "Climate Series",
        "Hot Water Series",
    ],
)
def test_unproven_type_has_no_purifier(model):
    assert purifier.family(Device("test", model=model)) is None


@pytest.mark.parametrize("profile", ["air_1001i", "air_6001i"])
def test_profile_features_discrete_speed_and_diagnostic_redaction(profile):
    account, fan, data = configured(profile)
    assert fan.available and fan.is_on is True and fan.preset_mode == "auto"
    assert fan.preset_modes == (
        ["auto", "manual"] if profile == "air_1001i" else list(purifier.MODES)
    )
    assert (
        fan.supported_features
        == Feature.TURN_ON | Feature.TURN_OFF | Feature.PRESET_MODE
    )
    assert "percentage" not in fan.state_attributes
    stage = BaconPurifierNumber(account, "serial-A", "fan_speed")
    assert stage.native_value == 1 and stage.native_min_value == 1
    assert stage.native_max_value == (3 if profile == "air_1001i" else 5)
    assert stage.native_step == (1 if profile == "air_1001i" else 2)
    assert stage.native_unit_of_measurement is None and stage.mode == "box"
    assert BaconPurifierSensor(account, "serial-A", "filter_life").native_value == 50
    assert (
        BaconPurifierSensor(account, "serial-A", "filter_life").state_class
        == "measurement"
    )
    assert BaconPurifierSensor(account, "serial-A", "filter_status").native_value == (
        "ok" if profile == "air_1001i" else "warning"
    )
    assert (
        BaconPurifierSensor(account, "serial-A", "air_quality").native_value == "fair"
    )
    tvoc = BaconPurifierSensor(account, "serial-A", "tvoc")
    assert tvoc.available == (profile == "air_6001i")
    assert tvoc.device_class is None and tvoc.native_unit_of_measurement is None
    diagnostics = account.diagnostics()
    assert diagnostics["air_purifiers"][profile] == {
        "identified": 1,
        "metadata_current": 1,
        "sensor_current": 1,
    }
    for private in (
        "serial-A",
        data["info"]["appliance"]["type"],
        "filterCapacity",
        "20.5",
        "customTitle",
    ):
        assert private not in json.dumps(diagnostics)


@pytest.mark.parametrize("family", purifier.FAMILIES)
@pytest.mark.parametrize("value", [-1, 0, 1, 20, 21, 30, 31, 50, 51, 100, 101])
def test_family_filter_thresholds_are_remaining_percentage(family, value):
    _, fan, _ = configured()
    fan.device.model = purifier.FAMILIES[family][0]
    fan.device.sensor["filterCapacity"] = value
    error, warning = (0, 30) if family == "air_1001i" else (20, 50)
    if 0 <= value <= 100:
        assert purifier.reading(fan.device, "filter_life") == (value, "%")
        assert purifier.reading(fan.device, "filter_status") == (
            "error" if value <= error else "warning" if value <= warning else "ok",
            None,
        )
    else:
        assert purifier.reading(fan.device, "filter_life") is None
        assert purifier.reading(fan.device, "filter_status") is None


@pytest.mark.parametrize(
    "key,field,value,unit,expected",
    [
        ("temperature", "roomTemperature", 68.9, "degF", (20.5, "°C")),
        ("temperature", "roomTemperature", 20.5, "Celsius", (20.5, "°C")),
        ("temperature", "roomTemperature", 20.5, "K", None),
        ("temperature", "roomTemperature", 71, "degC", None),
        ("humidity", "relHumidity", 45.5, "%", (45.5, "%")),
        ("humidity", "relHumidity", 45.5, None, None),
        ("humidity", "relHumidity", 101, "%", None),
        ("pm2_5", "pm2_5", 12, "µg/m3", (12, "µg/m³")),
        ("pm2_5", "pm2_5", 1, "mg/m3", (1000, "µg/m³")),
        ("pm2_5", "pm2_5", 1, None, None),
        ("pm2_5", "pm2_5", 12, "ppm", None),
        ("pm2_5", "pm2_5", 1.5, "µg/m3", None),
        ("pm2_5", "pm2_5", -1, "µg/m3", None),
        ("tvoc", "tvoc", 3, None, (3, None)),
        ("tvoc", "tvoc", 3, "ppb", None),
        ("tvoc", "tvoc", 3, "µg/m3", None),
        ("filter_life", "filterCapacity", 50, "h", None),
        ("filter_life", "filterCapacity", 50, None, (50, "%")),
        ("filter_life", "filterCapacity", 50.5, "%", None),
        ("air_quality", "airQuality", 1, None, ("good", None)),
        ("air_quality", "airQuality", 4, None, ("bad", None)),
        ("air_quality", "airQuality", 0, None, None),
        ("air_quality", "airQuality", 2, "µg/m3", None),
    ],
)
def test_measurements_categories_and_units_are_not_interchangeable(
    key, field, value, unit, expected
):
    _, fan, _ = configured()
    fan.device.sensor[field] = value
    fan.device.metadata["topics"]["sensor"][field] = {"unit": unit}
    actual = purifier.reading(fan.device, key)
    if expected is not None and isinstance(expected[0], float):
        assert actual[0] == pytest.approx(expected[0])
        assert actual[1] == expected[1]
    else:
        assert actual == expected


@pytest.mark.parametrize(
    "value", [None, True, "3", [], {}, float("nan"), float("inf"), 10**1000]
)
@pytest.mark.parametrize("key", purifier.SENSORS)
def test_malformed_readings_stay_unknown(key, value):
    _, fan, _ = configured()
    fan.device.sensor[purifier.SENSORS[key]] = value
    assert purifier.reading(fan.device, key) is None


@pytest.mark.parametrize(
    "bounds",
    [
        {"min": True},
        {"max": "20"},
        {"min": 10**1000},
        {"min": 51},
        {"max": 49},
        {"min": 60, "max": 40},
    ],
)
def test_malformed_or_violated_sensor_bounds(bounds):
    _, fan, _ = configured()
    fan.device.metadata["topics"]["sensor"]["filterCapacity"].update(bounds)
    assert purifier.reading(fan.device, "filter_life") is None


def test_pm_aliases_agree_or_stay_unknown():
    _, fan, _ = configured()
    device = fan.device
    meta = device.metadata["topics"]["sensor"]
    meta["pm2.5"] = meta.pop("pm2_5")
    device.sensor["pm2.5"] = device.sensor.pop("pm2_5")
    assert purifier.reading(device, "pm2_5") == (12, "µg/m³")
    meta["pm2_5"] = copy.deepcopy(meta["pm2.5"])
    device.sensor["pm2_5"] = 12
    assert purifier.reading(device, "pm2_5") == (12, "µg/m³")
    device.sensor["pm2_5"] = 13
    assert purifier.reading(device, "pm2_5") is None
    device.sensor["pm2_5"] = 12
    meta["pm2_5"]["unit"] = "mg/m3"
    assert purifier.reading(device, "pm2_5") is None


async def test_narrow_commands_preserve_other_settings_and_device():
    account, fan, data = configured()
    _, other, _ = configured("air_1001i", "serial-B", account)
    original = copy.deepcopy(fan.device.state.reported)
    writes = responding(account, data)
    await fan.async_turn_off()
    assert fan.is_on is False and fan.preset_mode == "auto"
    await fan.async_set_preset_mode("manual")
    assert fan.is_on is False  # Selecting a preset never implicitly powers on.
    await fan.async_turn_on(preset_mode="silent")
    stage = BaconPurifierNumber(account, "serial-A", "fan_speed")
    await stage.async_set_native_value(3)
    assert fan.preset_mode == "silent"  # The separate stage command preserves mode.
    for key in purifier.SWITCHES:
        switch = BaconPurifierSwitch(account, "serial-A", key)
        await switch.async_turn_on()
        assert switch.is_on is True
        await switch.async_turn_off()
        assert switch.is_on is False
    color = BaconPurifierColor(account, "serial-A", "light_color")
    await color.async_select_option("blue")
    assert color.current_option == "blue"
    light = BaconPurifierNumber(account, "serial-A", "light_intensity")
    await light.async_set_native_value(60)
    assert light.native_value == 60 and light.native_unit_of_measurement == "%"
    assert writes[:4] == [
        {"powerEnabled": False},
        {"opMode": "manual"},
        {"powerEnabled": True, "opMode": "silent"},
        {"fanSpeed": 3},
    ]
    assert writes[4:] == [
        p
        for field in purifier.SWITCHES.values()
        for p in ({field: True}, {field: False})
    ] + [{"lightMode": "blue"}, {"lightIntensity": 60}]
    for key in ("filterReset", "onTimer", "offTimestamp", "customTitle"):
        assert fan.device.state.reported[key] == original[key]
    assert other.is_on is True and other.preset_mode == "auto"


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"filterReset": True},
        {"offTimer": 10},
        {"customTitle": "x"},
        {"fanSpeed": 3, "opMode": "manual"},
        {"powerEnabled": False, "opMode": "auto"},
        {"opMode": "unknown"},
        {"lightMode": "off"},
        {"lightMode": "auto"},
        {"powerEnabled": 1},
        {"fanSpeed": True},
        {"fanSpeed": 2},
        {"lightIntensity": 55},
        {"childLockEnabled": "on"},
    ],
)
def test_unsupported_commands_have_no_generic_escape(patch):
    _, fan, _ = configured()
    assert not purifier.command_valid(fan.device, patch, fan.device.model)


@pytest.mark.parametrize(
    "value", [True, None, "3", 2, 3.5, 0, 101, float("nan"), float("inf"), 10**1000]
)
async def test_invalid_stage_never_publishes(value):
    account, _, _ = configured()
    with pytest.raises(ServiceValidationError):
        await BaconPurifierNumber(
            account, "serial-A", "fan_speed"
        ).async_set_native_value(value)
    account._mqtt.publish.assert_not_called()


@pytest.mark.parametrize(
    "patch",
    [
        {"ro": True},
        {"ro": None},
        {"min": None},
        {"min": True},
        {"min": 0},
        {"max": 101},
        {"max": 1},
        {"step": 0},
        {"step": 1.5},
        {"step": 10},
        {"unit": "%"},
    ],
)
async def test_bad_numeric_metadata_withdraws_control(patch):
    account, _, data = configured()
    data["metadata"]["shadows"]["state"]["fanSpeed"].update(patch)
    emit(account, "topics/meta", data["metadata"])
    stage = BaconPurifierNumber(account, "serial-A", "fan_speed")
    assert not stage.available and stage.native_value is None
    with pytest.raises(ServiceValidationError):
        await stage.async_set_native_value(3)
    account._mqtt.publish.assert_not_called()


@pytest.mark.parametrize(
    "action",
    ["percentage", "set_percentage", "unknown", "unoffered", "extra", "unknown_stored"],
)
async def test_invalid_fan_requests_do_not_publish(action):
    account, fan, data = configured("air_1001i")
    with pytest.raises(ServiceValidationError):
        if action == "percentage":
            await fan.async_turn_on(percentage=50)
        elif action == "set_percentage":
            await fan.async_set_percentage(50)
        elif action == "extra":
            await fan.async_turn_on(direction="reverse")
        elif action == "unknown_stored":
            update(account, data, powerEnabled=False, opMode="unknown")
            assert (
                fan.preset_mode is None and not fan.supported_features & Feature.TURN_ON
            )
            await fan.async_turn_on()
        else:
            await fan.async_set_preset_mode(
                "unknown" if action == "unknown" else "silent"
            )
    account._mqtt.publish.assert_not_called()


@pytest.mark.parametrize(
    "change", ["read_only", "options", "bounds", "model", "offline", "metadata"]
)
async def test_fresh_read_revalidates_device_and_capability(change):
    account, fan, data = configured()

    def mutate():
        if change == "read_only":
            data["metadata"]["shadows"]["state"]["fanSpeed"]["ro"] = True
        elif change == "options":
            data["metadata"]["shadows"]["state"]["opMode"]["enum"] = ["auto"]
        elif change == "bounds":
            data["metadata"]["shadows"]["state"]["fanSpeed"]["max"] = 2
        elif change == "model":
            fan.device.model = "air_4000i"
        elif change == "offline":
            fan.device.online = False
        elif change == "metadata":
            data["metadata"] = {}
        emit(account, "topics/meta", data["metadata"])

    writes = responding(account, data, after_read=mutate)
    with pytest.raises(HomeAssistantError):
        if change == "options":
            await fan.async_set_preset_mode("manual")
        else:
            await BaconPurifierNumber(
                account, "serial-A", "fan_speed"
            ).async_set_native_value(3)
    assert writes == []


@pytest.mark.parametrize(
    "outcome", ["desired", "retained", "timeout", "disconnect", "reject"]
)
async def test_unconfirmed_commands_are_not_replayed(outcome):
    account, fan, data = configured()
    writes = responding(account, data, outcome=outcome)
    with pytest.raises(HomeAssistantError) as error:
        await fan.async_set_preset_mode("manual")
    assert error.value.translation_key == "bacon_command_failed"
    if outcome not in ("desired", "disconnect"):
        # C50: the immediate state read shows the actual, unchanged mode.
        assert fan.available and fan.preset_mode == "auto"
        assert writes == [{"opMode": "manual"}]
        return
    assert writes == [{"opMode": "manual"}] and not fan.available
    assert fan.is_on is None and not fan.preset_modes and fan.supported_features == 0
    if outcome != "disconnect":
        data["shadow"]["version"] += 2
        emit(account, "shadows/state/get/accepted", data["shadow"])
        assert fan.available and fan.preset_mode == "auto"
    assert writes == [{"opMode": "manual"}]


def test_stream_freshness_ordering_and_snapshot_recovery():
    account, fan, data = configured()
    sensor = BaconPurifierSensor(account, "serial-A", "pm2_5")
    identity = sensor.unique_id
    fan.device.sensor_received_at -= 3601
    for key in fan.device.sensor_fields_received_at:
        fan.device.sensor_fields_received_at[key] -= 3601
    for timestamp, retained in [(1, False), (2, False), (3, True)]:
        emit(
            account,
            "topics/sensor",
            {"items": [{"timestamp": timestamp, "pm2_5": 30}]},
            retained=retained,
        )
    assert not sensor.available and fan.available
    emit(account, "topics/sensor", {"items": [{"timestamp": 3, "pm2_5": 30}]})
    assert sensor.native_value == 30
    assert BaconPurifierSensor(account, "serial-A", "humidity").native_value is None
    fan.device.state_received_at -= 901
    for key in fan.device.state_fields_received_at:
        fan.device.state_fields_received_at[key] -= 901
    assert not fan.available and sensor.available
    number = BaconPurifierNumber(account, "serial-A", "fan_speed")
    assert (
        number.native_min_value == number.native_max_value == 0
        and number.native_step == 1
    )
    account._disconnected()
    assert not sensor.available
    account.connected = True
    account.generation += 1
    emit(account, "topics/info", {**data["info"], "timestamp": 3})
    emit(account, "shadows/state/get/accepted", data["shadow"])
    assert not fan.available
    emit(account, "topics/meta", data["metadata"], retained=True)
    assert fan.available and not sensor.available
    emit(
        account,
        "topics/sensor",
        {"items": [{**data["sensor"]["items"][0], "timestamp": 4}]},
    )
    assert sensor.native_value == 12
    assert BaconPurifierSensor(account, "serial-A", "pm2_5").unique_id == identity
    emit(
        account,
        "topics/info",
        {"timestamp": 4, "online": True, "appliance": {"type": "unknown"}},
    )
    assert not sensor.available and not fan.available


async def test_late_metadata_discovery_withdrawal_and_recovery(hass):
    account = environment()
    added, cleanup = [], []
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(bacon=account), async_on_unload=cleanup.append
    )
    await async_setup_entry(hass, entry, added.extend)
    track_entities(entry, account, added.extend, BaconPurifierSwitch, purifier.SWITCHES)
    track_entities(
        entry, account, added.extend, BaconPurifierSensor, purifier.SENSORS, sensor=True
    )
    track_entities(entry, account, added.extend, BaconPurifierNumber, purifier.NUMBERS)
    track_entities(
        entry, account, added.extend, BaconPurifierColor, {"light_color": "lightMode"}
    )
    assert not added
    _, _, data = configured("air_1001i", account=account)
    assert len(added) == 7  # fan, child lock, stage and four readings
    emit(account, "topics/meta", {})
    assert not any(entity.available for entity in added if entity.key != "fan")
    assert added[0].is_on is True and added[0].supported_features == 0
    emit(account, "topics/meta", data["metadata"])
    assert len(added) == 7 and all(entity.available for entity in added)
    _, _, extended = configured()
    data["metadata"] = extended["metadata"]
    data["shadow"]["state"]["reported"].update(extended["shadow"]["state"]["reported"])
    update(account, data)
    emit(account, "topics/meta", data["metadata"])
    emit(
        account,
        "topics/sensor",
        {"items": [{**extended["sensor"]["items"][0], "timestamp": 3}]},
    )
    assert len(added) == 16 and all(entity.available for entity in added)
    for remove in cleanup:
        remove()
    configured("air_6001i", "serial-B", account)
    assert len(added) == 16
    await async_setup_entry(
        hass, SimpleNamespace(runtime_data=SimpleNamespace(bacon=None)), Mock()
    )


async def test_delayed_reported_confirmation_never_uses_desired_as_state():
    account, fan, data = configured()
    account._timeout = 2
    sent = asyncio.Event()
    writes = []

    async def publish(topic, payload, **kwargs):
        if topic.endswith("/get"):
            emit(account, "shadows/state/get/accepted", data["shadow"])
            return
        writes.append(json.loads(payload)["state"]["desired"])
        desired = copy.deepcopy(data["shadow"])
        desired["version"] += 1
        desired["state"]["desired"] = writes[-1]
        emit(account, "shadows/state/update/accepted", desired)
        sent.set()

    account._mqtt.publish.side_effect = publish
    task = asyncio.create_task(fan.async_turn_off())
    try:
        await sent.wait()
        await asyncio.sleep(0)
        assert not task.done() and fan.is_on is True
        response = copy.deepcopy(data["shadow"])
        response["version"] += 2
        response["state"]["reported"]["powerEnabled"] = False
        emit(account, "shadows/state/update/accepted", response)
        await task
        assert fan.is_on is False and writes == [{"powerEnabled": False}]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("option,wire", purifier.COLOR_OPTIONS.items())
async def test_colors_use_ha_safe_tokens_and_exact_device_codes(option, wire):
    account, _, data = configured()
    color = BaconPurifierColor(account, "serial-A", "light_color")
    update(account, data, lightMode="off")
    assert color.current_option is None
    writes = responding(account, data)
    await color.async_select_option(option)
    assert writes == [{"lightMode": wire}]
    assert color.current_option == option
    with pytest.raises(ServiceValidationError):
        await color.async_select_option("auto")
    assert writes == [{"lightMode": wire}]


async def test_missing_rights_read_only_switch_and_filter_reset_stay_unwritable():
    account, fan, data = configured()
    data["metadata"]["shadows"]["state"]["childLockEnabled"].pop("ro")
    emit(account, "topics/meta", data["metadata"])
    switch = BaconPurifierSwitch(account, "serial-A", "child_lock")
    assert not switch.available and switch.is_on is None
    with pytest.raises(ServiceValidationError):
        await switch.async_turn_on()
    with pytest.raises(ServiceValidationError):
        await fan.write({"filterReset": True})
    fan.device.model = None
    with pytest.raises(ServiceValidationError):
        await fan.async_turn_off()
    account._mqtt.publish.assert_not_called()
