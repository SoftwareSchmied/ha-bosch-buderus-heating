"""Opt-in wallbox history: privacy, request budget, failure paths and the
names of unknown fields. Synthetic data only; no real wallbox was involved."""

import copy
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import wallbox_history
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    Gateway,
    PointTClient,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    AuthenticationError,
    ProtocolError,
    RateLimited,
    RequestTimeout,
    ResourceError,
    ResourceForbidden,
    ResourceNotAcceptable,
    ResourceNotFound,
)
from custom_components.bosch_buderus_heating.wallbox import (
    MAX_UNKNOWN_NAMES,
    WallboxCoordinator,
    parse_section,
)
from custom_components.bosch_buderus_heating.wallbox_history import (
    OPTION_WALLBOX_HISTORY,
    parse_chargelog,
    parse_energyhistory,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.helpers.managed_runtime import wait_for_sources, wallbox_coordinators
from tests.helpers.options_menu import outcome

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

ROOT = "custom_components.bosch_buderus_heating"
FIXTURE = Path(__file__).parent / "fixtures/contracts/pointt-wallbox/cp0-control.json"
GW = "wallbox-A"
# Everything below must never reach a sensor, the diagnostics or a log.
PRIVATE = ("Bella-card", "private-meter-serial", "guid-private-1", "rfid-uuid-9")

CHARGELOG = {
    "id": "/rest/v1/cp0/chargelog",
    "type": "wbChargelog",
    "values": [
        {
            "guid": "guid-private-1",
            "plugged": "2026-11-10T08:00:00+0100",
            "unplugged": "2026-11-10T18:00:00+0100",
            "begin": "2026-11-10T08:05:00+0100",
            "end": "2026-11-10T17:55:00+0100",
            "sessionDuration": 35400,
            "chargingDuration": 30000,
            "energy": 40.5,
            "cost": {"total": 11.4, "currency": "EUR"},
            "meter": {"serial": "private-meter-serial", "posBegin": 0.0},
            "authentication": {
                "uuid": "rfid-uuid-9",
                "source": "rfid",
                "label": "Bella-card",
            },
        },
        {
            "guid": "guid-private-2",
            "begin": "2026-11-12T19:00:00+01:00",
            "end": "2026-11-12T22:30:00+01:00",
            "chargingDuration": 12000,
            "energy": 11.25,
            "authentication": {"label": "Bella-card"},
        },
    ],
}
ENERGY = {
    "id": "/rest/v1/cp0/energyhistory",
    "start": "2026-11-01T00:00:00",
    "end": "2026-11-18T12:00:00",
    "values": [
        {
            "signal": "gridEnergy",
            "unitOfMeasure": "kWh",
            "recording": [{"c": 3, "y": 10.5}, {"c": 2, "y": 4.0}],
        },
        {
            "signal": "solarEnergy",
            "unitOfMeasure": "Wh",
            "recording": [{"c": 1, "y": 2500}],
        },
        {
            "signal": "costTotal",
            "unitOfMeasure": "EUR",
            "recording": [{"c": 1, "y": 99.0}],
        },
    ],
}


def answers():
    return {
        "chargelog": copy.deepcopy(CHARGELOG),
        "energyhistory": copy.deepcopy(ENERGY),
    }


# Parsers ---------------------------------------------------------------------


def test_the_newest_finished_session_keeps_four_values_and_nothing_else():
    session = parse_chargelog(CHARGELOG)
    assert session is not None
    assert session.start == datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
    assert session.end == datetime(2026, 11, 12, 21, 30, tzinfo=UTC)
    assert session.duration_seconds == 12000
    assert session.energy_kwh == 11.25
    assert set(session.__slots__) == {"start", "end", "duration_seconds", "energy_kwh"}
    assert not any(secret in repr(session) for secret in PRIVATE)


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"values": None}, {"values": {}}, "text", 7],
)
def test_a_charge_log_without_a_list_of_values_is_a_protocol_error(payload):
    with pytest.raises(ProtocolError):
        parse_chargelog(payload)


def test_unusable_sessions_are_skipped_not_zeroed():
    naive = {"begin": "2026-11-10T08:00:00", "end": "2026-11-10T09:00:00"}
    backwards = {
        "begin": "2026-11-10T09:00:00+01:00",
        "end": "2026-11-10T08:00:00+01:00",
    }
    unfinished = {"begin": "2026-11-10T09:00:00+01:00"}
    assert parse_chargelog({"values": [naive, backwards, unfinished, 3, None]}) is None
    assert parse_chargelog({"values": []}) is None
    odd = {
        "begin": "2026-11-10T09:00:00+01:00",
        "end": "2026-11-10T10:00:00+01:00",
        "chargingDuration": True,
        "energy": -1,
    }
    session = parse_chargelog({"values": [odd]})
    assert session is not None
    assert session.duration_seconds is None
    assert session.energy_kwh is None


LOCAL = timezone(timedelta(hours=1))


def test_a_time_without_an_offset_is_local_time_when_the_clock_is_known():
    naive = {
        "begin": "2026-11-10T08:00:00",
        "end": "2026-11-10T09:30:00",
        "energy": 5,
    }
    now = datetime(2026, 11, 18, 12, 0, tzinfo=LOCAL)
    session = parse_chargelog({"values": [naive]}, now)
    assert session is not None
    assert session.start == datetime(2026, 11, 10, 8, 0, tzinfo=LOCAL)
    assert session.end == datetime(2026, 11, 10, 9, 30, tzinfo=LOCAL)
    # Without a clock a time without an offset stays unusable.
    assert parse_chargelog({"values": [naive]}) is None


def test_a_session_ending_in_the_future_never_wins():
    now = datetime(2026, 11, 18, 12, 0, tzinfo=LOCAL)
    future = {
        "begin": "2099-01-01T08:00:00+01:00",
        "end": "2099-01-01T09:00:00+01:00",
        "energy": 99,
    }
    real = {
        "begin": "2026-11-10T08:00:00+01:00",
        "end": "2026-11-10T09:00:00+01:00",
        "energy": 7,
    }
    session = parse_chargelog({"values": [real, future]}, now)
    assert session is not None
    assert session.energy_kwh == 7
    assert parse_chargelog({"values": [future]}, now) is None
    # A few minutes of clock difference are tolerated.
    close = {
        "begin": "2026-11-18T10:00:00+01:00",
        "end": "2026-11-18T12:05:00+01:00",
    }
    assert parse_chargelog({"values": [close]}, now) is not None


@pytest.mark.parametrize(
    ("unit", "expected"),
    [(None, 40.5), ("kWh", 40.5), ("Wh", 0.0405), ("MWh", None), (7, None)],
)
def test_the_unit_of_a_session_energy_is_checked_when_named(unit, expected):
    item = {
        "begin": "2026-11-10T08:00:00+01:00",
        "end": "2026-11-10T09:00:00+01:00",
        "energy": 40.5,
    }
    if unit is not None:
        item["unitOfMeasure"] = unit
    session = parse_chargelog({"values": [item]})
    assert session is not None
    if expected is None:
        assert session.energy_kwh is None
    else:
        assert session.energy_kwh == pytest.approx(expected)


async def test_a_cycle_reads_an_answer_without_offsets_and_drops_a_future_end():
    log = {
        "values": [
            {"begin": "2026-11-10T08:00:00", "end": "2026-11-10T09:00:00"},
            {"begin": "2099-01-01T08:00:00", "end": "2099-01-01T09:00:00"},
        ]
    }
    client = SimpleNamespace(get_wallbox_history=AsyncMock(return_value=log))
    history = wallbox_history.WallboxHistory(interval=1800)
    now = datetime(2026, 11, 18, 12, 0, tzinfo=LOCAL)
    await history.read_due(client, GW, enabled=True, local_now=now)
    assert history.session is not None
    assert history.session.end == datetime(2026, 11, 10, 9, 0, tzinfo=LOCAL)


def test_energy_history_sums_grid_and_solar_and_converts_the_unit_once():
    totals = parse_energyhistory(ENERGY)
    assert totals is not None
    assert totals.grid_kwh == 14.5
    assert totals.solar_kwh == 2.5
    assert not any(hasattr(totals, name) for name in ("cost", "cost_total"))


def test_energy_history_needs_a_known_unit_and_a_known_signal():
    payload = copy.deepcopy(ENERGY)
    payload["values"][0]["unitOfMeasure"] = "MWh"
    totals = parse_energyhistory(payload)
    assert totals is not None
    assert totals.grid_kwh is None
    assert totals.solar_kwh == 2.5
    only_cost = {"values": [ENERGY["values"][2]]}
    assert parse_energyhistory(only_cost) is None
    with pytest.raises(ProtocolError):
        parse_energyhistory({"values": "x"})


# Client ----------------------------------------------------------------------


async def test_the_client_builds_the_two_queries_from_fixed_parts_only():
    async with aiohttp.ClientSession() as session:
        client = PointTClient(session, "synthetic")
        client._request = AsyncMock(return_value={})
        start = datetime(2026, 11, 1, 0, 0, 0, 999)
        end = datetime(2026, 11, 18, 12, 30, 5, tzinfo=UTC)
        await client.get_wallbox_history(GW, "chargelog", start, end)
        await client.get_wallbox_history(GW, "energyhistory", start, end)
        first, second = client._request.await_args_list
        assert first.args[1] == (
            "gateways/wallbox-A/resource/rest/v1/cp0/chargelog"
            "?filter_after=2026-11-01T00%3A00%3A00"
            "&filter_before=2026-11-18T12%3A30%3A05"
        )
        assert second.args[1] == (
            "gateways/wallbox-A/resource/rest/v1/cp0/energyhistory"
            "?filter_start=2026-11-01T00%3A00%3A00"
            "&filter_end=2026-11-18T12%3A30%3A05"
            "&sample_rate=P31D&filter_signals=gridEnergy%2CsolarEnergy"
        )
        for call in (first, second):
            assert call.kwargs["retryable"] is False
            assert call.kwargs["resource_path"].startswith("/rest/v1/cp0/")
            assert "?" not in call.kwargs["resource_path"]
        for kind in ("chargelog/../conf/auth", "conf/auth", "rfid", ""):
            with pytest.raises(ValueError):
                await client.get_wallbox_history(GW, kind, start, end)
        # The single-value reader still refuses both, as before.
        for leaf in ("chargelog", "energyhistory"):
            with pytest.raises(ValueError):
                await client.get_wallbox_leaf(GW, leaf)
        assert client._request.await_count == 2


# Coordinator -----------------------------------------------------------------


def coordinator_with(hass, *, history, friendly=False, **replies):
    data = json.loads(FIXTURE.read_text())
    config = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            "brand": "buderus",
            "gateway_ids": [GW],
            **tokens_to_data(AuthTokens("synthetic-access", "synthetic-refresh", 4e9)),
        },
        options={
            "polling_profile": "cloud_friendly" if friendly else "standard",
            **({OPTION_WALLBOX_HISTORY: True} if history else {}),
        },
    )
    config.add_to_hass(hass)
    served = answers()
    served.update(replies)
    calls: list[str] = []

    async def history_reply(gateway, kind, start, end):
        calls.append(kind)
        reply = served[kind]
        if isinstance(reply, Exception):
            raise reply
        return copy.deepcopy(reply)

    client = SimpleNamespace(
        get_wallbox_section=AsyncMock(
            side_effect=lambda gateway, section: copy.deepcopy(data[section])
        ),
        get_wallbox_history=AsyncMock(side_effect=history_reply),
    )
    coordinator = WallboxCoordinator(
        hass, client, Gateway(GW, device_type="commodule"), config
    )
    return coordinator, calls, served, config


async def test_nothing_is_requested_while_the_option_is_off(hass):
    coordinator, calls, _served, _config = coordinator_with(hass, history=False)
    for _ in range(4):
        await coordinator.async_refresh()
    assert calls == []
    assert coordinator.history.requests == 0
    assert coordinator.history.session is None
    assert coordinator.history.diagnostics()["enabled"] is False


async def test_the_first_cycle_costs_two_requests_and_then_one_pair_per_interval(
    hass,
):
    coordinator, calls, _served, _config = coordinator_with(hass, history=True)
    clock = [1000.0]
    with patch.object(wallbox_history, "monotonic", lambda: clock[0]):
        for _ in range(5):
            await coordinator.async_refresh()
        assert calls == ["chargelog", "energyhistory"]
        assert coordinator.history.session is not None
        assert coordinator.history.totals is not None
        clock[0] += 1797
        await coordinator.async_refresh()
        assert len(calls) == 2
        clock[0] += 3
        await coordinator.async_refresh()
        assert len(calls) == 4
    assert coordinator.history.interval == 1800


async def test_the_cloud_friendly_profile_reads_history_half_as_often(hass):
    coordinator, calls, _served, _config = coordinator_with(
        hass, history=True, friendly=True
    )
    clock = [1000.0]
    with patch.object(wallbox_history, "monotonic", lambda: clock[0]):
        await coordinator.async_refresh()
        clock[0] += 3500
        await coordinator.async_refresh()
        assert len(calls) == 2
        clock[0] += 200
        await coordinator.async_refresh()
        assert len(calls) == 4


async def test_switching_the_option_off_forgets_the_history_and_stops_requests(hass):
    coordinator, calls, _served, config = coordinator_with(hass, history=True)
    await coordinator.async_refresh()
    assert coordinator.history.session is not None
    hass.config_entries.async_update_entry(
        config, options={**config.options, OPTION_WALLBOX_HISTORY: False}
    )
    await coordinator.async_refresh()
    assert coordinator.history.session is None
    assert coordinator.history.totals is None
    assert coordinator.history.received == {}
    await coordinator.async_refresh()
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("refusal", "status"),
    [(ResourceNotFound, 404), (ResourceForbidden, 403), (ResourceNotAcceptable, 406)],
)
async def test_a_refused_part_is_not_asked_again_until_new_software(
    hass, refusal, status
):
    coordinator, calls, served, _config = coordinator_with(
        hass, history=True, energyhistory=refusal("energyhistory", status)
    )
    clock = [1000.0]
    with patch.object(wallbox_history, "monotonic", lambda: clock[0]):
        await coordinator.async_refresh()
        assert calls == ["chargelog", "energyhistory"]
        assert coordinator.history.errors == {"energyhistory": f"http_{status}"}
        for _ in range(3):
            clock[0] += 1801
            await coordinator.async_refresh()
        assert calls.count("energyhistory") == 1
        assert calls.count("chargelog") == 4
        # The readable part stays usable.
        assert coordinator.history.session is not None
        assert coordinator.history.totals is None
        served["energyhistory"] = copy.deepcopy(ENERGY)
        coordinator._note_firmware("5.1.0")
        coordinator._note_firmware("5.2.0")
        await coordinator.async_refresh()
        assert calls.count("energyhistory") == 2
        assert coordinator.history.totals is not None
        assert coordinator.history.errors == {}


async def test_a_transport_failure_ends_the_history_cycle_but_not_the_wallbox(hass):
    coordinator, calls, served, _config = coordinator_with(
        hass, history=True, chargelog=RequestTimeout("timeout")
    )
    clock = [1000.0]
    with patch.object(wallbox_history, "monotonic", lambda: clock[0]):
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        # The second request is not tried in a cycle that already failed.
        assert calls == ["chargelog"]
        assert coordinator.history.errors == {"chargelog": "request_failed"}
        await coordinator.async_refresh()
        assert calls == ["chargelog"]
        # Not before the interval, then again, and recovery clears the error.
        served["chargelog"] = copy.deepcopy(CHARGELOG)
        clock[0] += 1801
        await coordinator.async_refresh()
        assert calls == ["chargelog", "chargelog", "energyhistory"]
        assert coordinator.history.errors == {}
        assert coordinator.history.session is not None


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (RateLimited(30.0), "rate_limited"),
        (AuthenticationError("auth"), "request_failed"),
        (ProtocolError("bad"), "invalid_payload"),
        (ResourceError("boom", 500), "http_500"),
    ],
)
async def test_failure_categories_are_recorded_without_text(hass, failure, category):
    coordinator, _calls, _served, _config = coordinator_with(
        hass, history=True, chargelog=failure
    )
    await coordinator.async_refresh()
    assert coordinator.history.errors["chargelog"] == category
    assert not coordinator.history.fresh("chargelog")
    assert coordinator.history.session is None


async def test_stale_history_expires_after_twice_the_interval(hass):
    coordinator, _calls, _served, _config = coordinator_with(hass, history=True)
    clock = [1000.0]
    with patch.object(wallbox_history, "monotonic", lambda: clock[0]):
        await coordinator.async_refresh()
        assert coordinator.history.fresh("chargelog")
        clock[0] += 3600
        assert coordinator.history.fresh("chargelog")
        clock[0] += 1
        assert not coordinator.history.fresh("chargelog")


async def test_a_garbled_answer_withdraws_the_old_values(hass):
    coordinator, _calls, served, _config = coordinator_with(hass, history=True)
    clock = [1000.0]
    with patch.object(wallbox_history, "monotonic", lambda: clock[0]):
        await coordinator.async_refresh()
        assert coordinator.history.totals is not None
        served["energyhistory"] = {"values": "garbled"}
        clock[0] += 1801
        await coordinator.async_refresh()
        assert coordinator.history.totals is None
        assert coordinator.history.errors == {"energyhistory": "invalid_payload"}


# Names of unknown fields -----------------------------------------------------


def test_unknown_field_names_are_listed_without_values():
    payload = json.loads(FIXTURE.read_text())["telemetry"]
    payload["values"]["energyTotal"] = "123456;secret-value"
    parsed = parse_section("telemetry", payload)
    assert "energyTotal" in parsed.unknown_names
    assert "chargePermission" in parsed.unknown_names
    # Fields a parser reads are not unknown.
    assert not {"meter", "wbState", "limit", "emStatus"} & set(parsed.unknown_names)
    assert list(parsed.unknown_names) == sorted(parsed.unknown_names)
    assert "secret-value" not in repr(parsed.unknown_names)


def test_only_plain_tokens_become_names_and_the_list_is_bounded():
    values = {f"field{number:03}": "x" for number in range(100)}
    values.update(
        {
            "name=Max": "x",
            "serial 123": "x",
            "1234-5678": "x",
            "": "x",
            "ü": "x",
            "a" * 49: "x",
            "meter": "1;2",
        }
    )
    parsed = parse_section("telemetry", {"values": values})
    assert len(parsed.unknown_names) == MAX_UNKNOWN_NAMES
    assert all(name.startswith("field") for name in parsed.unknown_names)
    # 6 unnamed keys plus the 36 plain names beyond the limit.
    assert parsed.unnamed_count == 6 + 100 - MAX_UNKNOWN_NAMES
    assert "name=Max" not in repr(parsed.unknown_names)


def test_identifier_like_keys_are_counted_not_named():
    parsed = parse_section(
        "telemetry",
        {
            "values": {
                "AB12345678901": "x",
                "ab1234567": "x",
                "mac00112233": "x",
                "limit2": "x",
                "meter": "1;2",
                "chargePermission": "x",
            }
        },
    )
    assert parsed.unknown_names == ("chargePermission", "limit2")
    assert parsed.unnamed_count == 3


def test_no_unknown_field_gives_an_empty_list():
    parsed = parse_section("info", {"values": {"hwCurrentLimit": "16"}})
    assert parsed.unknown_names == ()
    assert parsed.unnamed_count == 0


# In a running Home Assistant -------------------------------------------------


@pytest.fixture
def wallbox_cloud():
    data = json.loads(FIXTURE.read_text())
    served = answers()
    calls = []

    async def section(gateway, name):
        return copy.deepcopy(data[name])

    async def leaf(gateway, path):
        if path not in data["leaves"]:
            raise ResourceNotFound(path, 404)
        return copy.deepcopy(data["leaves"][path])

    async def history(gateway, kind, start, end):
        calls.append(kind)
        return copy.deepcopy(served[kind])

    with (
        patch(
            ROOT + ".PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway(GW, device_type="commodule"),)),
        ),
        patch(ROOT + ".PointTClient.get_wallbox_section", side_effect=section),
        patch(ROOT + ".PointTClient.get_wallbox_leaf", side_effect=leaf),
        patch(ROOT + ".PointTClient.get_wallbox_history", side_effect=history),
        patch(ROOT + "._async_load_startup_data", AsyncMock()),
        patch(ROOT + ".async_setup_notifications", AsyncMock(return_value=())),
    ):
        yield calls, served


def entry(hass, *, history):
    config = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            "brand": "buderus",
            "gateway_ids": [GW],
            **tokens_to_data(AuthTokens("synthetic-access", "synthetic-refresh", 4e9)),
        },
        options={
            "polling_profile": "standard",
            **({OPTION_WALLBOX_HISTORY: True} if history else {}),
        },
    )
    config.add_to_hass(hass)
    return config


async def _setup(hass, config):
    assert await hass.config_entries.async_setup(config.entry_id)
    await wait_for_sources(hass, config, 1)
    await hass.async_block_till_done()
    coordinator = wallbox_coordinators(config)[0]
    for _ in range(3):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    return coordinator


def history_entities(hass):
    return {
        item.unique_id.rsplit(":", 1)[-1]: item
        for item in er.async_get(hass).entities.values()
        if item.unique_id.startswith(f"{GW}:wallbox:cp0:history_")
    }


async def test_without_the_option_no_history_entity_and_no_request(hass, wallbox_cloud):
    calls, _served = wallbox_cloud
    config = entry(hass, history=False)
    await _setup(hass, config)
    assert history_entities(hass) == {}
    assert calls == []
    assert await hass.config_entries.async_unload(config.entry_id)


async def test_with_the_option_six_sensors_show_the_reported_values(
    hass, wallbox_cloud
):
    calls, _served = wallbox_cloud
    config = entry(hass, history=True)
    coordinator = await _setup(hass, config)
    entities = history_entities(hass)
    assert set(entities) == {
        "history_last_session_start",
        "history_last_session_end",
        "history_last_session_duration",
        "history_last_session_energy",
        "history_month_grid_energy",
        "history_month_solar_energy",
    }
    for item in entities.values():
        assert item.disabled_by is None
        assert item.translation_key == "wallbox_" + item.unique_id.rsplit(":", 1)[-1]
    state = lambda key: hass.states.get(entities[key].entity_id)  # noqa: E731
    assert state("history_last_session_energy").state == "11.25"
    assert state("history_month_grid_energy").state == "14.5"
    assert state("history_month_solar_energy").state == "2.5"
    assert state("history_last_session_duration").state == "200.0"
    assert state("history_last_session_end").state.startswith("2026-11-12T21:30:00")
    assert (
        state("history_last_session_energy").attributes["unit_of_measurement"] == "kWh"
    )
    assert calls == ["chargelog", "energyhistory"]
    assert coordinator.history.requests == 2

    # No personal data anywhere a user or a log could see it.
    everything = json.dumps(
        {
            "states": [item.as_dict() for item in hass.states.async_all()],
            "diagnostics": await async_get_config_entry_diagnostics(hass, config),
        },
        default=str,
    )
    for secret in PRIVATE:
        assert secret not in everything
    history = (await async_get_config_entry_diagnostics(hass, config))["wallboxes"][0][
        "history"
    ]
    assert history["enabled"] is True
    assert history["requests"] == 2
    assert history["parts"]["chargelog"] == {
        "fresh": True,
        "error": None,
        "has_data": True,
    }
    # Counters and states only: no date, no value of a session or a total.
    assert set(history) == {"enabled", "requests", "interval_seconds", "parts"}
    assert set(history["parts"]) == {"chargelog", "energyhistory"}
    assert not any(token in json.dumps(history) for token in ("2026", "11.25", "14.5"))

    # Turning the option off: the entities stay but have nothing to show.
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.config_entries.async_update_entry(
            config, options={**config.options, OPTION_WALLBOX_HISTORY: False}
        )
        reload.assert_not_called()
    assert coordinator.source.current()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert state("history_last_session_energy").state == "unavailable"
    assert state("history_month_grid_energy").state == "unavailable"
    assert (await async_get_config_entry_diagnostics(hass, config))["wallboxes"][0][
        "history"
    ]["enabled"] is False
    assert await hass.config_entries.async_unload(config.entry_id)


async def test_a_wallbox_without_history_answers_creates_no_sensor(hass, wallbox_cloud):
    calls, _served = wallbox_cloud
    config = entry(hass, history=True)
    with patch.object(
        wallbox_history.WallboxHistory,
        "_read",
        AsyncMock(side_effect=ResourceNotFound("x", 404)),
    ):
        await _setup(hass, config)
    assert history_entities(hass) == {}
    assert calls == []
    assert await hass.config_entries.async_unload(config.entry_id)


async def test_the_diagnostics_list_unknown_field_names_not_values(hass, wallbox_cloud):
    config = entry(hass, history=False)
    await _setup(hass, config)
    report = (await async_get_config_entry_diagnostics(hass, config))["wallboxes"][0]
    sections = report["sections"]
    assert "chargePermission" in sections["telemetry"]["unknown_field_names"]
    assert "boxSerial" in sections["info"]["unknown_field_names"]
    assert "light" in sections["conf"]["unknown_field_names"]
    text = json.dumps(report)
    for value in (
        "private-serial",
        "private-label",
        "private-meter-serial",
        "unreviewed",
    ):
        assert value not in text
    assert await hass.config_entries.async_unload(config.entry_id)


async def test_the_options_step_stores_the_history_choice(hass, wallbox_cloud):
    config = entry(hass, history=False)
    assert await hass.config_entries.async_setup(config.entry_id)
    await wait_for_sources(hass, config, 1)
    await hass.async_block_till_done()
    with patch.object(hass.config_entries, "async_schedule_reload"):
        for choice in (True, False):
            menu = await hass.config_entries.options.async_init(config.entry_id)
            form = await hass.config_entries.options.async_configure(
                menu["flow_id"], {"next_step_id": "wallbox"}
            )
            assert form["type"] is FlowResultType.FORM
            default = {str(key): key.default() for key in form["data_schema"].schema}[
                OPTION_WALLBOX_HISTORY
            ]
            assert default is (not choice)
            result = await hass.config_entries.options.async_configure(
                form["flow_id"],
                {
                    "wallbox_authentication": False,
                    OPTION_WALLBOX_HISTORY: choice,
                },
            )
            assert await outcome(hass, result) == "wallbox_saved"
            assert config.options[OPTION_WALLBOX_HISTORY] is choice
    assert await hass.config_entries.async_unload(config.entry_id)


def test_the_option_is_only_on_for_true():
    on = wallbox_history.history_enabled
    assert on({OPTION_WALLBOX_HISTORY: True}) is True
    for options in (None, {}, {OPTION_WALLBOX_HISTORY: False}):
        assert on(options) is False
    for value in ("true", 1, "on", None):
        assert on({OPTION_WALLBOX_HISTORY: value}) is False


def test_history_window_is_a_fixed_number_of_days():
    assert wallbox_history.SESSION_WINDOW_DAYS == 35
    assert timedelta(days=wallbox_history.SESSION_WINDOW_DAYS) < timedelta(days=62)
