"""Diagnostics of plants without a cloud, redaction of what a brand report may
say, and the cleanup of a removed entry when single steps fail."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.bosch_buderus_heating import (
    _selected_gateway_ids,
    async_remove_entry,
    diagnostics,
)
from custom_components.bosch_buderus_heating.bacon.models import CONF_BACON_IDS
from custom_components.bosch_buderus_heating.const import CONF_GATEWAY_IDS, DOMAIN
from custom_components.bosch_buderus_heating.local_enrollment import (
    LocalEnrollmentError,
)
from tests.test_connection_runtime import entry_for

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


# ---------------------------------------------------------- diagnostics


def test_write_checks_of_several_selections_are_added_up_per_measurement() -> None:
    merged = diagnostics._merged_write_checks(
        [
            {"write_checks": {"flow": {"match": 2, "differs": 1}}},
            {"write_checks": {"flow": {"match": 3}, "power": {"match": 1}}},
            {},
        ]
    )
    assert merged == {
        "flow": {"differs": 1, "match": 5},
        "power": {"match": 1},
    }


@pytest.mark.parametrize(
    ("view", "kind"),
    [
        (SimpleNamespace(read_only_view=True), "local_plant"),
        (
            SimpleNamespace(
                faults=SimpleNamespace(local_authority=False), inventory_view=True
            ),
            "kept_for_cloud",
        ),
        (
            SimpleNamespace(
                faults=SimpleNamespace(local_authority=True), inventory_view=True
            ),
            "dormant",
        ),
        (
            SimpleNamespace(
                faults=SimpleNamespace(local_authority=True), inventory_view=False
            ),
            "held",
        ),
    ],
)
def test_the_kind_of_a_view_without_a_cloud(view, kind) -> None:
    assert diagnostics._view_kind(view) == kind


def test_a_brand_report_names_only_known_brands() -> None:
    def target(details):
        return SimpleNamespace(to_data=lambda: {"local_details": details})

    assert diagnostics._local_brand(target({"brand": "buderus"})) == "buderus"
    # Reported free text, even a look-alike, is never passed on.
    assert diagnostics._local_brand(target({"brand": "Buderus-SN12345"})) == (
        "not_reported"
    )
    assert diagnostics._local_brand(target({"brand": 5})) == "not_reported"
    assert diagnostics._local_brand(target("text")) == "not_reported"

    def broken():
        raise RuntimeError("serial 12345")

    assert diagnostics._local_brand(SimpleNamespace(to_data=broken)) == ("not_reported")


async def test_a_view_that_cannot_be_reported_does_not_break_the_report(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken:
        @property
        def gateway(self):
            raise RuntimeError("serial 12345")

    runtime = SimpleNamespace(
        hass=hass,
        entry=SimpleNamespace(entry_id="entry"),
        connections=SimpleNamespace(
            snapshot=SimpleNamespace(bindings={}),
            target_views=lambda: {"t1": Broken()},
        ),
    )
    with caplog.at_level(logging.INFO):
        reports = diagnostics._views_without_cloud(runtime, {"t1": 1})
    assert reports == [{"target": 1, "error": "unavailable"}]
    assert "12345" not in "\n".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.INFO
    )


# ------------------------------------------------------- gateway selection


@pytest.mark.parametrize(
    "data",
    [
        {},
        {CONF_GATEWAY_IDS: "gateway"},
        {CONF_GATEWAY_IDS: []},
        {CONF_GATEWAY_IDS: ["a", ""]},
        {CONF_GATEWAY_IDS: ["a", 5]},
    ],
)
def test_an_invalid_gateway_selection_is_refused(data) -> None:
    with pytest.raises(ValueError, match="selection is invalid"):
        _selected_gateway_ids(data)


def test_a_valid_selection_and_a_selection_with_only_devices() -> None:
    assert _selected_gateway_ids({CONF_GATEWAY_IDS: ["a", "b"]}) == {"a", "b"}
    assert (
        _selected_gateway_ids({CONF_GATEWAY_IDS: [], CONF_BACON_IDS: ["device"]})
        == frozenset()
    )


# ------------------------------------------------------ entry removal


async def test_a_failing_cleanup_step_never_stops_the_removal(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    entry, _, _ = entry_for(hass)
    issue_id = "managed_rate_limit_test"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        data={"entry_id": entry.entry_id},
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="managed_rate_limit",
    )
    stores = AsyncMock()
    with (
        patch(
            "custom_components.bosch_buderus_heating.local_enrollment."
            "LocalEnrollmentJournal.remove_owner",
            AsyncMock(side_effect=LocalEnrollmentError("storage_failed")),
        ),
        patch(
            "custom_components.bosch_buderus_heating.local_setup_history."
            "async_forget_local_setup_history",
            AsyncMock(side_effect=OSError("token=secret")),
        ),
        patch(
            "custom_components.bosch_buderus_heating.store_cleanup."
            "async_remove_entry_stores",
            stores,
        ),
        caplog.at_level(logging.WARNING),
    ):
        await async_remove_entry(hass, entry)
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert "Pending local setup storage could not be cleared" in messages
    assert "Local setup history could not be deleted" in messages
    assert not any("secret" in message for message in messages)
    # Everything after the failed steps still ran.
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    stores.assert_awaited_once_with(hass, entry)
