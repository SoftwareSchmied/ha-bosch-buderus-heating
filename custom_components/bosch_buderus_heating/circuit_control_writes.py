"""The guarded writes of the heating circuit settings (``circuit_controls``)
and of the ConnectKey zones' older number and select entities.

The number and select entities and the controls dialog write these settings
through the coordinator as every other control (one request, read-back, no
second request). Before the request the coordinator reads the guards again
on the gateway, in one request with the setting itself: a change of a guard
since it was shown sends nothing, and the setting's own limits and flags
come from that reading. A guard the plant has but without a current reading
sends nothing either. An entity sets a value of its own, so a change
of the setting's value since the last poll does not refuse it; the controls
dialog compares it with the value it showed. A zone's guards are the season
and control type of its heating circuit, as on the zone's climate entity.
"""

from __future__ import annotations

from homeassistant.exceptions import ServiceValidationError

from . import circuit_controls, variable_tariff
from .circuit_controls import (
    CONSTANT_CONTROL,
    CONSTANT_CONTROL_REFUSAL,
    CONTROL_TYPE,
)
from .const import DOMAIN
from .control import async_set_control, cloud_down_error
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .icom_profile import is_icom_gateway
from .pointt import Resource
from .writes import EnumWritePolicy, NumberWritePolicy
from .zone_climate import assigned_circuit
from .zone_controls import is_zone_control


def zone_guard_paths(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> tuple[str, ...]:
    """A ConnectKey zone's mode or manual setpoint: the season and control
    type of the zone's heating circuit, as for the zone's climate entity;
    none for every other control or a zone without a circuit."""
    if not is_zone_control(path):
        return ()
    root = path.split("/", 3)
    circuit = assigned_circuit(coordinator, "/".join(root[:3]))
    if circuit is None:
        return ()
    return (f"{circuit}/currentSuWiMode", f"{circuit}/{CONTROL_TYPE}")


def write_guards(
    coordinator: BoschBuderusDataUpdateCoordinator,
    path: str,
    policy: object,
    *,
    include_target: bool = True,
    include_coupled: bool = True,
) -> tuple[Resource, ...] | None:
    """The current readings a write is checked against (``expected``).

    ``()`` for every other control. ``None`` when one of them exists but
    has no current reading: then nothing is written. Without
    ``include_coupled`` the other one of two coupled levels is left out (an
    entity reads it again with the write instead, ``fresh``).
    """
    data = coordinator.data or {}
    guards: list[Resource] = []
    coupled = () if include_coupled else circuit_controls.coupled_paths(path, policy)
    for item in (
        *circuit_controls.guard_paths(path, policy),
        *zone_guard_paths(coordinator, path),
    ):
        if (item == path and not include_target) or item in coupled:
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
    """``control.async_set_control`` with the guards of the setting; the
    setting itself (and the other one of two coupled levels) is read again
    with them for its current limits and flags.

    A coupled level is no guard of an entity's write: two levels set at the
    same time (a scene) are written one after the other, each checked
    against the other's current reading. A zone whose circuit runs at
    constant values is only shown, as on the zone's climate entity.
    """
    guards = write_guards(
        coordinator, path, policy, include_target=False, include_coupled=False
    )
    if guards is None:
        if (down := cloud_down_error(coordinator)) is not None:
            raise down
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="write_validation_failed"
        )
    if is_zone_control(path) and any(
        guard.path.endswith(f"/{CONTROL_TYPE}") and guard.value == CONSTANT_CONTROL
        for guard in guards
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key=CONSTANT_CONTROL_REFUSAL
        )
    await async_set_control(
        coordinator,
        path,
        value,
        policy,
        expected=guards,
        fresh=_target(coordinator, path, policy),
    )


def _target(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str, policy: object
) -> tuple[Resource, ...]:
    """The last reading of a heating circuit setting or a zone control and of
    the other one of two coupled levels, read again before the write; none
    for every other control."""
    data = coordinator.data or {}
    if not circuit_controls.guard_paths(path, policy) and not is_zone_control(path):
        return ()
    return tuple(
        snapshot.resource
        for item in (path, *circuit_controls.coupled_paths(path, policy))
        if (snapshot := data.get(item)) is not None
    )


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
