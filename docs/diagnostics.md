# Diagnostics and request metrics

Home Assistant can generate a diagnostics report for Bosch/Buderus Home Comfort.
It is intended for troubleshooting and support and does not make additional
cloud requests.

Schema 47 (0.9.1) adds `gateways[].heat_generator` and the same block in
`views_without_cloud[]`: the heat generator modules of the module list with
their product names and hardware types, never serial numbers (see
[heat generator modules](#heat-generator-modules)).

Schema 46 (0.9.0) adds fields, again without values, names or identifiers:

- `starts` in each entry under `connections[]` and `sources[]`: why the
  access or the source started since the integration started. `total`
  counts the starts, `recent` lists the last 10, each with `at` (the UTC
  time of the start as an ISO 8601 timestamp, to the second; unlike schema
  45, this schema contains such times), `reason` and `failure`. `reason`
  is a fixed code: `start` (the first start), `configuration_changed` (the
  options or the stored access changed it, or it was switched off and on
  again), `after_failure` (the previous attempt failed; `failure` is the
  error class only, for example `SourceFailure`), `source_ended` (a source
  ended on its own),
  `access_restarted` (its access started again, and the source with it),
  `requested` (a restart you confirmed in a repair) or `other`. `failure` is
  `null` for every other reason;
- `gateways[].faults.state_held`: `true` while the fault entities show the
  last confirmed fault state although the latest reads got no answer (see
  [validity of readings](reading-validity.md#display-values-of-several-readings));
- on a room controller: `point_shapes` in `rrc_programs.schedules`, the
  forms of the switch points by key names and value types (see
  [RRC program selection](#rrc-program-selection)), the reason
  `optional_key_type`, `confirmed_on` in `rrc_profile` (see
  [RRC room controller profile](#rrc-room-controller-profile)), the last
  reason without boost limits and its age in `contracts.absence`, and in the
  energy history the reasons `no_records`, `no_completed_days` and
  `all_series_empty` with the count `records_seen` (see
  [RRC room controller functions](#rrc-room-controller-functions)).

Schema 45 (0.9.0-alpha.6) adds sections and fields, again without values,
names, times or identifiers:

- `gateways[].firmware_updates`: the reads of the firmware update list (see
  below);
- `gateways[].icom`: only for a gateway with the device type `icom`, the
  load profile, the component types of `/system/info`, the bus and the
  holiday rules (see [iCom gateways](#icom-gateways));
- `gateways[].switch_program_writes`: the writes of the weekly heating and
  hot water programs. `heating` and `dhw` count `writes_sent`,
  `writes_confirmed`, `writes_unconfirmed`, `writes_rejected` and
  `late_confirmed`; `read_back_delays` is the read-back schedule of a write
  and `last_read_back_seconds` the duration of the last confirmed read-back.
  `programs` has per program path `editable`, `edit_issue` (a fixed token
  such as `not_writable`, `unavailable`, `not_used`, `not_level_program`,
  `program_mode_unknown` (the circuit reports its program type, but it has
  no current reading), `level_not_listed` or `no_current_reading`, otherwise
  `null`) and `points_per_day`, the number of switch points of each weekday;
- on a room controller: the reasons why a program stays read-only
  (`rrc_contract.schedule_issues`, `rrc_programs.schedules`, see
  [RRC program selection](#rrc-program-selection)), the confirmation per
  function group (`partly_confirmed` with `confirmed_functions`, see
  [RRC room controller functions](#rrc-room-controller-functions)), why the
  boost settings have no limits (`boost_temperature_limits_issue`,
  `boost_duration_limits_issue`), `room_controller_absence` in the holiday
  block, and `overrides_active` counts only zones in the schedule mode;
- `start_counter.structure` describes the form of a start counter that
  reads as `invalid_structure` (see [Start counters](#start-counters)).

The p95 of the request metrics never exceeds the measured maximum, and the
stored discovery inventory keeps no values of private or identifier
resources.

Schema 44 adds `failures` to each entry under `connections[]` and `sources[]`.
It counts the failed attempts to connect an access or to start and run a
source since the integration started (after a restart or reload it starts
again at zero), with fixed codes only and never an error message, value or
identifier:

- `failures`: all failed attempts;
- `consecutive_failures`: failed attempts in a row since the last successful
  start (0 while it works);
- `last_failure`: the error class of the last failed attempt, for example
  `SourceFailure` for a failure the source reported itself, or the class of
  an unexpected error such as `KeyError`; `null` before the first failure;
- `last_failure_stage`: `connecting` or `running` for an access, `starting`
  or `running` for a source;
- `last_failure_phase`: the phase it went into, `unavailable`,
  `auth_required` or `unsupported`;
- `retry_seconds`: the pause before the next attempt while it is
  `unavailable`, otherwise `null`. Failed attempts in a row wait longer each
  time: 1, 2, 4, 8, 16 and then 30 minutes, at least as long as a pause the
  cloud asked for. A successful start resets the pause to one minute.

The first unexpected error of a series is logged once as a warning with its
error class; its details and all further failures are logged at debug level,
and the next successful start is logged as info.

Schema 43 adds `connection_reuse` to each local access under
`connections[].local[]`. Local reads keep an answered connection to the
gateway open and use it again within 4 seconds, so a new TLS handshake is
only needed after a pause, a failure or an address change. The certificate
pin is checked on every new connection before anything is sent, and a kept
connection is only used again with the same pin. The section counts since
the local access started (after a restart or reload it starts again at zero)
and contains only counts, durations and fixed codes:

- `mode`: `keep_alive`, or `new_connection_per_read` while the automatic
  fallback is active;
- `measured_seconds`: how long the counts have run;
- `tls_handshakes` and `tls_handshakes_per_hour`: certificate inspections
  plus new connections for requests (`null` during the first minute);
- `new_connections` and `reused_connections`: requests that opened a
  connection and requests that used a kept one;
- `closed_by_gateway`: answers with which the gateway announced that it
  closes the connection;
- `reuse_failures`: failures on kept connections by kind, `closed` (closed or
  reset before an answer), `timeout` or `invalid_response`. A read whose kept
  connection was closed before an answer is not paused or counted as a
  connection failure; it is reported under `deferred_reasons` as
  `reused_connection_closed` and read again with a new connection;
- `fallback_active`, `fallback_reason` (the kind of failure that started the
  last fallback), `fallback_remaining_seconds` and `fallbacks`: three
  failures on kept connections within 15 minutes switch to a new connection
  per read, the behaviour before keep-alive, for 6 hours, a repeated
  fallback for 12 and then 24 hours. The first fallback is logged once as a
  warning;
- `overlapping_reads`: reads that started while another read was still
  running, to judge whether two parallel reads are still needed;
- `response_times_by_connection`: for answered reads over a `new` and a
  `reused` connection the number of samples and the median, 95th percentile
  and maximum in milliseconds of the last 256 reads. These are response times
  as defined below.

Each local access under `connections[].local[]` also reports `start_spacing`,
the spacing between the starts of two local reads (see
[local polling](local-polling.md)): `base_ms` (200), `current_ms` (the
spacing in effect now), `median_response_ms` and `response_samples` (the median
**gateway time** of the last 32 answered reads and their number, which the
latency brake follows; `null` before the first answer), `brake_active`,
`brake_reason` (`timeout` or `server_error`) and `brake_remaining_seconds`. It
is `null` while the local access is not running.

The local access uses two kinds of times, always in milliseconds:

- the **response time** of a read (`response_times`,
  `response_times_by_connection`) runs from the start of the read until it is
  complete: the body is read and decoded, or an error answer is closed. It
  includes setting up the connection but not the wait for the spacing between
  two requests and not saving a pause afterwards. A read that timed out counts
  with the time until the timeout;
- the **gateway time** (`start_spacing.median_response_ms`,
  `response_phases.gateway_wait`) runs from sending the request headers until
  the response headers arrive.

`response_phases` splits the answered reads of the last 256 into
`connection_setup` (from the start of the read until the request headers are
sent: address check, a certificate inspection when needed, a new TCP and TLS
connection), `gateway_wait` (the gateway time) and `body_and_decode` (reading
and decoding the body, or closing an error answer), each with the number of
samples and the median, 95th percentile and maximum. The three phases of a
read add up to its response time. Time in which Home Assistant was busy with
other work counts in the phase it falls into.

Schema 43 also reports how often readings are requested now, again as counts
without paths, values or identifiers:

- each local access under `connections[].local[]` adds
  `signals_read_hourly_unused`: local signals read only hourly because every
  entity that shows them is disabled, and
  `signals_read_hourly_without_entity`: local signals read only hourly
  because no entity shows them at all, such as identifiers (see
  [local polling](local-polling.md));
- each gateway `runtime` adds `early_read_paths` (paths read a few minutes
  before their group's deadline to fill batches that are sent anyway),
  `local_served_paths` (paths whose entities show the local value, so their
  cloud copy is read at the slower rate), `local_served_interval_seconds`
  (that rate, 900), `switch_reads_pending` (cloud copies still to be read at
  once after their entity switched back to the cloud value or became enabled
  again), `controls_slowed_unused` (controls read with the slow values
  because all their entities are disabled), `explicit_refreshes_full` and
  `explicit_refreshes_control_only` (explicit refreshes, such as the "Update
  entity" action, that read every group, and those within
  `explicit_refresh_gap_seconds` of the last full one that read only the
  controls); see
  [request behavior](request-behavior.md) and
  [source selection](source-selection.md#cloud-reads-while-local-values-are-shown);
- `inventory.polling_groups` and each capability's `poll_group` give the
  group a path is actually polled in, so a control read with the slow values
  counts as `slow`.

In `observation_pairs`, schema 43 also counts any other pair that differs
after more than 60 seconds as `changed_between_reads`, not only counters that
counted on: such a value most likely changed between the two reads, which
happens often now that the cloud copy of a locally shown value is read every
15 minutes. `differ` and `differing_measurements` therefore report readings
that disagree within one poll period, and counters that went backwards. The
`write_checks` keep their meaning: a local read-back that does not confirm a
written value counts as `differ` however long after the write it arrived.

Schema 42 extends existing sections, again without values, addresses or
identifiers:

- each local access under `connections[].local[]` adds `condition_changes`
  (the last 20 changes of its condition with UTC time and a fixed reason),
  `failures_by_class` (`timeout`, `tls`, `not_found`, `http_4xx`, `http_5xx`,
  `connection` or `other`), `response_times` (number of samples and the
  average, 95th percentile and maximum in milliseconds of the last 256 reads)
  and `deferred_reasons` (reads not sent, by reason, for example `busy` or
  `paused`). A path that was read once but has no expiry of its own, such as
  the signal list, shows the freshness `read`;
- `observation_pairs` adds the result `changed_between_reads`: a counter
  whose later reading is higher in at least one part and lower in none, read
  more than 60 seconds apart, most likely counted on between the two reads.
  `differing_measurements` names the catalog measurements of pairs that
  really differ, and `maximum_receive_gap_seconds_by_class` reports the
  largest receive gap per local polling class, so rarely read values no
  longer hide the gaps of live readings;
- each gateway `runtime` adds `p95_poll_duration_ms` and
  `poll_duration_samples` (the 95th percentile of the last 200 cloud cycles),
  and `capability_attempts` adds `failed` and `paths_failing` (paths whose
  latest attempt failed);
- `startup_data.paths` gives the same masked path an `index` when it occurs
  more than once, for example the product names of two devices, and the
  fault paths are masked like all other paths;
- `local_plant_view` adds `listed_at_most` and `omitted_entries`, the number
  of entries its lists leave out, next to `truncated`.

Schema 41 adds `views_without_cloud`: one entry per plant that is shown
without a running cloud source. `kind` is `dormant` (a switched-off cloud
access shown from its stored inventory), `held` (the cloud access was switched
off while running), `local_plant` (shown from the local readings alone),
`target_view` (a system whose cloud access was removed) or `kept_for_cloud`
(kept for local readings while a cloud that is expected back needs a new
sign-in or does not answer). `local_list_decides` tells whether the local
error list alone decides the faults, `local_list_current` whether it is
current. `entities` and `entities_available` count the enabled entities of the
plant and those with a state, `active_faults` the active faults. The section
has no identifiers, paths or values. Every gateway's `faults` section also
counts the room devices' own error lists under `device_errors` (`lists`,
`active`, `inactive`, `other`), without device numbers or error types.

Schema 40 adds `local_plant_view` per installation that has a local access
next to a running cloud access. It is a preview of operation without the
cloud: it works out which entities the current local readings alone would
give and compares them with the cloud's entities and the entity registry. The
section is only calculated for the report; it creates no entities, sends no
requests and changes no availability.

- `local_readings`, `cloud_form_resources` (readings of the pair catalog or of
  a path this cloud offers that give the same entities, with the same IDs, as
  the cloud), `local_sensors`
  (all other readable readings, by the rules of the
  [local-only readings](entities.md#readings-only-the-local-access-provides);
  `local_sensors_reviewed` and
  `local_sensors_enabled` count those with a reviewed meaning and those
  enabled by default) and `templates_compatible` (cloud readings that match
  type, unit and allowed values of the local reading);
- `skipped`: counts per reason a local reading gives no entity, for example
  `private` (identifiers, addresses, names, network or update data), `text`
  (free text), `multipart` or `unsupported_shape`;
- `counts`, `matching`, `only_local` (entities only the local access could
  give), `needs_cloud` (entities that need the cloud, such as controls, with
  `needs_cloud_domains`), `local_only_with_cloud_path` (readings of paths the
  cloud offers that still become local-only sensors, because the cloud form
  gives no entity) and `mismatches` (differences in unit, device
  class, state class or allowed values; for entities only the registry knows,
  the unit is not compared when the device class matches, because Home
  Assistant may show it converted, for example in °F). Entities are listed as platform and
  unique ID without the gateway ID, for example
  `sensor:heatingCircuits:hc1:currentRoomSetpoint`; unrecognized installation
  IDs are replaced by placeholders. Lists hold at most 150 entries
  (`truncated`), the counts are complete. Optional per-source entities are
  only counted (`per_source_entities`). If the preview cannot be calculated,
  the installation shows only `error: unavailable`; the rest of the report is
  unaffected.

Schema 39 adds `discovery_recovery` to every gateway. It shows whether parts
that were discovered before were missing after the start-up discovery and how
the search for them went:

- `state`: `none` (nothing known before and nothing missing), `complete`
  (nothing missing), `pending`, `recovered` or `given_up`;
- `reference_resources`, `missing_at_start`, `missing` and `recovered`: counts;
- `missing_templates`: path templates without installation IDs, for example
  `/system` or `/heatingCircuits/{hc}`, at most 20;
- `missing_reasons`: counts per result category of the start-up discovery, for
  example `http_404`, `timeout`, `malformed` or `not_referenced` when a
  container answered without the reference;
- `attempts`, `attempts_planned`, `postponed` (rate limits, and searches that
  got no answer or ended with a 5xx failure of the whole cloud), `last_result`
  and `next_attempt_in_seconds`. A search in which a missing path answers
  HTTP 5xx got an answer and uses an attempt. After 24 postponements the
  series ends with `state` `given_up` and `last_result` `unanswered`; unlike
  a series that used up its attempts, the parts stay expected at the next
  start.

`startup_inventory` shows how the gateway started: `mode` is `live` or
`inventory` (the cloud did not answer during start-up and the saved resources
were used), with `reason` (for example `gateway_list_timeout`,
`gateway_not_listed` when the answered gateway list did not contain the
gateway, or `discovery_rate_limited`), `inventory_age_hours`, `discovery_attempts`,
`discovery_result` and `next_discovery_in_seconds` for the discovery that
follows the first working cycle.

`firmware_changes` shows firmware updates: `changes_while_running`,
`changed_since_last_start`, `last_change_age_seconds` and the discovery a change
triggers (`discovery_attempts`, `discovery_postponed`, `discovery_result`,
`next_discovery_in_seconds`). The gateway runtime adds
`changed_contracts_detected`, the number of known readings that changed their
value type (for example text instead of a number) since discovery. See [compatibility](compatibility.md).

`firmware_updates` shows the reads of the firmware update list (see
[entities](entities.md#firmware-update)) once the update platform started
them: `active`, `result` and `results` (counts per class: `ok`,
`endpoint_refused` for HTTP 403, 404 or 405, `not_listed`, `schema_issue`,
`rate_limited`, `no_answer`, `unexpected_status`, `sign_in_required`,
`error`), `attempts`, `held_rounds` (rounds the cloud state held back without
a request), `last_attempt_age_seconds`, `last_answer_age_seconds`,
`next_read_in_seconds`, `shown`, `restored` (the shown answer is the one kept
from before the last restart), `installed_version`, `update_version`,
`update_offered`, `update_state`, the number of `release_notes_links` and
`auto_update_enabled`/`auto_update_changeable`. It never contains the gateway
ID or a link; a version that does not look like one shows as `other`. The
request metrics count these reads as `firmware_updates`.

The discovered resources are kept per gateway in
`.storage/bosch_buderus_heating.discovery_inventory.<entry>`, only on the Home
Assistant host and deleted together with the integration entry. The file is
not part of diagnostics. Since 0.9.0-alpha.6 it keeps no values of private or
identifier resources (for example the gateway serial number, the UUID or the
module serial numbers of `/system/info`), only their description; an older
file loses those values the next time it is saved. After a start from this
file, a known device keeps the serial number, model and versions its device
page shows until the values are read again.

Schema 38 adds `update_load` with process-wide counts and whole milliseconds
since Home Assistant started: garbage collections per generation, their total
and longest duration, collections of at least 100 ms in total and during entity
update passes, and the number, average and longest duration of update passes,
how many took at least 400 ms (Home Assistant's threshold for a slow state
update) and the largest number of entities in one pass. It helps to tell
whether a slow state update came from Python's garbage collector or from the
integration. It contains no paths, values or identifiers.

Schema 37 adds `observation_pairs[].local_values.write_checks` (per catalog
measurement: local read-backs after a confirmed cloud write that agree, differ,
are incomparable or arrived too late) and the cloud reason `older_than_write`.
See [source selection](source-selection.md#writes).

Schema 36 adds `observation_pairs[].counters` (per counter measurement: shown
source, source switches and prevented switches) and `counter_evidence` (equal
or different, largest relative difference in percent, finest reported step per
source and receive-time gap), without absolute counts. See
[source selection](source-selection.md#counters).

Schema 35 adds `cloud_reason_measurements`: for each reason in
`cloud_reasons`, the names of the affected measurements from the pair catalog
(for example `room_setpoint_current`), without values, paths or identifiers.

Schema 34 adds `cloud_reasons` to `observation_pairs[].local_values`: how many
eligible entity sources use the cloud because the option is off
(`option_off`), the cloud source is stopped (`source_stopped`), the local
reading is not current (`local_not_current`), invalid (`local_invalid`), of
another type, unit or shape, or not convertible. A list with an entry that is
not declared, or a text the cloud does not declare, counts as
`unknown_entry`; a local shape the entity cannot take
counts as `shape_differs`, never as `local_invalid`. See [source selection](source-selection.md).

Schema 33 adds the [validity of readings](reading-validity.md): each gateway
`runtime` reports `validity_limits_seconds` per polling group and
`resources_expired`, the number of readings currently past their limit.
`observation_pairs` adds `incomparable_units`, the unit combinations of pairs
that differ only by unit (for example `-|kWh`), without values or paths.

Schema 32 adds `observation_pairs` per installation, comparing verified cloud
and local reading pairs without values, paths or identifiers. Its `local_values`
part shows whether local values supply existing entities and how many eligible
entity sources use the local or the cloud value. See
[observation pairs](observation-pairs.md).

Schema 31 adds `authentication` for investigating repeated sign-in requests. It
was introduced as schema 28 in 0.8.4-beta.1. Managed entries report it per
cloud connection; legacy entries and entries without a running setup report a
list of their saved cloud accesses. It is built from memory and local storage, without a cloud request,
so it does not depend on a working sign-in. It contains:

- `current`: remaining access-token lifetime in seconds, whether a refresh
  token exists, its relation to the recorded history, the granted scope names
  and whether renewal stopped after a rejection;
- a start after a rejection is recorded as `credentials_loaded` with reason
  `retry_after_rejection`;
- `events`: the last 60 sign-in events with UTC time and age. Starts record
  whether the stored refresh token is the newest known one. Renewals record
  their trigger (`expiry` or `access_token_rejected`), duration and outcome,
  and on success the reported access and refresh lifetimes, whether a new
  refresh token was issued and whether it was saved;
- for a rejected renewal: HTTP status, OAuth error code, a sanitized
  description of at most 120 characters, body format and content type. The
  same fields appear for a renewal that was refused without a definite OAuth
  error (outcome `failed`, `error_type` `RefreshRefused`); such a refusal is
  retried later and does not ask for a new sign-in;
- `presented_refresh_token`: whether the rejected token was the newest known
  one, how long ago it was first seen, how often it had been presented before
  and whether the service had accepted it before;
- `issued_refresh_tokens` and `counters`: anonymous rank, source, age and use
  count of recently seen refresh tokens, plus totals per outcome.

Refresh tokens are recognized through an HMAC fingerprint whose random key
stays in Home Assistant's local `.storage` folder. Neither the key, the
fingerprints nor any token are exported. Long opaque strings and anything
resembling an e-mail address are removed from the service description. The
original cloud access keeps `.storage/bosch_buderus_heating.auth_history.<entry>`
from 0.8.4-beta.1, so its history continues; further accesses use
`auth_history.<entry>.<connection>`. Removing an access or the entry deletes its
history. It survives restarts, so the
history starts with the first version that records it.

A rejected renewal is also written to the Home Assistant log as a warning that
starts with `SingleKey ID rejected the token refresh`. A start with stored
sign-in data older than the newest received token and a refreshed token that
could not be saved are logged as warnings as well.

Schema 30 adds `cloud_pause` to each managed cloud connection: whether a
server-requested pause is active, its remaining seconds, whether it was
restored after a restart and whether pause storage is available. See
[cloud pauses](cloud-pauses.md). Each gateway `runtime` adds
`maximum_poll_duration_ms`, `poll_interval_seconds`,
`polls_longer_than_interval` and `cycles_over_budget` (cycles that stopped
starting requests after 50 seconds); `request_metrics.slot_wait` reports how long
requests waited for one of the three shared request slots. See
[capacity with several systems](request-behavior.md#capacity-with-several-systems).

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
redacted. See [older holiday formats](legacy-holidays.md). On a room
controller the block also has `room_controller_absence`: its calendar, away
mode and boost are reported under `rrc_features.contracts.absence`, not
here.

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

## Managed connections

Schema 28 adds diagnostics for the prepared version 4 owner. Connections,
targets and source bindings use export-local numbers, including disabled or
removed access with retained targets. Request metrics and additional-cloud
diagnostics stay separate per cloud connection. Gateway and wallbox reports
reference their connection and target numbers. The export includes no stable
identifiers, credentials, endpoint addresses or user-defined names and makes no
additional API requests. Existing reviewed technical metadata remains governed
by the same allowlist.

Normal version 4 setup uses these diagnostics after migration. Schema 29 adds
local source conditions, fixed failure codes, request counts, discovery progress,
remaining pauses and per-resource age, target interval, actual interval and
availability. Device and signal paths use catalog templates to hide dynamic IDs.
It contains no local values, credentials or system module identities and performs
no extra requests. See [managed local access](local-runtime.md).


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
When a part reads as `invalid_structure`, `structure` describes the form of
the reading without values: `reason` (`item_not_object`), `value` and
`values` as types, list lengths and key names (for example
`list[2]<dict{ch:int,total:int}|int>`; a key that does not look like a plain
name shows as `?`), and `values_count`. It helps to learn a counter form
that the integration does not read yet. Since 0.9.0 a list of up to eight
objects in `value` is read as the parts of the counter, so this form no
longer shows as `invalid_structure`.

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
| `implausible_bounds` | A bound lies outside −30 to 100 °C or 0 to 2,880 minutes, after unit conversion. |

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

## iCom gateways

`gateways[].icom` appears only for a gateway that reports the device type
`icom`; other gateways keep their report. It contains sizes, counts, fixed
tokens and component type numbers only:

- `load_profile`: the current batch size (`bulk_size`, normally 30, at least
  10 after timeouts), `good_cycles_towards_next_step`, `bulk_size_reductions`,
  `circuit_openings_in_outage`, the breaker pauses in minutes
  (`circuit_pause_minutes`: 5, 10, 20, 30) and the `read_back_delays` of a
  write (1, 2, 3 and 4 s), see [request behavior](request-behavior.md#icom-gateways);
- `system_info_read` and `system_components`: the component type (`id`) and
  token (`token`) of each entry of `/system/info`. Serial numbers, product
  names and versions are never included;
- `bus`: `EMS2_0`, `EMS1_0`, `CAN`, `MODBUS`, `No_Bus`, `other` or `null`.
  With `EMS1_0`, `CAN` or `No_Bus`, `bus_hint` is
  `bus_possibly_unsupported_reported_values_only`: the bus is possibly not
  supported by the cloud access (expected, untested), and the integration
  shows only the values the cloud reports. Otherwise `bus_hint` is `null`;
- `holiday_rules`: whether the HMI800 HP Light rule applies and which heating
  circuits token 16 hides, with `status: expected_untested`
  (see [older holiday formats](legacy-holidays.md)).

## Heat generator modules

`gateways[].heat_generator` (schema 47) appears for every gateway except
room controllers; a system shown from its local readings
alone has the same block in `views_without_cloud[]`. It belongs to the
[heat generator sensor](entities.md#heat-generator-models) and makes no
request:

- `table_version`: the version of the model table;
- `cloud_modules` and `local_modules`: how many modules with a product name
  the current answer of the cloud (`/system/info`) and of the local access
  (`/system/basicInfo`) lists, `null` without a current answer;
- `source`: the access whose list is used (`cloud`, `local` or `null`);
  `known`: whether the table describes the product of every module;
- `conflict`: `true` while the two answers list different modules; then
  `other_source_modules` lists the modules of the other answer;
- `modules`: `role`, `product_name` and `hardware_type` of each module;
- `unmapped_modules`: the modules without a table entry for their product.
  Reporting them lets the model table grow.

Only the product name and the hardware type of a module are read. Serial
numbers, tokens, versions and type numbers are never included; a name field
that holds a serial number of the same module, or only digits, is left out.
Because product names are included, the privacy block then sets
`contains_raw_resource_values: true` with
`raw_resource_values_scope: allowlisted_technical_metadata`. The anonymized
inventory drops this block.

## RRC program selection

`gateways[].rrc_programs` explains for the zones of an RRC room controller,
such as the TC100.2, why the heating schedule selection is offered or not.
It was added in 0.9.0-alpha.5 within schema 44. The section contains counts
and fixed codes only, never program names or current values:

- `program_selections`: zones with a selected program (`clockProgram`);
- `program_selections_offered`: how many of them offer the selection;
- `program_list`: the state of the controller's program list
  (`/programs/list`): `current` when the last read is fresh, `not_current`
  when it is known but its last read failed or is outdated, and `missing`
  when it has not been found yet;
- `program_list_issue`: why the program list supplies no programs (`type`,
  `value` or `ids`, see below), or `null` when it is valid or missing;
- `program_count`: the number of programs in the list, 0 if it is unusable;
- `program_names_readable`: how many of them have a readable name;
- `zones`: one entry per zone with its `path`, `selection` and `used`. `used`
  is the flag the controller sets for the zone's program; it is `false` in
  manual mode, and the selection is offered all the same.

`selection` is `offered` or the first reason that applies:

| Code | Meaning |
| --- | --- |
| `reading_invalid` | The zone reports no usable program number. |
| `selection_not_writable` | The controller does not allow changing the program. |
| `program_list_missing` | The program list has not been read yet. |
| `program_list_invalid_type` | The program list has an unexpected resource type. |
| `program_list_invalid_value` | The program list has no list value, or more than 128 entries. |
| `program_list_invalid_ids` | An entry has no valid, unique program number from 0 to 9999. |
| `program_list_empty` | The program list is valid but contains no programs. |

`schedules` counts the week and day programs (`week`, `day`): `count`,
`readable` (the integration understands the form), `editable`, `issues`,
how often each reason below occurs, and `point_shapes`, how many programs
have switch points of each form. A form names the keys of a point and the
types of their values, never a value, for example
`dict{Th:float,d:str,name:int,t:int}`; a key that does not look like a plain
name shows as `?`, more than four forms in one program end with `more`, and
at most eight forms are listed. Each program's capability entry adds
`rrc_contract.schedule_issues`, its reasons in a fixed order. A program
without a reason can be edited; the form is never changed on a guess.

| Code | Meaning |
| --- | --- |
| `type_unexpected` | The resource type is not `weekProgram` (a day may also be `dayProgram`). |
| `not_available` | The controller marks the program as not used or not available. |
| `has_references`, `has_values`, `no_value`, `not_a_list` | The program has no list of switch points. |
| `too_many_points` | More than 512 switch points. |
| `point_not_object` | A switch point is not an object. |
| `day_missing_d` | A switch point of a week program has no day (`d`); a day program may leave it out. |
| `day_invalid` | A switch point names no known day. |
| `day_mismatch` | A point of a day program names another day. |
| `minute_invalid` | The time (`t`) is no whole minute of the day. |
| `temperature_invalid` | The temperature (`Th`) is no number from 0 to 30 °C. |
| `not_writable` | Read-only: the controller does not allow changing the program. |
| `unknown_fields` | Read-only: the program carries fields the integration does not know. |
| `extra_key` | Read-only: a switch point has a key besides `d`, `t`, `Th` and `name`. |
| `optional_key_type` | Read-only: a switch point has `name`, but not as a whole number. |
| `unsorted` | Read-only: the points of a day are not in ascending time order. |
| `off_grid_minute` | Read-only: a time is not on the 5-minute grid. |
| `temperature_range` | Read-only: a temperature is outside 5 to 30 °C. |
| `temperature_grid` | Read-only: a temperature is not on the 0.5 °C grid. |

The codes up to `temperature_invalid` mean the form is not read. The check
stops at the first of them, so only that one is named, after the read-only
reasons found until then. A read-only code keeps the program readable but
not editable; all of them are named.

If zones select a program but the program list has not been found, the
integration searches for it again on its own, at most seven times within a
day (see `discovery_recovery`). The names of the programs are shown by the
`program_names` and `program_name` attributes of the selection; diagnostics
only count them.

## RRC room controller profile

`gateways[].rrc_profile` describes a room controller of the RRC 2.0 platform.
It contains counts, fixed codes and the firmware version only, never names,
device identifiers or radio keys. On other gateways it contains only
`profile: null` and the two access fields.

- `profile`: `rrc2`, and `evidence`: what showed it (`device_type`,
  `product_type`, `zones`);
- `confirmation`: the confirmation of the room controller functions as a
  whole, the same as `rrc_features.confirmation` (`partly_confirmed` while
  some functions are confirmed on a real controller), with `confirmed_on`
  (`tc100_2`, otherwise `null`); `physical_device_verified` stays `false`,
  because the diagnostics cannot tell which controller model this is. A
  confirmation on a TC100.2 does not confirm a CT200;
- `local_access`: `not_available`, as these controllers have none;
- `product_type`: `RRC 2.0`, `other` or `null`; `firmware`: the controller's
  firmware version;
- `zone_count`, `program_count`, `device_count`, `device_types` (count per
  reported type), `devices_with_low_battery`, `device_fields_missing` (how
  many listed devices sent no usable type, zone, battery or signal) and
  `device_firmware_read`;
- `device_list`: `current`, `not_current` or `missing`, and
  `device_list_issue` as for the program list;
- `forbidden_paths` and `access_reason`. A controller refuses some advertised
  paths as a matter of course (a TC100.2 about 35); that is
  `some_paths_forbidden`. `scope_or_permission_denied` means that the
  request as a whole, the zones or every read was refused: the sign-in may
  lack the rights for this controller.

`gateways[].discovery` also reports `rrc_evidence`, `forbidden_paths`,
`access_reason` and `paths_not_read`, the number of scheduled paths that were
deliberately not read: `never_read_private` (keys and personal data),
`outside_rrc_device_scope` (device parts a room controller does not need)
and `rrc_5xx_assumed_missing` (probes answered with HTTP 500). The device
profile is `rrc2` for these controllers.

## RRC room controller functions

`gateways[].rrc_features` appears only for a room controller of the RRC 2.0
platform; other gateways keep their report. It contains counts, fixed codes
and metadata flags only, never values, names, dates, zone or device numbers.
It was added in 0.9.0-alpha.5 within schema 44. The functions are described in
[room controllers](room-controllers.md).

- `confirmation`: `partly_confirmed` while some functions are confirmed on a
  real controller. Each entry of `contracts` has its own `confirmation`:
  `partly_confirmed` with `confirmed_functions` (fixed tokens such as
  `temporary_setpoint`, `away_mode` or `operation_mode`) and
  `confirmed_on` (`tc100_2`) for a group with confirmed functions,
  otherwise `unconfirmed`. A function not named there is expected,
  untested;
- `read_back_delays`: the read-back after a change, `[1, 2, 4, 8]` seconds;
- `awaiting_late_confirmation`: changes sent but not confirmed yet that a
  later regular reading may still confirm;
- `energy_history`: the state of the energy history import, for example
  `waiting`, `mode` (`date` or `entry` requests), `reason` (for example
  `gas_unit_unconfirmed`, `history_change_outside_window`,
  `heat_source_type_unavailable`, while days with the older keys wait for
  the plant type, or `legacy_keys_ambiguous`, when the plant type could not
  be read for 24 hours and the older keys were left out; after a cycle
  without an error that still has nothing to import: `no_records`, when no
  answer held a valid daily record, `no_completed_days`, when the records
  were only of the running day or outside the import window, or
  `all_series_empty`, when every value of the imported days was zero),
  `records_seen` (the valid daily records in the answers, a record read
  again counts again; a count, no value),
  `heat_source_type_unreadable_seconds` (how long the plant type has been
  answered with a server error, a duration, no date) and
  `heat_source_type_failed_cycles`, `cycles`,
  `last_cycle_requests`, `imported_days`, `days_behind`, the imported
  `statistics`, `gas_unit_confirmed` and `next_cycle_in_seconds`;
- `contracts`: one entry per function group, each with `writes`:
  `writes_sent`, `writes_confirmed`, `writes_unconfirmed`, `late_confirmed`,
  `writes_rejected`, `awaiting_late_confirmation` and `rejection_reasons`
  (fixed message keys). `core` counts the zone mode, manual setpoint and
  program changes.

| Entry | Counts |
| --- | --- |
| `zone` | `zones`, `override_paths`, `overrides_active` (only zones in the schedule mode, where a temporary setpoint acts), `next_setpoint_none`, `status_unknown`, `window_status_unknown`, `window_detection_paths` |
| `absence` | `events` (`lists`, `lists_readable`, `events_away`, `events_weekday`, `entries_skipped`, `write_issue`), `boost` (`writable`, `missing` or why it cannot be written), the boost limits and, for each, why there are none (`boost_temperature_limits_issue`, `boost_duration_limits_issue`: `missing`, `type`, `flags`, `unit`, `range`, `step` or `null`); the last such reason seen in a fresh reading since Home Assistant started, also after it went away, and how many seconds ago it was last seen (`boost_temperature_limits_last_issue`, `boost_temperature_limits_last_issue_age_s`, and the same for the duration; `missing` is not kept) |
| `dhw` | hot water circuits, offered options, extra hot water states, time types, programs and unknown levels |
| `circuit` | `circuits` and per setting `present`, `writable`, `unavailable`, `with_limits` (and `offered_modes` for the season mode) |
| `system` | no counts of its own, only `confirmation` and `writes` |
| `devices` | `device_settings_read`, `leaves`, `settings_not_offered`, `valve_status_shapes`, `valves_with_problem`, `unknown_valve_states`, `window_sensitivity_levels` |

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

`/heatSources/{hs}/activefailure` and `failurelist` are probed only for heat
sources found during discovery. A 404 or 403 counts as "not supported": that
path pauses for 24 hours (counted in the gateway `runtime` as
`negative_pauses_active`), and a 403 or 404 never clears a known fault.

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

A request-wide transport error is a timeout or network failure without any
answer, or an HTTP 5xx answer to a whole bulk call. An HTTP status answer to
one individual fallback read, for example 504 or 409, concerns only that path,
as in the regular poll: its `fallback_result` shows `http_504` or `http_409`,
and discovery goes on with the other paths. Only three individual fallback
reads in a row that answer HTTP 5xx, without any working answer in between,
end the discovery with `service_unavailable`; this bounds the extra reads when
the cloud itself fails. A bulk answer that contains resources ends such a
series as well.

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

`polls_total` in a gateway's `runtime` counts update cycles of that gateway's
coordinator. `requests_per_hour` counts every cloud HTTP request since start
(discovery, start-up data, writes, retries, fallbacks), projected to one hour,
and appears after 60 seconds. One poll can send several requests or none, so
the two do not match.

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

Three diagnostic sensors are created disabled by default. They expose the total
PointT request count, the request count over the last hour and the response time
over the last hour.

Enable them under **Settings → Devices & services → Entities** when continuous
monitoring is useful. The response-time sensor is the average of successful
attempts; its attributes include the sample count, approximate p95, maximum,
and latest attempt duration. The p95 is the upper bound of its histogram bucket
(up to the 30-second timeout), never above the measured maximum. The request sensors expose bounded aggregate
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

`energy_counter_resets_detected` increases when an individual part of a
PointT energy collection becomes smaller than its previously confirmed value,
compared in kWh, so a change of the reported unit is no reset. It contains
neither the old nor the new measurement and exists only to make resets after
firmware updates, device replacement, or manual resets visible. The produced
heat (`outputProduced`) is a net total that falls while the heat pump
defrosts; its decreases are counted apart in
`produced_heat_decreases_detected`.

`rate_limit_events` counts the rate limits the cloud reported to this gateway
since the start, `rate_limit_events_last_24h` those within the last 24 hours,
which decide the repair. A request that the running pause of the cloud access
held back before it was sent is no event.

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
- firmware identifiers and complete model names, except the product names
  of the heat generator modules (see [heat generator modules](#heat-generator-modules));
- user-defined heating-circuit, hot-water, and schedule names;
- current temperatures, setpoints, operating modes, and energy consumption;
- complete request or response bodies.
