# Configuration storage foundation

I added a pure storage model and a converter for the existing cloud configuration.
They support independent connections within the existing configuration owner.
The active Home Assistant entry version is **4**, with minor version **2**
(since 0.9.0-alpha.1). Normal HA setup migrates existing entries, including
version 4.1, before starting the managed runtime; new configuration writes the
new format directly. Minor version 2 adds the
[local credential contract](local-credentials.md). Migration, cloud dialogs and
lifecycle tests run without a version override.

## Contracts

`configuration.load_configuration` validates a version **4.1** or **4.2** snapshot;
a later minor version of 4 is read like 4.2.
It returns immutable views of connections, targets and bindings, plus detached
JSON exports for later persistence. A target can represent a PointT object, an
independent cloud device or a local installation. A target does not establish
device evidence or create a Home Assistant device.

| Collection | Responsibility |
| --- | --- |
| Connections | Authentication context and credentials, separate from appliance identity |
| Targets | Durable identity references and compatibility with existing entity namespaces |
| Bindings | One connection's explicit association with one target |
| Options | Connection and binding activation, read order and existing user settings |

The model rejects missing references, competing targets, ambiguous identities,
duplicate bindings, invalid read orders and incompatible known adapter families.
PointT and local gateway identifiers cannot be made account-relative to bypass
identity conflicts. Separate cloud devices retain their actual account scope.
There are no fixed slots for heating circuits, hot-water circuits or solar groups.

An owner may contain a target without any remaining connection. Its identity
survives, ready for a later verified reconnection. The model validates this stored
state; the managed lifecycle removes access and retains unavailable entities.

Unknown JSON fields and adapter records survive explicit serialization. They
do not authorize transport, establish account identity or enable writes. A future
runtime must select only supported adapter and identity contracts.

A minor version only adds optional data, which older code can ignore; an
incompatible change needs a new major version. The loader therefore reads a
later minor version of 4, such as 4.3, like 4.2: unknown fields stay stored
and unused, and the entry keeps its minor version, so saving never lowers it.
A return within the same major version (for example from a later 0.9.x to
0.9.0) thus needs no backup. A later major version is not loaded.

## Preparing an existing entry

`configuration_migration.convert_legacy_configuration` accepts the owner's ID,
version 3 data and options. It returns a proposal without modifying its inputs,
loading a Home Assistant registry, contacting a server or saving an entry.

- Existing gateways and independent cloud devices share one proposed cloud
  connection. The last stored token set and the effective polling profile remain
  intact. An expired token can be converted without refreshing it.
- Internal IDs are derived deterministically from the owner and existing
  identity references. Gateway order, display names and token rotation do not
  replace them. Existing device and entity namespaces remain compatibility data.
- Extra data fields are retained in `legacy_extra`. Existing notification and
  other options survive. Old options whose names collide with the new collections
  are retained in `legacy_options`. An invalid old polling profile retains its
  original value there and keeps its previous effective `standard` behavior.
- The historical additional-cloud account hint is kept as `legacy_bacon_account`.
  It is not promoted to a verified general account identity.
- An additional-cloud entity prefix can be reconstructed from the old token's
  routing subject only when its hash matches the retained account hint. This
  reproduces the existing name formula and does not authenticate that token.
  Missing or conflicting routing information leaves the prefix unresolved.
- Versions 1 and 2 still require the existing preview migration to version 3.
  The converter never invokes that migration or attempts to downgrade a newer
  configuration.

The converter does not receive or replace the HA entry title, its historical
`unique_id`, global HA preferences or registry settings. The prepared HA storage
transition below preserves them while persisting a validated proposal. The
active migration hook converts to version 4 before managed setup.

## Prepared storage transition and offline rehearsal

`migrate_entry_storage` validates the complete proposal before applying data,
options and the storage version in one Home Assistant entry update. The function
does not yield between reading the current entry and updating it. It rejects an
entry that is no longer registered or whose state has not reached a supported
migration boundary. The caller remains responsible for stopping old runtime jobs.
An ongoing credential rotation blocks the transition even when its original
waiter was cancelled. The rotation must finish saving in the old format first;
a retry then migrates the latest tokens and retains the existing account pause.
The migration hook invokes this function only when the setup flow advertises
version 4, which is now the active version. No test override is needed.

Invalid input leaves the previous data unchanged. Repeating the operation on a
valid version 4 record does not write again; a broken or unsupported newer record
is rejected. Preview versions 1 and 2 must first complete the existing version 3
migration. That earlier device-registry work remains a separate operation.

I checked the prepared transition with HA configuration and registry APIs,
including interruption before the update and repeated loading of complete old
or new records. Existing identities, user names, disabled and hidden entities,
aliases, polling preferences and unknown options remain intact. The transition
does not rewrite registry or fault-store files and performs no transport requests.

I also replayed a private snapshot of my installation with real credential
strings replaced before export. Both offline starts and removal of the last
connection retained its 138 entities, device assignments and local fault stores.
This checks the captured configuration and local lifecycle, not fresh readings
or compatibility with other physical installations. Tests with synthetic active
faults cover notification and dismissal retention.

Returning to old software requires the complete matching previous configuration
or a Home Assistant backup. Changing only the version number cannot convert the
new structure back. The local rehearsal restores the previous configuration; it
does not claim to test Home Assistant backup restoration or the continued
validity of old login tokens after a real token rotation.

## Regular HA setup and migration dispatch

I connected the version 4 runtime to the integration's regular entry setup hook.
The migration hook follows the version advertised by the setup flow. That flow
advertises version 4. Existing entries are migrated; new entries use the same
storage shape directly. Tests exercise HA's own migration, setup, reload and
unload calls without changing the advertised version.

Before the storage transition, migration waits up to 30 seconds for this entry's
existing credential rotation. It does not start a refresh or wait for another
entry's authentication. Cancelling or timing out that wait leaves the rotation
running so it can still save its result. Expected transport or authentication
failures leave the saved credentials available for the new runtime's scoped
recovery. Unexpected persistence failures stop migration, and exception details
are not copied into migration logs. The synchronous transition checks the entry
and pending-rotation state again immediately before saving.

A slow cloud connection does not prevent the HA entry and its settings from
loading. An authentication failure opens reauthentication for that connection;
other connections continue independently. Failed setup cleans up background
jobs and platforms before the normal setup retry. If a platform refuses to
unload, the integration reports failure rather than discarding its live owner.
Shared gateway requests and token refreshes keep explicit ownership after their
last waiter stops, including retrieval of late errors.

The tested HA version treats migration errors and failed unloads as requiring a
restart. A simple reload is not supported in those states. The tests distinguish
this from a successful setup and reload, and from reconstructing a stopped entry
with corrected complete configuration. They do not claim to test an entire HA
process restart or backup restoration.

I also repeated the private 138-entity offline probe through HA's regular setup
and unload APIs. Both the unavailable-access case and removing the last access
preserved the captured registry settings and fault stores across two starts.

The cloud management dialogs below now support initial configuration,
reconnection, discovered device selection and each connection's polling profile.
Identity checks prevent a new entry from claiming targets retained by another
entry. Automatic migration is enabled in this implementation. The local product
adapter remains disabled.

## Configuration access and token persistence

I routed existing cloud configuration reads through `EntryConfiguration`.
Version 3 uses a deterministic connection ID that is also retained by the pure
converter. Version 4 helpers require an explicit connection ID, even when only
one connection exists. They never guess a cloud connection from dictionary order.
The existing metadata readers remain usable while credentials need renewal.

Token managers and account backoffs are held per `(entry_id, connection_id)`.
PointT, wallbox and additional cloud devices on the same connection share a
manager. An explicit replacement login creates a new manager while retaining
any active account pause. These keys do not establish whether separately stored
connections belong to the same real account.

Legacy version 3 data has one optional additive field, `cloud_auth_epoch`.
It defaults to zero and increases when a flow intentionally replaces credentials,
including a login that returns identical token values. Ordinary refreshes keep
the epoch. Conversion carries it into the connection's `auth_epoch`.

Before saving a rotation, the callback checks the entry object, registered
manager, connection ID, storage version, login context and last saved token set.
It reads current data and changes only that connection's token fields, without
awaiting between the final comparison and Home Assistant's update. Version 4
token writes also increase the data revision. A disabled but retained connection
may still save an already running rotation. Saving does not enable it or start a
request. Removed or replaced credentials cannot be restored by a late callback.

Other connections, unknown stored fields and options survive these narrow updates.
A broken independent source does not prevent valid rotated cloud credentials from
being saved. Full graph validation belongs to configuration changes, rather than
discarding an already issued rotating token because another source needs repair.

Open reauthentication and reconfiguration dialogs retain their login and selection
context. Token rotation and notification changes can proceed while a dialog is
open. A newer login, selection or polling-profile change aborts the stale dialog
with a specific message. Saving merges only the dialog's intended changes into
the latest data and options.

## Change distribution and request generations

`configuration_changes` supplies the pure dispatch contract for version 4.
Connection changes invalidate requests for that connection. Binding changes
invalidate only work through that binding. A target's read-order change is
reported to view consumers without retiring its unchanged readers. Removing one
connection therefore preserves the other source's pending requests, observations
and listeners, even when both serve the same target. The same applies when an
existing binding is removed or restored with otherwise unchanged target context.

Target identity, target data and target options other than the known `read_order`
preference still invalidate that target's source work. Unknown future options
remain conservative until their meaning is defined. Other targets keep running.
Consumers choosing between source observations must use the latest target
preferences; a valid source request stamp does not freeze that selection.
Ordinary cloud token rotation and display preferences do not invalidate request
generations. Local token replacement, changed endpoints and new authentication
epochs continue to retire the affected connection's work.

`ConfigurationRuns` issues request stamps before asynchronous work and checks
them before results are accepted. Stamps from another runtime, a removed scope or
an earlier generation remain invalid after the same scope is restored. This
request generation is separate from the credential epoch. The tests demonstrate
that disabling a source rejects its late readings while still retaining valid
rotated credentials.

The legacy listener reloads its single cloud setup for configuration changes
and leaves ordinary token rotation running. The active version 4 lifecycle uses
separate connection and source jobs. Cloud adapters, entity platforms and
dependent services use that owner after validated migration. The
[local connection](local-runtime.md) has its own managed lifecycle.

## Managed connection lifecycle

`ManagedEntry` loads the retained inventory and starts `ConnectionRuntime`
without waiting for a network response. Explicit driver factories supply the
actual transports. A connection owns its session; each target binding has a
separate source job. Unknown connection kinds are retained as unsupported and
cannot select an executable adapter through configuration data.

The framework implements separate connection and source states. Disabling one
binding stops that source while other targets keep their shared connection.
Disabling or removing a complete connection stops all its sources. With no
enabled bindings its session is idle. Source failures can retry independently;
an authentication failure stops the affected connection's consumers and requests
sign-in for that connection. The existing adapter remains responsible for its
normal polling cadence and for checking source context before requests, writes
and publication. No new polling interval is introduced here.

Configuration changes invalidate the affected scopes synchronously, including
when a result arrives before the Home Assistant update listener. Every source
attempt also has its own context, so a late result from a failed attempt cannot
replace a successful retry. A replacement waits for cleanup of its own preceding
job, without holding up another connection. Invalid owner or binding structure
stops old routing until valid configuration is restored.

## Connection settings and replacement sign-in

Version 4 connection dialogs can disable an access, change its enabled target
bindings, request cloud sign-in or remove the access after confirmation. Removing
an access removes only its credentials, bindings and associated options. Durable
targets, Home Assistant devices, entity IDs and user customizations remain. A
selected system with a local access is marked in its target options
(`local_plant`) so that it keeps showing its local readings; versions without
this feature ignore the mark. Adding a cloud access that selects the system
again removes it; a cloud access that does not select it leaves it in place.

A pending edit retains its exact connection and assigned targets. Ordinary token
rotation and changes to another connection do not invalidate it. Changing its
login epoch, connection settings or target assignments does. A successful save
updates the latest stored data rather than replacing an older dialog snapshot.

Replacement cloud credentials are accepted only after **every assigned target**
has been confirmed with the new authenticated session, including disabled
bindings. PointT gateway and wallbox IDs and standalone cloud device IDs are
checked through their respective backends; the existing standalone account
namespace must also match. A missing target or unavailable service leaves the
previous credentials intact. Rate-limit pauses also apply to verification.
This is a conservative check of existing assignments, not proof of a permanent
account identity. Adding further cloud accounts still needs that separate proof.

The Home Assistant reauthentication flow records the explicit connection ID.
Successful sign-in replaces that connection without a global entry reload.
Several authentication failures are handled one at a time. Local access cannot
accidentally open the cloud sign-in flow; its product-specific token setup
belongs to the later local adapter.

## Retained entity inventory

The version 1 inventory store records only platform, unique ID, target, binding,
adapter, semantic key and an optional canonical resource path. It has a limit of
4,096 records per owner. Names, areas, live values and writable capabilities are
not copied into it. The Home Assistant registries remain authoritative.

The framework can keep registered entities unavailable when no source can supply
fresh data, including after the last connection is removed. Cached write features
are cleared. Disabled registry entries remain disabled, and inventory restoration
never invents devices or writable entities. Corrupt inventory does not prevent
the registry fallback and is not overwritten with an empty cache merely because
loading failed. This includes a file that cannot be read, one written by a newer
version and one without its storage envelope; the entry starts and logs the
cause. Fresh, validated source records can rebuild it. Saving and
whole-owner removal are serialized to prevent a pending save from resurrecting
deleted inventory.

The inventory supports identity descriptions for all current platforms.
During an offline restart, the HA registry supplies unavailable states; entity
objects and control permissions are not reconstructed from cached descriptions.
Fresh source data creates the native objects through the platform builders
described below. Existing device evidence rules still apply.
Normal version 4 setup activates this owner after migration where needed.

## PointT adapter in the managed owner

`CloudDriverFactory` connects the existing PointT HTTP client and coordinator to
the managed lifecycle. Each cloud connection shares one token manager, account
backoff, request counter, concurrency limit and supplementary-read session.
Its initial gateway-list request is shared between PointT sources. A slow gateway
does not delay the functional discovery of another gateway. Standard and
cloud-friendly polling intervals, discovery bounds and confirmed writes use the
existing coordinator policies.

Each gateway client is bound to the exact gateway identifier and source attempt.
It checks that scope before authentication, after token refresh, before every
HTTP attempt, after waiting for concurrency and after the response. Stopping a
source prevents new requests and discards late results, including a late 401 that
would otherwise trigger token refresh. An authentication refresh already in
progress can persist its rotated credentials if the connection is only disabled;
removing or replacing the login rejects that result. An already transmitted
appliance write cannot be undone by cancellation, so a stopped source never
claims confirmation or retries the write.

An authentication failure requests sign-in for the affected cloud connection.
Other connections remain active. Replacing credentials still requires every
assigned target to be verified, including disabled targets. Replacement source
jobs receive new coordinators and clients; references held by old consumers
remain invalid. Scheduled polls and recovery tasks stop, retained values become
unavailable, and an initialized fault baseline is flushed before replacement.
Closing a source whose fault store could not be loaded does not overwrite it.

Gateways reported as `commodule` or `commodule2` use `ManagedWallboxCoordinator`.
It retains the existing read-only `cp0` parser, eight sensor descriptions and
section intervals. Wallbox sources share the PointT account resources with
heating sources, without running heating discovery or holding its supplementary
startup queue. A wallbox-only account does not need a heating source. Multiple
wallboxes keep their own gateway bindings, values and sensor identifiers.
After a first service or connection failure, an identified wallbox still publishes
its fixed sensor set as unavailable. Regular polling can recover those same
entities; authentication failures and active account rate limits keep their
existing stop behavior.

`SourceUpdates` supplies the same request-result guards, scoped authentication
failure handling and active-poll cancellation to both coordinator types. Their
device-specific parsing, polling and cleanup remain separate. Wallbox readings
become unavailable immediately when their source is disabled, and late responses
cannot revive a retired coordinator. Section-local failures retain the other
readings; a service failure affects that wallbox, while authentication and rate
limits retain their shared account scope. Reauthentication verifies selected
wallboxes even when their bindings are disabled. These lifecycle changes add no
charging commands or writable controls.

## Additional cloud devices in the managed owner

The same `CloudDriverFactory` owns a single additional-cloud MQTT session for
the enabled device bindings of each cloud connection. PointT and the claiming
HTTP client share token refresh, the account pause and HTTP concurrency. Device
selection is independent of PointT gateway discovery; an account containing only
additional-cloud devices does not start heating discovery.

Each `BaconDeviceAccess` exposes one exact device to the existing family entities
through the `BaconAccess` protocol. Disabling a device immediately invalidates
its view and cancels its pending operations. Other devices keep their session.
Disabling the last device closes MQTT while PointT sources continue. Reenabling
a device creates a fresh view and obtains its metadata through a targeted
subscription, including when selection changes overlap. The old view remains
invalid. Known ordering timestamps and versions are retained as rejection
boundaries, without copying old values or control permissions into the new view.

Membership and state resynchronization remain at five minutes; maintenance and
token checks remain at one minute. Existing freshness rules, retry delays and
family-specific control validation continue to apply. A broker or claiming
service failure retries the additional-cloud session independently. Authentication
failure retires all consumers of the affected cloud login and requests scoped
reauthentication. HTTP or MQTT rate limits pause cloud requests for that account.

Writes retain their existing current-state read, validation and reported-state
confirmation. Queued operations cannot send after their device source stops.
An already transmitted command cannot be undone by cancellation and is never
replayed automatically. The protocol does not establish request correlation or
exclusive ownership of a reported change; confirmation remains a check that the
newer reported state matches the requested values.

Native entity attachment and dependent services are described below. Normal
version 4 setup uses these adapters. Local product access remains disabled.

## Native entity attachment

All twelve HA platforms share their existing builders between version 3 setup
and the prepared managed owner. `PlatformScope` supplies only the current source,
its discovery-listener lifetime and any connection statistics. Model-specific
entity classes, capabilities, polling and write validation stay in their
existing modules.

`ManagedPlatforms` serializes replacement per binding and platform. It retires
the old discovery listeners and removes the affected native objects through HA's
normal entity lifecycle. The entity registry remains authoritative for unique
IDs, entity IDs, custom names, icons, disabled state, device assignments and
options. A fresh source then builds new objects using the same identities.
Unrelated source objects and their network sessions remain active.

Notification event entities store their original event timestamp alongside HA's
existing event restore data. If the last HA state is `unavailable` or `unknown`,
the native event restoration can recover that timestamp from the same entity's
extra data. Availability still depends on a current source. Restoring the event
does not trigger a new event, replay a notification or alter registry identities.
This uses HA's public restore hooks without modifying its shared restore cache or
private event fields. Existing valid timestamp states keep their normal restore
path. Legacy or invalid extra data cannot supply a missing timestamp; observation
times and device-reported fault dates are not used as substitutes. This additive
restore field does not change the configuration storage version.

Old objects keep the stopped coordinator or device access. Their outstanding
commands cannot move to a replacement login. A source stopped during entity
addition is checked again after addition and its late objects are removed.
A partial setup failure cleans up listeners and entities before a subsequent
source update can retry. An identity owned by another configuration entry is
rejected rather than reassigned.

Validated source records extend the value-free inventory. Offline restarts
retain unavailable registry states without old measurement values or writable
capabilities. Removing the last connection retains the targets and registry
entries. Stopping the entire owner closes connections, discovery listeners and
native platforms.

PointT request statistics are separate for each cloud connection. The migrated
original connection keeps its existing
`<entry_id>:request_metrics:<metric>` unique IDs. Additional connections use
`<entry_id>:connection:<connection_id>:request_metrics:<metric>`. These metrics
describe their PointT requests; they are not an aggregate of MQTT messages.
They exist while that connection has a heating coordinator, matching the prior
availability rule. Statistics belong to the connection, not an appliance target,
so the HA registry retains them without an appliance-inventory record or a new
device. Counters from different cloud connections are never combined.

Normal setup migrates to version 4 and activates this platform lifecycle.

## Diagnostics, notifications, repairs and dialogs

Schema 28 reports the prepared managed owner without assuming a single cloud
account. Export-local connection, target and binding numbers describe their
relationships, enabled state and availability. PointT request metrics and
additional-cloud reports stay separate per connection. Current gateway and
wallbox reports reference the same numbers. Removed access can leave a retained
target with no binding. No credentials, endpoint addresses, stable IDs or user
names are included, and diagnostics trigger no transport requests.

A fault notification belongs to its retained PointT target. Its existing ID,
dismissal fingerprints and notification preferences survive source replacement.
If access fails, is disabled or is removed, a visible incident remains marked
as unconfirmed and can still be dismissed locally. An offline owner restart
loads the existing fault baseline and dismissal preferences without creating
a transport or making a request. Only fresh source state can confirm resolution.
The managed notification consumer never saves an old fault baseline over a new
source's data; source drivers own those writes. Removing the complete HA entry
remains distinct from removing its last connection.

Removing the complete entry deletes its private files, also when it was never
loaded in this process: fault baselines, notification dismissals, token-refresh
histories, learned local schedules and the estimated energy totals of gateways
no other entry uses. All its repair notes are removed as well. Removing a local
access deletes its learned schedule. A gateway exchange deletes the old
gateway's fault and notification files once they are copied. At every start,
files of entry IDs that no longer exist are removed; backups set aside as
`.corrupt` stay.

Repeated-rate-limit repairs now belong to a cloud connection. Their confirmation
changes only that connection's polling profile. Firmware capability repairs
belong to a source binding and restart only that source's discovery. Open
confirmations reject changed connection settings or a replaced source. Neither
repair performs a whole-entry reload, resets an account pause, changes appliance
settings or enables disabled access. Orphaned scoped repairs are removed when
their connection or binding is removed.

The controls, holiday and RRC schedule dialogs resolve only current PointT
coordinators. A submitted draft remains tied to the coordinator used when it
was displayed. Identical values from replacement access do not authorize an
old draft. Schedule selections also retain the original offered source so a
changed list order cannot select another installation. Date layout, minute-only
time fields and the inline holiday delete button are unchanged.

Normal version 4 setup uses these services. The offline rehearsal checks the
storage transition. Local product access is not enabled.

## Local access is read-only

The [official K40/MX400 documentation](https://github.com/bosch-home-comfort/api-docs/blob/main/docs/k-40-rf.md)
describes the current local appliance API as read-only. The local storage shape
requires `open_api.read`; storing additional metadata does not grant a local
appliance write operation. Creating an access token is a separate authentication
operation.

In a future combined setup, supported local readings can complement cloud data.
Changing an MX400-connected appliance still requires the supported cloud write
path and its read-back confirmation. Local readings cannot replace cloud writes
during a cloud outage.

## Privacy and limits

The snapshot and its typed views omit private configuration from their ordinary
representations. Errors use fixed codes. Explicit `to_data()` and `to_options()`
exports are persistence data and must not be used as public diagnostics.

The JSON boundary rejects nonfinite numbers, unsupported object types and cyclic
input. Defensive limits are 32 nesting levels, 65,536 values, a 4 MiB ASCII JSON
representation and 1,024 characters per generic opaque identifier. These are
storage safeguards, not claimed appliance topology limits. The existing
additional-cloud adapter retains its own identifier and selection limits.

The endpoint check validates storage shape only. Authentication, certificate
verification, device evidence and connection ownership must still be checked by
the later setup flow before any local source is activated.

## Validation

The tests cover mixed and standalone-only cloud accounts, exact legacy identity
formats, deterministic IDs, unknown settings, immutable snapshots, invalid and
conflicting references, local-only storage and retention after the last connection
is removed. A separate process blocks Home Assistant, transport imports and socket
creation while exercising conversion and loading. Property-based cases check
that unknown JSON values survive conversion and repeated loading.

Release tests include the storage, access, change-distribution and managed
lifecycle modules and import them from an isolated ZIP installation. Additional Home Assistant tests
exercise actual in-memory entry updates, concurrent refreshes, cancelled waiters,
replacement logins, retained rate limits and stale dialogs. Synthetic version 4
tests cover independent connections and target-specific request invalidation.
The managed lifecycle tests use real Home Assistant flow and registry APIs with
controlled transport drivers and synthetic backend responses. They cover
independent startup, shutdown, retry, scoped sign-in, retained ownership and
storage failures. Additional managed PointT tests use the real client and
coordinator against a loopback HTTP server, including discovery, polling,
confirmed manual-room-setpoint writes, in-flight shutdown and account-scoped
authentication. These are simulated appliance responses, not installation or
write tests on real equipment. Managed wallbox tests cover both reported transport
families, section recovery, actual HTTP paths, eight native sensor descriptions,
separate wallboxes, retained registry preferences, delayed responses and shared
account failures. Managed additional-cloud tests exercise the real HTTP client
and MQTT 5 codec against a loopback WebSocket broker, including existing family
controls, separate device lifetimes, selection races, token rotation, login
replacement and cancellation. Native HA platform tests also cover all twelve
platform types, PointT, wallbox sensors and five additional-cloud family examples.
They verify automatic reattachment, disabled entries, registry customizations,
separate connection statistics, replacement login, delayed additions, partial
setup failure and retention after removing the last connection and restarting
offline. HA service calls confirm writes against synthetic HTTP/MQTT responses.
Managed service tests additionally cover redacted relationships, offline fault
notices, dismissal persistence, independent repair scopes and stale dialog
rejection. Storage-transition tests cover unchanged registries, one configuration
update, repeatability, invalid input, stopped-runtime boundaries, older previews
and local restoration of a complete previous configuration. The regular setup
and migration dispatch are covered by isolated HA tests. New and reconnect dialogs, device selection and polling profiles also run through
HA flow APIs. Loopback tests cover removal and reconnection through regular HA
setup, retaining device assignments, entity names and request-statistic IDs.
The active version now uses these paths; real installation remains a separate step.

## Cloud management dialogs

The active version 4 flow creates one configuration owner with explicit cloud
access and authenticated target selections. Its identity is independent of the
current gateway list. Existing owners expose connection settings even while all
sources are unavailable. The options allow scoped sign-in, enabling or removing
access, selection of existing targets, discovery of additional devices and a
polling profile for the selected cloud connection. HA's reconfigure action points
to these settings; it does not start an unsupported options-flow redirect.

Additional parallel cloud sign-ins are temporarily blocked across all entries,
including disabled ones. Different gateway lists or brand choices do not prove
different accounts. Existing multiple connections remain manageable; they are
not deleted or combined. A registration in progress also reserves the sign-in
flow until completion or cancellation. This is an interim restriction pending
a verified account-identity contract.

Removing cloud access retains the owner and targets. The owner then offers
"Connect cloud access". A fresh login and device listing are required, followed
by explicit selection. Exact provider identities reuse retained targets within
that owner; identities retained by another owner cannot be claimed silently.
The original cloud connection ID is reused for retained request-statistic IDs.
New owners store that ID in default_cloud_connection_id; migrated owners
recover their deterministic original ID. A removed connection's polling profile
is not retained; reconnection starts with the standard profile. Registry settings
and unrelated owner options remain intact.

Device discovery combines the fresh listing with previously assigned devices.
A temporarily missing assigned device can stay selected. Newly added devices
must be present in the authenticated listing, which expires after five minutes.
Deselecting a device disables its binding and preserves the target. Existing
target selection and polling settings remain available without a cloud query.
Discovery requires enabled cloud access and respects the account pause.

Managed device discovery and replacement sign-in verification share the same
three concurrent resource HTTP slots as running PointT, wallbox and additional
cloud sources. The resources belong to `(entry_id, connection_id)` and survive
runtime replacement and entry reloads. Removing the saved connection or entry
forgets its resources. Separate saved accesses retain separate budgets; these
keys do not establish a common real-world account identity.

Waiting checks revalidate the selected access, dialog lifetime and account pause
after acquiring a slot. Cancelling a waiter sends no HTTP request and records no
request attempt. Results from closed dialogs or changed access cannot replace
credentials. Replacement credentials are still accepted only after every assigned
target has been confirmed, including disabled targets.

OAuth exchange and token renewal do not consume resource slots. The retained
token manager serializes renewal and preserves a completed rotation even if its
original waiter is cancelled. The claiming client closes a rejected HTTP response
and releases its slot before requesting a new token, then retries at most once.
Existing-connection discovery uses that retained provider; replacement sign-in
checks use the submitted credentials without first changing the saved login.

The existing PointT statistics now include actual PointT management attempts and
retries. They exclude OAuth, claiming HTTP and MQTT; their sensor IDs are unchanged.
Local identity comparison already uses the managed PointT client and thus the same
resource budget. Local resource reads retain their separate scheduler and budget.
New cloud registration has no existing access to share and remains subject to the
separate registration exclusion. Legacy compatibility paths are unchanged.

Saving a dialog rejects changed ownership, removed access and replacement
credentials. Ordinary token rotation is preserved. The complete data and
options update is validated before saving. No local adapter, new physical-device
split or appliance write is enabled by these dialogs.

## Activation and rollback

The [local credential contract](local-credentials.md) adds minor version 2,
which the normal flow advertises since 0.9.0-alpha.1. New local records
distinguish reported token scope from verified read access and require explicit
certificate evidence. The transition of a 4.1 entry to 4.2 requires a stopped
owner with no pending credential refresh. Returning to a release before
0.9.0-alpha.1 requires the matching configuration backup.

Entry version 4 is now advertised by the setup flow. Existing version 3 entries
are converted before managed setup. Valid earlier previews first complete their
legacy device migration. Their later storage proposal is validated before that
registry work, so invalid data or an unsupported minor version leaves the preview
unchanged. Already valid version 4 entries are not rewritten. A disabled entry
is migrated only when HA starts it after reenabling.

The temporary limit on registering another cloud account applies to new sign-ins.
It does not remove or combine existing cloud entries during migration. Their
configured credentials and selected targets remain separate.

Returning to previous software requires both that software and the complete
matching configuration saved before migration, including entry data, options,
version metadata and registries. Changing only the version field or reinstalling
old code over new storage is unsupported. The previous release rejects version 4
data. Settings created after the backup are not part of that restored state.
Expired or invalidated cloud tokens may require a fresh login after rollback.

The isolated check uses the actual previous v0.8.3-beta.1 source in a separate
process, rather than changing the current flow's version for the test. It checks
rejection of new storage, loading of the restored private capture without cloud
access, and successful reads from a synthetic HTTP server after a full rollback.
It does not test the HA backup service or guarantee that old tokens remain valid
after a real cloud token rotation. No installation was performed for this check.
