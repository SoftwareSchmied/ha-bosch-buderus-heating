"""P50: recognise an exchanged gateway and continue the same system.

The evidence is a fingerprint of the system's device modules (heat source,
controller, outdoor unit) from the cloud's ``/system/info``. The gateway's own
rows are left out, because exactly they change with an exchange. Only hashes
are stored, never serial numbers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .configuration import (
    CLOUD_KIND,
    LOCAL_KIND,
    ConfigurationError,
    ConfigurationSnapshot,
    JsonObject,
)
from .configuration_management import ConfigurationManagement, ConnectionEdit, section
from .const import DOMAIN, GATEWAY_EXCHANGE_ISSUE_PREFIX
from .repair_scope import repair_data, repair_id

if TYPE_CHECKING:
    from .managed_cloud import CloudConnection
    from .pointt import Gateway

_LOGGER = logging.getLogger(__name__)
FINGERPRINT_VERSION = 1
_FINGERPRINTS = f"{DOMAIN}_module_fingerprints"
_SAVE_DELAY = 10.0
JOURNAL_VERSION = 1
# This Home Assistant process. A journal is deleted only by a later process,
# because registry and entry saves are delayed and can be lost in a crash.
_RUN_ID = uuid4().hex
_JOURNALS = f"{DOMAIN}_gateway_exchange_journals"
_LOCKS = f"{DOMAIN}_gateway_exchange_locks"
ISSUE_TRANSLATION_KEY = "gateway_exchange"


class ModuleEvidence(StrEnum):
    """How strongly the device modules show that two gateways serve one system."""

    MATCH = "match"
    NO_MATCH = "no_match"
    UNKNOWN = "unknown"


def _text(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    return text or None


def module_fingerprint(values: Iterable[object]) -> frozenset[str]:
    """Hashes of the device modules in one ``/system/info`` answer.

    A row counts as a device module when it names a product or a software
    identification and carries a module serial number. The gateway's rows
    carry neither name, so an exchanged gateway does not change the result.
    """
    result: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping):
            continue
        serial = _text(item.get("ModuleSerialNumber"))
        if serial is None or not (
            _text(item.get("ProductName")) or _text(item.get("SwIdenStr"))
        ):
            continue
        parts = (
            _text(item.get("Tok")) or "",
            _text(item.get("Id")) or "",
            serial,
            _text(item.get("ModuleHwIdentStr")) or "",
        )
        result.add(hashlib.sha256("|".join(parts).encode()).hexdigest()[:24])
    return frozenset(result)


def compare_modules(
    stored: frozenset[str] | None, observed: frozenset[str]
) -> ModuleEvidence:
    """At least one shared device module is evidence of the same system."""
    if not stored or not observed:
        return ModuleEvidence.UNKNOWN
    return ModuleEvidence.MATCH if stored & observed else ModuleEvidence.NO_MATCH


@dataclass(frozen=True, slots=True)
class CounterRecord:
    """How one counter entity continued after the exchange at ``at`` (4B)."""

    at: str
    settled: bool
    last_reset: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {"at": self.at, "settled": self.settled, "last_reset": self.last_reset}

    @classmethod
    def from_data(cls, data: object) -> CounterRecord | None:
        if not isinstance(data, Mapping):
            return None
        at, settled, last_reset = (
            data.get("at"),
            data.get("settled"),
            data.get("last_reset"),
        )
        if (
            not isinstance(at, str)
            or not isinstance(settled, bool)
            or not (last_reset is None or isinstance(last_reset, str))
        ):
            return None
        return cls(at, settled, last_reset)


class ModuleFingerprints:
    """The last fingerprint per stored target, private and without serials."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass,
            FINGERPRINT_VERSION,
            f"{DOMAIN}.module_fingerprints.{entry_id}",
            private=True,
        )
        self._targets: dict[str, dict[str, Any]] = {}
        # "binding|new gateway" pairs the user kept separate (3C).
        self._declined: set[str] = set()
        # The time of each confirmed exchange per target, for later steps.
        self._exchanges: dict[str, dict[str, Any]] = {}
        # Per counter entity (new unique_id): settled after an exchange (4B).
        self._counters: dict[str, CounterRecord] = {}
        self._loaded = False
        self._load_lock = asyncio.Lock()

    async def async_load(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            # Many entities may ask at once; only the first one reads.
            if not self._loaded:
                await self._async_read()

    async def _async_read(self) -> None:
        try:
            data = await self._store.async_load()
        except Exception:
            data = None
        if not isinstance(data, dict):
            data = {}
        counters = data.get("counters")
        if isinstance(counters, dict):
            for key, value in counters.items():
                record = CounterRecord.from_data(value)
                if isinstance(key, str) and record is not None:
                    self._counters[key] = record
        declined = data.get("declined")
        if isinstance(declined, list):
            self._declined = {item for item in declined if isinstance(item, str)}
        exchanges = data.get("exchanges")
        if isinstance(exchanges, dict):
            self._exchanges = {
                key: {"at": value["at"]}
                for key, value in exchanges.items()
                if isinstance(key, str)
                and isinstance(value, dict)
                and isinstance(value.get("at"), str)
            }
        targets = data.get("targets")
        if isinstance(targets, dict):
            for target_id, item in targets.items():
                modules = item.get("modules") if isinstance(item, dict) else None
                if (
                    isinstance(target_id, str)
                    and isinstance(modules, list)
                    and all(isinstance(value, str) for value in modules)
                ):
                    self._targets[target_id] = {
                        "modules": sorted(set(modules)),
                        "seen": item.get("seen"),
                    }
        self._loaded = True

    def get(self, target_id: str) -> frozenset[str] | None:
        item = self._targets.get(target_id)
        return frozenset(item["modules"]) if item else None

    def observe(self, target_id: str, values: Iterable[object]) -> bool:
        """Remember a fresh answer; True when the stored fingerprint changed."""
        if not self._loaded:
            return False  # never overwrite what has not been read yet
        modules = module_fingerprint(values)
        if not modules:
            return False
        previous = self._targets.get(target_id)
        if previous is not None and set(previous["modules"]) == modules:
            return False
        self._targets[target_id] = {
            "modules": sorted(modules),
            "seen": datetime.now(UTC).isoformat(),
        }
        self._store.async_delay_save(self._data, _SAVE_DELAY)
        return True

    def forget(self, target_id: str) -> None:
        if self._targets.pop(target_id, None) is not None:
            self._store.async_delay_save(self._data, _SAVE_DELAY)

    def declined(self, binding_id: str, gateway_id: str) -> bool:
        return f"{binding_id}|{gateway_id}" in self._declined

    async def async_decline(self, binding_id: str, gateway_id: str) -> None:
        """Remember that this new gateway is a separate system (saved now)."""
        await self.async_load()
        self._declined.add(f"{binding_id}|{gateway_id}")
        await self._store.async_save(self._data())

    def exchange(self, target_id: str) -> str | None:
        item = self._exchanges.get(target_id)
        return item["at"] if item else None

    async def async_record_exchange(self, target_id: str, at: str) -> None:
        """Remember when the gateway of a target was exchanged (saved now)."""
        await self.async_load()
        if self._exchanges.get(target_id) == {"at": at}:
            return
        self._exchanges[target_id] = {"at": at}
        await self._store.async_save(self._data())

    def counter(self, unique_id: str) -> CounterRecord | None:
        return self._counters.get(unique_id)

    def set_counter(
        self, unique_id: str, record: CounterRecord, *, immediate: bool = False
    ) -> None:
        """Remember a counter's decision; ``immediate`` saves it right away."""
        if not self._loaded or self._counters.get(unique_id) == record:
            return
        self._counters[unique_id] = record
        if immediate:
            self._hass.async_create_task(
                self._store.async_save(self._data()), "save exchanged counter"
            )
        else:
            self._store.async_delay_save(self._data, _SAVE_DELAY)

    def _data(self) -> dict[str, Any]:
        return {
            "targets": self._targets,
            "declined": sorted(self._declined),
            "exchanges": self._exchanges,
            "counters": {
                key: record.to_data() for key, record in self._counters.items()
            },
        }

    async def async_remove(self) -> None:
        await self._store.async_remove()


def module_fingerprints(hass: HomeAssistant, entry_id: str) -> ModuleFingerprints:
    """Shared per entry, like the discovery inventory."""
    stores: dict[str, ModuleFingerprints] = hass.data.setdefault(_FINGERPRINTS, {})
    if (store := stores.get(entry_id)) is None:
        store = stores[entry_id] = ModuleFingerprints(hass, entry_id)
    return store


async def async_remove_module_fingerprints(hass: HomeAssistant, entry_id: str) -> None:
    store = hass.data.get(_FINGERPRINTS, {}).pop(entry_id, None)
    if store is None:
        store = ModuleFingerprints(hass, entry_id)
    await store.async_remove()


# ---------------------------------------------------------------- detection


def short_id(gateway_id: str) -> str:
    """Only the last four characters of a gateway ID appear in texts."""
    return gateway_id[-4:]


def exchange_issue_id(
    entry: ConfigEntry[Any], connection_id: str, binding_id: str
) -> str:
    return repair_id(GATEWAY_EXCHANGE_ISSUE_PREFIX, entry, connection_id, binding_id)


def _locator(snapshot: ConfigurationSnapshot, binding_id: str) -> str:
    return cast(
        str,
        cast(JsonObject, snapshot.bindings[binding_id].to_data()["locator"])["value"],
    )


def exchange_candidates(
    hass: HomeAssistant,
    snapshot: ConfigurationSnapshot,
    gateways: Iterable[Gateway],
) -> tuple[Gateway, ...]:
    """Listed gateways that could be the new gateway of a system.

    No wallbox, nothing this entry knows already (as a binding or as a stored
    identity of any target) and nothing another entry owns.
    """
    from .cloud_configuration import foreign_identities
    from .wallbox import is_wallbox

    known = {
        cast(str, item["value"])
        for target in snapshot.targets.values()
        for item in cast(list[JsonObject], target.to_data()["identities"])
        if item["namespace"] == "pointt_gateway"
    } | {
        _locator(snapshot, key)
        for key, binding in snapshot.bindings.items()
        if binding.adapter == "pointt"
    }
    foreign = {
        value
        for namespace, _, value in foreign_identities(hass, snapshot.owner_id)
        if namespace == "pointt_gateway"
    }
    return tuple(
        gateway
        for gateway in gateways
        if not is_wallbox(gateway)
        and gateway.gateway_id not in known
        and gateway.gateway_id not in foreign
    )


def exchange_candidate(
    hass: HomeAssistant,
    snapshot: ConfigurationSnapshot,
    connection_id: str,
    binding_id: str,
    gateways: Iterable[Gateway],
    *,
    old_gateway: Gateway | None = None,
) -> str | None:
    """The one new gateway that can replace this binding's missing one.

    Exactly one enabled system of this access is missing from the list and
    exactly one listed gateway is free; anything else stays undecided.
    """
    from .wallbox import is_wallbox

    listed = tuple(gateways)
    visible = {gateway.gateway_id for gateway in listed}
    options = section(snapshot.to_options(), "bindings")
    missing = [
        key
        for key, binding in snapshot.bindings.items()
        if binding.connection_id == connection_id
        and binding.adapter == "pointt"
        and options[key]["enabled"]
        and _locator(snapshot, key) not in visible
    ]
    if missing != [binding_id]:
        _LOGGER.debug("Gateway exchange undecided: %s systems missing", len(missing))
        return None
    if old_gateway is not None and is_wallbox(old_gateway):
        return None
    candidates = exchange_candidates(hass, snapshot, listed)
    if len(candidates) != 1:
        _LOGGER.debug(
            "Gateway exchange undecided: %s possible new gateways", len(candidates)
        )
        return None
    return candidates[0].gateway_id


@callback
def sync_exchange_issue(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    snapshot: ConfigurationSnapshot,
    connection_id: str,
    binding_id: str,
    gateways: Iterable[Gateway],
    fingerprints: ModuleFingerprints,
    *,
    old_gateway: Gateway | None = None,
) -> str | None:
    """Raise or clear "Gateway replaced?" from the list already fetched.

    Never sends a request. Returns the new gateway when the issue is shown.
    """
    issue_id = exchange_issue_id(entry, connection_id, binding_id)
    new = exchange_candidate(
        hass, snapshot, connection_id, binding_id, gateways, old_gateway=old_gateway
    )
    if new is None or fingerprints.declined(binding_id, new):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return None
    old = _locator(snapshot, binding_id)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_TRANSLATION_KEY,
        translation_placeholders={"old": short_id(old), "new": short_id(new)},
        data={
            **repair_data(entry, connection_id, binding_id),
            "new_gateway": new,
        },
    )
    return new


@callback
def clear_exchange_issue(
    hass: HomeAssistant, entry: ConfigEntry[Any], connection_id: str, binding_id: str
) -> None:
    ir.async_delete_issue(
        hass, DOMAIN, exchange_issue_id(entry, connection_id, binding_id)
    )


@callback
def clear_entry_exchange_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Every "Gateway replaced?" of a removed entry, loaded or not."""
    registry = ir.async_get(hass)
    for domain, issue_id in tuple(registry.issues):
        issue = registry.issues[(domain, issue_id)]
        if (
            domain == DOMAIN
            and issue_id.startswith(GATEWAY_EXCHANGE_ISSUE_PREFIX)
            and (issue.data or {}).get("entry_id") == entry_id
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


# ------------------------------------------------------------ confirmation


class GatewayExchangeError(Exception):
    """A static reason, shown as the abort reason of the repair."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ExchangeCheck:
    """A re-verified exchange, ready for the user's confirmation."""

    connection_id: str
    binding_id: str
    target_id: str
    old: str
    new: str
    evidence: ModuleEvidence
    edit: ConnectionEdit


def _system_values(system: object) -> list[object]:
    values = system.get("values") if isinstance(system, Mapping) else None
    return list(values) if isinstance(values, list) else []


async def async_verify_exchange_list(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    connection_id: str,
    binding_id: str,
    new: str,
) -> CloudConnection:
    """The running access's gateway list still shows exactly this exchange.

    The old gateway is absent, the new one is listed and still free. Returns
    the running cloud connection.
    """
    from .discovery_inventory import discovery_inventory
    from .managed_cloud import CloudConnection
    from .managed_entry import ManagedEntry
    from .pointt.exceptions import PointTError

    runtime = getattr(entry, "runtime_data", None)
    if entry.version != 4 or not isinstance(runtime, ManagedEntry):
        raise GatewayExchangeError("exchange_unavailable")
    driver = runtime.connections.connection_driver(connection_id)
    if not isinstance(driver, CloudConnection):
        raise GatewayExchangeError("cloud_unavailable")
    try:
        gateways = await driver.gateways()
    except PointTError:
        raise GatewayExchangeError("cloud_unavailable") from None
    try:
        snapshot = ConfigurationManagement(hass, entry).current()
        if binding_id not in snapshot.bindings:
            raise GatewayExchangeError("configuration_changed")
        inventory = discovery_inventory(hass, entry.entry_id)
        await inventory.async_load()
        stored = inventory.gateway(_locator(snapshot, binding_id))
        candidate = exchange_candidate(
            hass,
            snapshot,
            connection_id,
            binding_id,
            gateways,
            old_gateway=stored.gateway if stored is not None else None,
        )
    except ConfigurationError:
        raise GatewayExchangeError("configuration_changed") from None
    if candidate != new:
        raise GatewayExchangeError("exchange_changed")
    return driver


async def async_check_exchange(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    connection_id: str,
    binding_id: str,
    new: str,
) -> ExchangeCheck:
    """Re-verify the situation and read the evidence once.

    The gateway list of the running access (shared with its sources) and
    two reads of the new gateway: its metadata and its ``/system/info``.
    Nothing is written. A cloud that cannot answer now (rate limit,
    connection, sign-in) aborts; missing module data means UNKNOWN.
    """
    from .pointt.exceptions import PointTError, ProtocolError, ResourceError

    management = ConfigurationManagement(hass, entry)
    try:
        snapshot = management.current()
        binding = snapshot.bindings.get(binding_id)
        connection = snapshot.connections.get(connection_id)
        options = snapshot.to_options()
        if (
            binding is None
            or connection is None
            or binding.connection_id != connection_id
            or binding.adapter != "pointt"
            or connection.kind != CLOUD_KIND
            or not section(options, "bindings")[binding_id]["enabled"]
            or not section(options, "connections")[connection_id]["enabled"]
        ):
            raise GatewayExchangeError("configuration_changed")
        edit = management.edit(connection_id)
    except ConfigurationError:
        raise GatewayExchangeError("configuration_changed") from None
    old = _locator(snapshot, binding_id)
    driver = await async_verify_exchange_list(
        hass, entry, connection_id, binding_id, new
    )
    fingerprints = module_fingerprints(hass, entry.entry_id)
    await fingerprints.async_load()
    try:
        _, system = await driver.client.get_local_identity_evidence(new)
        observed = module_fingerprint(_system_values(system))
    except ResourceError, ProtocolError:
        observed = frozenset()  # the new gateway reports no device modules
    except PointTError:
        # Rate limit, connection or sign-in: try later, never "unverified".
        raise GatewayExchangeError("cloud_unavailable") from None
    try:
        unchanged = management.edit(connection_id) == edit
    except ConfigurationError:
        unchanged = False
    if not unchanged:
        raise GatewayExchangeError("configuration_changed")
    return ExchangeCheck(
        connection_id,
        binding_id,
        binding.target_id,
        old,
        new,
        compare_modules(fingerprints.get(binding.target_id), observed),
        edit,
    )


# --------------------------------------------------------------- migration


@dataclass(frozen=True, slots=True)
class ExchangePlan:
    """What an exchange changes, with the values before (journal content)."""

    attempt: str
    entry_id: str
    connection_id: str
    binding_id: str
    target_id: str
    old: str
    new: str
    at: str
    entities: tuple[tuple[str, str, str], ...]
    device_id: str | None
    device_identifiers: tuple[tuple[str, str], ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "entry_id": self.entry_id,
            "connection_id": self.connection_id,
            "binding_id": self.binding_id,
            "target_id": self.target_id,
            "old": self.old,
            "new": self.new,
            "at": self.at,
            "entities": [list(item) for item in self.entities],
            "device_id": self.device_id,
            "device_identifiers": [list(item) for item in self.device_identifiers],
        }

    @classmethod
    def from_data(cls, data: object) -> ExchangePlan | None:
        if not isinstance(data, Mapping):
            return None
        names = (
            "attempt",
            "entry_id",
            "connection_id",
            "binding_id",
            "target_id",
            "old",
            "new",
            "at",
        )
        values = [data.get(name) for name in names]
        entities = data.get("entities")
        identifiers = data.get("device_identifiers")
        device_id = data.get("device_id")
        if (
            not all(isinstance(value, str) and value for value in values)
            or not isinstance(entities, list)
            or not isinstance(identifiers, list)
            or not (device_id is None or isinstance(device_id, str))
        ):
            return None

        def rows(items: list[Any], size: int) -> tuple[tuple[str, ...], ...] | None:
            if not all(
                isinstance(item, list)
                and len(item) == size
                and all(isinstance(part, str) for part in item)
                for item in items
            ):
                return None
            return tuple(tuple(item) for item in items)

        moved, before = rows(entities, 3), rows(identifiers, 2)
        if moved is None or before is None:
            return None
        attempt, entry_id, connection_id, binding_id, target_id, old, new, at = cast(
            list[str], values
        )
        return cls(
            attempt=attempt,
            entry_id=entry_id,
            connection_id=connection_id,
            binding_id=binding_id,
            target_id=target_id,
            old=old,
            new=new,
            at=at,
            entities=cast(tuple[tuple[str, str, str], ...], moved),
            device_id=device_id,
            device_identifiers=cast(tuple[tuple[str, str], ...], before),
        )


@dataclass(frozen=True, slots=True)
class JournalState:
    """The plan and how far it got.

    ``applied_run`` names the Home Assistant process that applied the
    registry and configuration change. Only a later process may delete the
    journal, after it found the change in what it loaded from disk.
    """

    plan: ExchangePlan
    applied_run: str | None = None
    finished: bool = False


class ExchangeJournal:
    """Private, immediately saved record of one running exchange.

    Every operation uses a fresh store, so a read never returns data that a
    store object kept from an earlier save or load.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass, self._key = hass, f"{DOMAIN}.gateway_exchange.{entry_id}"

    def _store(self) -> Store[dict[str, Any]]:
        return Store(
            self._hass, JOURNAL_VERSION, self._key, private=True, atomic_writes=True
        )

    async def async_load(self) -> JournalState | None:
        try:
            data = await self._store().async_load()
        except Exception:
            _LOGGER.error("The gateway exchange journal could not be read")
            return None
        if not isinstance(data, dict):
            return None
        plan = ExchangePlan.from_data(data.get("plan"))
        applied, finished = data.get("applied_run"), data.get("finished", False)
        if (
            plan is None
            or not (applied is None or isinstance(applied, str))
            or not isinstance(finished, bool)
        ):
            return None
        return JournalState(plan, applied, finished)

    async def async_save(
        self,
        plan: ExchangePlan,
        *,
        applied_run: str | None = None,
        finished: bool = False,
    ) -> None:
        await self._store().async_save(
            {"plan": plan.to_data(), "applied_run": applied_run, "finished": finished}
        )

    async def async_remove(self, attempt: str | None = None) -> None:
        """Remove the journal; with ``attempt`` only this exchange's journal."""
        if attempt is not None:
            state = await self.async_load()
            if state is None or state.plan.attempt != attempt:
                return
        await self._store().async_remove()


def exchange_journal(hass: HomeAssistant, entry_id: str) -> ExchangeJournal:
    journals = hass.data.setdefault(_JOURNALS, {})
    if (journal := journals.get(entry_id)) is None:
        journal = journals[entry_id] = ExchangeJournal(hass, entry_id)
    return cast(ExchangeJournal, journal)


async def async_remove_exchange_journal(hass: HomeAssistant, entry_id: str) -> None:
    journal = hass.data.get(_JOURNALS, {}).pop(entry_id, None)
    await (journal or ExchangeJournal(hass, entry_id)).async_remove()


def _registry_moves(
    hass: HomeAssistant, entry_id: str, old: str, new: str
) -> list[tuple[str, str, str, str]]:
    """(entity_id, domain, old unique_id, new unique_id) of the old gateway."""
    prefix = f"{old}:"
    return [
        (item.entity_id, item.domain, item.unique_id, new + item.unique_id[len(old) :])
        for item in er.async_entries_for_config_entry(er.async_get(hass), entry_id)
        if item.platform == DOMAIN and item.unique_id.startswith(prefix)
    ]


def _devices(
    hass: HomeAssistant, entry_id: str, old: str, new: str
) -> tuple[dr.DeviceEntry | None, dr.DeviceEntry | None]:
    from .ha_compat import device_by_identifier

    registry = dr.async_get(hass)
    return (
        device_by_identifier(registry, (DOMAIN, old), entry_id),
        device_by_identifier(registry, (DOMAIN, new), entry_id),
    )


def _check_collisions(
    hass: HomeAssistant,
    entry_id: str,
    old: str,
    new: str,
    moves: list[tuple[str, str, str, str]],
) -> None:
    """Nothing may already carry an identity the exchange would assign."""
    from .ha_compat import identifier_owned_elsewhere

    registry = er.async_get(hass)
    if any(
        registry.async_get_entity_id(domain, DOMAIN, new_unique_id) is not None
        for _, domain, _, new_unique_id in moves
    ):
        raise GatewayExchangeError("exchange_collision")
    old_device, new_device = _devices(hass, entry_id, old, new)
    if old_device is not None and (
        (new_device is not None and new_device.id != old_device.id)
        or identifier_owned_elsewhere(dr.async_get(hass), (DOMAIN, new), entry_id)
    ):
        raise GatewayExchangeError("exchange_collision")


def plan_exchange(
    hass: HomeAssistant, entry: ConfigEntry[Any], check: ExchangeCheck
) -> ExchangePlan:
    """The pure plan; raises without any change if an identity is taken."""
    moves = _registry_moves(hass, entry.entry_id, check.old, check.new)
    _check_collisions(hass, entry.entry_id, check.old, check.new, moves)
    device, _ = _devices(hass, entry.entry_id, check.old, check.new)
    return ExchangePlan(
        uuid4().hex,
        entry.entry_id,
        check.connection_id,
        check.binding_id,
        check.target_id,
        check.old,
        check.new,
        datetime.now(UTC).isoformat(),
        tuple((entity_id, old, new) for entity_id, _, old, new in moves),
        device.id if device is not None else None,
        tuple(sorted(device.identifiers)) if device is not None else (),
    )


async def _settled[T](task: asyncio.Task[T]) -> T:
    from .local_handover import _settled as settled

    return await settled(task)


async def async_exchange_gateway(
    hass: HomeAssistant, entry: ConfigEntry[Any], check: ExchangeCheck
) -> None:
    """Continue the system with the new gateway after the user's confirmation.

    Re-checks the gateway list, stops the entry, rewrites the registry and the
    configuration in one step without an await, then removes the old local
    access and starts the entry again. The journal lets a later start finish
    an interrupted exchange.
    """
    from .auth_lifecycle import async_wait_for_entry_refresh

    locks = cast(dict[str, asyncio.Lock], hass.data.setdefault(_LOCKS, {}))
    async with locks.setdefault(entry.entry_id, asyncio.Lock()):
        management = ConfigurationManagement(hass, entry)

        def ensure_unchanged() -> None:
            try:
                if management.edit(check.connection_id) != check.edit:
                    raise GatewayExchangeError("configuration_changed")
            except ConfigurationError:
                raise GatewayExchangeError("configuration_changed") from None

        ensure_unchanged()
        await async_verify_exchange_list(
            hass, entry, check.connection_id, check.binding_id, check.new
        )
        ensure_unchanged()
        plan = plan_exchange(hass, entry, check)
        journal = exchange_journal(hass, entry.entry_id)
        await journal.async_save(plan)
        stopped = False
        try:
            try:
                await async_wait_for_entry_refresh(hass, entry.entry_id)
                ensure_unchanged()
                if entry.state is ConfigEntryState.LOADED:
                    stopped = True
                    if not await _settled(
                        hass.async_create_task(
                            hass.config_entries.async_unload(entry.entry_id),
                            "unload entry for gateway exchange",
                        )
                    ):
                        raise GatewayExchangeError("unload_failed")
                elif entry.state is not ConfigEntryState.NOT_LOADED:
                    raise GatewayExchangeError("unload_failed")
                await _async_complete(hass, entry, plan, journal)
            except BaseException:
                if not _configuration_done(hass, entry, plan):
                    await journal.async_remove(plan.attempt)
                raise
        finally:
            if stopped:

                async def restore() -> None:
                    if (
                        hass.config_entries.async_get_entry(entry.entry_id) is entry
                        and entry.disabled_by is None
                        and entry.state is ConfigEntryState.NOT_LOADED
                    ):
                        await hass.config_entries.async_setup(entry.entry_id)

                await _settled(
                    hass.async_create_task(
                        restore(), "restore entry after gateway exchange"
                    )
                )


def _configuration_done(
    hass: HomeAssistant, entry: ConfigEntry[Any], plan: ExchangePlan
) -> bool:
    try:
        snapshot = ConfigurationManagement(hass, entry).current()
    except ConfigurationError:
        return False
    return (
        plan.binding_id in snapshot.bindings
        and _locator(snapshot, plan.binding_id) == plan.new
    )


def _state_done(
    hass: HomeAssistant, entry: ConfigEntry[Any], plan: ExchangePlan
) -> bool:
    """Registry and configuration show the exchange completely."""
    return (
        _configuration_done(hass, entry, plan)
        and not _registry_moves(hass, entry.entry_id, plan.old, plan.new)
        and _devices(hass, entry.entry_id, plan.old, plan.new)[0] is None
    )


async def async_resume_gateway_exchange(
    hass: HomeAssistant, entry: ConfigEntry[Any]
) -> None:
    """Finish an interrupted exchange before the entry's sources start.

    Each part is skipped when already done. The journal is deleted only by a
    later Home Assistant process that loaded the finished change from disk.
    If the exchange cannot be finished, the entry does not start, so no
    source can create entities next to the half-moved ones.
    """
    from homeassistant.exceptions import ConfigEntryError

    journal = exchange_journal(hass, entry.entry_id)
    state = await journal.async_load()
    if state is None:
        return
    plan = state.plan
    if plan.entry_id != entry.entry_id:
        await journal.async_remove()
        return
    if state.applied_run is not None and _state_done(hass, entry, plan):
        if state.finished and state.applied_run == _RUN_ID:
            return  # this process applied it; a later start deletes it
        if not state.finished and not await _async_finish(hass, entry, plan):
            return  # retried at the next start
        if state.applied_run != _RUN_ID:
            await journal.async_remove(plan.attempt)
        else:
            await journal.async_save(plan, applied_run=_RUN_ID, finished=True)
        return
    try:
        await _async_complete(hass, entry, plan, journal)
    except Exception as error:
        reason = (
            error.reason if isinstance(error, GatewayExchangeError) else "unexpected"
        )
        _LOGGER.error(
            "An interrupted gateway exchange could not be finished (%s); the "
            "entry does not start and the journal is kept",
            reason,
        )
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="gateway_exchange_incomplete",
        ) from error


def _local_connections(snapshot: ConfigurationSnapshot, target_id: str) -> set[str]:
    return {
        binding.connection_id
        for binding in snapshot.bindings.values()
        if binding.target_id == target_id
        and snapshot.connections[binding.connection_id].kind == LOCAL_KIND
    }


async def _async_complete(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    plan: ExchangePlan,
    journal: ExchangeJournal,
) -> None:
    management = ConfigurationManagement(hass, entry)
    try:
        snapshot = management.current()
    except ConfigurationError:
        raise GatewayExchangeError("configuration_changed") from None
    binding = snapshot.bindings.get(plan.binding_id)
    if (
        binding is None
        or binding.target_id != plan.target_id
        or binding.connection_id != plan.connection_id
        or _locator(snapshot, plan.binding_id) not in (plan.old, plan.new)
    ):
        raise GatewayExchangeError("configuration_changed")
    _apply(hass, entry, plan)
    await journal.async_save(plan, applied_run=_RUN_ID)
    if await _async_finish(hass, entry, plan):
        await journal.async_save(plan, applied_run=_RUN_ID, finished=True)


@callback
def _apply(hass: HomeAssistant, entry: ConfigEntry[Any], plan: ExchangePlan) -> None:
    """Registry and configuration in one step without an await."""
    moves = _registry_moves(hass, entry.entry_id, plan.old, plan.new)
    _check_collisions(hass, entry.entry_id, plan.old, plan.new, moves)
    entities, devices = er.async_get(hass), dr.async_get(hass)
    management = ConfigurationManagement(hass, entry)
    moved: list[tuple[str, str]] = []
    device_before: tuple[str, set[tuple[str, str]]] | None = None
    try:
        for entity_id, _, old_unique_id, new_unique_id in moves:
            entities.async_update_entity(entity_id, new_unique_id=new_unique_id)
            moved.append((entity_id, old_unique_id))
        device, _ = _devices(hass, entry.entry_id, plan.old, plan.new)
        if device is not None:
            device_before = device.id, set(device.identifiers)
            devices.async_update_device(
                device.id,
                new_identifiers=(set(device.identifiers) - {(DOMAIN, plan.old)})
                | {(DOMAIN, plan.new)},
            )
        if not _configuration_done(hass, entry, plan):
            management.exchange_gateway(plan.binding_id, plan.old, plan.new)
    except Exception as error:
        _revert(hass, moved, device_before)
        raise GatewayExchangeError("exchange_failed") from error


def _revert(
    hass: HomeAssistant,
    moved: list[tuple[str, str]],
    device: tuple[str, set[tuple[str, str]]] | None,
) -> None:
    try:
        if device is not None:
            dr.async_get(hass).async_update_device(device[0], new_identifiers=device[1])
        registry = er.async_get(hass)
        for entity_id, unique_id in reversed(moved):
            registry.async_update_entity(entity_id, new_unique_id=unique_id)
    except Exception:
        _LOGGER.exception("A failed gateway exchange could not be reverted fully")


async def _async_remove_local_access(
    hass: HomeAssistant, entry: ConfigEntry[Any], target_id: str
) -> None:
    """The local access belongs to the old gateway: its token and certificate
    pins are void. Pending enrollments are fenced like in the options."""
    from .local_enrollment import enrollment_journal

    management = ConfigurationManagement(hass, entry)
    for connection_id in sorted(_local_connections(management.current(), target_id)):
        await enrollment_journal(hass).remove_access(
            management, management.edit(connection_id)
        )
    management.drop_local_identities(target_id)


async def _async_copy_store(
    hass: HomeAssistant, version: int, old_key: str, new_key: str
) -> None:
    """Continue a per-gateway store under the new gateway's key.

    The old gateway's file is deleted once the new one holds the data
    (FAULT-5); a repeated run then finds nothing to copy.
    """
    source = Store[dict[str, Any]](hass, version, old_key, private=True)
    data = await source.async_load()
    if data is None:
        return
    target = Store[dict[str, Any]](hass, version, new_key, private=True)
    if await target.async_load() is None:
        await target.async_save(data)
    await source.async_remove()


async def _async_finish(
    hass: HomeAssistant, entry: ConfigEntry[Any], plan: ExchangePlan
) -> bool:
    """Follow-up work while the entry is stopped; each part is repeatable.

    True when every part succeeded; otherwise the next start repeats them.
    """
    from homeassistant.components import persistent_notification

    from .connection_inventory import ConnectionInventory
    from .counter_selection import counter_source_store
    from .faults import FAULT_STORAGE_VERSION, fault_store_key
    from .notifications import notification_id

    entry_id = entry.entry_id
    steps: tuple[tuple[str, Callable[[], Any]], ...] = (
        (
            "local access",
            lambda: _async_remove_local_access(hass, entry, plan.target_id),
        ),
        (
            "inventory",
            lambda: ConnectionInventory(hass, entry).rekey(
                plan.binding_id, f"{plan.old}:", f"{plan.new}:"
            ),
        ),
        (
            "faults",
            lambda: _async_copy_store(
                hass,
                FAULT_STORAGE_VERSION,
                fault_store_key(entry_id, plan.old),
                fault_store_key(entry_id, plan.new),
            ),
        ),
        (
            "notifications",
            lambda: _async_copy_store(
                hass,
                1,
                notification_id(entry_id, plan.old),
                notification_id(entry_id, plan.new),
            ),
        ),
        ("discovery", lambda: _drop_discovery(hass, entry_id, plan.old)),
        (
            "exchange time",
            lambda: module_fingerprints(hass, entry_id).async_record_exchange(
                plan.target_id, plan.at
            ),
        ),
        (
            # The last shown source says nothing about the new gateway's
            # counters; the exchange's counter rule decides alone.
            "counter sources",
            lambda: counter_source_store(hass).async_discard_target(
                entry_id, plan.target_id
            ),
        ),
    )
    complete = True
    for name, step in steps:
        try:
            await step()
        except Exception:
            complete = False
            _LOGGER.warning("Gateway exchange follow-up failed: %s", name)
    clear_exchange_issue(hass, entry, plan.connection_id, plan.binding_id)
    persistent_notification.async_dismiss(hass, notification_id(entry_id, plan.old))
    return complete


async def _drop_discovery(hass: HomeAssistant, entry_id: str, gateway_id: str) -> None:
    from .discovery_inventory import discovery_inventory

    inventory = discovery_inventory(hass, entry_id)
    await inventory.async_load()
    inventory.drop(gateway_id)
