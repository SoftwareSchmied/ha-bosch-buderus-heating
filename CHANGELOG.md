# Changelog

All notable changes will be documented here. The project follows Semantic
Versioning after its first tagged preview.

## [Unreleased]

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
