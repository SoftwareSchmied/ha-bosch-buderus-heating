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

Discovery follows the reference tree by level and retrieves up to 30 paths in
one batch. Runtime polling combines all groups due in the same cycle before it
splits the paths into batches of at most 30. A typical installation should need
one request per minute plus the less frequent additional batches and remain
well below 5,000 HTTP requests per day. Preserve successful batch items and
per-resource last-good state with explicit freshness.

On the first live K40 profile, 94 readable resources require eight HTTP
requests during discovery. Its runtime groups contain 20 fast, 20 control,
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
