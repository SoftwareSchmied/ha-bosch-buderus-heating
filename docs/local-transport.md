# Local setup transport

The local transport is a callable foundation for K40/MX400 setup. It is not yet
connected to the setup dialog, credential storage or polling. It reads only
`/gateway/brand`, `/gateway/versionFirmware` and `/system/basicInfo`, then passes
the responses to the [local setup probe](local-setup-probe.md). A successful probe
still needs the [gateway identity comparison](gateway-identity.md).

## Certificate binding

Authentication uses HTTPS port 9442; resources use HTTPS port 9443. Certificate
observations and confirmed bindings belong to one endpoint, setup context and
port role. Different ports can have different certificates. An existing token
can be checked on the resource port without reaching the authentication port.

Certificate inspection performs a TLS handshake without sending HTTP or
credentials. It returns an unapproved observation. The future setup owner must
obtain explicit confirmation before constructing a binding. Neither inspection
nor construction alone establishes that the user has trusted the gateway.

The binding records SHA-256 of the complete DER leaf certificate. Every change,
including renewal with the same public key, stops the probe until explicitly
confirmed. Each authenticated connection checks the fingerprint before sending
HTTP headers. The selected numeric peer is fixed for the probe, preventing DNS
address changes between certificate observation and the authenticated request.
A certificate mismatch stops immediately, even if another resolved peer exists.
Only connection failures and timeouts during inspection can try another peer.

This is explicit certificate pinning, not public PKI validation. It does not
validate a CA chain, hostname or certificate expiration. First trust therefore
depends on the user's selected endpoint and confirmation; the later identity
comparison serves a separate purpose. The transport requires TLS 1.2 or newer.
It does not change Home Assistant's shared TLS configuration or cloud sessions.

## Request limits and failure handling

- A managed setup probe has a 120-second budget including waiting for the
  [shared gateway budget](local-budget.md), DNS, TLS inspection and HTTP.
  Each admitted path has five seconds for inspection and HTTP together. Standalone
  probes used by isolated callers retain the original 15-second overall limit.
  Each candidate peer
  gets at most two seconds for its initial connection and TLS handshake.
- TLS stream cleanup has a separate quarter-second bound and can briefly extend
  cancellation cleanup. Cancelled requests propagate cancellation.
- At most the first 16 resolver results are considered. Unsafe addresses are
  rejected; scoped IPv6 uses an interface that exists on the executing host.
- At most three HTTP GET requests are sent, one per required path. There are no
  automatic HTTP retries or followed redirects. A per-request middleware also
  blocks the HTTP library's otherwise automatic retry after a disconnect.
- The transport accepts only uncompressed UTF-8 JSON responses. Its body reader
  retains at most 256 KiB plus one detection byte per response. Socket buffers
  can already contain additional received bytes; this is not a wire-byte quota.
  The pure probe separately validates JSON structure and resource contents.
- A non-200 response ends the probe without collecting its error body. A usable
  `Retry-After` from 429 or 503 is returned as evidence and, for managed probes,
  saved in the shared budget before rejecting a stale configuration. Other 5xx
  responses use a 60-second pause. There is no automatic retry. API and transport
  failures remain distinct.
- Context and generation guards prevent stale results from being accepted. The
  caller must invalidate the guard on cancellation, endpoint, certificate or
  credential changes and before committing the result.

Each probe owns its session. Proxy environment variables, ambient authentication
and cookies are disabled. Tokens are supplied only to pinned resource requests;
they are not persisted, logged or included in result representations. Fixed
failure codes replace private endpoint and TLS error text.

## Remaining setup work

The planned UI offers device-label login and an existing token. Creating a token,
confirming trust, storing credentials and recovering from a changed certificate
are subsequent work. The transport does not send token-creation or revocation
requests and has no appliance-write API.

A successful GET with an imported token proves read access, not issuer-reported
scope metadata. The prepared [credential storage and handover](local-credentials.md)
preserves that distinction; its activation and setup UI are still pending. The official creation
example does not include a token ID, so it does not justify revoking an allegedly
new token based only on its name or creation time. An ambiguous token-creation
timeout must not cause an automatic repeat or deletion of another token.

I tested the transport with temporary TLS servers, generated certificates and
synthetic credentials, including renewal with the same key, a certificate swap
between inspection and HTTP, disconnects, redirects, bounded responses,
timeouts, cancellation and stale contexts. These tests are separate from earlier
gateway experiments and do not confirm this transport on a real appliance.

Protocol reference: [official K40/MX400 API documentation](https://github.com/bosch-home-comfort/api-docs/blob/78e36b3b57e11ac6266558acba96c13bee88ceff/docs/k-40-rf.md).
