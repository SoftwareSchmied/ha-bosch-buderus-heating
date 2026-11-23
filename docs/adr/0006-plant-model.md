# ADR 0006: Plant model

- Status: Accepted
- Date: 2026-11-22
- Amended: 2026-11-23 (later stages of 0.9.3-beta.6)

## Context

The component list of a system (`/system/info` of the cloud,
`/system/basicInfo` of the local access) names each component by a type
number (`Id`) and a place number (`Tok`). Until 0.9.3-beta.5 only the
product name and the hardware type were used, and the kind of a plant was
decided in several places from different evidence. Diagnostics showed
neither the type numbers nor the reported type values, so a plant of another
kind could not be checked.

## Decision

One pure resolver works out the plant kind of a gateway from what the plant
reports. Its inputs, its result and its output form are fixed as follows.

### Component table

- `system_components.json` names component types (`Id`) and places (`Tok`)
  and holds roles per type and place. Each entry has a basis: `name` (it only
  names), `rule` (a fixed rule of the plant uses it), `observed` (seen in a
  real module list with the role proven by the same plant) or `documented`
  (manufacturer documentation). Behaviour comes only from roles with the
  basis `rule`, `observed` or `documented`, never from a name. Conditions
  such as firmware or version limits are code, not table data.
- Place keys: `heat_generator_control` (8), `basic_controller` (9),
  `system_controller` (16), `pool` (17), `remote_hc1_legacy` to
  `remote_hc4_legacy` (24-27), `fresh_water` (42), `solar` (48),
  `remote_hc1` to `remote_hc4` (56-59), `gateway` (72), `gateway_service`
  (75), `radio` (80), `ventilation` (81), `hybrid_manager` (83).
- Roles at the start: 248 and 16 at place 83 `hybrid_manager` (rule); 8 at
  place 8 `heat_source` and 65281 at place 8 `outdoor_unit` (observed); 253 at
  place 16 `system_controller` (observed); 17 at places 72 and 75 `gateway`
  (observed); type 240 has the flag `hmi800_hp_light` (rule). Table
  version 2 adds two roles seen on an iCom gateway: 172 at place 8
  `heat_source` and 158 at place 16 `system_controller` (observed).
- A row of the gateway itself is recognized by place 72 or 75 or by type 17,
  never by a name.
- A faulty or missing table is rejected as a whole with one warning; the
  integration keeps working without table behaviour.

### Component rows

`ComponentRow(type_id, tok, version, product_name, hardware_type,
sw_update_type, sw_ident, article_number)`; every field is optional and
checked. Type and place numbers have 1 to 6 digits (a boolean, a fraction, an
empty or negative value, or more digits is no number). A version follows the
rule of the software version of the outdoor unit and is left out when it
equals an identifier of the same row; a version with a control or other
unprintable character, or longer than 64 characters, is dropped as a whole. A product name that holds a serial
number or only digits is left out. Hardware type, update type, software
identifier (`SwIdenStr`) and article number of the model (`ProductTtn`,
else `ModuleTtn`) are kept only as safe texts. `No`,
`ProductSerialNumber`, `ModuleSerialNumber` and `ProductUuid` are only
compared to leave out a text equal to them; they are never kept or shown.
At most 64 rows of an answer are read. The holiday rule of iCom
gateways keeps its own parser.

### Plant kind

- `plant_kind`: `heat_pump`, `hybrid`, `boiler`, `ventilation`, `none` or
  `None`; `fuel`: `gas`, `oil`, `unknown` or `None`; `cascade`: bool or
  `None`. `None` means: behave as before the plant model.
- `hybrid` is the effective kind: a plant is hybrid exactly when the hybrid
  evidence the integration used before or a new piece of evidence says so.
  New evidence: a heat source type of a hybrid system (`Hybrid`,
  `Hybrid_Gas`, `Hybrid_Oil`), a heat pump type and a boiler type in the
  same plant, the outdoor status `CONNECTED`, a hybrid manager at the hybrid
  place. `hybman` and `hybridBoiler` stay hybrid; `eBoiler` and unknown
  values do not count.
- `eHybrid` is a heat pump with an additional heater, not a hybrid system:
  it is no hybrid evidence (neither in the model nor in the evidence used
  before, nor for the targeted reads), and the heat source types decide
  (`hs1` = `Heatpump` gives `heat_pump`).
- Heat source types: `Heatpump`, `GasBoiler`, `OilBoiler`, `unknownBoiler`,
  `No_Appliance`, and the values of `/heatSources/type` of a room controller
  `Boiler` (boiler, fuel not stated), `Hybrid`, `Hybrid_Gas`, `Hybrid_Oil`
  (hybrid system, fuel not stated, gas or oil) and `No_Applicance` (no
  appliance; written so by the devices). Other values such as `eBoiler`
  stay unknown.
- Order: a bus still detecting gives no result (the known kind is kept);
  then hybrid evidence; then the heat source types, then the system type (a
  contradiction gives `None`); heat sources without an appliance give
  `ventilation` with a Modbus bus or a component at place 81, else `none`.
- Hybrid manager at the hybrid place: only the first row at place 83 counts,
  with type 248 from version 06.01 on or type 16; only on K30 gateways (K30,
  K30RF, MX300) from firmware 08.02.02 and K40 gateways (K40, K40RF, MX400)
  from firmware 11.00.00 on. Versions compare by the part before the first
  `-`, split at dots, a part that is not a number counts as 0, a missing
  version never passes. Other gateways (iCom, room controllers) show the row
  only. A boiler type with the outdoor status `NOT_CONNECTED` overrules the
  rule. A role never decides the plant kind: type 172 at place 8 only shows
  a contradiction to a boiler type.
- Evidence codes: `reported_system_type`, `reported_heat_source_type`,
  `reported_heat_pump_type`, `reported_outdoor_status`,
  `hybrid_manager_component`, `heat_source_types_combined`,
  `hybrid_resources`, `hybrid_manager_signals`, `stored`.
- Conflict codes (diagnostics only): `system_type_vs_heat_source_type`,
  `components_vs_outdoor_status`, `heat_pump_component_on_boiler_plant`,
  `hybrid_name_rule_only`, `cloud_local_components_differ`,
  `unknown_type_value`, `component_rule_skipped`.
- `MODEL_VERSION` (now 1) is the version of these rules; a stored result of
  another version is worked out again from its stored inputs.

### Inputs, stored row and failures

- Inputs come from the cloud's data and resources, including the values the
  discovery inventory keeps, and from the newest local reading, current or
  not. The current readings of the calculated values (only fresh ones) are
  not used, so an outage never changes an input.
- One row `<gateway>:plant` in the saved calculated values
  (`.storage/bosch_buderus_heating.calculated_values`), format `v: 1`
  (`docs/schemas/plant-store-row.v1.json`): `model_version`,
  `table_version`, `sources.cloud` and `sources.local` (rows
  `[tok, id, version, product_name, hardware_type, sw_update_type, sw_ident,
  article_number]`, at most 64, texts at most 64 characters, with
  `changed_at`), `shown`, `components` (`first_seen` per place and type,
  `removed_at` kept for 30 days), `inputs` (raw type values as safe tokens,
  `allowed_values` at most 16 each, the hybrid evidence used before, gateway
  class, firmware and platform), `gates`, `kind`, `fuel`, `cascade`,
  `evidence`, `conflicts`, `changes`, `probes`, `updated_at`.
- The row is read before the first result is worked out and written only
  when its content changes; `updated_at` changes only with the content.
- An input that is missing now is taken from the row; only a new reading
  that says something else changes the result (logged once at the info level
  with the old and new kind only, counted in `changes`).
- The row is deleted with its gateway (`async_forget_gateways`). A gateway
  exchange does not move it: the new gateway gets its own row after its
  first reading.
- The component list shown follows the heat generator sensor: one current
  answer alone is shown; when both agree (the gateway's own rows are not
  compared) the source shown last stays and a change of source never shows
  fewer rows; when they disagree, the answer that matches the one shown last
  stays, else the newer one. Both lists are kept.

### Privacy

Versions, software identifiers and article numbers of the model are kept in
the stored row and shown in the diagnostics. They never appear in the
attributes of the heat generator sensor or in the log. The software version
of a component device is shown by the device registry while the optional
component devices are on; the existing diagnostic sensor Outdoor unit
software version (since 0.9.2) keeps showing the version of the outdoor
unit. Serial numbers, UUIDs and the `No` field of a component row are never
part of states, attributes, diagnostics, stored values or the log; the
gateway's own device shows its serial number as before. The key of the
stored row and the identifier of a component device begin with the gateway
ID like every key of the integration.

### Diagnostics

Schema 49 adds `gateways[].plant` and `views_without_cloud[].plant`
(`docs/schemas/diagnostics-plant.v1.json`), for every heating gateway
including iCom gateways and room controllers (with their full rows), not for
water softeners, air conditioners or wallboxes. The iCom block
`icom.system_components` stays. The anonymized inventory keeps only
`plant_kind`. The optional component devices add the field
`component_devices` (`enabled`, `count`) to the block within schema 49.

### Interface for later stages

`plant_inputs` offers:

- `track_plant(entry, coordinator)`: starts the model of a gateway (done in
  the sensor platform setup); reads the row first.
- `current_plant(coordinator) -> PlantResult | None`: the result of the
  current update pass (worked out once per pass); `None` before the row is
  read or without a result. `PlantResult` has `plant_kind`, `fuel`,
  `cascade`, `evidence`, `conflicts`, `gates`, `model_version` and
  `hybrid`.
- `components(coordinator) -> ShownComponents | None`: the rows shown, their
  `source` (`cloud`, `local` or `None`), `restored` and the
  `table_version`.
- `describe(row, table=None) -> ComponentDescription`: place key, place
  name, type name, role (only from entries whose basis is not a name), basis
  and whether it is a row of the gateway.
- `set_probe_results(coordinator, results) -> bool`: keeps the results of
  targeted reads in the row (at most 48 entries of safe texts, numbers and
  flags).
- `plant_diagnostics(coordinator)`: the diagnostics block.

## Later stages (0.9.3-beta.6)

The stages after the model use only the interface above and treat `None`
as "behave as before".

### Heat generator sensor (contract 1)

- The attributes of the heat generator sensor are a versioned interface for
  cards (`contract_version: 1`, `docs/schemas/heat-generator-sensor.v1.json`):
  every key is always present; the keys of 0.9.1 to 0.9.3-beta.5 keep their
  meaning; new are `state_basis` (`product_name` or `component_type`),
  `plant_kind`, `plant_kind_evidence`, `fuel`, `cascade`, `components`
  (at most 32: place, place number, type number, type name, role, basis and
  product name, without the gateway's own rows, rows without a valid type,
  versions and serial numbers), `components_source` and
  `components_table_version`.
- `stored` stays in the diagnostics and is never part of
  `plant_kind_evidence`, so a restart does not change the attribute.
  `components` is not recorded in the history. The restore data keep the
  form of 0.9.3-beta.5.
- Without a module with a product name the state is the name of the
  component at the heat generator's place (a component with a role first,
  then the smallest type number). The entity is created for a named module,
  evidence of the heat source kind, a component at the heat generator's
  place or the plant kind `heat_pump`, `hybrid` or `boiler`; never for a
  room controller.
- The sensor reads the stored row before it writes its first state.
- The evidence `hybrid_manager_component` and `heat_source_types_combined`
  also counts for the hybrid detection of the burner share and of the
  hybrid diagnostics; it only adds to the evidence used before.
- A module that the model table gives no role (for example one without a
  hardware type) gets the role the component table gives its type number at
  its place, only from an entry whose basis is not a name. Type number and
  place are not part of the restore data; the stored row gives them back.

### Targeted reads (`module_probes`)

- Fixed rules R1 to R6 on the component rows and a few reported values plan
  reads of areas the gateway does not list: the heat pump at `hs2` of a
  hybrid system (never from `eHybrid`), pool, ventilation and solar by their places, the operation
  mode of `dhw1` and `dhw2` without a listed hot water circuit (and the
  known children of a circuit that answers), the heat pumps `hs1` to `hsN`
  of a cascade. Only places and roles with a basis other than `name` count.
- `/system/info` is read in the first batch of the discovery; its later
  place stays empty, so every other path keeps its batch. The targeted reads
  come after every other path in batches of their own, at most 48 per
  discovery (`module_probe_limit`), never a path of the list of paths that
  are never read. They are off on iCom gateways, room controllers, water
  softeners and in the search for missing parts.
- An error of a targeted read (a series of server errors, 429, a timeout)
  ends only the targeted reads; the discovery stays complete and no reading
  becomes unavailable.
- The results of the last run are kept in the stored row (`probes`, per path
  rule, result class and time, written only when they change). They are
  shown in the diagnostics and never skip a read.

### Recordings

- R7: an iCom gateway whose plant kind is `heat_pump` reads the energy
  monitoring of the total (heat produced, compressor, electric heater)
  instead of the boiler series; the solar yield of `sc1` is read for the
  diagnostics only.
- Option B: a plant of the kind `hybrid` whose heat pump reports no counters
  of its own reads the heat pump's recordings at `hs2`
  (`heat_pump_source_basis: plant_kind`); a heat pump with counters keeps
  its heat source (`counters`). Never on an iCom gateway.

### Component devices (`component_devices`)

- The option `component_devices` (off by default, no change of the entry
  version) adds one device per place and type number of the component list
  shown, identified by `(DOMAIN, "<gateway>:component:<place>:<type>")`,
  connected through the gateway's device, without entities. Model: the type
  name; model ID: the type number; software version: the whole version text
  of the list (none when two components of one kind report different ones);
  never a serial number. The gateway's own rows and rows without a valid type
  get no device.
- A view from local readings alone never changes the registry. Switching
  the option off removes these devices; a gateway without a stored system
  loses them at setup (also after a gateway exchange).
- The device diagnostics of a component device add the block `component`.

## Consequences

- The plant model alone (the first stage) changed no entity, name, control
  or request; the later stages above use it for the heat generator sensor,
  the hybrid detection, targeted reads, recordings and the optional
  component devices, and change no name, control or unique ID.
- Later stages read the plant kind and the components through this
  interface only and treat `None` as "behave as before".
- A new rule or role needs a code change with tests, or a table entry with a
  basis other than `name`; a new name never changes behaviour.
