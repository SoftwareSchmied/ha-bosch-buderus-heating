"""Local source health is independent of its active configuration generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

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

    @property
    def current_observations(self) -> tuple[LocalObservation, ...]:
        """Only explicitly current resource views can supply measurement data."""
        if self.condition is not LocalCondition.READY or not self.identity_confirmed:
            return ()
        return tuple(
            v.observation
            for v in self.resources
            if v.available and v.observation is not None
        )

    def __post_init__(self) -> None:
        if (
            self.context.namespace is not GatewayNamespace.K40_LOCAL
            or not isinstance(self.condition, LocalCondition)
            or type(self.identity_confirmed) is not bool
            or not isinstance(self.last_observations, tuple)
            or len(self.last_observations) > 2048
            or (self.condition is LocalCondition.READY and not self.identity_confirmed)
            or any(
                not isinstance(observation, LocalObservation)
                or observation.context != self.context
                for observation in self.last_observations
            )
        ):
            raise ValueError("invalid_local_source_state")
        paths = [observation.request.target for observation in self.last_observations]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate_local_observation")
        if (
            not isinstance(self.resources, tuple)
            or len(self.resources) > 2048
            or any(
                not isinstance(v, ResourceView)
                or (v.observation is not None and v.observation.context != self.context)
                for v in self.resources
            )
            or len({v.request.target for v in self.resources}) != len(self.resources)
        ):
            raise ValueError("invalid_local_resource_views")
