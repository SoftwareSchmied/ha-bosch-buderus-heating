"""Local source health is independent of its active configuration generation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import compress
from operator import is_not
from typing import Any

from .gateway_identity import GatewayNamespace, IdentityContext
from .local_api.models import LocalObservation
from .local_polling.inventory import ResourceView


class LocalCondition(StrEnum):
    INITIALIZING = "initializing"
    READY = "ready"
    PAUSED = "paused"
    UNAVAILABLE = "unavailable"
    AUTH_REQUIRED = "auth_required"
    CERTIFICATE_CHANGED = "certificate_changed"


# M2: a published state differs from the one before in a few views only; the
# views and observations are immutable, so one that is the very object the
# last valid state of the same context held needs no new check. Whatever is
# not certain this way is checked in full, with the same errors as before.
_MAX_VALIDATED = 4
type _Checked = tuple[
    tuple[ResourceView, ...], tuple[LocalObservation, ...], tuple[ResourceView, ...]
]
_VALIDATED: dict[IdentityContext, _Checked] = {}


def _validated_before(context: IdentityContext) -> _Checked | None:
    return _VALIDATED.get(context) if isinstance(context, IdentityContext) else None


def _remember_validated(
    context: IdentityContext,
    resources: tuple[ResourceView, ...],
    observations: tuple[LocalObservation, ...],
    fault_views: tuple[ResourceView, ...],
) -> None:
    if context not in _VALIDATED and len(_VALIDATED) >= _MAX_VALIDATED:
        _VALIDATED.clear()
    _VALIDATED[context] = (resources, observations, fault_views)


def fault_path(path: str) -> bool:
    """The readings of the fault lists: the notifications and the error
    lists of the devices."""
    return path == "/notifications" or (
        path.startswith("/devices/") and path.endswith("/errors")
    )


def _changes[T](
    new: tuple[T, ...],
    old: tuple[T, ...],
    valid: Callable[[Any], bool],
    target: Callable[[Any], str],
) -> list[int] | None:
    """The positions in which ``new`` is ``old`` with items replaced by valid
    ones of the same target (then it is as valid as ``old`` was, targets still
    unique); ``None`` when it is anything else."""
    if len(new) != len(old):
        return None
    changed = []
    for index in compress(range(len(new)), map(is_not, new, old, strict=True)):
        item = new[index]
        if not valid(item) or target(item) != target(old[index]):
            return None
        changed.append(index)
    return changed


@dataclass(frozen=True, slots=True)
class LocalSourceState:
    """Retain observations, without treating them as newly read or unexpired.

    The polling adapter evaluates each observation's own expiry.
    Publishing a health change never changes an observation's time or context.
    """

    context: IdentityContext = field(repr=False)
    condition: LocalCondition
    identity_confirmed: bool = False
    last_observations: tuple[LocalObservation, ...] = field(default=(), repr=False)
    resources: tuple[ResourceView, ...] = field(default=(), repr=False)
    problem: str | None = None
    # Worked out once: a published state never changes, and every entity
    # update asks for it again.
    _current: tuple[LocalObservation, ...] = field(
        default=(), init=False, repr=False, compare=False
    )
    # The views of the fault lists, in the order of ``resources``.
    _fault_views: tuple[ResourceView, ...] = field(
        default=(), init=False, repr=False, compare=False
    )

    @property
    def fault_views(self) -> tuple[ResourceView, ...]:
        """The views of ``/notifications`` and of the device error lists."""
        return self._fault_views

    @property
    def current_observations(self) -> tuple[LocalObservation, ...]:
        """Only explicitly current resource views can supply measurement data,
        from a ready or merely paused access with a confirmed identity."""
        return self._current

    def __post_init__(self) -> None:
        if (
            self.context.namespace is not GatewayNamespace.K40_LOCAL
            or not isinstance(self.condition, LocalCondition)
            or type(self.identity_confirmed) is not bool
            or not isinstance(self.last_observations, tuple)
            or len(self.last_observations) > 2048
            or (self.condition is LocalCondition.READY and not self.identity_confirmed)
        ):
            raise ValueError("invalid_local_source_state")
        previous = _validated_before(self.context)
        context = self.context
        if previous is None or (
            _changes(
                self.last_observations,
                previous[1],
                lambda item: (
                    isinstance(item, LocalObservation) and item.context == context
                ),
                lambda item: item.request.target,
            )
            is None
        ):
            if any(
                not isinstance(observation, LocalObservation)
                or observation.context != self.context
                for observation in self.last_observations
            ):
                raise ValueError("invalid_local_source_state")
            paths = [
                observation.request.target for observation in self.last_observations
            ]
            if len(set(paths)) != len(paths):
                raise ValueError("duplicate_local_observation")
        if not isinstance(self.resources, tuple) or len(self.resources) > 2048:
            raise ValueError("invalid_local_resource_views")
        changed = (
            _changes(
                self.resources,
                previous[0],
                lambda item: (
                    isinstance(item, ResourceView)
                    and (
                        item.observation is None or item.observation.context == context
                    )
                ),
                lambda item: item.request.target,
            )
            if previous is not None
            else None
        )
        if changed is None and (
            any(
                not isinstance(v, ResourceView)
                or (v.observation is not None and v.observation.context != self.context)
                for v in self.resources
            )
            or len({v.request.target for v in self.resources}) != len(self.resources)
        ):
            raise ValueError("invalid_local_resource_views")
        # The fault lists change rarely: the views of the last state stay
        # valid while none of the replaced views belongs to one.
        if (
            previous is not None
            and changed is not None
            and not any(fault_path(self.resources[i].request.path) for i in changed)
        ):
            fault_views = previous[2]
        else:
            fault_views = tuple(v for v in self.resources if fault_path(v.request.path))
        object.__setattr__(self, "_fault_views", fault_views)
        _remember_validated(
            self.context, self.resources, self.last_observations, fault_views
        )
        # A pause of a confirmed access only slows the requests: values read
        # before stay current until their own expiry (three intervals). Every
        # other condition withdraws them at once.
        if self.identity_confirmed and self.condition in {
            LocalCondition.READY,
            LocalCondition.PAUSED,
        }:
            object.__setattr__(
                self,
                "_current",
                tuple(
                    [
                        v.observation
                        for v in self.resources
                        if v.available and v.observation is not None
                    ]
                ),
            )
