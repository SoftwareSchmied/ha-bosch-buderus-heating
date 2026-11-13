# Shared local budgets and source health

> Development record: the status sentences on this page describe the state
> before 0.9.0-alpha.1. Since then local setup and regular local polling are
> active; see [local setup completion](local-completion.md) and
> [managed local access](local-runtime.md).

I added the shared foundation for local setup and the read scheduler.
It does not enable public local setup or regular local polling.

## Admission and identity

One `LocalBudgetManager` belongs to Home Assistant. Clients obtain scoped handles
and never reset the shared state when replacing a client or configuration
generation. A gateway permits two concurrent reads, with at least 200 ms between
admissions and separately between HTTP header starts. The scheduler waits only
for the admission spacing; the header spacing is kept while a read runs. A
latency brake widens both to 1.5 times the median gateway time of the last 32
reads (from sending the request headers to receiving the response headers)
and doubles them for five minutes after a timeout on a new connection or a
server error, never to more than one second; see
[local polling](local-polling.md).
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

Every received pause is applied immediately. A pause that gets longer by more
than a second is saved atomically and read back; one the running pause already
holds writes nothing. Admission remains closed while the save is pending.
Cancellation of the caller does not cancel the write, including while it waits
behind another write. A failed save keeps the pauses in effect in memory and is
tried again a minute later. A file that cannot be read stops further local
admission and is read again with the next start attempt.

Reading the file is tolerant of what a later release may add: further fields
are ignored, an unknown reason counts as a general failure, and rows or
aliases that cannot be used are left out. An unreadable file (broken bytes,
another form or version) is kept as the only backup,
`.storage/bosch_buderus_heating.local_budget.corrupt`, and the local access
starts without saved pauses, with a warning in the log, instead of staying
closed until the file is removed by hand. This does not stop cloud polling.
These guarantees rely on the filesystem and do not claim protection against
every hardware failure.

Deadlines use monotonic time. HTTP-date Retry-After is converted once to a
duration, and later shorter delays never shorten an existing pause. Long delays
are kept up to 24 hours; a longer one is cut to 24 hours, as for the cloud
(see [cloud pauses](cloud-pauses.md)). While a pause exists, remaining time is checkpointed every
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

Only a ready or merely paused access with a confirmed identity supplies normal
source data. A pause only slows the requests: values read before stay current
until their own expiry. Other states withdraw them at once and may retain
immutable historical observations with their original times.
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
