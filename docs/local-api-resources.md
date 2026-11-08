# Local resource catalog and response contracts

I added a pure resource layer covering all 261 GET path templates in the pinned
K40/MX400 OpenAPI specification. It describes valid addresses and decodes local
responses; it does not make network requests, discover installed equipment or
create Home Assistant entities. Regular local polling and public setup remain
disabled until the runtime has been connected and tested. The separate
[HTTPS client](local-read-client.md) now supports individual bounded reads.

## Complete, versioned catalog

The catalog includes heating circuits, hot water, heat sources, solar circuits,
ventilation, zones, paired devices, system information, notifications, pool data,
recordings and gateway administration. Each operation retains its own path and
query parameters, response schemas and inline restrictions. For example, some
recording operations allow only `hs2` through `hs6`, while other heat-source
operations also allow `hs1`.

The documented parameter domains describe 1,287 fixed address possibilities,
plus dynamic signals. Loading this catalog does not expand or request those
addresses. The caller must select an actual resource. A dynamic signal request
requires an ID previously returned by `/signals`. This is resource evidence,
not proof of a separate physical device or an automatic cloud/local identity
match. Numbering such as `zone1` and `hc1` is never used to infer equivalence.

`make_request` accepts one exact template with explicit parameters.
`request_for_path` matches a canonical resource path. Unknown fields, invalid
IDs, out-of-domain numbers and malformed dates are rejected. Paths cannot
contain queries, fragments, encoded traversal or URI hosts. Accepted Unicode
signal IDs are encoded once when building the HTTP target. Neither function
stores a gateway address or a credential.

Recording requests support all documented sample rates: `PT1H`, `P1D`, `P1M`
and `all`, with optional `startDate` and `endDate`. Same-day windows are valid;
an end before the start is rejected. These contracts do not initiate an
unbounded history query. Window limits and scheduling remain transport/runtime
work. The catalog records the Wi-Fi `refresh` parameter, but normal read requests
reject `refresh=true` because the manufacturer describes it as triggering a scan.

## Typed, immutable observations

`decode_resource` receives the selected request, a bounded successful-response
body, its local source context and separate wall-clock and monotonic observation
times. HTTP errors belong to the next transport layer. It returns an immutable
`LocalObservation`; a caller cannot accidentally mutate nested results later.
The source context must be captured before the request. Supplying a context is
not itself authentication or evidence of device ownership.

All 21 referenced response schemas are covered, including inline variants:

| Source forms | Decoded form |
| --- | --- |
| Float, integer and string values; binary, custom-name, timezone, update-status and ventilation string variants; Wi-Fi pairing booleans | Scalar with validated metadata, options, named states and optional decoded text |
| `arrayData` | Ordered string collection |
| `emonValue` | Named numbers with their reported unit, without summation |
| `refEnum` | Resource references with separate IDs and URIs |
| `configData` | Validated immutable configuration object |
| `systeminfo` | Gateway information and explicit module records |
| `errorList`, `deviceErrors` | Separate typed fault collections |
| `apList`, `inclusionWhitelist` | Access points and inclusion entries |
| `updateStatus`, `updateReport` | Validated progress objects and module reports |
| `yRecordingExtended` | Resource reference, recording type and timestamped sample groups |

The complete validated document also remains available as immutable private data,
including bounded unknown fields where the source schema allows them. Closed
source objects still reject extra fields. These documents are not a public
diagnostic export; they can contain device names, identifiers and network data.
Value-bearing result representations and exceptions do not echo these fields.

## Interpretation rules

- Local capability is always read-only. `writeable` and `recordable` are retained
  as reported metadata, never as permission to send a local write command.
- JSON integers remain exact. Fractional/exponent numbers use `Decimal`, keeping
  long integral module identifiers exact even when written in scientific
  notation. Fractional identifiers are rejected instead of rounded. String
  identifiers keep leading zeroes.
- Numeric states such as `OFF_HEAT: 0` and `OFF_COOL: 90` retain their labels.
  A matching state marker is not returned as a numeric measurement.
- Units remain source-specific. Scalar and recording units use `unitOfMeasure`;
  named values use `unit`. There is no implicit conversion, energy classification
  or summation of totals and components. Unrecognized units remain visible as
  data issues; scalars with those units are not exposed as measurements.
- Duplicate named values or state names are rejected. Missing units on named
  values and recordings are explicitly marked. Unknown finite named fields can
  be preserved without assigning meaning to them.
- Text decoding requires explicit `valueConfig`. Documented plain UTF-8/ASCII
  names stay plain. Base64 names support UTF-8, ASCII and UTF-16, with BOM-aware
  decoding and big-endian UTF-16 when no BOM is present. Unsupported encoding
  metadata retains the original value with a data issue. Invalid explicit
  encoding is rejected; a string that merely resembles Base64 is not decoded.
- Reference URIs are data only. A URI cannot redirect a request to another host,
  change the selected HTTPS port or replace the validated resource ID.
- Recordings preserve each sample rate, timezone offset, `y` value and `c` count,
  including empty groups and zero-count samples. Negative counts, invalid
  timestamps and reversed windows are rejected. No statistics are imported and
  no undocumented sample timing or aggregation semantics are inferred.
- A response ID must match the request. Recording IDs may also include the
  requested query parameters, as the manufacturer examples do. Their values
  must match exactly; reordering is accepted, duplicate or changed parameters
  are rejected. A bare matching recording path is also accepted.

## Parser bounds and compatibility

The default JSON limit is 256 KiB, depth 32 and 8,192 values. Oversized responses
are rejected rather than truncated. These are defensive defaults, not evidence
that every possible recording fits. A caller can explicitly choose another
validated `JsonLimits` budget; a future transport must enforce the same byte
bound while reading. Finite decimal magnitudes are limited to exponents within
308 orders of magnitude. The parser rejects invalid UTF-8, duplicate JSON keys
and nonfinite numbers.

The [setup probe](local-setup-probe.md) and [token transport](local-enrollment.md)
now share the JSON machinery while retaining their earlier float handling,
response budgets and error outcomes. The enrollment journal reuses the duplicate
key check. Their existing tests remain part of validation.

## Source and verification

The source is the [official OpenAPI at commit 78e36b3](https://github.com/bosch-home-comfort/api-docs/blob/78e36b3b57e11ac6266558acba96c13bee88ceff/openapi/k-40-rf.yaml).
Its SHA-256 is
`ee7f17d9b783e4722b82e9c141dd63f1bc717c13c04bb435bfb2a7989323fb93`.
The extracted facts and fixture examples retain the upstream Apache-2.0
attribution and license. The extraction omits descriptive prose and preserves
the structural constraints. The handwritten integration code retains the
project license.

To reproduce or check the extraction in the development environment, run
`python scripts/update_local_api_catalog.py /path/to/k-40-rf.yaml --check`.
Updating the source pin is a deliberate review: new schema facets must also be
supported by the validator. Runtime imports need neither YAML nor JSON Schema
libraries and do not download or load external API files.

I tested the documented address domains and 253 source examples. Two source
examples contain placeholder heat-source IDs (`hsX` and `hs${hs_id}`); they are
tested as invalid addresses, with separate valid-address synthetic cases.
Eight operations have no source response example and have explicit synthetic
responses. The example unit `rpm"` is retained as an unknown unit, not silently
corrected. Additional cases cover malformed inputs, immutable unknown fields,
precise identifiers, mixed recording offsets, query matching and read-only
capability. These are automated contract tests, not a real gateway load test or
confirmation that all resources exist on one installation.
