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

## Consequences

Writable metadata alone is insufficient evidence. Each released control needs
real-device proof, failure tests, and translated actionable errors.
