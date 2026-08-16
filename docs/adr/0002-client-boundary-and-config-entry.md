# ADR 0002: Client boundary and config entry

- Status: Superseded by ADR 0005
- Date: 2026-08-16

## Decision

Keep transport, authentication, discovery, parsing, and write transactions in
the separately released `bosch-buderus-cloud` package with no Home Assistant
imports. This repository adapts that package to Home Assistant.

Use one config entry per SingleKey account and brand, with multiple selected
gateways. The entry unique ID is `{brand}:{stable_account_subject}` and contains
no plaintext username or email.

## Consequences

Token rotation has one owner per account/brand. The integration must pin a
released compatible client version; moving Git branches are prohibited.
