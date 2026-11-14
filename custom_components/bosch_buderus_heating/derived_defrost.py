"""Defrosts of the heat pump, worked out from its readings.

One monitor per gateway follows the defrost sources of ``derived_activity``
on every update: the hybrid manager's defrost signal, the local refrigerant
circuit status, the cloud compressor status and the heat sources' defrost
flags, in this order. Only a plant with evidence that it defrosts at all gets
the entities (``defrost_capable``): a brine-to-water heat pump never does.

A defrost counts from the first reading that shows it to the first one that
no longer does, so its duration is good to about one polling interval. A
defrost already running when Home Assistant starts, or one whose source was
unreadable for longer than ``GAP``, is not counted: its start is unknown.

While it runs the monitor follows the 3-way valve VW1 (the stages: first the
heating side, then hot water) and the heat counters. The heat taken back
from the water shows as a dip of the produced-heat counters; their readings
change only every few minutes and in 0.01 kWh steps, so the monitor waits up
to ``SETTLE`` after the end, until every counter has risen again, before it
reports the defrost. The electricity is the change of the compressor and
heater counters between start and end, with the same resolution. Defrosts
are counted only while Home Assistant runs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final
from weakref import WeakKeyDictionary

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.event import EventEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfTime
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .derived import power_kilowatts
from .derived_activity import (
    COMPRESSOR_STATUS,
    DHW_VALVE,
    DHW_VALVE_RELAY,
    defrost_capable,
    defrost_paths,
    defrost_state,
)
from .derived_inputs import Input, Trace, energy_part, first, flag, number, read
from .derived_store import DerivedStore, derived_store, stored_number
from .device import device_info_for_resource, grouped_entity_name
from .heat_source_kind import defrost_evidence, note_defrost_evidence
from .i18n import localize

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .platform_scope import DiscoveryLifecycle

_LOGGER = logging.getLogger(__name__)
EVENT_TYPE: Final = "defrost_finished"
# How long after the end the heat counters may take to show the dip.
SETTLE: Final = timedelta(minutes=10)
# The longest gap of the defrost source that a defrost may span.
GAP: Final = timedelta(minutes=15)
HEAT_COUNTERS: Final = (
    "/heatSources/emon/chConsumption",
    "/heatSources/emon/dhwConsumption",
    "/heatSources/emon/poolConsumption",
)
TOTAL_COUNTER: Final = "/heatSources/emon/totalConsumption"
MODE_COUNTERS: Final = (*HEAT_COUNTERS, "/heatSources/emon/coolingConsumption")
PRODUCED: Final = "outputProduced"
ELECTRIC_PARTS: Final = ("compressor", "eheater")
OUTDOOR: Final = "/system/sensors/temperatures/outdoor_t1"
# The electrical power during a defrost: the compressor (first path with a
# reading) plus the electric heater, integrated over the readings.
COMPRESSOR_POWER: Final = (
    "/heatSources/compressor/powerElecActual",
    "/heatSources/hs1/refrigerant/compressorElecPowerActual",
)
HEATER_POWER: Final = "/heatSources/eHeater/powerElecActual"
# The longest gap between two power readings (and from the start or to the
# end of the defrost) that is still bridged; the local access reads them
# about every minute.
MAX_POWER_GAP: Final = timedelta(seconds=150)
AIR_INLET: Final = "/heatSources/sensors/evaporatorTemp_tl2"
STAGE_HEATING: Final = "heating"
STAGE_HOT_WATER: Final = "dhw"
# The entity's device and the reading it belongs to.
ANCHOR: Final = COMPRESSOR_STATUS
_STORE_SUFFIX: Final = "calculated_defrost"
_MONITORS: WeakKeyDictionary[object, DefrostMonitor] = WeakKeyDictionary()

ACTIVE_KEY: Final = "calculated_defrost_active"
EVENT_KEY: Final = "calculated_defrost"
LAST_KEY: Final = "calculated_last_defrost"
COUNT_KEY: Final = "calculated_defrost_count"
DURATION_KEY: Final = "calculated_defrost_duration"
HEAT_KEY: Final = "calculated_defrost_heat_withdrawn"
ELECTRICITY_KEY: Final = "calculated_defrost_electricity"
_TOTAL_FIELDS: Final = {
    COUNT_KEY: "count",
    DURATION_KEY: "duration_min",
    HEAT_KEY: "heat_withdrawn_kwh",
    ELECTRICITY_KEY: "electricity_kwh",
}


def unique_id(gateway_id: str, key: str) -> str:
    return f"{gateway_id}:heatSources:{key}"


def input_paths(coordinator: BoschBuderusDataUpdateCoordinator) -> tuple[str, ...]:
    """Every reading the defrost entities combine (entity use)."""
    return tuple(
        dict.fromkeys(
            (
                *defrost_paths(coordinator),
                DHW_VALVE,
                DHW_VALVE_RELAY,
                TOTAL_COUNTER,
                *MODE_COUNTERS,
                OUTDOOR,
                AIR_INLET,
                *COMPRESSOR_POWER,
                HEATER_POWER,
            )
        )
    )


def _stage_of(reading: Input | None) -> str | None:
    on = flag(reading)
    return None if on is None else STAGE_HOT_WATER if on else STAGE_HEATING


@dataclass(slots=True)
class _Defrost:
    """One defrost while it runs and until its counters have settled."""

    started_at: datetime
    detected_by: str
    last_seen: datetime
    trace: Trace = field(default_factory=Trace)
    stages: list[tuple[str, datetime]] = field(default_factory=list)
    heat_start: dict[str, float] = field(default_factory=dict)
    heat_min: dict[str, float] = field(default_factory=dict)
    risen: set[str] = field(default_factory=set)
    electricity_start: dict[str, float] = field(default_factory=dict)
    electricity_end: dict[str, float] | None = None
    outdoor: float | None = None
    air_inlet: float | None = None
    ended_at: datetime | None = None
    # Power readings (time, kW) while the defrost runs, each once.
    power: list[tuple[datetime, float]] = field(default_factory=list)


class DefrostMonitor:
    """The defrosts of one gateway, their totals and the last one."""

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, store: DerivedStore
    ) -> None:
        self._coordinator = coordinator
        self._store = store
        self._key = f"{coordinator.gateway.gateway_id}:{_STORE_SUFFIX}"
        self._listeners: list[Callable[[], None]] = []
        self._event_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._unsubscribe: CALLBACK_TYPE | None = None
        self.active: bool | None = None
        self.source: Input | None = None
        self.totals: dict[str, float] = dict.fromkeys(_TOTAL_FIELDS.values(), 0.0)
        self.last: dict[str, Any] | None = None
        self._current: _Defrost | None = None
        self._last_known: bool | None = None
        self._last_known_at: datetime | None = None
        self._evidence_saved = False
        self._loaded = False
        # Defrosts finished before the saved totals were read.
        self._pending: list[dict[str, Any]] = []

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def running(self) -> bool:
        return self._current is not None and self._current.ended_at is None

    def subscribe(self, entry: DiscoveryLifecycle) -> None:
        """Follow the coordinator's updates for the lifetime of ``entry``."""
        if self._unsubscribe is not None:
            return
        remove = self._coordinator.async_add_listener(self.update)

        def unsubscribe() -> None:
            remove()
            self._unsubscribe = None

        self._unsubscribe = unsubscribe
        entry.async_on_unload(unsubscribe)

    async def async_load(self) -> None:
        if self._loaded:
            return
        await self._store.async_load()
        if self._loaded:
            return
        row = self._store.get(self._key)
        for name in self.totals:
            value = stored_number(row, name)
            if value is not None:
                self.totals[name] = value
        last = row.get("last") if row is not None else None
        if isinstance(last, dict) and isinstance(last.get("started_at"), str):
            self.last = last
        if row is not None and row.get("evidence") is True:
            note_defrost_evidence(self._coordinator)
            self._evidence_saved = True
        self._loaded = True
        pending, self._pending = self._pending, []
        for attributes in pending:
            self._count(attributes)
        if pending:
            self._save(now=True)
        self._notify()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def add_event_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        self._event_listeners.append(listener)
        return lambda: self._event_listeners.remove(listener)

    @callback
    def update(self, now: datetime | None = None) -> None:
        """Take the current readings; called on every coordinator update."""
        now = now or dt_util.utcnow()
        reading = defrost_state(self._coordinator)
        active = reading.active if reading is not None else None
        observed = reading.source.observed_at or now if reading is not None else now
        changed = active != self.active or (
            reading is not None
            and (self.source is None or reading.source.path != self.source.path)
        )
        self.active = active
        self.source = reading.source if reading is not None else None
        current = self._current
        if current is not None and current.ended_at is not None:
            self._follow_heat(current)
            if (
                active is True
                or now - current.ended_at >= SETTLE
                or current.risen >= set(current.heat_start)
            ):
                self._finish(current)
                current = None
        if current is not None and current.ended_at is None:
            if active is True:
                current.last_seen = observed
                self._follow_stage(current, observed)
                self._follow_heat(current)
                self._follow_power(current)
            elif active is False:
                current.ended_at = max(observed, current.started_at)
                current.electricity_end = self._electricity(current.trace)
                self._follow_power(current)
                self._follow_heat(current)
            elif now - current.last_seen > GAP:
                _LOGGER.debug("Defrost source unreadable too long; defrost dropped")
                self._current = None
        if (
            self._current is None
            and reading is not None
            and active is True
            and self._last_known is False
            and self._last_known_at is not None
            and observed - self._last_known_at <= GAP
        ):
            self._start(reading.source, observed)
        if active is not None:
            self._last_known, self._last_known_at = active, observed
        if (
            self._loaded
            and not self._evidence_saved
            and defrost_evidence(self._coordinator)
        ):
            self._evidence_saved = True
            self._save()
        if changed:
            self._notify()

    def _start(self, source: Input, observed: datetime) -> None:
        current = _Defrost(observed, source.path, observed)
        current.trace.note(source)
        for path in HEAT_COUNTERS:
            value = energy_part(
                current.trace.note(read(self._coordinator, path)), PRODUCED
            )
            if value is not None:
                current.heat_start[path] = current.heat_min[path] = value
        if not current.heat_start:
            value = energy_part(
                current.trace.note(read(self._coordinator, TOTAL_COUNTER)), PRODUCED
            )
            if value is not None:
                current.heat_start[TOTAL_COUNTER] = value
                current.heat_min[TOTAL_COUNTER] = value
        current.electricity_start = self._electricity(current.trace)
        current.outdoor = number(current.trace.note(read(self._coordinator, OUTDOOR)))
        current.air_inlet = number(
            current.trace.note(read(self._coordinator, AIR_INLET))
        )
        self._current = current
        self._follow_stage(current, observed)
        self._follow_power(current)

    def _follow_power(self, current: _Defrost) -> None:
        """Take the current power reading once; the heater counts when read."""
        found = first(
            self._coordinator,
            COMPRESSOR_POWER,
            lambda item: power_kilowatts(item.value, item.unit) if item else None,
        )
        if found is None:
            return
        reading, compressor = found
        when = reading.observed_at
        if when is None:
            return
        heater = read(self._coordinator, HEATER_POWER)
        extra = power_kilowatts(heater.value, heater.unit) if heater else None
        current.trace.note(reading)
        if extra is not None:
            current.trace.note(heater)
        if current.power and current.power[-1][0] >= when:
            return  # the same reading again
        current.power.append((when, compressor + (extra or 0.0)))

    def _follow_stage(self, current: _Defrost, observed: datetime) -> None:
        found = first(
            self._coordinator, (DHW_VALVE, DHW_VALVE_RELAY), _stage_of, current.trace
        )
        if found is None:
            return
        reading, stage = found
        if current.stages and current.stages[-1][0] == stage:
            return
        # The valve's own reading time; never before the defrost started.
        since = reading.observed_at or observed
        current.stages.append((stage, max(since, current.started_at)))

    def _follow_heat(self, current: _Defrost) -> None:
        for path in current.heat_start:
            reading = read(self._coordinator, path)
            value = energy_part(reading, PRODUCED)
            if value is None or reading is None:
                continue
            lowest = current.heat_min[path]
            if value < lowest:
                current.heat_min[path] = value
            elif (
                current.ended_at is not None
                and value > lowest + 1e-9
                and reading.observed_at is not None
                and reading.observed_at > current.ended_at
            ):
                current.risen.add(path)

    def _electricity(self, trace: Trace) -> dict[str, float]:
        values: dict[str, float] = {}
        for paths in ((TOTAL_COUNTER,), MODE_COUNTERS):
            for path in paths:
                reading = read(self._coordinator, path)
                for part in ELECTRIC_PARTS:
                    value = energy_part(reading, part)
                    if value is not None:
                        trace.note(reading)
                        values[f"{path}|{part}"] = value
            if values:
                return values
        return values

    def _finish(self, current: _Defrost) -> None:
        self._current = None
        ended = current.ended_at or current.started_at
        duration = (ended - current.started_at).total_seconds() / 60
        stage_minutes: dict[str, float] = {}
        for index, (stage, since) in enumerate(current.stages):
            until = (
                current.stages[index + 1][1]
                if index + 1 < len(current.stages)
                else ended
            )
            minutes = max(0.0, (until - since).total_seconds() / 60)
            stage_minutes[stage] = round(stage_minutes.get(stage, 0.0) + minutes, 1)
        heat = (
            round(
                sum(
                    max(0.0, current.heat_start[path] - current.heat_min[path])
                    for path in current.heat_start
                ),
                2,
            )
            if current.heat_start
            else None
        )
        end = current.electricity_end or {}
        common = set(current.electricity_start) & set(end)
        electricity = (
            round(
                sum(
                    max(0.0, end[key] - current.electricity_start[key])
                    for key in common
                ),
                2,
            )
            if common
            else None
        )
        attributes: dict[str, Any] = {
            "started_at": current.started_at.isoformat(),
            "ended_at": ended.isoformat(),
            "duration_min": round(duration, 1),
            "stages": [stage for stage, _ in current.stages],
            "stage_minutes": stage_minutes,
            "heat_withdrawn_kwh": heat,
            "electricity_kwh": electricity,
            "electricity_kwh_from_power": _power_energy(
                current.power, current.started_at, ended
            ),
            "outdoor_temp_start": current.outdoor,
            "air_inlet_temp_start": current.air_inlet,
            "detected_by": current.detected_by,
            "derived_from": current.trace.attribute(),
        }
        self.last = attributes
        if self._loaded:
            self._count(attributes)
            self._save(now=True)
        else:
            self._pending.append(attributes)
        for listener in tuple(self._event_listeners):
            listener(attributes)
        self._notify()

    def _count(self, attributes: dict[str, Any]) -> None:
        self.totals["count"] += 1
        self.totals["duration_min"] += float(attributes["duration_min"])
        for name in ("heat_withdrawn_kwh", "electricity_kwh"):
            value = attributes.get(name)
            if isinstance(value, int | float):
                self.totals[name] += float(value)

    def _save(self, *, now: bool = False) -> None:
        row: dict[str, Any] = {
            **{name: round(value, 4) for name, value in self.totals.items()},
            "evidence": defrost_evidence(self._coordinator),
        }
        if self.last is not None:
            row["last"] = self.last
        self._store.set(self._key, row, now=now)

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    def diagnostics(self) -> dict[str, object]:
        """No values beyond the counts and states shown by the entities."""
        return {
            "capable": defrost_capable(self._coordinator),
            "active": self.active,
            "source": self.source.path if self.source is not None else None,
            "source_origin": self.source.origin if self.source is not None else None,
            "running": self.running,
            "settling": self._current is not None and not self.running,
            "count": int(self.totals["count"]),
            "last_started_at": (self.last or {}).get("started_at"),
        }


def _power_energy(
    points: list[tuple[datetime, float]], start: datetime, end: datetime
) -> float | None:
    """kWh between start and end, linear between the power readings
    (trapezoid), the first and last reading held up to the edges.

    ``None`` without readings or when two readings, or a reading and the
    start or end, are more than ``MAX_POWER_GAP`` apart.
    """
    if not points or end <= start:
        return None
    times = [moment for moment, _ in points]
    if times[0] - start > MAX_POWER_GAP or end - times[-1] > MAX_POWER_GAP:
        return None
    if any(later - earlier > MAX_POWER_GAP for earlier, later in pairwise(times)):
        return None

    def at(moment: datetime) -> float:
        if moment <= points[0][0]:
            return points[0][1]
        for (t0, p0), (t1, p1) in pairwise(points):
            if t0 <= moment <= t1:
                share = (moment - t0) / (t1 - t0) if t1 > t0 else 0.0
                return p0 + (p1 - p0) * share
        return points[-1][1]

    inner = [(moment, power) for moment, power in points if start < moment < end]
    curve = [(start, at(start)), *inner, (end, at(end))]
    energy = sum(
        (p0 + p1) / 2 * (t1 - t0).total_seconds()
        for (t0, p0), (t1, p1) in pairwise(curve)
    )
    return round(energy / 3600, 3)


def defrost_monitor(
    coordinator: BoschBuderusDataUpdateCoordinator, entry: DiscoveryLifecycle
) -> DefrostMonitor:
    """The gateway's monitor, following its coordinator for ``entry``."""
    monitor = _MONITORS.get(coordinator)
    if monitor is None:
        monitor = _MONITORS[coordinator] = DefrostMonitor(
            coordinator, derived_store(coordinator.hass)
        )
        coordinator.hass.async_create_task(monitor.async_load(), eager_start=True)
    monitor.subscribe(entry)
    return monitor


def calculated_diagnostics(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> dict[str, object]:
    """The defrost monitor of a gateway; never fails."""
    try:
        monitor = existing_monitor(coordinator)
        return {
            "defrost_capable": defrost_capable(coordinator),
            "defrost": monitor.diagnostics() if monitor is not None else None,
        }
    except Exception:  # the report must stay available
        return {"error": "unavailable"}


def existing_monitor(coordinator: object) -> DefrostMonitor | None:
    try:
        return _MONITORS.get(coordinator)
    except TypeError:
        return None


def _registered(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    """A defrost entity of this entry exists from an earlier start."""
    registry = er.async_get(coordinator.hass)
    config_entry = getattr(coordinator, "config_entry", None)
    gateway_id = coordinator.gateway.gateway_id
    for domain, key in (
        ("binary_sensor", ACTIVE_KEY),
        ("event", EVENT_KEY),
        ("sensor", LAST_KEY),
    ):
        entity_id = registry.async_get_entity_id(
            domain, DOMAIN, unique_id(gateway_id, key)
        )
        entry = registry.async_get(entity_id) if entity_id is not None else None
        if (
            entry is not None
            and config_entry is not None
            and entry.config_entry_id == config_entry.entry_id
        ):
            return True
    return False


def track_defrost_entities(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    create: Callable[[BoschBuderusDataUpdateCoordinator, DefrostMonitor], list[Entity]],
) -> None:
    """Add a platform's defrost entities once the plant shows it defrosts."""
    added = False

    @callback
    def discover() -> None:
        nonlocal added
        if added:
            return
        if not defrost_capable(coordinator) and not _registered(coordinator):
            return
        added = True
        monitor = defrost_monitor(coordinator, entry)
        monitor.update()
        async_add_entities(create(coordinator, monitor))

    discover()
    if not added:
        entry.async_on_unload(coordinator.async_add_listener(discover))


class _DefrostEntity(Entity):
    """Shown from the monitor; written when the monitor changes."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    resource_path = ANCHOR

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        monitor: DefrostMonitor,
        key: str,
        name: str,
    ) -> None:
        self.coordinator = coordinator
        self._monitor = monitor
        self._attr_translation_key = key
        self._attr_unique_id = unique_id(coordinator.gateway.gateway_id, key)
        self._attr_name = grouped_entity_name(coordinator, ANCHOR, name)
        self.extra_resource_paths = input_paths(coordinator)

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, ANCHOR)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._monitor.async_load()
        self.async_on_remove(self._monitor.add_listener(self._changed))

    @callback
    def _changed(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()


class DefrostActiveBinarySensor(_DefrostEntity, BinarySensorEntity):
    """Whether the heat pump defrosts right now."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, monitor: DefrostMonitor
    ) -> None:
        super().__init__(
            coordinator,
            monitor,
            ACTIVE_KEY,
            localize(
                coordinator.hass.config.language,
                "Abtauung aktiv (berechnet)",
                "Defrost active (calculated)",
            ),
        )

    @property
    def available(self) -> bool:
        return self._monitor.active is not None

    @property
    def is_on(self) -> bool | None:
        return self._monitor.active

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        source = self._monitor.source
        if source is None:
            return None
        return {
            "detected_by": source.path,
            "derived_from": {source.path: source.origin},
        }


class DefrostEvent(_DefrostEntity, EventEntity):
    """One event per finished defrost, with its duration, stages and energy."""

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, monitor: DefrostMonitor
    ) -> None:
        self._attr_event_types = [EVENT_TYPE]
        super().__init__(
            coordinator,
            monitor,
            EVENT_KEY,
            localize(
                coordinator.hass.config.language,
                "Abtauung (berechnet)",
                "Defrost (calculated)",
            ),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._monitor.add_event_listener(self._finished))

    @callback
    def _finished(self, attributes: dict[str, Any]) -> None:
        self._trigger_event(EVENT_TYPE, attributes)
        self.async_write_ha_state()


class LastDefrostSensor(_DefrostEntity, SensorEntity):
    """When the last counted defrost started, with its details."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _unrecorded_attributes = frozenset({"derived_from", "stage_minutes", "stages"})

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, monitor: DefrostMonitor
    ) -> None:
        super().__init__(
            coordinator,
            monitor,
            LAST_KEY,
            localize(
                coordinator.hass.config.language,
                "Letzte Abtauung (berechnet)",
                "Last defrost (calculated)",
            ),
        )

    @property
    def available(self) -> bool:
        return self._monitor.loaded

    @property
    def native_value(self) -> datetime | None:
        last = self._monitor.last
        started = last.get("started_at") if last is not None else None
        parsed = dt_util.parse_datetime(started) if isinstance(started, str) else None
        return parsed if parsed is not None and parsed.tzinfo is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        last = self._monitor.last
        if last is None:
            return None
        return {key: value for key, value in last.items() if key != "started_at"}


COUNTER_KEYS: Final = (COUNT_KEY, DURATION_KEY, HEAT_KEY, ELECTRICITY_KEY)


def _counter_name(language: str, key: str) -> str:
    if key == COUNT_KEY:
        return localize(
            language, "Anzahl Abtauungen (berechnet)", "Number of defrosts (calculated)"
        )
    if key == DURATION_KEY:
        return localize(
            language, "Abtaudauer gesamt (berechnet)", "Total defrost time (calculated)"
        )
    if key == HEAT_KEY:
        return localize(
            language,
            "Wärmeentnahme beim Abtauen (berechnet)",
            "Heat withdrawn by defrosting (calculated)",
        )
    return localize(
        language,
        "Strom beim Abtauen (berechnet)",
        "Electricity during defrosting (calculated)",
    )


class DefrostCounterSensor(_DefrostEntity, SensorEntity):
    """A total over every counted defrost; disabled by default."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        monitor: DefrostMonitor,
        key: str,
    ) -> None:
        super().__init__(
            coordinator,
            monitor,
            key,
            _counter_name(coordinator.hass.config.language, key),
        )
        self._field = _TOTAL_FIELDS[key]
        if key == DURATION_KEY:
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_native_unit_of_measurement = UnitOfTime.MINUTES
            self._attr_suggested_display_precision = 0
        elif key in (HEAT_KEY, ELECTRICITY_KEY):
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_suggested_display_precision = 2
        # The count takes its icon from icons.json.

    @property
    def available(self) -> bool:
        return self._monitor.loaded

    @property
    def native_value(self) -> float | int:
        value = self._monitor.totals[self._field]
        return int(value) if self._field == "count" else round(value, 2)


def binary_sensors(
    coordinator: BoschBuderusDataUpdateCoordinator, monitor: DefrostMonitor
) -> list[Entity]:
    return [DefrostActiveBinarySensor(coordinator, monitor)]


def events(
    coordinator: BoschBuderusDataUpdateCoordinator, monitor: DefrostMonitor
) -> list[Entity]:
    return [DefrostEvent(coordinator, monitor)]


def sensors(
    coordinator: BoschBuderusDataUpdateCoordinator, monitor: DefrostMonitor
) -> list[Entity]:
    return [
        LastDefrostSensor(coordinator, monitor),
        *(DefrostCounterSensor(coordinator, monitor, key) for key in COUNTER_KEYS),
    ]
