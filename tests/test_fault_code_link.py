"""Unknown faults point to the official code search; known ones do not."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.binary_sensor import (
    BoschBuderusSystemFaultBinarySensor,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.event import (
    BoschBuderusNotificationEvent,
)
from custom_components.bosch_buderus_heating.faults import (
    FAULT_CODE_SEARCH_URL,
    fault_code_search_url,
    parse_fault_resource,
)
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource
from custom_components.bosch_buderus_heating.sensor import _fault_list_attributes
from tests.ha_test_helpers import MockConfigEntry

NOW = datetime(2026, 8, 19, tzinfo=UTC)
PATH = "/notifications"
UNKNOWN = {"ccd": "X1"}
KNOWN = {"ccd": 6249, "fc": "12", "orig": "8"}
NOTICE = {"ccd": "1234", "fc": "MAINTENANCE"}


def _faults(*values: dict[str, object]):
    return parse_fault_resource(
        Resource(path=PATH, values=tuple(values)), observed_at=NOW
    ).faults


def _coordinator(hass: HomeAssistant) -> BoschBuderusDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one", device_type="K40"), entry
    )
    coordinator.last_update_success = True
    return coordinator


def test_the_link_is_the_official_search_page() -> None:
    assert FAULT_CODE_SEARCH_URL.startswith("https://www.bosch-homecomfort.com/")
    assert FAULT_CODE_SEARCH_URL.endswith("/bosch-fehlercode-suche/")


def test_only_an_unexplained_fault_with_a_code_gets_the_link() -> None:
    unknown, known, notice, no_code = _faults(
        UNKNOWN, KNOWN, NOTICE, {"occurrenceId": "o1"}
    )
    assert fault_code_search_url(unknown) == FAULT_CODE_SEARCH_URL
    assert fault_code_search_url(known) is None
    assert fault_code_search_url(notice) is None
    assert fault_code_search_url(no_code) is None


def test_a_verified_code_with_its_subcode_gets_no_link() -> None:
    (fault,) = _faults({"ccd": 1000, "dcd": "a11", "fc": "12"})
    assert fault_code_search_url(fault) is None


def test_the_fault_binary_sensor_names_the_search_for_an_unexplained_fault(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    sensor = BoschBuderusSystemFaultBinarySensor(coordinator)
    coordinator.faults.process_resources(
        {PATH: Resource(path=PATH, values=(KNOWN,))}, observed_at=NOW
    )
    assert "code_search_url" not in sensor.extra_state_attributes
    coordinator.faults.process_resources(
        {PATH: Resource(path=PATH, values=(KNOWN, UNKNOWN))}, observed_at=NOW
    )
    assert sensor.extra_state_attributes["code_search_url"] == FAULT_CODE_SEARCH_URL
    for _ in range(3):  # a fault ends only after repeated empty answers
        coordinator.faults.process_resources(
            {PATH: Resource(path=PATH, has_values=True)}, observed_at=NOW
        )
    assert not sensor.is_on
    assert "code_search_url" not in sensor.extra_state_attributes


def test_the_fault_list_carries_the_link_only_on_unexplained_entries() -> None:
    attributes = _fault_list_attributes(_faults(KNOWN, UNKNOWN), "en")
    known, unknown = attributes["faults"]  # type: ignore[misc]
    assert "code_search_url" not in known
    assert unknown["code_search_url"] == FAULT_CODE_SEARCH_URL


@pytest.mark.parametrize(
    ("values", "expected"),
    [((UNKNOWN,), FAULT_CODE_SEARCH_URL), ((KNOWN,), None)],
)
def test_the_fault_event_carries_the_link_only_for_an_unexplained_fault(
    hass: HomeAssistant, values, expected
) -> None:
    coordinator = _coordinator(hass)
    entity = BoschBuderusNotificationEvent(coordinator)
    entity._trigger_event = Mock()  # type: ignore[method-assign]
    entity.async_write_ha_state = Mock()  # type: ignore[method-assign]
    coordinator.faults.process_resources(
        {PATH: Resource(path=PATH, values=())}, observed_at=NOW
    )
    event = coordinator.faults.process_resources(
        {PATH: Resource(path=PATH, values=values)}, observed_at=NOW
    )[0]
    entity._handle_fault_event(event)
    attributes = entity._trigger_event.call_args.args[1]
    assert attributes.get("code_search_url") == expected
