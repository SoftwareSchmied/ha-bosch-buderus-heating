# Changelog

All notable changes will be documented here. The project follows Semantic
Versioning after its first tagged preview.

## [Unreleased]

## [0.7.0-beta.2] - 2026-09-04

This beta recognizes the second heating-circuit schedule type used by the
official MyBuderus and HomeCom Easy apps. Installations reporting the PointT
value `absolute` now show a clear translated state instead of a raw or unknown
value.

### Fixed

- Added the app-confirmed `absolute` value to
  `/heatingCircuits/{hc}/switchProgramMode`.
- Displayed that value as **Freely Adjustable Temperatures** in English and
  **Frei einstellbare Temperaturen** in German.

### Behavior changes

- Schedule-type sensors now recognize both known modes, `level` and
  `absolute`, even when the gateway does not advertise a complete enum list.
- The sensor remains read-only; this release does not add schedule editing or
  additional PointT writes.

### Security and compatibility

- Existing entity unique IDs, device identifiers, history, automations, and
  dashboards remain unchanged.
- Polling intervals, cloud-request counts, credentials, and write permissions
  are unchanged.
- Unrecognized future enum values continue to use the existing safe fallback
  instead of being passed to Home Assistant as an invalid enum state.

### Validation

- 506 automated tests pass with 95.13% branch coverage, including the
  `absolute` PointT value, enum options, and the exact German and English app
  terminology.
- Ruff formatting and linting and strict Mypy validation pass.

### Upgrade notes and limitations

- Restart Home Assistant after updating so frontend state translations are
  reloaded.
- The schedule type is still informational. Full schedule editing requires a
  separately verified PointT schema and remains outside this beta.

## [0.7.0-beta.1] - 2026-09-03

This beta adds a transparent calculated dew point for heating circuits
that report both room temperature and relative humidity, without adding PointT
traffic. It is intended for comparison with the controller's internal dew-point
display before the feature is included in a stable release.

### Added

- A **Dew point (calculated)** temperature sensor for every heating circuit
  with compatible `roomtemperature` and `actualHumidity` capabilities.
- Source temperature, source humidity, calculation method, and Magnus
  coefficients as attributes of the calculated dew-point sensor.
- A `value_source` attribute on total electricity consumption to distinguish a
  direct PointT value from the existing complete compressor-plus-auxiliary-
  heater fallback.

### Behavior changes

- Purely derived entity names consistently use the suffix **(calculated)** in
  English and **(berechnet)** in German. The existing system-pressure status
  now follows this convention.
- The dew-point sensor becomes unavailable with either source resource and
  never substitutes a room setpoint for a measured room temperature.

### Security and compatibility

- The calculation runs locally from already polled values and creates no new
  cloud request or write.
- Existing device identifiers and entity unique IDs remain unchanged. The
  system-pressure status receives only a display-name clarification.
- Invalid, non-finite, sentinel, or implausible input values produce no
  calculated measurement.

### Validation

- 504 automated tests pass with 95.13% branch coverage.
- Ruff formatting and linting and strict Mypy validation pass.
- Automated tests cover the Magnus calculation, invalid inputs, exact
  same-circuit capability matching, availability, Home Assistant metadata,
  attributes, and direct-versus-calculated electricity provenance.

### Upgrade notes and limitations

- The calculated dew point is not the controller's private dew-point value or
  cooling-flow setpoint and does not include an unknown installer safety
  offset or minimum-flow-temperature limit.
- The entity is created only where PointT exposes both required measurements
  with the expected type and unit.

## [0.6.0] - 2026-08-30

Version 0.6.0 makes PointT cloud traffic and response behavior observable
without exposing installation data or increasing the request rate.

### Added

- Rolling request metrics for the current and previous 59 clock-minute
  buckets, including successes, failures, exact HTTP statuses, request types,
  retries, fallbacks, rate limits, and bulk-item outcomes.
- Successful-response-time statistics for the last hour: average,
  approximate p95, maximum, latest attempt, and sample count.
- A memory-only diagnostics log containing up to 250 individual HTTP attempts
  from the last 60 minutes. Each entry records a sequence number, age, method,
  request type, outcome, response time, retry state, and safe bulk summaries.
- Three integration-wide diagnostic sensors for total requests, requests in
  the last hour, and average response time in the last hour.

### Behavior changes

- Retries and bounded single-resource fallbacks are identified on the actual
  HTTP attempt instead of being represented only by aggregate counters.
- Successful PointT bulk envelopes keep their outer HTTP status separate from
  aggregated `serverStatus` and inner gateway response statuses.
- All three new diagnostic entities are disabled by default and cause no
  additional cloud requests when enabled.

### Security and compatibility

- Recent request diagnostics never retain URLs, resource paths, gateway or
  config-entry identifiers, tokens, request bodies, response payloads, or
  resource values.
- History is bounded by age and count, held only in memory, and cleared on
  every Home Assistant restart.
- Existing device identifiers, entity IDs, polling intervals, controls, and
  dashboards remain unchanged.

### Validation

- 483 automated tests pass with 95.18% branch coverage, including rolling
  expiry, bounded history, retry and fallback accounting, bulk status
  separation, privacy, and disabled-by-default entity behavior.
- Ruff formatting and linting and strict Mypy type checking pass.

### Upgrade notes and limitations

- Restart Home Assistant after updating.
- Enable the optional request diagnostic sensors manually under **Settings →
  Devices & services → Entities** if continuous visibility is wanted.
- The p95 value is a bounded histogram estimate, not a raw-request percentile.
- Metrics restart at zero when Home Assistant restarts and are not a billing
  or provider quota counter.

## [0.5.5] - 2026-08-30

Version 0.5.5 gives known PointT percentage measurements their correct Home
Assistant semantics without changing discovery or cloud traffic.

### Added

- Humidity device classes for supported heating-circuit, room-zone,
  room-device, and ventilation humidity resources.
- Battery device classes for supported room-device battery levels.

### Behavior changes

- Pump modulation, fan output, radio signal, update progress, and power
  reduction remain generic percentages because they have different semantics.

### Security and compatibility

- Device-class selection requires both an exact allowlisted PointT path and the
  expected percent unit. Existing entity IDs, device identifiers, polling, and
  availability behavior remain unchanged.

### Validation

- 479 automated tests pass with 95.14% branch coverage.
- Ruff formatting and linting pass.
- Strict Mypy type checking passes.

### Upgrade notes and limitations

- No migration or reconfiguration is required.
- The change annotates only resources exposed by the connected installation;
  it cannot make a humidity or battery resource appear when PointT returns it
  as unsupported.

## [0.5.4] - 2026-08-28

Version 0.5.4 improves K30 compatibility when PointT accepts a
bulk request but reports temporary failures for every resource inside it.

### Added

- Diagnostics separately count bulk `serverStatus` and inner gateway-response
  status classes.
- Diagnostics group bounded individual fallback requests by a privacy-safe
  reason.

### Fixed

- Discovery and polling now retry individually when a correctly formed bulk
  item reports a temporary 5xx failure, not only when its payload is malformed.

### Behavior changes

- Bulk remains the default. Individual fallback stays bounded and still does
  not run for unsupported resources, authentication failures, or rate limits.

### Security and compatibility

- The new diagnostics contain only aggregate status classes and reasons. They
  contain no gateway identifiers, resource values, paths, or raw responses.
- Existing entity IDs, device identifiers, history, automations, and dashboards
  remain unchanged.

### Validation

- 469 automated tests pass with 95.13% branch coverage.
- Ruff formatting and linting pass.
- Strict Mypy type checking passes.

### Upgrade notes and limitations

- Affected MX300/K30 installations should retry setup after updating and share
  only reviewed, redacted diagnostics if discovery still fails.

## [0.5.3] - 2026-08-28

Version 0.5.3 improves compatibility with evolving PointT enum values and
makes incomplete but supported capabilities easier to diagnose.

### Added

- Diagnostics identify supported scalar capabilities that currently provide no
  value.
- Diagnostics count distinct undeclared enum values without retaining or
  exposing the values themselves.

### Fixed

- Enum values first reported after entity creation can no longer produce an
  invalid Home Assistant sensor state. The entity reports `unknown` until the
  integration knows the value.
- The PointT heat-source type `gas_boiler` is normalized to the existing
  translated **Boiler** state (**Heizkessel** in German).

### Behavior changes

- One privacy-safe warning is logged per affected entity, using only a
  normalized resource-path template.
- A heat-source type describes that heat source only and is not used to infer
  the type of the complete installation.

### Security and compatibility

- Raw unknown enum values, gateway identifiers, and resource values remain
  excluded from logs and diagnostics.
- Existing entity IDs, device identifiers, history, automations, and dashboards
  remain unchanged.

### Validation

- 466 automated tests pass with 95.16% branch coverage.
- Ruff formatting and linting pass.
- Strict Mypy type checking passes.
- German, English, and source translation catalogs are covered by repository
  tests.
- Dependency audit reports no known vulnerabilities.
- Hassfest, HACS validation, and CodeQL pass in the protected release workflow.

### Upgrade notes and limitations

- Restart Home Assistant after updating.
- Previously unknown enum values remain unavailable until they are explicitly
  understood and translated by the integration.
- No migration is required.

## [0.5.2] - 2026-08-28

Version 0.5.2 improves compatibility with MX300 installations using K30
gateways when individual PointT resources are available but the corresponding
bulk response cannot be processed.

### Added

- Privacy-safe diagnostics are now available while initial setup is still
  retrying.
- Request diagnostics distinguish HTTP-successful bulk items whose payload
  could not be parsed.

### Fixed

- Malformed successful bulk items and unreadable bulk envelopes are recovered
  through strictly bounded individual resource reads.
- MX300/K30 installations with divergent PointT bulk responses can complete
  discovery.
- Diagnostics no longer fail because runtime data is unavailable during the
  first setup attempt.

### Why bulk requests remain the default

A heating installation can expose dozens or hundreds of resources. PointT bulk
requests retrieve up to 30 resources with one HTTP request, reducing cloud
traffic, update latency, and the risk of rate limiting.

Individual requests are therefore used only as a bounded compatibility
fallback. Successfully parsed bulk resources are not requested again
individually.

### Behavior changes

- Discovery uses no more than 30 individual fallback reads in total.
- Normal polling uses no more than 5 fallback reads per affected bulk chunk.
- Unsupported resources returning 403, 404, or 406 do not trigger fallback
  requests.
- No entities, controls, entity IDs, or device identifiers are changed.

### Security and compatibility

- Debug output contains only normalized path templates, HTTP status, and parser
  error categories.
- Tokens, gateway identifiers, raw payloads, and resource values remain
  excluded.
- Authentication and rate-limit handling are unchanged.
- Existing Home Assistant history, automations, and dashboards are preserved.

### Validation

- 463 automated tests pass with 95.13% branch coverage.
- Ruff formatting and linting pass.
- Strict Mypy type checking passes.
- Dependency audit reports no known vulnerabilities.
- Hassfest, HACS validation, and CodeQL pass.

### Upgrade notes and limitations

- Restart Home Assistant after updating.
- An entry currently in `setup_retry` will retry setup automatically.
- The exact cloud-side response difference could not be reproduced locally.
  Confirmation on the affected MX300/K30 installation is still required.

## [0.5.1] - 2026-08-23

Version 0.5.1 corrects how Home Assistant identifies the controller and the
PointT communication gateway of an installation.

### Added

- Controller-aware device identification for MX300 and MX400 modules reported
  in structured `/system/info` data.
- Regression coverage for a real-world MX400 system reported through a
  K40/K40RF gateway and for ambiguous multi-controller data.

### Changed

- Home Assistant now identifies an MX300 or MX400 controller from structured
  `/system/info` module data instead of presenting a K30/K40 communication
  gateway as the heating-system model. K-series gateway identity and hardware
  remain available through diagnostics and gateway resources. Ambiguous or
  missing controller data falls back to a neutral Bosch/Buderus Heating device.
- Development tooling updates Hypothesis to 6.165.10, Mypy to 2.3.1, and Ruff
  plus its pre-commit hook to 0.16.3. Pytest remains at the Home Assistant test
  framework's required 9.0.3 release.

### Security and compatibility

- Device and entity identifiers remain unchanged, preserving existing entity
  IDs, history, automations, and dashboards.
- Recognition uses a strict MX300/MX400 allowlist. A K30/K40 gateway name alone
  is never used to infer a controller model, and conflicting controller data is
  not guessed.
- No additional cloud requests, permissions, stored credentials, or writable
  resources are introduced.

### Validation

- 453 automated tests pass with 95.21% branch coverage.
- Ruff formatting and linting and strict Mypy type checking pass.
- The model-selection test mirrors the observed MX400 plus K40RF structure and
  verifies that the gateway hardware remains available.

### Upgrade notes and limitations

- Restart Home Assistant after upgrading so the device registry receives the
  corrected model and name.
- Only MX300 and MX400 are currently recognized as controller models. Unknown,
  missing, or ambiguous controller information is displayed as the neutral
  Bosch/Buderus Heating device while gateway details remain available in
  diagnostics.

## [0.5.0] - 2026-08-23

Version 0.5.0 expands heat-pump monitoring with optional PointT
resources confirmed in both MyBuderus and HomeCom Easy while retaining strict
capability and schema checks.

### Added

- Optional operating values for emergency and standby mode, the PV contact,
  Smart Function, primary auxiliary-heater state, and passive-cooling inlet
  temperature.
- Per-source current power, power percentage, defrost state, and brine inlet
  and outlet temperatures for every heat-source ID reported by PointT.
- Optional read-only capabilities for heating-circuit boost and cooling,
  fresh-water stations, per-source energy, hybrid systems, low-noise and power
  limiting, solar, pool, ventilation, room zones, room devices, photovoltaics,
  and gateway-provided variable-tariff state.
- English and German names and state translations based on the official app
  terminology.
- A capability-gated Silent Mode select with Off, Automatic, and On options.
- A capability-gated auxiliary-heater mode select with Off, On, and Auto.
- A maximum-supply-temperature number control with whole-degree steps and an
  installation-specific range read dynamically from PointT. A broad 0–100 °C
  plausibility envelope rejects corrupt metadata. It is disabled by default
  because it is an installer-level setting.

### Changed

- Discovery combines gateway references with a bounded official-app path
  catalog. Per-source probes are derived dynamically and do not assume `hs1`
  or `hs2`.
- Unsupported optional paths are discarded after the initial probe and never
  enter recurring polling.
- Fault discovery now derives failure paths only from real `hs<number>` heat
  sources. Ordinary heat-source resources no longer cause pointless
  `activefailure` and `failurelist` probes.
- Optional energy totals, water/gas consumption, volume flow, operating hours,
  power, pressure, humidity, and temperature values use native Home Assistant
  units and state classes when the PointT unit is known.

### Security and compatibility

- Optional resources remain read-only unless they have an explicit write
  policy. Silent Mode and auxiliary-heater mode require their exact writable
  `stringValue` schemas and complete allowed-value sets. Maximum supply
  temperature requires a writable Celsius number with complete gateway limits
  inside a broad 0–100 °C plausibility envelope.
- All three controls use the existing single-PUT transaction with mandatory,
  staggered read-back and no blind write retry.
- Entities are created only when the returned PointT type and unit match a
  known schema. Unexpected schemas remain excluded and visible in redacted
  capability diagnostics.
- Existing entity IDs, devices, and controls are unchanged. New values use the
  existing cadence groups and do not create new write permissions.

### Validation

- 451 automated tests pass with 95.20% branch coverage.
- The development build loads successfully on the physical Buderus K40 test
  system. That profile additionally exposes power-limitation and silent-mode
  state; unsupported optional families remain absent without setup errors.
- The K40 Silent Mode completed a physical `off` → `auto` → `off` write and
  read-back round trip. The original state was restored successfully.
- The K40 auxiliary-heater mode completed a physical `off` → `auto` → `off`
  write and read-back round trip. The heater itself remained off and the
  original setting was restored.
- The K40 maximum supply temperature completed a physical `40` → `41` → `40`
  °C write and read-back round trip. The original setting was restored.
- Ruff formatting and linting and strict Mypy type checking pass.

### Upgrade notes and limitations

- Restart Home Assistant after installing this release so discovery can
  probe the additional paths.
- Every value is optional and appears only when the connected gateway supports
  it. Historical `/recordings` data and the separate shared-energy tariff
  service remain intentionally excluded until an opt-in request and statistics
  model is defined.

## [0.4.1] - 2026-08-23

Version 0.4.1 fixes editing and moving ordinary holiday periods from Home
Assistant and makes the detailed holiday configuration match the terminology
and limits used by MyBuderus and HomeCom Easy.

### Fixed

- Editing, moving, or deleting an ordinary holiday no longer fails with
  **Recurring holidays are not supported** when Home Assistant supplies its
  technical event identifier and the empty recurrence range used for a single
  event.
- Actual recurrence ranges such as `THISANDFUTURE` and recurrence rules such
  as `RRULE` remain explicitly rejected because PointT holiday periods do not
  support recurring events.
- A date or time-only change continues to preserve the existing heating,
  hot-water, ventilation, circuit-assignment, thermal-disinfection, and
  temperature settings.

### Changed

- **Constant temperature** now appears directly below the central-heating
  mode in the **Configure holiday** dialog, before the hot-water settings.
- Constant-temperature input is restricted to the 5–30 °C range supported by
  the official apps. Stricter limits reported by the connected gateway still
  take precedence.
- English and German holiday labels now follow the terms extracted from
  MyBuderus and HomeCom Easy, including **Apply for**, **Central Heating**,
  **Constant temperature**, **Hot Water**, **Ventilation**, **Setback**,
  **OFF with Thermal Disinfection**, and their German equivalents.
- A new step-by-step guide explains how to create, configure, move, rename,
  and delete holiday periods and why calendar details and heating-system
  settings use separate Home Assistant dialogs.

### Validation

- 417 automated tests pass with 95.03% coverage.
- Ruff formatting and linting and strict Mypy type checking pass.
- The updated integration was installed on the physical Buderus K40 test
  system; Home Assistant's configuration check and restart completed
  successfully.

### Upgrade notes

- No entity IDs, devices, or existing dashboard references are changed.
- Restart Home Assistant after installing or updating the integration.

## [0.4.0] - 2026-08-22

Version 0.4.0 turns the previously read-only PointT holiday calendar into a
safe, fully confirmed control. Holiday dates can be managed in Home
Assistant's calendar, while the new integration dialog exposes the additional
heating settings that the standard calendar editor cannot represent.

### Added

- Capability-gated create, edit, and delete support for the **Holiday periods**
  calendar using Home Assistant's standard calendar interface.
- Exact PointT holiday payloads derived from MyBuderus and HomeCom Easy,
  including date mode, affected circuits, heating, hot-water, ventilation,
  thermal-disinfection, fixed-temperature, and encoded-name fields.
- Privacy-safe diagnostics indicating whether holiday calendar writes are
  currently available. The diagnostics schema is now version 5.
- A separate **Configure holiday** dialog for circuit assignments, heating,
  hot-water and ventilation modes, thermal disinfection, and constant room
  temperature. Its fields and choices follow the live PointT configuration.

### Changed

- Editing a holiday changes only its dates and, where PointT supports cloud
  names, its name. All circuit assignments and operating-mode settings are
  preserved from the latest confirmed PointT value.
- New holidays use the same conservative defaults as the official apps:
  all advertised circuits, fixed-temperature heating at 17 °C when supported,
  hot water off, ventilation off when supported, and thermal disinfection on
  when supported.
- PointT date-only end dates and Home Assistant's exclusive calendar end are
  converted in both directions without shifting the visible holiday period.
- Home Assistant's technical `recurrence_id` is accepted for ordinary event
  edits and deletions. Actual recurrence ranges and `RRULE` values remain
  unsupported.

### Security and compatibility

- Holiday writes are enabled only while the read-only list and the dedicated
  write configuration are available, current, and schema-valid.
- POST, PUT, and DELETE requests are each sent at most once. A timed-out
  mutation is resolved by bounded read-back and is never blindly repeated.
- Every mutation must be confirmed from `/holidayMode/list`; otherwise Home
  Assistant reports it as unsuccessful.
- Detailed holiday settings are accepted only from the options advertised by
  `/holidayMode/configuration`, and successful writes preserve the period's
  name and dates.
- Recurrence, location, and description fields are rejected because PointT
  does not support them. Unknown modes, malformed IDs, unsupported time steps,
  and incomplete existing periods are never written.

### Validation

- Creating, editing, configuring, and deleting a holiday was verified against
  a physical Buderus K40 installation. A date or time-only edit preserved all
  circuit assignments, modes, and temperatures.
- Multiple periods and Europe/Berlin daylight-saving transitions are covered
  by automated tests.
- 414 tests pass with 95.02% coverage. Ruff, Mypy, CodeQL, Hassfest, HACS
  validation, and the dependency audit pass.

### Upgrade notes

- No entity IDs or existing dashboard references are changed.
- Holiday controls appear only when the connected gateway advertises the
  required PointT resources. Unsupported fields remain hidden.
- Restart Home Assistant after installing or updating the integration.

## [0.3.0] - 2026-08-22

### Added

- Optional read-only holiday support for `/holidayMode/list`,
  `/holidayMode/configuration`, and `/holidayMode/activeModes`.
- A **Holiday mode active** binary sensor and a read-only **Holiday periods**
  calendar when the connected gateway exposes the corresponding resources.
- A **Next holiday** timestamp sensor showing the current or next period on
  the device page, with its end and status available as attributes.
- Tolerant handling of multiple periods, date-only and timestamp values,
  gateway or Home Assistant time zones, incomplete entries, and empty lists.

### Changed

- Holiday entities now use terminology aligned with MyBuderus and HomeCom Easy:
  **Holiday mode active**, **Next holiday**, and **Holiday periods**, with
  corresponding German translations.
- The capability roadmap now reflects read-only holiday support as released.

### Security and compatibility

- Holiday mode remains separate from the existing Away mode.
- No holiday write, edit, or delete action is exposed before physical
  write/read-back verification.
- Unsupported holiday paths are discarded during capability discovery and
  therefore add no recurring requests.
- Diagnostics contain only support and parser counters, never holiday names,
  dates, raw payloads, or other user-entered details.
- The diagnostics schema is version 4 and exposes only bounded holiday support
  and parser-health metadata.

### Validation

- Verified all three holiday resources on a physical Buderus K40 installation
  with a configured MyBuderus holiday period.
- Confirmed discovery, parsing, calendar output, active-state handling, and the
  next-period timestamp in Home Assistant.

## [0.2.2] - 2026-08-22

This release aligns heating/cooling and circuit states with the enums embedded
in MyBuderus and HomeCom Easy. It replaces previously inferred labels with the
terminology and raw values used by both official apps.

### Added

- A read-only **Heating/cooling switching** entity is created for every
  dynamically discovered `/heatingCircuits/{hc}/suWiSwitchMode` resource.
  Supported states are idle, automatic switching, heating only, and cooling
  only. The entity is omitted when the gateway does not expose the resource.
- Complete English and German state labels for all APK-confirmed heating-
  circuit and hot-water status values, including enabled, disabled, automatic,
  manual, cooling, holiday, extra hot water, and thermal disinfection states.

### Fixed

- **Current heating/cooling status** now uses the PointT values `off`, `forced`,
  and `cooling` instead of the previously inferred summer/winter enum.
- **Heating/cooling support** now recognizes `heat`, `cool`, and `heatCool`;
  camel-case values are normalized only at the Home Assistant boundary.
- **Season optimization** now recognizes `off`, `automatic`, `forcedHeat`, and
  `forcedCool`, with app-aligned labels for automatic switching, heating only,
  and cooling only.
- **Heating circuit status** now includes the APK-confirmed `ch_enabled` value
  in addition to disabled, emergency, floor-drying, summer-pause, boost, away,
  holiday, manual heating/cooling, and automatic heating states.
- **Hot water status** now uses the complete app enum: enabled, disabled,
  automatic, off, Eco, Eco+, Comfort, extra hot water, away, holiday, floor
  drying, and thermal disinfection.

### Compatibility

- Existing config entries, devices, entity IDs, history, dashboards, and
  automations are preserved; no migration is required.
- Only displayed state values and translations change where an older inferred
  enum did not match the official apps.
- The new entity remains read-only. Heating/cooling switching is not exposed as
  a control until a physical write and read-back test confirms safe behavior.
- Unsupported optional paths are ignored after capability discovery and do not
  add recurring cloud requests.

### Quality

- 315 automated tests passed with 95.63% coverage.
- Ruff formatting and linting, Mypy, CodeQL, hassfest, HACS validation, and the
  dependency audit passed.
- The release contains a reproducible integration ZIP and a SHA-256 checksum.

## [0.2.1] - 2026-08-20

### Added

- App-aligned operating states for current heat demand, compressor, electric
  auxiliary heater, central heating, seasonal optimization, data processing,
  and iSRC support.
- German and English state labels based on the terminology used by MyBuderus
  and HomeCom Easy, including blocked, defrost, home cooling, and pool modes.

### Enhanced

- Stable entities are now created for known array and multipart resources even
  when their startup payload is empty or temporarily incomplete.
- Dynamic PointT discovery is complemented by a bounded set of optional status
  resources used by the official apps. Unsupported resources remain ignored
  and do not add recurring cloud requests.
- Starts and operating times consistently provide total, heating, cooling, and
  hot-water entities, ready for values that become available after startup.

## [0.2.0] - 2026-08-19

### Added

- A guarded calculated environmental-energy sensor based on the complete
  PointT total energy balance.
- Active PointT fault and notification support with aggregate problem and count
  entities, appeared/resolved events, restart-safe deduplication, adaptive
  polling, tolerant parsing, and privacy-safe diagnostics.

### Changed

- Energy entities now use consistent context-first German and English names,
  including explicit calculated-value labeling and cooling-energy terminology.
- A direct PointT `electricity` counter and its component fallback now share a
  single total-consumption entity instead of creating duplicate entities.
- Default entity activation now follows an explicit user-facing policy:
  measurements, operating states, energy counters, starts, operating hours,
  TC3, pressure, and controls are enabled; sensitive, technical, and duplicate
  read-only entities remain opt-in.

## [0.1.0] - 2026-08-17

### Added

- Reproducible HACS release archives with a SHA-256 checksum and a tag-gated
  prerelease workflow that repeats all release checks before publishing.
- Capability-based firmware schema checks with an actionable rediscovery
  Repair when known PointT types or units change.
- A local exporter that turns the already-redacted Home Assistant diagnostics
  into a smaller, value-free installation inventory for compatibility work.
- Anonymous energy-counter reset counts in diagnostics while retaining Home
  Assistant's native `total_increasing` reset handling.
- Phase 0 repository and Home Assistant integration skeleton.
- Architecture, privacy, fixture, contribution, and security policies.
- Automated linting, type checking, tests, Home Assistant/HACS validation,
  dependency updates, and CodeQL analysis.
- Repository security baseline with CODEOWNERS, dependency auditing, protected
  branch rules, private vulnerability reporting, and squash-only merges.
- Internal, Home Assistant-independent PointT client with OAuth/PKCE helpers,
  serialized token refresh, typed reads, bounded bulk requests, error mapping,
  and diagnostics redaction.
- Home Assistant config flow for brand selection, SingleKey ID redirect
  validation, gateway discovery and selection, retry, and reauthentication.
- Typed config-entry runtime data with serialized token persistence and gateway
  availability checks.
- Gateway devices and a batch-based data coordinator with resource-level
  availability, last-good values, and separate fast and energy polling.
- Read-only sensors for central temperatures, modulation, system pressure,
  operating status, and direct PointT energy counters.
- Illustrated German beginner setup guide covering installation, SingleKey ID,
  the app redirect, gateway selection, troubleshooting, and safe support
  requests.
- Complete privacy-classified catalog of the 94 resources observed on the
  first K40 profile, including dynamic circuit rules, user-facing terminology,
  and staged write eligibility.
- Bounded batch discovery of the live PointT reference tree, dynamic devices
  for every discovered circuit and read-only entities for safe scalar and
  boolean resources.
- Cloud-friendly polling groups with 60-second live values, five-minute
  settings and energy counters, 15-minute long-term values, static startup
  data, last-good preservation, and bounded rate-limit backoff.
- Migration of early preview logical-device identifiers without losing entity
  IDs, history, areas, or dashboard references.
- Reference catalog of all current entities with PointT write metadata, Home
  Assistant access status, and polling frequency.
- Resource-level attempt, freshness, source, error, and failure state; bounded
  negative pauses for 403/404/504; a five-path core fallback after a batch
  failure; and a gateway circuit breaker after repeated complete failures.
- Gateway serial number, firmware, hardware, and model identifiers in Home
  Assistant device information, plus opt-in diagnostics for serial number,
  UUID, country, and sanitized system information.
- Six pressure-limit sensors from `/heatSources/systemPressureRange`, including
  support for the structured `values` object returned by PointT.
- A serialized PointT write transaction with strict live-capability validation,
  non-retried PUT requests, mandatory read-back, translated HA errors, and a
  heating-circuit operation-mode select verified on the reference K40.
- Dynamic, metadata-bounded controls for heating setpoints, hot-water mode and
  temperatures, extra hot water, fault reduction, and away mode while keeping
  administrative and installer resources read-only.

### Changed

- Use empirically confirmed one-degree steps for hot-water temperature controls
  while retaining half-degree steps for heating-circuit setpoints.
- Localize generated entity and group names from the configured Home Assistant
  system language, while preserving genuinely user-defined circuit names.
- Clarify the three hot-water temperature levels as Comfort, Eco, and Eco+
  start temperatures; document the related PointT-blocked stop temperatures
  and charging deltas from the appliance expert menu.
- Consolidate each gateway into one Home Assistant device and prefix entity
  names with dynamic groups such as `Heating circuit 1 –` and `Heat pump –`.
- Classify the six pressure-limit entities as diagnostics instead of primary
  operating sensors.
- Organize service counters and TC3 as diagnostics and outdoor temperature and
  away mode as primary values.
- Use MyBuderus labels for heating and hot-water temperature levels and decode
  PointT's UTF-16/Base64 configured names for display.
- Retire the empty switch-program placeholder and the duplicate aggregate
  start counter while preserving the detailed compressor counters, and omit
  optional circuit-name sensors when no name is configured.
- Represent static system information as a readable module-and-version text
  instead of an unhelpful module count while retaining sanitized attributes.
- Align German heating labels with the resolved MyBuderus and HomeCom Easy 5.0
  language resources while keeping clearer energy terminology where needed.
- Use the detected Bosch or Buderus brand for device manufacturers and gateway
  names, and identify PointT heat-pump sources as `Heat pump`.
- Redrew the brand icon with a compact symmetric three-blade fan and added its
  SVG source.
- Replaced the planned external Python client distribution with an internal
  `pointt` package that ships with the integration.
- Aligned the development and test environment with Home Assistant 2026.8 on
  Python 3.14.2.
- Expanded setup, reauthentication, retry, and error messages in English and
  German with step-by-step instructions.
