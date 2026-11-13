"""Why each access and each source started, kept for the diagnostics."""

from __future__ import annotations

from copy import deepcopy

from custom_components.bosch_buderus_heating.connection_runtime import (
    ConnectionRuntime,
)
from custom_components.bosch_buderus_heating.pointt import RefreshTokenRejected
from custom_components.bosch_buderus_heating.source_restarts import (
    RECENT_STARTS,
    StartHistory,
    StartReason,
)
from tests.test_connection_retries import (
    PRIVATE,
    FailingDriver,
    FailingSource,
    StepHarness,
    step,
)
from tests.test_connection_runtime import entry_for, settle


def _reasons(report: dict[str, object]) -> list[tuple[object, object]]:
    recent = report["recent"]
    assert isinstance(recent, list)
    return [(item["reason"], item["failure"]) for item in recent]


def test_history_keeps_the_recent_starts_with_their_first_cause() -> None:
    history = StartHistory()
    history.started("a")
    history.note("a", StartReason.REQUESTED)
    history.note("a", StartReason.CONFIGURATION)  # the first cause counts
    history.started("a")
    history.started("a")  # no cause noted: never guessed
    history.started("a", StartReason.FAILURE, failure="TimeoutError")
    assert _reasons(history.diagnostics("a")) == [
        ("start", None),
        ("requested", None),
        ("other", None),
        ("after_failure", "TimeoutError"),
    ]
    assert all(
        isinstance(item["at"], str) for item in history.diagnostics("a")["recent"]
    )
    for _ in range(RECENT_STARTS):
        history.started("a", StartReason.ENDED)
    report = history.diagnostics("a")
    assert report["total"] == 4 + RECENT_STARTS
    assert len(report["recent"]) == RECENT_STARTS
    assert history.diagnostics("unknown") == {"total": 0, "recent": []}
    history.note("b", StartReason.ACCESS)
    history.keep({"b"})
    assert history.diagnostics("a") == {"total": 0, "recent": []}
    history.started("b")
    assert _reasons(history.diagnostics("b")) == [("access_restarted", None)]


class EndingSource(FailingSource):
    """Ends on its own, without an error, while it is still current."""

    def __init__(self, binding_id, harness):
        super().__init__(binding_id, harness.start_errors)
        self.harness = harness

    async def run(self, context):
        if self.binding_id in self.harness.ending:
            self.harness.ending.discard(self.binding_id)
            context.publish({"sample": self.binding_id})
            return
        await super().run(context)


class EndingDriver(FailingDriver):
    def source(self, binding_id, binding):
        source = EndingSource(binding_id, self.harness)
        self.sources.setdefault(binding_id, []).append(source)
        return source


class EndingHarness(StepHarness):
    def __init__(self):
        super().__init__()
        self.ending: set[str] = set()

    def factory(self, key, record):
        driver = EndingDriver(key, self)
        self.drivers.setdefault(key, []).append(driver)
        return driver


def _runtime(hass):
    entry, cloud, _ = entry_for(hass)
    harness = EndingHarness()
    runtime = ConnectionRuntime(
        hass,
        entry,
        {"singlekey_cloud": harness.factory, "k40_local": harness.factory},
        sleep=harness.sleep,
    )
    binding = next(
        key
        for key, item in runtime.snapshot.bindings.items()
        if item.connection_id == cloud and item.adapter == "pointt"
    )
    return entry, cloud, binding, harness, runtime


async def test_each_source_start_names_its_cause(hass) -> None:
    _entry, cloud, binding, harness, runtime = _runtime(hass)
    runtime.start()
    await settle(hass)
    try:
        assert _reasons(runtime.connection_starts(cloud)) == [("start", None)]
        assert _reasons(runtime.source_starts(binding)) == [("start", None)]
        assert _reasons(runtime.source_starts("local")) == [("start", None)]

        # A failure while running: the next start names its class only.
        harness.source(cloud, binding).messages.put_nowait(KeyError(PRIVATE))
        await settle(hass)
        await step(hass, harness)
        assert _reasons(runtime.source_starts(binding))[-1] == (
            "after_failure",
            "KeyError",
        )
        assert PRIVATE not in repr(runtime.source_starts(binding))

        # A source that ends on its own while it is still current.
        harness.ending.add(binding)
        harness.source(cloud, binding).messages.put_nowait(KeyError(PRIVATE))
        await settle(hass)
        await step(hass, harness)  # this start ends on its own
        await step(hass, harness)
        assert _reasons(runtime.source_starts(binding))[-2:] == [
            ("after_failure", "KeyError"),
            ("source_ended", None),
        ]

        # A new search the owner asked for.
        context = runtime.source_context(binding)
        assert context is not None
        runtime.restart_source(context)
        await settle(hass)
        assert _reasons(runtime.source_starts(binding))[-1] == ("requested", None)

        # Switched off and on in the options.
        management = runtime.management
        management.set_binding_enabled(management.edit(cloud), binding, False)
        await settle(hass)
        management.set_binding_enabled(management.edit(cloud), binding, True)
        await settle(hass)
        assert _reasons(runtime.source_starts(binding))[-1] == (
            "configuration_changed",
            None,
        )
        # The access and the other sources did not start again.
        assert runtime.connection_starts(cloud)["total"] == 1
        assert runtime.source_starts("local")["total"] == 1
    finally:
        await runtime.close()


async def test_each_access_start_names_its_cause(hass) -> None:
    entry, cloud, binding, harness, runtime = _runtime(hass)
    harness.errors[cloud] = ConnectionError(PRIVATE)
    runtime.start()
    await settle(hass)
    try:
        # The first connection fails; the next attempt names the failure.
        del harness.errors[cloud]
        await step(hass, harness)
        assert _reasons(runtime.connection_starts(cloud)) == [
            ("start", None),
            ("after_failure", "ConnectionError"),
        ]
        assert _reasons(runtime.source_starts(binding)) == [("start", None)]
        assert PRIVATE not in repr(runtime.connection_starts(cloud))

        # A changed local access starts again, and its source with it.
        data = deepcopy(dict(entry.data))
        data["connections"]["local"]["endpoint"]["host"] = "new.example.invalid"
        data["connections"]["local"]["config_revision"] += 1
        hass.config_entries.async_update_entry(entry, data=data)
        await settle(hass)
        assert _reasons(runtime.connection_starts("local"))[-1] == (
            "configuration_changed",
            None,
        )
        assert _reasons(runtime.source_starts("local"))[-1] == (
            "configuration_changed",
            None,
        )
    finally:
        await runtime.close()


async def test_sources_of_a_stopped_access_name_it_when_they_start_again(
    hass,
) -> None:
    entry, cloud, binding, harness, runtime = _runtime(hass)
    runtime.start()
    await settle(hass)
    siblings = [
        key
        for key, item in runtime.snapshot.bindings.items()
        if item.connection_id == cloud and key != binding
    ]
    try:
        # A rejected sign-in stops the access with all its sources.
        harness.source(cloud, binding).messages.put_nowait(
            RefreshTokenRejected(PRIVATE)
        )
        await settle(hass)
        assert runtime.pending_reauthentication == (cloud,)
        # The new sign-in changes the stored access: it starts again.
        data = deepcopy(dict(entry.data))
        data["connections"][cloud]["config_revision"] += 1
        hass.config_entries.async_update_entry(entry, data=data)
        await settle(hass)
        assert _reasons(runtime.connection_starts(cloud))[-1] == (
            "configuration_changed",
            None,
        )
        for key in (binding, *siblings):
            assert _reasons(runtime.source_starts(key))[-1] == (
                "access_restarted",
                None,
            )
        # A removed source keeps no history.
        assert set(runtime._source_starts._recent) <= set(runtime.snapshot.bindings)
    finally:
        await runtime.close()
