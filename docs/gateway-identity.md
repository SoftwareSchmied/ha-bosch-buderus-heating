# Gateway identity evidence

> Development record: the status sentences on this page describe the state
> before 0.9.0-alpha.1. Since then local setup and regular local polling are
> active; see [local setup completion](local-completion.md) and
> [managed local access](local-runtime.md).

I added a separate identity parser and comparison in `gateway_identity.py`.
It provides a foundation for connecting a future local source to an existing
installation. It does not yet run during setup or polling.

## Parsing

The PointT parser reads one original gateway-list object before the tolerant
gateway parser discards additional ID fields. All present supported aliases
(`deviceId`, `id`, `gatewayId`, `gateway_id`) must contain the same valid string.
An invalid secondary alias or a contradiction is an error. Unrelated fields
such as model names and product serial numbers do not establish aliases.

The K40/MX400 parser reads the original `/system/basicInfo` response. It requires
that exact response path, type `systeminfo`, status `completed`, integer
`writeable: 0`, the documented timestamp shape, an array in `values`, and a
valid `gatewayId`. An empty timestamp is permitted by the published schema;
the timestamp is not proof of response freshness. Module contents are outside
this parser and are neither interpreted nor retained.

An identifier is an opaque, nonempty string. Whitespace and nonprintable
characters are rejected. Leading zeros, case and Unicode composition are
preserved; integers and floats are never converted to strings. The defensive
limit of 1,024 characters is an implementation limit, not a claimed vendor
length. Printable punctuation is retained as data, not interpreted as a path
or URL. A transport adapter must apply its own URL rules.

## Provenance and existing references

`IdentityContext` records the namespace, configuration owner, connection ID
and nonnegative integer generation. The adapter captures it before the request.
When the host, credentials or connection lifecycle change, the owner must
advance that generation or replace the connection ID. The comparison rejects
an observation whose context no longer equals the current one.

Retagging an old response with the current context would defeat this check.
The parser does not own requests, connection lifecycles or an elapsed-time
freshness policy; those belong to the future connection implementation.

`GatewayReference` represents a retained identity already accepted by its
configuration owner. It can be supplied from stored configuration without
waiting for a cloud request. It is not a container for unprocessed asynchronous
responses. The owner remains responsible for accepting, storing and updating
references through the appropriate connection validation.

## Comparison

The caller selects an existing `PlantKey` and supplies the relevant configured
inventory, including disabled entries. The comparison never selects the first
gateway from a list and never changes the requested target.

| Result | Meaning |
| --- | --- |
| `matched` | Comparable identities agree, the selected plant exists, and no competing installation or owner matches. The result retains the selected key. |
| `unverified` | The observation is stale, the target is absent, or the source namespaces have not been verified as comparable. |
| `conflict` | A comparable reference of the selected plant has a different identity. |
| `owner_conflict` | The selected plant belongs to another request owner, or the same comparable identity is already retained by another owner. |
| `ambiguous` | The same comparable identity is retained for multiple installations under this owner. |

Several agreeing references for one plant do not constitute several plants.
Contradictory comparable references of that plant cannot be ignored.
Unverified namespaces are not silently treated as aliases.

PointT and local system IDs remain separate namespaces. Cross-source
comparisons are disabled by default. A caller can supply
`verified_namespace_pairs` only after establishing a contract for the device
scope of that particular comparison. The pair works in either direction,
supporting both cloud-first and local-first setups. This module ships no
global approval matrix for device families.

My local MX400 check confirmed that its locally read gateway ID equals the
PointT ID currently stored in Home Assistant. This is evidence for that
reference installation. I did not obtain a fresh PointT response in that
check, and it does not establish a universal identity contract.

I subsequently ran this parser against a fresh local MX400 system response.
The comparison rejected an absent source contract, matched the explicitly
scoped reference contract, retained the selected installation key and rejected
a changed connection generation. This test did not install a local connection
in Home Assistant.

## Boundaries

A successful comparison is a decision value. It does not activate a connection,
change a device or entity ID, assign a resource, migrate an installation or
grant write access. Transport authentication and certificate binding remain
separate prerequisites. Gateway replacement and merging two existing
installations require their own explicit workflows.

Parser errors contain fixed codes. Identity values, connection IDs, owner IDs
and plant keys are excluded from the new objects' default representations.
Their explicit attributes are still private data; do not serialize these
objects wholesale into diagnostics or logs.

The implementation depends on the existing [plant model](architecture.md#plant-model-and-device-evidence)
and preserves its separation between devices and functional groups.
