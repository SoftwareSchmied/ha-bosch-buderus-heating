"""Explicit compatibility fixtures for tests that inject a legacy runtime."""

from unittest.mock import patch

import pytest

from custom_components.bosch_buderus_heating.config_flow import BoschBuderusConfigFlow
from custom_components.bosch_buderus_heating.pointt import PointTClient

_MISSING = object()
_CLIENT = dict(vars(PointTClient))


@pytest.fixture
def legacy_platform_runtime():
    """Keep injected v3 platform fixtures distinct from actual v4 setup tests."""
    with patch.object(BoschBuderusConfigFlow, "VERSION", 3):
        yield


def _restore_client() -> list[str]:
    """Put back what a test left patched on the PointT client."""
    now = dict(vars(PointTClient))
    left = sorted(
        name
        for name in _CLIENT.keys() | now.keys()
        if _CLIENT.get(name, _MISSING) is not now.get(name, _MISSING)
    )
    for name in left:
        if name in _CLIENT:
            setattr(PointTClient, name, _CLIENT[name])
        else:
            delattr(PointTClient, name)
    return left


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Fail the test that leaves the PointT client patched and restore it.

    Checked after all fixtures of the test are finished. Without this, every
    later test of the same run talks to the leftover fake answers and waits
    for sources that never come up.
    """
    try:
        result = yield
    finally:
        left = _restore_client()
    if left:
        pytest.fail(f"The test left PointTClient patched: {', '.join(left)}")
    return result
