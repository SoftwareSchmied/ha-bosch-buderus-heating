"""App-shaped messages, field freshness and write acknowledgement regressions."""

import copy
from unittest.mock import patch

import pytest

from custom_components.bosch_buderus_heating.bacon.models import BaconError
from tests.test_bacon_dhw import configured as dhw_configured
from tests.test_bacon_dry import configured as dry_configured
from tests.test_bacon_purifier import configured as purifier_configured
from tests.test_bacon_rac import configured as rac_configured
from tests.test_bacon_session import emit, environment, state

MODULE = "custom_components.bosch_buderus_heating.bacon.session"
PROFILES = [
    (dhw_configured, "midea", "currentTemp", 49),
    (dhw_configured, "rwh", "currentTemp", 49),
    (rac_configured, "ac_186i3", "roomTemperature", 24.5),
    (purifier_configured, "air_6001i", "relHumidity", 44.5),
    (dry_configured, "dry_8000i", "relHumidity", 61),
]


@pytest.mark.parametrize("configure,name,field,value", PROFILES)
def test_newest_sensor_item_and_non_null_partial_updates(configure, name, field, value):
    account, _, data = configure(name)
    device = account.devices["serial-A"]
    old = copy.deepcopy(device.sensor)
    row = data["sensor"]["items"][0]
    emit(
        account,
        "topics/sensor",
        {
            "deviceType": device.model,
            "items": [{**row, "timestamp": 4, field: value}, {**row, "timestamp": 3}],
        },
    )
    assert device.sensor[field] == value
    assert device.sensor_timestamp == 4
    observed = device.sensor_fields_received_at[field]
    emit(account, "topics/sensor", {"items": [{"timestamp": 5, field: None}]})
    assert device.sensor[field] == value
    assert device.sensor_fields_received_at[field] == observed
    for key in old.keys() - {field, "timestamp"}:
        assert device.sensor[key] == old[key]


@pytest.mark.parametrize("configure,name,field,value", PROFILES)
@pytest.mark.parametrize(
    "reported", [True, False], ids=["reported-patch", "desired-only"]
)
def test_partial_state_keeps_unrelated_settings(
    configure, name, field, value, reported
):
    account, _, data = configure(name)
    device = account.devices["serial-A"]
    previous = copy.deepcopy(device.state.reported)
    observed = device.state_fields_received_at["opMode"]
    emit(
        account,
        "shadows/state/update/accepted",
        {
            "version": data["shadow"]["version"] + 1,
            "timestamp": data["shadow"]["timestamp"] + 1,
            "state": {"reported" if reported else "desired": {"powerEnabled": False}},
        },
    )
    assert device.state.reported["opMode"] == previous["opMode"]
    assert device.state_fields_received_at["opMode"] == observed
    assert device.state.reported["powerEnabled"] is (
        False if reported else previous["powerEnabled"]
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"timestamp": 1, "currentTemp": 40},
        {},
        {"items": {}},
        {"items": [None]},
        {"items": [{}]},
        {"items": [{"timestamp": True}]},
        {"items": [{"timestamp": -1}]},
        {"items": [{"timestamp": 1.5}]},
        {"deviceType": 1, "items": []},
        {
            "items": [
                {"timestamp": 4, "currentTemp": 40},
                {"timestamp": 4, "currentTemp": 50},
            ]
        },
    ],
)
def test_malformed_envelopes_do_not_replace_existing_readings(payload):
    account, _, _ = dhw_configured()
    device = account.devices["serial-A"]
    before = copy.deepcopy(device.sensor)
    with pytest.raises(BaconError):
        emit(account, "topics/sensor", payload)
    assert device.sensor == before


def test_empty_old_and_retained_envelopes_do_not_refresh_fields():
    account, _, data = dhw_configured()
    device = account.devices["serial-A"]
    before = device.sensor_fields_received_at.copy()
    emit(account, "topics/sensor", {"items": []})
    emit(account, "topics/sensor", data["sensor"])
    emit(
        account,
        "topics/sensor",
        {"items": [{"timestamp": 99, "currentTemp": 60}]},
        retained=True,
    )
    assert device.sensor_fields_received_at == before
    assert device.sensor["currentTemp"] == 48


@pytest.mark.parametrize("stream,max_age", [("sensor", 3600), ("state", 900)])
def test_partial_updates_do_not_rejuvenate_unrelated_fields(stream, max_age):
    account, heater, _ = dhw_configured()
    device = account.devices["serial-A"]
    baseline = max(device.sensor_received_at, device.state_received_at)
    with patch(MODULE + ".monotonic", return_value=baseline + max_age - 1):
        if stream == "sensor":
            emit(
                account,
                "topics/sensor",
                {"items": [{"timestamp": 3, "vacationModeEnabled": True}]},
            )
        else:
            emit(
                account,
                "shadows/state/update/accepted",
                {
                    "version": 2,
                    "timestamp": 3,
                    "state": {"reported": {"powerEnabled": False}},
                },
            )
    with patch(MODULE + ".monotonic", return_value=baseline + max_age + 1):
        account.expire_values(device)
        if stream == "sensor":
            assert "currentTemp" not in device.sensor
            assert device.sensor["vacationModeEnabled"] is True
            assert heater.native_current_temperature is None
        else:
            assert "opMode" not in device.state.reported
            assert device.state.reported["powerEnabled"] is False
            assert device.state_fields_version["opMode"] == 1


def test_get_replaces_snapshot_and_clears_missing_fields():
    account, _, _ = dhw_configured()
    emit(
        account,
        "shadows/state/get/accepted",
        {
            "version": 2,
            "timestamp": 3,
            "state": {"reported": {"powerEnabled": False}},
        },
    )
    device = account.devices["serial-A"]
    assert device.state.reported == {"powerEnabled": False}
    assert set(device.state_fields_received_at) == {"powerEnabled"}


def test_new_connection_does_not_merge_old_generation_fields():
    account, _, _ = dhw_configured()
    account._disconnected()
    account.connected = True
    account.generation += 1
    emit(account, "topics/sensor", {"items": [{"timestamp": 3, "currentTemp": 40}]})
    emit(
        account,
        "shadows/state/update/accepted",
        {
            "version": 2,
            "timestamp": 3,
            "state": {"reported": {"powerEnabled": False}},
        },
    )
    device = account.devices["serial-A"]
    assert device.sensor == {"currentTemp": 40, "timestamp": 3}
    assert device.state.reported == {"powerEnabled": False}


def test_replayed_partial_state_does_not_refresh_field_time():
    account = environment()
    emit(account, "shadows/state/get/accepted", state())
    payload = state(2, "on")
    emit(account, "shadows/state/update/accepted", payload)
    device = account.devices["serial-A"]
    before = device.state_fields_received_at.copy()
    with patch(MODULE + ".monotonic", return_value=device.state_received_at + 500):
        emit(account, "shadows/state/update/accepted", payload)
    assert device.state_fields_received_at == before


@pytest.mark.parametrize("reported", [False, True])
async def test_desired_ack_then_reported_confirmation(reported):
    account = environment()
    sends = []

    async def publish(name, payload, **kwargs):
        sends.append(name)
        if name.endswith("/get"):
            emit(account, "shadows/state/get/accepted", state())
        else:
            emit(
                account,
                "shadows/state/update/accepted",
                {
                    "version": 2,
                    "timestamp": 2,
                    "state": {"desired": {"mode": "on"}},
                },
            )
            assert account.devices["serial-A"].state.reported["mode"] == "off"
            if reported:
                emit(
                    account,
                    "shadows/state/update/accepted",
                    {
                        "version": 3,
                        "timestamp": 3,
                        "state": {"reported": {"mode": "on"}},
                    },
                )

    account._mqtt.publish.side_effect = publish
    if reported:
        result = await account.write_state(
            "serial-A", {"mode": "on"}, lambda d, v: True
        )
        assert result.reported["mode"] == "on"
    else:
        with pytest.raises(BaconError, match="command_unconfirmed"):
            await account.write_state("serial-A", {"mode": "on"}, lambda d, v: True)
    assert sum(x.endswith("/update") for x in sends) == 1


async def test_desired_only_get_does_not_complete_read_or_clear_uncertainty():
    account = environment()
    device = account.devices["serial-A"]
    device.uncertain = True

    async def publish(*args, **kwargs):
        emit(
            account,
            "shadows/state/get/accepted",
            {
                "version": 2,
                "timestamp": 2,
                "state": {"desired": {"mode": "on"}},
            },
        )

    account._mqtt.publish.side_effect = publish
    with pytest.raises(BaconError, match="state_unconfirmed"):
        await account.read_state("serial-A")
    assert device.uncertain


def test_null_reported_field_preserves_value_without_refreshing_it():
    account = environment()
    emit(account, "shadows/state/get/accepted", state())
    before = account.devices["serial-A"].state_fields_received_at.copy()
    emit(
        account,
        "shadows/state/update/accepted",
        {
            "version": 2,
            "timestamp": 2,
            "state": {"reported": {"mode": None}},
        },
    )
    assert account.devices["serial-A"].state.reported["mode"] == "off"
    assert account.devices["serial-A"].state_fields_received_at == before


def test_same_version_partial_confirmation_does_not_conflict_with_desired_ack():
    account = environment()
    emit(account, "shadows/state/get/accepted", state())
    for section in ("desired", "reported"):
        emit(
            account,
            "shadows/state/update/accepted",
            {
                "version": 2,
                "timestamp": 2,
                "state": {section: {"mode": "on"}},
            },
        )
    assert account.devices["serial-A"].state.reported["mode"] == "on"


@pytest.mark.parametrize("change_desired", [False, True])
def test_expired_reported_fields_cannot_be_revived_by_same_version(change_desired):
    account = environment()
    emit(account, "shadows/state/get/accepted", state())
    update = state(2, "on")
    emit(account, "shadows/state/update/accepted", update)
    device = account.devices["serial-A"]
    now = device.state_received_at
    with patch(MODULE + ".monotonic", return_value=now + 901):
        account.expire_values(device)
        if change_desired:
            update["state"]["desired"] = {"other": True}
        emit(account, "shadows/state/update/accepted", update)
    assert "mode" not in device.state.reported
    assert "mode" not in device.state_fields_received_at


def test_full_snapshot_prevents_replay_of_removed_fields_at_same_version():
    account = environment()
    emit(account, "shadows/state/get/accepted", state())
    emit(
        account,
        "shadows/state/get/accepted",
        {
            "version": 2,
            "timestamp": 2,
            "state": {"reported": {"other": True}},
        },
    )
    emit(account, "shadows/state/update/accepted", state(2, "on"))
    assert account.devices["serial-A"].state.reported == {"other": True}


ENTITY = "custom_components.bosch_buderus_heating.bacon.entity"


def reconnect(account, data, serial="serial-A"):
    """Our own reconnect: the broker redelivers only retained messages."""
    account._disconnected()
    account.connected = True
    account._start_generation()
    emit(account, "topics/info", data["info"], serial, retained=True)
    emit(account, "topics/meta", data["metadata"], serial, retained=True)


def test_reconnect_keeps_readings_within_their_age_limits():
    account, heater, data = dhw_configured()
    device = account.devices["serial-A"]
    reconnect(account, data)
    assert heater.available
    assert heater.native_current_temperature == 48
    assert heater.native_target_temperature == 50
    # Later observations extend the carried-over readings instead of replacing them.
    emit(account, "topics/sensor", {"items": [{"timestamp": 3, "currentTemp": 49}]})
    assert heater.native_current_temperature == 49
    assert device.state.reported["tempSetpoint"] == 50


def test_carried_over_readings_still_expire_by_their_own_age():
    account, heater, data = dhw_configured()
    device = account.devices["serial-A"]
    start = max(device.sensor_received_at, device.state_received_at)
    reconnect(account, data)
    # Only these two clocks: patch.stopall() would also end the test
    # framework's own patches (for example its socket guard helpers).
    with (
        patch(MODULE + ".monotonic", return_value=start + 901),
        patch(ENTITY + ".monotonic", return_value=start + 901),
    ):
        account.expire_values(device)
        assert heater.native_target_temperature is None
        assert "tempSetpoint" not in device.state.reported
        assert device.sensor["currentTemp"] == 48
    with patch(MODULE + ".monotonic", return_value=start + 3601):
        account.expire_values(device)
        assert "currentTemp" not in device.sensor


def test_reconnect_does_not_carry_readings_older_than_their_limit():
    account, _, data = dhw_configured()
    device = account.devices["serial-A"]
    start = max(device.sensor_received_at, device.state_received_at)
    account._disconnected()
    with patch(MODULE + ".monotonic", return_value=start + 3601):
        account.connected = True
        account._start_generation()
        emit(account, "topics/info", data["info"], retained=True)
        # Retained messages never extend an age or prove a live device.
        emit(account, "topics/sensor", data["sensor"], retained=True)
        account.expire_values(device)
        assert "currentTemp" not in device.sensor
        assert "tempSetpoint" not in device.state.reported
        assert not account.available("serial-A")


def test_membership_loss_still_clears_readings():
    account, heater, data = dhw_configured()
    device = account.devices["serial-A"]
    account._set_membership(("serial-B",))
    account._set_membership(("serial-A", "serial-B"))
    reconnect(account, data)
    assert heater.native_current_temperature is None
    assert heater.native_target_temperature is None
    emit(
        account,
        "topics/sensor",
        {"items": [{"timestamp": 3, "vacationModeEnabled": True}]},
    )
    # A new sample starts a fresh set: the old temperature is not merged back.
    assert "currentTemp" not in device.sensor


def test_explicit_offline_after_reconnect_hides_readings_at_once():
    account, heater, data = dhw_configured()
    reconnect(account, data)
    assert heater.native_current_temperature == 48
    emit(account, "topics/info", {**data["info"], "timestamp": 9, "online": False})
    assert not heater.available
    assert heater.native_current_temperature is None
