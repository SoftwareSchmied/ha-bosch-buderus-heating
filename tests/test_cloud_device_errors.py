"""B06: device error lists read from the cloud (``/devices/deviceN/errors``).

The rows follow the gateway's documented ``deviceErrors`` schema
(``errorType``, ``occurrenceId``, ``value``, ``allowedValues``). No cloud
capture with radio room devices exists yet; ``device_error_rows`` is the one
place to add it (see docs/faults.md).
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components import persistent_notification as pn
from homeassistant.core import callback

from custom_components.bosch_buderus_heating.const import CONF_GATEWAY_IDS, DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.faults import (
    DeviceError,
    FaultEventType,
    FaultSeverity,
    FaultTracker,
    fault_store_key,
    fault_summary,
    local_device_errors,
    parse_fault_resource,
)
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_api.models import (
    DeviceFault,
    DeviceFaultList,
)
from custom_components.bosch_buderus_heating.notifications import (
    FaultNotifications,
    _incident_key,
    notification_id,
)
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource
from tests.ha_test_helpers import MockConfigEntry

NOW = datetime(2026, 11, 9, 6, tzinfo=UTC)
EXAMPLES = Path(__file__).parent / "fixtures" / "local_api_examples.json"
DEVICE1 = "/devices/device1/errors"
DEVICE2 = "/devices/device2/errors"
SENSOR = "defectiveTempSensor"

# Fingerprints that 0.9.0-alpha.3 gave these rows (computed with that
# version). Stored baselines of existing setups contain them.
OLD_SENSOR = "d307ffac17b434d6525db117"  # "occurrence|10", any device
OLD_VALVE = "4d1de620d4f3dacf1b74ad4f"  # "occurrence|0", any device
OLD_TYPE_SHAPE = "b50c9e5d66ee5b7f6654dbdf"  # {"type": ..., "occurrenceId": 3}


def device_error_rows() -> list[dict[str, object]]:
    """The device error rows the tests read as a cloud answer.

    Today the documented gateway example (``local_api_examples.json``), which
    the cloud is expected to pass on unchanged. Once a real cloud capture with
    radio room devices exists, add its rows here (or parametrize over both) so
    every test below also runs against the real answer.
    """
    examples = json.loads(EXAMPLES.read_text(encoding="utf-8"))
    for example in examples["examples"]:
        if example.get("template") == "/devices/{deviceId}/errors":
            return deepcopy(example["response"]["values"])
    raise AssertionError("the documented device error example is missing")


def rows(*, sensor: str = "active") -> list[dict[str, object]]:
    values = device_error_rows()
    for row in values:
        if row["errorType"] == SENSOR:
            row["value"] = sensor
    return values


def resource(path: str, values: list[dict[str, object]]) -> Resource:
    return Resource(path=path, values=tuple(values), has_values=True)  # type: ignore[arg-type]


def read(tracker: FaultTracker, *items: Resource, when: datetime = NOW):
    return tracker.process_resources(
        {item.path: item for item in items}, observed_at=when
    )


def kinds(events) -> list[tuple[str, str | None]]:
    return [(event.event_type.value, event.fault.code) for event in events]


# ---------------------------------------------------------------- the parser


def test_the_documented_rows_give_one_named_device_fault() -> None:
    parsed = parse_fault_resource(resource(DEVICE1, rows()), observed_at=NOW)

    assert parsed.invalid_entries == 0
    (fault,) = parsed.faults  # the valve status is no fault
    assert fault.code == SENSOR  # named by errorType, not by "active"
    assert fault.severity is FaultSeverity.UNKNOWN
    assert (fault.component_type, fault.component_id) == ("device", "device1")
    assert fault.occurrence_id == "10"
    assert fault_summary(fault, "de") == f"Unbekannte Störung (Code {SENSOR})"
    # The earlier identities are known, so stored incidents keep theirs.
    assert parsed.legacy_fingerprints == ((fault.fingerprint, OLD_SENSOR),)
    assert parsed.retired_fingerprints == (OLD_VALVE,)


def test_the_same_error_on_two_devices_is_two_faults() -> None:
    first = parse_fault_resource(resource(DEVICE1, rows()), observed_at=NOW)
    second = parse_fault_resource(resource(DEVICE2, rows()), observed_at=NOW)

    assert first.faults[0].fingerprint != second.faults[0].fingerprint
    assert second.faults[0].component_id == "device2"


def test_inactive_and_non_error_rows_are_no_faults() -> None:
    parsed = parse_fault_resource(
        resource(DEVICE1, rows(sensor="inactive")), observed_at=NOW
    )
    assert parsed.faults == ()
    assert parsed.invalid_entries == 0
    # Only the valve row would have been a fault before (the inactive one
    # was skipped then too).
    assert parsed.retired_fingerprints == (OLD_VALVE,)


@pytest.mark.parametrize(
    "allowed",
    [
        ["valveTight", "active"],
        ["inactive", "active", "unknown"],
        ["active"],
        [],
        "active",
        ["inactive", 1],
    ],
)
def test_an_active_row_of_a_non_binary_error_is_no_fault(allowed) -> None:
    row = {"errorType": "eTRVValveStatus", "occurrenceId": 0, "value": "active"}
    parsed = parse_fault_resource(
        resource(DEVICE1, [{**row, "allowedValues": allowed}]), observed_at=NOW
    )
    assert parsed.faults == ()
    assert parsed.invalid_entries == 0


def test_rows_without_allowed_values_and_the_type_shape_still_count() -> None:
    parsed = parse_fault_resource(
        resource(
            DEVICE2,
            [
                {"type": "eTRVCommunication", "occurrenceId": 3, "value": "active"},
                {"errorType": "lowBattery", "occurrenceId": 4.0, "value": " Active "},
                {"errorType": "cleared", "value": "active", "state": "resolved"},
                {"errorType": "notAnError", "value": "D1"},
                {"errorType": "noState"},
            ],
        ),
        observed_at=NOW,
    )

    assert [(item.code, item.occurrence_id) for item in parsed.faults] == [
        ("eTRVCommunication", "3"),
        ("lowBattery", "4"),
    ]
    assert dict(parsed.legacy_fingerprints)[parsed.faults[0].fingerprint] == (
        OLD_TYPE_SHAPE
    )
    assert parsed.invalid_entries == 0


def test_an_active_row_without_a_name_is_malformed() -> None:
    parsed = parse_fault_resource(
        resource(DEVICE1, [{"occurrenceId": 5, "value": "active"}, "bad"]),
        observed_at=NOW,
    )
    assert parsed.faults == ()
    assert parsed.invalid_entries == 2


@pytest.mark.parametrize(
    ("path", "values", "fingerprint"),
    [
        ("/notifications", [{"ccd": 6249, "fc": "12"}], "e514db7c8a4090dc8973c2b0"),
        (
            "/notifications",
            [{"ccd": 6249, "fc": "12", "occurrenceId": "incident-1"}],
            "d48519ab5d8922b218c7014c",
        ),
        (
            "/notifications",
            [{"ccd": 1000, "dcd": "A11", "orig": "HS", "fc": "BLOCKING"}],
            "9d699f2184b149ab30ab9d19",
        ),
        (
            "/heatSources/hs1/activefailure",
            [{"ccd": "HS", "fc": "LOCKING"}],
            "62fe92a621e4931dd02de0d8",
        ),
    ],
)
def test_other_fault_sources_keep_their_fingerprints(path, values, fingerprint):
    """Golden values of 0.9.0-alpha.3: stored faults of these sources stay."""
    parsed = parse_fault_resource(resource(path, values), observed_at=NOW)
    assert [item.fingerprint for item in parsed.faults] == [fingerprint]
    assert parsed.legacy_fingerprints == parsed.retired_fingerprints == ()
    # A "value" marker keeps its meaning outside device error lists.
    other = resource(path, [{**values[0], "value": "valveTight"}])
    assert len(parse_fault_resource(other, observed_at=NOW).faults) == 1


# ------------------------------------------------------- the local path (B3)


def test_the_local_list_shows_only_active_two_state_errors() -> None:
    def entry(kind, value, allowed):
        return DeviceFault(kind, Decimal(1), value, tuple(allowed))

    observation = SimpleNamespace(
        request=request_for_path("/devices/device4/errors"),
        data=DeviceFaultList(
            (
                entry(SENSOR, "active", ("inactive", "active")),
                entry("eTRVValveStatus", "active", ("active", "valveTight")),
                entry("lowBattery", "active", ()),
                entry("noise", "inactive", ("inactive", "active", "x")),
            )
        ),
    )
    _, entries = local_device_errors(observation)
    assert entries == (
        DeviceError(4, SENSOR, "active"),
        DeviceError(4, "eTRVValveStatus", "other"),
        DeviceError(4, "lowBattery", "other"),
        DeviceError(4, "noise", "other"),
    )


# ------------------------------------------------------------ the lifecycle


def test_a_device_error_appears_once_and_ends_after_two_reads(hass) -> None:
    tracker = FaultTracker(hass, "entry", "gateway")
    read(
        tracker,
        resource("/notifications", []),
        resource(DEVICE1, rows(sensor="inactive")),
    )
    assert tracker.has_known_state and not tracker.active_faults

    events = read(tracker, resource("/notifications", []), resource(DEVICE1, rows()))
    assert kinds(events) == [("appeared", SENSOR)]
    events = read(tracker, resource("/notifications", []), resource(DEVICE1, rows()))
    assert events == ()

    quiet = (resource("/notifications", []), resource(DEVICE1, rows(sensor="inactive")))
    assert read(tracker, *quiet) == ()
    assert kinds(read(tracker, *quiet)) == [("resolved", SENSOR)]
    assert not tracker.active


def test_two_devices_with_the_same_error_are_announced_and_end_apart(hass):
    tracker = FaultTracker(hass, "entry", "gateway")
    read(
        tracker,
        resource(DEVICE1, rows(sensor="inactive")),
        resource(DEVICE2, rows(sensor="inactive")),
    )
    events = read(tracker, resource(DEVICE1, rows()), resource(DEVICE2, rows()))
    assert sorted(event.fault.component_id for event in events) == [
        "device1",
        "device2",
    ]
    assert len(tracker.active_faults) == 2

    for _ in range(2):
        events = read(
            tracker,
            resource(DEVICE1, rows(sensor="inactive")),
            resource(DEVICE2, rows()),
        )
    assert [event.fault.component_id for event in events] == ["device1"]
    assert [item.component_id for item in tracker.active_faults] == ["device2"]


# ------------------------------------------------------- stored incidents


def _old_fault(fingerprint: str, code: str, first_seen: datetime) -> dict:
    """One fault as 0.9.0-alpha.3 stored it."""
    return {
        "fingerprint": fingerprint,
        "code": code,
        "subcode": None,
        "severity": "unknown",
        "pointt_class": None,
        "component_type": "device",
        "occurred_at": None,
        "first_seen_at": first_seen.isoformat(),
        "last_seen_at": first_seen.isoformat(),
        "time_source": "home_assistant_observed",
    }


def _seed(hass_storage, entry_id: str, data: dict) -> None:
    key = fault_store_key(entry_id, "gateway-one")
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": data}


def _old_store(*faults: dict, evidence: bool = True) -> dict:
    data: dict = {"active": list(faults)}
    if evidence:
        data["required_source_keys"] = [hashlib.sha256(DEVICE1.encode()).hexdigest()]
        data["unverified_faults"] = []
    return data


SEEN = NOW - timedelta(days=3)


@pytest.mark.parametrize("evidence", [True, False])
async def test_stored_device_incidents_keep_their_identity_after_the_update(
    hass, hass_storage, evidence
) -> None:
    """The active error keeps its incident; the valve status ends normally."""
    _seed(
        hass_storage,
        "entry",
        _old_store(
            _old_fault(OLD_SENSOR, "active", SEEN),
            _old_fault(OLD_VALVE, "valveTight", SEEN),
            evidence=evidence,
        ),
    )
    tracker = FaultTracker(hass, "entry", "gateway-one")
    await tracker.async_load()
    before = {item.fingerprint: _incident_key(item) for item in tracker.active}
    events: list = []
    tracker.async_add_listener(events.append)

    for minute in range(3):
        read(tracker, resource(DEVICE1, rows()), when=NOW + timedelta(minutes=minute))
    # Nothing new: only the valve status, never a fault, has ended.
    assert kinds(events) == [("resolved", "valveTight")]
    (fault,) = tracker.active
    assert fault.fingerprint == OLD_SENSOR
    assert fault.code == SENSOR  # the name is corrected
    assert fault.first_seen_at == SEEN
    assert _incident_key(fault) == before[OLD_SENSOR]  # the bell stays the same
    assert tracker.current_state_confirmed

    # A restart keeps both the identity and the corrected name.
    await tracker.async_flush()
    stored = hass_storage[fault_store_key("entry", "gateway-one")]["data"]
    assert [(item["fingerprint"], item["code"]) for item in stored["active"]] == [
        (OLD_SENSOR, SENSOR)
    ]
    restarted = FaultTracker(hass, "entry", "gateway-one")
    await restarted.async_load()
    later: list = []
    restarted.async_add_listener(later.append)
    read(restarted, resource(DEVICE1, rows()), when=NOW + timedelta(hours=1))
    assert later == []
    assert [item.fingerprint for item in restarted.active] == [OLD_SENSOR]

    # It ends once, and a recurrence is a new incident with the new identity.
    for minute in range(2):
        read(
            restarted,
            resource(DEVICE1, rows(sensor="inactive")),
            when=NOW + timedelta(hours=2, minutes=minute),
        )
    assert kinds(later) == [("resolved", SENSOR)]
    read(restarted, resource(DEVICE1, rows()), when=NOW + timedelta(hours=3))
    assert kinds(later)[-1] == ("appeared", SENSOR)
    assert restarted.active[0].fingerprint not in (OLD_SENSOR, OLD_VALVE)


async def test_a_shared_stored_incident_goes_to_one_device_only(
    hass, hass_storage
) -> None:
    """Two devices shared one fingerprint before; the second is its own fault."""
    _seed(hass_storage, "entry", _old_store(_old_fault(OLD_SENSOR, "active", SEEN)))
    tracker = FaultTracker(hass, "entry", "gateway-one")
    await tracker.async_load()

    events = read(tracker, resource(DEVICE1, rows()), resource(DEVICE2, rows()))
    assert [(e.event_type, e.fault.component_id) for e in events] == [
        (FaultEventType.APPEARED, "device2")
    ]
    by_device = {item.component_id: item for item in tracker.active}
    assert by_device["device1"].fingerprint == OLD_SENSOR
    assert by_device["device1"].first_seen_at == SEEN
    # Stable over further reads: no flip between the devices.
    assert read(tracker, resource(DEVICE2, rows()), resource(DEVICE1, rows())) == ()
    assert {item.component_id: item.fingerprint for item in tracker.active} == {
        "device1": OLD_SENSOR,
        "device2": by_device["device2"].fingerprint,
    }


@pytest.mark.parametrize("notification", [True, False])
async def test_a_stored_incident_of_another_source_is_never_taken(
    hass, hass_storage, notification
) -> None:
    """A notification with occurrence ID 10 had the device rows' old identity."""
    _seed(
        hass_storage,
        "entry",
        {
            "active": [
                {
                    **_old_fault(OLD_SENSOR, "6249", SEEN),
                    "severity": "fault",
                    "component_type": "system",
                }
            ],
            "required_source_keys": [],
        },
    )
    tracker = FaultTracker(hass, "entry", "gateway-one")
    await tracker.async_load()
    sources = [resource(DEVICE1, rows())]
    if notification:
        sources.append(
            resource("/notifications", [{"ccd": 6249, "fc": "12", "occurrenceId": 10}])
        )
    events = read(tracker, *sources)
    assert kinds(events) == [("appeared", SENSOR)]
    codes = {item.fingerprint: item.code for item in tracker.active}
    assert codes[OLD_SENSOR] == "6249"
    assert len(codes) == 2


async def test_a_dismissed_device_notification_stays_dismissed(
    hass, hass_storage
) -> None:
    """A real cycle with the bell: the update shows no notification again."""
    await hass.config.async_set_time_zone("UTC")
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_GATEWAY_IDS: ["gateway-one"]})
    entry.add_to_hass(hass)
    old = _old_fault(OLD_SENSOR, "active", SEEN)
    _seed(hass_storage, entry.entry_id, _old_store(old))
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one"), entry
    )
    await coordinator.faults.async_load()
    (restored,) = coordinator.faults.active
    key = notification_id(entry.entry_id, "gateway-one")
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"dismissed": {_incident_key(restored): 0}, "reset": 0},
    }
    shown: dict = {}

    @callback
    def record(kind, notifications):
        if kind is not pn.UpdateType.REMOVED:
            shown.update(notifications)

    remove = pn.async_register_callback(hass, record)
    manager = FaultNotifications(hass, entry, coordinator)
    try:
        await manager.async_start()
        for minute in range(3):
            read(
                coordinator.faults,
                resource(DEVICE1, rows()),
                when=NOW + timedelta(minutes=minute),
            )
        await hass.async_block_till_done()
        assert shown == {}
        # A new error on another device is shown as usual.
        read(
            coordinator.faults,
            resource(DEVICE1, rows()),
            resource(DEVICE2, rows()),
            when=NOW + timedelta(minutes=5),
        )
        await hass.async_block_till_done()
        assert SENSOR in shown[key]["message"]
    finally:
        await manager.async_close()
        await coordinator.async_shutdown()
        remove()
