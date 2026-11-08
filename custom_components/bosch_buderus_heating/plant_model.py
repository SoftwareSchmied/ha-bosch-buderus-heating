"""Device evidence and resource groups independent of Home Assistant registries."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from .pointt import Gateway
from .resource_catalog import FunctionGroup, function_group_for_path


@dataclass(frozen=True, slots=True)
class PlantKey:
    """A system key: its configuration owner and stored target (P50, E-A).

    The target stays when the gateway is exchanged; the gateway is only the
    device identifier of :class:`PointTDevice`.
    """

    owner_id: str
    target_id: str


@dataclass(frozen=True, slots=True)
class PointTDevice:
    """A device supplied by the gateway adapter, not inferred from a resource."""

    identifier: str
    evidence: Literal["pointt_gateway"] = field(default="pointt_gateway", init=False)


@dataclass(frozen=True, slots=True)
class GroupKey:
    """A functional identity distinct from any Home Assistant device identifier."""

    plant: PlantKey
    kind: str
    reported_id: str


@dataclass(frozen=True, slots=True)
class ResourceBinding:
    """Associate a resource with its device and optional functional group."""

    path: str
    device: PointTDevice
    group: FunctionGroup | None
    group_key: GroupKey | None


@dataclass(frozen=True, slots=True)
class GroupInventory:
    """An immutable group of observed resource paths, without live values."""

    key: GroupKey
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlantModel:
    """Project current PointT resources onto the existing gateway device.

    The first stage accepts only the gateway device already supplied by setup.
    Additional resource groups, including /devices paths, do not establish
    another device. Classification does not discover paths or enable controls.
    """

    key: PlantKey
    gateway_device: PointTDevice

    @classmethod
    def from_pointt_gateway(
        cls, owner_id: str, gateway: Gateway, *, target_id: str | None = None
    ) -> PlantModel:
        """The system of a stored target with its gateway as the device.

        Managed setups pass their stored target, which survives a gateway
        exchange (P50). The older entry format has no targets; there the
        gateway identifier stands in for the target. This factory never infers
        that two gateways are one system.
        """
        return cls(
            PlantKey(owner_id, target_id or gateway.gateway_id),
            PointTDevice(gateway.gateway_id),
        )

    @property
    def devices(self) -> tuple[PointTDevice, ...]:
        """Return only devices supported by the current evidence contract."""
        return (self.gateway_device,)

    def bind_resource(self, path: str) -> ResourceBinding:
        """Resolve ownership centrally without interpreting values as identity."""
        group = function_group_for_path(path)
        key = (
            GroupKey(self.key, group.kind, group.logical_id)
            if group is not None
            else None
        )
        return ResourceBinding(path, self.gateway_device, group, key)

    def groups_for_paths(self, paths: Iterable[str]) -> tuple[GroupInventory, ...]:
        """Group supplied inventory paths without expanding references or IDs.

        The caller supplies its existing inventory, including retained paths
        during temporary outages. This view stores no values, names or rights
        and does not maintain a second discovery cache.
        """
        grouped: dict[GroupKey, set[str]] = {}
        for path in paths:
            key = self.bind_resource(path).group_key
            if key is not None:
                grouped.setdefault(key, set()).add(path)
        return tuple(
            GroupInventory(key, tuple(sorted(grouped[key])))
            for key in sorted(grouped, key=lambda key: (key.kind, key.reported_id))
        )
