# Architecture

Bosch/Buderus Heating contains a Home Assistant-independent PointT client in
`custom_components/bosch_buderus_heating/pointt`. That internal package owns
OAuth/PKCE, transport, PointT parsing, batch behavior, and error normalization.
The integration layer owns Home Assistant lifecycle, config flows,
coordinators, devices, entities, translations, repairs, and diagnostics.

```text
Config Flow
    -> TokenManager
    -> PointTClient
    -> DiscoveryService
    -> CapabilitySnapshot
    -> PollPlanner / ResourceStore
    -> Coordinators
    -> EntityFactory
```

## Boundaries

- Home Assistant injects its HTTP session; entity code performs no HTTP calls.
- PointT modules have no Home Assistant imports and can be tested in isolation.
- An account and brand form one config entry that may contain multiple gateways.
- Discovery follows bounded, cycle-safe references and runs only on explicit or
  infrequent events.
- Only curated capabilities become entities; unknown resources stay diagnostic.
- Batch item failures do not discard successful items or last good values.
- A rate limit opens a global brake and never triggers an individual-request
  storm.
- Writes validate current metadata and become successful only after read-back.
- Diagnostics never expose credentials, stable personal identifiers, names, or
  raw usage profiles.

The observed resource inventory, user-facing naming rules, dynamic circuit
model, and write-release classification are maintained in
[`ressourcenkatalog.md`](ressourcenkatalog.md).

## Stable identities

Config entry: `{brand}:{stable_account_subject}`

Gateway device: `(domain, gateway_id)`

Logical entity group: `{kind}:{logical_id}` inside the stable entity key

Entity: `{gateway_id}:{logical_device}:{semantic_key}`

All entities of one gateway share its Home Assistant device. Dynamic logical
groups remain part of stable entity keys and are rendered as name prefixes.
Localized labels, model names, and live API values never participate in unique
IDs. Any future schema change requires an explicit migration and migration test.
