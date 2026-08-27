# Diagnostics and request metrics

Home Assistant can generate a diagnostics report for Bosch/Buderus Heating.
It is intended for troubleshooting and support and does not make additional
cloud requests.

Diagnostics remain downloadable while the first integration setup is still in
`setup_retry`. Before runtime initialization they contain only redacted config
metadata and no gateway data. Once the runtime exists, they also include
request metrics and any partially initialized gateway coordinators.

## Download diagnostics

1. Open **Settings → Devices & services**.
2. Open **Bosch/Buderus Heating**.
3. Open the integration's three-dot menu.
4. Select **Download diagnostics**.

Always review the file before publishing it. Automatic redaction is an
additional layer of protection, not a replacement for reviewing a file before
attaching it to a public issue.

## Included information

- selected brand and number of configured gateways;
- anonymized device class such as `k40`, `k40rf`, `mx300`, or `mx400`;
- normalized resource paths without specific heating-circuit, hot-water, or
  heat-source identifiers;
- resource type, unit, polling group, maturity, default activation, and
  writeability;
- counts of allowed options, references, and structured subvalues;
- availability, freshness, error category, and consecutive failure count;
- whether a supported scalar resource currently provides no value;
- bounded counts of undeclared enum values observed after entity creation;
- counts of active negative pauses, rate-limit backoff, and circuit-breaker
  state;
- aggregated request and polling metrics.
- supported fault-resource templates, active fault and notification counts,
  severity counts, known active codes, last successful fault read, and parser
  status.

The report explicitly excludes current measurements, settings, and energy
values. It also excludes raw notification payloads and installation-specific
component IDs. Error codes remain visible because they are required to match a
diagnostic report with the appliance display.

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
- latest, average, and maximum request duration;
- coordinator poll count, failures, and duration;
- detected decreases in cumulative energy counters.

URLs, resource paths, gateway IDs, payloads, and response values are not stored
for these metrics.

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
- gateway IDs, config-entry IDs, serial numbers, and UUIDs;
- IP addresses, MAC addresses, SSIDs, and location data;
- firmware identifiers and complete model names;
- user-defined heating-circuit, hot-water, and schedule names;
- current temperatures, setpoints, operating modes, and energy consumption;
- complete request or response bodies.
