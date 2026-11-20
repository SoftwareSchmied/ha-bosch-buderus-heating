"""A reading that answers HTTP 200 with ``"value": null`` brought no value.

The cloud sometimes answers a scalar (text, float, integer) with a null value
for an hour or more (hot water operation mode of one K40 for 88 minutes).
That is no new reading: it must not replace the last good value, so it is
handled like a reading the answer did not contain, and the entities keep
the last value for the short time every other missing answer is held.
"""

from __future__ import annotations

from typing import Final

from .pointt import BatchItemResult, Resource
from .pointt.exceptions import ResourceOmitted

_SCALAR_TYPES: Final = frozenset({"stringValue", "floatValue", "integerValue"})


def is_null_reading(resource: Resource) -> bool:
    """A scalar resource whose answer carried a null value."""
    return (
        resource.metadata.resource_type in _SCALAR_TYPES
        and resource.has_value
        and resource.value is None
        and not resource.references
        and not resource.has_values
    )


def holds_value(resource: Resource) -> bool:
    return resource.has_value and resource.value is not None


def without_answer(result: BatchItemResult) -> BatchItemResult:
    """The result of a null reading: no answer for that path."""
    return BatchItemResult(
        result.gateway_id,
        result.path,
        None,
        error=ResourceOmitted("The answer carried no value"),
    )
