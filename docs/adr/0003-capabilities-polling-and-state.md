# ADR 0003: Capabilities, polling, and state

- Status: Accepted
- Date: 2026-08-16

## Decision

Discover actual resource references and match them against a curated capability
catalog. Do not infer capabilities from marketing model names and do not expose
every unknown resource as an entity.

Poll batch-first in fast, control, energy, slow, static, and discovery groups:

- fast operating values every 60 seconds;
- writable settings and energy counters every 5 minutes;
- runtime counters and other slow diagnostics every 15 minutes;
- static values only during startup discovery.

Discovery processes the reference tree in batches of at most 30 paths.
Runtime polling combines all groups due in the same cycle before it
splits the paths into batches of at most 30. A typical installation should need
one request per minute plus the less frequent additional batches and remain
well below 5,000 HTTP requests per day. Preserve successful batch items and
per-resource last-good state with explicit freshness.

Paths explicitly referenced by PointT are fully processed before optional
app-path probes. Each discovery call submits only one batch; a malformed
envelope triggers individual reads only for that batch. Every recoverable
malformed or 5xx item can receive one logical individual read. Discovery has no
shared 30-item fallback budget or consecutive-path-failure cutoff. Optional
probes cannot spend the path budget while advertised references remain queued.
If an optional probe reveals new references, they take priority over the next
optional batch. `403`, `404`, and `406` items never cause an individual fallback.
Authentication failures and request-wide service or connection errors stop the
run, including during individual fallback; `429` stops discovery through the
account-wide backoff. The depth and 512-requested-path bounds remain protection
against malformed or cyclic graphs and are reported as incomplete discovery.
Transport retries are counted separately from logical resource reads.

The initial live K40 profile contained 94 readable resources and used eight HTTP
requests with the discovery strategy at the time of capture. Separating
reference and optional batches can add partly filled batches; the current count
is available in diagnostics. Its runtime groups contain 20 fast, 20 control,
4 energy, 9 slow, and 41 static resources. With coinciding due groups combined,
normal operation uses about 1,728 HTTP requests per day; retry traffic is not
included in that baseline.

The observed K40 advertises `/heatSources/emon` but returns HTTP 403 for that
container even though its four documented counter children are readable. The
discovery layer may seed only those known children when it sees this exact
opaque reference. It must not guess arbitrary paths for other containers.

## Consequences

Discovery is bounded and runs once per integration load. A reload is required
after the physical system gains or loses a circuit. `429` pauses all cloud
polling according to `Retry-After`, bounded to one hour; if the server omits the
header, the default pause is five minutes. A failed batch preserves last-good
values, and a partial cycle keeps successful earlier batches. After one failed
batch, at most five central operating paths are read individually; `429` never
triggers this fallback. Three consecutive complete gateway failures open a
five-minute circuit breaker.

Resource failures remain local. A `404` pauses the path for 24 hours, a second
consecutive `403` pauses it for 24 hours, and a `504` pauses it for 15 minutes.
A later successful read clears the pause. Each resource stores its last
successful and attempted timestamps, value source, freshness, sanitized error
category, and consecutive failure count. Entity availability is therefore
resource-specific.
