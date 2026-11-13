"""The test suite's own isolation: no fake-cloud patch outlives its test."""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.bosch_buderus_heating import coordinator
from custom_components.bosch_buderus_heating.pointt import PointTClient
from tests.helpers.open_patches import close_open_stacks
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches

CONFTEST = Path(__file__).with_name("conftest.py")


def _patched() -> tuple[object, object]:
    return PointTClient.__dict__["get_resource"], coordinator.monotonic


def _conftest(request: pytest.FixtureRequest):
    return next(
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if Path(getattr(plugin, "__file__", "") or "") == CONFTEST
    )


def test_open_fake_cloud_patches_are_closed_and_counted() -> None:
    original = _patched()
    cloud_patches(RrcCloud({}), Clock())
    assert _patched()[0] is not original[0]
    assert _patched()[1] is not original[1]
    assert close_open_stacks() == 1
    assert _patched() == original
    assert close_open_stacks() == 0


def test_closed_fake_cloud_patches_are_not_counted_again() -> None:
    with cloud_patches(RrcCloud({}), Clock()):
        pass
    stack = cloud_patches(RrcCloud({}), Clock())
    stack.close()
    assert close_open_stacks() == 0


def test_the_teardown_check_fails_the_test_and_restores_the_client(
    request: pytest.FixtureRequest,
) -> None:
    """A test that failed before its own teardown (here: never closed)."""
    original = _patched()
    cloud_patches(RrcCloud({}), Clock())
    hook = _conftest(request).pytest_runtest_teardown(request.node, None)
    next(hook)
    with pytest.raises(pytest.fail.Exception, match="1 fake-cloud patch stack"):
        hook.send(None)
    # The later tests of this worker run against the real client and clock.
    assert _patched() == original
