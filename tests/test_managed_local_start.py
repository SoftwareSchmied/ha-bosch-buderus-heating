"""B01: a failing local start check is retried unless it needs the user.

Real start cycles of the managed local source (scheduler, probe assessment,
budget and repair note) with synthetic probe answers. The waits between two
checks are recorded and stepped through the repair's own retry.
"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.bosch_buderus_heating import managed_local as module
from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from custom_components.bosch_buderus_heating.const import (
    DOMAIN,
    LOCAL_START_ISSUE_PREFIX,
)
from custom_components.bosch_buderus_heating.local_probe import (
    ProbePath,
    TransportFailure,
)
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from custom_components.bosch_buderus_heating.local_transport import LocalProbeExchange
from custom_components.bosch_buderus_heating.repair_scope import repair_id
from custom_components.bosch_buderus_heating.repairs import (
    InvalidRepairFlow,
    LocalStartRepairFlow,
    async_create_fix_flow,
)
from tests.test_local_probe import response
from tests.test_managed_local import prepared, wait_until  # noqa: F401

INTEGRATION = Path(module.__file__).parent
KEYS = (
    "local_start_certificate",
    "local_start_incomplete",
    "local_start_unrecognized",
    "local_start_response",
)


def answer(context, gateway, kind):
    """Probe answers of one start check; ``kind`` names what goes wrong."""
    rows = []
    for path in ProbePath:
        row = response(path, context=context, gatewayId=gateway)
        if kind == "pending" and path is ProbePath.SYSTEM:
            body = json.loads(row.body)
            body["status"] = "pending"
            row = replace(row, body=json.dumps(body).encode())
        elif kind == "html" and path is ProbePath.BRAND:
            row = replace(row, body=b"<html>starting</html>")
        elif kind in ("http_error", "auth", "busy") and path is ProbePath.BRAND:
            status = {"http_error": 408, "auth": 401, "busy": 503}[kind]
            rows.append(replace(row, status=status, body=b""))
            break
        elif kind in ("untrusted", "changed") and path is ProbePath.BRAND:
            failure = (
                TransportFailure.TLS
                if kind == "untrusted"
                else TransportFailure.TLS_CHANGED
            )
            rows.append(replace(row, status=None, body=b"", failure=failure))
            break
        elif kind == "brand" and path is ProbePath.BRAND:
            row = response(path, context=context, value=1)
        elif kind == "unknown_brand" and path is ProbePath.BRAND:
            row = response(path, context=context, value="unknown")
        elif kind == "old_firmware" and path is ProbePath.FIRMWARE:
            row = response(path, context=context, value="14.00.01")
        elif kind == "brand_and_old" and path is not ProbePath.SYSTEM:
            row = response(
                path,
                context=context,
                value="" if path is ProbePath.BRAND else "14.00.01",
            )
        elif kind == "other_gateway" and path is ProbePath.SYSTEM:
            row = response(path, context=context, gatewayId="other-gateway")
        rows.append(row)
    return tuple(rows)


@pytest.fixture
def start(hass, prepared, monkeypatch):  # noqa: F811
    """Answers in turn from ``plan`` (then ``ok``) and the recorded waits."""
    p = prepared
    p.plan, p.waits, p.retry_after = [], [], None
    original = module.LocalSource._wait

    async def probe(context, token, pin, *, is_current, budget):
        p.probes.append(context)
        kind = p.plan.pop(0) if p.plan else "ok"
        return LocalProbeExchange(
            answer(context, p.control.gateway, kind),
            retry_after_seconds=p.retry_after,
        )

    async def wait(self, seconds):
        # The real wait, ended only by the repair's retry in these tests.
        p.waits.append(seconds)
        await original(self, 3600 * 24)

    monkeypatch.setattr(module, "read_local_probe", probe)
    monkeypatch.setattr(module.LocalSource, "_wait", wait)
    p.source = lambda: p.runtime.connection_driver("local").sources["local"]
    p.issue_id = repair_id(LOCAL_START_ISSUE_PREFIX, p.entry, "local", "local")
    p.issue = lambda: ir.async_get(hass).async_get_issue(DOMAIN, p.issue_id)
    return p


async def step(hass, p, waits):
    """Let the source reach its ``waits``-th wait between two checks."""
    await wait_until(hass, lambda: len(p.waits) >= waits)
    assert len(p.waits) == waits


async def running(hass, p):
    await wait_until(
        hass,
        lambda: (
            (state := p.runtime.local_state("local")) is not None
            and bool(state.current_observations)
        ),
    )


@pytest.mark.parametrize(
    "kind,problem",
    [
        ("pending", "system_information_incomplete"),
        ("html", "invalid_json"),
        ("http_error", "http_error"),
        ("untrusted", "certificate_untrusted"),
        ("brand", "invalid_response"),
    ],
)
async def test_a_transient_start_problem_is_retried_until_the_start_succeeds(
    hass, start, kind, problem
):
    p = start
    p.plan = [kind, kind]
    p.runtime.start()
    await step(hass, p, 1)
    state = p.runtime.local_state("local")
    assert state.condition is LocalCondition.UNAVAILABLE and state.problem == problem
    assert not p.clients
    assert p.source().retry_start()
    await step(hass, p, 2)
    p.source().retry_start()
    await running(hass, p)
    assert len(p.probes) == 3 and len(p.clients) == 1
    # The growing waits of the first two failed checks; no note before five.
    assert p.waits == [60.0, 120.0]
    assert p.issue() is None
    changes = [c["condition"] for c in p.source()._changes]
    assert changes.count("unavailable") == 2 and changes[-1] == "ready"


async def test_a_gateway_that_reports_no_known_brand_starts_at_once(hass, start):
    p = start
    p.plan = ["unknown_brand"]
    p.runtime.start()
    await running(hass, p)
    assert len(p.probes) == 1 and len(p.clients) == 1
    assert p.waits == [] and p.issue() is None


@pytest.mark.parametrize(
    "kind,condition,problem",
    [
        ("auth", LocalCondition.AUTH_REQUIRED, "auth_required"),
        ("changed", LocalCondition.CERTIFICATE_CHANGED, "certificate_changed"),
        ("old_firmware", LocalCondition.UNAVAILABLE, "firmware_update_required"),
        # An unrecognized brand does not hide too old firmware.
        ("brand_and_old", LocalCondition.UNAVAILABLE, "firmware_update_required"),
        ("other_gateway", LocalCondition.UNAVAILABLE, "local_identity_conflict"),
    ],
)
async def test_a_permanent_start_problem_stops_and_removes_the_retry_note(
    hass, start, kind, condition, problem
):
    p = start
    p.plan = ["pending"] * module.START_ISSUE_ATTEMPTS + [kind]
    p.runtime.start()
    for count in range(1, module.START_ISSUE_ATTEMPTS + 1):
        await step(hass, p, count)
        p.source().retry_start()
    await wait_until(
        hass,
        lambda: (
            (state := p.runtime.local_state("local")).condition is condition
            and state.problem == problem
        ),
    )
    assert not p.clients
    assert p.issue() is None
    # No further check, not even on request.
    p.source().retry_start()
    for _ in range(5):
        await asyncio.sleep(0.01)
    assert len(p.probes) == module.START_ISSUE_ATTEMPTS + 1
    assert len(p.waits) == module.START_ISSUE_ATTEMPTS
    assert p.runtime.local_state("local").condition is condition


async def test_the_repair_note_names_the_cause_after_five_checks_and_goes_on_success(
    hass, start, enable_custom_integrations, hass_client
):
    p = start
    p.plan = ["pending"] * 6
    p.runtime.start()
    for count in range(1, module.START_ISSUE_ATTEMPTS):
        await step(hass, p, count)
        assert p.issue() is None
        p.source().retry_start()
    await step(hass, p, module.START_ISSUE_ATTEMPTS)
    assert p.waits == [60.0, 120.0, 300.0, 600.0, 1200.0]
    issue = p.issue()
    assert issue is not None and issue.is_fixable
    assert issue.translation_key == "local_start_incomplete"
    assert issue.translation_placeholders == {"code": "system_information_incomplete"}
    # Scoped like every managed repair, so the orphan cleanup finds it.
    assert issue.data == {
        "entry_id": p.entry.entry_id,
        "connection_id": "local",
        "binding_id": "local",
        "problem": "system_information_incomplete",
    }

    # The repair, opened like the repair dialog does, asks first and then
    # tries again at once; Home Assistant removes the note when the flow
    # ends, and the next failed check brings it back.
    p.entry.runtime_data = SimpleNamespace(connections=p.runtime)
    hass.config.components.add(DOMAIN)
    assert await async_setup_component(hass, "repairs", {})
    notes = []

    @callback
    def note_changed(event):
        if event.data["issue_id"] == p.issue_id:
            notes.append(event.data["action"])

    hass.bus.async_listen(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED, note_changed)
    client = await hass_client()
    response = await client.post(
        "/api/repairs/issues/fix", json={"handler": DOMAIN, "issue_id": p.issue_id}
    )
    assert response.status == 200
    form = await response.json()
    assert form["type"] == "form" and form["step_id"] == "confirm"
    assert form["description_placeholders"] == {"code": "system_information_incomplete"}
    assert len(p.probes) == module.START_ISSUE_ATTEMPTS
    response = await client.post(f"/api/repairs/issues/fix/{form['flow_id']}")
    assert response.status == 200
    assert (await response.json())["type"] == "create_entry"
    await step(hass, p, 6)
    await hass.async_block_till_done()
    assert p.waits[-1] == 2400.0
    assert notes == ["remove", "create"]
    assert p.issue() is not None

    # The gateway is ready: the start succeeds and the note goes.
    p.source().retry_start()
    await running(hass, p)
    assert p.issue() is None
    assert len(p.probes) == 7


async def test_waits_respect_retry_after_and_the_gateway_pause(hass, start):
    p = start
    # A longer Retry-After is kept for an unusable answer ...
    p.plan, p.retry_after = ["pending"], 5000.0
    p.runtime.start()
    await step(hass, p, 1)
    assert p.waits == [5000.0]
    # ... and an unreachable gateway pauses every reader for it; a requested
    # retry does not end that pause.
    p.plan = ["busy"]
    p.source().retry_start()
    await wait_until(
        hass,
        lambda: p.runtime.local_state("local").condition is LocalCondition.PAUSED,
    )
    source = p.source()
    assert len(p.probes) == 2
    assert source.retry_start()
    for _ in range(5):
        await asyncio.sleep(0.01)
    assert len(p.probes) == 2 and source is p.source()
    assert p.runtime.local_state("local").condition is LocalCondition.PAUSED


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        (0.0, [60.0, 120.0, 300.0, 600.0, 600.0]),
        (1.0, [60.0, 120.0, 300.0, 600.0, 600.0]),
        (None, [60.0, 120.0, 300.0, 600.0, 600.0]),
        # A longer Retry-After is kept in full.
        (5000.0, [5000.0] * 5),
    ],
)
async def test_a_short_retry_after_still_lets_the_start_pause_grow(
    hass, start, monkeypatch, retry_after, expected
):
    """TLS-2: an overloaded gateway answering 503 with Retry-After 0 or 1 is
    not probed again every second; the pause grows as without the header."""
    from custom_components.bosch_buderus_heating import local_budget

    p = start
    pauses: list[tuple[float, str]] = []

    async def pause(self, seconds, reason="failure"):
        pauses.append((seconds, reason))  # recorded; the next check runs at once

    monkeypatch.setattr(local_budget.LocalReadBudget, "pause", pause)
    p.plan, p.retry_after = ["busy"] * 5, retry_after
    p.runtime.start()
    await running(hass, p)
    assert [seconds for seconds, _ in pauses] == expected
    assert len(p.probes) == 6


async def test_removing_the_access_removes_its_retry_note(hass, start):
    p = start
    p.plan = ["html"] * module.START_ISSUE_ATTEMPTS
    p.runtime.start()
    for count in range(1, module.START_ISSUE_ATTEMPTS):
        await step(hass, p, count)
        p.source().retry_start()
    await step(hass, p, module.START_ISSUE_ATTEMPTS)
    assert p.issue().translation_key == "local_start_response"
    management = ConfigurationManagement(hass, p.entry)
    management.remove(management.edit("local"))
    await wait_until(hass, lambda: p.issue() is None)


async def test_the_retry_repair_refuses_stale_or_invalid_notes(hass, start):
    p = start
    data = {
        "entry_id": p.entry.entry_id,
        "connection_id": "local",
        "binding_id": "local",
        "problem": "invalid_json",
    }
    for changed in (
        {"problem": None},
        {"binding_id": None},
        {"connection_id": "other"},
    ):
        flow = await async_create_fix_flow(hass, p.issue_id, {**data, **changed})
        assert isinstance(flow, InvalidRepairFlow)
    # The source does not run (no runtime, or it was replaced meanwhile).
    flow = await async_create_fix_flow(hass, p.issue_id, data)
    flow.hass = hass
    assert (await flow.async_step_init())["reason"] == "configuration_changed"
    p.entry.runtime_data = SimpleNamespace(connections=p.runtime)
    p.plan = ["html"]
    p.runtime.start()
    await step(hass, p, 1)
    form = await flow.async_step_init()
    assert form["type"] is FlowResultType.FORM
    source = p.source()
    source._closed = True
    assert (await flow.async_step_confirm({}))["reason"] == "configuration_changed"
    source._closed = False
    flow = LocalStartRepairFlow("removed-entry", "local", "invalid_json")
    flow.hass = hass
    assert (await flow.async_step_init())["reason"] == "entry_not_found"


def test_each_retry_note_is_translated() -> None:
    texts = {}
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        texts[name] = json.loads((INTEGRATION / name).read_text(encoding="utf-8"))[
            "issues"
        ]
    assert texts["strings.json"] == texts["translations/en.json"]
    for name, issues in texts.items():
        for key in KEYS:
            flow = issues[key]["fix_flow"]
            assert issues[key]["title"], (name, key)
            assert "{code}" in flow["step"]["confirm"]["description"], (name, key)
            assert set(flow["abort"]) == {"entry_not_found", "configuration_changed"}
    for problem, key in module._START_ISSUE_KEYS.items():
        assert key in KEYS, problem


async def test_the_wait_between_two_checks_ends_on_time_or_on_request() -> None:
    holder = SimpleNamespace(_retry=asyncio.Event())
    async with asyncio.timeout(1):
        await module.LocalSource._wait(holder, 0.01)
    holder._retry.set()
    async with asyncio.timeout(1):
        await module.LocalSource._wait(holder, 3600)
