# RRC feature packages: hooks for contributors

Room controllers of the RRC 2.0 platform get their phase 2 and 3 functions
in separate packages (W1 zones, W2 absence and events, W3 hot water, W4
heating circuit, W5 system and energy, W6 radio devices). The foundation
(W0) gives every package its own files, so that a package never edits a
shared file. This page lists the hooks and the rules they follow.

## Files a package owns

| Package | Contract | Entities | Further own modules |
|---|---|---|---|
| W1 | `rrc_contract_zone.py` | `rrc_zone_entities.py` | `rrc.py`, `rrc_select.py`, `rrc_climate.py`, RRC guard in `heating_climate.py` |
| W2 | `rrc_contract_absence.py` | `rrc_absence_entities.py` | `rrc_events.py`, `rrc_event_calendar.py`, `rrc_boost.py` |
| W3 | `rrc_contract_dhw.py` | `rrc_dhw_entities.py` | `rrc_dhw.py`, `pointt_water_heater.py`, `water_heater.py` |
| W4 | `rrc_contract_circuit.py` | `rrc_circuit_entities.py` | – |
| W5 | `rrc_contract_system.py` | `rrc_system_entities.py` | `rrc_energy.py`, `rrc_energy_statistics.py`, `faults.py` |
| W6 | `rrc_contract_devices.py` | `rrc_device_setting_entities.py` | `rrc_devices.py`, `rrc_device_entities.py`, `rrc_device_settings.py` |

Every package also owns its tests (`tests/test_rrc_<topic>*.py`), fixtures
(`tests/fixtures/contracts/rrc/<topic>*.json`) and its pending translations
(`tests/translation_pending/W<n>.json`). `strings.json`,
`translations/en.json` and `translations/de.json` are shared: add keys only
and merge them with `scripts/merge_translation_keys.py`.

Everything else, including `writes.py`, `coordinator.py`, the platform files
and `rrc_contracts.py`, belongs to W0. If a package needs a change there, it
names the change in its report; it is added as a small separate commit
before the package is merged.

## The contract (`rrc_contract_base.RrcContract`)

Each contract module defines `CONTRACT = RrcContract(name=..., ...)`. Path
patterns are regular expressions matched with `re.fullmatch` against a
concrete path; use `rrc_contract_base.RRC_ID` for numeric IDs.

| Field | Effect (only on an RRC controller) |
|---|---|
| `enum_policies`, `number_policies` | Write policies tried before the generic ones; they become generic switches, selects, numbers or buttons |
| `blocked` | Pattern → refusal token; refuses writing on every gateway |
| `poll_groups` | Pattern → `PollGroup` of the path; it also keeps a list that is otherwise read hourly (W1 reads `/zones/list` with the modes) |
| `names` | Pattern → (German, English) name of the entity or control |
| `entity_keys` | Pattern → translation key, or platform → translation key |
| `option_keys` | Pattern → wire value → option key of a select |
| `read_only_mirrors` | The generic sensor of the path becomes a disabled diagnostic one |
| `understood` | The path counts as understood (needed for structures, see below) |
| `default_enabled`, `categories`, `icons` | Default activation, `config`/`diagnostic`, icon of generic entities and controls |
| `dialog_paths` | Paths shown in the controls dialog |
| `zone_fields` | More leaves below `/zones/znN` (W1) |
| `device_leaves` | Device type of `/devices/list` → leaves below `/devices/deviceN/` (W6) |
| `extra_roots` | Roots read only on an RRC controller (W2 `/events`, W5 `/energy`) |
| `optional_children` | Container pattern → leaves probed below it |
| `diagnostics` | Value-free report of the contract's resources (S12) |
| `texts` | Other (German, English) texts built from data, for the translation checks |

Two contracts with an equal pattern in one table fail at import. Patterns
that overlap without being equal give no control at all
(`rrc_contract_conflict`); call `rrc_contracts.registry().check_paths(...)`
with the paths of your fixture in a test.

S11: the write policies apply only through an RRC write context, the other
tables only where the caller knows the plant. The same paths exist on other
gateways, so never decide by the path alone.

## Write policies (`write_policies.py`)

All new fields default to today's behaviour.

- `EnumWritePolicy.fallback_options`: offered only when the resource has no
  `allowedValues` key at all and its current value is one of them (or an
  equivalent read of one).
- `EnumWritePolicy.on_value` / `off_value`: a switch with these exact wire
  values (S2), for example `"true"`/`"false"`.
- `EnumWritePolicy.equivalent_reads`: readings that confirm a written value,
  for example `{"Off": frozenset({"off"})}`.
- `EnumWritePolicy.ignored_options`: advertised placeholders left out of the
  options, for example `""`; `min_options`: fewer usable options leave no
  control (`too_few_options`), for example a mode with a single choice.
- `NumberWritePolicy.wire_integer`: sent as an integer; fractions are refused.
- `NumberWritePolicy.strict_grid`: the advertised step, else `step`, counted
  from `minValue`; off-grid values are refused, never rounded (S4). A step
  wider than the range leaves no control (`invalid_step`).
- `NumberWritePolicy.accepted_units`: metadata units accepted instead of the
  family of `unit` (for example `{"", "other"}`).
- `NumberWritePolicy.ui_unit_override`: the unit the number shows (`""` for
  none); `create_entity=False` writes a path without a number of its own.
- `value_check(resource, value, related)`: a refusal token before sending
  (`ContractRejected`, the token is the translation key). `related` holds the
  coordinator's fresh readings and the live reads of `expected`.
- `read_back_check(resource, value)`: a token that ends the read-back as not
  confirmed (`RrcWriteNotConfirmed` with that key), for example
  `rrc_override_ended`.

The flags of a contract setting (one check,
`writes._contract_availability_issue`, for every contract policy; the states
come from `rrc.flag_state`):

- `available` false, used or not, or malformed flags: no control
  (`rrc_unavailable`); an existing entity goes unavailable.
- `available` true (or missing) with `used` false: the control stays and
  every write is refused unsent (`ControlUnused`, a `ControlLocked`; the
  message comes from `control.unused_translation_key`).
- `unused_writable=True` on a policy keeps the earlier rule for settings
  that do not depend on `used` (the heating circuit): `used` false stays
  writable, used but unavailable is shown and locked (S8, `ControlLocked`).

Readings decide on their own whether a flag matters to them. Writing needs
`writeable` (S1) and limits from the metadata (S3).

## Writing

- Scalars: `control.async_set_control(coordinator, path, value, policy,
  expected=(...))`. The coordinator reads `expected` and, for a contract
  policy, the written path itself freshly in one bulk request first (S5; a
  change of `expected` refuses, a change of the path itself only updates the
  reading it is checked against), writes once (S6) and reads back after 1,
  2, 4 and 8 seconds on an RRC controller (S7, `RRC_READ_BACK_DELAYS`).
  Unconfirmed: `rrc_write_pending_confirmation`, and the path is read again
  with the next cycle; a later regular poll that shows the value counts as
  `late_confirmed`. A reading that a `read_back_check` ended the wait with
  is published as current. A `value_check` gets the fresh snapshots and the
  readings of `expected`; the zone mode and manual setpoint policies of the
  core use the W1 checks `rrc_contract_zone.mode_value_check` and
  `manual_value_check` (zone lock: error, standby, away mode).
- Program selection and switch points (`rrc_programs.RrcProgramService`):
  one PUT through `RrcStructWrite` on the RRC schedule, counted with
  `note_rrc_write_sent`; `selection_shows` and `edit_shows` let a later poll
  confirm it.
- Structures (events, boost): `coordinator.async_run_rrc_operation(path,
  operation, require_snapshot=..., late_check=...)` around
  `coordinator.rrc_struct_writes.async_write(preflight=..., send=...,
  confirm=..., guard=..., pending_reason=...,
  on_sent=lambda: coordinator.note_rrc_write_sent(path))` from
  `rrc_write.py`. `guard`
  blocks a second creation for 10 minutes while the first one's outcome is
  unknown (`RrcWritePending`). Only an unresolved outcome
  (`WriteNotConfirmed`, a cancellation) marks the path stale and reads it
  with the next cycle; a failed preflight or a refusal keeps the reading.
  `on_sent` runs once the request may have left, not for a connection that
  never opened.
- Client: `put_resource_json`, `post_resource` and `delete_resource` send once
  without retries; `get_resource_query(gateway, path, {"entry": n})` accepts
  only `entry`, `next` (integers) and `date` (`yyyy-MM-dd`).

Counters per contract (`writes_sent`, `writes_confirmed`,
`writes_unconfirmed`, `late_confirmed`, `writes_rejected`, rejection tokens)
appear in the diagnostics under `rrc_features`, with
`confirmation: "unconfirmed"`. RRC writes outside any contract count as
`core`.

## Entities

Each entity module defines `TRACKERS: Mapping[Platform, Tracker]`.
`rrc_platforms.track` calls the tracker of a platform once per coordinator
from the platform's setup: sensor, binary sensor, number, select, switch,
button, climate, calendar, water heater and time (the new platform for W3).
Use `control.track_rrc_entities(entry, coordinator, add, build, create)`:
`build(resources, context)` runs only on an RRC controller, and each
description key is added once.

Unique IDs: `<gateway_id>:<path without the leading slash, "/" → ":">[:<role>]`.
A structure path that gets an entity of its own (for example
`…:boostShortcut:switch`) must be listed in `understood`; otherwise the
cleanup of unknown structures removes the entity at the next start.

## Discovery

`extra_roots`, `optional_children` and `device_leaves` are read only on an
RRC controller. Device leaves are read for listed devices only, leaf by leaf
(the first leaf of every device, then the second, and so on), at most
`MAX_RRC_DEVICE_SETTING_PROBES` (192), after every other path. They have
this cap of their own instead of a share of the overall limit of 512 paths;
the rest is counted as `rrc_device_setting_limit` in the diagnostics. An
HTTP 5xx answer to such a probe means "probably missing" (CT-B06). An item
5xx in a bulk answer to an optional probe or a `zone_fields` leaf is taken
as missing without an individual read; the known catalog paths keep it.
That rule needs a cloud that answers: a batch of nothing but such probes
carries one path discovered before as its last path (a full batch leaves
one probe for the next); if it brought no resource at all, the 5xx answers
count towards the outage rule. A 5xx below an
`extra_roots` root never ends the discovery. The event entries
(`/events/evN`, `/events/activeEvent`, `rrc.is_unread_event_entry`) are
never read. `zone_fields` extend `rrc.ZONE_FIELDS`.

## Translations

Add new keys to `strings.json`, `translations/en.json` and
`translations/de.json`, and German and English built texts next to the code.
List the keys and English texts still missing in the other languages in
`tests/translation_pending/W<n>.json` (see the README there). W0 lists
`exceptions.rrc_write_pending_confirmation`, which every package uses.

## Tests

`tests/helpers/rrc_cloud.py` is a fake cloud for real setups: GET, bulk,
PUT, structured PUT, POST, DELETE and query reads, with late adoption
(`adopt_after`), 204 without effect (`ignore_writes`), a timeout after the
write, 429 with `Retry-After`, 403 per path and 500 instead of 404, and
counters per method and path. `tests/test_rrc_w0_baseline.py` pins the
entities and requests of existing plants; it must stay green.
