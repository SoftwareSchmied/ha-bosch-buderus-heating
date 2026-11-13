"""Synthetic Midea/RWH contract cases, including adversarial metadata."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.components.water_heater import WaterHeaterEntityFeature as Feature
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.bacon import dhw
from custom_components.bosch_buderus_heating.bacon.dhw_status import (
    BaconDhwStatus,
    track_status,
)
from custom_components.bosch_buderus_heating.bacon.models import BaconError, Device
from custom_components.bosch_buderus_heating.const import DEFAULT_MANUFACTURER
from custom_components.bosch_buderus_heating.ha_compat import temperature_unit
from custom_components.bosch_buderus_heating.water_heater import (
    BaconWaterHeater,
    async_setup_entry,
)
from tests.test_bacon_session import emit, environment

FIXTURES = Path(__file__).parent / "fixtures/contracts/bacon-dhw"


def configured(name="midea", serial="serial-A", account=None):
    account = account or environment()
    data = json.loads((FIXTURES / (name + ".json")).read_text())
    for suffix, key in (
        ("topics/info", "info"),
        ("topics/meta", "metadata"),
        ("topics/sensor", "sensor"),
        ("shadows/state/get/accepted", "shadow"),
    ):
        emit(account, suffix, data[key], serial)
    return account, BaconWaterHeater(account, serial), data


def responding(
    account, data, serial="serial-A", *, after_read=None, outcome="reported"
):
    writes = []

    async def publish(topic, payload, **kwargs):
        assert kwargs == {"qos": 1, "retain": False}
        if topic.endswith("/get"):
            if after_read:
                after_read()
            emit(
                account,
                topic.rsplit(serial + "/", 1)[1] + "/accepted",
                data["shadow"],
                serial,
            )
            return
        assert topic.endswith(serial + "/shadows/state/update")
        patch = json.loads(payload)["state"]["desired"]
        writes.append(patch)
        response = copy.deepcopy(data["shadow"])
        response["version"] += 1
        if outcome == "reported":
            response["state"]["reported"].update(patch)
            emit(account, "shadows/state/update/accepted", response, serial)
            data["shadow"] = response
        elif outcome == "desired":
            response["state"]["desired"] = patch
            emit(account, "shadows/state/update/accepted", response, serial)
        elif outcome == "retained":
            response["state"]["reported"].update(patch)
            emit(
                account,
                "shadows/state/update/accepted",
                response,
                serial,
                retained=True,
            )
        elif outcome == "disconnect":
            account._disconnected()
        elif outcome == "reject":
            emit(account, "shadows/state/update/rejected", {"code": 400}, serial)

    account._mqtt.publish.side_effect = publish
    return writes


@pytest.mark.parametrize("name", ["midea", "rwh"])
def test_complete_family_readings_and_features(name):
    account, heater, data = configured(name)
    assert heater.available
    assert heater.native_current_temperature == 48
    assert heater.native_target_temperature == 50
    assert temperature_unit(heater) == "°C"
    assert (
        heater.supported_features == Feature.TARGET_TEMPERATURE | Feature.OPERATION_MODE
    )
    assert heater.operation_list == ["off", *dhw.MODES[name]]
    assert heater.min_temp == (35 if name == "midea" else 30)
    assert heater.max_temp == (65 if name == "midea" else 70)
    assert heater.target_temperature_step == (5 if name == "midea" else 1)
    for key in dhw.STATUS_FIELDS:
        entity = BaconDhwStatus(account, heater.serial, key)
        assert entity.available and entity.is_on is False
    diagnostic = json.dumps(account.diagnostics())
    for private in (
        "serial-A",
        data["info"]["appliance"]["type"],
        "private title",
        "currentTemp",
        "tempSetpoint",
    ):
        assert private not in diagnostic
    assert account.diagnostics()["water_heaters"][name] == {
        "identified": 1,
        "metadata_current": 1,
        "sensor_current": 1,
    }


@pytest.mark.parametrize(
    "name,prefix", [(n, p) for n, ps in dhw.FAMILIES.items() for p in ps]
)
def test_documented_model_prefixes(name, prefix):
    assert dhw.family(Device("test", model=prefix + " synthetic variant")) == name


@pytest.mark.parametrize(
    "model",
    [
        None,
        "",
        "Midea",
        "RWH",
        "WDDW2",
        "k40",
        "RAC",
        "xHot Water Series",
        "hot water series",
    ],
)
def test_unknown_and_other_families_do_not_match(model):
    assert dhw.family(Device("test", model=model)) is None


@pytest.mark.parametrize("value", [True, "48", 48.0, -1, 101, 32767, -32768])
def test_invalid_current_temperature_is_never_zero(value):
    account, heater, data = configured()
    data["sensor"]["items"][0].update(timestamp=3, currentTemp=value)
    emit(account, "topics/sensor", data["sensor"])
    assert heater.native_current_temperature is None


@pytest.mark.parametrize("unit", ["C", "°C", "degC", "Celsius"])
def test_every_celsius_spelling_of_the_device_families_is_accepted(unit):
    account, heater, data = configured()
    data["metadata"]["topics"]["sensor"]["currentTemp"]["unit"] = unit
    data["metadata"]["shadows"]["state"]["tempSetpoint"]["unit"] = unit
    emit(account, "topics/meta", data["metadata"])
    assert heater.native_current_temperature is not None
    assert heater.native_target_temperature is not None
    # One manufacturer value for every device family.
    assert heater.device_info["manufacturer"] == DEFAULT_MANUFACTURER


@pytest.mark.parametrize(
    "change",
    [
        {"unit": "F"},
        {"unit": None},
        {"unit": "K"},
        {"min": True},
        {"min": 60},
        {"max": 20},
    ],
)
def test_incompatible_temperature_metadata(change):
    account, heater, data = configured()
    data["metadata"]["topics"]["sensor"]["currentTemp"].update(change)
    emit(account, "topics/meta", data["metadata"])
    assert heater.native_current_temperature is None


@pytest.mark.parametrize(
    "change",
    [
        {"min": None},
        {"min": -1},
        {"max": 101},
        {"min": 66},
        {"step": 0},
        {"step": True},
        {"step": 31},
        {"step": 0.5},
        {"unit": "F"},
    ],
)
async def test_invalid_target_limits_disable_writes(change):
    account, heater, data = configured()
    data["metadata"]["shadows"]["state"]["tempSetpoint"].update(change)
    emit(account, "topics/meta", data["metadata"])
    assert heater.native_target_temperature is None
    assert not heater.supported_features & Feature.TARGET_TEMPERATURE
    assert heater.min_temp == heater.max_temp == 0
    assert heater.target_temperature_step is None
    with pytest.raises(ServiceValidationError):
        await heater.async_set_temperature(temperature=55)
    account._mqtt.publish.assert_not_called()


@pytest.mark.parametrize("name", ["midea", "rwh"])
async def test_each_mode_writes_only_own_contract_fields(name):
    account, heater, data = configured(name)
    writes = responding(account, data)
    for mode in ["off", *dhw.MODES[name]]:
        await heater.async_set_operation_mode(mode)
        assert heater.current_operation == mode
        expected = {"powerEnabled": mode != "off"}
        if mode != "off":
            expected["opMode"] = dhw.MODES[name][mode]
        assert writes[-1] == expected
    assert all("tempSetpoint" not in patch for patch in writes)
    assert data["shadow"]["state"]["reported"]["tempSetpoint"] == 50
    assert data["shadow"]["state"]["reported"]["vacationOnTimestamp"] == 0


@pytest.mark.parametrize("name", ["midea", "rwh"])
async def test_target_write_confirmation_and_noop(name):
    account, heater, data = configured(name, serial="serial-B")
    writes = responding(account, data, "serial-B")
    await heater.async_set_temperature(temperature=55.0)
    assert writes == [{"tempSetpoint": 55}]
    assert heater.native_target_temperature == 55
    await heater.async_set_temperature(temperature=55)
    assert len(writes) == 1
    assert account.devices["serial-A"].state is None


@pytest.mark.parametrize(
    "value", [None, "55", True, float("nan"), float("inf"), 55.5, 52, 34, 66, -32768]
)
async def test_invalid_target_rejected_before_network(value):
    account, heater, _ = configured()
    with pytest.raises(ServiceValidationError):
        await heater.async_set_temperature(temperature=value)
    account._mqtt.publish.assert_not_called()


async def test_combined_settings_and_huge_integer_do_not_partially_write():
    account, heater, _ = configured()
    with pytest.raises(ServiceValidationError) as error:
        await heater.async_set_temperature(temperature=55, operation_mode="off")
    assert error.value.translation_key == "bacon_separate_settings"
    with pytest.raises(ServiceValidationError):
        await heater.async_set_temperature(temperature=10**1000)
    account._mqtt.publish.assert_not_called()


@pytest.mark.parametrize(
    "outcome", ["desired", "retained", "disconnect", "reject", "timeout"]
)
async def test_unconfirmed_changes_never_appear_successful(outcome):
    account, heater, data = configured()
    writes = responding(account, data, outcome=outcome)
    with pytest.raises(HomeAssistantError) as error:
        await heater.async_set_temperature(temperature=55)
    assert error.value.translation_key == "bacon_command_failed"
    assert writes == [{"tempSetpoint": 55}]
    if outcome in ("desired", "disconnect"):
        # No newer full state arrives: the outcome stays unconfirmed.
        assert not heater.available
        assert heater.native_target_temperature is heater.current_operation is None
        assert heater.device.uncertain
    else:
        # C50: the immediate state read shows the actual, unchanged value.
        assert heater.available and not heater.device.uncertain
        assert heater.native_target_temperature == 50
    assert writes == [{"tempSetpoint": 55}]


@pytest.mark.parametrize(
    "change",
    [
        "limits",
        "readonly",
        "model",
        "options",
        "metadata_missing",
        "power_missing",
        "offline",
    ],
)
async def test_revalidate_after_fresh_read(change):
    account, heater, data = configured()

    def mutate():
        meta = data["metadata"]["shadows"]["state"]
        if change == "limits":
            meta["tempSetpoint"]["max"] = 50
        elif change == "readonly":
            meta["tempSetpoint"]["ro"] = True
        elif change == "options":
            meta["opMode"]["enum"] = ["eHeater"]
        elif change == "metadata_missing":
            data["metadata"] = {}
        elif change == "power_missing":
            data["shadow"]["state"]["reported"].pop("powerEnabled")
            data["shadow"]["version"] += 1
        elif change in ("model", "offline"):
            info = copy.deepcopy(data["info"])
            info["timestamp"] += 1
            if change == "model":
                info["appliance"]["type"] = "hpwh_vw_v1"
            else:
                info["online"] = False
            emit(account, "topics/info", info)
        emit(account, "topics/meta", data["metadata"])

    writes = responding(account, data, after_read=mutate)
    with pytest.raises(HomeAssistantError):
        if change in ("options", "power_missing"):
            await heater.async_set_operation_mode("hybrid")
        else:
            await heater.async_set_temperature(temperature=55)
    assert writes == []


def test_metadata_changes_are_individual_and_do_not_add_unknown_options():
    account, first, data = configured()
    _, second, _ = configured("rwh", "serial-B", account)
    data["metadata"]["shadows"]["state"]["opMode"]["enum"] = [
        "hybrid",
        "eco",
        "unrecognized",
        1,
        {},
    ]
    emit(account, "topics/meta", data["metadata"])
    assert first.operation_list == ["off", "hybrid"]
    assert second.operation_list == ["off", "comfort", "dual", "eco"]
    data["metadata"]["shadows"]["state"]["powerEnabled"]["ro"] = True
    emit(account, "topics/meta", data["metadata"])
    assert first.operation_list == []
    assert first.current_operation == "electric"
    assert not first.supported_features & Feature.OPERATION_MODE


def test_missing_power_and_unknown_mode_are_not_off():
    account, heater, data = configured()
    for patch in (
        {"powerEnabled": None},
        {"powerEnabled": True, "opMode": "new-secret-code"},
    ):
        data["shadow"]["version"] += 1
        data["shadow"]["state"]["reported"].update(patch)
        emit(account, "shadows/state/get/accepted", data["shadow"])
        assert heater.current_operation is None


def test_readonly_defaults_and_optional_setpoint_step():
    account, heater, data = configured()
    meta = data["metadata"]["shadows"]["state"]
    meta["tempSetpoint"].pop("step")
    meta["tempSetpoint"].pop("ro")
    meta["opMode"].pop("ro")
    emit(account, "topics/meta", data["metadata"])
    assert heater.target_temperature_step == 1
    assert heater.native_target_temperature == 50
    assert heater.operation_list == ["off"]
    assert heater.supported_features == Feature.OPERATION_MODE


def test_nullable_optional_sensor_bounds_and_ceiling_are_not_error_values():
    account, heater, data = configured()
    data["metadata"]["topics"]["sensor"]["currentTemp"].update(min=None, max=None)
    emit(account, "topics/meta", data["metadata"])
    data["shadow"]["state"]["reported"]["setTempMax"] = None
    data["shadow"]["version"] += 1
    emit(account, "shadows/state/get/accepted", data["shadow"])
    assert heater.native_current_temperature == 48
    assert heater.native_target_temperature == 50
    assert heater.max_temp == 65


@pytest.mark.parametrize(
    "ceiling,expected",
    [(61, 60), (50, 50), (35, 35), (101, None), (None, 65), (True, None), (20, None)],
)
def test_set_temp_max_can_only_narrow_target_range(ceiling, expected):
    account, heater, data = configured()
    data["shadow"]["version"] += 1
    data["shadow"]["state"]["reported"]["setTempMax"] = ceiling
    emit(account, "shadows/state/get/accepted", data["shadow"])
    limits = dhw.target_range(heater.device)
    assert (limits.maximum if limits else None) == expected


def test_freshness_is_per_stream_and_retained_sensor_never_refreshes():
    account, heater, data = configured()
    status = BaconDhwStatus(account, heater.serial, "vacation")
    heater.device.sensor_received_at -= 3601
    for key in heater.device.sensor_fields_received_at:
        heater.device.sensor_fields_received_at[key] -= 3601
    emit(account, "shadows/state/get/accepted", data["shadow"])
    assert heater.available and heater.native_current_temperature is None
    assert not status.available
    data["sensor"]["items"][0].update(timestamp=999, currentTemp=70)
    emit(account, "topics/sensor", data["sensor"], retained=True)
    assert heater.native_current_temperature is None
    assert heater.device.sensor_timestamp == 2
    data["sensor"]["items"][0]["timestamp"] = 3
    emit(account, "topics/sensor", data["sensor"])
    assert heater.native_current_temperature == 70
    heater.device.state_received_at -= 901
    for key in heater.device.state_fields_received_at:
        heater.device.state_fields_received_at[key] -= 901
    assert heater.native_target_temperature is None
    assert heater.current_operation is None
    assert heater.supported_features == 0
    assert heater.native_current_temperature == 70
    emit(account, "topics/info", {**data["info"], "timestamp": 3, "online": False})
    assert not heater.available
    assert heater.native_current_temperature is None


@pytest.mark.parametrize("stamp", [None, True, -1, 1.0, "2"])
def test_invalid_sensor_timestamp(stamp):
    account, _, data = configured()
    data["sensor"]["items"][0]["timestamp"] = stamp
    with pytest.raises(BaconError, match="invalid_sensor"):
        emit(account, "topics/sensor", data["sensor"])


def test_duplicate_and_older_sensor_messages_cannot_change_values_or_freshness():
    account, heater, data = configured()
    received = heater.device.sensor_received_at
    for stamp in (1, 2):
        data["sensor"]["items"][0].update(timestamp=stamp, currentTemp=80)
        emit(account, "topics/sensor", data["sensor"])
        assert heater.native_current_temperature == 48
        assert heater.device.sensor_received_at == received


def test_reconnection_requires_all_current_streams_and_keeps_identity():
    account, heater, data = configured()
    identity, device_info = heater.unique_id, heater.device_info
    account._disconnected()
    assert not heater.available
    account.connected = True
    account.generation += 1
    emit(account, "topics/info", {**data["info"], "timestamp": 3})
    assert not heater.available
    emit(account, "shadows/state/get/accepted", data["shadow"])
    assert heater.available and heater.native_target_temperature is None
    assert heater.operation_list == []
    emit(account, "topics/meta", data["metadata"], retained=True)
    assert heater.native_target_temperature == 50
    assert heater.native_current_temperature is None
    emit(
        account,
        "topics/sensor",
        {"items": [{**data["sensor"]["items"][0], "timestamp": 3}]},
    )
    assert heater.native_current_temperature == 48
    assert heater.unique_id == identity and heater.device_info == device_info
    replacement = BaconWaterHeater(account, heater.serial)
    assert replacement.unique_id == identity


def test_partial_sensor_update_preserves_other_fields_and_respects_metadata():
    account, heater, data = configured()
    indicator = BaconDhwStatus(account, heater.serial, "vacation")
    emit(
        account,
        "topics/sensor",
        {"items": [{"timestamp": 3, "vacationModeEnabled": True}]},
    )
    assert indicator.is_on is True
    assert heater.native_current_temperature == 48
    emit(
        account,
        "topics/sensor",
        {"items": [{"timestamp": 4, "vacationModeEnabled": "true"}]},
    )
    assert not indicator.available and indicator.is_on is None
    data["metadata"]["topics"]["sensor"].pop("vacationModeEnabled")
    emit(account, "topics/meta", data["metadata"])
    emit(
        account,
        "topics/sensor",
        {"items": [{"timestamp": 5, "vacationModeEnabled": True}]},
    )
    assert not indicator.available


async def test_late_family_and_capabilities_create_entities_once(hass):
    account = environment()
    cleanup, heaters, statuses = [], [], []
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(bacon=account, coordinators=()),
        async_on_unload=cleanup.append,
    )
    await async_setup_entry(hass, entry, heaters.extend)
    track_status(entry, account, statuses.extend)
    assert not heaters and not statuses
    _, _, data = configured(account=account)
    assert len(heaters) == 1 and len(statuses) == 6
    assert len({s.unique_id for s in statuses}) == 6
    emit(account, "topics/meta", data["metadata"])
    account._notify()
    assert len(heaters) == 1 and len(statuses) == 6
    for remove in cleanup:
        remove()
    configured("rwh", "serial-B", account)
    assert len(heaters) == 1 and len(statuses) == 6
    await async_setup_entry(
        hass,
        SimpleNamespace(runtime_data=SimpleNamespace(bacon=None, coordinators=())),
        Mock(),
    )


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"manualThermalDisinfectionEnabled": True},
        {"vacationOnTimestamp": 100},
        {"opMode": "hybrid"},
        {"powerEnabled": 1, "opMode": "hybrid"},
        {"powerEnabled": True},
        {"powerEnabled": False, "customTitle": "x"},
        {"powerEnabled": True, "opMode": "eco"},
    ],
)
def test_family_validator_has_no_generic_write_escape(patch):
    _, heater, _ = configured()
    assert not dhw.command_valid(heater.device, patch, "midea")
