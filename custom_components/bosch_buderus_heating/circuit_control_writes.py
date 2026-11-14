"""The guarded writes of the heating circuit settings (``circuit_controls``).

The number and select entities and the controls dialog write these settings
through the coordinator as every other control (one request, read-back, no
second request). Before the request the coordinator reads the guards again
on the gateway, in one request with the setting itself: a change since they
were shown sends nothing, and the setting's own limits come from that
reading. A guard the plant has but without a current reading sends nothing
either.
"""

from __future__ import annotations

from homeassistant.exceptions import ServiceValidationError

from . import circuit_controls, variable_tariff
from .const import DOMAIN
from .control import async_set_control, cloud_down_error
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .icom_profile import is_icom_gateway
from .pointt import Resource
from .writes import EnumWritePolicy, NumberWritePolicy


def write_guards(
    coordinator: BoschBuderusDataUpdateCoordinator,
    path: str,
    policy: object,
    *,
    include_target: bool = True,
) -> tuple[Resource, ...] | None:
    """The current readings a write is checked against (``expected``).

    ``()`` for every other control. ``None`` when one of them exists but
    has no current reading: then nothing is written.
    """
    data = coordinator.data or {}
    guards: list[Resource] = []
    for item in circuit_controls.guard_paths(path, policy):
        if item == path and not include_target:
            continue
        snapshot = data.get(item)
        if snapshot is None:
            continue
        if (
            not coordinator.last_update_success
            or not snapshot.available
            or snapshot.freshness is not Freshness.FRESH
            or snapshot.resource.path != item
        ):
            return None
        guards.append(snapshot.resource)
    return tuple(guards)


def offered_on(coordinator: BoschBuderusDataUpdateCoordinator, path: str) -> bool:
    """``circuit_controls.offered`` with the coordinator's current readings."""
    data = coordinator.data or {}
    readings = {
        item: snapshot.resource
        for item, snapshot in data.items()
        if item.startswith("/heatingCircuits/")
    }
    return circuit_controls.offered(path, readings)


async def async_set_guarded(
    coordinator: BoschBuderusDataUpdateCoordinator,
    path: str,
    value: str | float,
    policy: EnumWritePolicy | NumberWritePolicy,
) -> None:
    """``control.async_set_control`` with the guards of the setting."""
    guards = write_guards(coordinator, path, policy)
    if guards is None:
        if (down := cloud_down_error(coordinator)) is not None:
            raise down
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="write_validation_failed"
        )
    await async_set_control(coordinator, path, value, policy, expected=guards)


def circuit_control_diagnostics(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> dict[str, object]:
    """Counts and types only: how many temperature levels have a name that
    is not operated, and whether the iCom tariff paths exist (no values)."""
    resources = coordinator.resources
    return {
        "unknown_temperature_levels": circuit_controls.unknown_level_count(resources),
        "variable_tariff": variable_tariff.diagnostics(
            resources,
            variable_tariff.ICOM_TARIFF_PATHS
            if is_icom_gateway(coordinator.gateway)
            else (),
        ),
    }
