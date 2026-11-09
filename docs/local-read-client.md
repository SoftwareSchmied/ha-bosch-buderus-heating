# Local HTTPS read client

I added an asynchronous client for individual requests from the complete
[local resource catalog](local-api-resources.md). It returns a validated local
observation or a structured failure. It does not start polling, discover an
installation, assign devices or update Home Assistant entities. The
[managed local runtime](local-runtime.md) owns setup and scheduled reads.

## Request and trust boundary

`LocalReadClient` receives one captured source context, a validated token, the
previously confirmed full certificate fingerprint for the resource port, a
gateway budget handle and a guard for the current configuration generation.
The client cannot approve a certificate, create a token or send a write request.

Before the first GET, and again after a connection failure or an address
change, anonymous TLS inspection checks the entire leaf certificate and selects
a numerical peer. Every new credentialed connection checks that same SHA-256
fingerprint again, right after the handshake and before any request line or
header is written. Even a renewed certificate with the same public key stops
the client. The caller must complete the explicit trust workflow before
constructing a replacement client.

The client owns its HTTP session. An answered connection is kept and used again
for the next request within 4 seconds of idle time (keep-alive), so TLS
handshakes are only needed after pauses and failures. The pool keys a kept
connection by numerical peer, server name and the client's single pin object,
so no request is ever sent over a connection that was not checked against the
pin. An error answer releases its connection without reading the body; the
connection is only kept when that answer was already complete. Redirects,
ambient proxy settings, cookies, automatic decompression and automatic HTTP
retries are disabled. Only a validated catalog target is appended to the
selected resource origin. Queries and Unicode signal IDs retain their validated
encoding.

When kept connections misbehave, the client falls back automatically to a new
connection per read, the behaviour before keep-alive. Three failures on kept
connections (closed or reset before an answer, no response headers in time,
or a broken answer) within 15 minutes start the fallback for 6 hours; a repeated fallback
lasts 12 and then 24 hours. Keep-alive is then tried again. The first fallback
is logged once as a warning. The fallback only changes how connections are
opened, so reads continue during it. The
[diagnostics](diagnostics.md) report the counts under `connection_reuse`.

## Bounded reads and shared pauses

`LocalReadBudget` is obtained from the HA-wide `LocalBudgetManager`. Handles for
the same connection, retained target, endpoint, resolved address or verified
local gateway share admission and pauses. Each handle still checks its caller's
owner and connection. Independent gateways and cloud access have separate gates.
See [shared local budgets](local-budget.md) for alias and persistence limits.

The gate permits two in-flight reads with at least 200 ms between admissions
and separately between actual HTTP header starts. This allows at most five
starts per second; the latency brake of the shared budget can widen the
spacing (see [shared local budgets](local-budget.md)). A call made while busy or paused returns `deferred`
immediately, without DNS, TLS, HTTP or an internal queue. The admission guard
also bounds attempts that fail before HTTP. Setup, certificate inspection and
token enrollment use exclusive slots in the same budget. Setup waits boundedly
for running reads and prevents further regular admission while waiting.

The default request deadline is five seconds across inspection, HTTP and
decoding. Successful bodies must be uncompressed UTF-8 JSON. The default limits
are 256 KiB, depth 32 and 8,192 JSON nodes. Declared and streamed body sizes are
checked; oversized, malformed, truncated or mismatched responses never produce
an observation. Header parsing is bounded separately. Socket cleanup may need
its own bounded closing time after a cancelled inspection.

These are implementation limits, not manufacturer-tested load limits. The
[polling service](local-polling.md) applies fair scheduling and a total cycle
budget to the 60-second live target. Loading the complete catalog does not
request all of it. History windows and discovery have separate limits.

## Results and failure scope

| Condition | Result and effect |
| --- | --- |
| Valid HTTP 200 response | Immutable observation with its original source context and observation times |
| Busy gate or active pause | Deferred, without a network request |
| Kept connection closed or reset before any answer | Deferred (`reused_connection_closed`) without a pause; the next read opens a new connection |
| HTTP 400, 403, 404 or 406 | Path error; no permanent unsupported-device decision |
| Redirect or other non-success status below 500 | Path error; no redirect is followed |
| Invalid body, wrong resource or timeout after request headers start | Path error; earlier successful observations remain intact. A timeout after the response headers arrived (a slow body, or a complete answer just after the deadline on a busy host) does not count against kept connections |
| An answer the schema accepts but the decoder cannot handle | Path error `local_decode_failed` of that path only; logged once per path template and error type as a warning |
| HTTP 401 | Access error; this client stops until credentials are deliberately replaced |
| Any certificate change | Access error; this client stops until trust is explicitly confirmed |
| HTTP 429 or 503 | Access error and the complete valid `Retry-After` pause, otherwise 60 seconds |
| Other HTTP 5xx, DNS, connection or TLS failure, or timeout before headers | Access error and a 60-second pause |

No error response body is included in a result. Problems use fixed codes.
Results do not refresh or clear a previously successful value. The polling
service applies expiry and availability rules and distinguishes a newly missing
path from a known resource that temporarily disappeared.

Pauses use monotonic time and are extended, never shortened by later failures.
HTTP-date `Retry-After` values are converted once to a duration. A long pause
is not capped at one hour. A zero delay still respects minimum request spacing;
the polling service then still pauses with its growing access backoff.
A pause already received is retained even if the generation changes before the
result can be returned. Managed pauses are atomically saved and read back before
admission reopens. A restart restores the full saved remaining duration; wall
clock changes cannot shorten it. Setup and enrollment use this same state.
The [polling service](local-polling.md) adds regular scheduling, discovery and
explicit reads through the managed local source.

## Lifecycle and verification

Stale contexts and cancellation raise without returning a usable result. The
generation guard is checked before HTTP headers, during body processing and
before returning a decoded observation. The deadline is checked again after
decoding, including when synchronous processing ran past it.

Each read uses a task owned by the client. Closing cancels those tasks and closes
the session without waiting for unrelated work in a caller. Cancelling a close
waiter does not cancel cleanup. The token reference is cleared after cleanup.
The owner must still await closing as part of unloading the connection.

Metrics distinguish calls, inspection operations, HTTP header-send attempts,
received HTTP statuses, successes and deferrals. An inspection can involve
multiple candidate peers. A header-send attempt is not proof that the gateway
received or processed it. There is no internal HTTP retry hidden behind a
logical read.

I tested the client with synthetic credentials and loopback HTTPS gateways,
including renewed certificates, a certificate swap between connections,
concurrent clients, long pauses, truncated and oversized bodies, disconnects,
timeouts, stale generations and cancellation. The existing setup and enrollment
tests also exercise the shared transport helpers. No real appliance or PointT
request was made for this step, and no installation was changed.
