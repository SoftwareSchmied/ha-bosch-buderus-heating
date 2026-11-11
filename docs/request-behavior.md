# Request behavior and recovery

The later [connection recovery corrections](connection-recovery.md) cover
cancelled token-refresh waiters, OAuth request limits and reauthentication.

This document describes the behavior in **0.8.1-beta.1**, compared
with stable 0.8.0. The changes make nonessential startup data less disruptive
and reduce repeat reads. They do not change the configured heating settings
or the normal polling intervals for operating values.

## Start-counter recovery in 0.8.2-beta.2

I keep discovered start counters in the slow group, including while their
current schema or value is unusable. I also restore exact registered counter
paths during bounded discovery. Failed optional probes do not become regular
polls; recovery is limited to registered counters and explicitly referenced
leaves that discovery attempted.

The existing profile controls timing: 15 or 30 minutes. These paths share the
discovery budget and normal batching, authentication and backoff handling.
Recovering an omitted path can add requests, so unchanged intervals do not
imply unchanged request counts. The [counter report](start-counters.md#recovery-and-request-cost)
describes the tested workload, 404 and 403 handling, and remaining limitations.

## What changes

| Data or action | 0.8.0 | 0.8.1-beta.1 |
| --- | --- | --- |
| `/gateway/thirdPartyLicenseInformation` | Could enter discovery and individual recovery reads, although no entity used it. A service failure could abort discovery. | This exact endpoint is never requested. If encountered, diagnostics record that it was omitted. Other `/gateway` paths are unaffected. |
| `/devices/{reported-id}/productName` | A read-only scalar normally joined the slow group, every 15 or 30 minutes. | Read in the supplementary phase. After a valid read-only scalar response, keep the value until the integration is loaded again. Retain existing sensor IDs and user preferences. |
| `/heatSources/numberOfRefrigerantCircuitsInstalled` | A read-only scalar normally joined the slow group, every 15 or 30 minutes. | The same successful-once policy, with validation of type, unit and a finite whole-number count from 0 through 65535. |
| `/gateway/tosAccepted`, `/system/dateTime`, `/gateway/wifi/mac`, `/devices/{reported-id}/inclusionTime`, `/devices/{reported-id}/sgtin` | Could be read inside the common discovery tree. | Defer these reviewed leaves until functional discovery finishes. Cache valid read-only scalar responses for this load. Do not create entities from these administrative values. |
| Other static resources, such as firmware and manufacturer information | Read through ordinary discovery, without periodic polling when classified static. | Keep their existing discovery behavior. The new supplementary retry policy applies only to the reviewed paths above and bounded children actually referenced by their responses. |
| Functional values and controls | Discover supported resources, then poll their assigned groups. | Keep the same groups, write contracts and read-back checks. Supplementary reads cannot turn a failed functional cycle into a successful one. |

Device IDs must come from actual references, an existing curated discovery
candidate or an existing sensor in the same config entry and gateway. The
integration does not enumerate guessed device or heating-circuit numbers.
Only the listed leaf paths are deferred. Whole directories are not excluded.

## Exactly what "once" means

"Once" means **one accepted response per resource during one integration
load**, not one attempt and not once for the lifetime of the installation.
Responses without a usable value are not treated as success.

| Event | What happens to successful supplementary values and pending work |
| --- | --- |
| Normal polling, opening diagnostics or enabling debug logging | Retain the current cache. Successful supplementary reads do not repeat. |
| A temporary failure of a supplementary request | Keep that path pending. Retry later under the rules below, without restarting Home Assistant. |
| Home Assistant automatically retries an unfinished setup | Retain successful supplementary reads, pending work and the distinct-path budget when gateway selection, options and credentials are unchanged. |
| Ordinary access-token refresh | Retain the cache. Rotating tokens during the existing sign-in session does not restart metadata discovery. |
| Explicit integration reload, including reload while setup is retrying | Start a new metadata session and run discovery again. This also rereads previously successful supplementary values. |
| Reconfiguration that reloads the integration, changed selected gateways, changed options at the next setup, or replaced credentials | Start a new metadata session. Values are never reused across a changed setup. |
| Home Assistant restart | All supplementary memory is cleared. The next integration setup reads the data again. |

The cache is held in memory, separately for each config entry and gateway.
No supplementary cache file is written. The account-wide rate-limit pause is
retained separately across integration reloads and automatic setup retries.
Removing the entry clears its pause; restarting Home Assistant clears memory.
Reloading is therefore not a way to bypass an active cloud rate limit.

## Startup order and time budgets

1. Discover functional resources for **every selected gateway**. Reported
   references take priority over heating-circuit core paths, which in turn
   precede other optional candidates. Existing bounded fault probes remain.
2. Give supplementary reads a **single 30-second budget for the whole entry**,
   starting only after that functional work. This is not a 30-second limit on
   total setup time and is not 30 seconds per gateway or per path.
3. Take at most 30 paths in each supplementary batch. Cycle through gateways
   while time remains. Several batches may run in this initial phase.
4. When time runs out, defer unsent work until at least the next minute and
   finish setup with the usable functional resources. An unsent path gets
   `budget_wait`, with no invented HTTP attempt or failed request.

Core discovery and supplementary requests share the limit of **512 distinct
requested paths per gateway and metadata session**. The later search for
parts missing at start (see below) is charged to the same limit. Reference
depth is limited
to eight, with roots at depth zero. The supplementary queue also holds at most
512 paths. These bounds survive automatic setup retries. Retrying an already
requested path does not consume another distinct-path slot. A `resource_limit`
or `depth_limit` result is a safety stop for this session, not an ordinary
temporary failure that will be retried indefinitely.

On a room controller, the setting leaves of the listed radio devices are read
last and have a limit of their own of 192 paths, outside the 512. The first
leaf of every device comes first, then the second, so a limit drops the least
important leaves of all devices. A room controller answers HTTP 500 for a
leaf it does not have; for optional probes and zone detail leaves I take such
an answer in a batch as "missing" without a single read of its own.

## Recovery after startup

Recovery uses existing coordinator wake-ups. It does not add a separate timer
or request another gateway listing. In both polling profiles, coordinators
wake approximately once a minute, but only due groups issue normal requests.

After a gateway's normal work, supplementary recovery may run if that cycle
did not fail. A gateway whose preceding update is still unsuccessful does not
use an empty wake-up to declare itself healthy. Healthy gateways with pending
work take turns, and older due paths precede paths that have just failed.

There is at most **one recovery batch of 30 paths per config entry per minute**,
across its gateways. Each recovery batch has a 30-second deadline. This is a
ceiling, not a promise of one request every minute. When no path is due, there
is no supplementary request. Normal operating reads take priority and can
postpone recovery. Missed deadlines do not cause a burst of catch-up batches.

| Supplementary result | Recovery and effect on setup/entities |
| --- | --- |
| Valid read-only scalar with the reviewed schema | Cache it with the actual read time. Stop periodic reads for the rest of this load. |
| Timeout, connection/service failure, malformed response, missing requested item or unusable value, including an empty HTTP 200 | Keep the affected paths pending. The delay after successive failures is **60, 120, 300, 900, then 1800 seconds**. Further failures keep the 1800-second delay; there is no lifetime attempt limit. |
| HTTP 403, 404 or 406 for an advertised metadata sensor or an existing registered metadata sensor | Retain it as expected and retry no earlier than **1800 seconds** later. These local errors do not fail the complete integration. |
| HTTP 403, 404 or 406 for diagnostic-only data or an unconfirmed optional candidate | Mark it unsupported for this load. Do not keep requesting an unsupported informational path. A new explicit discovery can reconsider it. |
| HTTP 429, including a rate-limited item inside a successful bulk envelope | Respect the account-wide pause before any further PointT read. Use `Retry-After` where supplied, bounded to 60 through 3600 seconds; otherwise pause for 300 seconds. Per-path deadlines still apply afterward. |
| An expired access token that can be refreshed | Keep the existing single token refresh and repeat the PointT request once. This is distinct from a transport retry. |
| A 401 for a single item inside an accepted bulk request | Treat it like the regular poll does: a temporary failure of that path only, retried with the delays above. The request itself was accepted with the current token, so no new sign-in is requested. |
| Authentication of the request still fails | End the metadata session and let Home Assistant request reauthentication. Do not hide an invalid sign-in as an optional-data problem. |
| Cancellation or expiry of the batch deadline | Release the recovery reservation. Attempted paths remain pending with the appropriate cancellation/timeout result. Cancellation is not counted as HTTP success. |
| A reviewed leaf unexpectedly contains references or is writable | Route the confirmed resource through normal catalog handling. Follow only actual references within allowed roots and the shared bounds. Do not freeze a writable control as static metadata. |
| Reply for another gateway, an unrequested path or a different resource path | Do not store it as the requested value. Leave the missing/invalid requested data eligible for recovery. |

Retry delays start when the failure is recorded. An actual retry occurs on the
next eligible wake-up after the deadline, subject to healthy functional work,
gateway rotation, batch capacity and rate limits. The integration does not
promise recovery at an exact wall-clock second. If the first five failures
finish at the earliest possible times, the waits before the next attempts are
1, 2, 5, 15 and 30 minutes, not all measured from the original failure.

Supplementary reads use one logical batch call with **no immediate individual
GET fallback and no automatic retry for network/5xx errors**. A recoverable 401
may still cause the token refresh described above. Consequently, a logical
batch can have two actual PointT HTTP attempts. Normal discovery and normal
polling retain their existing bounded retry and fallback rules; disabling
those retries is specific to supplementary reads.

In discovery, as in normal polling, an HTTP 5xx or other status answer to one
individual fallback read stays local to that path, and discovery continues
with the remaining paths. A timeout or network failure without any answer, a
sign-in failure or a rate limit still ends the discovery at once. Three
fallback reads in a row that answer HTTP 5xx end it as well, so a failing
cloud does not receive up to 30 individual reads per batch.

## Entity continuity and limits

An already registered product-name or refrigerant-count sensor is created with
its existing identity even if its value is temporarily missing. It remains
unavailable until a valid response arrives. Recovery updates that same entity
without a restart and preserves its custom name and enable/disable choice.
A newly recovered supported resource can add its entity during normal operation.
Default-disabled sensors appear in the entity registry but remain disabled
unless the user enables them.

This is not continuous rediscovery of the entire installation. A physically
added circuit or a new path that was never reported or scheduled still needs
[rediscovery](reconfiguration.md). The one exception is a part that an earlier
start discovered and that is missing now: the integration searches that
subtree again 5, 15 and 60 minutes after the start and then every six hours
until one day has passed. A search waits for a working cycle, a rate limit
postpones it, and it is charged to the same path limit. A missing path that
answers HTTP 5xx during the search uses up that search. Only a search without
any answer, or one that the cloud ends with three 5xx answers in a row, is
postponed, at most 24 times; then the search waits for the next start, which
still expects the part. A part still missing after the last search is no longer expected at
the next start; its entities stay in the registry. Without stored resources, a
root that ended with a temporary error is searched again on the same schedule. Other static resources outside the reviewed
list retain their previous behavior. Previously discovered dynamic values
returning 404 already recover on the next regular group read, with their
existing entities. Fault probes, other path pauses and the gateway circuit
breaker retain their existing policies.

Supplementary success does not refresh an unrelated measurement's timestamp,
confirm that a heating fault has cleared, or make an unsuccessful functional
poll healthy. Stored values and unavailable states are not fabricated.

## Normal polling and request savings

| Group | Standard profile | Cloud-friendly profile |
| --- | --- | --- |
| Operating values | 60 seconds | 120 seconds |
| Notifications without an active notification | 300 seconds | 300 seconds |
| Notifications while active | 60 seconds | 60 seconds |
| Controls and energy counters | 300 seconds | 600 seconds |
| Slow values, such as operating hours | 900 seconds | 1800 seconds |
| Successfully read supplementary metadata | No periodic requests | No periodic requests |

A control whose entities are all disabled in Home Assistant is polled with the
slow values instead (900 or 1800 seconds). Controls that the climate, hot water,
holiday and calendar entities read keep the control group. Enabling an entity
moves its control back to the control group within seconds, without a restart;
a cloud value older than the control interval is then read at once. A reading
that is also shown from the local access keeps the slower of the two rates.

Due functional groups share batches of up to 30 paths. Group scheduling permits
one second of timer-rounding tolerance; retry deadlines and rate-limit pauses
are not shortened by that tolerance. Normal writes retain metadata validation
and read-back confirmation and are never blindly retried.

A write waits at most 60 seconds for a running poll or another write of its
gateway; after that it sends nothing and says so. Since 0.9.0-alpha.6 a write
that has started also has an overall deadline of 60 seconds (180 seconds for a
holiday change) for its fresh reads, its one request and the read-back. With a
slow cloud it then no longer holds the polling of its gateway and the calling
automation for minutes. At the deadline it ends as not confirmed: the request
may already have been sent, so the reading involved is no longer shown as
current until it is read again, and nothing is sent a second time.

Since 0.9.0-alpha.3, paths of the groups read every 5, 15 or 60 minutes are
spread over the minutes before their group's deadline where the batch of that
minute has room left, instead of all falling due together and needing extra
batches. Each path keeps its group's interval; only the minute within it
moves, and a path read early is not read again before its group's deadline.
A wake-up without a batch never gets one this way, and when no minute has room
the deadline itself is used, as before. Small plants therefore keep their
previous timing. With the anonymized cloud inventory of my heat pump (20 live
values, 32 paths every 5 minutes, 16 every 15 minutes and 9 hourly paths) a
simulated hour needs 60 batches instead of 76, with the same 1,657 path reads.
If a group cannot complete because a batch with another of its paths keeps
failing, a path read early (or in an earlier, budget-limited cycle) waits at
most one interval of its group and one minute; then it is read again, so its
value does not expire. The first reads after a start or a requested discovery stay immediate.

A batch that fails as a whole beside answered ones keeps its group due. The
paths answered in that cycle are then not read again before their own group
interval has passed; only the failing batch is repeated each minute, as a
batch of its own read after the others, so its failure does not take other
readings with it. A cycle in which only that batch was read and failed is no
gateway failure, as when it failed beside the others; a cycle in which other
readings fail too is. The batch warning appears once and ends only after a
cycle in which every batch answered.

An explicit refresh, for example the "Update entity" action, reads every
group. A further explicit refresh within the shortest group interval of the
profile (60 seconds, or 120 seconds with the cloud-friendly profile) reads
only the control group, so an automation that asks every few seconds does not
read the whole plant each time.

While an entity shows the local value of a paired reading, its cloud path is
read every 15 minutes only and at once again at its normal rate when the cloud
value is shown again; see
[source selection](source-selection.md#cloud-reads-while-local-values-are-shown).

For **each** eligible product-name or installed-circuit-count path, stopping
slow polling avoids 96 scheduled path reads per day in the standard profile or
48 in the cloud-friendly profile after success. These are **path reads**, not
necessarily separate HTTP requests: several paths can share a batch.

A local HTTP test with 30 fast paths and two such metadata leaves measured one
hour of steady polling, after startup, without simulated failures:

| Profile | 0.8.0 HTTP requests | New HTTP requests | Metadata path reads before / after |
| --- | ---: | ---: | ---: |
| Standard | 64 | 60 | 8 / 0 |
| Cloud-friendly | 32 | 30 | 4 / 0 |

These figures cover that batch layout, not every installation. When metadata
fits into an existing batch, the benefit is fewer requested paths and smaller
responses rather than fewer HTTP requests. Separating startup metadata can
add a dedicated initial batch. Failed supplementary reads also need later
recovery requests. The test does not establish faster startup, a guaranteed
percentage reduction, or real-world uptime on a physical heating system.

## Capacity with several systems

All PointT requests of one saved cloud access share three request slots:
regular polling of every gateway, wallboxes, startup metadata, device checks,
reconnection and confirmation reads. Each gateway sends its batches one after
another and never overlaps its own cycles. If a cycle takes longer than its
interval, the next one starts later instead of running in parallel.

I measured the actual coordinator, client and slot limit against a local server
with fixed response times of 20 and 50 ms. The table scales the measured cycle
length at 50 ms, in multiples of one response time, to the average response
time of 0.56 seconds observed on my installation and to a pessimistic 2 seconds:

| Systems × operating values | Batches per minute | Cycle at 0.56 s | Cycle at 2 s |
| --- | ---: | ---: | ---: |
| 1 × 40 | 2 | about 1.5 s | about 6 s |
| 3 × 120 | 12 | about 4 s | about 15 s |
| 5 × 256 | 45 | about 13 s | about 48 s |
| 10 × 512, the discovery maximum | 180 | about 55 s | about 200 s |

No more than three requests ran at once, and all gateways progressed evenly.
A device check issued during full load waited roughly the duration of one to
ten responses. These are synthetic measurements, not a statement about Bosch
cloud limits. Large accounts may therefore prefer the cloud-friendly profile.

Diagnostics report the longest cycle per gateway, how often a cycle exceeded
its interval and how long requests waited for a free slot.

## Manual heating-circuit setpoint recovery

When a reported heating circuit is confirmed to be in Manual and its
`manualRoomSetpoint` is missing or unavailable, the coordinator can make up to
three targeted reads. It waits **3, 10 and 20 seconds in succession**, so the
earliest starts are approximately 3, 13 and 33 seconds after scheduling, plus
request and lock-wait time. These reads disable immediate transport retries;
the existing access-token refresh may still repeat an HTTP request once.

There is one series per continuous unavailability episode and at most one task
per circuit and gateway. Repeated failure polls do not renew the series. A
successful setpoint read or a confirmed departure from Manual resets the
episode. Recovery stops when the target is fresh, Manual is no longer confirmed,
the integration unloads, or a connection, service, authentication or rate-limit
error prevents further reads. Existing cloud, circuit-breaker and per-resource
pauses apply. A 429 item in a successful batch envelope also activates backoff.

After the series, a previously discovered target keeps its normal polling.
An absent startup target remains a candidate only for that reported circuit's
exact setpoint path, while Manual is confirmed. It joins the regular control
group reads every **5 minutes**, or **10 minutes** in the cloud-friendly profile.
Leaving Manual removes that provisional candidate. A successful response joins
normal catalog polling and can create supported entities using its actual
metadata. Unknown circuits and other failed optional discovery paths are not
added by this mechanism. The shared distinct-path budget still applies.

A 404 for this provisional target does not install a 24-hour absence pause.
403 pauses remain unchanged. Targeted results update only the affected resource;
they do not declare a failed main cycle healthy or reset its polling timer.

The 3/10/20-second sequence and the episode limit above are integration
policy. How long a particular installation takes to expose its setpoint has not
been established. No appliance write test is claimed for this change.

## Diagnostics and privacy

Schema 14 distinguishes functional discovery, omitted license data and
supplementary pending/recovered reads. It includes actual attempt/success
times and earliest retry times. `requests_by_type.startup_metadata` counts
physical PointT attempts; `startup_data.logical_attempts` counts logical
resource attempts. A single 30-path batch contributes up to 30 logical resource
attempts but usually one physical HTTP request.

The diagnostic download and debug status messages use existing local state
and make no new cloud requests. Debug messages contain sanitized paths and
result categories, not raw metadata bodies. The technical-value allowlist
contains known product/manufacturer labels, strict numeric firmware/hardware
versions and the validated circuit count. Unknown product text, network
identifiers, installation timestamps, current operating values and credentials
are excluded. The structural inventory exporter also removes the allowlisted
technical values. See [Diagnostics](diagnostics.md) for field names and scope.

## Kurz erklärt

„Einmalig“ heißt: einmal **erfolgreich pro Laden der Integration**. Bei einem
Home-Assistant-Neustart oder bewussten Neuladen werden diese Zusatzdaten erneut
gelesen. Ein automatischer neuer Einrichtungsversuch behält bereits erfolgreich
gelesene Zusatzdaten, sofern die Konfiguration und Anmeldung unverändert sind.

Vorübergehend fehlende Zusatzdaten werden ohne Neustart nachgeladen. Bestehende
Sensoren behalten ihre Identität und werden nach erfolgreichem Lesen wieder
verfügbar. Die Wartezeiten steigen nach Fehlern von 1 auf 2, 5, 15 und 30 Minuten
und bleiben danach bei 30 Minuten. Cloud-Sperrzeiten und reguläre Anlagenabfragen
haben Vorrang. Nicht unterstützte reine Zusatzpfade und Sicherheitsgrenzen sind
ausdrücklich von dieser fortgesetzten Wiederholung ausgenommen.

Ein Schreibvorgang wartet höchstens 60 Sekunden auf eine laufende Abfrage und
sendet sonst nichts. Seit 0.9.0-alpha.6 hat ein begonnener Schreibvorgang
außerdem eine Gesamtfrist von 60 Sekunden (Urlaubsänderungen 180 Sekunden) für
die frische Prüfung, die eine Anfrage und das Zurücklesen. Danach gilt er als
nicht bestätigt; der betroffene Wert gilt bis zum nächsten Lesen nicht mehr
als aktuell, und nichts wird ein zweites Mal gesendet.

Die Integration spart wiederholte Metadatenabrufe und lässt unwichtige
Zusatzabfragen die funktionierende Einrichtung nicht mehr abbrechen. Neue,
bisher unbekannte Anlagenbestandteile benötigen weiterhin eine neue Discovery.

Für einen fehlenden manuellen Heizkreis-Sollwert gibt es zusätzlich höchstens
drei gezielte Abrufe pro zusammenhängendem Ausfall. Die Wartezeiten von 3, 10
und 20 Sekunden folgen aufeinander. Weitere Fehler-Polls starten keine neue
Serie. Nach den Versuchen greift der reguläre Abfragerhythmus. Ein beim Start
fehlender Sollwert wird dabei nur am tatsächlich gemeldeten Heizkreis und bei
bestätigtem Handbetrieb weitergeprüft. Eine erfolgreiche Antwort kann das
Bedienelement ohne Neustart ergänzen. Vorhandene 403-Pausen und Cloud-Sperrzeiten
bleiben erhalten.
