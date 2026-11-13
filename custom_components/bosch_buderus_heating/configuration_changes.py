"""Classify storage changes and invalidate only their affected request scopes.

This is the pure version 4 dispatch contract used by managed runtime consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from .configuration import (
    LOCAL_KIND,
    ConfigurationError,
    ConfigurationSnapshot,
    JsonObject,
)


@dataclass(frozen=True, slots=True)
class ConfigurationChanges:
    """Changed views and invalidated source scopes; IDs are private.

    Targets include read-order changes for view consumers. Bindings identify
    retired source work; a view preference alone does not retire a reader.
    """

    connections: frozenset[str] = field(repr=False)
    bindings: frozenset[str] = field(repr=False)
    targets: frozenset[str] = field(repr=False)
    tokens: frozenset[str] = field(repr=False)
    display_options: bool


def _section(data: JsonObject, key: str) -> dict[str, JsonObject]:
    """The immutable snapshot already validated these structural sections."""
    return cast(dict[str, JsonObject], data[key])


def _connection_parameters(connection: JsonObject) -> JsonObject:
    return {key: value for key, value in connection.items() if key != "credentials"}


# Known view preferences change how retained data is shown, not source lifetime.
# "local_plant" (R1, P49b) only decides whether a target view is shown.
_TARGET_VIEW_OPTIONS = frozenset({"read_order", "local_values", "local_plant"})


def _target_source_options(options: JsonObject) -> JsonObject:
    """Only known view preferences are independent of source lifetime.

    Unknown future options remain conservative until their semantics are known.
    """
    return {
        key: value for key, value in options.items() if key not in _TARGET_VIEW_OPTIONS
    }


def configuration_changes(
    before: ConfigurationSnapshot, after: ConfigurationSnapshot
) -> ConfigurationChanges:
    """Ignore revision-only and token-only updates when deciding what to restart."""
    if before.owner_id != after.owner_id:
        raise ConfigurationError("owner_changed")
    left, right = before.to_data(), after.to_data()
    old_options, new_options = before.to_options(), after.to_options()
    connections: set[str] = set()
    tokens: set[str] = set()
    for key in before.connections.keys() | after.connections.keys():
        old, new = before.connections.get(key), after.connections.get(key)
        if old is None or new is None:
            connections.add(key)
            continue
        a, b = old.to_data(), new.to_data()
        if (
            _connection_parameters(a) != _connection_parameters(b)
            or _section(old_options, "connections")[key]
            != _section(new_options, "connections")[key]
        ):
            connections.add(key)
        if a.get("credentials") != b.get("credentials"):
            tokens.add(key)
            # Local tokens have no ordinary refresh path. Any replacement must
            # invalidate in-flight reads, including data loaded outside a dialog.
            if new.kind == LOCAL_KIND:
                connections.add(key)
    bindings = {
        key
        for key in before.bindings.keys() | after.bindings.keys()
        if _section(left, "bindings").get(key) != _section(right, "bindings").get(key)
        or _section(old_options, "bindings").get(key)
        != _section(new_options, "bindings").get(key)
    }
    targets = {
        key
        for key in before.targets.keys() | after.targets.keys()
        if _section(left, "targets").get(key) != _section(right, "targets").get(key)
        or _section(old_options, "targets").get(key)
        != _section(new_options, "targets").get(key)
    }
    source_targets = {
        key
        for key in targets
        if _section(left, "targets").get(key) != _section(right, "targets").get(key)
        or _target_source_options(_section(old_options, "targets").get(key, {}))
        != _target_source_options(_section(new_options, "targets").get(key, {}))
    }
    # Removing a binding also changes read_order, but must not retire its peers.
    # Identity and other target-context changes still invalidate all its sources.
    for snapshot in (before, after):
        bindings.update(
            key
            for key, binding in snapshot.bindings.items()
            if binding.target_id in source_targets
        )
    reserved = {"connections", "bindings", "targets"}
    display_changed = {
        key: value for key, value in old_options.items() if key not in reserved
    } != {key: value for key, value in new_options.items() if key not in reserved}
    return ConfigurationChanges(
        frozenset(connections),
        frozenset(bindings),
        frozenset(targets),
        frozenset(tokens),
        display_changed,
    )


@dataclass(frozen=True, slots=True)
class RequestGeneration:
    """A request captures this before awaiting; never retag a late response."""

    runtime: object = field(repr=False)
    connection_id: str = field(repr=False)
    connection_generation: int
    binding_id: str | None = field(repr=False)
    binding_generation: int


class ConfigurationRuns:
    """Track connection and target-specific invalidations independently of auth."""

    def __init__(self, snapshot: ConfigurationSnapshot) -> None:
        self._snapshot = snapshot
        self._runtime = object()
        self._connections: dict[str, int] = {}
        self._bindings: dict[str, int] = {}
        self._options_cache: tuple[ConfigurationSnapshot, JsonObject] | None = None

    def _options(self) -> JsonObject:
        """The current snapshot's options, decoded once per snapshot (O5).

        Keyed by snapshot identity: a new snapshot is decoded again at once.
        Read-only; never modify the returned object.
        """
        snapshot, cached = self._snapshot, self._options_cache
        if cached is None or cached[0] is not snapshot:
            cached = self._options_cache = (snapshot, snapshot.to_options())
        return cached[1]

    def update(self, snapshot: ConfigurationSnapshot) -> ConfigurationChanges:
        changes = configuration_changes(self._snapshot, snapshot)
        for key in changes.connections:
            self._connections[key] = self._connections.get(key, 0) + 1
        for key in changes.bindings:
            self._bindings[key] = self._bindings.get(key, 0) + 1
        self._snapshot = snapshot
        return changes

    def capture(
        self, connection_id: str, binding_id: str | None = None
    ) -> RequestGeneration:
        """Allow a generation only for a currently enabled, existing scope."""
        options = self._options()
        if (
            connection_id not in self._snapshot.connections
            or not _section(options, "connections")[connection_id]["enabled"]
        ):
            raise ConfigurationError("connection_unavailable")
        if binding_id is not None:
            binding = self._snapshot.bindings.get(binding_id)
            if (
                binding is None
                or binding.connection_id != connection_id
                or not _section(options, "bindings")[binding_id]["enabled"]
            ):
                raise ConfigurationError("binding_unavailable")
        return RequestGeneration(
            self._runtime,
            connection_id,
            self._connections.get(connection_id, 0),
            binding_id,
            self._bindings.get(binding_id, 0) if binding_id else 0,
        )

    def peek(self, connection_id: str, binding_id: str) -> RequestGeneration:
        """The current generation of a switched-off scope (P49).

        Never accepted while the access is off, so nothing captured with it can
        publish or request; it only identifies a dormant view.
        """
        return RequestGeneration(
            self._runtime,
            connection_id,
            self._connections.get(connection_id, 0),
            binding_id,
            self._bindings.get(binding_id, 0),
        )

    def accepts(self, generation: RequestGeneration) -> bool:
        """Check immediately before publishing a result or scheduling more work."""
        try:
            return generation == self.capture(
                generation.connection_id, generation.binding_id
            )
        except ConfigurationError:
            return False

    def invalidate_binding(self, binding_id: str) -> None:
        """Retire one runtime attempt without changing stored settings."""
        if binding_id not in self._snapshot.bindings:
            raise ConfigurationError("binding_unavailable")
        self._bindings[binding_id] = self._bindings.get(binding_id, 0) + 1
