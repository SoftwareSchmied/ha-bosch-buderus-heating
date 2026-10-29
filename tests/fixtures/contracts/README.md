# Contract fixtures

These small fixtures exercise reviewed response shapes and semantic mappings.
Each JSON file states its provenance. `kind: synthetic` means manually
constructed data, even when the field layout is based on a compatibility
mapping. `physical_device_verified: false`, `firmware: null` and
`capture_date: null` must not be interpreted as a real installation capture.

| Folder | Examples |
|---|---|
| connectkey | Temperature, global and structured duration, structured energy, independent heating/cooling zone commands, read-only heating and hot-water switch programs (anonymized structure, constructed times) |
| icom | Heating and DHW temperatures |
| rrc | DHW and away-mode temperature, independent zn zones, program selection and existing weekly switch points |
| wddw2 | Unitless hours, explicit minutes, starts, DHW temperature and collection |
| water_softener | Remaining volume, flow, durations and regeneration phase |
| bacon-purifier | Two synthetic Air profiles with different offered capabilities; all seven family/filter mappings are exercised by tests |
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
