"""CT-G05: the energy history of an RRC 2.0 room controller as statistics.

One ``EnergyHistory`` per coordinator reads the history with
``rrc_energy.run_cycle`` and adds each day as an external statistic of Home
Assistant's recorder: the energy values as running sums in kWh, the outdoor
temperature as a daily mean. Nothing becomes the state of an entity (P39),
and nothing is added to the energy dashboard by itself.

Load on the cloud: a cycle at most every ``CADENCE`` (3 h), the first one
``START_DELAY`` after the start, at most ``rrc_energy.MAX_REQUESTS``
requests each. A cycle waits while the coordinator has no cloud (sign-in,
pause, rate limit, outage); a rate limit answered to the history pauses
the whole cloud access like any other. Where the import stands is stored
privately per gateway, so a restart neither reads again nor waits less.

Everything is expected, untested on real hardware (W5-E1).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Final
from weakref import WeakKeyDictionary

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .i18n import localize
from .log_helpers import log_failure
from .pointt import ResourceError, UnexpectedHttpStatus
from .rrc_energy import (
    ENTRIES_PATH,
    FIRMWARE_PATH,
    GAS_UNIT_PATH,
    HISTORY_PATH,
    KEY_BY_SLUG,
    HistoryRefused,
    HistoryState,
    Rows,
    history_items,
    importable,
    run_cycle,
)
from .write_policies import context_of

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

CADENCE: Final = timedelta(hours=3)
START_DELAY: Final = timedelta(minutes=15)
# Waiting for the cloud, the recorder or the controller's recognition.
RETRY_DELAY: Final = timedelta(hours=1)
STORE_VERSION: Final = 1
_HISTORIES: WeakKeyDictionary[BoschBuderusDataUpdateCoordinator, EnergyHistory] = (
    WeakKeyDictionary()
)


def gateway_digest(gateway_id: str) -> str:
    """A stable token of a gateway that names no identifier."""
    return hashlib.sha256(gateway_id.encode()).hexdigest()[:16]


def energy_store_key(entry_id: str, gateway_id: str) -> str:
    """The private import state of one gateway, without its identifier."""
    return f"{DOMAIN}.rrc_energy.{entry_id}.{gateway_digest(gateway_id)}"


def statistic_id(gateway_id: str, slug: str) -> str:
    """``bosch_buderus_heating:rrc_<digest>_<slug>``, without the identifier."""
    return f"{DOMAIN}:rrc_{gateway_digest(gateway_id)[:12]}_{slug}"


async def async_remove_energy_store(
    hass: HomeAssistant, entry_id: str, gateway_id: str
) -> None:
    """Delete the import state of one gateway of a removed entry (STORE-5)."""
    await Store[dict[str, Any]](
        hass, STORE_VERSION, energy_store_key(entry_id, gateway_id), private=True
    ).async_remove()


def day_start(day: date, zone: tzinfo) -> datetime:
    """Local midnight of a day as the top of its UTC hour.

    The recorder takes rows only at the top of an hour; in a zone with a
    half-hour offset the row starts at the hour before local midnight.
    """
    start = datetime.combine(day, time(0), tzinfo=zone).astimezone(dt_util.UTC)
    return start.replace(minute=0, second=0, microsecond=0)


class _CloudSource:
    """The three reads of a cycle through the coordinator's client."""

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        self._client = coordinator.client
        self._gateway_id = coordinator.gateway.gateway_id

    async def entries(self) -> int | None:
        resource = await self._client.get_resource(self._gateway_id, ENTRIES_PATH)
        value = resource.value
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if value != value or value < 0 or int(value) != value:
            return None
        return int(value)

    async def page(self, query: Mapping[str, str | int]) -> list[object]:
        try:
            resource = await self._client.get_resource_query(
                self._gateway_id, HISTORY_PATH, query
            )
        except ResourceError as err:
            if err.status in (400, 404):
                raise HistoryRefused(f"HTTP {err.status}") from err
            raise
        except UnexpectedHttpStatus as err:
            if err.status == 400:
                raise HistoryRefused("HTTP 400") from err
            raise
        return history_items(resource.value, resource.values)

    async def gas_unit(self) -> str | None:
        resource = await self._client.get_resource(self._gateway_id, GAS_UNIT_PATH)
        return resource.value if isinstance(resource.value, str) else None


class EnergyHistory:
    """The history import of one RRC controller."""

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        *,
        now: Callable[[], datetime] = dt_util.utcnow,
    ) -> None:
        self._coordinator = coordinator
        self._hass: HomeAssistant = coordinator.hass
        entry = coordinator.config_entry
        entry_id = entry.entry_id if entry is not None else "none"
        gateway_id = coordinator.gateway.gateway_id
        self._gateway_id = gateway_id
        self._store: Store[dict[str, Any]] = Store(
            self._hass,
            STORE_VERSION,
            energy_store_key(entry_id, gateway_id),
            private=True,
        )
        self._now = now
        self.state = HistoryState()
        self._loaded = False
        self._unsub: CALLBACK_TYPE | None = None
        self._running = False
        self._stopped = False
        self.waiting: str | None = "not_started"

    # ------------------------------------------------------------ lifecycle
    async def async_start(self) -> None:
        try:
            self.state = HistoryState.from_dict(await self._store.async_load())
        except Exception as err:
            log_failure(
                _LOGGER,
                logging.WARNING,
                "The stored energy history import could not be read",
                error=err,
            )
            self.state = HistoryState()
        self._loaded = True
        if self._stopped:
            return
        earliest = self._now() + START_DELAY
        due = self.state.next_due
        self._schedule(due if due is not None and due > earliest else earliest)

    @callback
    def stop(self) -> None:
        self._stopped = True
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _schedule(self, when: datetime) -> None:
        if self._stopped:
            return
        if self._unsub is not None:
            self._unsub()
        self.state.next_due = when
        self._unsub = async_track_point_in_utc_time(self._hass, self._due, when)

    @callback
    def _due(self, _now: datetime) -> None:
        self._unsub = None
        if self._stopped or self._running:
            return
        entry = self._coordinator.config_entry
        coroutine = self.async_run()
        if entry is not None:
            entry.async_create_background_task(
                self._hass, coroutine, "bosch_buderus_heating_rrc_energy"
            )
        else:  # pragma: no cover - a coordinator always has its entry
            self._hass.async_create_background_task(
                coroutine, "bosch_buderus_heating_rrc_energy"
            )

    # ---------------------------------------------------------------- cycle
    def _blocked(self) -> str | None:
        """Why no cycle may read now, or None."""
        coordinator = self._coordinator
        if not context_of(coordinator).rrc_plant:
            return "not_rrc"  # S11: only on a recognised controller
        if "recorder" not in self._hass.config.components:
            return "recorder_unavailable"
        if coordinator.cloud_unavailable():
            return "cloud_unavailable"
        return None

    async def async_run(self) -> None:
        """One cycle now (also from tests), then the next one is planned."""
        if self._running or self._stopped or not self._loaded:
            return
        self._running = True
        now = self._now()
        try:
            if (blocked := self._blocked()) is not None:
                self.waiting = blocked
                self._schedule(now + RETRY_DELAY)
                return
            self.waiting = None
            firmware_resource = self._coordinator.resources.get(FIRMWARE_PATH)
            firmware = (
                firmware_resource.value
                if firmware_resource is not None
                and isinstance(firmware_resource.value, str)
                else None
            )
            zone = dt_util.get_default_time_zone()
            today = now.astimezone(zone).date()
            result = await run_cycle(
                self.state,
                _CloudSource(self._coordinator),
                today=today,
                now=now,
                firmware=firmware,
                write=lambda rows: self._write(rows, zone),
            )
            if result.retry_after is not None or result.reason == "rate_limited":
                # The whole cloud access pauses, as for every other answer.
                backoff = getattr(self._coordinator.client, "backoff", None)
                if backoff is not None:
                    backoff.activate(result.retry_after)
            delay = CADENCE
            if result.defer is not None:
                delay = max(delay, result.defer)
            if result.retry_after is not None:
                delay = max(delay, timedelta(seconds=result.retry_after))
            self._schedule(now + delay)
        except Exception as err:
            log_failure(
                _LOGGER,
                logging.WARNING,
                "The energy history import failed",
                error=err,
            )
            self.state.reason = "import_failed"
            self._schedule(now + CADENCE)
        finally:
            self._running = False
            await self._save()

    async def _save(self) -> None:
        try:
            await self._store.async_save(self.state.to_dict())
        except Exception as err:
            log_failure(
                _LOGGER,
                logging.WARNING,
                "The energy history import could not be stored",
                error=err,
            )

    def _write(self, rows: Rows, zone: tzinfo) -> None:
        """Add the rows of one answer to the recorder."""
        from homeassistant.components.recorder.models import (
            StatisticData,
            StatisticMeanType,
            StatisticMetaData,
        )
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
        )

        language = self._hass.config.language
        for slug, items in rows.items():
            key = KEY_BY_SLUG[slug]
            if not importable(key, self.state) or not items:
                continue
            temperature = key.kind == "temperature"
            metadata = StatisticMetaData(
                mean_type=(
                    StatisticMeanType.ARITHMETIC
                    if temperature
                    else StatisticMeanType.NONE
                ),
                has_sum=not temperature,
                name=localize(language, *key.names),
                source=DOMAIN,
                statistic_id=statistic_id(self._gateway_id, slug),
                unit_class="temperature" if temperature else "energy",
                unit_of_measurement="°C" if temperature else "kWh",
            )
            statistics = [
                StatisticData(
                    start=day_start(day, zone),
                    mean=value,
                    min=value,
                    max=value,
                )
                if temperature
                else StatisticData(start=day_start(day, zone), sum=value, state=value)
                for day, value in items
            ]
            async_add_external_statistics(self._hass, metadata, statistics)

    # ---------------------------------------------------------- diagnostics
    def diagnostics(self) -> dict[str, object]:
        """S12: where the import stands, without values, dates or names."""
        state = self.state
        today = dt_util.as_local(self._now()).date()
        return {
            "confirmation": "unconfirmed",
            "waiting": self.waiting,
            "mode": state.mode,
            "form_switched": state.switched,
            "unsupported": state.unsupported,
            "reason": state.reason,
            "cycles": state.cycles,
            "last_cycle_requests": state.last_requests,
            "imported_days": state.imported_days,
            "days_behind": (today - state.last_day).days if state.last_day else None,
            "statistics": sorted(
                slug for slug in state.active if importable(KEY_BY_SLUG[slug], state)
            ),
            "gas_unit_confirmed": state.gas_unit_confirmed,
            "invalid_records": state.invalid_records,
            "changes_outside_window": state.old_changes,
            "next_cycle_in_seconds": (
                max(0, int((state.next_due - self._now()).total_seconds()))
                if state.next_due is not None
                else None
            ),
        }


def track(
    on_unload: Callable[[Callable[[], None]], None],
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> EnergyHistory:
    """Start the import of one coordinator; a previous one stops."""
    if (previous := _HISTORIES.get(coordinator)) is not None:
        previous.stop()
    history = EnergyHistory(coordinator)
    _HISTORIES[coordinator] = history
    on_unload(history.stop)
    coordinator.hass.async_create_task(
        history.async_start(), "bosch_buderus_heating_rrc_energy_start"
    )
    return history


def history_of(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> EnergyHistory | None:
    return _HISTORIES.get(coordinator)


def diagnostics(coordinator: BoschBuderusDataUpdateCoordinator) -> dict[str, object]:
    """The import's report for ``rrc_features`` (an RRC controller only)."""
    history = _HISTORIES.get(coordinator)
    if history is None:
        return {"confirmation": "unconfirmed", "waiting": "not_started"}
    return history.diagnostics()
