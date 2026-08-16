# Roadmap

## Phase 0 — decisions and repository

Complete: stable names/domain, MIT license, ADRs, package skeleton, CI, privacy,
fixture, contribution, and security rules.

## Phase 1 — client foundation

Complete: internal typed PointT models and error hierarchy, brand-aware PKCE,
serialized token rotation, gateway listing, single-resource reads, batch reads
of at most 30 paths, partial-success handling, redaction, and isolated tests.

Implemented locally: the Home Assistant config flow launches the authorization
URL, validates the pasted app redirect, discovers gateways, persists rotated
tokens, supports reauthentication, and exposes translated errors. This remains
a developer feature until it passes a real Bosch and Buderus login.

## Phase 2 — discovery and capabilities

Implemented locally: bounded, batched reference-tree discovery, multiple
logical circuits, cycle/size protection, privacy classification, immutable
resources, cloud-friendly polling classes, resource-specific negative pauses,
a bounded core fallback, and a gateway circuit breaker. Remaining: anonymized
contract fixtures from additional installations.

## Phase 3 — read-only Home Assistant preview

Config flow, reauthentication, gateway selection, typed runtime data, dynamic
logical devices, scalar sensors, binary sensors, resource-level availability,
last-good values, grouped batch polling, redacted diagnostics, and aggregate
in-memory request metrics are implemented locally. Reconfigure supports gateway
selection, app-brand correction, explicit rediscovery, and two safe polling
profiles. Repeated rate limits create an actionable Repair. Curated capability
maturity prevents unknown API leaves from becoming entities. A separate,
explicit default policy enables normal measurements, states, energy counters,
and long-term values while keeping sensitive, technical, and duplicate sensors
opt-in. Known PointT schemas are checked after
discovery; changed types or units create a rediscovery Repair without blocking
new firmware versions merely because their version string is unknown.

## Next robustness increments

1. Real-device checks across additional installation profiles.
2. Additional anonymized installation inventories and contract fixtures.
3. A post-deployment long run covering token rotation and cloud failures.

## Phase 4 — energy

Release direct EMON counters first. Add calculated totals only when all
components share a proven balance and time basis. Never infer SCOP, standby
power, or historical values from incomplete snapshots.

## Phase 5 — verified controls

Add one control per small pull request after capability validation, risk
classification, real-device evidence, translated errors, and read-back tests.
The generic write transaction and the safe user-control allowlist are
implemented. Dynamic selects, numbers, and switches cover heating-circuit and
hot-water modes, user setpoints, extra hot water, and away mode. Administrative
and installer parameters remain blocked. The K40 sequence Manual → Auto →
Manual passed; delayed cloud propagation is handled by bounded, staggered
read-back GETs without repeating the PUT. Remaining controls still need
individual real-device checks.

## Release gate for 1.0

At minimum: three distinct installation profiles; both brands; MX300 and one
K30/K40/MX400 profile; a 30-day run without rate-limit cascades; measured cloud
load; real reauth and fallback tests; verified writes; redacted diagnostics;
95% coverage; passing HACS/hassfest; complete user and migration documentation.

## Preview 0.1.0

Prepared locally: aligned version metadata, deterministic component-rooted HACS
ZIP, SHA-256 checksum, isolated archive import test, and a tag-gated prerelease
workflow. Publishing remains a separate, explicit action. The workflow repeats
quality, dependency, hassfest, and HACS checks before creating a GitHub release.
