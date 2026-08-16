"""Bounded discovery of the PointT resource reference tree."""

from __future__ import annotations

from .pointt import PointTClient, Resource

ROOT_RESOURCE_PATHS: tuple[str, ...] = (
    "/gateway",
    "/system",
    "/heatingCircuits",
    "/dhwCircuits",
    "/heatSources",
)

MAX_DISCOVERY_DEPTH = 8
MAX_DISCOVERY_RESOURCES = 256

# Some gateways advertise this container but reject reading it, while its
# stable public children remain readable. Keep this fallback deliberately
# narrow so ordinary capabilities still come exclusively from references.
OPAQUE_CONTAINER_CHILDREN: dict[str, tuple[str, ...]] = {
    "/heatSources/emon": (
        "/heatSources/emon/totalConsumption",
        "/heatSources/emon/chConsumption",
        "/heatSources/emon/dhwConsumption",
        "/heatSources/emon/coolingConsumption",
    )
}


async def async_discover_resources(
    client: PointTClient,
    gateway_id: str,
    *,
    roots: tuple[str, ...] = ROOT_RESOURCE_PATHS,
    maximum_depth: int = MAX_DISCOVERY_DEPTH,
    maximum_resources: int = MAX_DISCOVERY_RESOURCES,
) -> dict[str, Resource]:
    """Follow PointT references without escaping configured safety bounds."""
    if maximum_depth < 0 or maximum_resources < 1:
        raise ValueError("Discovery bounds must be positive")

    pending = [(path, 0) for path in roots]
    queued = set(roots)
    discovered: dict[str, Resource] = {}
    while pending:
        depth = pending[0][1]
        capacity = maximum_resources - len(discovered)
        frontier: list[str] = []
        while pending and pending[0][1] == depth and len(frontier) < capacity:
            frontier.append(pending.pop(0)[0])
        results = await client.get_resources_bulk(gateway_id, frontier)
        for result in results:
            resource = result.resource
            if resource is None:
                continue
            discovered[result.path] = resource
            if len(discovered) >= maximum_resources or depth >= maximum_depth:
                continue
            for reference in resource.references:
                child = reference.path
                if child in queued or not _is_allowed_reference(child, roots):
                    continue
                queued.add(child)
                pending.append((child, depth + 1))
                for fallback in OPAQUE_CONTAINER_CHILDREN.get(child, ()):
                    if fallback not in queued:
                        queued.add(fallback)
                        pending.append((fallback, depth + 2))
    return discovered


def _is_allowed_reference(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in roots)
