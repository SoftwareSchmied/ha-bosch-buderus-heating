"""Fix round beta.3 (cloud): repeated rejection, energy backoff, leak, time zone."""

import asyncio
import gc
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from aiohttp import web

from custom_components.bosch_buderus_heating.bacon import rac_energy
from custom_components.bosch_buderus_heating.bacon.client import BaconClient
from custom_components.bosch_buderus_heating.bacon.models import BaconError
from custom_components.bosch_buderus_heating.pointt.metrics import (
    resource_path_category,
)
from tests.test_bacon_contracts import provider
from tests.test_bacon_rac import configured
from tests.test_bacon_session import environment
from tests.test_pointt_transport_and_client import serve


@pytest.fixture(autouse=True)
def restore_constants(monkeypatch):
    monkeypatch.setattr(rac_energy, "INTERVAL", rac_energy.INTERVAL)
    monkeypatch.setattr(rac_energy, "MAX_RETRY", rac_energy.MAX_RETRY)
    monkeypatch.setattr(
        rac_energy, "RETRY_AFTER_FAILURE", rac_energy.RETRY_AFTER_FAILURE
    )


# -- C1: a rejected live report is remembered across connections ---------------


@pytest.mark.usefixtures("socket_enabled")
async def test_persistent_401_over_many_connections_means_one_request_no_refresh():
    """Real client and session handling over several simulated reconnects."""
    calls = []
    tokens = provider()

    async def live(request):
        calls.append(1)
        return web.Response(status=401)

    async with (
        serve([("POST", "/live", live)]) as url,
        aiohttp.ClientSession() as http,
    ):
        account = environment()
        account.client = BaconClient(http, tokens, url=url + "/devices")
        notified = Mock()
        account.on_error = notified
        for _ in range(5):
            account._live.new_connection()  # what run_connection does
            await account._report_live()
    assert len(calls) == 1
    assert tokens.get_access_token.await_count == 1
    assert account._live.disabled and account._live.disabled_total == 1
    notified.assert_not_called()
    assert account.connected and account.error is None


# -- C2: see tests/test_connection_status.py (second-cloud devices) ------------


# -- C3: the shared feeds do not keep an account alive -------------------------


def test_the_energy_feed_does_not_keep_its_account_alive():
    account = configured()[0]
    feed = rac_energy.feed(account, "serial-A")
    assert feed.account is account
    before = len(rac_energy._FEEDS)
    del account
    gc.collect()
    assert len(rac_energy._FEEDS) == before - 1
    with pytest.raises(BaconError, match="source_stopped"):
        _ = feed.account
    assert feed.current("heating") is None


# -- C4: failures of the energy query back off to one interval -----------------


def test_backoff_doubles_up_to_one_interval_and_a_pause_is_no_failure():
    feed = rac_energy.RacEnergy(lambda: None, "serial-A")
    waits = [feed._backoff(0.0) for _ in range(5)]
    assert waits == [300.0, 600.0, 1200.0, 1800.0, 1800.0]
    assert feed._backoff(2400.0) == 2400.0


@pytest.mark.usefixtures("socket_enabled")
@pytest.mark.parametrize("failure", ["errors", "5xx", "garbage"])
async def test_a_failing_energy_query_waits_longer_each_time(monkeypatch, failure):
    async def graphql(request):
        if failure == "5xx":
            return web.Response(status=503)
        if failure == "garbage":
            return web.Response(text="not json")
        return web.json_response({"errors": [{"message": "Not Authorized"}]})

    waits = []
    real_sleep = asyncio.sleep

    async def record(delay):
        waits.append(delay)
        await real_sleep(0)

    async with (
        serve([("POST", "/graphql", graphql)]) as url,
        aiohttp.ClientSession() as http,
    ):
        account = configured()[0]
        account.client = BaconClient(http, provider(), url=url + "/devices")
        feed = rac_energy.feed(account, "serial-A")
        monkeypatch.setattr(rac_energy.asyncio, "sleep", record)
        task = asyncio.create_task(feed._run(Mock()))
        for _ in range(300):
            await real_sleep(0.01)
            if len(waits) >= 5:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert waits[:5] == [300.0, 600.0, 1200.0, 1800.0, 1800.0]


async def test_an_unexpected_exception_does_not_end_the_feed(monkeypatch):
    waits = []

    async def record(delay):
        waits.append(delay)
        if len(waits) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(rac_energy.asyncio, "sleep", record)
    monkeypatch.setattr(
        rac_energy, "fetch_totals", AsyncMock(side_effect=RecursionError)
    )
    account = configured()[0]
    feed = rac_energy.feed(account, "serial-A")
    with pytest.raises(asyncio.CancelledError):
        await feed._run(Mock())
    assert waits == [300.0, 600.0]
    assert feed.requests == 2


async def test_a_success_resets_the_backoff(monkeypatch):
    results = iter(
        [BaconError("service_unavailable"), {"heating": 1.0}, BaconError("connection")]
    )
    waits = []

    async def fetch(client, serial):
        result = next(results)
        if isinstance(result, BaseException):
            raise result
        return result

    async def record(delay):
        waits.append(delay)
        if len(waits) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(rac_energy.asyncio, "sleep", record)
    monkeypatch.setattr(rac_energy, "fetch_totals", fetch)
    account = configured()[0]
    feed = rac_energy.feed(account, "serial-A")
    with pytest.raises(asyncio.CancelledError):
        await feed._run(Mock())
    assert waits == [300.0, rac_energy.INTERVAL, 300.0]


# -- U2: GraphQL time zone comes from Home Assistant ---------------------------


@pytest.mark.parametrize("hours", [0, 1, 2, -5])
def test_the_offset_follows_the_home_assistant_zone_not_the_process(hours):
    local = datetime(2026, 7, 1, 12, tzinfo=timezone(timedelta(hours=hours)))
    with patch.object(rac_energy.dt_util, "now", return_value=local):
        assert rac_energy._offset_minutes() == hours * 60


# -- C5: wallbox paths get their own category -----------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/rest/v1/cp0/chargelog", "cp0"),
        ("rest/v1/cp1/charging/state?x=1", "cp1"),
        ("/rest/v1/abc123def/state", "rest"),
        ("/rest/v1/cp0", "cp0"),
        ("/rest", "rest"),
        ("/rest/v1/CP0/x", "rest"),
        ("/heatingCircuits/hc1/x", "heatingCircuits"),
    ],
)
def test_wallbox_paths_have_a_category_without_identifiers(path, expected):
    assert resource_path_category(path) == expected
