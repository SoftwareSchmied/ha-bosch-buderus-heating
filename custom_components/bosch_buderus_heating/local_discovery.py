"""Bounded hints from Home Assistant's native Zeroconf dispatcher."""

from dataclasses import dataclass, field
from time import monotonic
from typing import cast

from homeassistant.core import HomeAssistant
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .configuration import LOCAL_KIND, ConfigurationError, load_configuration
from .const import DOMAIN
from .local_credentials import LocalCredentialError, load_local_access
from .local_probe import LocalEndpoint, parse_discovery_candidate

_HINTS = f"{DOMAIN}.local_discovery_hints"
_FLOWS = f"{DOMAIN}.local_discovery_flows"
HINT_LIFETIME = 600
MAX_HINTS = 64


@dataclass(frozen=True, slots=True)
class DiscoveryHint:
    endpoints: tuple[LocalEndpoint, ...] = field(repr=False)
    expires: float


def discovery_endpoints(hass: HomeAssistant) -> tuple[LocalEndpoint, ...]:
    hints = cast(dict[str, DiscoveryHint], hass.data.setdefault(_HINTS, {}))
    for name, hint in tuple(hints.items()):
        if hint.expires <= monotonic():
            del hints[name]
    return tuple(dict.fromkeys(e for hint in hints.values() for e in hint.endpoints))


def remember_discovery(
    hass: HomeAssistant,
    info: ZeroconfServiceInfo,
) -> tuple[LocalEndpoint, ...]:
    """Do not use TXT fields, names or addresses as persistent device identity."""
    candidate = parse_discovery_candidate(
        service_type=info.type,
        hostname=info.hostname,
        addresses=info.addresses,
        port=info.port,
    )
    discovery_endpoints(hass)
    hints = cast(dict[str, DiscoveryHint], hass.data[_HINTS])
    # Names are dictionary keys only. No untrusted service text is rendered.
    if info.name not in hints and len(hints) >= MAX_HINTS:
        del hints[next(iter(hints))]
    hints[info.name] = DiscoveryHint(candidate.endpoints, monotonic() + HINT_LIFETIME)
    return candidate.endpoints


def configured_discovery(
    hass: HomeAssistant, endpoints: tuple[LocalEndpoint, ...]
) -> bool:
    """Suppress a repeated address hint, never establish gateway identity.

    Saved access includes prior verification. Disabled connections still count
    as configured; reconnecting and certificate checks remain in options.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        try:
            current = load_configuration(
                entry.entry_id,
                entry.data,
                entry.options,
                version=entry.version,
                minor_version=entry.minor_version,
            )
        except ConfigurationError:
            continue
        for connection in current.connections.values():
            if connection.kind != LOCAL_KIND:
                continue
            try:
                access = load_local_access(connection.to_data())
            except LocalCredentialError:
                continue
            if access.endpoint in endpoints:
                return True
    return False


def track_discovery_flow(
    hass: HomeAssistant, flow_id: str, endpoints: tuple[LocalEndpoint, ...]
) -> bool:
    """Keep at most one pending flow for overlapping transient address hints."""
    flows = cast(dict[str, tuple[LocalEndpoint, ...]], hass.data.setdefault(_FLOWS, {}))
    if any(set(endpoints).intersection(other) for other in flows.values()):
        return False
    flows[flow_id] = endpoints
    return True


def forget_discovery_flow(hass: HomeAssistant, flow_id: str) -> None:
    flows = cast(dict[str, tuple[LocalEndpoint, ...]], hass.data.get(_FLOWS, {}))
    flows.pop(flow_id, None)


def dismiss_configured_discoveries(hass: HomeAssistant) -> None:
    """Dismiss untouched notices only; preserve dialogs the user has advanced."""
    flows = cast(dict[str, tuple[LocalEndpoint, ...]], hass.data.get(_FLOWS, {}))
    manager = hass.config_entries.flow
    for result in manager.async_progress_by_handler(
        DOMAIN, match_context={"source": "zeroconf"}
    ):
        if (
            result.get("step_id") in ("local_owner", "local_discovered")
            and (endpoints := flows.get(result["flow_id"]))
            and configured_discovery(hass, endpoints)
        ):
            manager.async_abort(result["flow_id"])
