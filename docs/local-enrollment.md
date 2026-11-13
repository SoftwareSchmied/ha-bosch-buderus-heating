# Local token enrollment and recovery

The enrollment component extends the [credential handover](local-credentials.md).
It prepares device-label authentication and restart recovery for an explicitly
selected retained installation. Completion and native removal now use the journal;
setup UI and polling remain separate.
The active config flow uses storage version 4.2 (since 0.9.0-alpha.1).

## One request with explicit trust

`local_auth.py` accepts a context-bound, explicitly confirmed authentication-port
certificate. It inspects TLS first and checks the full SHA-256 leaf fingerprint
again on the actual connection. It sends one form-encoded `POST /auth/token` to
port 9442. Password hyphens are removed as documented. The login and password
exist only in the current call. The client name is a fixed application label;
it does not prove token ownership.

There are no redirects, HTTP retries, environment proxies, cookies or appliance
write operations. The active request has a 15-second budget,
5 seconds per stage and 2 seconds per peer inspection. Managed enrollment first
waits for an exclusive slot in the [shared gateway budget](local-budget.md).
Admission reserves those 15 seconds within a total 120-second time budget.
These are implementation
budgets, not manufacturer limits. The response has a separate 16 KiB limit,
strict UTF-8 JSON validation and the existing structure limits.
An already running atomic disk write is allowed to finish after cancellation;
these network budgets do not bound filesystem latency.

A successful response must report a Bearer token and `open_api.read`. The
resource port and appliance identity still require separate verification.

| Result | Meaning |
| --- | --- |
| `not_sent` | The request did not begin, for example after a failed TLS preflight. |
| `rejected` | The endpoint explicitly rejected the request. Statuses include invalid credentials, missing physical proximity, forbidden access and a full token database. |
| `received` | A valid token response was received and can be retained for later checks. |
| `uncertain` | Creation may have happened. This includes a lost response, cancellation after dispatch, an unexpected status or an invalid success body. |

Once request dispatch begins, a transport failure is conservatively uncertain
even if no body ultimately reached the gateway. An inactive UI does not turn a
received token into a valid configuration update, but the token is retained for
recovery. Cancellation propagates to the caller.

## Persistent pending state

`local_enrollment.py` provides one process-wide journal through
`enrollment_journal(hass)`. The file is
`.storage/bosch_buderus_heating.local_enrollment`. It is private HA storage,
not an additional encrypted vault. It contains at most 64 records and 1 MiB.
It is not exported through diagnostics.

Before any request, the journal saves and reads back an uncertain attempt. It
uses Home Assistant's atomic JSON writer with private permissions and propagated
errors. The usual `Store.async_save` catches some write errors and can defer a
write during shutdown, so it is unsuitable as this pre-request acknowledgement.
The atomic helper uses file synchronization; no absolute hardware durability or
atomic transaction with the gateway is claimed.

The record holds the selected owner and target, optional replaced connection,
canonical host, confirmed authentication fingerprint, creation time and a digest
of the selected configuration basis. It does not copy the old access token,
login or password. A valid newly received token is then stored separately with
its issuer-reported scope. No token ID is inferred from a name or timestamp.

One lock covers reads, writes and enrollment. Executor writes finish before the
lock is released, even after repeated cancellation. A new instance reloads the
file; malformed or unsupported data blocks creation instead of appearing empty.
Removing a local access or an entry does not fail on such a file: it is moved
aside as `bosch_buderus_heating.local_enrollment.corrupt`, and the next setup
starts with an empty journal. A file that cannot be read at all still stops the
removal with a storage message.
Failures before the initial write send no request. Failures during a later save
leave the previous durable record authoritative, potentially still uncertain.

A `rejected` or `not_sent` result certainly created no token. Its record is
resolved at once, so the next attempt needs no acknowledgement; its
`Retry-After` pause stays in the shared budget. Another attempt for the same
owner/target or canonical endpoint is blocked until an `uncertain` or
`received` attempt is explicitly acknowledged. This is not a claim that different
host aliases identify different physical gateways. The selected target and later
full identity comparison remain authoritative. Setup for separate targets is
serialized; these limits do not affect regular cloud polling.

## Resume and acknowledge

Recovery returns the retained credentials and a new setup context. The selected
configuration basis must still match. Independent cloud-token renewal is allowed;
changed local settings or target ownership require correction. Resource trust,
all three fresh GET responses and the full inventory identity comparison remain
necessary before the prepared handover can accept access.

If no valid response was saved, the journal cannot recover an unknown token from
the gateway's token list. The UI must explain that the previous request may have
created a token before the user explicitly acknowledges another attempt. There
is no automatic recreation or revocation. A received token is kept until explicit
resolution; it is not given an invented expiry. `Retry-After` remains effective
even after acknowledgment.

`discard` removes the pending credential only after explicit confirmation and
does not delete anything on the gateway. The future UI must explain this
difference. Automatic cleanup after a successful handover must wait until the
final configuration is durably confirmed; an in-memory HA update is insufficient.
The [completion coordinator](local-completion.md) provides this distinction.
Certificate and physical-unlock dialogs and activation of the local connection
remain separate work.

Journal version 3 retains explicitly imported tokens and records whether an old
pause has been migrated. Imported scopes remain unknown and no authentication
certificate is invented. Versions 1 and 2 remain readable. Version 1 acquires no
completion claim during conversion. Both token origins require fresh resource
and identity checks.

Old UTC-only pauses are transferred to the shared budget using the entire span
from the recorded attempt start to its retry deadline. This deliberately avoids
assuming that a later wall-clock reading proves elapsed time. The budget is
saved before the journal is marked as migrated. Later reads do not restart the
pause. A wall-clock deadline remains a display hint; monotonic remaining time is
authoritative. Acknowledging or removing an enrollment does not clear the budget.

## Messages and a changed certificate

Each known failure has its own message: wrong sign-in details (also a 401
from the sign-in service), a sign-in service that refuses the request (403),
missing confirmation on the device (412), a full token list on the gateway
(507), a token the data port rejects, a resource the data port denies, an
unclear creation result and an incomplete completion. Only unexpected faults
show the generic message.

A changed gateway certificate is never accepted on its own. The local access
pauses, its status says that the certificate changed, and a repair note
explains the way out: in the options of the integration, choose the local
access and **Change local access**. After confirming the new certificate, the
current token can be kept; it is read again against the confirmed certificate
before anything is saved, so no new token is created on the gateway. While the
gateway refuses a resource (403), the access shows **access denied** instead of
**not reachable**. Tokens are never revoked automatically: not when an access
or the whole entry is removed, not when an attempt is discarded and not when a
new token replaces an old one.

## Tokens on the gateway

Every sign-in with the device label creates another token on the gateway, and
the gateway holds only a limited number of them. Once it is full, a new sign-in
fails with the message about the full token list (507). Two actions in the
options of a local access help with this. Both need a working stored token, so
they are available only while a local access exists; after a 507 without any
access the message stays as before.

- **Tokens on the gateway** reads the token list once, when it is opened
  (`GET /auth/token` on port 9442 with the access's token). It shows the number
  of tokens and, for each, the client name and the creation time; token values
  are never part of the answer, and the token IDs are not shown. The list is
  not saved and does not appear in the log or in diagnostics. Client names come
  from the gateway and are shown as literal text.
- **Revoking a token** is offered only for entries with this integration's
  client name (`Home Assistant Home Comfort`); tokens of other clients are
  never offered. The list has no token values, so the dialog cannot tell which
  of these entries the access itself uses. The user chooses by creation time;
  the dialog shows when the access was last set up as a guide. The revocation
  needs its own confirmation. Right before it, the list is read again, and
  nothing is sent unless the chosen entry is still listed unchanged. Then
  exactly one `POST /auth/revoke` (`token_id`, `usage_type=private`) is sent,
  without any retry, and the list is read once more to confirm it. If the
  gateway confirms neither way, the dialog says so and repeats nothing.
- If the revoked token was the one the access uses, the gateway refuses the
  read-back. The dialog then reports that local access has ended and switches
  the access off, instead of leaving it failing; a new sign-in with the device
  label sets it up again.
- **Remove access** for a local access has an extra choice to revoke a token
  of this integration first. Only with that choice is anything sent; the access
  is removed only after the revocation was confirmed (or when no token is
  chosen).

All three requests use the confirmed certificate of the sign-in port and the
shared [gateway budget](local-budget.md): they wait for an exclusive turn,
respect its pauses and finish within its two-minute limit. The revocation is
sent only while the remaining time still covers it and its read-back. An access
set up with an entered token has no confirmed certificate for the sign-in port;
the same certificate as on the confirmed data port is accepted, any other one
needs a confirmation that is valid for the open dialog only.

## Validation

I tested real form encoding and TLS against synthetic loopback servers, private
atomic files, reconstruction after restart, lost responses, cancellation, storage
failure and concurrent attempts. Recovery is also exercised through fresh TLS
resource reads and the existing configuration proposal. No real gateway tokens,
appliance writes or installation changes are part of these tests.

The token list and the revocation were tested the same way, against a
synthetic gateway on the loopback interface: an empty, a full and an oversized
list, unusable answers, refusals (401, 403, 404, 429, 503), a changed
certificate, a paused access, a foreign entry and a disguised one, a list that
changed before the revocation, a lost answer, a failed read-back, a refused
revocation and the revocation of the access's own token. They have not been run
against a real gateway yet.

Protocol reference: [official K40/MX400 documentation](https://github.com/bosch-home-comfort/api-docs/blob/78e36b3b57e11ac6266558acba96c13bee88ceff/docs/k-40-rf.md).
