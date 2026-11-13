"""Forms must use HA's actual validator, not whichever package is installed."""

import importlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import TextSelector

from custom_components.bosch_buderus_heating import ha_schema
from custom_components.bosch_buderus_heating.config_flow import (
    BRAND_SCHEMA,
    BoschBuderusOptionsFlow,
)


def test_form_uses_the_installed_ha_validation_backend():
    assert isinstance(BRAND_SCHEMA, type(cv.PLATFORM_SCHEMA))
    with pytest.raises(data_entry_flow.InvalidData.__base__):
        BRAND_SCHEMA({})


def test_ha_suggested_values_survive_using_the_matching_marker_type():
    validation = ha_schema.validation
    schema = validation.Schema({validation.Required("name"): TextSelector()})
    flow = BoschBuderusOptionsFlow(SimpleNamespace())
    suggested = flow.add_suggested_values_to_schema(schema, {"name": "My holiday"})
    marker = next(iter(suggested.schema))
    assert marker.description == {"suggested_value": "My holiday"}
    assert next(iter(schema.schema)).description is None
    assert suggested({"name": "My holiday"}) == {"name": "My holiday"}


@pytest.mark.parametrize("backend", ["voluptuous", "probatio"])
def test_backend_selection_follows_ha_even_when_both_libraries_are_installed(backend):
    module = pytest.importorskip(backend)
    try:
        with patch.object(cv, "PLATFORM_SCHEMA", module.Schema({})):
            importlib.reload(ha_schema)
            assert ha_schema.validation.Schema is module.Schema
    finally:
        importlib.reload(ha_schema)
