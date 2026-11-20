"""A local iCom access with real cycles against a simulated gateway.

Real HTTP, real encryption, the real runtime and scheduler; only the time
scale is shortened (periods, pauses, timeouts) so the cycles run in seconds.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from custom_components.bosch_buderus_heating import icom_client, icom_source
from custom_components.bosch_buderus_heating import managed_local as module
from custom_components.bosch_buderus_heating.connection_runtime import (
    ConnectionPhase,
    ConnectionRuntime,
)
from custom_components.bosch_buderus_heating.icom_access import icom_connection_data
from custom_components.bosch_buderus_heating.icom_plan import UUID_PATH
from custom_components.bosch_buderus_heating.local_polling.inventory import Freshness
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from custom_components.bosch_buderus_heating.observations import pair_for_path
from tests.helpers.icom_gateway import SimulatedIcomGateway
from tests.test_connection_runtime import Harness, entry_for, settle

GATEWAY_PASSWORD = "abcd-efgh-ijkl-mnop"
PRIVATE_PASSWORD = "private-secret"
PERIOD = 0.5
pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")


@pytest.fixture
async def gateway():
    simulated = SimulatedIcomGateway(
        gateway_password=GATEWAY_PASSWORD, private_password=PRIVATE_PASSWORD
    )
    await simulated.start()
    yield simulated
    await simulated.stop()


@pytest.fixture
def fast(monkeypatch):
    """The time scale of the cycles; the logic stays the real one."""
    monkeypatch.setattr(icom_client, "REQUEST_GAP", 0.0)
    monkeypatch.setattr(icom_client, "REQUEST_TIMEOUT", 0.4)
    monkeypatch.setattr(icom_source, "PAUSES", (0.4, 0.8, 1.6))
    monkeypatch.setattr(icom_source, "IDLE_MAX", 0.05)
    monkeypatch.setattr(icom_source, "RETRY_SECONDS", 0.2)
    monkeypatch.setattr(icom_source.IcomSource, "period", lambda self, request: PERIOD)


def build(
    hass, gateway, *, password=PRIVATE_PASSWORD, local_id="000123", fingerprint=None
):
    entry, cloud, target = entry_for(hass)
    data = dict(entry.data)
    data["connections"] = dict(data["connections"])
    data["connections"]["local"] = {
        "kind": "k40_local",
        "auth_epoch": 0,
        "config_revision": 1,
        **icom_connection_data(
            host="127.0.0.1",
            port=gateway.port,
            gateway_password=GATEWAY_PASSWORD,
            private_password=password,
            gateway_id="000123",
            local_id=local_id,
            verified_at=1.0,
            fingerprint=fingerprint,
        ),
    }
    hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
    harness = Harness()
    factory = module.LocalDriverFactory(hass, entry, lambda: runtime)
    runtime = ConnectionRuntime(
        hass,
        entry,
        {"singlekey_cloud": harness.factory, "k40_local": factory},
        sleep=harness.sleep,
    )
    return SimpleNamespace(entry=entry, runtime=runtime, cloud=cloud, target=target)


async def until(hass, predicate, *, seconds=8.0):
    async with asyncio.timeout(seconds):
        while not predicate():
            await settle(hass)
            await asyncio.sleep(0.02)


def state(p):
    return p.runtime.local_state("local")


def source(p):
    return p.runtime.connection_driver("local").sources["local"]


def first_pass_done(p):
    records = source(p)._records if p.runtime.connection_driver("local") else {}
    return bool(records) and all(r.attempted for r in records.values())


@pytest.fixture
async def running(hass, gateway, fast):
    p = build(hass, gateway)
    p.runtime.start()
    await until(hass, lambda: state(p) and state(p).current_observations)
    await until(hass, lambda: first_pass_done(p))
    yield p
    await p.runtime.close()


def conditions(p):
    return [item["condition"] for item in source(p).diagnostics()["condition_changes"]]


# ------------------------------------------------------------------ values


async def test_values_are_published_as_local_readings_of_the_pair_paths(
    hass, gateway, running
):
    p = running
    current = state(p)
    assert current.condition is LocalCondition.READY and current.identity_confirmed
    by_path = {item.request.path: item for item in current.current_observations}
    assert by_path["/system/sensors/temperatures/outdoor_t1"].data.value == 4.5
    assert by_path["/heatSources/flameStatus"].data.value == "on"
    assert by_path["/dhwCircuits/dhw1/actualTemp"].data.unit == "C"
    # The same context as the source state: no retagged late answer.
    assert all(item.context == current.context for item in current.last_observations)
    assert p.runtime.connection_phase("local") is ConnectionPhase.READY
    # A cloud-independent view: the cloud harness never published anything.
    assert state(p).problem is None


async def test_only_the_identity_the_directories_and_pair_paths_are_read(
    hass, gateway, running
):
    allowed = {UUID_PATH, "/heatingCircuits", "/dhwCircuits", "/heatSources"}
    for path in set(gateway.requests) - allowed:
        assert pair_for_path(path) is not None, path
    assert not any(
        "holiday" in path or "switchPrograms" in path for path in gateway.requests
    )
    assert gateway.requests[0] == UUID_PATH
    # One request at a time, ever.
    assert gateway.max_concurrent == 1
    assert {h["User-Agent"] for h in gateway.headers_seen} == {icom_client.USER_AGENT}


async def test_circuits_come_from_the_directories_of_the_gateway(hass, gateway, fast):
    gateway.drop("/heatingCircuits")
    p = build(hass, gateway)
    p.runtime.start()
    try:
        await until(hass, lambda: state(p) and state(p).current_observations)
        await until(hass, lambda: len(source(p).diagnostics()["resources"]) > 20)
        assert not any(
            path.startswith("/heatingCircuits/") for path in gateway.requests
        )
        assert source(p).diagnostics()["circuits"]["hc"] == 0
        assert any(path.startswith("/dhwCircuits/dhw1/") for path in gateway.requests)
    finally:
        await p.runtime.close()


async def test_new_values_replace_old_ones_in_the_next_period(hass, gateway, running):
    p = running
    gateway.value("/system/sensors/temperatures/outdoor_t1", -7.5)

    def current():
        item = {o.request.path: o for o in state(p).current_observations}
        return item.get("/system/sensors/temperatures/outdoor_t1")

    await until(hass, lambda: current() is not None and current().data.value == -7.5)


async def test_paths_the_gateway_does_not_offer_are_asked_once_in_a_week(
    hass, gateway, running
):
    p = running
    await until(hass, lambda: len(source(p).diagnostics()["resources"]) > 35)
    missing = [item for item in source(p).diagnostics()["resources"] if item["absent"]]
    assert missing, "the fixture does not offer every pair path"
    await asyncio.sleep(3 * PERIOD)
    counts = [
        gateway.count(record.request.path)
        for record in source(p)._records.values()
        if record.absent
    ]
    assert counts and all(count == 1 for count in counts)
    assert all(
        record.due > icom_source.monotonic() + 6 * 86400
        for record in source(p)._records.values()
        if record.absent
    )


async def test_an_unusable_document_is_a_problem_of_its_path_only(hass, gateway, fast):
    gateway.documents["/heatSources/systemPressure"] = {
        "id": "/heatSources/systemPressure",
        "type": "floatValue",
        "value": "not a number",
        "writeable": 0,
        "unitOfMeasure": "bar",
    }
    p = build(hass, gateway)
    p.runtime.start()
    try:
        await until(hass, lambda: state(p) and state(p).current_observations)
        await until(hass, lambda: len(source(p).diagnostics()["resources"]) > 30)
        item = next(
            r
            for r in source(p).diagnostics()["resources"]
            if r["path"] == "/heatSources/systemPressure"
        )
        assert item["problem"] == "local_decode_failed" and not item["available"]
        paths = {o.request.path for o in state(p).current_observations}
        assert "/heatSources/systemPressure" not in paths
        assert "/system/sensors/temperatures/outdoor_t1" in paths
        assert state(p).condition is LocalCondition.READY
    finally:
        await p.runtime.close()


# --------------------------------------------------------------- identity


async def test_another_gateway_stops_the_access_before_any_value_is_read(
    hass, gateway, fast
):
    gateway.documents[UUID_PATH]["value"] = "999999"
    p = build(hass, gateway)
    p.runtime.start()
    try:
        await until(
            hass,
            lambda: state(p) and state(p).condition is LocalCondition.UNAVAILABLE,
        )
        assert state(p).problem == "local_identity_conflict"
        assert not state(p).identity_confirmed and not state(p).current_observations
        await asyncio.sleep(1.0)
        assert gateway.requests == [UUID_PATH]
    finally:
        await p.runtime.close()


async def test_a_gateway_exchanged_later_is_noticed_after_a_pause(
    hass, gateway, running
):
    p = running
    gateway.documents[UUID_PATH]["value"] = "424242"
    gateway.hang_next = 2  # two lost requests pause the access
    await until(hass, lambda: source(p).pause_reason == "connection")
    await until(
        hass,
        lambda: (
            state(p).condition is LocalCondition.UNAVAILABLE
            and state(p).problem == "local_identity_conflict"
        ),
    )
    assert not state(p).current_observations
    requests = len(gateway.requests)
    await asyncio.sleep(1.0)
    assert len(gateway.requests) == requests  # stopped for good


# ------------------------------------------------ identity by a fingerprint

HARDWARE = "/gateway/versionHardware"


async def test_a_gateway_without_a_usable_id_is_recognised_by_its_fingerprint(
    hass, gateway, fast
):
    gateway.value(UUID_PATH, "-1")
    p = build(hass, gateway, fingerprint=(HARDWARE, "fixture-hw"))
    p.runtime.start()
    try:
        await until(hass, lambda: state(p) and state(p).current_observations)
        assert state(p).condition is LocalCondition.READY
        assert state(p).identity_confirmed
        assert gateway.requests[0] == HARDWARE and UUID_PATH not in gateway.requests
        assert source(p).diagnostics()["identity_method"] == "values"
        assert (await source(p).test_connection())[0] == "ok"
    finally:
        await p.runtime.close()


async def test_a_changed_fingerprint_stops_the_access_before_any_value(
    hass, gateway, fast
):
    gateway.value(HARDWARE, "another-model")
    p = build(hass, gateway, fingerprint=(HARDWARE, "fixture-hw"))
    p.runtime.start()
    try:
        await until(
            hass,
            lambda: state(p) and state(p).condition is LocalCondition.UNAVAILABLE,
        )
        assert state(p).problem == "local_identity_conflict"
        assert not state(p).identity_confirmed and not state(p).current_observations
        await asyncio.sleep(1.0)
        assert gateway.requests == [HARDWARE]
        assert (await source(p).test_connection())[0] == "other_gateway"
    finally:
        await p.runtime.close()


async def test_the_own_id_is_the_method_unless_a_fingerprint_was_stored(
    hass, gateway, running
):
    assert source(running).diagnostics()["identity_method"] == "uuid"


# --------------------------------------------------------------- password


async def test_a_wrong_password_stops_the_access_without_further_requests(
    hass, gateway, fast
):
    p = build(hass, gateway, password="wrong")
    p.runtime.start()
    try:
        await until(
            hass,
            lambda: state(p) and state(p).condition is LocalCondition.AUTH_REQUIRED,
        )
        assert state(p).problem == "auth_required"
        assert p.runtime.connection_phase("local") is ConnectionPhase.READY
        await asyncio.sleep(1.2)
        # The second undecryptable answer ends it: no value is ever read.
        assert gateway.requests == [UUID_PATH, UUID_PATH]
        assert not state(p).current_observations
    finally:
        await p.runtime.close()


async def test_a_password_changed_later_stops_the_access(hass, gateway, running):
    p = running
    from custom_components.bosch_buderus_heating.icom_crypto import derive_key

    gateway.key = derive_key(GATEWAY_PASSWORD, "changed-meanwhile")
    await until(
        hass, lambda: state(p).condition is LocalCondition.AUTH_REQUIRED, seconds=15
    )
    assert state(p).problem == "auth_required"
    assert not state(p).current_observations
    count = len(gateway.requests)
    await asyncio.sleep(1.0)
    assert len(gateway.requests) == count


async def test_one_noisy_answer_does_not_stop_the_access(hass, gateway, running):
    p = running
    gateway.noise_next = 1
    await until(hass, lambda: gateway.noise_next == 0)
    await asyncio.sleep(1.5)
    assert state(p).condition is LocalCondition.READY
    assert state(p).problem is None


async def test_two_noisy_answers_in_a_row_are_a_wrong_password(hass, gateway, running):
    p = running
    gateway.noise_next = 2
    await until(
        hass, lambda: state(p).condition is LocalCondition.AUTH_REQUIRED, seconds=15
    )
    assert state(p).problem == "auth_required"


# ----------------------------------------------------------- hangs, pauses


async def test_a_single_lost_request_does_not_pause_the_access(hass, gateway, running):
    p = running
    gateway.hang_next = 1
    await until(hass, lambda: gateway.hang_next == 0)
    await asyncio.sleep(1.5)
    assert source(p).diagnostics()["pauses"]["total"] == 0
    assert state(p).condition is LocalCondition.READY


async def test_two_lost_requests_pause_everything_and_recovery_is_automatic(
    hass, gateway, running
):
    p = running
    gateway.hang = True
    await until(hass, lambda: source(p).pause_reason == "connection")
    assert state(p).condition is LocalCondition.PAUSED
    # The readings of a confirmed access stay current for the first pause.
    assert state(p).current_observations
    during = len(gateway.requests)
    await asyncio.sleep(0.3)
    assert len(gateway.requests) == during  # nothing is requested while paused
    gateway.hang = False
    await until(hass, lambda: state(p).condition is LocalCondition.READY)
    await until(hass, lambda: source(p).diagnostics()["pauses"]["in_a_row"] == 0)
    assert source(p).diagnostics()["pauses"]["total"] >= 1


async def test_a_longer_outage_withdraws_the_readings_and_pauses_grow(
    hass, gateway, running
):
    p = running
    gateway.hang = True
    await until(
        hass,
        lambda: source(p).diagnostics()["pauses"]["in_a_row"] >= 2,
        seconds=15,
    )
    assert state(p).condition is LocalCondition.UNAVAILABLE
    assert not state(p).current_observations
    assert state(p).last_observations  # kept for the status, not for supply
    gateway.hang = False
    await until(hass, lambda: state(p).condition is LocalCondition.READY, seconds=20)
    assert "paused" in conditions(p) and "unavailable" in conditions(p)


async def test_the_pause_after_an_overload_answer_keeps_the_retry_after(
    hass, gateway, running
):
    p = running
    gateway.status, gateway.retry_after = 503, "2"
    await until(hass, lambda: source(p).pause_reason == "rate_limited")
    assert source(p).pause_remaining() >= 1  # more than the first pause (0.4 s)
    gateway.status = None
    await until(hass, lambda: state(p).condition is LocalCondition.READY, seconds=10)


async def test_server_errors_count_like_lost_requests(hass, gateway, running):
    p = running
    gateway.status = 500
    await until(hass, lambda: source(p).pause_reason == "connection")
    gateway.status = None
    await until(hass, lambda: state(p).condition is LocalCondition.READY, seconds=10)


async def test_an_unreachable_gateway_starts_with_pauses_and_connects_later(
    hass, gateway, fast
):
    await gateway.stop()
    p = build(hass, gateway)
    p.runtime.start()
    try:
        await until(
            hass, lambda: state(p) and state(p).condition is LocalCondition.UNAVAILABLE
        )
        assert not state(p).identity_confirmed
        await gateway.start()
        # The port changed with the restart: the stored access still points to
        # the old one, so the access stays unavailable (and keeps pausing).
        await asyncio.sleep(1.0)
        assert state(p).condition is LocalCondition.UNAVAILABLE
        assert source(p).diagnostics()["pauses"]["total"] >= 1
    finally:
        await p.runtime.close()


async def test_readings_expire_when_no_answer_comes(hass, gateway, running):
    p = running
    await until(hass, lambda: len(source(p).diagnostics()["resources"]) > 30)
    gateway.status = 503
    gateway.retry_after = "30"
    await until(hass, lambda: not state(p).current_observations, seconds=12)
    assert state(p).last_observations
    assert all(
        view.freshness in (Freshness.EXPIRED, Freshness.UNCONFIRMED)
        or not view.available
        for view in state(p).resources
    )


# ----------------------------------------------------- interface and health


async def test_diagnostics_have_counts_and_codes_but_no_secret_or_identifier(
    hass, gateway, running
):
    p = running
    await until(hass, lambda: len(source(p).diagnostics()["resources"]) > 30)
    text = json.dumps(source(p).diagnostics(), default=str)
    for secret in (
        GATEWAY_PASSWORD,
        GATEWAY_PASSWORD.replace("-", ""),
        PRIVATE_PASSWORD,
        "000123",
        "127.0.0.1",
        str(gateway.port),
    ):
        assert secret not in text
    data = source(p).diagnostics()
    assert data["protocol"] == "icom" and data["identity_confirmed"] is True
    metrics = data["request_metrics"]
    assert metrics["requests"] >= metrics["answered"] > 10
    assert metrics["requests"] > 30
    assert data["circuits"] == {"dhw": 1, "hc": 1, "hs": 1}
    assert all(
        "{" in item["path"] or item["path"].startswith("/")
        for item in data["resources"]
    )


async def test_refresh_reads_known_paths_again_soon(
    hass, gateway, running, monkeypatch
):
    p = running
    await until(hass, lambda: len(source(p).diagnostics()["resources"]) > 30)
    monkeypatch.setattr(icom_source.IcomSource, "period", lambda self, request: 3600)
    await asyncio.sleep(1.0)
    path = "/heatSources/flameStatus"
    before = gateway.count(path)
    assert source(p).refresh((path, "/not/a/known/path")) == 1
    await until(hass, lambda: gateway.count(path) > before)


async def test_refresh_does_nothing_while_the_access_is_paused(hass, gateway, running):
    p = running
    gateway.hang = True
    await until(hass, lambda: source(p).pause_reason == "connection")
    assert source(p).refresh(("/heatSources/flameStatus",)) == 0


async def test_the_connection_test_reads_the_identity_once(hass, gateway, running):
    p = running
    before = gateway.count(UUID_PATH)
    result, millis = await source(p).test_connection()
    assert result == "ok" and millis >= 0
    assert gateway.count(UUID_PATH) == before + 1
    gateway.documents[UUID_PATH]["value"] = "777777"
    assert (await source(p).test_connection())[0] == "other_gateway"
    gateway.status = 503
    assert (await source(p).test_connection())[0] == "paused"


async def test_closing_ends_the_requests_and_the_session(hass, gateway, running):
    p = running
    driver = source(p)
    await p.runtime.close()
    assert driver._session is None
    count = len(gateway.requests)
    await asyncio.sleep(0.8)
    assert len(gateway.requests) == count


# ------------------------------------------------------------- the pace


def bare_source(options):
    """A source with only what the pace rules read."""
    driver = object.__new__(icom_source.IcomSource)
    driver.connection = SimpleNamespace(identifier="local")
    driver.context = SimpleNamespace(
        runtime=SimpleNamespace(
            snapshot=SimpleNamespace(to_options=lambda: {"connections": options})
        )
    )
    return driver


@pytest.mark.parametrize(
    ("settings", "interval"),
    [
        ({}, 120),
        ({"local": {"enabled": True}}, 120),
        ({"local": {"local_interval": 60}}, 120),
        ({"local": {"local_interval": 120}}, 120),
        ({"local": {"local_interval": 300}}, 300),
        ({"local": {"local_interval": 900}}, 900),
        ({"local": {"local_interval": "300"}}, 120),
        ({"local": {"local_interval": True}}, 120),
    ],
)
def test_the_interval_is_never_below_two_minutes(settings, interval):
    assert bare_source(settings).interval() == interval


def test_every_reading_keeps_the_period_of_its_class_at_least_the_interval():
    from custom_components.bosch_buderus_heating.local_api.catalog import (
        request_for_path,
    )

    driver = bare_source({})
    periods = {
        path: driver.period(request_for_path(path))
        for path in (
            "/system/sensors/temperatures/outdoor_t1",
            "/heatingCircuits/hc1/maxFlowTemp",
            "/heatSources/numberOfStarts",
            "/gateway/versionHardware",
        )
    }
    assert periods == {
        "/system/sensors/temperatures/outdoor_t1": 120,
        "/heatingCircuits/hc1/maxFlowTemp": 300,
        "/heatSources/numberOfStarts": 900,
        "/gateway/versionHardware": 86400,
    }
    slower = bare_source({"local": {"local_interval": 600}})
    assert slower.period(request_for_path("/heatSources/flameStatus")) == 600


async def test_removing_the_cloud_connection_ends_the_polling_probe(
    hass, gateway, running
):
    p = running
    management = p.runtime.management
    management.remove(management.edit(p.cloud))
    await settle(hass)
    await asyncio.sleep(0.3)
    count = len(gateway.requests)
    await asyncio.sleep(2.0)
    assert len(gateway.requests) == count


# ------------------------------------------------ unusable ID, restart, session


@pytest.mark.parametrize("unusable", ["-1", "", None])
async def test_an_unusable_id_after_a_pause_is_no_other_gateway(
    hass, gateway, running, unusable
):
    p = running
    gateway.value(UUID_PATH, unusable)
    gateway.hang_next = 2  # two lost requests pause the access
    await until(hass, lambda: source(p).pause_reason == "identity_unavailable")
    assert state(p).problem != "local_identity_conflict"
    # The access keeps looking again, with growing pauses.
    first = gateway.count(UUID_PATH)
    await until(hass, lambda: gateway.count(UUID_PATH) >= first + 2, seconds=15)
    assert "local_identity_conflict" not in [
        item["reason"] for item in source(p).diagnostics()["condition_changes"]
    ]
    assert state(p).problem != "local_identity_conflict"
    gateway.value(UUID_PATH, "000123")
    await until(hass, lambda: state(p).condition is LocalCondition.READY, seconds=20)
    assert state(p).identity_confirmed and state(p).problem is None


@pytest.mark.parametrize("unusable", ["-1", ""])
async def test_an_unusable_id_at_the_start_waits_and_never_stops(
    hass, gateway, fast, unusable
):
    gateway.value(UUID_PATH, unusable)
    p = build(hass, gateway)
    p.runtime.start()
    try:
        await until(
            hass,
            lambda: (
                p.runtime.connection_driver("local") is not None
                and source(p).pause_reason == "identity_unavailable"
            ),
        )
        await until(hass, lambda: gateway.count(UUID_PATH) >= 3, seconds=15)
        assert state(p).problem != "local_identity_conflict"
        assert not state(p).current_observations
        assert (await source(p).test_connection())[0] != "ok"
        gateway.value(UUID_PATH, "000123")
        await until(
            hass, lambda: state(p).condition is LocalCondition.READY, seconds=20
        )
    finally:
        await p.runtime.close()


async def test_a_usable_different_id_still_stops_after_a_pause(hass, gateway, running):
    p = running
    gateway.value(UUID_PATH, "-1")
    gateway.hang_next = 2
    await until(hass, lambda: source(p).pause_reason == "identity_unavailable")
    gateway.value(UUID_PATH, "424242")
    await until(
        hass,
        lambda: state(p).problem == "local_identity_conflict",
        seconds=15,
    )


async def test_one_undecryptable_answer_at_the_start_is_looked_at_again(
    hass, gateway, fast
):
    # The stored access was verified before (after a restart): one noisy
    # first answer is no reason to ask for the passwords again.
    gateway.noise_next = 1
    p = build(hass, gateway)
    p.runtime.start()
    try:
        await until(hass, lambda: state(p) and state(p).current_observations)
        assert "auth_required" not in conditions(p)
        assert state(p).condition is LocalCondition.READY
        assert source(p).diagnostics()["pauses"]["total"] >= 1
    finally:
        await p.runtime.close()


async def test_the_session_is_closed_without_a_session_warning(
    hass, gateway, fast, caplog, monkeypatch
):
    created = []
    real = icom_source.async_create_clientsession

    def spy(*args, **kwargs):
        created.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(icom_source, "async_create_clientsession", spy)
    p = build(hass, gateway)
    p.runtime.start()
    await until(hass, lambda: state(p) and state(p).current_observations)
    await p.runtime.close()
    assert "closes the Home Assistant aiohttp session" not in caplog.text
    # Our own session, ended by us (detached), never by the entry's cleanup.
    assert created == [{"auto_cleanup": False}]
