# Local credentials and verified handover

This foundation prepares local access for an explicitly selected, retained
installation. The normal setup flow advertises entry version 4.2 since
0.9.0-alpha.1 and writes the local format described here; discovery and
polling are described in [local setup completion](local-completion.md) and the
[managed local runtime](local-runtime.md).

## Stored data and provenance

The local credential record distinguishes two sources:

| Source | Reported scope | Additional evidence |
| --- | --- | --- |
| Token returned by device-label login | Parsed from the successful authentication response; it must include `open_api.read` | Confirmed certificate bindings for authentication and resources, followed by the complete resource probe |
| Existing token supplied by the user | `null`, because a successful GET does not report the token's scope | Confirmed resource certificate and the complete resource probe |

Both records use Bearer authentication. Tokens, scope contents, fingerprints and
private endpoints are excluded from object representations. Explicit serialization
contains private data and belongs only in the configuration entry, never in
diagnostics. The device-label password is not part of this storage contract.

The endpoint retains its canonical host and fixed resource port 9443. Certificate
bindings record SHA-256 of the complete leaf certificate separately for both port
roles. Stored read evidence includes the exact gateway ID, the probe method and
its timestamp. It describes a past successful verification, not current
availability after a restart. Every certificate change still requires confirmation.

The codec records no guessed token ID or expiry. Unknown token-response fields,
application names and creation times do not prove token ownership. Unknown scopes
never authorize appliance writes. There is no automatic token revocation.

## Verification and proposal

1. Capture the selected connection, target, endpoint and a distinct attempt
   context. A second local connection to the same retained target is rejected,
   including when the existing connection is disabled.
2. Check the confirmed certificate bindings and read the three required resources
   through the [bounded transport](local-transport.md). Failed, cancelled or stale
   probes produce no accepted credential proposal.
3. Compare the returned gateway ID against the complete current inventory,
   including disabled entries and retained targets without access. Cross-source
   comparison requires an explicit identity contract validated for the selected
   device. Equal strings alone do not enable such a contract.
4. Build the update from current data. Changes to the selected access invalidate
   the attempt; unrelated cloud token rotation and general options are preserved.
   Existing connection, target, binding and legacy entity identity keys remain.
5. Recheck the context and the short-lived verification receipt immediately
   before the owner's single update callback. The receipt is usable for up to
   60 seconds and only while the originating setup flow is still active.

A replacement advances both its authentication epoch and configuration revision,
even when it uses the same token. Local credential-only changes also invalidate
old local requests because local tokens have no ordinary refresh mechanism.
Existing enabled states and source order remain intact. A new local binding is
appended to the existing source order; selecting a preferred data source belongs
to the later local/cloud resource policy.

## Version transition and HA persistence

Readers support the earlier 4.1 contract and the active 4.2 contract. A new
provenance record is rejected when presented as 4.1 data. Legacy local preview records remain readable without inventing
certificate trust or fresh read evidence; they need verification before reuse.
Returning to older software requires the matching configuration backup.

The initial 4.1 to 4.2 transition requires a stopped entry and no pending cloud
credential refresh. The setup flow arranges this transition and resumes the
entry afterwards. Subsequent local replacements within 4.2 use the scoped
update. The cloud authentication context includes the minor storage version so
a restarted owner cannot reuse an old token-persistence callback. Existing
account backoff is retained when the manager is rebuilt.

HA accepts data, options and the new minor version in one callback. Its normal
disk write is scheduled later. The callback is not a synchronous durable-storage
acknowledgement and is not a transaction across configuration and registries.
An error before acceptance leaves the old record intact. An error after
acceptance, such as a listener failure, is reported as an uncertain outcome; no
rollback overwrites newer state. The previous token is never revoked by this
handover, and an uncertain outcome is not automatically repeated.

## Validation and remaining work

I tested the codec, the complete read-to-proposal path with synthetic TLS servers,
concurrent configuration changes, conflicting identities, cancelled attempts,
and native HA configuration updates in an isolated environment. The tests enable
minor version 2 only in that environment. They also check that a pending cloud
refresh survives and that a replacement cloud manager can persist refreshed tokens
after the storage transition.

The [enrollment component](local-enrollment.md) now supplies the device-label
authentication transport and persistent recovery of pending attempts. The
physical-unlock dialog, certificate confirmation UI, coordinated version
transition and runtime activation remain separate work.
No live token was created, imported or revoked for these tests, and no real
installation was changed.

Protocol reference: [official K40/MX400 documentation](https://github.com/bosch-home-comfort/api-docs/blob/78e36b3b57e11ac6266558acba96c13bee88ceff/docs/k-40-rf.md).
