"""Read-only lifetime energy totals of the additional-cloud air conditioners.

One bounded GraphQL query per device and interval reads six totals in kWh
from the history service of the same cloud. The query is made only while at
least one of the energy entities of that device is enabled, with the bearer
token, the semaphore and the shared pause of the other cloud requests.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic, time
from typing import TYPE_CHECKING, Any, Final, cast
from weakref import WeakKeyDictionary, ref

import aiohttp
from homeassistant.util import dt as dt_util

from .client import BaconClient
from .models import CLAIM_URL, MAX_BYTES, USER_AGENT, BaconError, identifier

if TYPE_CHECKING:
    from .access import BaconAccess

GRAPHQL_URL: Final = "https://history.euc1.bacon.bosch-tt-cw.com/graphql"
# These totals are read at most every 30 minutes.
INTERVAL: Final = 1800.0
# Retry after a failed query, never faster than the pause the service named.
RETRY_AFTER_FAILURE: Final = 300.0
# Longest wait between two queries after repeated failures (one interval).
MAX_RETRY: Final = INTERVAL
# A total older than this is shown as unavailable.
STALE_AFTER: Final = 4 * INTERVAL
# Largest plausible total in kWh (rejects garbage, not a manufacturer limit).
_MAX_KWH: Final = 1.0e9
# The service rejects the request itself: stop for the rest of the session.
STOPPING: Final = frozenset({"history_rejected", "history_unsupported"})
_PAUSES: Final = frozenset({"paused", "rate_limited"})
# Sign-in problems belong to the main connection; this feed only waits.
_ENDED: Final = "source_stopped"

# Sensor key -> field of the response item.
TOTALS: Final = {
    "heating": "totalElectricalEnergyConsumptionHeat",
    "cooling": "totalElectricalEnergyConsumptionCool",
    "dry": "totalElectricalEnergyConsumptionDry",
    "fan": "totalElectricalEnergyConsumptionFan",
    "heat_output_heating": "totalThermalEnergyOutputHeat",
    "heat_output_cooling": "totalThermalEnergyOutputCool",
}
QUERY: Final = (
    "query getAdditionalInformation($serialNumbers: [ID!]!, "
    "$currentTimestamp: AWSTimestamp!, $currentTimeZoneOffset: Int!, "
    "$firstDayOfWeek: WEEKDAY!) { consumption: cumulateRacEmonRecords("
    "serialNumbers: $serialNumbers, timestamp: { between: [0,$currentTimestamp] "
    "timeZoneOffset: $currentTimeZoneOffset firstDayOfWeek: $firstDayOfWeek } , "
    "interval: YEARLY, transformation: LATEST, groupFunction: SUM, limit: 1) "
    "{ items { timestamp totalElectricalEnergyConsumptionHeat "
    "totalElectricalEnergyConsumptionCool totalElectricalEnergyConsumptionDry "
    "totalElectricalEnergyConsumptionFan totalThermalEnergyOutputCool "
    "totalThermalEnergyOutputHeat } } }"
)


def graphql_url(client: BaconClient) -> str:
    """The history endpoint next to the claiming endpoint of this client."""
    if client.url != CLAIM_URL and client.url.endswith("/devices"):
        return client.url.removesuffix("devices") + "graphql"
    return GRAPHQL_URL


def _offset_minutes() -> int:
    """Offset of the time zone set in Home Assistant, not of the process."""
    offset = dt_util.now().utcoffset()
    return 0 if offset is None else int(offset.total_seconds() // 60)


def _number(value: object, default: float) -> float:
    """A finite number (never a bool), else the default."""
    if (type(value) is int or type(value) is float) and math.isfinite(value):
        return float(value)
    return default


def totals(body: object) -> dict[str, float]:
    """Valid totals of the newest item; fields that are not valid are left out."""
    data = body.get("data") if isinstance(body, dict) else None
    consumption = data.get("consumption") if isinstance(data, dict) else None
    items = consumption.get("items") if isinstance(consumption, dict) else None
    if not isinstance(items, list):
        raise BaconError("invalid_history")
    candidates = [item for item in items if isinstance(item, dict)]
    if not candidates:
        return {}
    newest = max(candidates, key=lambda item: _number(item.get("timestamp"), -1.0))
    result: dict[str, float] = {}
    for key, name in TOTALS.items():
        value = _number(newest.get(name), -1.0)
        if 0 <= value <= _MAX_KWH:
            result[key] = value
    return result


async def fetch_totals(client: BaconClient, serial: str) -> dict[str, float]:
    """One query for one device; the shared 429 pause applies."""
    client.check_request()
    try:
        result = await _fetch(client, identifier(serial))
    except Exception:
        client._guard()
        raise
    client._guard()
    return result


async def _fetch(client: BaconClient, serial: str) -> dict[str, float]:
    if client.clock() < client.retry_at:
        raise BaconError("paused", client.retry_at - client.clock())
    token = await client.access_token()
    for attempt in range(2):
        async with client._semaphore:
            client.check_request()
            if client.clock() < client.retry_at:
                raise BaconError("paused", client.retry_at - client.clock())
            result = await _request(client, serial, token, retry=attempt == 0)
        if result is not None:
            return result
        token = await client.access_token(rejected=token)
    raise BaconError("history_rejected")


async def _request(
    client: BaconClient, serial: str, token: str, *, retry: bool
) -> dict[str, float] | None:
    """One bounded POST; None requests one token refresh."""
    body = {
        "operationName": "getAdditionalInformation",
        "query": QUERY,
        "variables": {
            "serialNumbers": [serial],
            "currentTimestamp": int(time()),
            "currentTimeZoneOffset": _offset_minutes(),
            "firstDayOfWeek": "MONDAY",
        },
    }
    try:
        async with client.session.post(
            graphql_url(client),
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=False,
        ) as response:
            client._guard()
            status = response.status
            if status == 401 and retry:
                return None
            if status in (401, 403):
                raise BaconError("history_rejected")
            if status == 429:
                raise client._rate_limited(response)
            if status in (404, 405):
                raise BaconError("history_unsupported")
            if status != 200:
                raise BaconError("service_unavailable")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise BaconError("invalid_history")
            return totals(json.loads(raw))
    except (TimeoutError, aiohttp.ClientError) as err:
        raise BaconError("connection") from err
    except (ValueError, UnicodeError) as err:
        raise BaconError("invalid_history") from err


@dataclass(slots=True)
class RacEnergy:
    """The totals of one device, refreshed while an energy entity wants them."""

    # Held weakly: the feed is stored under its account and must not keep it alive.
    _account: ref[Any]
    serial: str
    values: dict[str, float] = field(default_factory=dict)
    fetched_at: float | None = None
    stopped: str | None = None
    requests: int = 0
    failures: int = 0
    _subscribers: int = 0
    _task: asyncio.Task[None] | None = None
    _listeners: set[Callable[[], None]] = field(default_factory=set)
    _logged: set[str] = field(default_factory=set)

    @property
    def account(self) -> BaconAccess:
        account = self._account()
        if account is None:
            raise BaconError(_ENDED)
        return cast("BaconAccess", account)

    def current(self, key: str) -> float | None:
        if self._account() is None:
            return None
        if (
            self.fetched_at is None
            or monotonic() - self.fetched_at > STALE_AFTER
            or not self.account.available(self.serial)
        ):
            return None
        return self.values.get(key)

    def subscribe(
        self,
        listener: Callable[[], None],
        start: Callable[[Any], asyncio.Task[None]],
        log: Callable[[str], None],
    ) -> Callable[[], None]:
        """Count one entity; the first one starts the refresh task."""
        self._listeners.add(listener)
        self._subscribers += 1
        if self._task is None and self.stopped is None:
            self._task = start(self._run(log))

        def release() -> None:
            self._listeners.discard(listener)
            self._subscribers -= 1
            if self._subscribers <= 0 and self._task is not None:
                self._task.cancel()
                self._task = None

        return release

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    def _backoff(self, retry_after: float) -> float:
        """Wait 300 s, then twice as long per repeated failure, at most 1800 s."""
        self.failures += 1
        wait = float(min(MAX_RETRY, RETRY_AFTER_FAILURE * 2 ** (self.failures - 1)))
        return max(wait, retry_after)

    async def _run(self, log: Callable[[str], None]) -> None:
        while True:
            delay = INTERVAL
            try:
                device = self.account.devices.get(self.serial)
                if (
                    device is not None
                    and device.online is True
                    and self.account.available(self.serial)
                ):
                    self.requests += 1
                    self.values = await fetch_totals(self.account.client, self.serial)
                    self.fetched_at = monotonic()
                    self.failures = 0
                    self._notify()
                else:
                    # Nothing is asked while the device is offline or unknown.
                    delay = RETRY_AFTER_FAILURE
            except BaconError as err:
                if err.category == _ENDED:
                    self._task = None
                    return
                if err.category in STOPPING:
                    self.stopped = err.category
                    if err.category not in self._logged:
                        self._logged.add(err.category)
                        log(err.category)
                    self._notify()
                    return
                if err.category in _PAUSES:
                    # A pause the service named is no failure of the query.
                    delay = max(RETRY_AFTER_FAILURE, float(err.retry_after))
                else:
                    delay = self._backoff(float(err.retry_after))
            except Exception:
                # An unexpected answer of the optional query never ends the
                # task; it waits like after any other failure.
                delay = self._backoff(0.0)
            await asyncio.sleep(delay)


_FEEDS: WeakKeyDictionary[Any, dict[str, RacEnergy]] = WeakKeyDictionary()


def feed(account: BaconAccess, serial: str) -> RacEnergy:
    """The one shared feed of a device, so that it makes one query per interval."""
    feeds = _FEEDS.setdefault(account, {})
    if serial not in feeds:
        feeds[serial] = RacEnergy(ref(account), serial)
    return feeds[serial]
