"""Gateway exchange: damaged stored data, journal read failures, refused or
changed situations and follow-up steps that fail without losing the rest."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.bosch_buderus_heating import gateway_exchange as ge
from custom_components.bosch_buderus_heating.const import (
    DOMAIN,
    GATEWAY_EXCHANGE_ISSUE_PREFIX,
)
from custom_components.bosch_buderus_heating.gateway_exchange import (
    CounterRecord,
    ExchangePlan,
    GatewayExchangeError,
    ModuleEvidence,
    ModuleFingerprints,
    async_check_exchange,
    async_resume_gateway_exchange,
    async_verify_exchange_list,
    clear_entry_exchange_issues,
    compare_modules,
    exchange_journal,
    module_fingerprint,
)
from custom_components.bosch_buderus_heating.managed_cloud import CloudConnection
from custom_components.bosch_buderus_heating.managed_entry import ManagedEntry
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    PointTError,
    ProtocolError,
    ResourceError,
)
from tests.test_connection_runtime import entry_for

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
ROOT = "custom_components.bosch_buderus_heating.gateway_exchange"


def envelope(data, version=1):
    return {"version": version, "minor_version": 1, "key": "", "data": data}


def _plan_data(**changes):
    data = {
        "attempt": "a1",
        "entry_id": "owner",
        "connection_id": "c",
        "binding_id": "b",
        "target_id": "t",
        "old": "gw-old",
        "new": "gw-new",
        "at": "2026-11-15T10:00:00+00:00",
        "entities": [["sensor.x", "gw-old:a", "gw-new:a"]],
        "device_id": "dev",
        "device_identifiers": [[DOMAIN, "gw-old"]],
    }
    data.update(changes)
    return data


# ------------------------------------------------------ module evidence


def test_rows_without_a_name_or_serial_are_no_device_module() -> None:
    rows = [
        "text",
        {"ModuleSerialNumber": "s1"},  # nothing names it
        {"ProductName": "x"},  # no serial
        {"ModuleSerialNumber": True, "ProductName": "x"},
        {"ModuleSerialNumber": "  ", "ProductName": "x"},
        {"ModuleSerialNumber": "s2", "SwIdenStr": "app", "Tok": 8, "Id": 3},
    ]
    found = module_fingerprint(rows)
    assert len(found) == 1
    # A serial number never appears in the stored value.
    assert not any("s2" in value for value in found)


def test_missing_evidence_on_either_side_is_unknown() -> None:
    assert compare_modules(None, frozenset({"a"})) is ModuleEvidence.UNKNOWN
    assert compare_modules(frozenset({"a"}), frozenset()) is ModuleEvidence.UNKNOWN
    assert compare_modules(frozenset({"a"}), frozenset({"b"})) is (
        ModuleEvidence.NO_MATCH
    )
    assert compare_modules(frozenset({"a", "b"}), frozenset({"b"})) is (
        ModuleEvidence.MATCH
    )


@pytest.mark.parametrize(
    "data",
    [
        None,
        "x",
        {"at": 5, "settled": True},
        {"at": "t", "settled": "yes"},
        {"at": "t", "settled": True, "last_reset": 4},
    ],
)
def test_a_damaged_counter_record_is_ignored(data) -> None:
    assert CounterRecord.from_data(data) is None


# -------------------------------------------------- stored fingerprints


async def test_damaged_stored_fingerprints_keep_only_the_good_parts(
    hass, hass_storage
) -> None:
    key = f"{DOMAIN}.module_fingerprints.owner"
    hass_storage[key] = envelope(
        {
            "targets": {
                "t1": {"modules": ["a", "b"], "seen": "x"},
                "t2": {"modules": ["a", 3]},
                "t3": "text",
                3: {"modules": ["a"]},
            },
            "declined": ["b|g", 5],
            "exchanges": {"t1": {"at": "when"}, "t2": {"at": 3}, "t3": "no"},
            "counters": {
                "u1": {"at": "t", "settled": True},
                "u2": {"at": 1, "settled": True},
            },
        },
        version=ge.FINGERPRINT_VERSION,
    )
    fingerprints = ModuleFingerprints(hass, "owner")
    await fingerprints.async_load()
    assert fingerprints.get("t1") == {"a", "b"}
    assert fingerprints.get("t2") is None
    assert fingerprints.get("t3") is None
    assert fingerprints.declined("b", "g") is True
    assert fingerprints.exchange("t1") == "when"
    assert fingerprints.exchange("t2") is None
    assert fingerprints.counter("u1") == CounterRecord("t", True)
    assert fingerprints.counter("u2") is None


async def test_an_unreadable_fingerprint_store_starts_empty(hass) -> None:
    fingerprints = ModuleFingerprints(hass, "owner")
    with patch.object(
        fingerprints._store, "async_load", AsyncMock(side_effect=OSError("disk"))
    ):
        await fingerprints.async_load()
    assert fingerprints.get("t1") is None
    # Loaded now: a later answer is remembered, not blocked.
    assert fingerprints.observe("t1", [{"ModuleSerialNumber": "s", "ProductName": "p"}])


async def test_nothing_is_remembered_before_the_store_was_read(hass) -> None:
    fingerprints = ModuleFingerprints(hass, "owner")
    rows = [{"ModuleSerialNumber": "s", "ProductName": "p"}]
    assert fingerprints.observe("t1", rows) is False
    fingerprints.set_counter("u", CounterRecord("t", True))
    assert fingerprints.counter("u") is None
    await fingerprints.async_load()
    assert fingerprints.observe("t1", rows) is True
    # The same answer again changes nothing; an empty one never replaces it.
    assert fingerprints.observe("t1", rows) is False
    assert fingerprints.observe("t1", []) is False
    fingerprints.forget("t1")
    assert fingerprints.get("t1") is None
    fingerprints.forget("t1")


async def test_a_counter_decision_is_saved_once_and_on_demand(hass) -> None:
    fingerprints = ModuleFingerprints(hass, "owner")
    await fingerprints.async_load()
    record = CounterRecord("t", True)
    with (
        patch.object(fingerprints._store, "async_delay_save") as delayed,
        patch.object(fingerprints._store, "async_save", AsyncMock()) as saved,
    ):
        fingerprints.set_counter("u", record)
        fingerprints.set_counter("u", record)  # unchanged: nothing more
        delayed.assert_called_once()
        fingerprints.set_counter("u2", CounterRecord("t", False), immediate=True)
        await hass.async_block_till_done()
        saved.assert_awaited_once()
    assert fingerprints.counter("u2") == CounterRecord("t", False)


# ------------------------------------------------------------ the plan


def test_a_stored_plan_survives_a_round_trip() -> None:
    plan = ExchangePlan.from_data(_plan_data())
    assert plan is not None
    assert ExchangePlan.from_data(plan.to_data()) == plan


@pytest.mark.parametrize(
    "changes",
    [
        {"attempt": ""},
        {"new": 5},
        {"entities": "x"},
        {"device_identifiers": None},
        {"device_id": 5},
        {"entities": [["only", "two"]]},
        {"entities": [["a", "b", 3]]},
        {"device_identifiers": [["a", "b", "c"]]},
        {"device_identifiers": ["ab"]},
    ],
)
def test_a_damaged_stored_plan_is_refused(changes) -> None:
    assert ExchangePlan.from_data(_plan_data(**changes)) is None
    assert ExchangePlan.from_data("plan") is None


def test_a_plan_without_a_device_is_fine() -> None:
    plan = ExchangePlan.from_data(_plan_data(device_id=None, device_identifiers=[]))
    assert plan is not None
    assert plan.device_id is None
    assert plan.device_identifiers == ()


# --------------------------------------------------------- the journal


async def test_journal_content_of_the_wrong_shape_is_no_journal(
    hass, hass_storage
) -> None:
    key = "bosch_buderus_heating.gateway_exchange.owner"
    journal = exchange_journal(hass, "owner")
    assert await journal.async_load() is None  # nothing stored
    for content in (
        {"plan": _plan_data(), "applied_run": 3},
        {"plan": _plan_data(), "finished": "yes"},
        {"plan": "broken"},
        {},
    ):
        hass_storage[key] = envelope(content, version=ge.JOURNAL_VERSION)
        assert await journal.async_load() is None, content
    hass_storage[key] = envelope(
        {"plan": _plan_data(), "applied_run": "run-1", "finished": True},
        version=ge.JOURNAL_VERSION,
    )
    state = await journal.async_load()
    assert state is not None
    assert (state.applied_run, state.finished) == ("run-1", True)


async def test_an_unreadable_journal_is_logged_by_class_only(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    journal = exchange_journal(hass, "owner")
    with (
        patch.object(
            ge.ExchangeJournal,
            "_store",
            return_value=Mock(
                async_load=AsyncMock(side_effect=OSError("token=secret"))
            ),
        ),
        caplog.at_level(logging.ERROR),
    ):
        assert await journal.async_load() is None
    visible = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert visible == ["The gateway exchange journal could not be read (OSError)"]


async def test_a_journal_of_another_attempt_is_not_removed(hass, hass_storage) -> None:
    key = "bosch_buderus_heating.gateway_exchange.owner"
    hass_storage[key] = envelope(
        {"plan": _plan_data(), "applied_run": None, "finished": False},
        version=ge.JOURNAL_VERSION,
    )
    journal = exchange_journal(hass, "owner")
    await journal.async_remove("another-attempt")
    assert key in hass_storage
    await journal.async_remove("a1")
    assert key not in hass_storage


async def test_the_journal_of_an_unknown_plan_removal_is_quiet(
    hass, hass_storage
) -> None:
    other = "bosch_buderus_heating.gateway_exchange.other"
    hass_storage[other] = envelope(
        {"plan": _plan_data(entry_id="other"), "applied_run": None, "finished": False},
        version=ge.JOURNAL_VERSION,
    )
    await exchange_journal(hass, "owner").async_remove("a1")
    assert "bosch_buderus_heating.gateway_exchange.owner" not in hass_storage
    assert other in hass_storage


async def test_a_journal_that_belongs_to_another_entry_is_dropped_on_resume(
    hass, hass_storage
) -> None:
    entry, _, _ = entry_for(hass)
    key = f"bosch_buderus_heating.gateway_exchange.{entry.entry_id}"
    hass_storage[key] = envelope(
        {
            "plan": _plan_data(entry_id="someone-else"),
            "applied_run": None,
            "finished": False,
        },
        version=ge.JOURNAL_VERSION,
    )
    with patch.object(ge, "_async_complete", AsyncMock()) as complete:
        await async_resume_gateway_exchange(hass, entry)
    complete.assert_not_awaited()
    assert key not in hass_storage


async def test_an_unfinishable_exchange_stops_the_entry_and_keeps_the_journal(
    hass, hass_storage, caplog: pytest.LogCaptureFixture
) -> None:
    from homeassistant.exceptions import ConfigEntryError

    entry, _, _ = entry_for(hass)
    key = f"bosch_buderus_heating.gateway_exchange.{entry.entry_id}"
    hass_storage[key] = envelope(
        {
            "plan": _plan_data(entry_id=entry.entry_id),
            "applied_run": None,
            "finished": False,
        },
        version=ge.JOURNAL_VERSION,
    )
    # The configuration does not hold the plan's binding any more.
    with caplog.at_level(logging.ERROR), pytest.raises(ConfigEntryError):
        await async_resume_gateway_exchange(hass, entry)
    assert key in hass_storage
    assert "configuration_changed" in caplog.text
    assert "gw-new" not in caplog.text


# ------------------------------------------------- the verified situation


def _running(entry, driver):
    runtime = Mock(spec=ManagedEntry)
    runtime.connections = Mock()
    runtime.connections.connection_driver.return_value = driver
    entry.runtime_data = runtime


async def test_an_entry_that_is_not_running_cannot_exchange(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    with pytest.raises(GatewayExchangeError) as error:
        await async_verify_exchange_list(hass, entry, cloud, "b", "gw-new")
    assert error.value.reason == "exchange_unavailable"


async def test_a_local_or_missing_driver_is_no_cloud_access(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    _running(entry, object())
    with pytest.raises(GatewayExchangeError) as error:
        await async_verify_exchange_list(hass, entry, cloud, "b", "gw-new")
    assert error.value.reason == "cloud_unavailable"


async def test_a_cloud_that_cannot_list_gateways_aborts_without_a_verdict(
    hass,
) -> None:
    entry, cloud, _ = entry_for(hass)
    driver = Mock(spec=CloudConnection)
    driver.gateways = AsyncMock(side_effect=PointTError("rate limit"))
    _running(entry, driver)
    with pytest.raises(GatewayExchangeError) as error:
        await async_verify_exchange_list(hass, entry, cloud, "b", "gw-new")
    assert error.value.reason == "cloud_unavailable"


async def test_a_removed_binding_changes_the_situation(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    driver = Mock(spec=CloudConnection)
    driver.gateways = AsyncMock(return_value=())
    _running(entry, driver)
    with pytest.raises(GatewayExchangeError) as error:
        await async_verify_exchange_list(hass, entry, cloud, "removed", "gw-new")
    assert error.value.reason == "configuration_changed"


async def test_a_gateway_that_is_no_longer_the_candidate_is_refused(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    binding = next(
        key
        for key, value in entry.data["bindings"].items()
        if value["connection_id"] == cloud and value["adapter"] == "pointt"
    )
    driver = Mock(spec=CloudConnection)
    driver.gateways = AsyncMock(return_value=())
    _running(entry, driver)
    with (
        patch(ROOT + ".exchange_candidate", return_value="a-different-gateway"),
        pytest.raises(GatewayExchangeError) as error,
    ):
        await async_verify_exchange_list(hass, entry, cloud, binding, "gw-new")
    assert error.value.reason == "exchange_changed"


async def test_a_configuration_error_while_verifying_is_a_changed_situation(
    hass,
) -> None:
    from custom_components.bosch_buderus_heating.configuration import (
        ConfigurationError,
    )

    entry, cloud, _ = entry_for(hass)
    binding = next(
        key
        for key, value in entry.data["bindings"].items()
        if value["connection_id"] == cloud and value["adapter"] == "pointt"
    )
    driver = Mock(spec=CloudConnection)
    driver.gateways = AsyncMock(return_value=())
    _running(entry, driver)
    with (
        patch(ROOT + ".exchange_candidate", side_effect=ConfigurationError("x")),
        pytest.raises(GatewayExchangeError) as error,
    ):
        await async_verify_exchange_list(hass, entry, cloud, binding, "gw-new")
    assert error.value.reason == "configuration_changed"


# ------------------------------------------------------ the evidence check


async def test_a_disabled_or_foreign_binding_cannot_be_checked(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    local_binding = next(
        key
        for key, value in entry.data["bindings"].items()
        if value["adapter"] != "pointt"
    )
    for connection, binding in (
        (cloud, "removed"),
        ("removed", "b"),
        (cloud, local_binding),
    ):
        with pytest.raises(GatewayExchangeError) as error:
            await async_check_exchange(hass, entry, connection, binding, "gw-new")
        assert error.value.reason == "configuration_changed", (connection, binding)


def _pointt_binding(entry, cloud) -> str:
    return next(
        key
        for key, value in entry.data["bindings"].items()
        if value["connection_id"] == cloud and value["adapter"] == "pointt"
    )


async def _check_with(hass, entry, cloud, answer):
    binding = _pointt_binding(entry, cloud)
    driver = Mock(spec=CloudConnection)
    driver.client = Mock()
    driver.client.get_local_identity_evidence = answer
    with patch(ROOT + ".async_verify_exchange_list", AsyncMock(return_value=driver)):
        return await async_check_exchange(hass, entry, cloud, binding, "gw-new")


@pytest.mark.parametrize(
    "failure", [ResourceError("/system/info", 404), ProtocolError("odd")]
)
async def test_a_new_gateway_without_module_data_gives_unknown_evidence(
    hass, failure
) -> None:
    entry, cloud, _ = entry_for(hass)
    check = await _check_with(hass, entry, cloud, AsyncMock(side_effect=failure))
    assert check.evidence is ModuleEvidence.UNKNOWN
    assert check.new == "gw-new"


async def test_a_cloud_that_cannot_answer_gives_no_unverified_verdict(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    with pytest.raises(GatewayExchangeError) as error:
        await _check_with(hass, entry, cloud, AsyncMock(side_effect=PointTError("x")))
    assert error.value.reason == "cloud_unavailable"


async def test_a_configuration_change_during_the_check_is_noticed(hass) -> None:
    from custom_components.bosch_buderus_heating.configuration_management import (
        ConfigurationManagement,
    )
    from custom_components.bosch_buderus_heating.const import PollingProfile

    entry, cloud, _ = entry_for(hass)

    async def evidence(_gateway):
        # The same connection is changed while the cloud is asked.
        management = ConfigurationManagement(hass, entry)
        management.set_polling_profile(
            management.edit(cloud), PollingProfile.CLOUD_FRIENDLY
        )
        return None, {"values": []}

    with pytest.raises(GatewayExchangeError) as error:
        await _check_with(hass, entry, cloud, evidence)
    assert error.value.reason == "configuration_changed"


# ------------------------------------------------------------- issues


async def test_only_exchange_issues_of_the_removed_entry_are_cleared(hass) -> None:
    def create(issue_id, entry_id):
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="managed_gateway_exchange",
            data={"entry_id": entry_id},
        )

    create(f"{GATEWAY_EXCHANGE_ISSUE_PREFIX}one", "gone")
    create(f"{GATEWAY_EXCHANGE_ISSUE_PREFIX}two", "other")
    create("something_else", "gone")
    clear_entry_exchange_issues(hass, "gone")
    registry = ir.async_get(hass)
    assert (
        registry.async_get_issue(DOMAIN, f"{GATEWAY_EXCHANGE_ISSUE_PREFIX}one") is None
    )
    assert registry.async_get_issue(DOMAIN, f"{GATEWAY_EXCHANGE_ISSUE_PREFIX}two")
    assert registry.async_get_issue(DOMAIN, "something_else")
