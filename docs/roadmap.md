# Roadmap

## Published releases

As of 2026-10-29, **0.8.2 is Stable/Latest** and includes the work from all
0.8.2 betas plus the compatible maintenance updates and sign-in guide changes.
**0.8.3-beta.1** adds read-only time programs for heating and hot water.
The new sensors are disabled by default and await field confirmation of the
review corrections in #95. Existing control features remain part of 0.8.2.
The [project status](specification-status.md) separates
release results from remaining development work. The released foundation
supports Bosch and Buderus PointT systems and includes:

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
- understood heating, cooling, hot-water, compressor, auxiliary-heater, and
  season-optimization states in English and German;
- stable entity creation for known empty or partial startup payloads;
- optional holiday discovery with a dedicated active-state binary sensor,
  next-period timestamp sensor, and a multiple-period calendar whose create,
  edit, and delete actions are guarded by current PointT capabilities and
  mandatory read-back;
- privacy-safe holiday diagnostics without dates, names, or raw payloads.

The first physical contract profile is a Buderus heat-pump installation with a
K40 gateway. The integration is designed for compatible MX300, MX400,
K30/K30RF, and K40/K40RF installations, but support always depends on the
capabilities exposed by the individual PointT gateway.

Stable 0.8.2 includes the Midea/RWH water heaters, Midea Climate Series,
seven Air purifier families, Dry 4000i/6000i/8000i, read-only PointT wallboxes,
zone controls, RRC editing of existing weekly-program points and additional
holiday formats introduced in Beta.6. These new families still need physical-device confirmation.
P15, consumption history and regeneration logs, was cancelled.

## Added in Beta.7

Beta.7 includes the changes from PRs #85, #86 and #87:
the [controls dialog and PointT climates](heating-controls.md),
[connection recovery corrections](connection-recovery.md) and
[native PointT hot water](pointt-hot-water.md). They build on one another in
that order. They retain the existing appliance device, entity IDs and user
customizations. The dialog uses drafts; native and individual entity controls
write immediately with confirmation.

I confirmed the manual heating target and hot-water mode round trips in
Home Assistant and MyBuderus on my Buderus installation and restored the
original settings. This validates those operations on that system. It does
not replace testing on other devices or during real connection failures.

Install stable 0.8.2 through HACS, restart Home Assistant and reload the browser
to receive these controls and the Home Comfort display name.

## Easier sign-in in a later beta

I am investigating an optional local sign-in helper, potentially packaged as a
Home Assistant app. It should remove the need to copy the app callback from the
browser console. The current OAuth application rejects direct Home Assistant
callback URLs. Only an anonymous browser-extension proof of concept has been
verified; a full sign-in through a Home Assistant app still needs implementation
and testing. No helper or new sign-in method is included in stable 0.8.2.
The setup guide now covers preserving Chrome Console messages and investigating
a missing redirect address; this does not confirm the outcome of issue #49.

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

1. Continue validating the comprehensive optional read-only catalog on more
   installations. Discovery now covers hybrid/bivalence information,
   low-noise and silent modes, power limiting, boost, temporary setpoints,
   cooling limits, fresh-water-station values, solar, pool, ventilation, room
   devices, zones, PV status, and the gateway's variable-tariff resources.
   Unsupported paths are discarded after the initial probe. Silent Mode,
   auxiliary-heater operation mode, and maximum supply temperature are
   additionally available as strictly capability-gated controls.
2. Tariff offers from the separate shared-energy service. This is distinct
   from the read-only variable-tariff state already exposed by the gateway.
3. Promote optional resources from understood diagnostics to normal entities
   as their schemas and semantics are confirmed on real installations.
4. Additional schedule schemas, new programs and new switch points only after
   their contracts and safe round trips have been verified. Editing existing
   supported RRC weekly-program points is already included in Beta.6.

Consumption history, historical `/recordings` imports and regeneration logs
are not planned under the current scope because P15 was cancelled. Wallbox
controls remain outside the implemented scope; the eight read-only wallbox
entities and identified Midea air conditioners are already in Beta.6.
Gateway network configuration, device pairing, factory reset, raw EMS commands,
gateway removal and user administration remain excluded.

## Optional local backend

An eventual EMS-ESP or Modbus backend may implement the same semantic
capability model. It would be a separate transport with separate fixtures and
must not be described as local MX300/MX400 access.

## Official HACS publication

The integration was accepted into the default HACS catalog on 2026-10-20 through
[hacs/default#10040](https://github.com/hacs/default/pull/10040). I have updated
the [installation guide](setup.md#install-boschbuderus-heating) for direct HACS
search and installation. Thank you to the HACS maintainers for reviewing and
accepting the submission.

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
- [x] Official HACS catalog review completed on 2026-10-20.
- [ ] Complete installation, removal, privacy, troubleshooting, and migration
  documentation.
- [ ] Migration tests for config entries and unique IDs.
