"""The extra hot water duration of a KM200 reports limits in 1/15 minutes.

Issue 602 of another project: 225 to 960 "mins" (15 minutes = 225, one hour =
900). The scale is not confirmed, so the control is created disabled and no
conversion is guessed.
"""

from __future__ import annotations

import pytest

from custom_components.bosch_buderus_heating.charge_duration import (
    scale_is_unclear,
)
from custom_components.bosch_buderus_heating.number import build_number_descriptions
from custom_components.bosch_buderus_heating.pointt import (
    Resource,
    ResourceMetadata,
)

PATH = "/dhwCircuits/dhw1/chargeDuration"


def _duration(minimum: float, maximum: float, unit: str = "mins") -> Resource:
    return Resource(
        PATH,
        value=minimum,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="floatValue",
            unit=unit,
            minimum=minimum,
            maximum=maximum,
            writable=True,
        ),
    )


def _enabled(resource: Resource) -> bool:
    (description,) = build_number_descriptions({resource.path: resource})
    assert description.translation_key == "extra_hot_water_duration"
    # The limits stay the ones the gateway reports, nothing is converted.
    assert description.native_min_value == resource.metadata.minimum
    assert description.native_max_value == resource.metadata.maximum
    return bool(description.entity_registry_enabled_default)


@pytest.mark.parametrize(("minimum", "maximum"), [(225, 960), (120, 960), (900, 960)])
def test_a_scale_in_fifteenths_creates_the_control_disabled(
    minimum: float, maximum: float
) -> None:
    resource = _duration(minimum, maximum)
    assert scale_is_unclear(PATH, resource.metadata)
    assert _enabled(resource) is False


@pytest.mark.parametrize(
    ("minimum", "maximum", "unit"),
    [(15, 2880, "mins"), (60, 2880, "mins"), (1, 48, "h"), (119, 960, "mins")],
)
def test_ordinary_limits_keep_the_control_enabled(
    minimum: float, maximum: float, unit: str
) -> None:
    resource = _duration(minimum, maximum, unit)
    assert not scale_is_unclear(PATH, resource.metadata)
    assert _enabled(resource) is True


def test_only_the_charge_duration_is_judged() -> None:
    metadata = _duration(225, 960).metadata
    assert not scale_is_unclear("/dhwCircuits/dhw1/singleChargeSetpoint", metadata)
