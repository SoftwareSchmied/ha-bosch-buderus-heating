# Anonymized installation inventory

An anonymized inventory helps extend support to other Bosch and Buderus
installations without sharing access to a heating account or system. The tool
works exclusively from an already redacted Home Assistant diagnostics report
and does not make a cloud request.

Diagnostics schema 18 is supported. Duration and collection decisions remain
in the full diagnostics; the smaller inventory retains only its field allowlist.

## 1. Download diagnostics

1. Open **Settings → Devices & services**.
2. Open **Bosch/Buderus Home Comfort**.
3. Open the three-dot menu.
4. Select **Download diagnostics**.
5. Review the file yourself before sharing it.

## 2. Create the smaller inventory

Run this command from a checked-out project directory:

```bash
python -m custom_components.bosch_buderus_heating.inventory \
  config_entry-bosch_buderus_heating.json \
  pointt-inventory.json
```

An existing output file is not overwritten. Add `--force` only when replacing
it is intentional.

## Contents

The inventory contains only:

- an anonymous gateway class such as `k40` or `mx400`;
- path templates containing `{hc}`, `{dhw}`, and `{hs}`;
- data types, units, polling groups, and maturity levels;
- writeability and the presence of limits or selectable values;
- aggregated counts per capability class;
- the plant kind of each gateway (`plant_kind`, schema 49), never its
  components, versions or type values.

It discards raw values, runtime and request metrics, discovery details, concrete
diagnostic paths, display names, gateway and
config-entry IDs, serial numbers, firmware strings, tokens, network data, and
user-defined names. The tool aborts unless the report explicitly declares all
privacy-sensitive fields redacted and the exported heating-circuit, hot-water,
and heat-source path templates contain no concrete installation identifier.

Schema 14 may explicitly declare `allowlisted_technical_metadata` as its only
raw-value scope. The exporter accepts this scoped declaration and discards
the entire technical-metadata section. Its output still declares no raw
resource values. Unrestricted raw-value declarations remain rejected.

## Firmware warning

After every complete discovery, the integration compares known PointT paths
with their safe data types and units. A new firmware version alone does not
create a warning. Only an actual schema conflict creates the **PointT
capabilities changed after a firmware update** repair. The repair reloads the
integration and checks every capability again. If the warning remains, an
anonymized inventory is the appropriate basis for an issue report.
