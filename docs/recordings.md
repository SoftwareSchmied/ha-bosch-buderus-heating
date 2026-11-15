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
   months). Afterwards, every hour at minute 20, the hours of the last two
   days are read again for each recording that answered (about 13 reads per
   hour on a typical heat pump).
2. **Cloud**: only when the local access is missing or does not answer, and
   only for the nine energy recordings (heat, compressor and heater for
   total, heating and hot water). Every hour today, and yesterday until it
   is confirmed (then like an older day at most twice a day), plus at most
   a few older days, months and years per run until the history is complete
   (at most 30 reads per run). The cloud's pauses and rate limits
   apply as for every other read.
3. **Counters of the integration**: where neither recording has a value,
   the period entities use the change of the integration's own counter
   entity over the period (from Home Assistant's long-term statistics).

A recording that answers 404 is tried again once a day; one that answers 403
is not tried again before the next start. A recording in an unknown unit is
not used. Nothing is read while no period entity is enabled, the import is
off and no other part of the integration needs the recordings, except once
after every start to find the recordings of the plant.

## Period entities

For each recording there are five entities, for example *Total – Heat
produced yesterday (system recording)*. The electricity of a mode is the
sum of its compressor and heater recordings. Enabled by default are only
**total heat produced yesterday** and **total electricity yesterday**; all
others are created disabled.

The plant splits the heat into heating and hot water by hour; in an hour in
which it switches between the two, both modes get the heat of that hour, so
heating plus hot water can be more than the total (about 2 kWh on a day
observed on a real installation). The heat per mode is therefore taken from
the counters first; those entities are named *(calculated)*. Each energy
entity has a disabled companion with the other method (*(calculated)* from
the counters, or *(system recording)* from the plant) for comparison.

Attributes:

| Attribute | Meaning |
|---|---|
| `period_start`, `period_end` | First and last day of the period shown, in the plant's days |
| `source` | `local_recording`, `cloud_recording`, `local_and_cloud_recording` or `counters` |
| `resolution` | `daily` or `monthly` (from which values the period was summed), `hourly` for counters |
| `derived_from` | The recordings, or the counter entity, the value comes from |
| `method` | `recording` or `calculated`, as in the name |

A past period (yesterday, previous month, previous year) changes only once
the new period is confirmed by a read after its end. Just after midnight or
the first of a month the entity still shows the period before, with its
dates in the attributes. The values have no state class: they are not
counters. The months July to October 2026 of the reference installation are
rounded to whole kWh by the plant itself; months are therefore summed from
the days wherever the days are complete. The current month with a day
missing shows the sum of the known days, or the plant's month value read at
the start where that is larger.

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
  imported.

## Diagnostics

The section `recordings` of each gateway shows, without values, dates or
identifiers: whether the import is on, the state per recording (`ok`,
`missing`, `forbidden`, `unit`) for local and cloud, how many hours, days
and months are known, how many hours the import is behind, discarded values
with their reason, local and cloud differences, days whose hours could not
be placed, the last runs (reason, source, number of reads, and for an
import that failed the number of recordings) and whether the
plant's UTC offset differs from the time zone of Home Assistant, and the
plausibility bound per hour.

## Limits

- Values are read only while Home Assistant runs; missing hours are filled
  from the days after a longer stop (the plant keeps hours for about ten
  days).
- The days of the periods follow the plant. When the time zone of Home
  Assistant differs from the plant's, the attributes show the plant's days.
- A plant with more than one heat source records per heat source in paths
  the integration does not read yet.
