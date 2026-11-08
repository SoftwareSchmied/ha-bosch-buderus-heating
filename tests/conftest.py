"""Explicit compatibility fixtures for tests that inject a legacy runtime."""

from unittest.mock import patch

import pytest

from custom_components.bosch_buderus_heating.config_flow import BoschBuderusConfigFlow


@pytest.fixture
def legacy_platform_runtime():
    """Keep injected v3 platform fixtures distinct from actual v4 setup tests."""
    with patch.object(BoschBuderusConfigFlow, "VERSION", 3):
        yield
