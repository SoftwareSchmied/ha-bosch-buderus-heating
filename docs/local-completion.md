# Local setup completion

The completion layer connects the [pending enrollment journal](local-enrollment.md)
to the guarded [credential handover](local-credentials.md). The normal config flow
advertises version 4.2 with the [managed local runtime](local-runtime.md).
This completion layer adds no appliance writes.

## Accepted and confirmed configuration

Home Assistant acknowledges `async_update_entry` in memory and saves it later.
The coordinator therefore records its intended local state before asking HA to
accept an update. It uses the public entry API and never reads, writes or flushes
HA's private config-entry storage directly.

The completion intent contains the selected connection and binding IDs, a digest
of their expected local configuration and a process identifier. It does not copy
the old cloud or local configuration. The existing pending record retains the new
token until completion is confirmed or it is explicitly discarded.

| Loaded state | Recovery behavior |
| --- | --- |
| Selected configuration still matches the previous basis | The retained token can be checked again. A staged new connection keeps its reserved IDs. No automatic replay occurs. |
| Configuration matches the completion intent | The operation is already applied. Repeating completion performs no update and does not advance its authentication epoch. |
| Local configuration or selected target changed | Recovery reports a conflict and preserves the current configuration. |
| No usable token was received | The outcome remains unresolved and another creation still needs explicit acknowledgment. |
| The owner was removed | Its pending records are removed locally. Gateway tokens are not revoked. |

Only the journal's first initialization during a later HA process startup may
automatically clear the redundant token copy, and only if the configuration loaded
at that moment matches the intent. A running-process reload, late integration
setup, or initialization after journal mutations does not establish persistence.
Malformed pending data is preserved and reported with a fixed message; it does
not prevent cloud setup or the remaining native removal work.

Independent cloud-token rotation is excluded from the local digest and survives
handover. Local credentials, certificate trust, identity, binding and target
settings are included. A mismatch never restores an older snapshot.

## Format transition and removal

The first transition to 4.2 waits for pending cloud credential refresh.
For a running entry, it finishes native unloading before changing the format.
After success, failure or cancellation it starts that entry again if it is still
present, enabled and unloaded. An entry already stopped by the user is not
started. Cancellation waits for a started native unload or restart to settle.
An unsuccessful restart is reported and the recovery record remains available.
Replacing local access within 4.2 does not require stopping cloud access.

Owner and connection removal mark in-flight enrollment before waiting for its
storage lock. Late responses cannot restore the removed pending token. Atomic
cleanup finishes despite caller cancellation. Connection removal retains the
installation and its existing identities. Removing the native entry clears only
that owner's pending records.

## Identity evidence and validation

`ScopedIdentityContract` binds caller-verified source evidence to one owner,
selected target and gateway. A contract label alone does not prove that two API
fields have the same semantics. The managed identity policy acquires fresh
PointT and local system evidence before constructing this record, or reuses the
same previously verified relationship. It never comes from a free-text setup
field. The full inventory comparison still checks other owners and targets.

I tested native HA update outcomes, actual private journal files, recovery from
both journal formats, imported and created tokens, synthetic TLS resource reads,
concurrent removal, cancellation and runtime restart. The tests use fabricated
credentials and identity contracts. They do not validate a real installation.
