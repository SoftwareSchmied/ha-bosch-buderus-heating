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

Heating-circuit operation-mode controls use only the intersection of the known
write codes (`off`, `manual`, `auto`) and each circuit's current `allowedValues`.
The official app's heating-mode mapper accepts each known advertised option
independently, so a circuit advertising only `manual` and `auto` still receives
a select control. Its options follow metadata changes during normal polling.
Unknown values are never added as write options, and every write still checks
current permissions and advertised values before its single PUT and read-back.
Other enum controls retain their existing complete-option requirements.

Scalar-control diagnostics share the entity builders' eligibility checks. They
report fixed rejection codes, known advertised enum options, and numeric
capability bounds without including current settings or unknown enum text.

## Consequences

Writable metadata alone is insufficient evidence. Each released control needs
real-device proof, failure tests, and translated actionable errors.
