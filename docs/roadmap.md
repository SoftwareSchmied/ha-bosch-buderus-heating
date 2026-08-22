# Roadmap

## Current status — 0.3.0

The integration is available as a stable GitHub release and supports Bosch and
Buderus PointT heating systems through one capability-based implementation.
The released foundation includes:

- brand-aware SingleKey ID authorization, serialized token rotation, reauth,
  and selection of one or more gateways;
- bounded dynamic discovery plus a small catalog of paths verified in both
  MyBuderus and HomeCom Easy;
- grouped batch polling, partial-result handling, negative capability pauses,
  bounded rate-limit backoff, and a gateway circuit breaker;
- dynamic heating-circuit, hot-water, heat-source, energy, diagnostic, and
  control entities without assuming `hc1`, `dhw1`, or `hs1`;
- active faults and notifications with appeared/resolved events, adaptive
  polling, deduplication, and privacy-safe diagnostics;
- direct EMON counters and a guarded calculated total environmental-energy
  value;
- validated user controls with one PUT, staggered read-back, and no automatic
  write retry;
- APK-aligned heating, cooling, hot-water, compressor, auxiliary-heater, and
  season-optimization states in English and German;
- stable entity creation for known empty or partial startup payloads;
- optional read-only holiday discovery with a dedicated active-state binary
  sensor, next-period timestamp sensor, and multiple-period calendar;
- privacy-safe holiday diagnostics without dates, names, or raw payloads.

The first physical contract profile is a Buderus heat-pump installation with a
K40 gateway. The integration is designed for compatible MX300, MX400,
K30/K30RF, and K40/K40RF installations, but support always depends on the
capabilities exposed by the individual PointT gateway.

## Next robustness increments

1. Collect anonymized inventories and contract fixtures from additional
   installation profiles.
2. Run a post-deployment 30-day test covering token rotation, cloud failures,
   rate limits, measured request load, and recovery behavior.
3. Confirm Bosch branding and behavior on a physical Bosch installation.
4. Test installations with multiple heating circuits, hot-water circuits, and
   heat sources.
5. Verify every released control individually on physical equipment and retain
   write/read-back evidence.
6. Extend notification fixtures with warning, maintenance, critical,
   device-error, and simultaneous multi-source payloads.

## Planned feature modules

Features are added only when their paths, schemas, semantics, and cloud cost
are understood. Unsupported paths must remain absent instead of producing
non-functional entities.

1. Additional heat-pump operating values, including source temperatures,
   passive cooling, instantaneous power, hybrid/bivalence information, silent
   operation, power limiting, boost, temporary setpoints, and cooling limits.
2. Historical `/recordings` data as an isolated module with explicit request
   budgets and no duplication of Home Assistant history.
3. Variable-tariff information from the separate shared-energy service.
4. Dynamically available solar, pool, ventilation, room-device, zone, and PV
   modules.
5. Holiday editing and schedule editing only after multiple real schemas and
   safe round trips have been verified.

Wallboxes, air conditioners, gateway network configuration, device pairing,
factory reset, raw EMS commands, gateway removal, and user administration are
outside the integration scope.

## Optional local backend

An eventual EMS-ESP or Modbus backend may implement the same semantic
capability model. It would be a separate transport with separate fixtures and
must not be described as local MX300/MX400 access.

## Official HACS publication

The integration repository passes its own HACS and hassfest validation. The
official catalog submission is
[hacs/default#10040](https://github.com/hacs/default/pull/10040). The pull
request remains open and requires a fresh upstream check run and maintainer
review before the integration appears in the default HACS catalog.

## Release gate for 1.0

- [ ] At least three distinct installation profiles.
- [ ] Successful physical tests with both Bosch and Buderus branding.
- [ ] MX300 plus at least one K30/K40/MX400 profile.
- [ ] A 30-day run without rate-limit cascades.
- [ ] Measured cloud request load documented.
- [ ] Real reauthentication and batch/single-read fallback tests.
- [ ] Physical write/read-back evidence for every released control.
- [ ] No known silent write failures or unhandled enum values.
- [x] Privacy-redacted Home Assistant diagnostics.
- [x] At least 95 percent automated test coverage.
- [x] Repository HACS and hassfest validation passing.
- [ ] Official HACS catalog review completed.
- [ ] Complete installation, removal, privacy, troubleshooting, and migration
  documentation.
- [ ] Migration tests for config entries and unique IDs.
