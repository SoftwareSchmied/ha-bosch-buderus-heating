# Diagnostics and request metrics

Home Assistant can generate a diagnostics report for Bosch/Buderus Home Comfort.
It is intended for troubleshooting and support and does not make additional
cloud requests.

Schema 28 adds an `authentication` section for investigating repeated sign-in
requests. It is built from memory and local storage, without a cloud request,
so it does not depend on a working sign-in. It contains:

- `current`: remaining access-token lifetime in seconds, whether a refresh
  token exists, its relation to the recorded history, the granted scope names
  and whether renewal stopped after a rejection;
- `events`: the last 60 sign-in events with UTC time and age. Starts record
  whether the stored refresh token is the newest known one. Renewals record
  their trigger (`expiry` or `access_token_rejected`), duration and outcome,
  and on success the reported access and refresh lifetimes, whether a new
  refresh token was issued and whether it was saved;
- for a rejected renewal: HTTP status, OAuth error code, a sanitized
  description of at most 120 characters, body format and content type;
- `presented_refresh_token`: whether the rejected token was the newest known
  one, how long ago it was first seen, how often it had been presented before
  and whether the service had accepted it before;
- `issued_refresh_tokens` and `counters`: anonymous rank, source, age and use
  count of recently seen refresh tokens, plus totals per outcome.

Refresh tokens are recognized through an HMAC fingerprint whose random key
stays in Home Assistant's local `.storage` folder. Neither the key, the
fingerprints nor any token are exported. Long opaque strings and anything
resembling an e-mail address are removed from the service description. The
history is stored in `.storage/bosch_buderus_heating.auth_history.<entry>`
and deleted together with the integration entry. It survives restarts, so the
history starts with the first version that records it.

A rejected renewal is also written to the Home Assistant log as a warning that
starts with `SingleKey ID rejected the token refresh`. A start with stored
sign-in data older than the newest received token and a refreshed token that
could not be saved are logged as warnings as well.

Schema 23 adds aggregate Midea Climate Series identification and stream
freshness counts under `air_conditioners`. No climate settings, values or
identifiers are included. See [air conditioners](bacon-air-conditioners.md).

Schema 22 adds counts of identified Midea/RWH water heaters and current
metadata/sensor streams. These counters describe the received data, not
registered HA entities. No device values, model strings or identifiers are
included. See [water heaters](bacon-water-heaters.md).

Schema 21 adds aggregate connection information for additional cloud devices.
It includes selected and available device counts, received-state counts,
connection error categories and unconfirmed-command counts. It omits device
identifiers, names, network details and message contents.
See [the connection foundation](bacon-connection.md).

Schema 19 adds holiday format flags for modern, ICOM and WDDW2 resources.
They report available contracts without dates, names, assignments, current
modes or temperatures. Calendar write availability includes editable ICOM
slots even when there is no free slot. Unknown holiday slot identifiers are
redacted. See [older holiday formats](legacy-holidays.md).

Schema 18 added `duration_contract` and `collection_contract`. Duration
diagnostics expose schema eligibility, a canonical source unit, the source
of a default unit, conversion to hours and fixed validity reasons per part.
Collection diagnostics explain why the DHW monitor collection is not a scalar
and that its advertised references are read separately. Current readings and
unknown unit text are excluded. See [read contracts](resource-contracts.md).

Schema 17 added validation details for the WDDW2 safety state without its value.
It retains the schema 16 water-softener device profiles, schema validation and local
entity registration information. For each known softener resource it
distinguishes an unregistered entity from a disabled, unloaded, unknown or
unavailable entity. Entity names, entity IDs and state values are omitted.
See [water softeners](water-softener.md) for the evidence and remaining
physical-device checks.

Diagnostics remain downloadable while the first integration setup is still in
`setup_retry`. Before runtime initialization they contain only redacted config
metadata and no gateway data. Once the runtime exists, they also include
request metrics and any partially initialized gateway coordinators.

## Download diagnostics

1. Open **Settings → Devices & services**.
2. Open **Bosch/Buderus Home Comfort**.
3. Open the integration's three-dot menu.
4. Select **Download diagnostics**.

Always review the file before publishing it. Automatic redaction is an
additional layer of protection, not a replacement for reviewing a file before
attaching it to a public issue.

## Included information

- selected brand and number of configured gateways;
- anonymized device class such as `k40`, `k40rf`, `mx300`, or `mx400`;
- concrete canonical logical paths such as
  `/heatingCircuits/hc2/operationMode`, while device identifiers and
  unrecognized dynamic IDs remain redacted;
- resource type, unit, polling group, maturity, default activation, and
  writeability;
- counts of allowed options, references, and structured subvalues;
- scalar-control eligibility and a specific rejection reason, known advertised
  enum options, and numeric bounds for released controls;
- availability, freshness, error category, and consecutive failure count;
- per-resource polling-pause state and remaining pause time in seconds;
- whether a supported scalar resource currently provides no value;
- bounded counts of undeclared enum values observed after entity creation;
- counts of active negative pauses, rate-limit backoff, and circuit-breaker
  state;
- aggregated request and polling metrics;
- a bounded, sanitized log of recent PointT HTTP attempts.
- supported fault-resource templates, active fault and notification counts,
  severity counts, known active codes, last successful fault read, and parser
  status.

The report explicitly excludes current measurements, settings, and energy
values. It also excludes raw notification payloads and installation-specific
component IDs. Error codes remain visible because they are required to match a
diagnostic report with the appliance display.

## Start counters

Schema 15 adds `start_counter` to start-counter capability entries. It reports
`schema_supported`, `schema_rejection_reason`, `error_metadata_valid`,
`error_marker_count` and a `readings` map for the scalar or known parts.
Reading statuses include `valid`, `missing_value`, `not_numeric`, `not_integer`,
`not_finite`, `out_of_range`, `device_error_value`, `invalid_error_metadata`,
`invalid_structure`, `conflicting_values`, `not_read` and `unavailable`.
No current counts, raw marker numbers or unknown field names are included.

The per-gateway `start_counter_recovery` section reports registered-path count,
paths waiting for their first response and whether the shared path budget
prevented recovery. Existing per-resource pause information still explains
deferred reads. The inventory exporter accepts schema 15 and continues to
discard technical values and additional counter details. See
[start-counter behavior](start-counters.md) for examples and limitations.

## Missing controls

Schema 15 retains `control` under each entry in `gateways → capabilities`.
The same assessment drives the select, number, and switch implementations:

- `platform`: the principal control type or `null` for unsupported paths;
- `eligible` and `rejection_reason`: whether the current metadata supplies a
  valid write contract, and the first failed check;
- `writable`: the resource's advertised permission;
- `current_value_issue`: a separate issue with the current state, which does
  not hide valid target options or bounds;
- `enabled_by_default`: the function's initial registry setting. Maximum flow
  temperature remains disabled by default.

This is a capability assessment, not an entity-registry inventory or a physical
write test. Availability and freshness are reported separately. Discovered
resources that become eligible are added during polling without restarting HA;
new paths still need discovery. Existing user enable/disable choices remain.

Enums accept all valid advertised string codes, including subsets and new
codes. `offered_option_count` counts those codes. `advertised_known_options`
contains only recognized codes, and `unrecognized_option_count` counts other
entries without exposing their text. `requires_all_options` is always false.
`switch_supported` and `select_supported` describe the available representations;
a switch variant may also have a separate select while preserving its switch ID.

Numeric `minimum`, `maximum`, and `unit` describe this resource's contract.
`ui_step` is only a display increment. `write_step` is `null` because the observed
schema does not establish a write increment. Reference-device safety-envelope
fields were removed. Non-finite bounds are exported as `null`. These fields
never include the current setpoint.

| Rejection reason | Meaning |
| --- | --- |
| `no_scalar_control_policy` | The function has no implemented scalar write contract. |
| `not_writable` | The resource does not advertise write permission. |
| `unsupported_resource_type` | The type does not match the function. |
| `missing_options` | No enum options were advertised. |
| `invalid_options` | The enum contract contains non-string, empty, or non-printable values. |
| `unsupported_unit` | The unit does not match the supported physical quantity. |
| `missing_bounds` | A numeric minimum or maximum is absent. |
| `non_finite_bounds` | A bound is NaN or infinite. |
| `inverted_bounds` | The minimum exceeds the maximum. |

State issues such as `missing_value`, `invalid_value_type`,
`current_option_not_advertised`, `non_finite_value`, or `value_out_of_bounds`
appear in `current_value_issue`, separately from metadata eligibility.

If a path is absent from `capabilities`, check `discovery → paths` to determine
whether it was attempted, failed, or was never scheduled. Controls are not
created for resources that were not discovered. Downloading this additional
evidence performs no cloud requests and includes no raw current values.

For advertised heating circuits, discovery also tries a small catalog of core
control paths even if the circuit directory is unreadable. These entries have
`source: catalog` unless PointT explicitly references them, in which case their
source is `reference`. `catalog_paths` and `catalog_paths_discovered` count them
both across discovery and within each `discovery → groups` entry. A failed
catalog probe is diagnostic evidence, not an invented resource or control.

Each capability includes `polling_paused` and
`polling_pause_remaining_seconds`. These describe only a local path pause;
account-wide rate-limit backoff and the gateway circuit breaker are reported
under `runtime`. Zero does not mean a request is immediately due: the regular
polling group still determines the next read. A previously discovered resource
returning 404 is unavailable but has no local 24-hour pause. It is tried again
at its next group poll and recovers on a valid response. Pauses for repeated
403 responses, gateway timeouts, and never-successful optional fault probes
remain visible here. Static resources have no recurring polling group.

Known optional fault paths that have never returned a resource remain as
capability placeholders and are checked again after their 24-hour pause. They
count toward the inventory but not toward available runtime resources. Historical
failure lists are checked at startup only.

## Fault parser status

`faults.parser_status` describes the last actual parsing attempt for a current
notification resource. `not_run` means none has been parsed since startup;
`empty` means the last attempt found a valid empty list; `ok` means it found
valid notifications; and `partial` means invalid entries were encountered.
Unrelated temperature, energy, or settings reads preserve this result.

This field is not a connectivity or current-health indicator. A subsequent
failed request remains visible in `resource_results` and the existing fault
availability handling, even when the last parser result was `empty`. Unrelated
reads do not advance `last_successful_update` or confirm a fault resolution.
These fault-parser fields retain their existing meanings in schema 15.

## Supplementary startup data

See [Request behavior and recovery](request-behavior.md) for the complete path
list, successful-once definition, timing, safety limits and entity lifecycle.

`startup_data` reports deferred reads separately from functional `discovery`.
Each path includes its advertised/registered origin, status, logical attempts,
actual attempt/success times, and seconds until the earliest retry. Actual HTTP
attempts are counted under `request_metrics.requests_by_type.startup_metadata`.
An access-token refresh can cause two HTTP attempts for one logical batch.
`omitted` records the unused license endpoint without counting a request or
failure. `budget_wait` means a read was not sent before the shared deadline.

Successful reviewed metadata is retained in memory for the current integration
load. Temporary failures retry after 60, 120, 300, 900, then 1800 seconds between
failures, using the next ordinary coordinator wake-up. Delays remain at 1800
seconds for further transient failures. Rate-limit pauses take precedence.
Advertised or previously registered metadata sensors with 403/404/406 retry
after 1800 seconds. Unsupported diagnostic-only paths and unconfirmed optional
probes with these local errors do not repeat continuously.

Existing metadata sensors remain unavailable while pending. Recovery restores
the same sensor or creates a newly discovered supported sensor. A diagnostic
download and debug logging do not trigger additional requests.

`technical_metadata` contains only reviewed technical values: known product
labels such as MX400, recognized manufacturer names, strictly numeric dotted
firmware/hardware versions, and a validated installed circuit count. Unknown
product text, network identifiers, device timestamps and personal names are
not exported. Whenever such technical values are present, the privacy block
truthfully sets `contains_raw_resource_values: true` and
`raw_resource_values_scope: allowlisted_technical_metadata`. Measurements,
current settings, credentials and stable identifiers remain excluded. The
anonymized inventory exporter drops this entire section.

## Request metrics

Metrics are stored in memory only and reset when Home Assistant restarts. They
cover:

- actual HTTP attempts grouped by category and method;
- HTTP status classes such as `2xx`, `4xx`, and `5xx`;
- outcomes such as success, timeout, rate limiting, and protocol errors;
- retries and bounded single-request fallbacks;
- batch count and maximum batch size;
- successful and failed items within batch responses, including a separate
  count for HTTP-successful items whose payload could not be parsed;
- separate status-class counts for the PointT `serverStatus` and the inner
  gateway response, so a bulk frontend failure can be distinguished from a
  gateway-side failure without recording paths or response values;
- bounded individual fallback requests grouped by privacy-safe reason, such as
  malformed payload, bulk-server 5xx, or gateway 5xx;
- latest, average, and maximum request duration;
- rolling request totals and successful response times for the last 60 minutes;
- up to 250 recent HTTP attempts from the last 60 minutes, including request
  type, method, HTTP status, outcome, duration, attempt number, retry flag,
  bulk size, and fallback reason;
- for successful bulk envelopes, separate aggregated `serverStatus` and inner
  gateway-status counts;
- coordinator poll count, failures, and duration;
- detected decreases in cumulative energy counters.

### Discovery details

Every gateway report contains a `discovery` section. It records how many paths
were scheduled, requested, and discovered; how many were advertised references
or optional probes; and the success or failure counts for individual fallbacks.
The `groups` section aggregates these counters for each logical circuit or
subsystem, while `paths` shows the bulk result, fallback reason, fallback result,
and final discovery state, including paths left `not_attempted` at a safety
bound. `paths_failed` counts requested paths without a recovered resource.

`attempts_scope: logical_resource_reads` distinguishes this work from actual
HTTP traffic. `bulk_calls` counts logical calls containing up to 30 paths;
`paths_requested` counts distinct paths included in those calls, and
`fallback_attempts` counts logical individual reads. A transport retry does not
increase these discovery counters. Actual HTTP attempts and retries remain in
the account-wide `request_metrics`; a batch shared by two circuits must not be
counted twice as cloud traffic.

`completed` is false when discovery stops for a resource or depth limit,
authentication failure, rate limit, or request-wide transport error.
`stop_reason` names the cause, including `resource_limit`, `depth_limit`,
`authentication_error`, `rate_limited`, `timeout`, and `service_unavailable`.
Individual resource failures remain visible without stopping later circuits.

Canonical circuit identifiers such as `hc2`, `dhw1`, and `hs1` remain visible
because they are necessary to distinguish missing resources on multi-circuit
installations. Arbitrary device IDs and unrecognized dynamic identifiers are
replaced with placeholders. Credentials, gateway IDs, request URLs, payloads,
and resource values remain excluded. Opening diagnostics causes no cloud calls.

URLs, resource paths, gateway IDs, payloads, and response values are not stored
in `request_metrics`. The recent-request log is memory-only, bounded, and cleared
when Home Assistant restarts. Its sequence number and age are local diagnostic
values, not cloud identifiers or wall-clock timestamps.

### Simple overview

`request_metrics` contains immediately understandable totals:

- `observation_seconds`: time since the integration started;
- `requests_total`: cloud requests that were actually made;
- `requests_successful` and `requests_failed`: successful and failed cloud
  requests;
- `success_rate_percent`: request success rate;
- `requests_per_hour`: projected hourly cloud load. This is calculated only
  after an observation period of 60 seconds;
- `rate_limit_events`: number of limits reported by PointT.

`rolling_60_minutes` contains the same operational view for the current and
previous 59 clock-minute buckets. It includes exact outer HTTP status counts,
request types (`bulk`, `single`, `fallback`, or `write`), retry and fallback
counts, and latency statistics. Failed requests are counted as requests but do
not distort the successful-response-time average or percentile.

`recent_requests` contains the most recent individual attempts. A retry is a
separate attempt, as is a single-resource fallback after a bulk failure. One
bulk call remains one HTTP attempt regardless of the number of resources in
it; `bulk_size` and the item-result counters show that logical work separately.
This distinction is useful when PointT returns HTTP 200 for the bulk envelope
but an inner gateway reports a 5xx status.

### Optional diagnostic entities

Three integration-wide diagnostic sensors are created disabled by default:

- **PointT API requests total**;
- **PointT API requests – last hour**;
- **PointT API response time – last hour**.

Enable them under **Settings → Devices & services → Entities** when continuous
monitoring is useful. The response-time sensor is the average of successful
attempts; its attributes include the sample count, approximate p95, maximum,
and latest attempt duration. The request sensors expose bounded aggregate
attributes for success, failures, request types, HTTP statuses, retries,
fallbacks, and rate limits.

The entities belong to the integration entry rather than to one heating
device, because one account may contain multiple gateways. Enabling them does
not change polling and does not create any additional PointT request.

### Counters per capability

Every entry under `gateways → capabilities` contains a `calls` section, for
example:

```json
{
  "name": "Outdoor temperature",
  "calls": {
    "attempts_total": 120,
    "successful": 119,
    "failed": 1,
    "success_rate_percent": 99.2,
    "results": {
      "success": 119,
      "timeout": 1
    },
    "last_result": "success"
  }
}
```

A call in this section is one attempt to read that capability within a batch.
One HTTP request can contain up to 30 capability reads, so the total number of
real cloud requests is reported separately under `request_metrics`.

Possible outcomes include `success`, `not_found`, `forbidden`, `timeout`,
`rate_limited`, `service_unavailable`, `authentication_error`, and
`request_failed`. Intentionally paused or not-yet-due capabilities are not
counted as failed reads.

The counters are derived from normal polling. Opening diagnostics does not
make another cloud request. All counters restart at zero after Home Assistant
restarts.

`energy_counter_resets_detected` increases when an individual non-negative
PointT energy counter becomes smaller than its previously confirmed value. It
contains neither the old nor the new measurement and exists only to make
resets after firmware updates, device replacement, or manual resets visible.

`supported_without_value_count` identifies capabilities that returned a valid,
supported scalar schema but no current value. Each affected capability also
contains `supported_without_value: true`. This distinction is useful for
optional and hybrid equipment: it is different from an unsupported resource
or a failed request.

`unknown_enum_values_detected` counts distinct enum values that appeared only
after an entity had been created and were not part of its declared options.
The entity reports `unknown` instead of passing an invalid state to Home
Assistant. The actual manufacturer value is deliberately excluded from logs
and diagnostics.

## Excluded information

- access or refresh tokens, OAuth codes, and redirect addresses;
- token fingerprints, their key, account names and e-mail addresses;
- gateway IDs, config-entry IDs, serial numbers, and UUIDs;
- IP addresses, MAC addresses, SSIDs, and location data;
- firmware identifiers and complete model names;
- user-defined heating-circuit, hot-water, and schedule names;
- current temperatures, setpoints, operating modes, and energy consumption;
- complete request or response bodies.
