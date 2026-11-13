# Privacy and fixture policy

PointT data can reveal identity, location, network configuration, equipment,
household routines, and energy use. Redaction is a product feature and a test
requirement.

## Never commit or publish

- access/refresh tokens, OAuth codes, redirect URLs, account subjects, or email;
- gateway IDs, UUIDs, serial numbers, MAC addresses, SSIDs, or IP addresses;
- location/time-zone data and user-defined circuit or schedule names;
- complete unreviewed response bodies or detailed usage profiles;
- proprietary app files, certificates, client secrets, or vendor logos.

## Contract fixture requirements

Fixtures contain the smallest structural evidence needed for a behavior. Stable
identifiers use fixture-local replacements such as `gateway-a`; free text and
base64-like names are removed; values are removed or coarsened unless their
exact boundary behavior is the subject of the test. A fixture documents device
class, firmware family, capture date, redaction method, and evidenced feature.

Automated redaction tests must reject token-shaped values, email addresses,
network identifiers, and known source IDs. Raw captures belong outside the
repository in an explicitly sensitive local directory and are never collected
automatically by Home Assistant.

Locally displayed identity diagnostics do not relax this policy. Serial
numbers, UUIDs, country data, and sanitized system information are disabled by
default in Home Assistant and must never be copied into issues, fixtures, logs,
or diagnostic exports without explicit redaction. Internal token fields from
system information are never exposed.

## Home Assistant diagnostics

The integration diagnostic export contains schema, aggregate state and a narrow
allowlist of technical metadata in schema 14. Known product labels, recognized
manufacturers, strictly numeric dotted version strings and validated installed
circuit counts may be included. Its privacy declaration explicitly identifies
these allowed resource values when present. It omits config-entry data,
credentials, stable identifiers, unrestricted firmware/product text, current
measurements and settings, user-defined names, request URLs and response bodies.
Canonical logical IDs such as `hc2`, `dhw1`, and `hs1` remain visible in local
diagnostics so multi-circuit discovery can be investigated. Arbitrary device IDs
and unrecognized dynamic IDs are replaced with path placeholders. Unknown model
strings are reduced to a generic device class.

Request metrics are in-memory counters. They retain categories, status
classes, batch sizes, outcomes, and durations, but never a URL, gateway ID,
resource path, payload, or returned value. The counters reset on restart and
do not create cloud requests. See [diagnostics.md](diagnostics.md) for the complete
field and user-review policy.

RRC schema 20 adds availability and schedule-shape decisions. It excludes
switch times, temperatures, program contents and unknown field names.
Canonical RRC IDs such as `zn2` and `pg3` remain visible; other dynamic
identifiers are redacted. The contract fixtures and browser data are synthetic,
except an anonymized TC100.2 read structure with constructed values.
See [RRC zones and schedules](rrc-zones-and-schedules.md) for their scope.
The room controller functions of 0.9.0-alpha.5 have one synthetic fixture
each in `tests/fixtures/contracts/rrc/` (`zone_climate.json`, `absence.json`,
`dhw.json`, `circuit.json`, `system.json`, `energy.json`,
`device_settings.json`), built from constructed values in the shapes of the
TC100.2 diagnostics, never from copied answers. Event names in `absence.json`
are marker values, and a test checks that no marker reaches a log or the
diagnostics. The `rrc_features` diagnostics count these functions without
values, names, dates or device numbers.

The energy history statistics and their stored import state are named by a
token of the gateway instead of its ID: an HMAC-SHA256 of the gateway ID,
keyed with the Home Assistant instance ID and cut to 16 hex digits (12 in the
statistic ID). A plain hash could be found back by hashing candidate IDs; the
keyed token cannot without the instance ID. Neither the gateway ID nor the
instance ID nor the token appears in the diagnostics.

Some resources are never requested at all, on any gateway: the radio device
arrays `/devices/devN` of room controllers (they carry the radio key `dlk` and
the radio identifier `sgtin`), `/devices/productLookup`, every `homematicip`
subtree, `/gateway/userPassword`, `/gateway/user/*`, `/gateway/installer/*`,
`/system/location/*` and `/system/autoAway/users/list`. They are not stored in
the discovery inventory either; an inventory of an earlier release drops them
when it is loaded. `dlk` counts as an identifier word like `sgtin`. The
synthetic fixture `ct200-shapes.json` fills these resources with marker
values, and a test checks that no marker reaches a request, the stored
inventory, a log, an entity state or the diagnostics.

Maintainers can reduce a downloaded diagnostics report further with the
[anonymized inventory exporter](anonymized-inventory.md). It removes runtime
metrics, discovery details, all technical values, concrete diagnostic paths,
and display names. It permits only structural capability fields and rejects reports that do not
declare all privacy guarantees or contain concrete heating-circuit, hot-water,
or heat-source IDs in their exported capability templates.
