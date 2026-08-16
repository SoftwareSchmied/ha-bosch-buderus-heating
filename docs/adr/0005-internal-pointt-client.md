# ADR 0005: Internal PointT client

- Status: Accepted
- Date: 2026-08-16
- Supersedes: ADR 0002 client distribution decision

## Decision

Keep the PointT client inside this repository under
`custom_components/bosch_buderus_heating/pointt`. The package has no Home
Assistant imports and retains explicit boundaries for authentication, token
rotation, transport, parsing, models, and redaction.

Do not publish or depend on a separate `bosch-buderus-cloud` distribution. The
integration uses Home Assistant's HTTP session and ships the client code as part
of the custom component.

One config entry still represents one SingleKey account and brand with multiple
selected gateways. Its future unique ID remains
`{brand}:{stable_account_subject}` and contains no plaintext username or email.

## Consequences

- Installation needs no additional PointT package from PyPI.
- Client and integration changes share one review and release cycle.
- PointT modules remain independently testable and must not import Home
  Assistant.
- The manifest keeps an empty `requirements` list unless another runtime
  dependency is introduced.
