# ADR 0006: Plant model

- Status: Accepted
- Date: 2026-11-22

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
  (observed); type 240 has the flag `hmi800_hp_light` (rule).
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
equals an identifier of the same row. A product name that holds a serial
number or only digits is left out. Hardware type, update type, software
identifier (`SwIdenStr`) and article number of the model (`ProductTtn`,
else `ModuleTtn`) are kept only as safe texts. `No`,
`ProductSerialNumber`, `ModuleSerialNumber` and `ProductUuid` are never
read. At most 64 rows of an answer are read. The holiday rule of iCom
gateways keeps its own parser.

### Plant kind

- `plant_kind`: `heat_pump`, `hybrid`, `boiler`, `ventilation`, `none` or
  `None`; `fuel`: `gas`, `oil`, `unknown` or `None`; `cascade`: bool or
  `None`. `None` means: behave as before the plant model.
- `hybrid` is the effective kind: a plant is hybrid exactly when the hybrid
  evidence the integration used before or a new piece of evidence says so.
  New evidence: the system type `eHybrid`, a heat pump type and a boiler
  type in the same plant, the outdoor status `CONNECTED`, a hybrid manager at
  the hybrid place. `eHybrid`, `hybman` and `hybridBoiler` stay hybrid;
  `eBoiler` and unknown values do not count.
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
  rule. Type 172 never decides anything.
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
the stored row and shown in the diagnostics. They never appear in entity
attributes or in the log. Serial numbers, UUIDs, the `No` field, gateway IDs
and entry IDs never appear anywhere.

### Diagnostics

Schema 49 adds `gateways[].plant` and `views_without_cloud[].plant`
(`docs/schemas/diagnostics-plant.v1.json`), for every heating gateway
including iCom gateways and room controllers (with their full rows), not for
water softeners, air conditioners or wallboxes. The iCom block
`icom.system_components` stays. The anonymized inventory keeps only
`plant_kind`. A placeholder for optional component devices is not part of
schema 49; its stage adds its own field.

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

## Consequences

- The plant kind is visible and stored, but in this stage it changes no
  entity, name, control or request.
- Later stages read the plant kind and the components through this
  interface only and treat `None` as "behave as before".
- A new rule or role needs a code change with tests, or a table entry with a
  basis other than `name`; a new name never changes behaviour.
