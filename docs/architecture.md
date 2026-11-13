# Architecture

The local [Bacon foundation](bacon-connection.md) has its own HTTP device list,
MQTT lifecycle and reported-state contract. It shares token refresh with
PointT while keeping device identifiers, state and command handling separate.

Additional-cloud family contracts feed native `water_heater` and `climate`
platforms through their shared identity/freshness layer. Both validate current
field metadata and use narrow shadow patches with reported-state confirmation.

The **PointT climate and hot-water entities from Beta.7** use a separate path:
they combine validated resources from one discovered circuit and write through
the existing serialized PointT preflight/PUT/read-back service. The controls
dialog submits drafts to the same service. These entities introduce no new
discovery paths or regular polling. See [Heating controls](heating-controls.md)
and [PointT hot water](pointt-hot-water.md).

The [token-lifecycle corrections from Beta.7](connection-recovery.md) retain
the account token manager across setup retries and reloads with unchanged
credentials. Cancelling a waiting caller does not cancel a shared refresh;
replacement sign-in credentials cannot be overwritten by an older response.

Bosch/Buderus Home Comfort contains a Home Assistant-independent PointT client in
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
- For advertised heating circuits, a small catalog of core child paths is read
  even if their parent directory fails. References precede catalog paths, which
  precede other optional probes. Failed probes do not invent capabilities.
- Only curated capabilities become entities; unknown resources stay diagnostic.
- Batch item failures do not discard successful items or last good values.
- A discovered resource returning 404 becomes unavailable until its next valid
  response, with a new attempt at the next regular group poll.
- A rate limit opens a global brake and never triggers an individual-request
  storm.
- `assess_control` derives each discovered resource's current write contract;
  UI options, write validation, and diagnostics use the same assessment.
- Control platforms listen for newly eligible resources without replacing
  entity identities or registry preferences.
- Writes validate current metadata and become successful only after read-back.
- Diagnostics never expose credentials, stable personal identifiers, names, or
  raw usage profiles.

The observed resource inventory, user-facing naming rules, dynamic circuit
model, and write-release classification are maintained in
[`resource-catalog.md`](resource-catalog.md).

## Nonessential startup reads

The exact paths, lifecycle events, error policy, timing and comparison with
0.8.0 are documented in [Request behavior and recovery](request-behavior.md).

Functional discovery runs for every selected gateway before supplementary
reads receive one shared 30-second entry budget. Only the exact unused license
endpoint is omitted. Reviewed informational leaves are deferred, never whole
directories. Device product names and installed refrigerant-circuit counts
become static only after a valid read-only scalar response. Writable or
structural surprises retain normal catalog handling; references remain bounded.

`StartupSession` owns memory-only values and pending reads per entry and gateway.
Its state module, `startup_data.py`, owns retry deadlines, path reservations,
gateway fairness and cache lifetime. Consumers use explicit state transitions;
read statuses are immutable and the queue is exposed through a read-only view.
`startup_reader.py` owns supplementary batch requests, deadlines, reply
validation and bounded reference expansion. Both modules are independent of
Home Assistant. The coordinator translates authentication errors, applies the
account brake, records outcomes and merges confirmed snapshots. Sensor code
only retains existing identities and adds recovered entities.

Automatic setup retries retain this state and the account brake. A completed
load followed by reload, a restart, changed options/gateway selection or changed
credentials starts fresh metadata state; ordinary token refresh does not.
No startup value cache is written. The discovered resources are kept per
gateway in `.storage/bosch_buderus_heating.discovery_inventory.<entry>` so that
parts missing at a later start can be noticed and searched again; existing
fault storage is unaffected.
The account brake is retained separately in Home Assistant memory across
explicit reloads as well, so clearing metadata does not bypass a rate limit.
Deleting the entry clears its brake; restarting Home Assistant clears memory.

Recovery runs after a gateway's normal polling work, never after a failed
functional cycle. One entry can claim at most one batch per minute, with a
30-second deadline and at most 30 paths. Gateways rotate, pending paths use
increasing per-path retry delays, and successful reads stop repeating. The
512-path discovery limit and depth-eight limit also constrain supplementary
reads and their references. No immediate GET fallback or automatic network
retry is used for these reads. Authentication refresh and account backoff
remain shared with the regular client.

Metadata timestamps record actual reads. Supplementary success neither
refreshes unrelated measurements nor confirms fault resolution. Sensor
listeners add recovered supported resources once and preserve registered IDs,
names and user enable/disable choices. Diagnostic-only values get no entities.

The local HTTP test models an already initialized gateway with 30 fast values
and two reviewed metadata leaves. It advances one hour of polling and counts
actual requests to a local server, including the 30-path batch boundary:

| Profile | Previous hourly HTTP requests | New hourly HTTP requests | Previous metadata path reads | New metadata path reads |
| --- | ---: | ---: | ---: | ---: |
| Standard | 64 | 60 | 8 | 0 |
| Cloud-friendly | 32 | 30 | 4 | 0 |

This measures steady polling after successful startup, not startup cost or
a physical installation. Smaller batches can carry metadata without needing
an extra HTTP request; in that case only requested paths and response size
decrease. Read-only product and circuit-count values no longer repeat 96 times
per day in the standard profile or 48 times in the cloud-friendly profile.

## Stable identities

Configuration owner: the existing Home Assistant `entry_id`.

For PointT setups, the entry's current `unique_id` is `{brand}:{digest}`, where
the digest is the first 24 hexadecimal characters of SHA-256 over the brand
and sorted gateway IDs. It is a gateway-list fingerprint, not a verified
stable account subject. Setups using only the additional cloud adapter use
`{brand}:bacon:{account_id}`. These identifiers are unchanged by the plant model.

Gateway device: `(domain, gateway_id)`

Logical entity group: `{kind}:{logical_id}` inside the stable entity key

Entity: `{gateway_id}:{logical_device}:{semantic_key}`

All entities of one gateway share its Home Assistant device. Dynamic logical
groups remain part of stable entity keys and are rendered as name prefixes.
Localized labels, model names, and live API values never participate in unique
IDs. Any future schema change requires an explicit migration and migration test.

## Plant model and device evidence

The separate [gateway identity contract](gateway-identity.md) now provides
strict parsers for original PointT gateway and local system responses, plus
an explicit selected-plant comparison. The [managed local runtime](local-runtime.md)
adds fresh system comparison for initial linking and saved evidence for later
offline starts. Registry ownership remains unchanged.

I introduced `plant_model.py` to distinguish installations, evidenced PointT
devices and functional groups. Its types and grouping operations work without
Home Assistant. The existing gateway adapter supplies the only device in this
first implementation. A heating circuit, hot-water circuit, solar circuit or
even a `/devices/...` resource path cannot establish an additional device.

The coordinator constructs one model per gateway. Device metadata and grouped
entity names resolve their ownership through this model. The current adapter
retains `(domain, gateway_id)` for every resource, including unknown paths.
Account-wide statistics and the separate additional-cloud and wallbox device
adapters keep their existing assignments.

`PlantKey` contains the configuration owner's `entry_id` and the stored
target of the system; the gateway is only the identifier of the device. The
older entry format has no targets, so there the gateway ID stands in. `GroupKey`
adds the group kind and the exact reported ID. Sparse numbering and additional
circuits therefore need no fixed slots or renumbering, and the system key stays
the same when the gateway is exchanged.

The inventory groups only paths supplied by the existing resource store.
It holds no live values, write permissions or second discovery cache. The
catalog's path rules classify groups; they do not prove resource availability
or enable a control. `FunctionGroup` and `function_group_for_path` now name
these rules explicitly. The former `LogicalDevice` and
`logical_device_for_path` names remain compatibility aliases.

I chose to keep the existing configuration entry as the lasting owner for
independent cloud and local connections. Disabling that entire entry
will still disable the whole setup. Separate connection settings, startup,
reauthentication and shutdown are implemented. Entry version 4 now activates
the managed cloud runtime and storage migration. Version 4.2 adds managed local
access for a retained target. Device evidence rules are unchanged.

The [local resource layer](local-api-resources.md) provides the complete pinned
K40/MX400 catalog and immutable response contracts. It has no polling loop,
registry effects or cloud aliases. The [local HTTPS client](local-read-client.md)
now connects selected catalog requests to the confirmed resource certificate,
with bounded reads and explicit failure results. The [shared local budget](local-budget.md)
coordinates setup and client admission, retains gateway pauses across restarts
and separates local health from configuration validity. The
[polling service](local-polling.md) adds fair scheduling, bounded discovery and
independent value expiry. Its local source now participates in the managed
runtime. Capability mapping to normal entities remains separate; reported local
write flags never grant write capability.

See [device presentation](device-presentation.md) for the device evidence rule,
area assignment and the requirements for any future assignment migration.

## Gateway exchange

A replaced gateway continues the same system only after the user confirms it.

Detection uses the gateway list that a cloud source already fetched at its
start; it sends no extra request. When a system's gateway is missing from the
list, exactly one enabled system of that cloud access is missing and exactly
one listed gateway is free (no wallbox, not known to this entry as a binding
or stored identity, not owned by another entry), the repair issue
"Gateway replaced?" appears. A pair the user kept separate is not offered
again. The issue disappears when the old gateway is listed again, when the
system is switched off or forgotten, and when the entry is removed.

Evidence is a fingerprint of the system's device modules from `/system/info`
(heat source, controller, outdoor unit; gateway rows excluded, only hashes
stored). The repair checks the running access's gateway list again and reads
the new gateway twice through it: its metadata and its `/system/info`. If at
least one device module matches, one confirmation is enough. Without a match,
or without stored or reported module data, the repair shows a warning and
requires a second confirmation. If the cloud cannot answer now (rate limit,
connection, sign-in), the repair stops and asks to try later. "Decide later"
changes nothing.

Continuing changes the stored target in place; `target_id` and `binding_id`
stay:

1. The gateway list is checked again: the old gateway is still absent, the new
   one is listed and still free.
2. A plan lists every registry entry of the entry whose unique ID starts with
   `{old_gateway}:` and the device with identifier `(domain, old_gateway)`.
   Nothing changes if a new unique ID or the new device identifier is taken.
   Request statistics and additional-cloud entities keep their IDs.
3. A private journal with the plan and the previous values is saved.
4. The entry stops. If it cannot stop, nothing is changed.
5. Without an await: unique IDs and the device identifier are rewritten and
   the configuration is saved (binding locator, the target's PointT identity,
   `legacy_identity.gateway_id`, the fault notification option). A failure in
   this step reverts the registry changes, and nothing else has changed.
6. While the entry is still stopped: a local access of the target is removed
   with the pending enrollment fence of the options flow, because its token
   and certificate pins belong to the old gateway, and its old local identity
   is dropped. Retained connection-inventory records move to the new unique
   IDs, the fault baseline and the notification dismissals continue under the
   new gateway, the old gateway's discovery inventory is dropped, the exchange
   time is recorded per target, the target's last shown counter sources are
   forgotten (the exchange's counter rule decides alone) and the issue is
   removed.
7. The entry starts again.

Home Assistant saves the registries and config entries with a delay, so a
crash can lose part of the change. The journal therefore records the process
that applied it and is deleted only by a later Home Assistant start that
loaded the complete change from disk: no `{old_gateway}:` unique IDs, the
device identifier and the binding moved. At each start the entry finishes an
existing journal before any source runs, and every part already done is
skipped. If the change cannot be completed (for example a unique ID is taken
meanwhile), the entry does not start, so no source can create entities next to
the old ones; the journal stays and the log names the reason. Entity IDs,
statistics, names, areas and automations therefore stay.

Counters (energy, runtime and start counters with state class `total` or
`total_increasing`) may continue in the heat source or start again with the
new gateway. Within 30 days after the recorded exchange each counter compares
its first values once with its last statistics state from before the exchange:
a value not lower passes, a value below 90 % is a new cycle (for `total` the
exchange time becomes a stored, unchanging `last_reset`), and a slightly lower
value shows no value until the counter reaches the recorded one again. The
decision is stored per entity in the module fingerprint store, so a settled
counter never reads the recorder again; without a recorder or recorded value
the values pass unchanged. A counter still below its recorded value when the
30 days end is shown again from then on, even though it is lower.

## Configuration storage foundation

I added a separate [storage contract and legacy converter](configuration-storage.md)
for independent connections, durable targets and explicit source bindings. It
prepares a version 4 proposal for the active entry format (4.2 since
0.9.0-alpha.1). The
converter preserves the existing cloud selection, credentials, compatibility
identities and user options without saving an entry or touching the registries.
Existing cloud readers and token persistence now use a central access layer.
Shared sessions are keyed by owner and connection; explicit login replacement is
distinguished from ordinary token rotation. A pure change classifier and request
generation tracker support targeted version 4 updates. The managed lifecycle
framework, cloud adapters and native entity attachment are tested with explicit
source contracts. Dependent services are wired as described below. Migration
validation precedes each storage conversion.

### Managed lifecycle preparation

The [configuration storage contract](configuration-storage.md#managed-connection-lifecycle)
now includes a startable version 4 owner and explicit connection-driver contracts.
Independent source jobs, retained entity identities and scoped reauthentication
are exercised through Home Assistant integration tests. All assigned cloud
targets must be confirmed before replacing a login.

The [managed PointT adapter](configuration-storage.md#pointt-adapter-in-the-managed-owner)
reuses the existing discovery, polling and confirmed-write coordinator with
gateway-bound clients. Its account resources remain shared, while stopping a
source invalidates pending requests and prevents stale consumers from using a
replacement login. Tests run the actual client against a loopback HTTP server.
Reported wallboxes use their existing read-only coordinator in the same connection
scope. `SourceUpdates` shares lifecycle guards and poll cancellation between
coordinators while keeping their data models separate. Wallbox polling cannot
hold the heating supplementary-read queue.

The additional-cloud adapter now uses one MQTT session per cloud connection and
a guarded access view per enabled device. Its claiming client shares token
refresh, rate limits and HTTP concurrency with PointT. Individual device changes
preserve peer devices and PointT sources; a replacement login retires all old
access views. The existing family entities consume a small `BaconAccess` protocol,
so their model rules remain independent of connection ownership. Tests use a
loopback HTTP server and MQTT 5 WebSocket broker.

The [native platform lifecycle](configuration-storage.md#native-entity-attachment)
now reuses each platform's existing builders through a small `PlatformScope`.
Replacing a source recreates only its entity objects while the HA registry keeps
their IDs and customizations. Old objects retain their stopped access view.
Request metrics remain separate per cloud connection, with the existing login's
legacy IDs preserved. No additional HA device is introduced for statistics.

The [dependent services](configuration-storage.md#diagnostics-notifications-repairs-and-dialogs)
now use current source ownership too. Diagnostics describe connections and
targets with numbers valid only inside that export. Fault notices retain an
unconfirmed baseline across missing access, including an offline restart.
Repairs change one cloud connection's polling or rediscover one source.
Controls, holidays and schedules reject drafts from a replaced source.
The [prepared storage transition](configuration-storage.md#prepared-storage-transition-and-offline-rehearsal)
now validates and applies a proposal in one HA entry update, preserving registry
identities and local fault stores. Offline rehearsal covers the captured
installation with substituted credentials. The [regular HA dispatch](configuration-storage.md#regular-ha-setup-and-migration-dispatch)
is connected and tested with the active version 4. The setup flow advertises
that version and runs these paths without a test override. The
[cloud management dialogs](configuration-storage.md#cloud-management-dialogs)
now support new setup, verified reconnection, device selection and polling
profiles. Additional parallel cloud sign-ins remain temporarily blocked because
a stable authenticated account identity is not yet established. Existing target
ownership and exact provider identities govern reconnection.
