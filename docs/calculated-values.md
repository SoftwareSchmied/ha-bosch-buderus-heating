# Calculated values

Some values have no reading of their own on the appliance. The integration
works them out from readings it reads anyway, sends no extra request for
them and names each entity "(calculated)". Every one of them lists its
inputs in the attribute `derived_from`: a map from the reading's path to
`local` or `cloud`, the access the value came from. A reading that the local
access offers comes from there while it is current; otherwise the cloud
reading counts within its validity limit. An expired or missing reading is
never used, and a value whose inputs are missing stays unknown instead of
being guessed.

What has been checked and what has not:

- Activity, `hvac_action`, the defrost entities and the net heat counters
  were checked with automated tests and with a replay of readings recorded
  on a real air-to-water heat pump with one underfloor circuit (refrigerant
  circuit status, 3-way valve VW1, local circuit pump, cloud counters). They
  have not yet run live on a real installation.
- Boilers, iCom gateways, room controllers, several heat pumps and cooling
  follow the same rules but are untested.
- The heat source type follows readings seen on a real air-to-water heat
  pump and in the diagnostics of a real brine-to-water heat pump.

## Activity of a heating circuit and `hvac_action`

Each heating circuit gets the enum sensor **Activity (calculated)**
(`<gateway>:heatingCircuits:<hc>:calculated_activity`, enabled by default).
The climate entities of the heating circuits use the same rule for their
`hvac_action`; the zones of a room controller keep their own zone status and
show `defrosting` instead of `heating` while the heat pump of the plant
defrosts. Only the standard actions are used:

| Activity | When |
|---|---|
| `off` | The circuit's mode is off, or its heating/cooling status (`currentSuWiMode`) is off (summer). |
| `defrosting` | The heat pump defrosts (see below); only on a plant that defrosts at all. |
| `idle` | The heat source does not run, runs for hot water or the pool, or the circuit pump is off. |
| `heating` | The heat source runs for the heating side and the circuit pump runs. |
| `cooling` | As heating, while a cooling signal, the compressor status or the cooling season says cooling. |

The inputs in their order:

- running: the compressor signals (`SRC.CUHP.HMI.CompressorActive`,
  `SRC.CUHP.HP1.CompressorStatus`), the refrigerant circuit status, the
  compressor status, the heat source's status (`flameStatus`);
- side: the 3-way valve VW1 (`SRC.CUHP.HP1.DHWValve`, then the relay
  `SRC.RelayStatus.3WayValve`), the refrigerant circuit status, the
  compressor status, the central heating status of a boiler (`chStatus`);
- circuit pump: `pumpStatus` of the circuit, the pump relay
  `SRC.CUHP.CH.HC<n>PumpRelay`, the circuit's pump modulation.

Without a current reading of the circuit's mode the sensor is unavailable;
with the mode but without the inputs the rule needs, it is unknown and the
climate shows no `hvac_action`.

## Defrosts

Only a plant with evidence that it defrosts gets these entities: a heat pump
whose reported type is `air_water` (or `exhaust_air`), one with a defrost
signal or defrost flag, the air inlet temperature TL2 or the outdoor unit's
fan, or one whose status has reported a defrost. A brine-to-water heat pump
shares the controller platform and many signal names (4-way valve, drip pan
heater, crankcase heater) with the air units; it never gets them, and its
circuits never show `defrosting`.

Defrost sources in their order: the hybrid manager's defrost signal
(`HYBMAN.CAN.DefrostActive`), the local refrigerant circuit status
(`defrost` or `passive_defrost`), the cloud compressor status (`defrost`) and
each heat source's `defrostActive`.

| Entity | Type | Default |
|---|---|---|
| Defrost active (calculated) | Binary sensor, device class `running`; attributes `detected_by` and `derived_from` | enabled |
| Defrost (calculated) | Event `defrost_finished` | enabled |
| Last defrost (calculated) | Timestamp sensor: the start of the last counted defrost, the other event attributes as attributes | enabled |
| Number of defrosts (calculated) | Counter, `total_increasing` | disabled |
| Total defrost time (calculated) | Minutes, `total_increasing` | disabled |
| Heat withdrawn by defrosting (calculated) | kWh, `total_increasing` | disabled |
| Electricity during defrosting (calculated) | kWh, `total_increasing` | disabled |

The event `defrost_finished` carries:

| Attribute | Content |
|---|---|
| `started_at`, `ended_at`, `duration_min` | From the first reading that shows the defrost to the first one that no longer does |
| `stages`, `stage_minutes` | The positions of the 3-way valve VW1 during the defrost in their order (`heating`, `dhw`) and the minutes of each; empty without a valve reading |
| `heat_withdrawn_kwh` | The dip of the produced-heat counters (heating, hot water, pool; otherwise the total) below their value at the start |
| `electricity_kwh` | The change of the compressor and heater counters between start and end |
| `electricity_kwh_from_power` | The electrical power of the compressor and the electric heater, integrated over the defrost: linear between the readings (trapezoid), the first and last reading held up to the start and end; `null` without power readings or when two readings, or a reading and the start or end, are more than 2.5 minutes apart |
| `outdoor_temp_start`, `air_inlet_temp_start` | Outdoor temperature and TL2 at the start, if read |
| `detected_by`, `derived_from` | The defrost source and every input used |

Limits:

- Defrosts are counted only while Home Assistant runs. A defrost that is
  already running at a start, or whose source is unreadable for more than
  15 minutes, is not counted, because its start is unknown.
- The duration is good to about one polling interval (about one minute
  with the local access).
- The energy is approximate: the counters change only every few minutes
  and in 0.01 kWh steps. The event therefore comes up to 10 minutes after
  the end, once every heat counter has risen again or that time is over.
- There are two values for the electricity. `electricity_kwh` comes from
  the counters and has their resolution: a defrost of six minutes moves
  them by only a few hundredths of a kWh, and the change may fall partly
  before or after the defrost. `electricity_kwh_from_power` follows the
  power readings, which the local access reads about every minute; a short
  peak or dip between two readings is not seen. Without the local power
  readings it is `null`. The counters of the defrost totals use the
  counter-based value.
- There is no classification as a long or intensive defrost; the raw
  duration and energy are reported instead.

## Net heat counters

The produced-heat counters of a heat pump fall while it defrosts: the heat
taken back from the water. They therefore have the state class `total`. For
each produced-heat counter (total, heating, hot water, pool) there is a
**Net heat … (calculated)** counter with the state class `total_increasing`:
it holds during such a dip and counts again only above the highest reading
so far. Its attribute `highest_reading` shows that reading. A drop of more
than 10 kWh counts as a replaced or reset counter: the net counter keeps its
total and continues from the new reading. It starts at the device's value,
is saved and never falls after a restart. Disabled by default.

## Run time per start and supply above setpoint

- **Average run time per start, heating (calculated)**: the heating
  operating hours of a heat source divided by its heating starts, in
  minutes. Diagnostic, enabled.
- **Supply above setpoint (calculated)**: for the heat source, the supply
  temperature minus the current supply setpoint while the heat source runs
  for heating (not during hot water or a defrost); for a heating circuit
  with its own supply sensor, its supply temperature minus its supply
  setpoint signal while the circuit heats. In kelvin, diagnostic, enabled.
  On a real installation with a heat pump at its minimum output, the supply
  was measured a few kelvin above the setpoint in normal operation.

There is no "compressor at minimum output" flag: the threshold would rest on
no manufacturer statement, and the compressor speed is available as a
reading of its own.

## Heat source type

The **Heat generator** sensor has the attribute `heat_source_type`: `air`,
`brine` or `water`, separate from `construction`. The reported heat pump
type decides first. Without it only readings that exist on one kind count:
a defrost signal, defrost flag or reported defrost, TL2 or the outdoor unit's
fan for air; a reading of the brine circuit (collector temperatures, brine
pump, pressure, flow) for brine. Names of shared signals never count, and
contradicting evidence gives no type. `water` is known only from a reported
type. With such evidence but no module with a product name, the sensor is
created all the same, without a state and with `known: false`.

The diagnostics show the type and its evidence (`heat_source_type`,
`heat_source_type_evidence`) and, under `unnamed_modules`, the module rows
without a product name counted by hardware type, never with serial numbers.
