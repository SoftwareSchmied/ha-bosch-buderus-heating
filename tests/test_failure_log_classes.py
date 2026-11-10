"""ERR-9: unexpected failures log their error class, the traceback at debug.

The error text may carry account details, file paths or identifiers. Each
case breaks one step that must never stop the integration and checks that
the log names only the class of the error.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import (
    gateway_exchange,
    remove_never_entities,
)
from custom_components.bosch_buderus_heating.faults import (
    FaultEventType,
    FaultTracker,
)
from custom_components.bosch_buderus_heating.repairs import (
    GatewayExchangeRepairFlow,
)
from tests.ha_test_helpers import MockConfigEntry

ROOT = "custom_components.bosch_buderus_heating"
SECRET = "/config/.storage/account@example.invalid"


def _logged(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == level]


def _only_the_class(caplog: pytest.LogCaptureFixture, level: int, message: str) -> None:
    (record,) = _logged(caplog, level)
    assert record.getMessage() == f"{message} (RuntimeError)"
    assert record.exc_info is None
    # The traceback follows at debug level only.
    details = [record for record in caplog.records if record.exc_info]
    assert [record.levelno for record in details] == [logging.DEBUG]
    assert not any(
        SECRET in record.getMessage()
        for record in caplog.records
        if record.levelno > logging.DEBUG
    )


async def test_a_failing_fault_listener(hass: HomeAssistant, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    tracker = FaultTracker(hass, "entry", "gateway")
    event = SimpleNamespace(
        event_type=FaultEventType.RESOLVED, observed_at=datetime.now(UTC)
    )
    tracker._notify_listener(Mock(side_effect=RuntimeError(SECRET)), event)
    _only_the_class(
        caplog,
        logging.ERROR,
        "Fault lifecycle listener failed for transition resolved",
    )


async def test_a_failing_registry_revert(hass: HomeAssistant, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    with patch.object(
        gateway_exchange.dr, "async_get", side_effect=RuntimeError(SECRET)
    ):
        gateway_exchange._revert(hass, [], ("device", set()))
    _only_the_class(
        caplog,
        logging.ERROR,
        "A failed gateway exchange could not be reverted fully",
    )


async def test_a_failing_gateway_exchange_repair(hass: HomeAssistant, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    entry = MockConfigEntry(domain="bosch_buderus_heating")
    entry.add_to_hass(hass)
    flow = GatewayExchangeRepairFlow(entry.entry_id, "issue", "cloud", "b", "new")
    flow.hass = hass
    flow._check = Mock()
    with patch(
        ROOT + ".repairs.async_exchange_gateway",
        AsyncMock(side_effect=RuntimeError(SECRET)),
    ):
        result = await flow._exchange()
    assert result["reason"] == "exchange_failed"
    _only_the_class(caplog, logging.ERROR, "The gateway exchange failed")


async def test_a_failing_cleanup_of_a_view(hass: HomeAssistant, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    entry = MockConfigEntry(domain="bosch_buderus_heating", version=4)
    entry.add_to_hass(hass)
    coordinator = SimpleNamespace(config_entry=entry, hass=hass)
    with patch(ROOT + "._remove_retired_entities", side_effect=RuntimeError(SECRET)):
        remove_never_entities(coordinator)
    _only_the_class(caplog, logging.ERROR, "Registry entries could not be cleaned up")


async def test_a_failing_tidy_up_of_descriptions(hass: HomeAssistant, caplog) -> None:
    from custom_components.bosch_buderus_heating.managed_entry import ManagedEntry

    caplog.set_level(logging.DEBUG)
    owner = SimpleNamespace(
        hass=hass,
        entry=SimpleNamespace(entry_id="entry"),
        _closed=False,
        connections=SimpleNamespace(),  # no snapshot: the tidy-up fails
    )
    inventory = SimpleNamespace(loaded=True, keep_templates=Mock())
    with patch(
        ROOT + ".discovery_inventory.discovery_inventory", return_value=inventory
    ):
        ManagedEntry._keep_templates(owner)
    inventory.keep_templates.assert_not_called()
    (record,) = _logged(caplog, logging.WARNING)
    assert record.getMessage() == (
        "Stored descriptions could not be tidied up (AttributeError)"
    )
    assert record.exc_info is None
