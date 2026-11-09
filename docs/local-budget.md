# Shared local budgets and source health

I added the shared foundation for local setup and the read scheduler.
It does not enable public local setup or regular local polling.

## Admission and identity

One `LocalBudgetManager` belongs to Home Assistant. Clients obtain scoped handles
and never reset the shared state when replacing a client or configuration
generation. A gateway permits two concurrent reads, with at least 200 ms between
admissions and separately between HTTP header starts. The scheduler waits only
for the admission spacing; the header spacing is kept while a read runs. A
latency brake widens both to twice the median of the last 32 response times
(at most one second) and doubles them for five minutes after a timeout or a
server error (at most two seconds); see [local polling](local-polling.md).
DNS, TLS inspection and cleanup belong to the admitted operation. There are no
automatic HTTP retries.

Certificate inspection, token enrollment and each of the three setup reads take
exclusive slots. Up to eight waiting setup operations prevent additional regular
admission and wait for existing operations to finish. A setup operation has a
120-second network time budget including admission; certificate inspection and
enrollment reserve their active request allowance within it. Persistent writes
already in progress finish after cancellation and can extend cleanup time.

Connection and target aliases preserve pauses across configuration changes.
Canonical endpoints and resolved peer addresses join known gateway budgets
before TLS; a successful local identity assessment can add the gateway ID.
After groups merge, admission is checked again before TLS and HTTP headers.
This grouping only limits load. It never proves resource ownership, creates a
Home Assistant device or replaces the full setup identity comparison.
Unrelated addresses cannot be recognized as the same hardware without evidence.

There are at most 256 groups and 2,048 hashed aliases. Capacity exhaustion refuses
new groups without clearing existing pauses. Independent gateways retain their
own capacity. Cloud requests use their existing independent limits.

## Persistent pauses

The private file `.storage/bosch_buderus_heating.local_budget` stores hashed
aliases, remaining durations and fixed reason codes. It contains no tokens,
passwords, resource values or raw gateway identifiers. Hashes are correlation
keys, not a claim of anonymization. The file is limited to 1 MiB and is not part
of public diagnostics.

Every received pause is applied immediately, saved atomically and read back.
Admission remains closed while the save is pending. Cancellation of the caller
does not cancel the write, including while it waits behind another write.
Malformed storage or a failed save stops further local admission; the existing
file is retained. This does not stop cloud polling. These guarantees rely on the
filesystem and do not claim protection against every hardware failure.

Deadlines use monotonic time. HTTP-date Retry-After is converted once to a
duration, and later shorter delays never shorten an existing pause. Long delays
are retained in full. While a pause exists, remaining time is checkpointed every
60 seconds and on clean shutdown. A restart restores the entire saved duration,
without subtracting wall-clock downtime. A crash or repeated restart can extend
a pause, but a clock jump cannot make it expire early. Expired pauses disappear
from the next saved snapshot.

Legacy enrollment pauses are migrated once before the next request. Their
original recorded duration is retained conservatively. Removing or acknowledging
an enrollment does not remove a gateway pause.

## Local health and observations

`LocalSourceState` separates the active configuration generation from local
health. States distinguish initialization, ready access, a pause, unavailability,
authentication and certificate confirmation. The runtime can accept recovery
updates for the current configuration even while its source is unavailable.

Only ready access with a confirmed identity supplies normal source data. Other
states may retain immutable historical observations with their original times.
Changing health does not make those observations fresh. Removed bindings and
obsolete generations cannot publish data. Cloud source behavior is unchanged.
The scheduler applies per-resource expiry and recovery rules.

## Verification and remaining work

Synthetic TLS gateways and private temporary files exercise shared admission,
cross-port pauses, alias merging, cancellation, restart recovery, old journal
migration and storage failures. Runtime tests cover health changes, retained
observation times and generation rejection. No real gateway call or installation
change is needed for these tests.

The [polling service](local-polling.md) implements fair scheduling, bounded
discovery and per-resource expiry through the [managed runtime](local-runtime.md).
The selected live-value target remains 60 seconds, with expiry
after three target intervals. The load limits are implementation choices, not
manufacturer-certified capacity.
