"""Verified cloud selections, retained identities and atomic registration."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Collection, Iterator, Mapping, MutableMapping
from contextlib import suppress
from dataclasses import dataclass, field, fields
from time import monotonic
from typing import Any, cast
from uuid import uuid4

from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowManager, UnknownFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auth_lifecycle import connection_resources, entry_auth
from .bacon.flow import discover
from .bacon.models import BaconError, device_key, token_subject
from .configuration import (
    CLOUD_KIND,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    ConfigurationError,
    ConfigurationSnapshot,
    JsonObject,
    load_configuration,
    require_identifier,
)
from .configuration_management import (
    ConfigurationManagement,
    ConnectionEdit,
    account_evidence,
    section,
    signed_in_account,
)
from .configuration_migration import (
    convert_legacy_configuration,
    legacy_cloud_connection_id,
)
from .const import DOMAIN, PollingProfile
from .data import tokens_to_data
from .icom_profile import any_icom_gateway
from .local_identity import CONTRACT
from .pointt import AuthTokens, Brand, Gateway, OAuthClient, OAuthFlow, PointTClient

CLAIM = DOMAIN + "_cloud_setup"
# When the claiming dialog last acted, and whether a newer one replaced it.
CLAIMED_AT = CLAIM + "_at"
TAKEN_OVER = CLAIM + "_taken_over"
# Its sign-in link has long expired by then: Home Assistant never removes a
# dialog that was reloaded or left open, so such a claim counts as orphaned.
CLAIM_ORPHANED_SECONDS = (
    cast(float, next(f.default for f in fields(OAuthFlow) if f.name == "max_age"))
    + 300.0
)
CLAIM_ABORTS = frozenset(
    {"cloud_already_connected", "cloud_setup_in_progress", "cloud_setup_taken_over"}
)
DISCOVERY_VALID_SECONDS = 300
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LocalFirstProof:
    """A fresh comparison of one plant's cloud and local system (P49b step 8).

    Scoped to the owner, the target as it was compared, its gateway and the
    local access that was read; valid for a short time only. Never stored.
    """

    owner_id: str = field(repr=False)
    target_id: str = field(repr=False)
    gateway_id: str = field(repr=False)
    binding_id: str = field(repr=False)
    fingerprint: str = field(repr=False)
    issued_at: float = field(default_factory=monotonic, repr=False)


def target_fingerprint(target: JsonObject) -> str:
    """A digest of one stored target, to detect any change after a comparison."""
    return hashlib.sha256(
        json.dumps(target, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def local_first_targets(
    data: JsonObject, options: JsonObject, gateways: Collection[str]
) -> dict[str, str]:
    """Plants of this entry that join a selected cloud only after a fresh
    comparison, by gateway: set up locally first (step 7), or shown from
    their local readings after a cloud removal without a stored comparison
    (R1 safety net)."""
    result = {}
    target_options = section(options, "targets")
    for target_id, target in section(data, "targets").items():
        identities = cast(list[JsonObject], target["identities"])
        local = [i for i in identities if i["namespace"] == "local_system_gateway"]
        if len(local) != 1 or str(local[0]["value"]) not in gateways:
            continue
        gateway = str(local[0]["value"])
        if target.get("kind") == "local_installation" or (
            target.get("kind") == "pointt_object"
            and target_options.get(target_id, {}).get("local_plant") is True
            and local[0].get("comparison_contract") != CONTRACT
            and any(
                i["namespace"] == "pointt_gateway" and i["value"] == gateway
                for i in identities
            )
        ):
            result[gateway] = target_id
    return result


def _join_local_first(
    owner_id: str,
    data: JsonObject,
    options: JsonObject,
    target_id: str,
    gateway: str,
    proof: LocalFirstProof | None,
) -> None:
    """M1: the plant becomes the cloud's object under the same target ID."""
    if proof is None:
        raise ConfigurationError("cloud_local_first_unverified")
    target = section(data, "targets")[target_id]
    binding = section(data, "bindings").get(proof.binding_id)
    if (
        proof.owner_id != owner_id
        or proof.target_id != target_id
        or proof.gateway_id != gateway
        or binding is None
        or binding["target_id"] != target_id
        or binding["adapter"] != "k40_local"
        or proof.fingerprint != target_fingerprint(target)
        or not 0 <= monotonic() - proof.issued_at <= DISCOVERY_VALID_SECONDS
    ):
        raise ConfigurationError("configuration_changed")
    identities = cast(list[JsonObject], target["identities"])
    for item in identities:
        if item["namespace"] == "local_system_gateway":
            # The comparison is stored, so a later local setup reuses it.
            item["evidence"] = "verified_local_setup"
            item["comparison_contract"] = CONTRACT
    if not any(item["namespace"] == "pointt_gateway" for item in identities):
        identities.append(
            {
                "namespace": "pointt_gateway",
                "value": gateway,
                "evidence": "authenticated_listing",
            }
        )
    target["kind"] = "pointt_object"
    target.setdefault("legacy_identity", {"format": "pointt_v3", "gateway_id": gateway})
    target.pop("local_details", None)
    target_options = section(options, "targets")[target_id]
    target_options.pop("local_plant", None)
    # Now cloud and local access: local values by default, a stored choice stays.
    target_options.setdefault("local_values", True)
    binding["link_evidence"] = {
        **cast(JsonObject, binding["link_evidence"]),
        "contract": CONTRACT,
    }


def cloud_snapshots(hass: HomeAssistant) -> tuple[ConfigurationSnapshot, ...]:
    """Include disabled entries and targets whose access has been removed.

    An entry whose stored configuration cannot be read is skipped: it cannot
    be set up, and its own error explains why. It must not turn every new
    cloud setup into a misleading "configuration changed". An entry saved by
    a newer version is never interpreted and still refuses (its ownership is
    unknown).
    """
    result = []
    for entry, snapshot in _read_entries(hass):
        if snapshot is not None:
            result.append(snapshot)
        elif not unreadable_gateway_ids(entry.data):
            # Never logs data; its gateways cannot be protected from a new setup.
            _LOGGER.warning(
                "Configuration entry %s cannot be read and is skipped; its "
                "systems are unknown to new setups",
                entry.entry_id,
            )
    return tuple(result)


def _read_entries(
    hass: HomeAssistant,
) -> list[tuple[ConfigEntry[Any], ConfigurationSnapshot | None]]:
    """Every non-ignored entry with its configuration, ``None`` if unreadable."""
    result: list[tuple[ConfigEntry[Any], ConfigurationSnapshot | None]] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.source == SOURCE_IGNORE:
            continue
        if entry.version > STORAGE_VERSION:
            raise ConfigurationError("unsupported_version")
        snapshot: ConfigurationSnapshot | None
        try:
            if entry.version >= 4:
                snapshot = load_configuration(
                    entry.entry_id,
                    entry.data,
                    entry.options,
                    version=entry.version,
                    minor_version=entry.minor_version,
                )
            else:
                snapshot = convert_legacy_configuration(
                    entry.entry_id,
                    entry.data,
                    entry.options,
                    version=3,
                    minor_version=entry.minor_version,
                )
        except ValueError:  # ConfigurationError included; never logs data
            snapshot = None
        result.append((entry, snapshot))
    return result


def unreadable_gateway_ids(data: Mapping[str, Any]) -> set[str]:
    """Gateway IDs an unreadable entry may still own, read conservatively.

    Looks at targets (identities and legacy identities), bindings and the
    legacy gateway list; anything that is not a plain string is ignored.
    """
    found: set[str] = set()

    def add(value: object) -> None:
        if isinstance(value, str) and value:
            found.add(value)

    def items(value: object) -> list[Any]:
        if isinstance(value, Mapping):
            return list(value.values())
        return list(value) if isinstance(value, list) else []

    for value in items(data.get("gateway_ids")):
        add(value)
    for target in items(data.get("targets")):
        if not isinstance(target, Mapping):
            continue
        for identity in items(target.get("identities")):
            if isinstance(identity, Mapping) and identity.get("namespace") in (
                "pointt_gateway",
                "local_system_gateway",
            ):
                add(identity.get("value"))
        legacy = target.get("legacy_identity")
        if isinstance(legacy, Mapping):
            add(legacy.get("gateway_id"))
    for binding in items(data.get("bindings")):
        locator = binding.get("locator") if isinstance(binding, Mapping) else None
        if isinstance(locator, Mapping) and locator.get("namespace") in (
            "pointt_gateway",
            "local_system_gateway",
        ):
            add(locator.get("value"))
    return found


def _claims(
    hass: HomeAssistant, flow_id: str
) -> Iterator[tuple[FlowManager[Any, Any, Any], str, MutableMapping[str, Any]]]:
    """Cloud setup claims of all other dialogs, live contexts included."""
    for manager in (hass.config_entries.flow, hass.config_entries.options):
        for flow in list(manager.async_progress(include_uninitialized=True)):
            context = cast(MutableMapping[str, Any], flow.get("context", {}))
            if flow["flow_id"] != flow_id and context.get(CLAIM):
                yield (
                    cast(FlowManager[Any, Any, Any], manager),
                    flow["flow_id"],
                    context,
                )


def _orphaned(context: Mapping[str, Any], now: float) -> bool:
    claimed_at = context.get(CLAIMED_AT)
    return (
        isinstance(claimed_at, int | float)
        and now - claimed_at > CLAIM_ORPHANED_SECONDS
    )


def _release(
    manager: FlowManager[Any, Any, Any],
    flow_id: str,
    context: MutableMapping[str, Any],
    *,
    remove: bool,
) -> None:
    """End another dialog's claim; it aborts on its next step or request."""
    context.pop(CLAIM, None)
    context[TAKEN_OVER] = True
    if remove:
        with suppress(UnknownFlow):
            manager.async_abort(flow_id)


def _require_no_cloud(hass: HomeAssistant) -> None:
    if any(
        c.kind == CLOUD_KIND
        for s in cloud_snapshots(hass)
        for c in s.connections.values()
    ):
        raise ConfigurationError("cloud_already_connected")


def renew_cloud_claim(context: MutableMapping[str, Any]) -> bool:
    """Return False once a newer dialog took over; otherwise renew the claim."""
    if context.get(TAKEN_OVER):
        return False
    if context.get(CLAIM):
        context[CLAIMED_AT] = monotonic()
    return True


def claim_cloud_setup(
    hass: HomeAssistant, flow_id: str, context: MutableMapping[str, Any]
) -> None:
    """Claim the only cloud setup; a newly opened dialog replaces older ones.

    The older dialogs abort on their next step, those orphaned are removed at
    once. Registration still checks with `check_new_cloud`.
    """
    if not renew_cloud_claim(context):
        raise ConfigurationError("cloud_setup_taken_over")
    _require_no_cloud(hass)
    now = monotonic()
    for flows, other_id, other in _claims(hass, flow_id):
        _release(flows, other_id, other, remove=_orphaned(other, now))
    context[CLAIM] = True
    context[CLAIMED_AT] = now


def check_new_cloud(hass: HomeAssistant, flow_id: str) -> None:
    """Temporarily forbid parallel cloud registration without an account contract."""
    for manager in (hass.config_entries.flow, hass.config_entries.options):
        for flow in manager.async_progress(include_uninitialized=True):
            if flow["flow_id"] == flow_id and not renew_cloud_claim(
                cast(MutableMapping[str, Any], flow.get("context", {}))
            ):
                raise ConfigurationError("cloud_setup_taken_over")
    _require_no_cloud(hass)
    now = monotonic()
    for flows, other_id, other in _claims(hass, flow_id):
        if not _orphaned(other, now):
            raise ConfigurationError("cloud_setup_in_progress")
        _release(flows, other_id, other, remove=True)


@dataclass(frozen=True, slots=True)
class CloudDiscovery:
    """A short-lived authenticated listing; never public diagnostics."""

    gateways: tuple[Gateway, ...] = field(repr=False)
    devices: tuple[str, ...] = field(repr=False)
    account: str = field(repr=False)
    issued_at: float = field(default_factory=monotonic)

    def validate(self, gateways: set[str], devices: set[str]) -> None:
        if monotonic() - self.issued_at > DISCOVERY_VALID_SECONDS:
            raise ConfigurationError("cloud_selection_expired")
        if not gateways.issubset(
            g.gateway_id for g in self.gateways
        ) or not devices.issubset(self.devices):
            raise ConfigurationError("cloud_selection_changed")


async def discover_connection(
    management: ConfigurationManagement,
    edit: ConnectionEdit,
    *,
    request_guard: Callable[[], None] = lambda: None,
) -> CloudDiscovery:
    def ensure_current() -> None:
        request_guard()
        if management.edit(edit.connection_id) != edit:
            raise ConfigurationError("configuration_changed")

    ensure_current()
    snapshot = management.current()
    if snapshot.connections[edit.connection_id].kind != CLOUD_KIND:
        raise ConfigurationError("unsupported_connection")
    if not section(snapshot.to_options(), "connections")[edit.connection_id]["enabled"]:
        raise ConfigurationError("cloud_connection_disabled")
    session = async_get_clientsession(management.hass)
    tokens, backoff = entry_auth(
        management.hass,
        management.entry,
        OAuthClient(session),
        connection_id=edit.connection_id,
    )
    backoff.raise_if_active()
    resources = connection_resources(
        management.hass, snapshot.owner_id, edit.connection_id
    )
    gateways = await PointTClient(
        session,
        tokens,
        backoff=backoff,
        metrics=resources.metrics,
        request_semaphore=resources.semaphore,
        request_guard=ensure_current,
    ).get_gateways()
    ensure_current()
    backoff.raise_if_active()
    try:
        devices, account = await discover(
            session,
            tokens.tokens,
            token_provider=tokens,
            backoff=backoff,
            request_semaphore=resources.semaphore,
            request_guard=ensure_current,
        )
    except BaconError as error:
        if error.category == "rate_limited":
            backoff.activate(error.retry_after)
        raise
    ensure_current()
    return CloudDiscovery(gateways, devices, account)


def ensure_active_cloud_flow(hass: HomeAssistant, flow_id: str) -> None:
    """A closed dialog must not send queued requests or accept new credentials."""
    if not any(
        flow["flow_id"] == flow_id and not flow.get("context", {}).get(TAKEN_OVER)
        for manager in (hass.config_entries.flow, hass.config_entries.options)
        for flow in manager.async_progress(include_uninitialized=True)
    ):
        raise ConfigurationError("configuration_changed")


def _signatures(target: JsonObject) -> set[tuple[str, str, str]]:
    return {
        (str(v["namespace"]), str(v.get("account_key", "")), str(v["value"]))
        for v in cast(list[JsonObject], target["identities"])
    }


def _identifier(kind: str) -> str:
    return kind + "_" + uuid4().hex


def foreign_identities(hass: HomeAssistant, owner_id: str) -> set[tuple[str, str, str]]:
    """Provider identities stored by any other configuration entry.

    The gateways of an unreadable entry count as claimed: a new setup taking
    them would later collide with its entities.
    """
    claimed: set[tuple[str, str, str]] = set()
    for entry, snapshot in _read_entries(hass):
        if entry.entry_id == owner_id:
            continue
        if snapshot is None:
            claimed.update(
                ("pointt_gateway", "", gateway)
                for gateway in unreadable_gateway_ids(entry.data)
            )
            continue
        for target in snapshot.targets.values():
            claimed.update(_signatures(target.to_data()))
    return claimed


def _merge_targets(
    hass: HomeAssistant,
    owner_id: str,
    data: JsonObject,
    options: JsonObject,
    connection: str,
    gateways: set[str],
    devices: set[str],
    account: str,
    tokens: AuthTokens,
    *,
    proofs: Mapping[str, LocalFirstProof] | None = None,
) -> None:
    """Match only exact provider identities and preserve every existing target."""
    targets, bindings = section(data, "targets"), section(data, "bindings")
    target_options, binding_options = (
        section(options, "targets"),
        section(options, "bindings"),
    )
    foreign = foreign_identities(hass, owner_id)
    # P49b step 8 (8D-a): a plant set up locally in another entry is never
    # claimed by this one; that entry adds the cloud itself.
    elsewhere = {
        str(item["value"])
        for snapshot in cloud_snapshots(hass)
        if snapshot.owner_id != owner_id
        for other in snapshot.targets.values()
        if other.kind == "local_installation"
        for item in cast(list[JsonObject], other.to_data()["identities"])
        if item["namespace"] == "local_system_gateway"
    }
    if gateways & elsewhere:
        raise ConfigurationError("cloud_local_first_other_entry")
    # M1: a plant of this entry joins the cloud under its own target, only
    # after a fresh comparison of both systems (never on the gateway ID alone).
    for gateway, target_id in sorted(
        local_first_targets(data, options, gateways).items()
    ):
        _join_local_first(
            owner_id,
            data,
            options,
            target_id,
            gateway,
            (proofs or {}).get(gateway),
        )
    selected_bindings = set()
    for adapter, namespace, account_key, identifier in [
        *[("pointt", "pointt_gateway", "", v) for v in sorted(gateways)],
        *[("bacon", "bacon_device", account, v) for v in sorted(devices)],
    ]:
        signature = namespace, account_key, identifier
        if signature in foreign:
            raise ConfigurationError("cloud_target_owned")
        found = [
            key for key, target in targets.items() if signature in _signatures(target)
        ]
        locator: JsonObject = {"namespace": namespace, "value": identifier}
        if adapter == "bacon":
            locator["account_key"] = account_key
        if found:
            target_id = found[0]
            if adapter == "pointt":
                # R1 (P49b): the cloud shows the system again.
                target_options[target_id].pop("local_plant", None)
        else:
            target_id = _identifier("target")
            legacy: JsonObject = {"format": "pointt_v3", "gateway_id": identifier}
            if adapter == "bacon":
                legacy = {
                    "format": "bacon_v3",
                    "account_key": account_key,
                    "serial": identifier,
                    "entity_prefix": device_key(
                        token_subject(tokens.access_token), identifier
                    ),
                }
            targets[target_id] = {
                "kind": "pointt_object" if adapter == "pointt" else "standalone_device",
                "identities": [{**locator, "evidence": "authenticated_listing"}],
                "legacy_identity": legacy,
            }
            target_options[target_id] = {"read_order": []}
        existing = [
            key
            for key, b in bindings.items()
            if b["target_id"] == target_id and b["adapter"] == adapter
        ]
        if any(bindings[key]["connection_id"] != connection for key in existing):
            raise ConfigurationError("cloud_target_owned")
        if existing:
            binding_id = existing[0]
        else:
            binding_id = _identifier("binding")
            bindings[binding_id] = {
                "connection_id": connection,
                "target_id": target_id,
                "adapter": adapter,
                "locator": locator,
                "link_evidence": {"basis": "authenticated_listing", "contract": None},
            }
            cast(list[str], target_options[target_id]["read_order"]).append(binding_id)
        selected_bindings.add(binding_id)
    for key, binding in bindings.items():
        if binding["connection_id"] == connection and binding["adapter"] in (
            "pointt",
            "bacon",
        ):
            binding_options[key] = {
                **binding_options.get(key, {}),
                "enabled": key in selected_bindings,
            }


def new_cloud_configuration(
    hass: HomeAssistant,
    owner_id: str,
    flow_id: str,
    brand: Brand,
    tokens: AuthTokens,
    discovery: CloudDiscovery,
    gateways: set[str],
    devices: set[str],
    before: ConfigurationSnapshot | None = None,
    *,
    proofs: Mapping[str, LocalFirstProof] | None = None,
) -> ConfigurationSnapshot:
    check_new_cloud(hass, flow_id)
    discovery.validate(gateways, devices)
    if not gateways and not devices:
        raise ConfigurationError("select_gateway")
    data: JsonObject = (
        before.to_data()
        if before
        else {
            "revision": 0,
            "connections": {},
            "targets": {},
            "bindings": {},
            "legacy_extra": {},
        }
    )
    options: JsonObject = (
        before.to_options()
        if before
        else {"connections": {}, "targets": {}, "bindings": {}}
    )
    connection = require_identifier(
        data.get(
            "default_cloud_connection_id",
            legacy_cloud_connection_id(owner_id)
            if before
            else _identifier("connection"),
        )
    )
    if connection in section(data, "connections"):
        raise ConfigurationError("configuration_changed")
    data["default_cloud_connection_id"] = connection
    section(data, "connections")[connection] = {
        "kind": CLOUD_KIND,
        "brand": brand.value,
        "auth_epoch": 0,
        "config_revision": 0,
        "credentials": tokens_to_data(tokens),
        # Only the hash of the account; a later sign-in must match it.
        "account_evidence": account_evidence(signed_in_account(tokens)),
        "legacy_bacon_account": discovery.account,
    }
    section(options, "connections")[connection] = {
        "enabled": True,
        # I1: a new connection with an iCom gateway starts cloud-friendly;
        # the options change it like any other profile.
        "polling_profile": (
            PollingProfile.CLOUD_FRIENDLY
            if any_icom_gateway(discovery.gateways, gateways)
            else PollingProfile.STANDARD
        ).value,
    }
    _merge_targets(
        hass,
        owner_id,
        data,
        options,
        connection,
        gateways,
        devices,
        discovery.account,
        tokens,
        proofs=proofs,
    )
    return load_configuration(
        owner_id,
        data,
        options,
        minor_version=before.minor_version if before else STORAGE_MINOR_VERSION,
    )


def check_cloud_selection(
    management: ConfigurationManagement,
    edit: ConnectionEdit,
    discovery: CloudDiscovery,
    gateways: set[str],
    devices: set[str],
) -> None:
    """Reject an outdated or changed selection before anything is read."""
    if management.edit(edit.connection_id) != edit:
        raise ConfigurationError("configuration_changed")
    from .configuration_access import EntryConfiguration

    old = EntryConfiguration(management.entry, edit.connection_id).cloud_data()
    # Previously assigned but temporarily unavailable targets remain selectable.
    discovery.validate(
        gateways - set(old["gateway_ids"]), devices - set(old["bacon_device_ids"])
    )
    if old["bacon_device_ids"] and discovery.account != old["bacon_account"]:
        raise ConfigurationError("reauth_targets_missing")


def update_cloud_selection(
    management: ConfigurationManagement,
    edit: ConnectionEdit,
    discovery: CloudDiscovery,
    gateways: set[str],
    devices: set[str],
    *,
    proofs: Mapping[str, LocalFirstProof] | None = None,
) -> ConfigurationSnapshot:
    check_cloud_selection(management, edit, discovery, gateways, devices)
    before = management.current()
    data, options = before.to_data(), before.to_options()
    from .configuration_access import EntryConfiguration

    config = EntryConfiguration(management.entry, edit.connection_id)
    old = config.cloud_data()
    if not old["bacon_device_ids"]:
        section(data, "connections")[edit.connection_id]["legacy_bacon_account"] = (
            discovery.account
        )
    _merge_targets(
        management.hass,
        before.owner_id,
        data,
        options,
        edit.connection_id,
        gateways,
        devices,
        discovery.account,
        config.tokens(),
        proofs=proofs,
    )
    return management.commit_selection(edit, data, options)


def selection_client(
    management: ConfigurationManagement,
    connection_id: str,
    *,
    request_guard: Callable[[], None],
) -> PointTClient:
    """The stored access's client for the comparison reads of a selection."""
    session = async_get_clientsession(management.hass)
    tokens, backoff = entry_auth(
        management.hass,
        management.entry,
        OAuthClient(session),
        connection_id=connection_id,
    )
    backoff.raise_if_active()
    resources = connection_resources(
        management.hass, management.current().owner_id, connection_id
    )
    return PointTClient(
        session,
        tokens,
        backoff=backoff,
        metrics=resources.metrics,
        request_semaphore=resources.semaphore,
        request_guard=request_guard,
    )
