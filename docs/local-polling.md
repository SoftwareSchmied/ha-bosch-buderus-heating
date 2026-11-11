# Local scheduling and resource discovery

I added a local polling service, a bounded resource inventory and resumable
discovery. These components use the existing read client and shared gateway
budget. The [managed local runtime](local-runtime.md) starts this service after
fresh local identity verification. Normal entity data routing and installation
acceptance remain separate steps.

## Scheduling and freshness

The reviewed policy assigns every one of the 261 GET templates to a class.
Live values, current operating modes and manual room setpoints use 60 seconds.
Other settings, energy values and radio diagnostics use 300 seconds. Counters
and the gateway firmware use 900 seconds, as do room-device and zone metadata
such as battery level and names; descriptive system metadata uses an hour;
static identifiers and the installation date signals use a day.
The signal directory uses 900 seconds and participates in initial discovery.
History and administration require explicit requests.

The configured live interval accepts 60 through 900 seconds in 60-second steps.
Each periodic class uses the larger of its own interval and that setting.
With **Use local values for existing entities** enabled, readings that the
cloud also provides under the same documented path, except counters, are polled
at least as often as the cloud polls them: the local interval is min(local
interval, cloud interval), never below 60 seconds. The cloud interval follows
its polling profile and actual plan; until a running cloud has discovered its
readings, its shortest interval applies. See
[source selection](source-selection.md).
A controller signal whose entities are all disabled in Home Assistant is read
at most once an hour; its entities are still created. Signals with a cloud
counterpart, counters, faults and the parts of the installation date keep
their usual interval. Enabling one of the entities restores the usual interval
within seconds, without a restart: a reading already older than the usual
interval is then due at once. A signal without a registered entity (for
example before its first reading) keeps the usual interval as well.
A signal that never gets an entity is also read at most once an hour: an
identifier or other private signal, and a signal whose latest answer is free
text without declared values or a structure. The same exceptions apply, and a
signal whose two possible forms are still undecided, or that any entity
shows, keeps its usual interval. When a later answer gives it an entity, the
usual interval applies again. The diagnostics count both kinds
(`signals_read_hourly_unused` and `signals_read_hourly_without_entity`).
A value becomes late after one target interval plus a grace of a tenth of the
interval, at least 10 seconds, and expires after three intervals. The grace
covers a slightly later start or a slower answer of the next read, so a value
read on time never shows as late. Actual request intervals and age are
separately available. An overloaded queue never extends validity. A missing
identity, a refused token, a changed certificate or an unavailable access make
a value unavailable earlier. A pause of the access does not: it only slows the
requests, and values read before stay available until they expire. Freshness
continues to advance independently.

One persistent scheduling wheel assigns four regular slots, one discovery slot,
four regular slots and one explicit slot. With all lanes continuously eligible,
their shares are 80%, 10% and 10%. Empty slots go to other eligible work. Regular
requests follow oldest due time. Each canonical request can be in flight only
once, including when explicit callers are waiting for a regular request.
Missed intervals do not create additional queued copies.

The shared gateway budget permits two concurrent operations and at least
200 ms between starts, measured from the start of a read. The scheduler waits
exactly until both the due work and this spacing allow the next start; work
that is already due starts as soon as the spacing has passed. It also leaves
the final three seconds of each 60-second section for already started work.
Under ideal response times this allows at most 285 starts per section. Setup
uses the same gateway budget and can reduce available capacity. Independent
gateways use stable initial offsets between zero and five seconds. Cloud
intervals are unchanged.

A latency brake protects slower gateways. It follows the gateway time of a
read: from sending the request headers until the response headers arrive.
Setting up the connection (address check, certificate inspection, a new TCP
and TLS connection), reading and decoding the body and other work of Home
Assistant are not part of it, so a busy Home Assistant host or the handshakes
after a fallback from kept connections do not slow the reads. The spacing is at
least 1.5 times the median gateway time of the last 32 reads. After a timeout
on a new connection or a server error (5xx) it doubles for five minutes. It is
never more than one second. A timeout on a kept connection is most likely a
connection the gateway dropped; it only counts for the fallback from kept
connections. A gateway that asks for a pause (429 or 503 with Retry-After), a
connection failure and the fallback keep their own pauses and take
precedence; the brake only widens the spacing between starts. The diagnostics
report the current spacing and the brake under `start_spacing` and the phases
of the reads under `response_phases`.

The next read of a resource is due one interval after the previous read
started, so the time a read takes does not lengthen every interval. A deferred
read stays due and starts again after the spacing. A failed read waits for its
next interval and is never repeated at once, except after a timeout: a known
resource is then read once more as soon as the spacing (and a pause of the
access, if any) allows, so a single timeout does not let its value expire.
Every further timeout in a row waits for the next interval. A known resource
without a usable value, after a restart or after an invalid answer, is asked
again after 30, 60, 120 and 300 seconds, never later than its interval, before
its interval applies again. One failed first read after a restart therefore
never keeps the signals away for 15 minutes or a static value for a day.

The service owns its read tasks and a separate expiry timer. Waiting for a
response, a gateway pause or a checkpoint does not keep old observations fresh.
Closing cancels owned work and waits for cleanup. Cancelling a close waiter does
not cancel cleanup. Current identity must be confirmed explicitly before reads;
old generations, authentication failures and certificate changes stop supply.

## Discovery without device inference

The candidate generator traverses the documented fixed address domains lazily.
It covers 1,172 ordinary addresses; historical and administrative addresses are
excluded. Two prioritized candidates alternate with one remaining candidate,
and address groups rotate. The 196 initial candidates cover heating circuits,
hot water, heat sources, zones, devices, solar and ventilation.

A failed early path does not exclude other paths in the same group. For example,
HC2 maxFlowTemp remains a candidate when its currentRoomSetpoint request fails.
Only a successful response promotes a resource into periodic polling. Candidate
paths and local functional groups do not create Home Assistant devices or prove
equivalence to a PointT resource.

Each search slice has at most 64 admitted attempts or ten minutes of elapsed
time, followed by a five-minute discovery pause. Deferred requests do not count
as attempts and remain pending. Other lanes can continue during the search pause.
When the fixed traversal completes, it restarts no earlier than a day later.
Negative candidates can retry inside later search slices at their own due times.
Validated new hints use the same bounded inventory and search budget.

Only a complete valid signal list updates dynamic discovery. Up to 512 unique
signals are accepted. An oversized or invalid list preserves the previous list.
Two valid lists without a previously known signal stop its polling; a returning
signal requires a fresh value before becoming available. Old unconfirmed search
records absent from the directory are pruned after a day. No entity registry
identity is deleted by this cache cleanup.

Firmware changes reconsider negative records within the normal search budget,
without resetting progress. Catalog changes revisit candidate addresses and
retain compatible cached backoffs; changed response contracts become eligible
for staged rechecking. Retired cache paths are discarded only across a catalog
revision and never become arbitrary network targets.

## Errors, storage and limits

A known path returning 403, 404, 406 or invalid data becomes unavailable
immediately, retaining its last observation and timestamps. Its next regular
request can restore the same record. A timeout after HTTP starts leaves the last
value usable only within its original validity period.

New forbidden or missing paths retry after one, six and then 168 hours (a
week); a changed answer schema keeps that pause. Invalid
responses use 15 minutes, one hour and 24 hours. Timeouts use five, 15, 60 minutes
and then a day. These retries still need a discovery slot. Access failures use
60, 120, 300 and then 600 seconds through the shared persistent budget, also
after a 429. A longer Retry-After is kept up to 24 hours (see
[cloud pauses](cloud-pauses.md)); a short or zero one does not stop the
growing pause. When both running reads fail together (an
unreachable gateway), the read that started before the pause counts as the
same failure. An unexpected problem of the local reads pauses them for 60 to
900 seconds; the same problem is logged as a warning at most once an hour.
A deliberate identity confirmation or the next
successful read resets the access failure counter without clearing a gateway
pause.

Learned pauses of paths that never answered successfully are forgotten when the
access gets a new sign-in or changed settings, and when the gateway reports
another firmware version, also if it changed while Home Assistant was stopped.
An empty signal directory after known signals counts as no answer, not as the
removal of every signal. Signals the gateway stops listing are removed after a
day, also when they were read before, so changing signal IDs cannot fill the
record limit. At the record limit the search pauses until the next daily
rescan instead of retrying continuously.

Checkpoints contain search progress, paths, confirmed-path flags, negative
backoffs, signal membership, the last firmware version and a sign-in and
settings generation number. They contain no measurements or credentials.
The private filename includes a hash of the owner-provided scope. Writes use
atomic private JSON storage with readback, at most once per minute during normal
operation and on close. A crash can repeat work since the last checkpoint. A
restart retains saved remaining pauses conservatively; it never compares old
monotonic measurement times or restores measurements as current. Dynamic signals
need a fresh directory before polling resumes. Failed storage stops this service;
an interrupted initial load cannot overwrite the previous checkpoint.

Each service permits at most 2,048 resource records, eight pending explicit
requests and eight explicit operations. Identical operations share their result
and original deadline. An explicit operation has 120 seconds including waiting,
at most four reads and at most 1 MiB of response bodies. It does not split or
paginate automatically. Inclusive history windows are limited to seven days for
PT1H, 31 for P1D, 366 for P1M and one day for all.

Retained observations are limited to 8 MiB of represented UTF-8 JSON and 65,536
JSON value nodes. These are accounting limits, not an exact Python heap size.
Rejected content never becomes fresh. Each response keeps the existing 256 KiB,
depth and node limits. The checkpoint file is limited to 1 MiB. Results retained
by callers remain the callers' responsibility.

## Validation and managed integration

I tested growing and overloaded synthetic populations, later heating circuits,
solar paths, signal changes, queue fairness, explicit deadlines, independent
expiry, restart checkpoints, cancellation and storage failures. A loopback TLS
test connects the scheduler to the actual local read client. A separate memory
test fills the inventory with large schema-valid decoded responses and measures
the traced allocation peak. These tests do not certify real gateway capacity.

The managed runtime publishes resource availability and validates the native
setup flow with running local data supply. Synthetic checks do not replace real
installation acceptance. No real appliance request, installation or release is
part of this change.

See [shared local budgets](local-budget.md), [local HTTPS reads](local-read-client.md)
and the [resource catalog](local-api-resources.md).
