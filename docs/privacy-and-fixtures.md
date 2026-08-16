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

The integration diagnostic export contains schema and aggregate state only.
It omits config-entry data, credentials, stable identifiers, firmware strings,
raw resource values, user-defined names, request URLs, and response bodies.
Dynamic PointT object IDs are replaced with `{hc}`, `{dhw}`, or `{hs}` path
placeholders. Unknown model strings are reduced to a generic device class.

Request metrics are in-memory counters. They retain categories, status
classes, batch sizes, outcomes, and durations, but never a URL, gateway ID,
resource path, payload, or returned value. The counters reset on restart and
do not create cloud requests. See [diagnose.md](diagnose.md) for the complete
field and user-review policy.

Maintainers can reduce a downloaded diagnostics report further with the
[anonymized inventory exporter](anonymisierte-inventur.md). It removes runtime
metrics and display names, permits only structural capability fields, and
rejects reports that do not declare all privacy guarantees or still contain a
concrete dynamic circuit identifier.
