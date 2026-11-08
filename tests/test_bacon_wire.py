"""Exercise the actual aiomqtt MQTT 5 WebSocket codec against a local broker."""

import asyncio
import json
import ssl
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit

import aiomqtt
import pytest
from aiohttp import web

from custom_components.bosch_buderus_heating.bacon.models import topic
from custom_components.bosch_buderus_heating.bacon.purifier_entities import (
    BaconPurifierNumber,
)
from custom_components.bosch_buderus_heating.climate import BaconClimate
from custom_components.bosch_buderus_heating.fan import BaconPurifierFan
from custom_components.bosch_buderus_heating.humidifier import BaconDehumidifier
from custom_components.bosch_buderus_heating.water_heater import BaconWaterHeater
from tests.test_bacon_contracts import token
from tests.test_bacon_session import environment, state
from tests.test_pointt_transport_and_client import serve

pytestmark = pytest.mark.usefixtures("socket_enabled")


def length(number):
    result = bytearray()
    while True:
        value = number % 128
        number //= 128
        result.append(value | (128 if number else 0))
        if not number:
            return bytes(result)


def packet(header, body):
    return bytes([header]) + length(len(body)) + body


def string(value):
    value = value.encode()
    return len(value).to_bytes(2, "big") + value


def publish(name, body):
    return packet(0x30, string(name) + b"\0" + json.dumps(body).encode())


@pytest.mark.parametrize(
    "family_name",
    [
        None,
        "midea",
        "rwh",
        "ac_186i3",
        "cl_7000i",
        "air_1001i",
        "air_6001i",
        "dry_4000i",
        "dry_8000i",
    ],
)
async def test_real_websocket_mqtt5_auth_subscription_and_readback(family_name):
    received = []
    writes = []
    ready = asyncio.Event()
    connected = asyncio.Event()
    is_rac = family_name in ("ac_186i3", "cl_7000i")
    is_purifier = family_name in ("air_1001i", "air_6001i")
    is_dry = family_name in ("dry_4000i", "dry_8000i")
    fixture = (
        json.loads(
            (
                Path(__file__).parent
                / (
                    "fixtures/contracts/bacon-dry"
                    if is_dry
                    else "fixtures/contracts/bacon-purifier"
                    if is_purifier
                    else "fixtures/contracts/bacon-rac"
                    if is_rac
                    else "fixtures/contracts/bacon-dhw"
                )
                / (family_name + ".json")
            ).read_text()
        )
        if family_name
        else None
    )
    shadow_state = fixture["shadow"] if fixture else state()

    async def broker(request):
        assert request.headers["Authorization"] == "Bearer " + token()
        ws = web.WebSocketResponse(protocols=("mqtt",))
        await ws.prepare(request)
        pending = bytearray()
        async for frame in ws:
            if frame.type != web.WSMsgType.BINARY:
                continue
            pending.extend(frame.data)
            while len(pending) > 1:
                count, shift, pos = 0, 0, 1
                while True:
                    value = pending[pos]
                    count += (value & 127) << shift
                    pos += 1
                    if value < 128:
                        break
                    shift += 7
                if len(pending) < pos + count:
                    break
                header = pending[0]
                body = bytes(pending[pos : pos + count])
                del pending[: pos + count]
                kind = header >> 4
                if kind == 1:
                    assert body.startswith(b"\0\4MQTT\5")
                    assert body[7] & 0xC2 == 0xC2  # username, password, clean start
                    assert string("account-A") in body
                    assert string(token()) in body
                    await ws.send_bytes(packet(0x20, b"\0\0\0"))
                    connected.set()
                elif kind == 8:
                    assert body[2:] == b"\0" + string("users/account-A/#") + b"\1"
                    await ws.send_bytes(packet(0x90, body[:2] + b"\0\1"))
                    await ws.send_bytes(
                        publish(
                            topic("account-A", "serial-A", "topics/info"),
                            fixture["info"]
                            if fixture
                            else {"online": True, "timestamp": 1},
                        )
                    )
                    if fixture:
                        for suffix, key in (
                            ("topics/meta", "metadata"),
                            ("topics/sensor", "sensor"),
                        ):
                            await ws.send_bytes(
                                publish(
                                    topic("account-A", "serial-A", suffix), fixture[key]
                                )
                            )
                elif kind == 3:
                    size = int.from_bytes(body[:2], "big")
                    name = body[2 : 2 + size].decode()
                    pid = body[2 + size : 4 + size]
                    assert body[4 + size] == 0  # no MQTT properties
                    received.append(name)
                    await ws.send_bytes(packet(0x40, pid))
                    if name.endswith("/shadows/state/get"):
                        assert body[5 + size :] == b""
                    else:
                        assert fixture and name == topic(
                            "account-A", "serial-A", "shadows/state/update"
                        )
                        desired = json.loads(body[5 + size :])["state"]["desired"]
                        writes.append(desired)
                        shadow_state["state"]["reported"].update(desired)
                        shadow_state["version"] += 1
                    await ws.send_bytes(publish(name + "/accepted", shadow_state))
                    ready.set()
                elif kind == 12:
                    await ws.send_bytes(packet(0xD0, b""))
                elif kind == 14:
                    return ws
        return ws

    account = environment()
    account._timeout = 2
    account.devices["serial-A"].info_timestamp = -1
    account.client.get_devices.return_value = ("serial-A",)
    async with serve([("GET", "/mqtt", broker)]) as url:
        target = urlsplit(url)

        def local_factory(host, port, **kwargs):
            assert host == "broker.euc1.bacon.bosch-tt-cw.com"
            assert port == 443
            assert len(kwargs["identifier"]) == 64
            assert all(c in "0123456789abcdef" for c in kwargs["identifier"])
            assert kwargs.pop("tls_context").verify_mode == ssl.CERT_REQUIRED
            return aiomqtt.Client(target.hostname, target.port, **kwargs)

        account._factory = Mock(side_effect=local_factory)
        task = asyncio.create_task(account.run())
        account.attach_task(task)
        try:
            async with asyncio.timeout(5):
                await connected.wait()
                await ready.wait()
                # A response already sent by the broker may still be in aiomqtt's queue.
                for _ in range(100):
                    if account.devices["serial-A"].state is not None:
                        break
                    await asyncio.sleep(0.001)
            assert account.devices["serial-A"].state.version == 1
            assert account.available("serial-A")
            assert received == [topic("account-A", "serial-A", "shadows/state/get")]
            assert account._factory.call_count == 1
            if is_dry:
                unit = BaconDehumidifier(account, "serial-A")
                assert unit.current_humidity == 60 and unit.action == "drying"
                await unit.async_turn_off()
                await unit.async_turn_on()
                await unit.async_set_humidity(60)
                await unit.async_set_mode("night")
                assert writes == [
                    {"powerEnabled": False},
                    {"powerEnabled": True},
                    {"humiditySetpoint": 60},
                    {"opMode": "night"},
                ]
                assert unit.target_humidity == 60 and unit.mode == "night"
            elif is_purifier:
                fan = BaconPurifierFan(account, "serial-A")
                stage = BaconPurifierNumber(account, "serial-A", "fan_speed")
                await fan.async_turn_off()
                await fan.async_turn_on(preset_mode="manual")
                await stage.async_set_native_value(3)
                assert writes == [
                    {"powerEnabled": False},
                    {"powerEnabled": True, "opMode": "manual"},
                    {"fanSpeed": 3},
                ]
                assert fan.is_on and fan.preset_mode == "manual"
                assert stage.native_value == 3
                assert fan.device.state.reported["filterReset"] is False
            elif is_rac:
                climate = BaconClimate(account, "serial-A")
                assert climate.native_current_temperature == 22.5
                await climate.async_turn_off()
                await climate.async_set_hvac_mode("cool")
                await climate.async_set_temperature(temperature=24)
                await climate.async_set_fan_mode("low")
                await climate.async_set_swing_mode("on")
                assert writes == [
                    {"powerEnabled": False},
                    {"powerEnabled": True, "opMode": "cool"},
                    {"tempSetpoint": 24},
                    {"fanSpeed": "low"},
                    {"vSwingEnabled": True},
                ]
                assert (
                    climate.native_target_temperature == 24
                    and climate.hvac_mode == "cool"
                )
                assert climate.fan_mode == "low" and climate.swing_mode == "on"
                assert climate.device.state.reported["ecoEnabled"] is True
            elif fixture:
                heater = BaconWaterHeater(account, "serial-A")
                assert heater.native_current_temperature == 48
                mode = "hybrid" if family_name == "midea" else "dual"
                await heater.async_set_operation_mode(mode)
                await heater.async_set_temperature(temperature=55)
                assert writes == [
                    {"powerEnabled": True, "opMode": mode},
                    {"tempSetpoint": 55},
                ]
                assert heater.current_operation == mode
                assert heater.native_target_temperature == 55
        finally:
            await account.close()
    assert not account.connected
