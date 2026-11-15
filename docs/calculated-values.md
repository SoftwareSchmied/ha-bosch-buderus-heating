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
- The current COP and the performance factor of a day were checked with
  automated tests and a replay of one day recorded on a real air-to-water
  heat pump (see below). The other periods, the back-fill from the plant
  recordings and the counters' statistics, the spreads and brine-to-water
  heat pumps are tested with automated tests only; none of them has run
  live on a real installation yet.

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
  was measured a few kelvin above the setpoint in normal operation; see
  [why the supply is above the setpoint](heating-controls.md#why-is-the-supply-temperature-above-the-setpoint).

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

## Current COP, performance factors and spreads

| Entity | Content | Default |
|---|---|---|
| Current COP (calculated) | Heat output divided by the electrical power, while the compressor runs | enabled |
| Total – Performance factor today (calculated) | Heat divided by electricity since midnight | enabled |
| Total – Performance factor this year (calculated) | The same since 1 January: the seasonal performance factor of the calendar year | enabled |
| Total – Performance factor since commissioning (calculated) | The device's counters as they are | enabled |
| Total – Performance factor yesterday / this month / previous month / previous year (calculated) | As above for that period | disabled |
| Heating – … and Hot water – … (calculated) | Every period per mode | disabled |
| Primary circuit spread (calculated) | Supply TC1 minus return TC0 of the heat pump, in kelvin | enabled |
| Brine spread (calculated) | Brine inlet minus brine outlet, in kelvin; only on heat pumps with both brine temperatures | disabled |

The ratios have no unit and the state class `measurement`. A disabled
performance factor is worked out in the background all the same, so it shows
its value as soon as it is enabled. The entities exist only where their
readings exist: the current COP needs the heat output and the compressor
power, the performance factors need a counter with heat and electricity.

### Current COP

The heat output is the thermal output of the refrigerant circuit
(`SRC.CUHP.HP1.CompressorPower`, a reading of the local access; on a real
installation it matches the heat meter). The electrical power is the
compressor's (`compressor/powerElecActual`, else
`hs1/refrigerant/compressorElecPowerActual`) plus the electric heater's
(`eHeater/powerElecActual`, else its coarse reading). The heater turns its
electricity into heat one to one, so its power is added to the heat output
as well.

The COP is shown only while the compressor runs (the same running rule as
the activity above). It is unknown, with the attribute `reason`:

- `defrosting` while the heat pump defrosts;
- `compressor_off` while the compressor stands;
- `inputs_missing` without a current heat output, compressor or heater
  reading (a plant without any heater reading adds nothing for the
  heater), or below 0.1 kW electrical power (start and stop);
- `implausible` above 15, when readings taken at different moments do not
  fit together.

The attributes show the heat output, the compressor, heater and total
electrical power in kW, `side` (`circuit` or `dhw`) and `derived_from`. On a
brine-to-water heat pump `brine_pump_included: false` says that the brine
pump's electricity is not part of it. Without the local access there is no
heat output reading and therefore no current COP.

### Performance factors

A performance factor is the produced heat of a period divided by the
electricity (compressor plus heater) of the same period, for heating, hot
water and in total. The counters' change includes the dip while the heat pump
defrosts, so the heat taken back from the water is netted.

| Period | Heat and electricity from |
|---|---|
| Today | The counters now minus the counters at midnight |
| Earlier days | The plant recordings for the electricity and the total heat, the counters' long-term statistics for the heat per mode; each falls back to the other |
| Since commissioning | The device's counters |

- The counters at midnight are the last readings before midnight while Home
  Assistant runs (at most 30 minutes before the first reading of the day).
  After a start during the day they are the counters' long-term statistic
  in the hour before midnight. Without either, today stays unknown until
  the next midnight. The start of the day is saved, so a restart keeps it.
- The heat per mode comes from the counters first because the plant gives
  an hour in which it switches between heating and hot water to both modes.
  A day the counters' statistics cover only partly (the first day of a new
  counter) never counts.
- With [plant recordings](recordings.md) the history goes back to the first
  recorded day, also on a new installation. Only days the recordings have
  confirmed count; until then (yesterday shortly after midnight) the
  counters stand in. A month whose days are not all
  known takes the plant's month value (whole kWh on some plants). Without
  recordings the counters' long-term statistics are used where Home
  Assistant has them.
- Below 0.1 kWh electricity or above 15 a factor is unknown.

Attributes: `period_start`, `period_end` (the last day), `heat_kwh`,
`electricity_kwh`, `heat_source` and `electricity_source` (`counters`,
`local_recording`, `cloud_recording`, `local_and_cloud_recording`, joined by
`+` when the days came from several), `days` (days with a value),
`missing_days` (days of the period without a value, today included while
its start is unknown), `first_day` (the first day of the history when it
lies within the period) and `derived_from` (the counter collection with
`local` or `cloud`, the recorded paths with `recording`).

A replay of one day recorded on a real air-to-water heat pump (heat output,
compressor and heater power and the counters as Home Assistant recorded
them) checks the two methods against each other: the integrated heat output
and electrical power agree with the counters' increments of the day within
3 % (about +1 % for the heat and −0.5 % for the electricity), and so does
their ratio.

### Spreads

- **Primary circuit spread:** supply TC1 (`additionalHeater/flowTemp`, else
  the signal `SRC.IntFlowTemp`) minus return TC0 (`returnTemperature`, else
  the signal `SRC.CUHP.HP1.ReturnTemp`), only while the primary pump runs
  (its relay, else its speed or flow above zero; otherwise `reason:
  pump_off`). During a defrost the spread is negative: heat flows back into
  the heat pump. TC0 is the return of the heating circuit only while the
  circuit pump PC1 moves at least as much water as the primary pump PC0;
  with more flow in PC0 the supply mixes into the return through the buffer.
  The attribute `return_is_circuit_return` says which case applies (`null`
  without both flows); `primary_flow` and `circuit_flow` show the flows.
- **Brine spread:** brine inlet (`brineCircuit/collectorOutflowTemp`) minus
  brine outlet (`brineCircuit/collectorInflowTemp`), while the brine pump
  runs, without a brine pump reading while the compressor runs. Disabled by
  default.
