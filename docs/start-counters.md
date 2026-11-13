# Start counters: behavior and implementation review

I revised PR #72 to expose missing general start counters while keeping each
counter tied to its own resource. This report describes the implementation
and the scope of validation, rather than assuming that all heating systems
return interchangeable counters.

## Previous weaknesses and corrections

| Previous behavior | Consequence | Change |
| --- | --- | --- |
| The integration excluded the general `/heatSources/numberOfStarts` resource. | Systems exposing only this counter had no corresponding sensor. | I expose its supported scalar format as a diagnostic sensor. |
| The initial PR suppressed that sensor whenever any per-source counter passed the general resource check. | A different counter could hide useful data; an unsupported scalar per-source payload could leave only empty part sensors. | I validate the two formats separately and keep supported sources independent. |
| Registry cleanup removed the general counter if discovery missed it or another counter appeared. | A temporary failure could discard its registry entry and user customization. | I remove this cleanup rule and preserve existing identities and activation choices. |
| Entity creation considered the currently available subset of resources. | Losing one source could create an additional general entity later in the session. | I create each supported source independently and deduplicate by its stable identity. |
| Generic scalar handling treated integer and floating-point error-like values differently and did not retain declared error markers. | Invalid readings could become counts, while a legitimate `32767.0` could be discarded. | I use one counter assessment for schema, numeric validation, entity readings and diagnostics. |
| Unknown multipart fields could become entities, and the first repeated field won. | Unexpected fields or conflicting values could be published. | I allow only known parts and reject conflicting readings deterministically. |
| An omitted startup resource had no regular polling path; an unexpected schema could be classified as static. | Retaining its registry entry alone did not ensure recovery. | I restore exact registered paths and keep eligible recovery leaves on the slow polling group. |
| Registry recovery accepted fewer source-ID forms than sensor creation. | An existing counter with a dotted or non-ASCII source ID could lose recovery after a failed startup read. | I use the same path validation for creation and recovery, and test the complete flow with the actual discovery implementation. |

## Supported contract

The general resource uses `floatValue`. Source-specific leaves support the
WDDW2 `floatValue` scalar as well as the ConnectKey `emonValue` format
with the known parts `total`, `ch`, `cooling`, `dhw` and `pool`. The parser
retains the existing object or array-of-objects representations of multipart
values. From 0.9.0 the parts may also come as a list of up to eight objects
in `value`, as a TC100.2 reports them (for example one object with `total`,
`ch` and `dhw`); a longer or empty list, or a list with something other than
objects, stays `invalid_structure`. Empty or partial supported responses
retain stable part entities.
The WDDW2 addition is included in v0.8.2-beta.5; its mapping and validation are
documented in [P01: WDDW2 types](wddw2-types.md).

I accept numeric, finite, non-negative whole counts with no unit or the
dimensionless `other` unit. Integer and whole floating-point representations
produce the same integer. Strings and booleans are rejected. I do not round,
truncate, sum parts, fill missing values with zero or substitute another source.
Counts above `2**53 - 1` are rejected to avoid lossy downstream numeric storage;
this is an integration precision limit, not an appliance-specific maximum.

From 0.8.2-beta.4, I ignore surrounding whitespace in counter units before
checking them. A single space, tabs and non-breaking spaces count as an empty
unit; surrounding whitespace around `other` is accepted too. Actual units
such as `C` and `kWh`, unexpected text and non-string unit fields are still
rejected. This rule applies to both the general `floatValue` counter and the
supported `emonValue` counters of individual heat sources. It does not change
unit handling for other sensors.

The optional `state` array contributes numeric error markers only for
`short`, `open`, `overflow`, `underflow` and `invalid`. A matching marker makes
the reading unknown. Missing, null or empty optional metadata is allowed.
Malformed known fields or more than 32 state entries make counter readings
unknown. Unknown fields, including non-error state labels, do not become
blanket numeric exclusions. A declared `off: 0` therefore does not discard zero.
Only the bounded numeric markers are retained, not arbitrary state content.

Each response replaces its predecessor's metadata. A valid response without
an old marker can recover the reading. Conflicting values for the same known
part make that part unknown; other valid parts remain usable. Identical valid
duplicates are accepted. An unassignable malformed multipart item makes that
response's part readings unknown.

## Identity, availability and statistics

I preserve the general identity `gateway:heatSources:numberOfStarts` and the
source identities `gateway:heatSources:hsN:numberOfStarts:part`. The new
source scalar uses `gateway:heatSources:hsN:numberOfStarts`. It never
reuses the multipart total's identity or statistics. Registry recovery retains
both identities if a device has reported both formats; only the currently
matching format can produce a value. Each supported
source is enabled by default when first created. From 0.9.0 a part that does
not occur in the reading the entity is created from (for example `cooling`
on a gas boiler) is created disabled, like `pool`; the key decides, not its
value. Without a usable reading (empty, or a form that is not read), the
parts keep these defaults. Existing registry choices,
including disabled entities, remain unchanged. Equal values do not establish
equal meaning. A source number does not by itself identify a boiler or heat pump.

A successful response with an unusable reading produces `unknown`. A failed
read produces `unavailable`. Neither condition publishes a synthetic zero.
The last successful payload may remain in memory for recovery bookkeeping,
but an unavailable counter does not return that payload as a current reading.

The state class remains `TOTAL_INCREASING`. A lower valid raw count is passed
through unchanged, so Home Assistant applies its normal reset semantics.
I do not create offsets or rewrite existing statistics. A lower reading alone
cannot establish whether an appliance actually reset its counter.

## Recovery and request cost

I restore exact supported identities only within their config entry and
gateway. Restored paths join the existing bounded discovery roots. A registry
entry supplies entity shape, never a fresh reading. Only actual successful
responses supply current schema and count data.

Accepted source IDs retain their exact spelling, including dots and non-ASCII
characters. Traversal segments, empty IDs, query or fragment components and
ambiguous entity identities remain rejected. The additional ID variants in
the tests are synthetic, not evidence of naming on a physical installation.

Registered leaves and explicitly referenced leaves that discovery attempted
remain eligible for recovery. Failed optional probes and references not
attempted because of discovery limits are not promoted. All recovery paths
share the existing 512-distinct-path budget. The eight-level discovery limit
and maximum of 30 paths per bulk request remain in force.

Recovery uses the slow group: 15 minutes with the standard profile and
30 minutes with the cloud-friendly profile. A confirmed or registered counter
can recover after a 404 on its next due group read. A newly advertised counter
that has never succeeded retains the existing 24-hour not-found pause.
Forbidden responses and service failures retain the coordinator's existing
pauses and backoff. A first forbidden response without any successful snapshot
uses the existing 24-hour pause; a previously successful resource uses the
existing repeated-forbidden threshold. Recovery creates no new retry timer.

The request increase is bounded by the additional admitted paths. One recovered
path adds one item per due group read; it adds a bulk HTTP request only when
another batch is needed. Existing transport retries still apply. The tests
check a single due path and a 65-path workload split into 30, 30 and 5 items,
and verify that these successful poll cycles make no individual GET requests.

## Diagnostics and validation

Schema 15 adds `start_counter` to relevant capability entries and
`start_counter_recovery` per gateway. These sections describe schema support,
reading rejection reasons, marker count, pending reads and resource-limit
status. They do not export counts, raw marker values or unknown field text.
Capability assessment describes the current resource, not the user's actual
entity-registry settings.

I first ran 20 focused regression cases against the original PR head
`d1028732955db93d3c33887beee529f2da0a6a0a`: 17 failed and 3 passed. The failing
cases covered source suppression, registry removal, invalid numeric readings,
declared error markers, unknown fields, conflicting parts and source outages.

The follow-up review reproduced five further failures caused by inconsistent
source-ID validation. I added creation-to-recovery identity checks, rejection
cases for unsafe paths, and complete discovery/polling cases for 403, 404 and
406 responses. The forbidden-resource pause remains effective while other
due resources can still be read.

The expanded tests exercise parser boundaries, malformed payloads, profile
timing, discovery budgets, partial failures, cloud backoff and authentication.
They also set up and unload the integration through Home Assistant with two
synthetic gateways, check actual states and registry entries, and repeat setup
to verify identity and customization retention.

An in-memory Home Assistant recorder test verifies actual accumulated
statistics: counts 100, 105 and 110 produce a sum of 10 despite an intervening
declared error marker and unavailable reading. A subsequent count of 3 gives
a sum of 13 through Home Assistant's normal reset handling.

Local validation on 2026-10-21 passed with 1,460 test cases and 96.64% combined
statement and branch coverage on Python 3.14.2. This includes 100 additional
cases relative to the initial PR, counting parametrized variants. The new
counter-assessment module has 100% statement and branch coverage. Ruff,
formatting and strict Mypy passed. The complete suite also verified repeatable
archive construction and import from an isolated installation of the ZIP.

CHLINDE confirmed that the initial implementation made the counter visible on
the affected system. The validation added in beta.2 later rejected that system's
single-space unit. In [PR #75](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/pull/75),
CHLINDE corrected the unit check and confirmed that the counter displays its
value again on a Buderus G125 BE ECO-21. Thank you, CHLINDE, for the detailed
report, fix, regression tests and appliance test.

## Validation of the beta.4 correction

I extended the regression tests to check both counter formats through parsing,
sensor creation and diagnostics. They also cover malformed unit types,
whitespace combined with invalid numbers or device error markers, transitions
between valid and invalid units, a 404 response, and recovery without replacing
existing entities. Setup and reload tests check actual Home Assistant states
and registry entries with whitespace units and two synthetic gateways.

The unit correction does not add requests, retries or write operations. Existing
discovery budgets, polling intervals and resource-error pauses remain in force.
The tests distinguish a successful response with an unusable count (`unknown`)
from an unsuccessful read (`unavailable`); neither publishes a synthetic zero.

CHLINDE's appliance test confirms the reported counter. I have not verified every
recovery scenario or multiple heat sources on physical installations. The test
payloads are synthetic. Final local check results are recorded in the PR
description; GitHub checks apply only to the commit they actually ran.
