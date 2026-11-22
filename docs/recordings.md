# Plant recordings

The heat pump records its own values per hour, per day and per month: heat
produced, compressor and electric heater electricity per mode (total,
heating, hot water, cooling, pool), starts, working time, the outdoor
temperature and the supply temperature. Gas and hybrid systems may also
record burner, solar and ventilation values. The integration reads these
recordings and

- shows **period entities** for yesterday, the previous month, the previous
  year, the current month and the current year, and
- can **import the hourly values into the long-term statistics** of Home
  Assistant (off by default, see [Import](#import-into-the-statistics)).

Tested on a real installation: the read-only answers of the local data port
and of the cloud (both forms, values and offsets) were compared with each
other and with the counters on a real heat pump. Expected, untested on a real
installation: the hourly reads over days, the switch between local and cloud,
the import into the statistics, the change of summer time (first change on
28 March 2027) and every recording of gas, hybrid, solar, pool and
ventilation systems.

## Where the values come from

1. **Local data port** (preferred): with a set-up local access. After every
   start of Home Assistant each recording is read once with its whole
   history (about ten days of hours, the days since recording began and the
   months). A recording the local access reports as busy is asked again
   twice within the same run, so all period entities exist after the first
   run. Afterwards, every hour at minute 20, the hours of the last two days
   are read again, but only for the recordings something needs: those of
   enabled period entities and, for the performance factors, the nine energy
   recordings; with the import switched on every recording. With the default
   entities that is about nine reads per hour on a typical heat pump (about
   20 with the import on, about 21 with all recording entities enabled). A
   recording that was not read for more than a day and a half is read with
   its whole history once it is needed again.
   About five minutes after the start, the needed recordings are read once
   more (unless the hourly run comes sooner): a past hour only counts after
   two reads with the same value, so yesterday is shown then instead of
   after the next hourly run. Entities added after a run get their values
   right away from what was already read. A run with nothing to read
   locally (for example only a *(calculated)* entity enabled) never falls
   back to the cloud while the local access answers.
2. **Cloud**: only when the local access is missing or does not answer, and
   only for the nine energy recordings (heat, compressor and heater for
   total, heating and hot water). Most installations have no local access,
   so these reads are kept low and spread over hours:
   - the start run reads yesterday once per recording (to find them), with
     no second run five minutes later;
   - every hour yesterday until it is confirmed (then like an older day at
     most twice a day), only for the recordings something needs;
   - today only for an enabled *current month* or *current year* entity or
     with the import on;
   - the history (older months, years and, with the import, older days)
     at most four intervals per hourly run, never in the start run and not
     for the rest of the day after a rate limit;
   - at most 24 reads per run. With the default entities and the
     performance factors that is about nine reads in the start run, about
     a dozen in the first hourly runs and a few per hour afterwards, well
     below the regular reads of the integration.

   The cloud's pauses and rate limits apply as for every other read; a
   pause or a rate limit during a run ends it. The manufacturer's app reads
   the recordings of a whole period at once (`query=all&sampleRate=all`);
   that form was refused (403) for a real account, so the integration
   reads one day, month or year per request.
3. **Counters of the integration**: where neither recording has a value,
   the period entities use the change of the integration's own counter
   entity over the period (from Home Assistant's long-term statistics).

The recordings are read only for a plant that offers a reading they belong
to: an energy counter collection, the starts or working time of the heat
source, or the refrigerant circuit or compressor of a heat pump. A room
controller that only shows a heat source's modulation gets no requests and no
period entities (`waiting` is `no_heat_source_anchor` in the diagnostics).

A recording that answers 404 is tried again once a day; one that answers 403
is not tried again before the next start. A recording in an unknown unit is
not used. A recording whose reads fail (another error, an answer that is no
recording, a server error of the local data port) is tried again once a day
(locally after two failures in a row when it answered before; what was read
stays) and never holds up the others; a cloud recording that fails while
the others answer is left out for three hours. A recording whose whole
history does not arrive within 30 seconds is read by its last two days
only. Nothing is read while no period entity is enabled, the import is
off and no other part of the integration needs the recordings, except once
after every start to find the recordings of the plant.

## Period entities

For each recording there are five entities, for example *Total – Heat
produced yesterday (system recording)*. The electricity of a mode is the
sum of its compressor and heater recordings. Enabled by default are
**total heat produced** and **total electricity** for all five periods
(yesterday, current and previous month, current and previous year); all
others are created disabled. An installation that already has these
entities keeps their state; the defaults apply to new entities only.

The plant splits the heat into heating and hot water by hour; in an hour in
which it switches between the two, both modes get the heat of that hour, so
heating plus hot water can be more than the total (about 2 kWh on a day
observed on a real installation). The heat per mode is therefore taken from
the counters first; those entities are named *(calculated)*. The
manufacturer's app shows the recorded heat per mode, so its values for
heating and hot water can be higher than these entities (on a real
installation hot water yesterday was 13.6 kWh in the app and 11.5 kWh from
the counters); the total is the same. Each energy entity has a disabled
companion with the other method (*(calculated)* from the counters, or
*(system recording)* from the plant) for comparison.

Attributes:

| Attribute | Meaning |
|---|---|
| `period_start`, `period_end` | First and last day of the period shown, in the plant's days |
| `source` | `local_recording`, `cloud_recording`, `local_and_cloud_recording` or `counters` |
| `resolution` | `daily` or `monthly` (from which values the period was summed), `hourly` for counters |
| `derived_from` | The recordings, or the counter entity, the value comes from |
| `method` | `recording` or `calculated`, as in the name |
| `counter_history_start` | Only on a *(calculated)* value of a month or year that starts before the counter's statistics in Home Assistant: the first day of those statistics |

A *(calculated)* energy of a month or year sums what the counter entity's
statistics in Home Assistant hold, so it covers the period only from
`counter_history_start` on. The *(system recording)* entity reads the plant's
own history, which can begin earlier, so the two can differ by the energy
before that day (on a real installation 91 kWh of 1,960 kWh this year, with
the counters' history beginning in July). Home Assistant does not mix the
two sources silently: the attribute names the start, and the recording entity
is the one that covers the whole period. The performance factors take such
periods from the recordings where the plant offers them.

A past period (yesterday, previous month, previous year) changes only once
the new period is confirmed by a read after its end. Just after midnight or
the first of a month the entity still shows the period before, with its
dates in the attributes. The values have no state class: they are not
counters. Some plants round older monthly values to whole kWh; months are
therefore summed from the days wherever the days are complete. The current
month with a day missing shows the sum of the known days, or the plant's
month value read at the start where that is larger.

A day or month without data (the plant or the gateway was off, `c = 0`) is
a gap, never a zero: yesterday then shows no value (or the counters' value
where the entity uses them), a month with such a day uses the plant's month
value, and a month without data adds nothing to its year. The plant's
placeholder values for such slots are dropped.

A period entity without a value (a previous year the plant has no data for yet,
cooling without cooling operation, a day not confirmed by a second read) shows
the state `unknown`. It shows `unavailable` only when it has no value and no
source of the recordings is attached (for example while the integration unloads). A
source that is attached but does not answer, such as during a cloud outage, leaves
the state `unknown`; a value already read stays available.

## Series without a local path: water softener and boilers

A few recordings exist only in the cloud, under paths a heat pump does not
have. They are read from the cloud only (never through the local data port,
which has no such path) and only for a plant that shows what they belong to.
Each becomes the same five period entities as any other recording, created
disabled; a path that does not answer (404, 403) or answers in a unit other
than the ones below gives no entity at all. Nothing is asked on any other
plant: a heat pump with energy monitoring (`/heatSources/emon`), a room
controller and every plant without the matching readings are not asked for
these paths. A heat pump of an older firmware without `/heatSources/emon` that
shows a heat pump reading counts as a boiler here: its three boiler paths are
asked (a 404 is remembered, at most one read per path and day), and an entity
exists only for a path that answers.

| Recording | Asked on a plant with | Unit accepted | Name |
|---|---|---|---|
| `/recordings/swCircuits/sw1/sensor/water` | a softener circuit (`/swCircuits/sw1/...`) | L, m³ (shown in L) | *Water consumption* |
| `/recordings/heatSources/actualCHPower` | a heat source with a reading a recording belongs to, without the energy monitoring (`/heatSources/emon`) a heat pump has | kWh, Wh, MWh | *Heating – Boiler energy* |
| `/recordings/heatSources/actualDHWPower` | as above | as above | *Hot water – Boiler energy* |
| `/recordings/heatSources/total/energyMonitoring/consumedEnergy` | as above | as above | *Total – Energy consumed* |

A plant with a softener circuit and no heat source reads only the water
recording. On a plant with local access the water or boiler paths are asked
in a short cloud run after the local run; they cost at most one read per
path and day while they do not answer. The recordings take part in the
import into the statistics like the others (water in litres, energy in kWh);
no counter is calculated for them, so there is no *(calculated)* companion.

The energy monitoring (*Total – Energy consumed*) has been reported to answer
the sum of minute values: a count of about 60 per hour and about 1,440 per day,
where the app shows the sum divided by 60 (a day of 420 as 7 kWh). I take that
form as reported and divide such a slot by 60; I could not test it on a plant
myself. Any other count that does not fit is not taken (see the next
paragraph).

Expected, untested on a real installation: no answer of a softener or a boiler
has been seen yet. The paths and the meaning of the values (energy summed per
hour, day and month, as for the heat pump) follow the recordings the
manufacturer's app reads; if a plant answers in another form, the series is
discarded (`unit` or a count mismatch in the diagnostics) rather than shown
wrongly. Please report such a case on GitHub with the diagnostics.

## The heat pump of a hybrid system

On a hybrid system (a heat pump together with a boiler) the gateway's energy
recordings (`/recordings/heatSources/emon/...`) count the boiler's heat. The
gateway keeps recordings of the heat pump under its heat source:

| Recording | Content |
|---|---|
| `/recordings/heatSources/hs2/emon/total/outputProduced` (also `ch`, `dhw`) | Heat produced by the heat pump |
| `/recordings/heatSources/hs2/emon/total/compressor` (also `ch`, `dhw`) | Electricity of its compressor (the heat pump of a hybrid system has no electric heater) |

They are asked only for a plant with a burner share (burner energy in the
gateway's counters, or a hybrid manager) whose heat pump reports counters of
its own under that heat source (for example
`/heatSources/hs2/emon/totalConsumption`, see
[calculated values](calculated-values.md)), and only for that heat source
(`hs2` to `hs6`). A plant with a heat pump only is never asked. With a local
access they are read through the local data port like the other recordings;
the cloud is asked only for those the local access does not answer. Without
a local access the cloud reads them like the nine energy recordings, with
the same limits, and before them, so that their history (back-fill) comes
first. A path that answers 404 is remembered and asked again once a day.

They have no period entities of their own (no entity is added). The
performance factors and the environmental energy of the heat pump use them
for yesterday, the months and the years, and the import into the statistics
takes them like the others (named for example *Heat pump – Total – Heat
produced (system recording)*). On such a plant the period entities of the
gateway's heat carry the attribute `heat_generator: boiler`.

Expected, untested on a real installation: the paths follow the local data
port's list of recordings for the heat sources hs2 to hs6; an answer of a
real hybrid system has not been seen yet. Please report on GitHub with the
diagnostics whether `hs2_total_heat` and `hs2_total_compressor` show `ok`
there.

## Import into the statistics

**Settings → Devices & services → Bosch/Buderus Home Comfort → Configure →
Plant recordings.** When switched on, every completed hour becomes a row of
an external statistic of the integration (`bosch_buderus_heating:rec_…`, one
per recording and gateway): energy, starts and working time as running sums,
temperatures as hourly means. On the first import the history is filled:
hourly where the plant still has hours, before that one row per day at
midnight, and before the first recorded day one row per month for the rest
of the month.

- Use **either** these statistics **or** the counter entities in the energy
  dashboard, not both: both count the same energy.
- **No fixed price for these statistics.** Home Assistant offers a fixed price
  (or a price entity) in the **Energy** dashboard only for counters that are
  entities. An imported external statistic has no such choice, so no cost can
  be assigned to it there. If you want costs, use the counter entities of the
  integration in the dashboard instead of the statistics (not both, see
  above).
  *Deutsch:* Im **Energie**-Dashboard bietet Home Assistant einen festen Preis
  (oder eine Preis-Entität) nur für Zähler an, die Entitäten sind. Einer
  importierten externen Statistik lässt sich dort kein Preis zuordnen. Wer
  Kosten sehen möchte, nimmt im Dashboard die Zähler-Entitäten der Integration
  statt der Statistiken (nicht beides, siehe oben).
- An hour is imported once it ended at least 15 minutes ago and two reads
  gave the same value; the hour still running is never imported. Every run
  reads the last 48 hours again; a changed hour is imported again from
  there on, so the sums stay consistent. Before Home Assistant 2026.11 the
  recorder cannot clear a range of rows: the rows are written over, and an
  hour that is now a gap gets a row without energy.
- A write that fails is tried again in the next run; nothing counts as
  imported before it was handed to the recorder. A failing recording does
  not stop the others or the entities' values.
- Hours without data (`c = 0`, for example while the plant was off) stay
  gaps, never zeros. Nothing is interpolated.
- Values with a start more than an hour ahead, before the first recorded
  day, below zero or above 90 kWh per hour (1.5 × 60 kW, days and months
  scaled) are discarded and counted in the diagnostics. The bound only
  catches obvious garbage; it is the same for every recording. A discarded
  hour counts as a gap: the other hours of its day are imported, and the
  day is summed up without it.
- A day whose hours cannot be placed on a change of summer time is imported
  as one day.
- Switching the import off keeps what was imported; switching it on again
  goes on from the last imported hour.
- The statistic IDs are derived from the gateway with a key of this Home
  Assistant: a new entry for the same gateway continues the same statistics
  and writes the available history again on top of the sum before it; the
  earlier rows from there on are replaced (a day the plant now only has as
  a day leaves no hourly rows behind).
- **Delete imported statistics** (with a confirmation) removes the imported
  statistics of the entry and switches the import off. Removing the entry
  keeps the imported statistics, as Home Assistant does for every
  statistic; the integration's own record of the recordings is removed.
  Right after the removal, a repair under **Settings → System → Repairs**
  offers to delete them (until the next restart of Home Assistant). Later,
  set up the gateway again and delete them in the options; a new entry for
  the same gateway uses the same statistics.
- Values the plant no longer has (after a reset or a new controller) stay
  imported; a confirmed hour or day that a later answer reports without
  data keeps its value (counted as `gap_after_value`).
- In a time zone off the full hour (for example India, Nepal, Iran,
  Newfoundland, central Australia) nothing is imported: the recorder takes
  rows on full UTC hours only. The diagnostics show
  `time_zone_on_the_hour: false`; the period entities work as usual.

## Counts that do not fit

Energy, starts and working time are sums with one value per hour: the count
`c` is 1 for an hour and, for a day, the number of hours with a value, at most
the hours of the day (24, or 23 and 25 on a change of summer time). A gateway that answers minute values instead
(`c` 60 per hour, `y` the sum of the minutes) would show about 60 times too
much, so a slot whose count does not fit is not taken. Nothing is
interpolated; the slot stays a gap, and the diagnostics count it as
`answer_count_form` under the events of the recording. Temperatures are means and
volumes are not judged. A day with a count below the hours of the day (the plant
was off for some hours) is taken as it is; only a count above the hours of
the day is discarded. Please report a plant that shows
`answer_count_form` on GitHub with its diagnostics.

## Diagnostics

The section `recordings` of each gateway shows, without values, dates or
identifiers: whether the import is on, the state per recording (`ok`,
`missing`, `forbidden`, `unit`, `failed`) for local and cloud, how many hours, days
and months are known, how many hours the import is behind, discarded values
with their reason, local and cloud differences, days whose hours could not
be placed, the last runs (reason, source, number of reads, and for an
import that failed the number of recordings) and whether the
plant's UTC offset differs from the time zone of Home Assistant, and the
plausibility bound per hour. Also: `time_zone_on_the_hour` (`false`: the
import waits), `local_paths_read_by_days_only` (recordings whose whole
history did not arrive in time) and `cloud_paths_left_out` (cloud
recordings left out for a while after a failed read), and on a hybrid system
`heat_pump_source` (the heat source whose own recordings are read, for
example `hs2`, with its recordings listed as `hs2_total_heat`,
`hs2_total_compressor` and so on); a run reports
`cloud_paths_left_out`, `cloud_backfill_left` and
`import_rows_not_on_the_hour` where they apply.

## Limits

- Values are read only while Home Assistant runs; missing hours are filled
  from the days after a longer stop (the plant keeps hours for about ten
  days).
- The days of the periods follow the plant. When the time zone of Home
  Assistant differs from the plant's, the attributes show the plant's days.
- A plant with more than one heat source records per heat source in paths
  the integration does not read yet. The same holds for a plant with more
  than one softener circuit: only `sw1` is read.
