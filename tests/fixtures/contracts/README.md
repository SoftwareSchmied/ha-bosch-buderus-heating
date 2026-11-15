# Contract fixtures

These small fixtures exercise reviewed response shapes and semantic mappings.
Each JSON file states its provenance. `kind: synthetic` means manually
constructed data, even when the field layout is based on a compatibility
mapping. `physical_device_verified: false`, `firmware: null` and
`capture_date: null` must not be interpreted as a real installation capture.

| Folder | Examples |
|---|---|
| connectkey | Temperature, global and structured duration, structured energy, independent heating/cooling zone commands, read-only heating and hot-water switch programs (anonymized structure, constructed times); `switch-programs-ab.json`: a synthetic gateway with weekly programs A and B of a heating circuit and program A of the hot water, writable, with constructed times, levels and names; `variable-tariff-k40.json`: a synthetic K40 variant of the variable tariff with all 15 children, types from the official local interface description, constructed values; `cooling-switch-programs.json`, `zone-switch-programs.json`, `ventilation-switch-program.json`, `switch-programs-absolute.json`: further program kinds after the app's model classes (constructed values, never seen on a device); `switch-program-names.json`: program names with the app's codings |
| icom | Heating and DHW temperatures; `gateway-shapes.json`: a synthetic iCom gateway with a boiler controller (91 resources, the 403/404 sets, five holiday slots, hot water charge, faults only through `/notifications`, a transport profile and two marked, not observed `/system/info` variants), built from the shapes of a real diagnostic with constructed values; `season-circuit.json`, `temperature-levels.json`, `tbso.json`: synthetic variants of the summer/winter switching, the cooling setpoints, the temperature levels and the variable tariff of a heating circuit (shapes of that diagnostic and of the manufacturer's app, constructed values) |
| rrc | DHW and away-mode temperature, independent zn zones, program selection and existing weekly switch points; string `used` flags and a mode without advertised options (anonymized TC100.2 structure, constructed values); `ct200-shapes.json`: a synthetic RRC 2.0 controller with 11 zones, 12 radio devices and 24 programs, built from constructed values and the anonymized TC100.2 device tree, with marker values for the privacy tests; `day-programs.json`, `dhw-week-program.json`: constructed day and hot water week programs for adding/deleting points and the hot water program |
| wddw2 | Unitless hours, explicit minutes, starts, DHW temperature and collection; `switch-program.json`: a constructed weekly program after the app's model classes |
| water_softener | Remaining volume, flow, durations and regeneration phase |
| bacon-purifier | Two synthetic Air profiles with different offered capabilities; all seven family/filter mappings are exercised by tests |
| captures | Anonymized discovery results of real systems with their firmware, each with a pinned `.expected.json` of the entities built from it (`tests/test_captures.py`) |
| bacon-dry | Two synthetic Dry capability profiles with different humidity ranges and modes; all three families and six prefixes are exercised by tests |

`readings.json` contains payloads and independently stated expected HA values
and units. Tests use the actual parser and sensor. `monitor-collection.json`
also records unverified envelope and child-read behavior. A family example
does not establish all models, units or firmware versions in that family.

For new synthetic cases, preserve the provenance fields and state the contract
being tested. For a future physical fixture, use `kind: anonymized_capture`,
record the relevant model, firmware, capture date and test scope, and remove
tokens, serial numbers, account details, location, network identifiers and
user-entered names before committing. Separate observed payloads from edited
or synthetic variants. Never relabel a synthetic response as a capture.

The full inventory and remaining evidence needs are described in
[resource-contracts.md](../../../docs/resource-contracts.md).
