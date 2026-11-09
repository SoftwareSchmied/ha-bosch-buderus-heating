"""Synthetic wallbox contracts, independent recovery and native HA readings."""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.entry_sources import pointt_coordinators
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    Gateway,
    PointTClient,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    AuthenticationError,
    ProtocolError,
    RateLimited,
    RequestTimeout,
    ResourceForbidden,
    ResourceNotAcceptable,
    ResourceNotFound,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.transport import RateLimitBackoff
from custom_components.bosch_buderus_heating.wallbox import (
    SECTIONS,
    STATES,
    WallboxCoordinator,
    is_wallbox,
    parse_section,
)
from custom_components.bosch_buderus_heating.wallbox_sensor import (
    DESCRIPTIONS,
    WallboxSensor,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.helpers.managed_runtime import wait_for_sources, wallbox_coordinators
from tests.test_pointt_transport_and_client import serve

ROOT = "custom_components.bosch_buderus_heating"
# Home Assistant 2026.9 renamed the reauthentication call of coordinators.
REAUTH_METHOD = (
    "async_start_reauth_if_available"
    if hasattr(ConfigEntry, "async_start_reauth_if_available")
    else "async_start_reauth"
)
FIXTURE = Path(__file__).parent / "fixtures/contracts/pointt-wallbox/cp0.json"


def fixture():
    return json.loads(FIXTURE.read_text())


def entry(hass, *, mixed=False, friendly=False):
    config = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            "brand": "buderus",
            "gateway_ids": ["wallbox-A"] + (["heating"] if mixed else []),
            **tokens_to_data(AuthTokens("synthetic-access", "synthetic-refresh", 4e9)),
        },
        options={"polling_profile": "cloud_friendly" if friendly else "standard"},
    )
    config.add_to_hass(hass)
    return config


def coordinator(hass, *, friendly=False):
    data = fixture()
    client = SimpleNamespace(
        get_wallbox_section=AsyncMock(
            side_effect=lambda gateway, section: data[section]
        )
    )
    return (
        WallboxCoordinator(
            hass,
            client,
            Gateway("wallbox-A", device_type="commodule"),
            entry(hass, friendly=friendly),
        ),
        client,
        data,
    )


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("commodule", True),
        ("CoMmOdUlE2", True),
        ("wallbox", False),
        ("COMMODULE3", False),
        (None, False),
        ("K40", False),
        ("commodule/../K40", False),
    ],
)
def test_transport_family_is_exact(kind, expected):
    assert (
        is_wallbox(Gateway("synthetic", device_type=kind, model="commodule wallbox"))
        is expected
    )


@pytest.mark.parametrize("raw,expected", STATES.items())
def test_all_documented_wire_states_stay_distinct(raw, expected):
    parsed = parse_section("telemetry", {"values": {"wbState": raw}})
    assert parsed.values["state"] == expected
    assert all(value is None for key, value in parsed.values.items() if key != "state")


@pytest.mark.parametrize(
    "value", [None, "", "Finished", "charging", "private-code", {}, [], 1, True]
)
def test_unknown_states_are_not_online_or_finished(value):
    assert (
        parse_section("telemetry", {"values": {"wbState": value}}).values["state"]
        is None
    )


@pytest.mark.parametrize(
    "meter,energy,power",
    [
        ("1250;7.2", 1.25, 7.2),
        ("0;0", 0, 0),
        ("5;middle;0.1", 0.005, 0.1),
        ("120;bad", 0.12, None),
        ("bad;2", None, 2),
        ("-1;NaN", None, None),
        ("1e309;inf", None, None),
        ("4", None, None),
        (None, None, None),
        (12, None, None),
        (";", None, None),
        ("1;" + "1" * 1024, None, None),
    ],
)
def test_meter_wh_and_kw_are_independent(meter, energy, power):
    parsed = parse_section("telemetry", {"values": {"meter": meter}})
    assert parsed.values["energy"] == energy and parsed.values["power"] == power


@pytest.mark.parametrize("value", [None, True, -1, 1.5, "35", 2**63])
def test_duration_requires_reported_nonnegative_long_minutes(value):
    assert (
        parse_section("telemetry", {"values": {"activeChargingTime": value}}).values[
            "duration"
        ]
        is None
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("32", 32),
        ("16.5", 16.5),
        ("0", 0),
        ("", None),
        (None, None),
        (True, None),
        (16, None),
        ("-1", None),
        ("nan", None),
        ("Inf", None),
        ("x" * 65, None),
    ],
)
def test_current_is_read_without_rounding_or_invented_defaults(value, expected):
    assert (
        parse_section("telemetry", {"values": {"limit": value}}).values["current_limit"]
        == expected
    )
    assert (
        parse_section("info", {"values": {"hwCurrentLimit": value}}).values[
            "hardware_current_limit"
        ]
        == expected
    )


@pytest.mark.parametrize(
    "value", [None, "2026-10-24T10:00:00", "2026-10-24", "", "invalid", "x" * 65, 123]
)
def test_charging_start_needs_unambiguous_timestamp(value):
    assert (
        parse_section("telemetry", {"values": {"chargingStart": value}}).values[
            "charging_start"
        ]
        is None
    )


@pytest.mark.parametrize("value", [None, "", "\nprivate", "x" * 129, 123])
def test_device_metadata_is_bounded(value):
    values = parse_section(
        "info", {"values": {"productName": value, "softwareVersion": value}}
    ).values
    assert values["product"] is values["software"] is None


@pytest.mark.parametrize(
    "mode", ["standalone", "leader", "follower", "unknown", None, {}]
)
def test_roles_are_not_charging_strategies(mode):
    assert parse_section("conf", {"values": {"wallboxMode": mode}}).values["role"] == (
        mode if mode in ("standalone", "leader", "follower") else None
    )


@pytest.mark.parametrize(
    "payload", [None, [], {}, {"values": []}, {"values": None}, {"value": "Charging"}]
)
def test_scalar_pointt_envelopes_are_not_accepted(payload):
    with pytest.raises(ProtocolError):
        parse_section("telemetry", payload)


@pytest.mark.parametrize("friendly", [False, True])
async def test_intervals_partial_failure_and_regular_recovery(hass, friendly):
    coord, client, data = coordinator(hass, friendly=friendly)
    fast = 120 if friendly else 60
    with patch(ROOT + ".wallbox.monotonic", return_value=100) as now:
        await coord.async_refresh()
        assert client.get_wallbox_section.await_count == 3
        assert coord.reading("telemetry", "duration") == 35
        assert coord.reading("telemetry", "charging_start") == datetime.fromisoformat(
            "2026-10-24T09:00:00+02:00"
        )
        await coord.async_refresh()
        assert client.get_wallbox_section.await_count == 3
        for failure in (
            ResourceNotFound("private", 404),
            ResourceForbidden("private", 403),
            ResourceNotAcceptable("private", 406),
            ProtocolError("private"),
        ):
            now.return_value += fast
            client.get_wallbox_section.side_effect = failure
            await coord.async_refresh()
            assert coord.reading("telemetry", "power") is None
            assert coord.reading("conf", "role") == "standalone"
            assert not coord.section_fresh("telemetry")
        now.return_value += fast
        client.get_wallbox_section.side_effect = lambda gateway, section: data[section]
        data["telemetry"]["values"].update(meter="1;0.0", activeChargingTime=0)
        await coord.async_refresh()
        assert coord.reading("telemetry", "energy") == 0.001
        assert coord.reading("telemetry", "duration") == 0
        assert coord.reading("telemetry", "power") == 0
        assert coord.reading("conf", "role") == "standalone"
        # No extra request or locally accumulated energy on repeated response.
        now.return_value += fast
        await coord.async_refresh()
        assert coord.reading("telemetry", "energy") == 0.001
        now.return_value += 2 * fast + 1
        assert coord.reading("telemetry", "state") is None
        assert not coord.diagnostics()["sections"]["telemetry"]["fresh"]


@pytest.mark.parametrize(
    "failure,category",
    [
        (ServiceUnavailable(503), "request_failed"),
        (RequestTimeout(), "request_failed"),
        (RateLimited(120), "rate_limited"),
        (AuthenticationError(), "authentication"),
    ],
)
async def test_global_failure_stops_reads_and_clears_old_data(hass, failure, category):
    coord, client, _ = coordinator(hass)
    with patch(ROOT + ".wallbox.monotonic", return_value=100) as now:
        await coord.async_refresh()
        now.return_value = 1000
        client.get_wallbox_section.reset_mock(side_effect=True)
        client.get_wallbox_section.side_effect = failure
        with patch.object(coord.config_entry, REAUTH_METHOD) as reauth:
            await coord.async_refresh()
            assert reauth.call_count == int(category == "authentication")
        assert client.get_wallbox_section.await_count == 1
        assert all(
            coord.reading(section, key) is None
            for section, key in (
                ("telemetry", "power"),
                ("conf", "role"),
                ("info", "product"),
            )
        )
        assert all(
            item["error"] == category
            for item in coord.diagnostics()["sections"].values()
        )


async def test_metadata_failure_does_not_discard_telemetry(hass):
    coord, client, data = coordinator(hass)
    client.get_wallbox_section.side_effect = [
        data["telemetry"],
        ResourceNotFound("conf", 404),
        data["info"],
    ]
    await coord.async_refresh()
    assert coord.reading("telemetry", "power") == 7.2
    assert coord.reading("conf", "role") is None
    assert coord.reading("info", "hardware_current_limit") == 32


async def test_slow_response_serialization_and_no_optimistic_updates(hass):
    coord, client, data = coordinator(hass)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def delayed(gateway, section):
        calls.append(section)
        if len(calls) == 1:
            started.set()
            await release.wait()
        return data[section]

    client.get_wallbox_section.side_effect = delayed
    first = asyncio.create_task(coord.async_refresh())
    await started.wait()
    assert coord.reading("telemetry", "power") is None
    second = asyncio.create_task(coord.async_refresh())
    release.set()
    await asyncio.gather(first, second)
    assert calls == list(SECTIONS)
    assert coord.reading("telemetry", "power") == 7.2


@pytest.mark.usefixtures("socket_enabled")
async def test_real_http_paths_shared_auth_backoff_and_no_write_surface():
    calls = []

    async def respond(request):
        calls.append(
            (
                request.method,
                request.path,
                request.headers.get("Authorization"),
                request.query_string,
            )
        )
        if request.path.endswith("conf"):
            return web.Response(status=429, headers={"Retry-After": "120"})
        return web.json_response(fixture()["telemetry"])

    async with (
        serve(
            [("*", "/gateways/{gateway}/resource/rest/v1/cp0/{section}", respond)]
        ) as url,
        aiohttp.ClientSession() as session,
    ):
        client = PointTClient(session, "synthetic", base_url=url)
        payload = await client.get_wallbox_section("private gateway", "telemetry")
        assert parse_section("telemetry", payload).values["power"] == 7.2
        with pytest.raises(RateLimited):
            await client.get_wallbox_section("private gateway", "conf")
        with pytest.raises(RateLimited):
            await client.get_wallbox_section("private gateway", "info")
        for invalid in ("../cmd/start", "cmd/limit", "cp1/telemetry", "force", ""):
            with pytest.raises(ValueError):
                await client.get_wallbox_section("private gateway", invalid)
    assert len(calls) == 2
    assert calls[0] == (
        "GET",
        "/gateways/private gateway/resource/rest/v1/cp0/telemetry",
        "Bearer synthetic",
        "",
    )
    assert all(call[0] == "GET" for call in calls)


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize(
    "language,mixed,offline",
    [("de", False, False), ("en", True, False), ("de", True, True)],
)
async def test_native_setup_translation_privacy_and_registry_recovery(
    hass, language, mixed, offline
):
    hass.config.language = language
    config = entry(hass, mixed=mixed)
    data = fixture()
    gateways = (Gateway("wallbox-A", device_type="commodule2"),) + (
        (Gateway("heating", device_type="K40"),) if mixed else ()
    )

    async def read(gateway, section):
        assert gateway == "wallbox-A"
        if offline:
            raise ServiceUnavailable(503)
        return data[section]

    async def discover(client, gateway_id, **kwargs):
        assert gateway_id == "heating"
        resource = Resource(
            "/heatSources/actualPower",
            value=2.0,
            has_value=True,
            metadata=ResourceMetadata(resource_type="floatValue", unit="kW"),
        )
        return {resource.path: resource}

    with (
        patch(ROOT + ".PointTClient.get_gateways", AsyncMock(return_value=gateways)),
        patch(ROOT + ".PointTClient.get_wallbox_section", side_effect=read),
        patch(
            ROOT + ".coordinator.async_discover_resources", side_effect=discover
        ) as discovery,
        patch(ROOT + ".coordinator.fault_resource_candidates", return_value=()),
        patch(ROOT + "._async_load_startup_data", AsyncMock()),
        patch(ROOT + ".async_setup_notifications", AsyncMock(return_value=())),
    ):
        assert await hass.config_entries.async_setup(config.entry_id)
        await wait_for_sources(hass, config, 1 + int(mixed))
        await hass.async_block_till_done()
        assert discovery.call_count == int(mixed)
        assert len(pointt_coordinators(config)) == int(mixed)
        assert len(wallbox_coordinators(config)) == 1
        registry = er.async_get(hass)

        def entity_id(key):
            result = registry.async_get_entity_id(
                "sensor", DOMAIN, "wallbox-A:wallbox:cp0:" + key
            )
            assert result is not None
            return result

        state_id = entity_id("state")
        if offline:
            assert hass.states.get(state_id).state == "unavailable"
            offline = False
            await wallbox_coordinators(config)[0].async_refresh()
            await hass.async_block_till_done()
        assert hass.states.get(state_id).state == "charging"
        assert hass.states.get(entity_id("energy")).state == "1.25"
        assert "state_class" not in hass.states.get(entity_id("energy")).attributes
        assert (
            hass.states.get(entity_id("power")).attributes["unit_of_measurement"]
            == "kW"
        )
        assert (
            len(
                [
                    e
                    for e in registry.entities.values()
                    if e.unique_id.startswith("wallbox-A:")
                ]
            )
            == 8
        )
        assert all(
            e.domain == "sensor"
            for e in registry.entities.values()
            if e.unique_id.startswith("wallbox-A:")
        )
        texts = await async_get_translations(hass, language, "entity", {DOMAIN})
        prefix = "component." + DOMAIN + ".entity.sensor.wallbox_"
        assert texts[prefix + "state.state.suspended_ev"] == (
            "Durch Fahrzeug pausiert" if language == "de" else "Paused by vehicle"
        )
        assert texts[prefix + "duration.name"] == (
            "Aktive Ladezeit" if language == "de" else "Active charging time"
        )
        diagnostics = await async_get_config_entry_diagnostics(hass, config)
        assert (
            diagnostics["diagnostics_schema"] == 44
            and len(diagnostics["wallboxes"]) == 1
        )
        serialized = json.dumps(diagnostics)
        for private in (
            "wallbox-A",
            "synthetic-access",
            "synthetic-refresh",
            "private-label",
            "private-serial",
            "Synthetic wallbox",
            "unreviewed",
            "2026-10-24",
        ):
            assert private not in serialized
        # The power reading must not leak; other numbers (for example token
        # lifetimes) may contain the digits "7.2" by chance.
        assert not re.search(r"(?<![\d.])7\.2(?!\d)", serialized)
        old = registry.async_get(state_id)
        registry.async_update_entity(state_id, name="Meine Wallbox")
        disabled = entity_id("hardware_current_limit")
        registry.async_update_entity(
            disabled, disabled_by=er.RegistryEntryDisabler.USER
        )
        await hass.async_block_till_done()
        previous = wallbox_coordinators(config)[0]
        assert await hass.config_entries.async_unload(config.entry_id)
        assert not previous.async_update_listeners()
        assert await hass.config_entries.async_setup(config.entry_id)
        await wait_for_sources(hass, config, 1 + int(mixed))
        await hass.async_block_till_done()
        assert registry.async_get(state_id).id == old.id
        assert registry.async_get(state_id).device_id == old.device_id
        assert registry.async_get(state_id).name == "Meine Wallbox"
        assert registry.async_get(disabled).disabled_by == er.RegistryEntryDisabler.USER
        assert await hass.config_entries.async_unload(config.entry_id)
        await hass.async_block_till_done()


async def test_first_auth_failure_requires_reauthentication(hass):
    coord, client, _ = coordinator(hass)
    coord.config_entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    client.get_wallbox_section.side_effect = AuthenticationError()
    with pytest.raises(ConfigEntryAuthFailed):
        await coord.async_config_entry_first_refresh()


async def test_sensors_keep_unknown_invalid_values_and_expire_all_sections(hass):
    coord, _, data = coordinator(hass)
    entities = [WallboxSensor(coord, item) for item in DESCRIPTIONS]
    assert all(not entity.available for entity in entities)
    with patch(ROOT + ".wallbox.monotonic", return_value=100) as now:
        await coord.async_refresh()
        assert all(entity.available for entity in entities)
        assert entities[0].device_info["model"] == "Synthetic wallbox"
        coord._next.clear()
        data["telemetry"]["values"] = {}
        data["info"]["values"] = {}
        await coord.async_refresh()
        assert entities[0].available and entities[0].native_value is None
        assert entities[0].device_info["model"] is None
        now.return_value = 4000
        assert all(not entity.available for entity in entities)


def test_wallbox_translation_keys_match():
    root = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"
    texts = [
        json.loads((root / p).read_text())["entity"]["sensor"]
        for p in ("strings.json", "translations/en.json", "translations/de.json")
    ]
    for description in DESCRIPTIONS:
        key = description.translation_key
        assert texts[0][key] == texts[1][key]
        assert texts[0][key].keys() == texts[2][key].keys()
        if description.options:
            assert (
                set(texts[0][key]["state"])
                == set(texts[2][key]["state"])
                == set(description.options)
            )


@pytest.mark.parametrize("friendly", [False, True])
async def test_wallbox_poll_is_not_skipped_by_actual_ha_timer(hass, friendly):
    """HA rounds the next refresh down; it can fire just before our deadline."""
    coord, client, _ = coordinator(hass, friendly=friendly)
    start = 1000.202
    path = ROOT + ".wallbox.monotonic"
    with patch(path, return_value=start):
        await coord._async_update_data()
    client.get_wallbox_section.reset_mock()
    coord._microsecond = 0.2
    finished = start + 0.35
    reads = []
    try:
        for tick in range(4):
            with (
                patch.object(hass.loop, "time", return_value=finished),
                patch.object(hass.loop, "call_at") as schedule,
            ):
                coord._schedule_refresh()
            now = schedule.call_args.args[0] + (0.001, 0.004, 0.002, 0.005)[tick]
            with patch(path, return_value=now):
                await coord._async_update_data()
            reads.append(
                sum(
                    call.args[1] == "telemetry"
                    for call in client.get_wallbox_section.await_args_list
                )
            )
            finished = now + 0.35
    finally:
        coord._async_unsub_refresh()
    assert reads == [1, 2, 3, 4]


async def test_due_tolerance_never_reads_a_section_much_too_early(hass):
    coord, client, _ = coordinator(hass)
    with patch(ROOT + ".wallbox.monotonic", return_value=100) as now:
        await coord.async_refresh()
        assert client.get_wallbox_section.await_count == 3
        now.return_value = 100 + 60 - 1.5
        await coord.async_refresh()
        assert client.get_wallbox_section.await_count == 3
        now.return_value = 100 + 60 - 0.9
        await coord.async_refresh()
        sections = [call.args[1] for call in client.get_wallbox_section.await_args_list]
        assert sections[3:] == ["telemetry"]


@pytest.mark.usefixtures("socket_enabled")
async def test_due_tolerance_does_not_shorten_a_cloud_pause(hass):
    calls = []

    async def respond(request):
        calls.append(request.match_info["section"])
        if len(calls) == 1:
            return web.Response(status=429, headers={"Retry-After": "7200"})
        return web.json_response(fixture()[request.match_info["section"]])

    clock = [5000.0]
    backoff = RateLimitBackoff(clock=lambda: clock[0])
    async with (
        serve(
            [("*", "/gateways/{gateway}/resource/rest/v1/cp0/{section}", respond)]
        ) as url,
        aiohttp.ClientSession() as session,
    ):
        client = PointTClient(session, "synthetic", base_url=url, backoff=backoff)
        coord = WallboxCoordinator(
            hass, client, Gateway("wallbox-A", device_type="commodule"), entry(hass)
        )
        with patch(ROOT + ".wallbox.monotonic", return_value=100) as now:
            await coord.async_refresh()
            assert calls == ["telemetry"]
            for _ in range(4):
                now.return_value += 60 - 0.9
                clock[0] += 60 - 0.9
                await coord.async_refresh()
            assert calls == ["telemetry"]
            assert 0 < backoff.remaining < 7200
            clock[0] += backoff.remaining
            now.return_value += 60
            await coord.async_refresh()
            # Freshness uses the same patched clock as the received snapshot.
            assert coord.reading("telemetry", "power") == 7.2
        assert calls == ["telemetry", "telemetry", "conf", "info"]
