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

- running: the compressor (its signals `SRC.CUHP.HMI.CompressorActive`,
  `SRC.CUHP.HP1.CompressorStatus`, the refrigerant circuit status, the
  compressor status) or the heat source's status (`flameStatus`, a
  burner): running when either runs, stopped only when every one with a
  reading stands;
- side: the 3-way valve VW1 (`SRC.CUHP.HP1.DHWValve`, then the relay
  `SRC.RelayStatus.3WayValve`), the refrigerant circuit status, the
  compressor status, the central heating status of a boiler (`chStatus`);
- circuit pump: `pumpStatus` of the circuit, the pump relay
  `SRC.CUHP.CH.HC<n>PumpRelay`, the circuit's pump modulation.

Without a current reading of the circuit's mode the sensor is unavailable;
with the mode but without the inputs the rule needs, it is unknown and the
climate shows no `hvac_action`.

The sensor is created only for a heating circuit for which the plant offers
a pump value (`pumpStatus`, the pump relay signal or `pumpModulation`, cloud
or local). Without one, the activity is unknown whenever the heat source runs
for heating, and the entity would only switch between unknown and idle at
every burner start. A sensor already in the registry stays; the climate keeps
its `hvac_action` rule (no `hvac_action` while it cannot be told).

Signals do not arrive at the same moment. At the end of a hot water charge
the valve can already be back on the heating side while the refrigerant
circuit still reports hot water or has stopped; contradicting readings count
as no heat for the circuit (`idle`), so the activity does not show `heating`
for the minute in between.

## Defrosts

Only a plant with evidence that it defrosts gets these entities: a heat pump
whose reported type is `air_water` (or `exhaustAir_water`), one with a defrost
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
- A single counter reading far off (for example a collection read as 0) is
  ignored: a heat reading more than 3 kWh below its value at the start is
  no part of the dip, and with an electricity change outside 0 to 2 kWh
  `electricity_kwh` is `null`. A defrost source that falls back to an older
  reading (the cloud's, read before, while the local access drops out) is
  treated as missing, so it neither ends nor starts a defrost.

## Net heat counters

The produced-heat counters of a heat pump fall while it defrosts: the heat
taken back from the water. They therefore have the state class `total`. For
each produced-heat counter (total, heating, hot water, pool) there is a
**Net heat … (calculated)** counter with the state class `total_increasing`:
it holds during such a dip and counts again only above the highest reading
so far. Its attribute `highest_reading` shows that reading. A drop of more
than 10 kWh counts as a replaced or reset counter once the next reading
confirms it; a single low reading (for example a collection read as 0)
counts nothing. The net counter then keeps its total and continues from the
new readings. It starts at the device's value,
is saved and never falls after a restart. Disabled by default.

## Run time per start and supply above setpoint

- **Average run time per start, heating (calculated)**: the heating
  operating hours of a heat source divided by its heating starts, in
  minutes. Diagnostic, enabled.
- **Supply above setpoint (calculated)**: for the heat source, the heat
  pump's supply after the heater (TC1; where the plant does not report it,
  the reported supply temperature, which can sit behind a buffer) minus the
  current supply setpoint while the heat source runs
  for heating (not during hot water or a defrost); for a heating circuit
  with its own supply sensor, its supply temperature minus its supply
  setpoint signal while the circuit heats. In kelvin, diagnostic, enabled.
  While the inputs are current but the heat source does not heat the circuit
  (standstill, hot water, defrost) the value is unknown, like the COP and
  the spread; it is unavailable only when the supply temperature or its
  setpoint is missing.
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
contradicting evidence gives no type. The gateway reports the type as
`air_water` or `exhaustAir_water` (both `air`) or `liquid_water`, which
covers brine and water circuits and shows as `brine`; so `water` does not
come from the gateway's reported type today. With such evidence but no module with a product name, the sensor is
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
| Total – Performance factor yesterday / this month / previous month / previous year (calculated) | As above for that period | enabled on a new installation; an existing entity keeps its state |
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
- `starting` in the first five minutes after the compressor started. The
  heat output rises more slowly than the electrical power then, so a COP
  would swing far too high (up to 13.8 was seen) or read zero; it would
  also distort the long-term statistics. A compressor that is already
  running when Home Assistant starts counts as running: its start is
  unknown, so its COP is shown at once;
- `no_heat_output` while the compressor draws power but no heat flows (the
  end of a run): there is never a COP of 0;
- `inputs_missing` without a current heat output, compressor or heater
  reading (a plant without any heater reading adds nothing for the
  heater), or below 0.1 kW electrical power (start and stop);
- `cooling` while the heat pump cools (its own cooling signals, or the
  cooling season while it serves the heating circuits; hot water in the
  cooling season is still a heating COP). A cooling run is not a heating
  COP: its heat output is zero or the cooling capacity, so the quotient would
  be an EER and mix into the statistics of the heating COP;
- `implausible` above 10, when readings taken at different moments do not
  fit together (a heat pump does not reach that);
- `implausible_low` when the COP stays below 1 for ten minutes without a
  break while the compressor runs. No heat pump delivers less heat than it
  takes in electricity for that long; the heat output reading is then more
  likely an electrical power, whose meaning differs between models and
  firmware versions. A shorter dip below 1 is shown as it is. The ten
  minutes start over after every value at or above 1 and after every
  unknown value.

An unknown COP is not recorded in the long-term statistics. The limits (1
for ten minutes, 10) are conservative for a heat pump; the period ratios
below keep their own limit of 15.

The attributes show the heat output, the compressor, heater and total
electrical power in kW, `side` (`circuit` or `dhw`) and `derived_from`. On a
brine-to-water heat pump `brine_pump_included: false` says that the brine
pump's electricity is not part of it. Without the local access there is no
heat output reading and therefore no current COP.

### Performance factors

Each factor is unknown while the period holds less than 0.1 kWh of
electricity, for instance for the first minutes after midnight. Its
attribute `reason` then says `too_little_electricity`.

After a start of Home Assistant, the factors of yesterday, of the months
and of the years stay unknown (`reason: history_loading`) until the plant
recordings and the counters' statistics were read once, about two minutes
after the start. Before, such a period would hold only today and show the
value of today. Today and since commissioning do not wait for it.

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
  Assistant runs (at most 30 minutes before the first reading of the day),
  also when the counters did not change. A single reading more than 10 %
  below the one before is ignored until the next reading confirms it.
  After a start during the day they are the counters' long-term statistic
  in the hour before midnight. Without either, today stays unknown until
  the next midnight. The start of the day is saved, so a restart keeps it.
- The heat per mode comes from the counters first because the plant gives
  an hour in which it switches between heating and hot water to both modes.
  The manufacturer's app shows the recorded heat per mode, so its heating
  and hot water values (and factors) can be higher than these. A day the counters' statistics cover only partly (the first day of a new
  counter) never counts. Home Assistant off for a whole day puts that day's
  energy into the counters' change of the next day; where the recordings
  fill the day itself, the counters of the days next to it do not count
  either, so nothing is counted twice.
- With [plant recordings](recordings.md) the history goes back to the first
  recorded day, also on a new installation. Only days the recordings have
  confirmed count; until then (yesterday shortly after midnight) the
  counters stand in. A month whose days are not all
  known takes the plant's month value (whole kWh on some plants). Without
  recordings the counters' long-term statistics are used where Home
  Assistant has them.
- Below 0.1 kWh electricity or above 15 a factor is unknown.
- **Burner share:** a factor divides heat by the electricity of the heat
  pump. If the plant reports burner energy above zero in the counter
  collection, or a hybrid system is detected, it is not known which share of
  the produced heat comes from the boiler, so the factor is unknown with the
  attribute `reason: burner_share` instead of a value that may be too high.
  The counters stay in the attributes. A hybrid system is detected by the
  signals of a hybrid manager, the resources of the hybrid control, a system
  or heat source type that names a hybrid system, and since 0.9.3-beta.6 by
  the [plant kind](system-components.md#plant-kind): a hybrid manager at the
  hybrid place of the component list (type 248 from version 06.01 on or
  type 16, on a K30 gateway from firmware 08.02.02 or a K40 gateway from
  firmware 11.00.00 on), or one heat source that reports a heat pump next to
  one that reports a boiler. These two only add to the evidence; a component
  name never counts, and until the plant model's stored row is read after a
  start the evidence is the one used before. The system type `eHybrid` is a
  heat pump with an additional heater and no evidence of a hybrid system.
  Such a hybrid system without a
  burner counter gets the factors of its heat pump when the heat pump reports
  counters of its own (see below); the recordings of `hs2` are read for a
  hybrid system without them as well (see
  [plant recordings](recordings.md#the-heat-pump-of-a-hybrid-system)).
  Hybrid system through a hybrid manager component: tested with synthetic
  component lists; not yet confirmed on a real hybrid system with a hybrid
  manager (#138). The rule looks at the burner counter of the collection
  (since commissioning), not at a single period: once a plant has reported
  burner energy, its factors stay unknown. A heat pump without a burner
  share is not affected.
- **Hybrid systems with counters of the heat pump:** the counters of the
  gateway (`/heatSources/emon/...`) count the boiler's heat. If the heat pump
  reports its own counters under its heat source (for example
  `/heatSources/hs2/emon/totalConsumption`, with `outputProduced` and its
  electricity, and the same for heating and hot water), the factors of all
  periods (today, yesterday, month, year, since commissioning) and the
  environmental energy are calculated from those counters alone: heat of the
  heat pump over electricity of the heat pump. The heat of the boiler is
  never part of it. The attribute `basis: heat_pump_counters` says so, and
  `derived_from` names the heat pump's collection. The heat pump's
  electricity is its `electricity` part, else `compressor` plus `eheater`;
  a heat pump collection that reports no `eheater` part at all (the heat
  pump of a hybrid system has no electric heater, #138) counts `compressor`
  alone. The gateway's collections still need both parts. The gateway also
  keeps recordings of the heat pump under its heat source
  (`/recordings/heatSources/hs2/emon/<total|ch|dhw>/<outputProduced|compressor>`,
  see [plant recordings](recordings.md)). The factors of yesterday, the
  months and the years take the heat and the electricity (the compressor)
  from these recordings first, so a month or a year is complete back to the
  first recorded day; the long-term statistics of the heat pump's counter
  entities count only where these recordings are missing. The gateway's own
  recordings count the boiler and are never used for these factors. Without
  the heat pump's recordings a month or a year from the counters alone stays
  unknown (`reason: partial_period`) while its days with a value cover less
  than half of the period's days so far (today included); the attribute
  `coverage_percent` always shows that share. The attribute
  `environmental_energy_kwh` gives the environmental energy of the period
  (heat minus electricity; left out when negative and while the value is a
  partial period). Without such counters (no
  separate heat counter, no electricity, or a value that is not current) the
  value stays unknown with `reason: burner_share`. The boiler's efficiency
  is not calculated. Through the cloud the integration reads only the heat
  pump's total counters (`/heatSources/hsN/emon/totalConsumption`), not its
  heating and hot water counters, so a hybrid system connected only through
  the cloud gets the total factors and environmental energy of the heat pump,
  while the heating and hot water factors stay unknown (`reason:
  burner_share`). A local access reads the heating and hot water counters
  as well where the gateway offers them.
- A factor stays shown when its period holds very little heat, but its
  attribute `note` is then `little_heat` and `little_heat_below_kwh` names
  the limit: 5 kWh of heat for today and yesterday, 30 kWh for a month, a
  year and since commissioning. Such a ratio rests on little data and says
  little; nothing else changes, no entity is
  added and the value is not hidden. The attributes are not recorded.

Attributes: `period_start`, `period_end` (the last day), `heat_kwh`,
`electricity_kwh`, `heat_source` and `electricity_source` (`counters`,
`local_recording`, `cloud_recording`, `local_and_cloud_recording`, joined by
`+` when the days came from several), `days` (days with a value),
`missing_days` (days of the period without a value, today included while
its start is unknown), `first_day` (the first day of the history when it
lies within the period) and `derived_from` (the counter collection with
`local` or `cloud`, the recorded paths with `recording`). For the heat pump
of a hybrid system also `basis`, `coverage_percent` and
`environmental_energy_kwh` (see above).

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

### Deutsch: Leistungszahl und Arbeitszahlen

Die aktuelle Leistungszahl wird nicht berechnet, solange die Wärmepumpe kühlt
(`reason: cooling`): Ein Kühllauf wäre sonst keine Heiz-Leistungszahl (die
Wärmeleistung ist null oder die Kälteleistung) und würde die Statistik
vermischen. Warmwasser in der Kühlsaison bleibt eine Heiz-Leistungszahl. Über 10
ist der Wert unplausibel (`reason: implausible`, bisher 15); bleibt sie bei
laufendem Verdichter zehn Minuten ohne Unterbrechung unter 1, ist sie ebenfalls
unbekannt (`reason: implausible_low`): Dann entspricht der Wert der Wärmeleistung
vermutlich einer elektrischen Leistung (die Bedeutung unterscheidet sich je
Modell und Firmware). Kürzere Einbrüche unter 1 werden unverändert angezeigt.
Ein unbekannter Wert kommt nicht in die Langzeitstatistik. Die Arbeitszahlen der
Zeiträume behalten ihre Grenze von 15.

Meldet die Anlage Brennerenergie über null in der Zähler-Sammlung oder wird
eine Hybridanlage erkannt, sind Arbeitszahlen und Umweltenergie unbekannt
(`reason: burner_share`), weil nicht bekannt ist, welcher Wärmeanteil vom Kessel
stammt. Die Regel betrachtet den Brennerzähler seit der Inbetriebnahme, nicht
den einzelnen Zeitraum. Eine reine Wärmepumpe ist nicht betroffen. Eine
Hybridanlage erkennt die Integration an den Signalen eines Hybrid-Managers, an
den Ressourcen der Hybridsteuerung, an einem System- oder Wärmequellentyp, der
eine Hybridanlage nennt, und seit 0.9.3-beta.6 an der
[Anlagenart](system-components.md#anlagenart): an einem Hybrid-Manager am
Hybridplatz der Komponentenliste (Typ 248 ab Version 06.01 oder Typ 16, auf
einem K30-Gateway ab Firmware 08.02.02 oder einem K40-Gateway ab Firmware
11.00.00) oder an einer Wärmequelle, die eine Wärmepumpe meldet, neben einer,
die einen Kessel meldet. Diese beiden Belege kommen nur hinzu; ein
Komponentenname zählt nie, und bis die gespeicherte Zeile des Anlagenmodells
nach einem Start gelesen ist, gelten die bisherigen Belege. Der Systemtyp
`eHybrid` ist eine Wärmepumpe mit Zusatzheizer und kein Beleg für eine
Hybridanlage. Eine solche
Hybridanlage ohne Brennerzähler erhält die Arbeitszahlen ihrer Wärmepumpe,
wenn die Wärmepumpe eigene Zähler meldet (siehe unten); die Aufzeichnungen
von `hs2` werden für eine Hybridanlage auch ohne sie gelesen (siehe
[Anlagenaufzeichnungen](recordings.md#the-heat-pump-of-a-hybrid-system)).
Hybridanlage über eine Hybrid-Manager-Komponente: Getestet mit synthetischen
Komponentenlisten; an einer echten Hybridanlage mit Hybrid-Manager noch nicht
bestätigt (#138).

Meldet die Wärmepumpe eines Hybridsystems eigene Zähler unter ihrer
Wärmequelle (zum Beispiel `/heatSources/hs2/emon/totalConsumption` mit
`outputProduced` und ihrem Strom, ebenso für Heizung und Warmwasser), werden die
Arbeitszahlen aller Zeiträume (heute, gestern, Monat, Jahr, seit
Inbetriebnahme) und die Umweltenergie allein aus diesen Zählern berechnet:
Wärme der Wärmepumpe durch Strom der Wärmepumpe. Die Wärme des Kessels fließt nie
ein. Das Attribut `basis: heat_pump_counters` zeigt das an, `derived_from` nennt
die Sammlung der Wärmepumpe. Strom der Wärmepumpe ist ihr Teil `electricity`,
sonst `compressor` plus `eheater`; meldet ihre Sammlung gar keinen Teil
`eheater` (die Wärmepumpe eines Hybridsystems hat keinen Heizstab, #138), zählt
`compressor` allein. Die Sammlungen des Gateways brauchen weiterhin beide Teile.
Das Gateway führt außerdem Aufzeichnungen der Wärmepumpe unter ihrer Wärmequelle
(`/recordings/heatSources/hs2/emon/<total|ch|dhw>/<outputProduced|compressor>`,
siehe [Anlagenaufzeichnungen](recordings.md)). Die Arbeitszahlen von gestern,
der Monate und der Jahre nehmen Wärme und Strom (den Verdichter) zuerst aus
diesen Aufzeichnungen, sodass Monat und Jahr bis zum ersten aufgezeichneten Tag
vollständig sind; die Langzeitstatistiken der Zähler-Entitäten der Wärmepumpe
zählen nur, wo diese Aufzeichnungen fehlen. Die eigenen Aufzeichnungen des
Gateways zählen den Kessel und fließen nie in diese Arbeitszahlen ein. Fehlen die
Aufzeichnungen der Wärmepumpe, bleibt ein Monat oder ein Jahr allein aus den
Zählern unbekannt (`reason: partial_period`), solange seine Tage mit Wert
weniger als die Hälfte der bisherigen Tage des Zeitraums (heute eingeschlossen)
abdecken; das Attribut `coverage_percent` zeigt diesen Anteil immer. Das
Attribut `environmental_energy_kwh` nennt die Umweltenergie des Zeitraums
(Wärme minus Strom; entfällt, wenn negativ, und solange der Zeitraum nur
teilweise abgedeckt ist). Fehlen solche Zähler (keine eigene Wärmemenge, kein
Strom oder kein aktueller Wert), bleibt der Wert unbekannt (`reason:
burner_share`). Der Wirkungsgrad des Kessels wird nicht berechnet. Über die
Cloud liest die Integration nur die Gesamtzähler der Wärmepumpe
(`/heatSources/hsN/emon/totalConsumption`), nicht ihre Zähler für Heizung und
Warmwasser. Ein Hybridsystem, das nur über die Cloud angebunden ist, erhält
daher die Gesamt-Arbeitszahlen und die Umweltenergie der Wärmepumpe; die
Arbeitszahlen für Heizung und Warmwasser bleiben unbekannt (`reason:
burner_share`). Ein lokaler Zugang liest auch diese Zähler, sofern das Gateway sie anbietet.
