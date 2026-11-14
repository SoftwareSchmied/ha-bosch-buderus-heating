"""The plant's own recordings: period entities and long-term statistics.

The heat pump records its energy, starts, working time and two
temperatures per hour, day and month. The integration reads them from the
local data port (preferred) or the cloud, shows yesterday, the previous and
current month and year as entities and, when switched on in the options,
imports the hourly values into Home Assistant's long-term statistics as
statistics of their own. See ``docs/recordings.md``.

``RecordingsView`` is the interface for other parts of the integration:
the days and months of every series, for example for performance factors
back to the first recorded day.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from homeassistant.core import HomeAssistant

from .ledger import DailyValue
from .manager import RecordingsManager, manager_of_gateway
from .periods import Period, PeriodValue, month_value

__all__ = [
    "DailyValue",
    "Period",
    "PeriodValue",
    "RecordingsView",
    "diagnostics",
    "recordings_view",
]


class RecordingsView:
    """Read-only access to one gateway's recordings.

    Keys are measure keys: a series (``total_heat``, ``ch_compressor``,
    ``outdoor_temperature`` …) or the electricity of a mode
    (``total_electricity``: compressor plus heater). Values are in kWh, h,
    a count or °C; temperatures as ``y`` (sum) and ``c`` (count), their mean
    is ``DailyValue.value``. Only days the plant recorded are returned; a
    gap is missing or has ``c == 0``. Days are kept for about 800 days,
    months for as long as the plant has them.
    """

    def __init__(self, manager: RecordingsManager) -> None:
        self._manager = manager

    @property
    def loaded(self) -> bool:
        return self._manager.loaded

    def measure_keys(self) -> tuple[str, ...]:
        return tuple(measure.key for measure in self._manager.measures())

    def first_day(self, key: str) -> date | None:
        """The first recorded day of a measure (the latest of its parts)."""
        measure = next((m for m in self._manager.measures() if m.key == key), None)
        if measure is None:
            return None
        days = [
            ledger.first_day()
            for part in measure.parts
            if (ledger := self._manager.ledgers.get(part)) is not None
        ]
        if not days or None in days:
            return None
        return max(day for day in days if day is not None)

    def daily(self, key: str, start: date, end: date) -> tuple[DailyValue, ...]:
        """The days from ``start`` to ``end`` (both included)."""
        return self._manager.daily(key, start, end)

    def monthly(self, key: str, first: date, today: date) -> PeriodValue | None:
        """One month (``first`` is its first day), from its days or the
        plant's month value; ``final`` once the month is complete."""
        measure = next((m for m in self._manager.measures() if m.key == key), None)
        if measure is None:
            return None
        from .periods import combine

        values = []
        for part in measure.parts:
            ledger = self._manager.ledgers.get(part)
            value = month_value(ledger, first, today) if ledger is not None else None
            if value is None:
                return None
            values.append(value)
        return combine(values)

    def period(self, key: str, period: Period, today: date) -> PeriodValue | None:
        measure = next((m for m in self._manager.measures() if m.key == key), None)
        if measure is None:
            return None
        return self._manager.recorded(measure, period, today)

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Called after every run of the reads."""
        return self._manager.add_listener(listener)

    def acquire(self) -> Callable[[], None]:
        """Keep the recordings read even without enabled entities; call the
        returned function to release."""
        return self._manager.acquire()


def recordings_view(hass: HomeAssistant, gateway_id: str) -> RecordingsView | None:
    manager = manager_of_gateway(hass, gateway_id)
    return RecordingsView(manager) if manager is not None else None


def diagnostics(hass: HomeAssistant, gateway_id: str) -> dict[str, object]:
    """The ``recordings`` section of a gateway's diagnostics."""
    manager = manager_of_gateway(hass, gateway_id)
    if manager is None:
        return {"waiting": "not_started"}
    return manager.diagnostics()
