"""Calculated performance sensors: current COP, performance factors, spreads.

Each one is named "(calculated)" and lists its inputs in ``derived_from``.
None of them sends a request of its own: they combine readings the
integration reads anyway, the plant's recordings and the counters'
long-term statistics (``performance``, ``performance_history``).

Enabled by default: the current COP, the performance factor of today, of
this year and since commissioning (in total) and the primary circuit
spread. The other periods, the per-mode factors and the brine spread are
disabled by default; the history behind them is kept all the same, so an
enabled entity shows its value at once.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .burner_share import BURNER_SHARE, burner_share
from .cop_plausibility import LowCopWatch
from .cop_start import StartPhase
from .device import device_info_for_resource, grouped_entity_name
from .held_readings import cloud_usable
from .i18n import localize
from .performance import (
    COLLECTIONS,
    MODES,
    LiveResult,
    brine_input_paths,
    brine_roots,
    brine_spread,
    cop_input_paths,
    cop_possible,
    counter_possible,
    live_cop,
    primary_spread,
    spread_input_paths,
    spread_possible,
    unknown_reason,
)
from .performance_history import (
    COUNTERS,
    PerformanceHistory,
    Period,
    PeriodResult,
    engine_for,
    source_text,
)
from .performance_note import little_heat_attributes
from .writes import context_of

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .platform_scope import DiscoveryLifecycle

HEAT_SOURCES: Final = "/heatSources"
ENABLED_FACTORS: Final = frozenset(
    {
        (Period.TODAY, "total"),
        (Period.YESTERDAY, "total"),
        (Period.CURRENT_MONTH, "total"),
        (Period.PREVIOUS_MONTH, "total"),
        (Period.CURRENT_YEAR, "total"),
        (Period.PREVIOUS_YEAR, "total"),
        (Period.SINCE_COMMISSIONING, "total"),
    }
)
HISTORY_LOADING: Final = "history_loading"
_RECORDING: Final = "/recordings/heatSources/emon/{mode}/{kind}"


def _key(path: str) -> str:
    return path.strip("/").replace("/", ":")


class _LiveSensor(CoordinatorEntity["BoschBuderusDataUpdateCoordinator"], SensorEntity):
    """A live calculated value; ``_derive`` works it out."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        path: str,
        key: str,
        name: str,
        inputs: tuple[str, ...],
    ) -> None:
        super().__init__(coordinator)
        self.resource_path = path
        self.extra_resource_paths = inputs
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:{_key(path)}:{key}"
        self._attr_name = grouped_entity_name(coordinator, path, name)
        self._result = LiveResult(None)

    @property
    def available(self) -> bool:
        # H-B3-2: over a short cloud blip the value stays shown.
        return cloud_usable(self.coordinator)

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.resource_path)

    @property
    def native_value(self) -> float | None:
        return self._result.value

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return self._result.attributes

    def _derive(self) -> LiveResult:
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._result = self._derive()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._result = self._derive()
        super()._handle_coordinator_update()

    async def async_update(self) -> None:
        """Calculated from readings the integration reads anyway."""


class CurrentCopSensor(_LiveSensor):
    """Heat output over electrical power while the compressor runs."""

    _attr_suggested_display_precision = 2
    _unrecorded_attributes = frozenset(
        {
            "heat_output_kw",
            "compressor_power_kw",
            "heater_power_kw",
            "electrical_power_kw",
            "side",
            "brine_pump_included",
            "derived_from",
        }
    )

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        self._phase = StartPhase()
        self._low = LowCopWatch()
        super().__init__(
            coordinator,
            HEAT_SOURCES,
            "calculated_cop",
            localize(
                coordinator.hass.config.language,
                "Aktuelle Leistungszahl (berechnet)",
                "Current COP (calculated)",
            ),
            cop_input_paths(coordinator),
        )

    def _derive(self) -> LiveResult:
        return live_cop(self.coordinator, self._phase, self._low)


class PrimarySpreadSensor(_LiveSensor):
    """Supply (TC1) minus return (TC0) of the heat pump."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE_DELTA
    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
    _attr_suggested_display_precision = 1
    _unrecorded_attributes = frozenset(
        {
            "supply_temperature",
            "return_temperature",
            "primary_flow",
            "circuit_flow",
            "derived_from",
        }
    )

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(
            coordinator,
            HEAT_SOURCES,
            "calculated_primary_spread",
            localize(
                coordinator.hass.config.language,
                "Spreizung Primärkreis (berechnet)",
                "Primary circuit spread (calculated)",
            ),
            spread_input_paths(),
        )

    def _derive(self) -> LiveResult:
        return primary_spread(self.coordinator)


class BrineSpreadSensor(_LiveSensor):
    """Brine inlet minus brine outlet."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE_DELTA
    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
    _attr_suggested_display_precision = 1
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = frozenset(
        {"inlet_temperature", "outlet_temperature", "derived_from"}
    )

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, root: str
    ) -> None:
        self._root = root
        super().__init__(
            coordinator,
            root + "/brineCircuit",
            "calculated_brine_spread",
            localize(
                coordinator.hass.config.language,
                "Spreizung Sole (berechnet)",
                "Brine spread (calculated)",
            ),
            brine_input_paths(coordinator, root),
        )

    def _derive(self) -> LiveResult:
        return brine_spread(self.coordinator, self._root)


# ------------------------------------------------------------- factors
def _mode_name(language: str, mode: str) -> str:
    if mode == "ch":
        return localize(language, "Heizung", "Heating")
    if mode == "dhw":
        return localize(language, "Warmwasser", "Hot water")
    return localize(language, "Gesamt", "Total")


def _template(language: str, period: Period) -> str:
    if period is Period.TODAY:
        return localize(
            language, "{name} heute (berechnet)", "{name} today (calculated)"
        )
    if period is Period.YESTERDAY:
        return localize(
            language, "{name} gestern (berechnet)", "{name} yesterday (calculated)"
        )
    if period is Period.CURRENT_MONTH:
        return localize(
            language,
            "{name} aktueller Monat (berechnet)",
            "{name} this month (calculated)",
        )
    if period is Period.PREVIOUS_MONTH:
        return localize(
            language,
            "{name} Vormonat (berechnet)",
            "{name} previous month (calculated)",
        )
    if period is Period.CURRENT_YEAR:
        return localize(
            language,
            "{name} aktuelles Jahr (berechnet)",
            "{name} this year (calculated)",
        )
    if period is Period.PREVIOUS_YEAR:
        return localize(
            language, "{name} Vorjahr (berechnet)", "{name} previous year (calculated)"
        )
    return localize(
        language,
        "{name} seit Inbetriebnahme (berechnet)",
        "{name} since commissioning (calculated)",
    )


def factor_name(language: str, period: Period, mode: str) -> str:
    kind = localize(language, "Arbeitszahl", "Performance factor")
    return _template(language, period).format(
        name=f"{_mode_name(language, mode)} \N{EN DASH} {kind}"
    )


class PerformanceFactorSensor(
    CoordinatorEntity["BoschBuderusDataUpdateCoordinator"], SensorEntity
):
    """Heat over electricity of one period and mode."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_translation_key = "calculated_performance_factor"
    _unrecorded_attributes = frozenset(
        {
            "period_start",
            "period_end",
            "heat_kwh",
            "electricity_kwh",
            "heat_source",
            "electricity_source",
            "days",
            "missing_days",
            "first_day",
            "derived_from",
            "reason",
            "note",
            "little_heat_below_kwh",
        }
    )

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        engine: PerformanceHistory,
        period: Period,
        mode: str,
    ) -> None:
        super().__init__(coordinator)
        self._engine = engine
        self._period = period
        self._mode = mode
        self.resource_path = COLLECTIONS[mode]
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:{_key(self.resource_path)}"
            f":calculated_performance_factor_{period.value}"
        )
        self._attr_name = grouped_entity_name(
            coordinator,
            self.resource_path,
            factor_name(coordinator.hass.config.language, period, mode),
        )
        self._attr_entity_registry_enabled_default = (period, mode) in ENABLED_FACTORS
        self._result: PeriodResult | None = None

    @property
    def available(self) -> bool:
        # H-B3-2: over a short cloud blip the value stays shown.
        return cloud_usable(self.coordinator)

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.resource_path)

    @property
    def native_value(self) -> float | None:
        if self._burner_share():
            return None
        return self._result.ratio if self._result is not None else None

    def _burner_share(self) -> bool:
        """Heat of a burner is not covered by the heat pump's electricity."""
        return burner_share(self.coordinator, self.resource_path)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        result = self._result
        if result is None:
            if self._burner_share():
                return {"reason": BURNER_SHARE}
            if self._period not in (Period.TODAY, Period.SINCE_COMMISSIONING) and (
                not self._engine.history_read
            ):
                return {"reason": HISTORY_LOADING}
            return None
        attributes: dict[str, object] = {}
        if result.start is not None and result.end is not None:
            attributes["period_start"] = result.start.isoformat()
            attributes["period_end"] = result.end.fromordinal(
                result.end.toordinal() - 1
            ).isoformat()
        attributes.update(
            {
                "heat_kwh": round(result.heat, 3),
                "electricity_kwh": round(result.electricity, 3),
                "heat_source": source_text(result.heat_sources),
                "electricity_source": source_text(result.electricity_sources),
            }
        )
        if result.start is not None:
            attributes["days"] = result.days
            attributes["missing_days"] = result.missing_days
        if result.first_day is not None:
            attributes["first_day"] = result.first_day.isoformat()
        attributes["derived_from"] = self._derived_from(result)
        if self._burner_share():
            attributes["reason"] = BURNER_SHARE
        elif result.ratio is None:
            attributes["reason"] = unknown_reason(result.electricity)
        else:
            attributes.update(little_heat_attributes(self._period, result.heat))
        return attributes

    def _derived_from(self, result: PeriodResult) -> dict[str, str]:
        found: dict[str, str] = {}
        sources = result.heat_sources | result.electricity_sources
        if COUNTERS in sources:
            live = self._engine.live_origin(self._mode)
            found[self.resource_path] = live or COUNTERS
        if result.heat_sources - {COUNTERS}:
            found[_RECORDING.format(mode=self._mode, kind="outputProduced")] = (
                "recording"
            )
        if result.electricity_sources - {COUNTERS}:
            for kind in ("compressor", "eheater"):
                found[_RECORDING.format(mode=self._mode, kind=kind)] = "recording"
        return dict(sorted(found.items()))

    def _refresh(self) -> None:
        self._sync_once()
        self._result = self._engine.value(self._period, self._mode)

    def _sync_once(self) -> None:
        """Take the counters' readings once per update pass: all factor
        sensors of a gateway share one engine, and the readings cannot change
        while a synchronous pass runs."""
        from .local_selection import LocalSelection

        selection = getattr(self.coordinator, "local_selection", None)
        if isinstance(selection, LocalSelection):
            selection._cached(("performance_sync", id(self._engine)), self._engine.sync)
        else:
            self._engine.sync()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._engine.add_listener(self._history_updated))
        self._refresh()

    @callback
    def _history_updated(self) -> None:
        self._refresh()
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh()
        super()._handle_coordinator_update()

    async def async_update(self) -> None:
        """Calculated from readings the integration reads anyway."""


# ------------------------------------------------------------- tracking
type _Factory = Callable[[], Entity]


def _candidates(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> list[tuple[str, _Factory]]:
    found: list[tuple[str, _Factory]] = []
    if not (getattr(coordinator, "data", None) or {}):
        return found
    if context_of(coordinator).rrc_plant:
        return found
    if cop_possible(coordinator):
        found.append(("cop", partial(CurrentCopSensor, coordinator)))
    if spread_possible(coordinator):
        found.append(("spread", partial(PrimarySpreadSensor, coordinator)))
    found.extend(
        (f"brine:{root}", partial(BrineSpreadSensor, coordinator, root))
        for root in brine_roots(coordinator)
    )
    modes = [mode for mode in MODES if counter_possible(coordinator, mode)]
    if modes and (engine := engine_for(coordinator)) is not None:
        found.extend(
            (
                f"factor:{mode}:{period.value}",
                partial(PerformanceFactorSensor, coordinator, engine, period, mode),
            )
            for mode in modes
            for period in Period
        )
    return found


def track_performance_sensors(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add each performance sensor once its inputs exist."""
    seen: set[str] = set()

    @callback
    def discover() -> None:
        additions: list[Any] = []
        for key, create in _candidates(coordinator):
            if key not in seen:
                seen.add(key)
                additions.append(create())
        if additions:
            async_add_entities(additions)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))
