"""The active-client report: HTTP contract of the client and session handling."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from aiohttp import web

from custom_components.bosch_buderus_heating.bacon import session as session_module
from custom_components.bosch_buderus_heating.bacon.client import BaconClient
from custom_components.bosch_buderus_heating.bacon.live_report import (
    LiveReport,
    age_bucket,
)
from custom_components.bosch_buderus_heating.bacon.models import LIVE_URL, BaconError
from custom_components.bosch_buderus_heating.pointt.transport import RateLimitBackoff
from tests.test_bacon_contracts import provider, token
from tests.test_bacon_session import environment
from tests.test_pointt_transport_and_client import serve

pytestmark = pytest.mark.usefixtures("socket_enabled")


def test_live_url_is_derived_next_to_the_device_list():
    assert BaconClient(None, provider()).live_url == LIVE_URL
    assert LIVE_URL.endswith("/v1/users/self/live")
    client = BaconClient(None, provider(), url="http://127.0.0.1:1/devices")
    assert client.live_url == "http://127.0.0.1:1/live"
    explicit = BaconClient(None, provider(), live_url="http://example.invalid/x")
    assert explicit.live_url == "http://example.invalid/x"


async def test_report_is_an_empty_post_with_bearer_and_user_agent():
    seen = []

    async def live(request):
        seen.append(
            (
                request.method,
                request.headers["Authorization"],
                request.headers["User-Agent"],
                await request.read(),
            )
        )
        return web.Response(status=204)

    async with (
        serve([("POST", "/live", live)]) as url,
        aiohttp.ClientSession() as http,
    ):
        client = BaconClient(http, provider(), url=url + "/devices")
        await client.report_live()
    assert seen == [("POST", "Bearer " + token(), "DashApp/3.7.0 (iOS-Release)", b"")]


async def test_a_401_is_a_rejected_report_without_a_token_refresh():
    calls = []
    tokens = provider()

    async def live(request):
        calls.append(1)
        return web.Response(status=401)

    async with (
        serve([("POST", "/live", live)]) as url,
        aiohttp.ClientSession() as http,
    ):
        client = BaconClient(http, tokens, url=url + "/devices")
        with pytest.raises(BaconError, match=r"^live_rejected$"):
            await client.report_live()
    # One request, no forced refresh: the optional report proves no access.
    assert len(calls) == 1 and tokens.get_access_token.await_count == 1
    assert all(
        call.kwargs.get("force_refresh") is not True
        for call in tokens.get_access_token.await_args_list
    )


@pytest.mark.parametrize(
    "status,category",
    [
        (401, "live_rejected"),
        (403, "live_rejected"),
        (404, "live_unsupported"),
        (405, "live_unsupported"),
        (500, "service_unavailable"),
        (302, "service_unavailable"),
    ],
)
async def test_other_statuses_have_their_own_category(status, category):
    async def live(request):
        return web.Response(status=status, headers={"Location": "/x"})

    async with (
        serve([("POST", "/live", live)]) as url,
        aiohttp.ClientSession() as http,
    ):
        client = BaconClient(http, provider(), url=url + "/devices")
        with pytest.raises(BaconError, match=rf"^{category}$"):
            await client.report_live()


async def test_429_starts_the_shared_pause_and_blocks_the_next_request():
    calls = []
    backoff = RateLimitBackoff()

    async def live(request):
        calls.append(1)
        return web.Response(status=429, headers={"Retry-After": "120"})

    async with (
        serve([("POST", "/live", live)]) as url,
        aiohttp.ClientSession() as http,
    ):
        client = BaconClient(
            http, provider(), url=url + "/devices", clock=lambda: 100, backoff=backoff
        )
        with pytest.raises(BaconError, match=r"^rate_limited$") as error:
            await client.report_live()
        assert error.value.retry_after == 120
        # The shared pause also stops the device list and the next report.
        with pytest.raises(BaconError, match=r"^paused$"):
            await client.report_live()
        with pytest.raises(BaconError, match=r"^paused$"):
            await client.get_devices()
    assert calls == [1]


async def test_timeout_and_transport_errors_are_connection_errors():
    session = SimpleNamespace(
        post=lambda *a, **k: (_ for _ in ()).throw(TimeoutError()),
    )
    with pytest.raises(BaconError, match=r"^connection$"):
        await BaconClient(session, provider()).report_live()


def test_age_classes_and_counters_hold_no_identifiers():
    assert [
        age_bucket(value) for value in (None, 0, 299, 300, 899, 3599, 21599, 7e4)
    ] == [
        "never",
        "under_5_min",
        "under_5_min",
        "under_15_min",
        "under_15_min",
        "under_1_h",
        "under_6_h",
        "6_h_or_more",
    ]
    report = LiveReport()
    for category in ("rate_limited", "service_unavailable", "connection", "paused"):
        report.failed(category)
    report.failed("something_else")
    assert report.errors == {"4xx": 1, "5xx": 1, "network": 1, "paused": 1, "other": 1}
    assert not report.disabled
    report.failed("live_unsupported")
    assert report.disabled and report.disabled_total == 1
    report.new_connection()
    assert not report.disabled and report.disabled_total == 1
    # A rejection is remembered across connections.
    report.failed("live_rejected")
    report.new_connection()
    assert report.disabled and report.disabled_total == 2


# -- session handling ---------------------------------------------------------


def logged(account):
    account.client.report_live = AsyncMock()
    return account.client.report_live


async def test_a_report_counts_and_needs_a_claimed_device():
    account = environment()
    report = logged(account)
    await account._report_live()
    assert report.await_count == 1 and account._live.sent == 1
    account._claimed.clear()
    await account._report_live()
    assert report.await_count == 1


@pytest.mark.parametrize("category", ["live_unsupported", "live_rejected"])
async def test_a_rejected_report_stops_and_logs_once(category, caplog):
    account = environment()
    report = logged(account)
    report.side_effect = BaconError(category)
    caplog.set_level("INFO", logger=session_module.__name__)
    await account._report_live()
    await account._report_live()
    assert report.await_count == 1
    assert account._live.disabled and account._live.errors["4xx"] == 1
    assert sum("active-client report" in item.message for item in caplog.records) == 1
    # Only "unsupported" is tried again on a new connection; a rejection is not.
    account._live.new_connection()
    await account._report_live()
    assert report.await_count == (2 if category == "live_unsupported" else 1)
    assert sum("active-client report" in item.message for item in caplog.records) == 1


@pytest.mark.parametrize(
    "error",
    [
        BaconError("connection"),
        BaconError("service_unavailable"),
        BaconError("paused"),
        BaconError("rate_limited", 90),
        TimeoutError(),
        RuntimeError("unexpected"),
    ],
)
async def test_other_failures_never_end_the_connection(error):
    account = environment()
    logged(account).side_effect = error
    on_error = Mock()
    account.on_error = lambda err: on_error(err.category)
    await account._report_live()
    assert account.connected and sum(account._live.errors.values()) == 1
    assert account.error is None
    assert on_error.call_count == (
        1 if getattr(error, "category", "") == "rate_limited" else 0
    )


@pytest.mark.parametrize("category", sorted(session_module.FATAL_SYNC_ERRORS))
async def test_fatal_categories_still_end_the_connection(category):
    account = environment()
    logged(account).side_effect = BaconError(category)
    with pytest.raises(BaconError, match=category):
        await account._report_live()


async def test_the_loop_reports_first_at_once_then_every_300_seconds():
    """Real maintenance cycles: first report after subscribing, then per interval."""
    account = environment()
    account.connected = False
    account.generation = 0
    clock = {"now": 1000.0}
    reports = []
    account.client.report_live = AsyncMock(
        side_effect=lambda: reports.append(clock["now"])
    )
    account.client.access_token = AsyncMock(return_value=token())
    account.client.get_devices = AsyncMock(return_value=("serial-A", "serial-B"))
    account._publish = AsyncMock()
    steps = iter(range(15))

    class Broker:
        async def subscribe(self, *args, **kwargs):
            return [SimpleNamespace(value=1)]

        @property
        def messages(self):
            async def forever():
                await asyncio.Event().wait()
                yield None

            return forever()

    mqtt = Broker()

    async def enter(*args, **kwargs):
        return mqtt

    account._enter = enter

    async def tick(receiver):
        clock["now"] += 60
        if next(steps, None) is None:
            raise asyncio.CancelledError

    account._wait_for_maintenance = tick
    with patch(session_module.__name__ + ".monotonic", lambda: clock["now"]):
        task = asyncio.create_task(account.run_connection())
        with pytest.raises(asyncio.CancelledError):
            await task
    # 16 maintenance steps of 60 s: at the start, then once the interval is over.
    assert reports == [1000.0, 1300.0, 1600.0, 1900.0]
