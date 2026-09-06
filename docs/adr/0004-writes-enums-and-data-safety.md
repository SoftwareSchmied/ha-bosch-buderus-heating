# ADR 0004: Writes, enums, and data safety

- Status: Accepted
- Date: 2026-08-16

## Decision

Runtime options combine catalog codes, resource `allowedValues`, and the current
value when the entity is created; presentation translations live only in the
HA adapter. If a previously undeclared value first appears later, the entity
reports `unknown` and records a bounded, value-free diagnostic counter instead
of passing an invalid enum state to Home Assistant. Logs identify only the
normalized resource-path template and never contain the raw value.

Every write is risk-classified and follows:

```text
validate current capability -> PUT -> validate response -> read back -> compare
```

HTTP success without confirmed state is `WriteNotConfirmed`. Administrative or
unclear resources are never exposed as normal controls. Diagnostics and
fixtures are redacted by default according to the privacy policy.

All supported scalar control families derive their write contracts independently
from each resource. Enum options are the advertised, valid string codes;
subsets and new vendor codes need no code release. Known codes retain their
translations. Reserved display keys are escaped reversibly so similar raw
codes such as `Off` and `off` never write to the wrong option. Current values
outside an advertised contract are diagnosed separately and do not prevent
selecting a valid replacement.

Numeric bounds come from the gateway, without reference-installation ranges.
Units must belong to the supported physical quantity. The currently observed
resource schema does not establish a write step. Existing increments are UI
hints only and never reject an otherwise valid numeric target.

`assess_control` is shared by entities, transactions, and diagnostics. Newly
eligible discovered resources are added during coordinator updates; identities
and user registry choices are preserved. A legacy string switch keeps its ID
when its two actions are supported. Other option sets receive a separate select
with an `:options:control` suffix; losing an action makes the switch unavailable.
Unknown administrative paths still require an explicit function implementation.

Writes revalidate under the coordinator lock and remain confirmed only after
reading back the requested resource. Explicitly conflicting response IDs are
protocol errors, including inside bulk responses. Holiday dialogs retain the
baseline shown when opened, merge only user changes, and validate advertised
modes again before mutation. Creating a holiday with unavailable defaults
requires explicit choices through the integration's Configure dialog.

Diagnostics report known option names and counts of other options. New option
text is allowed in the local UI but remains excluded from diagnostic exports,
as do current settings and measurements.

## Consequences

Writable metadata alone is insufficient evidence. Each released control needs
real-device proof, failure tests, and translated actionable errors.
