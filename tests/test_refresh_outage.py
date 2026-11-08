"""#60: a failed cloud refresh is an error only once the outage lasts."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import (
    coordinator as coordinator_module,
)
from custom_components.bosch_buderus_heating import refresh_outage
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.refresh_outage import (
    ERROR_GRACE_SECONDS,
    ERROR_MIN_FAILURES,
    RefreshOutageLog,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from tests.test_coordinator import _coordinator as cloud_coordinator
from tests.test_coordinator import _resource, _snapshot, _success

LOGGER = logging.getLogger("tests.refresh_outage")
PATH = "/heatSources/actualModulation"


def _levels(caplog: pytest.LogCaptureFixture) -> list[int]:
    return [record.levelno for record in caplog.records]


def test_the_values_match_the_documented_grace() -> None:
    assert ERROR_GRACE_SECONDS == 300
    assert ERROR_MIN_FAILURES == 3


def test_a_short_outage_stays_at_debug_level(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER.name)
    log = RefreshOutageLog(LOGGER, "cloud")
    for now in (0.0, 60.0, 120.0, 180.0, 240.0):
        log.failed(RuntimeError("no usable resources (timeout)"), now)
    assert _levels(caplog) == [logging.DEBUG] * 5
    assert "Logged as an error if it lasts 5 minutes" in caplog.records[0].getMessage()
    log.recovered()
    assert (log.failures, log.since, log.reported) == (0, None, False)


def test_a_lasting_outage_is_one_error(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER.name)
    log = RefreshOutageLog(LOGGER, "cloud")
    for now in (0.0, 60.0, 120.0, 180.0, 240.0, 300.0, 360.0, 420.0):
        log.failed(RuntimeError("no usable resources (timeout)"), now)
    assert (
        _levels(caplog) == [logging.DEBUG] * 5 + [logging.ERROR] + [logging.DEBUG] * 2
    )
    error = caplog.records[5].getMessage()
    assert "Error fetching cloud data: no usable resources (timeout)" in error
    assert "for 5 minutes (6 failed refreshes in a row)" in error
    assert "again (8 failed refreshes in a row)" in caplog.records[-1].getMessage()
    # After a working refresh the next outage gets its own grace and error.
    log.recovered()
    caplog.clear()
    for now in (1000.0, 1100.0, 1200.0, 1300.0):
        log.failed(RuntimeError("no usable resources (timeout)"), now)
    assert _levels(caplog) == [logging.DEBUG] * 3 + [logging.ERROR]


def test_both_the_time_and_the_count_must_be_reached(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER.name)
    log = RefreshOutageLog(LOGGER, "cloud")
    # Two failures far apart are not yet an error ...
    log.failed(RuntimeError("x"), 0.0)
    log.failed(RuntimeError("x"), 900.0)
    assert logging.ERROR not in _levels(caplog)
    # ... the third one is.
    log.failed(RuntimeError("x"), 901.0)
    assert _levels(caplog)[-1] == logging.ERROR
    # Without a time, the clock of the module is used.
    log = RefreshOutageLog(LOGGER, "cloud")
    with patch.object(refresh_outage, "monotonic", return_value=42.0):
        log.failed(RuntimeError("x"))
    assert log.since == 42.0


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_home_assistant_logs_no_error_for_a_short_outage(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    client = AsyncMock()
    coordinator = cloud_coordinator(hass, client)
    coordinator.resources = {PATH: _resource(PATH, 1.0)}
    coordinator.data = {PATH: _snapshot(coordinator.resources[PATH])}
    coordinator._paths_by_group = {PollGroup.FAST: (PATH,)}
    client.get_resources_bulk.side_effect = ServiceUnavailable(503)
    client.get_resource.side_effect = ServiceUnavailable(503)
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    clock = _Clock()

    async def refresh(at: float) -> None:
        clock.now = 1000.0 + at
        coordinator._next_update = {PollGroup.FAST: 0.0}
        await coordinator.async_refresh()

    def errors() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.ERROR
        ]

    with (
        patch.object(coordinator_module, "monotonic", clock),
        patch.object(refresh_outage, "monotonic", clock),
    ):
        # A short flicker: two failed cycles, then the cloud answers again.
        await refresh(0.0)
        assert not coordinator.last_update_success
        await refresh(60.0)
        assert errors() == []
        client.get_resources_bulk.side_effect = None
        client.get_resources_bulk.return_value = (_success(PATH, 2.0),)
        await refresh(120.0)
        assert coordinator.last_update_success
        assert errors() == []
        assert any("data recovered" in r.getMessage() for r in caplog.records)
        assert coordinator._refresh_outage.failures == 0
        # A lasting outage: one error after five minutes, none after that.
        client.get_resources_bulk.side_effect = ServiceUnavailable(503)
        for minute in range(3, 12):
            await refresh(minute * 60.0)
            if minute < 8:
                assert errors() == [], minute
        (error,) = errors()
        assert error.startswith(f"Error fetching {coordinator.name} data: ")
        assert "for 5 minutes (6 failed refreshes in a row)" in error
        assert not coordinator.last_update_success
        assert coordinator.last_exception is not None
