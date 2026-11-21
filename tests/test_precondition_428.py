"""The cloud refuses the data of a gateway with HTTP 428 (#146).

The cycles are real: a coordinator refreshes against a client whose bulk
answers go through the real parser, with a clock the tests move.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.bosch_buderus_heating import coordinator as coordinator_module
from custom_components.bosch_buderus_heating import refresh_outage
from custom_components.bosch_buderus_heating.access_precondition import (
    PRECONDITION_BACKOFF,
    AccessPrecondition,
    access_diagnostics,
    forget_entry_access,
    gateway_access,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    _cycle_failure_category,
)
from custom_components.bosch_buderus_heating.discovery import ROOT_RESOURCE_PATHS
from custom_components.bosch_buderus_heating.item_errors import (
    MAX_RECENT_ITEM_ERRORS,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    PointTError,
    ResourceError,
    ResourcePreconditionRequired,
)
from custom_components.bosch_buderus_heating.pointt.metrics import RequestMetrics
from custom_components.bosch_buderus_heating.pointt.parsers import (
    parse_batch_response,
    parse_gateway,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from tests.test_coordinator import _coordinator, _resource, _snapshot, _success

CORE_ROOTS = frozenset(
    {
        "/system",
        "/gateway",
        "/heatingCircuits",
        "/dhwCircuits",
        "/heatSources",
        "/notifications",
        "/solarCircuits",
    }
)
TRANSLATIONS = Path(coordinator_module.__file__).parent / "translations"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@contextmanager
def clock():
    current = Clock()
    with (
        patch.object(coordinator_module, "monotonic", current),
        patch.object(refresh_outage, "monotonic", current),
    ):
        yield current


def _refusing_client(*, refuse: set[str] | frozenset[str] = CORE_ROOTS) -> AsyncMock:
    """A cloud that refuses the core paths with 428 and answers the rest with
    a gateway 404, through the real bulk parser."""
    client = AsyncMock()
    client.metrics = RequestMetrics()

    def answer(gateway: str, paths: tuple[str, ...]):
        items = []
        for path in paths:
            if path in refuse:
                items.append(
                    {
                        "resourcePath": path,
                        "serverStatus": 428,
                        "errorId": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                        "code": 428,
                        "message": "Precondition Required for gateway 101506113",
                        "internal": "must-not-be-kept",
                    }
                )
            else:
                items.append(
                    {
                        "resourcePath": path,
                        "serverStatus": 200,
                        "gatewayResponse": {"status": 404},
                    }
                )
        return parse_batch_response(
            [{"gatewayId": gateway, "resourcePaths": items}],
            gateway_id=gateway,
            requested_paths=paths,
        )

    client.get_resources_bulk.side_effect = answer
    return client


def _issue(hass: HomeAssistant, coordinator: BoschBuderusDataUpdateCoordinator):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, coordinator._precondition_issue_id()
    )


# ---- the error category -------------------------------------------------


def test_a_428_item_is_its_own_error_category() -> None:
    results = parse_batch_response(
        [
            {
                "gatewayId": "gw",
                "resourcePaths": [{"resourcePath": "/system", "serverStatus": 428}],
            }
        ],
        gateway_id="gw",
        requested_paths=("/system",),
    )
    assert isinstance(results[0].error, ResourcePreconditionRequired)
    assert isinstance(results[0].error, ResourceError)
    assert results[0].status == 428 and results[0].fallback_reason is None
    assert _cycle_failure_category(list(results)) == "precondition_required"


def test_failed_item_keeps_only_redacted_allowlisted_fields() -> None:
    results = parse_batch_response(
        [
            {
                "gatewayId": "gw",
                "resourcePaths": [
                    {
                        "resourcePath": "/system",
                        "serverStatus": 428,
                        "errorId": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                        "code": 428,
                        "message": "Needs user a.b@example.org at 192.168.1.20 "
                        "gateway 101506113 token abcdefghijklmnopqrstuvwxyz0123",
                        "description": "Precondition Required",
                        "details": {"reason": "pairing", "serial": "SN-1234567"},
                        "authorization": "Bearer secret-token",
                        "gatewayId": "101506113",
                    }
                ],
            }
        ],
        gateway_id="gw",
        requested_paths=("/system",),
    )
    details = results[0].error_details
    assert details is not None
    assert set(details) <= {"code", "description", "details", "errorId", "message"}
    assert details["code"] == "428"
    assert details["description"] == "Precondition Required"
    assert details["details"]["reason"] == "pairing"
    flat = json.dumps(details)
    for private in (
        "a.b@example.org",
        "192.168.1.20",
        "101506113",
        "abcdefghijklmnopqrstuvwxyz",
        "SN-1234567",
        "secret-token",
    ):
        assert private not in flat


# ---- the gateway list: flags only ---------------------------------------


def test_gateway_list_keeps_only_presence_flags_of_the_passwords() -> None:
    gateway = parse_gateway(
        {
            "deviceId": "gw-1",
            "deviceType": "icom",
            "userPassword": "my-personal-secret",
            "gatewayPassword": None,
        }
    )
    assert gateway.user_password_set is True
    assert gateway.gateway_password_set is False
    assert "my-personal-secret" not in repr(gateway)
    absent = parse_gateway({"deviceId": "gw-2"})
    assert absent.user_password_set is None and absent.gateway_password_set is None
    assert (
        parse_gateway({"deviceId": "gw-3", "userPassword": ""}).user_password_set
        is False
    )


# ---- real cycles: discovery ---------------------------------------------


async def test_refused_discovery_raises_a_repair_and_backs_off(
    hass: HomeAssistant,
) -> None:
    client = _refusing_client()
    coordinator = _coordinator(hass, client)
    with clock() as now:
        await coordinator.async_refresh()

        assert not coordinator.last_update_success
        assert client.get_resources_bulk.await_count == 1
        issue = _issue(hass, coordinator)
        assert issue is not None
        assert issue.translation_key == "gateway_precondition"
        assert issue.is_fixable is False
        report = coordinator.last_failed_refresh.diagnostics()
        assert report is not None and report["category"] == "precondition_required"

        # Within the first pause the cloud is not asked, however often HA wakes.
        for _ in range(4):
            now.now += 60
            await coordinator.async_refresh()
        assert client.get_resources_bulk.await_count == 1

        # The pause grows with each refusal: 5, 15, 30, 60 minutes, then 60.
        waited = []
        for _ in range(5):
            before = coordinator.gateway_access.precondition.until
            now.now = max(now.now, before) + 1
            await coordinator.async_refresh()
            waited.append(coordinator.gateway_access.precondition.until - now.now)
        assert client.get_resources_bulk.await_count == 6
        assert [round(item / 60) for item in waited] == [15, 30, 60, 60, 60]


async def test_the_repair_goes_when_data_arrives(hass: HomeAssistant) -> None:
    client = _refusing_client()
    coordinator = _coordinator(hass, client)
    with clock() as now:
        await coordinator.async_refresh()
        assert _issue(hass, coordinator) is not None

        # The pairing was repaired: the cloud answers with data.
        client.get_resources_bulk.side_effect = lambda gateway, paths: tuple(
            _success(path, 1.0)
            if path in CORE_ROOTS
            else BatchItemResult(gateway, path, 404)
            for path in paths
        )
        now.now += PRECONDITION_BACKOFF[0].total_seconds() + 1
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert _issue(hass, coordinator) is None
    assert not coordinator.gateway_access.precondition.active


async def test_a_refusal_with_other_failures_in_the_majority_is_no_precondition(
    hass: HomeAssistant,
) -> None:
    """One 428 among server errors is an outage, not a missing precondition."""
    client = AsyncMock()
    client.metrics = RequestMetrics()
    client.get_resources_bulk.side_effect = lambda gateway, paths: tuple(
        BatchItemResult(gateway, path, 428, error=None, server_status=428)
        if index == 0
        else BatchItemResult(gateway, path, 500, server_status=500)
        for index, path in enumerate(paths)
    )
    client.get_resource.side_effect = PointTError()
    coordinator = _coordinator(hass, client)
    with clock():
        await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert _issue(hass, coordinator) is None


# ---- real cycles: a later refresh ---------------------------------------


async def test_a_later_refresh_refused_with_428_raises_the_repair(
    hass: HomeAssistant,
) -> None:
    client = _refusing_client(refuse=frozenset({"/system/a", "/system/b"}))
    coordinator = _coordinator(hass, client)
    paths = ("/system/a", "/system/b")
    coordinator.resources = {path: _resource(path, 1.0) for path in paths}
    coordinator.data = {
        path: _snapshot(resource) for path, resource in coordinator.resources.items()
    }
    coordinator._paths_by_group = {PollGroup.FAST: paths}
    with clock() as now:
        await coordinator.async_refresh()

        assert not coordinator.last_update_success
        assert _issue(hass, coordinator) is not None
        # No breaker: the gateway answers, the cloud refuses.
        assert coordinator._gateway_failure_count == 0
        assert client.get_resources_bulk.await_count == 1
        now.now += 60
        await coordinator.async_refresh()
        assert client.get_resources_bulk.await_count == 1  # paused

        client.get_resources_bulk.side_effect = lambda gateway, paths: tuple(
            _success(path, 2.0) for path in paths
        )
        now.now += PRECONDITION_BACKOFF[0].total_seconds() + 1
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert _issue(hass, coordinator) is None
    assert all(coordinator.data[path].resource.value == 2.0 for path in paths)


# ---- diagnostics ---------------------------------------------------------


async def test_failed_items_are_counted_and_listed_redacted(
    hass: HomeAssistant,
) -> None:
    client = _refusing_client()
    coordinator = _coordinator(hass, client)
    with clock() as now:
        await coordinator.async_refresh()
        now.now += PRECONDITION_BACKOFF[0].total_seconds() + 1
        await coordinator.async_refresh()
        entry_id = coordinator.config_entry.entry_id
        report = access_diagnostics(hass, entry_id, now.now)

    assert len(report) == 1
    errors = report[0]["bulk_item_errors"]
    assert errors["status_counts"]["428"] == 2 * len(
        CORE_ROOTS & set(ROOT_RESOURCE_PATHS)
    )
    assert 0 < len(errors["recent"]) <= MAX_RECENT_ITEM_ERRORS
    first = errors["recent"][0]
    assert first["status"] == 428 and first["error"] == "ResourcePreconditionRequired"
    assert first["count"] == 2  # the same failure again counts up its entry
    assert first["details"]["code"] == "428"
    flat = json.dumps(report)
    assert "101506113" not in flat and "must-not-be-kept" not in flat
    assert report[0]["precondition"]["active"] is True
    assert report[0]["precondition"]["refusals_in_a_row"] == 2


def test_gateway_flags_are_kept_without_values_and_not_overwritten(
    hass: HomeAssistant,
) -> None:
    access = gateway_access(hass, "entry", "gw")
    access.note_gateway(
        Gateway("gw", user_password_set=True, gateway_password_set=False)
    )
    access.note_gateway(Gateway("gw"))  # a stored gateway reports nothing
    report = access_diagnostics(hass, "entry", 0.0)
    assert report[0]["gateway_list"] == {
        "user_password_set": True,
        "gateway_password_set": False,
    }


def test_the_recent_list_is_bounded() -> None:
    access = AccessPrecondition()
    assert access.refused(0.0) == PRECONDITION_BACKOFF[0].total_seconds()
    from custom_components.bosch_buderus_heating.item_errors import ItemErrorLog

    log = ItemErrorLog()
    log.record(
        BatchItemResult("gw", f"/system/p{index}", 428, server_status=428)
        for index in range(MAX_RECENT_ITEM_ERRORS + 7)
    )
    report = log.diagnostics()
    assert len(report["recent"]) == MAX_RECENT_ITEM_ERRORS
    assert report["status_counts"] == {"428": MAX_RECENT_ITEM_ERRORS + 7}


async def test_unloading_the_entry_ends_pause_counts_and_note(
    hass: HomeAssistant,
) -> None:
    client = _refusing_client()
    coordinator = _coordinator(hass, client)
    with clock():
        await coordinator.async_refresh()
        assert _issue(hass, coordinator) is not None
        forget_entry_access(hass, coordinator.config_entry.entry_id)
        assert _issue(hass, coordinator) is None
        assert access_diagnostics(hass, coordinator.config_entry.entry_id, 0.0) == []
        # A new coordinator (reload) asks at once and finds the refusal again.
        again = BoschBuderusDataUpdateCoordinator(
            hass,
            client,
            Gateway("gateway-one", device_type="MX300"),
            coordinator.config_entry,
        )
        await again.async_refresh()
    assert client.get_resources_bulk.await_count == 2
    assert _issue(hass, again) is not None


async def test_a_reload_asks_at_once_and_clears_the_note(hass: HomeAssistant) -> None:
    """The real unload of the entry ends the pause: reloading retries now."""
    from unittest.mock import MagicMock

    from custom_components.bosch_buderus_heating import async_unload_entry
    from custom_components.bosch_buderus_heating.managed_entry import ManagedEntry

    client = _refusing_client()
    coordinator = _coordinator(hass, client)
    with clock():
        await coordinator.async_refresh()
        assert coordinator.gateway_access.precondition.active
        entry = coordinator.config_entry
        entry.runtime_data = MagicMock(spec=ManagedEntry)
        entry.runtime_data.unload = AsyncMock(return_value=True)
        assert await async_unload_entry(hass, entry)
        assert _issue(hass, coordinator) is None
        assert access_diagnostics(hass, entry.entry_id, 0.0) == []
        assert (
            gateway_access(hass, entry.entry_id, "gateway-one").precondition.remaining(
                1000.0
            )
            == 0
        )


# ---- texts ---------------------------------------------------------------


def test_the_repair_text_exists_in_every_language() -> None:
    files = sorted(TRANSLATIONS.glob("*.json"))
    assert len(files) == 19
    for path in [*files, TRANSLATIONS.parent / "strings.json"]:
        issue = json.loads(path.read_text(encoding="utf-8"))["issues"][
            "gateway_precondition"
        ]
        assert issue["title"].strip() and len(issue["description"]) > 150, path.name
        assert "428" in issue["description"], path.name
        assert not re.search(r"\{[a-z_]+\}", issue["description"]), path.name
