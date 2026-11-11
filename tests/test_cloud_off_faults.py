"""A1/A2: faults and bell notifications of a plant shown without its cloud.

The managed entry runs with its real platforms and the real PointT client
against a synthetic cloud that reports faults. Switching the cloud access off
while running keeps the running view for the local readings; from then on its
local error list decides the faults (as in the dormant view, 5A), and the bell
notification follows it. Switching the access on again lets the cloud continue
the same baseline without announcing a fault twice.
"""

import asyncio
import functools
import itertools
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components import persistent_notification as pn
from homeassistant.core import callback

from custom_components.bosch_buderus_heating.const import (
    CONF_FAULT_NOTIFICATIONS,
    DOMAIN,
)
from custom_components.bosch_buderus_heating.faults import (
    FaultEventType,
    FaultTracker,
)
from custom_components.bosch_buderus_heating.gateway_identity import GatewayNamespace
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from custom_components.bosch_buderus_heating.notifications import (
    gateway_notification_key,
    notification_id,
)
from tests.test_connection_runtime import settle
from tests.test_local_backed import GATEWAY, ReadingBackend, local_values, started
from tests.test_local_faults import ITEM
from tests.test_local_only_mode import cloud_off, first_run
from tests.test_local_plant_mode import entity_state, readings, retained
from tests.test_managed_local_plant import fault_view
from tests.test_managed_pointt import eventually, running, source_for

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
FAULTS = f"{GATEWAY}:active_faults"
PROBLEM = f"{GATEWAY}:system_fault"
CLOUD_ONLY = {**ITEM, "ccd": 1000, "dcd": "A11"}
NEW = {**ITEM, "ccd": 522, "dcd": "4P"}
LATER = {**ITEM, "ccd": 523, "dcd": "4R"}
_LISTS = itertools.count(1000.0)


def bounded(test):
    """Fail within 90 s instead of hanging."""

    @functools.wraps(test)
    async def run(*args, **kwargs):
        async with asyncio.timeout(90):
            await test(*args, **kwargs)

    return run


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    hass.config.components.add(DOMAIN)


@pytest.fixture
def notices(hass):
    current = {}

    @callback
    def capture(kind, notifications):
        for key, value in notifications.items():
            if kind is pn.UpdateType.REMOVED:
                current.pop(key, None)
            else:
                current[key] = value

    remove = pn.async_register_callback(hass, capture)
    yield current
    remove()


@pytest.fixture
def emitted():
    """Every lifecycle event of every tracker, in order."""
    events = []
    original = FaultTracker._emit

    def emit(self, event):
        events.append((self, event.event_type, event.fault.fingerprint))
        original(self, event)

    with patch.object(FaultTracker, "_emit", emit):
        yield events


class FaultBackend(ReadingBackend):
    """A cloud that reports the plant's error list."""

    def __init__(self):
        super().__init__()
        self.faults = [ITEM, CLOUD_ONLY]

    def resource(self, gateway, path):
        if path == "/notifications":
            return {"id": path, "type": "errorList", "values": list(self.faults)}
        return super().resource(gateway, path)


def local_list(owner, *faults):
    """A ready local state with a new, complete error list (maybe empty)."""
    runtime = owner.connections
    context = runtime._contexts["local"]
    runtime._local_states["local"] = SimpleNamespace(
        context=context.identity_context(GatewayNamespace.K40_LOCAL),
        condition=LocalCondition.READY,
        current_observations=tuple(readings()),
        resources=(fault_view(*faults, monotonic=next(_LISTS)),),
    )
    runtime._notify()


def fingerprints(tracker):
    return {item.fingerprint for item in tracker.active}


def fingerprint_of(tracker, code):
    return next(item.fingerprint for item in tracker.active if item.code == code)


def appeared(events, fingerprint):
    return [
        tracker
        for tracker, kind, item in events
        if kind is FaultEventType.APPEARED and item == fingerprint
    ]


async def switched_off(hass, owner, cloud, *, start=True):
    """Cloud and local list report faults; then the access is switched off."""
    if start:
        await started(hass, owner)
    live = source_for(owner, GATEWAY)
    await eventually(lambda: len(live.faults.active) == 2)
    local_list(owner, ITEM)
    await eventually(lambda: live.faults.local_list_current)
    assert not live.faults.local_authority
    management = owner.connections.management
    management.set_enabled(management.edit(cloud), False)
    await eventually(lambda: live.local_retained)
    await settle(hass)
    return live


@bounded
async def test_switching_the_cloud_off_uses_the_local_error_list(hass) -> None:
    backend = FaultBackend()
    async with running(hass, backend, configure=local_values()) as (_, cloud, owner, _):
        held = await switched_off(hass, owner, cloud)
        assert retained(owner) is held
        assert held.faults.local_authority
        # Shown at once from the current local list, without a restart.
        await eventually(lambda: entity_state(hass, owner, "sensor", FAULTS) == "2")
        assert entity_state(hass, owner, "binary_sensor", PROBLEM) == "on"
        cloud_only = fingerprint_of(held.faults, "1000")
        # The local list no longer reports the fault it reported: it ends
        # after two new readings. The fault only the cloud knew stays.
        local_list(owner)
        await settle(hass)
        assert len(held.faults.active) == 2
        local_list(owner)
        await eventually(lambda: entity_state(hass, owner, "sensor", FAULTS) == "1")
        assert fingerprints(held.faults) == {cloud_only}
        assert not held.faults.current_state_confirmed  # pending, not confirmed
        # A new local fault appears; without a current list no state.
        local_list(owner, NEW)
        await eventually(lambda: entity_state(hass, owner, "sensor", FAULTS) == "2")
        owner.connections._local_states.pop("local")
        owner.connections._notify()
        await eventually(
            lambda: entity_state(hass, owner, "sensor", FAULTS) == "unavailable"
        )
        assert len(held.faults.active) == 2  # kept, only not confirmed
        calls = len(backend.calls)
        await settle(hass)
        assert len(backend.calls) == calls  # the cloud is not asked


@bounded
async def test_switching_the_cloud_on_continues_the_baseline_once(
    hass, emitted
) -> None:
    backend = FaultBackend()
    async with running(hass, backend, configure=local_values()) as (_, cloud, owner, _):
        held = await switched_off(hass, owner, cloud)
        start = len(emitted)
        local_list(owner, ITEM, NEW)  # a local fault while the cloud is off
        await eventually(lambda: len(held.faults.active) == 3)
        new = fingerprint_of(held.faults, "522")
        assert appeared(emitted[start:], new) == [held.faults]
        # The cloud starts again; its first answer waits.
        backend.faults = [ITEM, CLOUD_ONLY, NEW, LATER]
        backend.gates[GATEWAY] = asyncio.Event()
        backend.entered.pop(GATEWAY, None)
        management = owner.connections.management
        management.set_enabled(management.edit(cloud), True)
        await eventually(lambda: held.faults._handed_over)
        await eventually(lambda: GATEWAY in backend.entered)
        # A change in this window is processed by the starting cloud only:
        # the held view shows the handed-over faults unchanged.
        local_list(owner, ITEM, NEW, LATER)
        await settle(hass)
        assert len(held.faults.active) == 3
        backend.gates[GATEWAY].set()
        await eventually(
            lambda: (
                (item := source_for(owner, GATEWAY)) is not None and item is not held
            )
        )
        current = source_for(owner, GATEWAY)
        await eventually(lambda: len(current.faults.active) == 4)
        assert not current.faults.local_authority
        later = fingerprint_of(current.faults, "523")
        # The cloud continued the baseline: nothing appeared twice.
        assert appeared(emitted[start:], new) == [held.faults]
        assert appeared(emitted[start:], later) == [current.faults]
        assert not [
            item for item in emitted[start:] if item[1] is FaultEventType.RESOLVED
        ]
        await eventually(lambda: not held.local_retained)
        assert not owner.connections._retained


@bounded
async def test_a_view_kept_for_a_sign_in_loads_its_baseline_first(hass) -> None:
    """Review A1: a view started from the inventory because the sign-in
    failed never loaded the stored faults. Switched off, it loads them before
    the local list decides, so a fault only the cloud knew stays."""
    from unittest.mock import AsyncMock

    from custom_components.bosch_buderus_heating.connection_runtime import (
        ConnectionPhase,
    )
    from custom_components.bosch_buderus_heating.pointt.exceptions import (
        RefreshTokenRejected,
    )

    backend = FaultBackend()
    async with running(hass, backend, configure=local_values()) as (_, _, owner, _):
        await started(hass, owner)
        live = source_for(owner, GATEWAY)
        await eventually(lambda: len(live.faults.active) == 2)
        local_list(owner, ITEM)
        await eventually(lambda: live.faults.local_list_current)
    backend.status["gateways"] = 401  # restart with a rejected sign-in
    with patch(
        "custom_components.bosch_buderus_heating.pointt.auth.OAuthClient.refresh",
        AsyncMock(side_effect=RefreshTokenRejected()),
    ):
        async with running(hass, backend, configure=local_values()) as (
            entry,
            cloud,
            owner,
            _,
        ):
            await owner.start_platforms()
            runtime = owner.connections
            await eventually(
                lambda: runtime.connection_phase(cloud) is ConnectionPhase.AUTH_REQUIRED
            )
            await eventually(lambda: retained(owner) is not None)
            view = retained(owner)
            assert view.inventory_view and not view.faults.local_authority
            await eventually(lambda: "local" in runtime._contexts)
            local_list(owner, ITEM)
            await settle(hass)
            management = runtime.management
            management.set_enabled(management.edit(cloud), False)
            await eventually(lambda: view.faults.local_authority)
            # The bell follows the view at once, before the next local list.
            await eventually(
                lambda: owner.services.notifications[GATEWAY].coordinator is view
            )
            local_list(owner, ITEM)
            await eventually(lambda: entity_state(hass, owner, "sensor", FAULTS) == "2")
            assert len(view.faults.active) == 2
            assert not view.faults.current_state_confirmed  # 1000 pending
        stored = FaultTracker(hass, entry.entry_id, GATEWAY)
        await stored.async_load()
        assert len(stored.active) == 2  # the stored baseline is kept


@bounded
async def test_a_sign_in_keeps_the_cloud_decision(hass) -> None:
    """Decision A1: a cloud that needs a new sign-in is expected back, so its
    kept view never lets the local list end a fault (P42)."""
    backend = FaultBackend()
    async with running(hass, backend, configure=local_values()) as (_, _, owner, _):
        await started(hass, owner)
        live = source_for(owner, GATEWAY)
        await eventually(lambda: len(live.faults.active) == 2)
        local_list(owner, ITEM)
        live._request_reauthentication()
        await eventually(lambda: live.local_retained)
        await settle(hass)
        assert not live.faults.local_authority
        for _ in range(3):
            local_list(owner)
        await settle(hass)
        assert len(live.faults.active) == 2
        assert entity_state(hass, owner, "sensor", FAULTS) == "unavailable"
        assert owner.services.notifications[GATEWAY].coordinator is None


async def test_a_handed_over_view_holds_its_faults_for_a_bounded_time(hass) -> None:
    from datetime import timedelta

    from custom_components.bosch_buderus_heating.faults import HAND_OVER_HOLD
    from tests.test_local_faults import Steps

    steps = Steps(hass)
    steps.tracker.local_authority = True
    steps.local(ITEM)  # the first complete list: the baseline
    await steps.tracker.async_hand_over(object())
    assert steps.local(ITEM, NEW) == ()  # shown unchanged while handed over
    assert len(steps.tracker.active) == 1
    steps.time += HAND_OVER_HOLD + timedelta(seconds=1)
    # A hand-over that never ended does not stop the display for good.
    (event,) = steps.local(ITEM, NEW)
    assert event.event_type is FaultEventType.APPEARED
    assert steps.tracker._handed_over  # and it still never writes


# ------------------------------------------------- A2: bell notifications


def message(notices, entry):
    item = notices.get(notification_id(entry.entry_id, GATEWAY))
    return item["message"] if item is not None else None


@bounded
async def test_the_notification_follows_the_local_list_without_the_cloud(
    hass, notices
) -> None:
    backend = FaultBackend()
    async with running(hass, backend, configure=local_values()) as (
        entry,
        cloud,
        owner,
        _,
    ):
        held = await switched_off(hass, owner, cloud)
        await eventually(lambda: not owner.services._jobs)
        manager = owner.services.notifications[GATEWAY]
        assert manager.coordinator is held
        await eventually(lambda: "521" in (message(notices, entry) or ""))
        local_list(owner)
        local_list(owner)
        # Ended by the local list: listed as no longer active; the fault only
        # the cloud knew stays, as not confirmed.
        await eventually(lambda: len(held.faults.active) == 1)
        text = message(notices, entry)
        assert "1000" in text
        assert "not yet been confirmed" in text
        assert "Confirmed resolved: 1" in text
        # Dismissed by the user: the same faults never show it again, also
        # not after the cloud continues the baseline.
        pn.async_dismiss(hass, notification_id(entry.entry_id, GATEWAY))
        await settle(hass)
        assert message(notices, entry) is None
        backend.faults = [CLOUD_ONLY]
        management = owner.connections.management
        management.set_enabled(management.edit(cloud), True)
        await eventually(
            lambda: (
                (item := source_for(owner, GATEWAY)) is not None and item is not held
            )
        )
        await eventually(lambda: manager.coordinator is source_for(owner, GATEWAY))
        await settle(hass)
        assert message(notices, entry) is None


@bounded
async def test_the_dormant_view_notifies_from_the_local_list(hass, notices) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    async with running(hass, backend, configure=cloud_off()) as (entry, _, owner, _):
        await owner.start_platforms()
        await eventually(lambda: retained(owner) is not None)
        dormant = retained(owner)
        await eventually(lambda: "local" in owner.connections._contexts)
        local_list(owner, ITEM)
        await eventually(lambda: dormant.faults.active)
        await eventually(lambda: owner.services.notifications.get(GATEWAY))
        assert owner.services.notifications[GATEWAY].coordinator is dormant
        await eventually(lambda: "521" in (message(notices, entry) or ""))
        local_list(owner)
        local_list(owner)
        await eventually(lambda: not dormant.faults.active)
        await eventually(lambda: "no longer active" in (message(notices, entry) or ""))


@bounded
async def test_a_switched_off_notification_option_shows_nothing(hass, notices) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    base = cloud_off()

    def configure(data, options):
        base(data, options)
        options[CONF_FAULT_NOTIFICATIONS] = {
            gateway_notification_key(GATEWAY): {"enabled": False, "reset": 0}
        }

    async with running(hass, backend, configure=configure) as (entry, _, owner, _):
        await owner.start_platforms()
        await eventually(lambda: retained(owner) is not None)
        dormant = retained(owner)
        await eventually(lambda: "local" in owner.connections._contexts)
        local_list(owner, ITEM)
        await eventually(lambda: dormant.faults.active)
        await eventually(lambda: owner.services.notifications.get(GATEWAY))
        await settle(hass)
        assert message(notices, entry) is None


# ----------------------------------------------------------- A5: diagnostics


@bounded
async def test_diagnostics_describe_views_without_a_cloud(hass) -> None:
    import json

    from custom_components.bosch_buderus_heating.diagnostics import (
        async_get_config_entry_diagnostics,
    )
    from custom_components.bosch_buderus_heating.inventory import (
        build_anonymized_inventory,
    )

    backend = FaultBackend()
    async with running(hass, backend, configure=local_values()) as (
        entry,
        cloud,
        owner,
        _,
    ):
        await started(hass, owner)
        result = await async_get_config_entry_diagnostics(hass, entry)
        assert result["diagnostics_schema"] == 45
        assert result["views_without_cloud"] == []  # the cloud runs
        await switched_off(hass, owner, cloud, start=False)
        await eventually(lambda: entity_state(hass, owner, "sensor", FAULTS) == "2")
        result = await async_get_config_entry_diagnostics(hass, entry)
        (view,) = result["views_without_cloud"]
        assert view["kind"] == "held"
        assert view["target"] in {item["target"] for item in result["targets"]}
        assert view["local_list_decides"] is True
        assert view["local_list_current"] is True
        assert view["active_faults"] == 2
        assert 0 < view["entities_available"] <= view["entities"]
        serialized = json.dumps(result["views_without_cloud"])
        for private in (entry.entry_id, GATEWAY, cloud, "/notifications"):
            assert private not in serialized
        owner.connections._local_states.pop("local")
        owner.connections._notify()
        await eventually(
            lambda: entity_state(hass, owner, "sensor", FAULTS) == "unavailable"
        )
        result = await async_get_config_entry_diagnostics(hass, entry)
        assert result["views_without_cloud"][0]["local_list_current"] is False
        # Schema 41 is accepted by the inventory export.
        assert build_anonymized_inventory(result)["gateway_count"] == 0


@bounded
async def test_diagnostics_name_the_dormant_view(hass) -> None:
    from custom_components.bosch_buderus_heating.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    backend = ReadingBackend()
    await first_run(hass, backend)
    async with running(hass, backend, configure=cloud_off()) as (entry, _, owner, _):
        await owner.start_platforms()
        await eventually(lambda: retained(owner) is not None)
        result = await async_get_config_entry_diagnostics(hass, entry)
        (view,) = result["views_without_cloud"]
        assert view["kind"] == "dormant"
        assert view["local_list_current"] is False
        assert view["active_faults"] == 0
