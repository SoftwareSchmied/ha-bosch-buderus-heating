"""Air conditioner timers and lifetime energy on the additional cloud."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
import pytest
from aiohttp import web
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.bacon import rac_energy, rac_timers
from custom_components.bosch_buderus_heating.bacon.client import BaconClient
from custom_components.bosch_buderus_heating.bacon.models import BaconError, Device
from custom_components.bosch_buderus_heating.bacon.rac_entities import (
    BaconRacEnergy,
    BaconRacTimer,
    track_entities,
)
from custom_components.bosch_buderus_heating.pointt.transport import RateLimitBackoff
from tests.test_bacon_contracts import provider, token
from tests.test_bacon_dhw import responding
from tests.test_bacon_rac import configured, update
from tests.test_bacon_session import emit
from tests.test_pointt_transport_and_client import serve

NOW = 1_700_000_000.0


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(rac_timers, "now", lambda: NOW)
    # Tests shorten the refresh interval; restore it afterwards.
    monkeypatch.setattr(rac_energy, "INTERVAL", rac_energy.INTERVAL)


def with_timers(account, data, *, on=0, off=0, meta=None):
    """Declare both timers as writable and report them."""
    states = data["metadata"]["shadows"]["state"]
    states["onTimestamp"] = {"ro": False, **(meta or {})}
    states["offTimestamp"] = {"ro": False, **(meta or {})}
    emit(account, "topics/meta", data["metadata"])
    update(account, data, onTimestamp=on, offTimestamp=off)


def timer(account, which, serial="serial-A"):
    return BaconRacTimer(account, serial, which)


# --- timers: contract ------------------------------------------------------


def test_remaining_minutes_round_up_and_expired_is_zero():
    account, _, data = configured()
    with_timers(account, data, on=int(NOW) + 61, off=int(NOW) - 5)
    device = account.devices["serial-A"]
    assert rac_timers.remaining_minutes(device, "onTimestamp") == 2
    assert rac_timers.remaining_minutes(device, "offTimestamp") == 0
    assert rac_timers.remaining_minutes(device, "unknownTimestamp") is None
    # Never more than the span the entity offers.
    device.state.reported["onTimestamp"] = int(NOW) + 10 * 86400
    assert rac_timers.remaining_minutes(device, "onTimestamp") == 1440


def test_target_time_is_minute_aligned_and_ahead():
    assert rac_timers.target_time(1, 61.0) == 120
    assert rac_timers.target_time(30, NOW) % 60 == 0
    assert rac_timers.target_time(30, NOW) - NOW >= 29 * 60


@pytest.mark.parametrize("value", [None, True, "5", -1, 4_200_000_000, 1.5])
def test_implausible_reported_values_are_no_timer(value):
    account, _, data = configured()
    with_timers(account, data)
    device = account.devices["serial-A"]
    device.state.reported["onTimestamp"] = value
    assert rac_timers.stored(device, "onTimestamp") is None
    assert not rac_timers.declared(device, "onTimestamp")


@pytest.mark.parametrize(
    "meta",
    [
        {"ro": True},
        {"ro": None},
        {"ro": False, "step": 30},
        {"ro": False, "step": 120},
        {"ro": False, "step": True},
    ],
)
def test_only_writable_timers_with_the_app_step_are_declared(meta):
    account, _, data = configured()
    with_timers(account, data)
    data["metadata"]["shadows"]["state"]["onTimestamp"] = meta
    emit(account, "topics/meta", data["metadata"])
    assert not rac_timers.declared(account.devices["serial-A"], "onTimestamp")
    assert rac_timers.declared(account.devices["serial-A"], "offTimestamp")


def test_a_declared_step_of_sixty_seconds_is_accepted():
    account, _, data = configured()
    with_timers(account, data, meta={"step": 60})
    assert rac_timers.declared(account.devices["serial-A"], "onTimestamp")


def test_command_validation_guards():
    account, _, data = configured()
    with_timers(account, data, off=int(NOW) + 3600)
    device = account.devices["serial-A"]
    model = device.model
    ahead = rac_timers.target_time(10, NOW)

    def valid(patch, at=NOW):
        return rac_timers.command_valid(device, patch, model, at)

    assert valid({"onTimestamp": ahead})
    assert valid({"onTimestamp": 0})
    # Past, too far, off the minute grid, wrong type, extra field, other field.
    assert not valid({"onTimestamp": (int(NOW) // 60 - 5) * 60})
    assert not valid({"onTimestamp": (int(NOW) // 60) * 60})
    assert not valid({"onTimestamp": ahead + 2 * 86400})
    assert not valid({"onTimestamp": ahead + 1})
    assert not valid({"onTimestamp": float(ahead)})
    assert not valid({"onTimestamp": True})
    assert not valid({"onTimestamp": ahead, "offTimestamp": 0})
    assert not valid({"customTitle": "x"})
    assert not valid({})
    assert not rac_timers.command_valid(device, {"onTimestamp": ahead}, "other", NOW)
    # Never the same minute as the other running timer.
    other = int(NOW) + 3600
    assert not valid({"onTimestamp": other - other % 60})
    # An expired other timer does not block.
    device.state.reported["offTimestamp"] = int(NOW) - 60
    assert valid({"onTimestamp": rac_timers.target_time(60, NOW)})


# --- timers: entities ------------------------------------------------------


async def test_timer_entity_shows_minutes_and_writes_one_absolute_time():
    account, climate, data = configured()
    with_timers(account, data, on=0, off=int(NOW) + 600)
    on, off = timer(account, "on"), timer(account, "off")
    assert on.available and on.native_value == 0
    assert off.native_value == 10
    assert on.entity_registry_enabled_default is False
    assert (on.native_min_value, on.native_max_value, on.native_step) == (0, 1440, 1)
    writes = responding(account, data)
    await on.async_set_native_value(45)
    assert writes == [{"onTimestamp": (int(NOW) // 60 + 45) * 60}]
    assert on.native_value == 45
    # The other timer and the climate fields are untouched.
    assert off.native_value == 10
    assert climate.device.state.reported["tempSetpoint"] == 22
    await on.async_set_native_value(0)
    assert writes[-1] == {"onTimestamp": 0} and on.native_value == 0
    assert len(writes) == 2


async def test_stopping_an_idle_timer_sends_nothing():
    account, _, data = configured()
    with_timers(account, data)
    writes = responding(account, data)
    await timer(account, "off").async_set_native_value(0)
    assert writes == []


@pytest.mark.parametrize("value", [-1, 1441, 1.5, float("nan"), "5"])
async def test_bad_minutes_do_not_publish(value):
    account, _, data = configured()
    with_timers(account, data)
    writes = responding(account, data)
    with pytest.raises(ServiceValidationError):
        await timer(account, "on").async_set_native_value(value)
    assert writes == []


async def test_the_other_timer_in_the_same_minute_is_refused():
    account, _, data = configured()
    with_timers(account, data, off=rac_timers.target_time(30, NOW))
    writes = responding(account, data)
    with pytest.raises(ServiceValidationError):
        await timer(account, "on").async_set_native_value(30)
    assert writes == []
    await timer(account, "on").async_set_native_value(31)
    assert len(writes) == 1


async def test_a_failed_confirmation_is_an_error_without_retry():
    account, _, data = configured()
    with_timers(account, data)
    writes = responding(account, data, outcome="desired")
    with pytest.raises(HomeAssistantError):
        await timer(account, "on").async_set_native_value(20)
    assert len(writes) == 1


async def test_timer_is_unavailable_without_fresh_state_or_declaration():
    account, _, data = configured()
    with_timers(account, data)
    entity = timer(account, "on")
    assert entity.available
    data["metadata"]["shadows"]["state"]["onTimestamp"] = {"ro": True}
    emit(account, "topics/meta", data["metadata"])
    assert not entity.available and entity.native_value is None
    with pytest.raises(ServiceValidationError):
        await entity.async_set_native_value(5)
    data["metadata"]["shadows"]["state"]["onTimestamp"] = {"ro": False}
    emit(account, "topics/meta", data["metadata"])
    assert entity.available
    emit(account, "topics/info", {"online": False, "timestamp": 9})
    assert not entity.available


def collected(account, platform):
    added: list = []
    entry = SimpleNamespace(async_on_unload=Mock())
    track_entities(entry, account, added.extend, platform)
    return added, entry


def test_timers_are_created_only_for_declared_writable_fields():
    account, _, data = configured()
    # The profile declares neither timer: nothing is created.
    assert collected(account, "number")[0] == []
    data["metadata"]["shadows"]["state"]["onTimestamp"] = {"ro": False}
    emit(account, "topics/meta", data["metadata"])
    added, _ = collected(account, "number")
    assert [entity.unique_id.rsplit(":", 1)[1] for entity in added] == ["rac_timer_on"]
    # No other family gets timers.
    configured("cl_7000i", "serial-B", account)
    numbers = collected(account, "number")[0]
    assert all(entity.serial == "serial-A" for entity in numbers)


def test_timers_follow_a_later_declaration():
    account, _, data = configured()
    added: list = []
    entry = SimpleNamespace(async_on_unload=Mock())
    track_entities(entry, account, added.extend, "number")
    assert added == []
    with_timers(account, data)
    # The tracker runs on the account's listeners; call the registered one.
    listener = entry.async_on_unload.call_args.args[0]
    assert callable(listener)
    for call in list(account._listeners):
        call()
    assert sorted(e.unique_id.rsplit(":", 1)[1] for e in added) == [
        "rac_timer_off",
        "rac_timer_on",
    ]
    # Each is added once.
    for call in list(account._listeners):
        call()
    assert len(added) == 2


# --- energy: parsing -------------------------------------------------------


def body(items):
    return {"data": {"consumption": {"items": items}}}


def item(**values):
    return {"timestamp": 100, **values}


def test_totals_take_the_newest_item_and_drop_invalid_fields():
    result = rac_energy.totals(
        body(
            [
                item(timestamp=50, totalElectricalEnergyConsumptionHeat=1.0),
                None,
                item(
                    timestamp=200,
                    totalElectricalEnergyConsumptionHeat=12.5,
                    totalElectricalEnergyConsumptionCool=0,
                    totalElectricalEnergyConsumptionDry=-1,
                    totalElectricalEnergyConsumptionFan="3",
                    totalThermalEnergyOutputCool=True,
                    totalThermalEnergyOutputHeat=float("nan"),
                ),
            ]
        )
    )
    assert result == {"heating": 12.5, "cooling": 0.0}


def test_totals_accept_every_documented_field():
    values = {
        name: float(index + 1) for index, name in enumerate(rac_energy.TOTALS.values())
    }
    result = rac_energy.totals(body([item(**values)]))
    assert result == {key: values[name] for key, name in rac_energy.TOTALS.items()}


def test_empty_items_give_no_value_and_a_bad_shape_is_an_error():
    assert rac_energy.totals(body([])) == {}
    assert rac_energy.totals(body([None, 3])) == {}
    for bad in (None, [], {"data": None}, {"data": {"consumption": None}}, body(None)):
        with pytest.raises(BaconError, match="invalid_history"):
            rac_energy.totals(bad)
    assert rac_energy.totals(body([item(totalThermalEnergyOutputHeat=1e12)])) == {}


# --- energy: HTTP contract -------------------------------------------------


def answer(**values):
    return web.json_response(body([item(**values)]))


@pytest.mark.usefixtures("socket_enabled")
async def test_query_is_one_json_post_with_bearer_and_one_serial():
    seen = []

    async def graphql(request):
        seen.append(
            (
                request.headers["Authorization"],
                request.headers["User-Agent"],
                await request.json(),
            )
        )
        return answer(totalElectricalEnergyConsumptionHeat=7.25)

    async with (
        serve([("POST", "/graphql", graphql)]) as url,
        aiohttp.ClientSession() as http,
    ):
        client = BaconClient(http, provider(), url=url + "/devices")
        assert rac_energy.graphql_url(client) == url + "/graphql"
        result = await rac_energy.fetch_totals(client, "serial-A")
    assert result == {"heating": 7.25}
    (auth, agent, request) = seen[0]
    assert auth == "Bearer " + token() and agent.startswith("DashApp/")
    assert request["operationName"] == "getAdditionalInformation"
    assert request["variables"]["serialNumbers"] == ["serial-A"]
    assert request["variables"]["firstDayOfWeek"] == "MONDAY"
    assert type(request["variables"]["currentTimestamp"]) is int
    assert type(request["variables"]["currentTimeZoneOffset"]) is int
    assert request["query"] == rac_energy.QUERY
    assert len(seen) == 1


def test_default_endpoint_is_the_history_service():
    client = BaconClient(None, provider())
    assert rac_energy.graphql_url(client) == rac_energy.GRAPHQL_URL
    assert (
        rac_energy.GRAPHQL_URL == "https://history.euc1.bacon.bosch-tt-cw.com/graphql"
    )
    assert (
        rac_energy.graphql_url(BaconClient(None, provider(), url="http://x/other"))
        == rac_energy.GRAPHQL_URL
    )


@pytest.mark.usefixtures("socket_enabled")
async def test_a_401_renews_the_token_once_and_a_second_one_is_rejected_quietly():
    calls = []
    tokens = provider()
    tokens.get_access_token.side_effect = [token(), token(signature="renewed")]

    async def graphql(request):
        calls.append(request.headers["Authorization"])
        return answer() if len(calls) > 1 else web.Response(status=401)

    async with (
        serve([("POST", "/graphql", graphql)]) as url,
        aiohttp.ClientSession() as http,
    ):
        client = BaconClient(http, tokens, url=url + "/devices")
        await rac_energy.fetch_totals(client, "serial-A")
    assert calls == ["Bearer " + token(), "Bearer " + token(signature="renewed")]

    calls.clear()
    tokens = provider()

    async def always(request):
        calls.append(1)
        return web.Response(status=401)

    async with (
        serve([("POST", "/graphql", always)]) as url,
        aiohttp.ClientSession() as http,
    ):
        client = BaconClient(http, tokens, url=url + "/devices")
        # Not "authentication": that would end the whole account connection.
        with pytest.raises(BaconError, match=r"^history_rejected$"):
            await rac_energy.fetch_totals(client, "serial-A")
    # One request, one renewal, one repeat; no second renewal.
    assert len(calls) == 2 and tokens.get_access_token.await_count == 2


@pytest.mark.usefixtures("socket_enabled")
@pytest.mark.parametrize(
    "status,category",
    [
        (403, "history_rejected"),
        (404, "history_unsupported"),
        (405, "history_unsupported"),
        (500, "service_unavailable"),
        (302, "service_unavailable"),
    ],
)
async def test_statuses_have_their_own_category(status, category):
    async def graphql(request):
        return web.Response(status=status, headers={"Location": "/x"})

    async with (
        serve([("POST", "/graphql", graphql)]) as url,
        aiohttp.ClientSession() as http,
    ):
        client = BaconClient(http, provider(), url=url + "/devices")
        with pytest.raises(BaconError, match=rf"^{category}$"):
            await rac_energy.fetch_totals(client, "serial-A")


@pytest.mark.usefixtures("socket_enabled")
@pytest.mark.parametrize("raw", [b"not json", b"[]", b"{}", b"x" * (130 * 1024)])
async def test_invalid_bodies_are_classified(raw):
    async def graphql(request):
        return web.Response(body=raw, content_type="application/json")

    async with (
        serve([("POST", "/graphql", graphql)]) as url,
        aiohttp.ClientSession() as http,
    ):
        client = BaconClient(http, provider(), url=url + "/devices")
        with pytest.raises(BaconError, match=r"^invalid_history$"):
            await rac_energy.fetch_totals(client, "serial-A")


@pytest.mark.usefixtures("socket_enabled")
async def test_429_starts_the_shared_pause_for_every_cloud_request():
    calls = []
    backoff = RateLimitBackoff()

    async def graphql(request):
        calls.append(1)
        return web.Response(status=429, headers={"Retry-After": "120"})

    async with (
        serve([("POST", "/graphql", graphql)]) as url,
        aiohttp.ClientSession() as http,
    ):
        client = BaconClient(
            http, provider(), url=url + "/devices", clock=lambda: 100, backoff=backoff
        )
        with pytest.raises(BaconError, match=r"^rate_limited$") as error:
            await rac_energy.fetch_totals(client, "serial-A")
        assert error.value.retry_after == 120
        with pytest.raises(BaconError, match=r"^paused$"):
            await rac_energy.fetch_totals(client, "serial-A")
        # The device list is blocked by the same pause.
        with pytest.raises(BaconError, match=r"^paused$"):
            await client.get_devices()
        # And a pause another request started blocks this one before any call.
        calls.clear()
        other = RateLimitBackoff()
        other.activate(300)
        idle = provider()
        blocked = BaconClient(http, idle, url=url + "/devices", backoff=other)
        with pytest.raises(BaconError, match=r"^paused$"):
            await rac_energy.fetch_totals(blocked, "serial-A")
        # Not even a token is requested while a pause holds.
        idle.get_access_token.assert_not_awaited()
    assert calls == []


@pytest.mark.usefixtures("socket_enabled")
async def test_the_clients_own_pause_blocks_before_a_token_is_requested():
    calls = []

    async def graphql(request):
        calls.append(1)
        return web.Response(status=429, headers={"Retry-After": "120"})

    tokens = provider()
    async with (
        serve([("POST", "/graphql", graphql)]) as url,
        aiohttp.ClientSession() as http,
    ):
        client = BaconClient(http, tokens, url=url + "/devices", clock=lambda: 100)
        with pytest.raises(BaconError, match=r"^rate_limited$"):
            await rac_energy.fetch_totals(client, "serial-A")
        assert tokens.get_access_token.await_count == 1
        with pytest.raises(BaconError, match=r"^paused$"):
            await rac_energy.fetch_totals(client, "serial-A")
    assert len(calls) == 1 and tokens.get_access_token.await_count == 1


async def test_a_serial_with_routing_characters_is_refused_before_any_request():
    client = BaconClient(None, provider())
    with pytest.raises(BaconError, match="invalid_identifier"):
        await rac_energy.fetch_totals(client, "a/b")


# --- energy: refresh feed and request budget -------------------------------


class Server:
    def __init__(self, handler=None):
        self.serials: list[str] = []
        self.handler = handler

    async def graphql(self, request):
        payload = await request.json()
        self.serials.append(payload["variables"]["serialNumbers"][0])
        if self.handler is not None:
            return await self.handler(request, self)
        return answer(
            totalElectricalEnergyConsumptionHeat=100.5,
            totalThermalEnergyOutputHeat=300.0,
        )


def live_account(client):
    account = configured()[0]
    configured("cl_7000i", "serial-B", account)
    account.client = client
    return account


async def run_feeds(account, serials, interval, duration):
    rac_energy.INTERVAL = interval
    logged: list[str] = []
    tasks: list[asyncio.Task] = []

    def start(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    feeds = [rac_energy.feed(account, serial) for serial in serials]
    releases = [
        feed.subscribe(Mock(), start, logged.append) for feed in feeds for _ in range(6)
    ]
    await asyncio.sleep(duration)
    return feeds, releases, tasks, logged


@pytest.mark.usefixtures("socket_enabled")
async def test_budget_is_one_query_per_device_and_interval(monkeypatch):
    monkeypatch.setattr(rac_energy, "RETRY_AFTER_FAILURE", 0.05)
    server = Server()
    async with (
        serve([("POST", "/graphql", server.graphql)]) as url,
        aiohttp.ClientSession() as http,
    ):
        account = live_account(BaconClient(http, provider(), url=url + "/devices"))
        # Six entities per device share one feed.
        feeds, releases, _tasks, _ = await run_feeds(
            account, ["serial-A", "serial-B"], 0.2, 0.5
        )
        for release in releases:
            release()
        await asyncio.sleep(0)
    # Intervals at 0, 0.2 and 0.4 s: three per device, never six.
    for serial in ("serial-A", "serial-B"):
        assert server.serials.count(serial) == 3
    assert all(feed.requests == 3 for feed in feeds)
    assert feeds[0].current("heating") == 100.5
    assert feeds[0].current("heat_output_heating") == 300.0
    assert feeds[0].current("cooling") is None


@pytest.mark.usefixtures("socket_enabled")
async def test_no_subscriber_means_no_request(monkeypatch):
    server = Server()
    async with (
        serve([("POST", "/graphql", server.graphql)]) as url,
        aiohttp.ClientSession() as http,
    ):
        account = live_account(BaconClient(http, provider(), url=url + "/devices"))
        rac_energy.INTERVAL = 0.05
        await asyncio.sleep(0.2)
        assert server.serials == []
        # The last release stops the refresh task.
        _feeds, releases, tasks, _ = await run_feeds(account, ["serial-A"], 0.05, 0.12)
        count = len(server.serials)
        assert count >= 2
        for release in releases:
            release()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.2)
        assert len(server.serials) == count
        assert all(task.cancelled() for task in tasks)


@pytest.mark.usefixtures("socket_enabled")
async def test_a_rejected_history_service_stops_the_feed_and_logs_once():
    async def reject(request, server):
        return web.Response(status=404)

    server = Server(reject)
    async with (
        serve([("POST", "/graphql", server.graphql)]) as url,
        aiohttp.ClientSession() as http,
    ):
        account = live_account(BaconClient(http, provider(), url=url + "/devices"))
        feeds, releases, _tasks, logged = await run_feeds(
            account, ["serial-A"], 0.05, 0.3
        )
        assert server.serials == ["serial-A"]
        assert logged == ["history_unsupported"]
        assert feeds[0].stopped == "history_unsupported"
        assert feeds[0].current("heating") is None
        # A new entity does not start it again.
        again = feeds[0].subscribe(
            Mock(), lambda c: asyncio.create_task(c), logged.append
        )
        await asyncio.sleep(0.1)
        assert server.serials == ["serial-A"]
        again()
        for release in releases:
            release()


@pytest.mark.usefixtures("socket_enabled")
async def test_a_429_pauses_the_feed_until_the_pause_ends(monkeypatch):
    monkeypatch.setattr(rac_energy, "RETRY_AFTER_FAILURE", 0.05)
    hits = []

    async def limited(request, server):
        hits.append(1)
        if len(hits) == 1:
            return web.Response(status=429, headers={"Retry-After": "120"})
        return answer(totalElectricalEnergyConsumptionHeat=1.0)

    server = Server(limited)
    async with (
        serve([("POST", "/graphql", server.graphql)]) as url,
        aiohttp.ClientSession() as http,
    ):
        backoff = RateLimitBackoff()
        client = BaconClient(http, provider(), url=url + "/devices", backoff=backoff)
        account = live_account(client)
        feeds, releases, _tasks, _ = await run_feeds(account, ["serial-A"], 0.05, 0.4)
        # One attempt, then the shared pause holds every further request.
        assert len(hits) == 1
        assert feeds[0].current("heating") is None
        for release in releases:
            release()


@pytest.mark.usefixtures("socket_enabled")
async def test_an_unavailable_device_is_not_asked(monkeypatch):
    monkeypatch.setattr(rac_energy, "RETRY_AFTER_FAILURE", 0.05)
    server = Server()
    async with (
        serve([("POST", "/graphql", server.graphql)]) as url,
        aiohttp.ClientSession() as http,
    ):
        account = live_account(BaconClient(http, provider(), url=url + "/devices"))
        emit(account, "topics/info", {"online": False, "timestamp": 9})
        _feeds, releases, _tasks, _ = await run_feeds(account, ["serial-A"], 0.05, 0.2)
        assert server.serials == []
        emit(account, "topics/info", {"online": True, "timestamp": 10})
        await asyncio.sleep(0.15)
        assert server.serials
        for release in releases:
            release()


# --- energy: entities ------------------------------------------------------


def test_energy_entities_are_disabled_and_have_no_state_class():
    account, _, _ = configured()
    entities = [BaconRacEnergy(account, "serial-A", key) for key in rac_energy.TOTALS]
    for entity in entities:
        assert entity.entity_registry_enabled_default is False
        assert entity.state_class is None
        assert entity.native_unit_of_measurement == "kWh"
        assert entity.device_class == "energy"
        assert entity.native_value is None and not entity.available
    assert len({entity.unique_id for entity in entities}) == 6


def test_values_become_unavailable_when_stale(monkeypatch):
    account, _, _ = configured()
    entity = BaconRacEnergy(account, "serial-A", "heating")
    feed = entity.totals
    feed.values = {"heating": 5.0}
    clock = {"t": 1000.0}
    monkeypatch.setattr(rac_energy, "monotonic", lambda: clock["t"])
    feed.fetched_at = 1000.0
    assert entity.available and entity.native_value == 5.0
    clock["t"] += rac_energy.STALE_AFTER + 1
    assert not entity.available and entity.native_value is None


def test_energy_sensors_exist_for_climate_devices_only():
    account, _, _ = configured()
    configured("cl_7000i", "serial-B", account)
    added, _ = collected(account, "sensor")
    assert len(added) == 12
    other = Device("serial-C", model="hpwh_vw_v1")
    account.devices["serial-C"] = other
    added, _ = collected(account, "sensor")
    assert all(entity.serial != "serial-C" for entity in added)


def test_no_account_without_climate_gets_no_energy_sensor():
    account, _, _ = configured()
    account.devices["serial-A"].model = "hpwh_vw_v1"
    account.devices["serial-B"].model = "dry_4000i_v2"
    assert collected(account, "sensor")[0] == []
    assert collected(account, "number")[0] == []


def test_feed_is_shared_per_account_and_serial():
    account, _, _ = configured()
    assert rac_energy.feed(account, "serial-A") is rac_energy.feed(account, "serial-A")
    assert rac_energy.feed(account, "serial-A") is not rac_energy.feed(
        account, "serial-B"
    )


# --- real Home Assistant: registry, defaults, services ---------------------

ROOT = "custom_components.bosch_buderus_heating"


async def _setup(hass, monkeypatch, *, enabled, fetch=None):
    from tests.test_bacon_ha import entry

    if enabled:
        for cls in (BaconRacTimer, BaconRacEnergy):
            monkeypatch.setattr(cls, "_attr_entity_registry_enabled_default", True)
    if fetch is not None:
        monkeypatch.setattr(rac_energy, "fetch_totals", fetch)
    config = entry(hass)
    account, _, data = configured()
    with_timers(account, data, off=int(NOW) + 600)
    writes = responding(account, data)

    async def wait():
        await asyncio.Event().wait()

    return config, account, data, writes, wait, er.async_get(hass)


@pytest.mark.usefixtures("enable_custom_integrations", "legacy_platform_runtime")
async def test_new_entities_are_registered_disabled_by_default(hass, monkeypatch):
    from unittest.mock import patch

    from custom_components.bosch_buderus_heating.const import DOMAIN

    calls = []

    async def fetch(client, serial):
        calls.append(serial)
        return {"heating": 1.0}

    config, account, _, _, wait, registry = await _setup(
        hass, monkeypatch, enabled=False, fetch=fetch
    )
    with (
        patch(ROOT + ".BaconAccount", return_value=account),
        patch.object(account, "run", side_effect=wait),
    ):
        assert await hass.config_entries.async_setup(config.entry_id)
        await hass.async_block_till_done()
        numbers = [
            e
            for e in registry.entities.values()
            if e.platform == DOMAIN and e.domain == "number"
        ]
        sensors = [
            e
            for e in registry.entities.values()
            if e.platform == DOMAIN
            and e.domain == "sensor"
            and ":rac_energy_" in e.unique_id
        ]
        assert sorted(e.unique_id.rsplit(":", 1)[1] for e in numbers) == [
            "rac_timer_off",
            "rac_timer_on",
        ]
        assert len(sensors) == 6
        assert all(
            e.disabled_by == er.RegistryEntryDisabler.INTEGRATION
            for e in (*numbers, *sensors)
        )
        # Disabled entities are never added to Home Assistant: no query at all.
        await asyncio.sleep(0.05)
        assert calls == []
        assert await hass.config_entries.async_unload(config.entry_id)
        await hass.async_block_till_done()


@pytest.mark.usefixtures("enable_custom_integrations", "legacy_platform_runtime")
async def test_enabled_entities_show_values_and_write_through_the_service(
    hass, monkeypatch
):
    from unittest.mock import patch

    from custom_components.bosch_buderus_heating.const import DOMAIN

    calls = []

    async def fetch(client, serial):
        calls.append(serial)
        return {"heating": 1234.5, "heat_output_heating": 3000.0}

    config, account, _, writes, wait, registry = await _setup(
        hass, monkeypatch, enabled=True, fetch=fetch
    )
    with (
        patch(ROOT + ".BaconAccount", return_value=account),
        patch.object(account, "run", side_effect=wait),
    ):
        assert await hass.config_entries.async_setup(config.entry_id)
        await hass.async_block_till_done()
        identity = BaconRacTimer(account, "serial-A", "on").unique_id.rsplit(":", 1)[0]
        on = registry.async_get_entity_id("number", DOMAIN, identity + ":rac_timer_on")
        off = registry.async_get_entity_id(
            "number", DOMAIN, identity + ":rac_timer_off"
        )
        assert hass.states.get(on).state == "0"
        assert hass.states.get(off).state == "10"
        assert hass.states.get(on).attributes["unit_of_measurement"] == "min"
        await hass.services.async_call(
            "number", "set_value", {"entity_id": on, "value": 25}, blocking=True
        )
        assert writes == [{"onTimestamp": (int(NOW) // 60 + 25) * 60}]
        assert hass.states.get(on).state == "25"
        with pytest.raises(Exception):  # noqa: B017
            await hass.services.async_call(
                "number", "set_value", {"entity_id": on, "value": 2000}, blocking=True
            )
        assert len(writes) == 1
        heating = registry.async_get_entity_id(
            "sensor", DOMAIN, identity + ":rac_energy_heating"
        )
        cooling = registry.async_get_entity_id(
            "sensor", DOMAIN, identity + ":rac_energy_cooling"
        )
        await hass.async_block_till_done()
        assert hass.states.get(heating).state == "1234.5"
        assert hass.states.get(heating).attributes["unit_of_measurement"] == "kWh"
        assert "state_class" not in hass.states.get(heating).attributes
        assert hass.states.get(cooling).state == "unavailable"
        # Twelve entities (six per device kind) share one query per device.
        assert calls == ["serial-A"]
        assert await hass.config_entries.async_unload(config.entry_id)
        await hass.async_block_till_done()
        # Unloading ends the refresh task.
        assert rac_energy.feed(account, "serial-A")._task is None


def test_a_timer_time_keeps_a_lead_for_the_device():
    """W-6: a time less than 10 seconds ahead moves to the next minute."""
    assert rac_timers.target_time(1, 61.0) == 120
    assert rac_timers.target_time(1, 61.0) - 61.0 >= rac_timers.MIN_LEAD_SECONDS
    assert rac_timers.target_time(1, 55.0) == 120
    assert rac_timers.target_time(1, 50.0) == 60
    assert rac_timers.target_time(1, 119.99) == 180
    assert rac_timers.target_time(2, 61.0) == 180
    for second in range(0, 120):
        assert rac_timers.target_time(1, float(second)) - second >= 10
