"""Server-requested cloud pauses: full duration, HTTP dates and restarts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web

from custom_components.bosch_buderus_heating.auth_lifecycle import (
    ACCOUNT_BACKOFFS,
    connection_backoff,
    forget_connection_auth,
)
from custom_components.bosch_buderus_heating.bacon.client import BaconClient
from custom_components.bosch_buderus_heating.bacon.models import BaconError
from custom_components.bosch_buderus_heating.cloud_pauses import (
    _STORES,
    STORAGE_KEY,
    CloudPauseStore,
    cloud_pause_store,
)
from custom_components.bosch_buderus_heating.connection_runtime import SourceFailure
from custom_components.bosch_buderus_heating.coordinator import (
    MAX_RATE_LIMIT_BACKOFF,
)
from custom_components.bosch_buderus_heating.pointt import RateLimited
from custom_components.bosch_buderus_heating.pointt.retry_after import (
    MAX_CLOUD_PAUSE_SECONDS,
    bounded_cloud_pause,
    parse_retry_after,
)
from custom_components.bosch_buderus_heating.pointt.transport import (
    PointTTransport,
    RateLimitBackoff,
)
from tests.ha_test_helpers import flush_store
from tests.test_bacon_contracts import provider, serve

NOW = 1_800_000_000.0
KEY = ("entry-1", "cloud_a")


class Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _http_date(seconds_from_now: float) -> str:
    return format_datetime(
        datetime.now(UTC) + timedelta(seconds=seconds_from_now), usegmt=True
    )


# ---------------------------------------------------------------- P09a probes


def test_explicit_two_hour_server_pause_is_not_shortened() -> None:
    backoff = RateLimitBackoff(clock=lambda: 1000.0)
    backoff.activate(7200)
    with pytest.raises(RateLimited) as error:
        backoff.raise_if_active()
    assert error.value.retry_after == 7200


def test_http_date_server_pause_is_not_replaced_with_five_minutes() -> None:
    with pytest.raises(RateLimited) as error:
        PointTTransport._raise_http_error(429, {"Retry-After": _http_date(7200)}, None)
    assert error.value.retry_after is not None
    assert 7195 <= error.value.retry_after <= 7200
    backoff = RateLimitBackoff(clock=lambda: 1000.0)
    backoff.activate(error.value.retry_after)
    assert 7195 <= backoff.remaining <= 7200


# ------------------------------------------------------------ interpretation


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("  120 ", 120.0),
        ("-5", 0.0),
        ("nan", None),
        ("inf", None),
        ("soon", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),
        ("Thu, 01 Jan 2027 00:10:00", 600.0),
    ],
)
def test_retry_after_accepts_seconds_and_http_dates(header, expected) -> None:
    now = datetime(2027, 1, 1, tzinfo=UTC).timestamp()
    assert parse_retry_after(header, now=now) == expected


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (None, 300),
        (float("nan"), 300),
        (0, 60),
        (30, 60),
        (5400, 5400),
        (86400, 86400),
        (10**9, MAX_CLOUD_PAUSE_SECONDS),
    ],
)
def test_pause_bounds_only_reject_nonsensical_values(requested, expected) -> None:
    assert bounded_cloud_pause(requested) == expected


def test_runtime_and_coordinator_use_the_same_safety_bound() -> None:
    assert SourceFailure(retry_after=7200).retry_after == 7200
    assert SourceFailure(retry_after=10**9).retry_after == MAX_CLOUD_PAUSE_SECONDS
    assert SourceFailure(retry_after=float("inf")).retry_after == 300
    assert MAX_RATE_LIMIT_BACKOFF.total_seconds() == MAX_CLOUD_PAUSE_SECONDS


def test_activation_never_shortens_and_reports_changes() -> None:
    clock = Clock(100.0)
    changes = []
    backoff = RateLimitBackoff(clock=clock, on_change=lambda: changes.append(1))
    backoff.activate(7200)
    backoff.activate(120)
    assert backoff.remaining == 7200
    assert changes == [1]
    clock.value += 7000
    backoff.activate(600)
    assert backoff.remaining == 600
    assert changes == [1, 1]
    backoff.restore(10)
    backoff.restore(float("nan"))
    assert backoff.remaining == 600
    assert backoff.restored is False


@pytest.mark.usefixtures("socket_enabled")
async def test_additional_cloud_devices_honor_http_date_pauses() -> None:
    calls = []

    async def response(_request):
        calls.append(1)
        return web.Response(status=429, headers={"Retry-After": _http_date(5400)})

    shared = RateLimitBackoff()
    async with (
        serve([("GET", "/", response)]) as url,
        aiohttp.ClientSession() as session,
    ):
        client = BaconClient(session, provider(), url=url, backoff=shared)
        with pytest.raises(BaconError, match="rate_limited") as error:
            await client.get_devices()
        assert 5395 <= error.value.retry_after <= 5400
        # The shared pause blocks locally and is not a new server limit.
        with pytest.raises(BaconError, match="paused"):
            await client.get_devices()
    assert calls == [1]
    assert 5395 <= shared.remaining <= 5400


# ----------------------------------------------------------------- restarts


async def _restart(hass, wall: Clock, clock: Clock) -> CloudPauseStore:
    store = CloudPauseStore(hass, wall_clock=wall, clock=clock)
    await store.async_initialize()
    return store


async def test_pause_survives_restart_and_counts_real_downtime(
    hass, hass_storage
) -> None:
    wall, clock = Clock(NOW), Clock(500.0)
    first = await _restart(hass, wall, clock)
    backoff = RateLimitBackoff(clock=clock)
    first.attach(KEY, backoff)
    backoff.activate(7200)
    await flush_store(first.store)
    assert hass_storage[STORAGE_KEY]["data"]["pauses"] == [
        {
            "owner": "entry-1",
            "connection": "cloud_a",
            "remaining": 7200,
            "saved_at": NOW,
        }
    ]

    # HA was down for 30 minutes; a new process has a new monotonic clock.
    wall.value += 1800
    clock.value = 10.0
    second = await _restart(hass, wall, clock)
    restored = RateLimitBackoff(clock=clock)
    second.attach(KEY, restored)
    assert restored.remaining == pytest.approx(5400)
    assert restored.restored is True
    with pytest.raises(RateLimited):
        restored.raise_if_active()
    assert second.diagnostics(KEY) == {
        "active": True,
        "remaining_seconds": 5400.0,
        "restored_after_restart": True,
        "persistence_available": True,
    }


@pytest.mark.parametrize(
    ("wall_shift", "expected"),
    [(-86400, 7200), (0, 7200), (600, 6600), (7200, 0), (90000, 0)],
)
async def test_restored_pause_is_never_longer_than_saved(
    hass, hass_storage, wall_shift, expected
) -> None:
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "pauses": [
                {
                    "owner": "entry-1",
                    "connection": "cloud_a",
                    "remaining": 7200,
                    "saved_at": NOW,
                }
            ]
        },
    }
    clock = Clock(0.0)
    store = await _restart(hass, Clock(NOW + wall_shift), clock)
    backoff = RateLimitBackoff(clock=clock)
    store.attach(KEY, backoff)
    assert backoff.remaining == pytest.approx(expected)


async def test_saved_pause_waits_for_its_access_and_stays_saved(
    hass, hass_storage
) -> None:
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "pauses": [
                {
                    "owner": "entry-1",
                    "connection": "cloud_a",
                    "remaining": 900,
                    "saved_at": NOW,
                },
                {
                    "owner": "entry-2",
                    "connection": "cloud_b",
                    "remaining": 10**9,
                    "saved_at": NOW,
                },
                {
                    "owner": "bad owner!",
                    "connection": "x",
                    "remaining": 50,
                    "saved_at": NOW,
                },
                {
                    "owner": "entry-3",
                    "connection": "y",
                    "remaining": "5",
                    "saved_at": NOW,
                },
                {
                    "owner": "entry-4",
                    "connection": "z",
                    "remaining": True,
                    "saved_at": NOW,
                },
                "broken",
            ]
        },
    }
    wall, clock = Clock(NOW), Clock(0.0)
    store = await _restart(hass, wall, clock)
    assert store.diagnostics(("entry-2", "cloud_b"))["remaining_seconds"] == (
        MAX_CLOUD_PAUSE_SECONDS
    )
    # Another access changes its pause; unattached pauses must stay saved.
    other = RateLimitBackoff(clock=clock)
    store.attach(("entry-5", "cloud_c"), other)
    other.activate(120)
    await flush_store(store.store)
    rows = {
        (row["owner"], row["connection"]): row["remaining"]
        for row in hass_storage[STORAGE_KEY]["data"]["pauses"]
    }
    assert rows == {
        ("entry-1", "cloud_a"): 900,
        ("entry-2", "cloud_b"): MAX_CLOUD_PAUSE_SECONDS,
        ("entry-5", "cloud_c"): 120,
    }


@pytest.mark.parametrize("stored", [None, ["unexpected"], {"pauses": "no"}])
async def test_damaged_or_missing_store_starts_without_pauses(
    hass, hass_storage, stored
) -> None:
    if stored is not None:
        hass_storage[STORAGE_KEY] = {
            "version": 1,
            "minor_version": 1,
            "key": STORAGE_KEY,
            "data": stored,
        }
    store = await _restart(hass, Clock(NOW), Clock(0.0))
    backoff = RateLimitBackoff()
    store.attach(KEY, backoff)
    assert backoff.remaining == 0
    assert store.diagnostics(KEY)["active"] is False


async def test_unreadable_store_does_not_block_setup(hass, caplog) -> None:
    store = CloudPauseStore(hass)
    with patch.object(store.store, "async_load", side_effect=OSError("disk")):
        await store.async_initialize()
    assert "could not be read" in caplog.text
    assert store.diagnostics(KEY)["persistence_available"] is True


async def test_pause_started_before_loading_is_saved_afterwards(
    hass, hass_storage
) -> None:
    clock = Clock(0.0)
    store = CloudPauseStore(hass, wall_clock=Clock(NOW), clock=clock)
    backoff = RateLimitBackoff(clock=clock)
    store.attach(KEY, backoff)
    backoff.activate(600)
    assert store.diagnostics(KEY)["persistence_available"] is False
    await store.async_initialize()
    await flush_store(store.store)
    assert hass_storage[STORAGE_KEY]["data"]["pauses"][0]["remaining"] == 600


# -------------------------------------------------- saved access ownership


async def test_shared_access_pause_is_persisted_and_removed_with_the_access(
    hass, hass_storage
) -> None:
    hass.data.pop(_STORES, None)
    store = cloud_pause_store(hass)
    await store.async_initialize()
    backoff = connection_backoff(hass, *KEY)
    assert connection_backoff(hass, *KEY) is backoff
    backoff.activate(3600 * 3)
    await flush_store(store.store)
    assert len(hass_storage[STORAGE_KEY]["data"]["pauses"]) == 1

    # A replacement login keeps the same access and therefore the same pause.
    hass.data[ACCOUNT_BACKOFFS].clear()
    store2 = CloudPauseStore(hass)
    hass.data[_STORES] = store2
    await store2.async_initialize()
    assert connection_backoff(hass, *KEY).remaining > 3600 * 3 - 5

    # Removing the access deletes its pause; a new access starts unpaused.
    forget_connection_auth(hass, *KEY)
    await flush_store(store2.store)
    assert hass_storage[STORAGE_KEY]["data"]["pauses"] == []
    assert connection_backoff(hass, "entry-1", "cloud_new").remaining == 0


async def test_removing_an_entry_removes_all_its_pauses(hass, hass_storage) -> None:
    store = CloudPauseStore(hass, wall_clock=Clock(NOW), clock=Clock(0.0))
    await store.async_initialize()
    for key in (("entry-1", "a"), ("entry-1", "b"), ("entry-2", "a")):
        backoff = RateLimitBackoff(clock=Clock(0.0))
        store.attach(key, backoff)
        backoff.activate(900)
    store.discard_owner("entry-1")
    store.discard(("entry-9", "never-attached"))
    await flush_store(store.store)
    assert [
        (row["owner"], row["connection"])
        for row in hass_storage[STORAGE_KEY]["data"]["pauses"]
    ] == [("entry-2", "a")]
