# Source selection for paired readings

An existing sensor or binary sensor of a paired reading can be supplied by the
local or the cloud connection. Paired readings use the same documented path on
both interfaces, are not counters and cannot be adjusted through the cloud.

- **With the cloud running**, the option **Use local values for existing
  entities** decides: when it is on, the local value is shown; when it is off,
  the cloud value. The option is on by default when a system gets its first
  local access and when a system set up with local access only joins the
  cloud; a choice saved before stays.
- **Without the cloud** (switched off, a new sign-in needed, paused,
  rate-limited or unreachable), paired readings always show their local
  values, whether the option is on or off. Counters follow the same rule
  under the counter rule below, so they never count backwards.

The rules below describe the selection whenever local values are used.

## Rules

- **Local first.** A current local reading is shown. Otherwise the cloud
  reading within its [validity limit](reading-validity.md) is shown; otherwise
  the entity is unavailable.
- **Never polled less often locally.** With the option enabled, paired readings
  are polled locally at min(local interval, cloud interval), never below 60
  seconds, so the local value is at least as recent as the cloud would be. There
  is no cap for readings the cloud reads only once, adjustable readings, readings
  the cloud does not provide and while the cloud connection is not running.
  Until a running cloud has discovered its readings, its shortest interval
  applies. A changed cap never ages a reading faster than the interval that was
  scheduled at its last read.
- **Switching away** happens when the local reading expires (three missed
  intervals) or an explicit local answer ends it. No additional error counter.
  A pause of the local access (for example after one failed request) keeps
  the local readings until they expire; a refused token, a changed
  certificate or an unavailable access ends them at once.
- **Return** happens with the first valid local reading. The three-interval
  limit keeps switching to at most one change and back per three intervals.
- **Lists** (heat demand, variable tariff support status) are shown locally
  when every entry is one the cloud declares (or, if it declares none, the
  local answer); an empty list shows the same state as an empty cloud list.
  Otherwise the cloud list stays, and diagnostics name the reason
  (`unknown_entry`, or `shape_differs` for another shape).
- **Texts** follow the same rule: when the cloud declares the values of a
  text reading, a local text it does not declare keeps the cloud state
  (`unknown_entry`), so a valid state never turns into an unknown one.
- **Contradictory valid readings** show the local value. Diagnostics count
  differing pairs; there is no automatic reaction.
- Selecting a source starts no request, never enables a disabled connection
  and never changes where writes go: writes always use the cloud.

## Cloud reads while local values are shown

While **Use local values for existing entities** is on and an entity shows
the local value of a paired reading, the cloud reads that reading only every
15 minutes instead of every minute (in the cloud-friendly profile every 14
minutes, seven of its two-minute intervals). Readings of groups that the
cloud reads every 15 minutes or less often anyway keep their interval.

- **Switching to the cloud value** (the local access is not ready or failed,
  the local reading expired or does not fit, or the option is
  switched off) reads the cloud value at once, if it is older than its normal
  interval, and from then on at its normal rate. Until that read arrives,
  usually within seconds, the entity shows the last cloud value (see
  [validity](reading-validity.md)). Without a working cloud the next working
  cloud cycle reads it first.
- **The local access keeps its pace.** The local interval of a paired reading
  still follows the normal cloud interval, so the local value stays the more
  recent one.
- **Unchanged:** counters (their cloud counts decide source changes), every
  setting that can be adjusted through the cloud, and readings whose cloud
  value a control or a calculated entity uses: the current hot-water setpoint
  (the water heater's target), the current summer/winter mode of a heating
  circuit (checked before a heating change) and the system pressure (the
  calculated pressure status).
- **After a confirmed write** the slower readings of the same circuit are read
  with the next regular cloud cycle, as before.
- An explicit refresh in Home Assistant reads every reading at once.
- The optional "(cloud)" entities and the diagnostics comparison of cloud and
  local readings show cloud values up to 15 minutes old for these readings;
  the receive-time gap of a pair grows accordingly.

## Counters

Paired energy, start and runtime counters follow the same local-first rule,
with one addition: a counter only changes its source if every number shown
from it is at least the last number shown. That includes each part (for
example compressor, auxiliary heater and produced energy) and the derived
values (total electricity, environmental energy). Home Assistant would
otherwise record a lower count as negative consumption, or as a meter reset
that adds the whole count again to the statistics.

- If the preferred source is lower, the previous source stays while its reading
  is valid; after that the counter is unavailable until a source reaches the
  last count. A gap in the history, never an artificial consumption.
- Falling back to the cloud, for example when local values are switched off or
  the local reading expires, follows the same rule.
- Within one source a small decrease is shown as reported, for example the
  produced heat during a defrost. A drop of more than 10 % of a part is held
  until the next reading of the same source shows it again: a single low or
  zero reading never counts the whole meter again, and a real reset of the
  device counter stays visible one reading later. While a drop is held the
  counter shows the other source if it is not lower, else nothing.
- Parts must match exactly. Units must be the same spelling, or the established
  unitless kWh of cloud energy counters against local kWh. Since
  0.9.0-alpha.2 the same applies to the working time of a heat source
  (`/heatSources/{hs}/workingTime`): its cloud value has no unit and counts
  seconds, the local one reports `s`, so it can now follow the local access
  too. Other runtime counters whose units differ stay on the cloud.
- Without stored history, for example after enabling the feature or with an
  unreadable store, the cloud sets the first count; a local count is only used
  after that.
- A part missing in one reading keeps its last shown number, so it still binds a
  later source change. If the shown source stops reporting it, the part waits
  like one taken over from the other source: it binds again when it is back.
  Seven days after the shown source stopped reporting it, it is dropped once
  no current reading reports it.
- When the source a counter was shown from last is gone for good (its access
  removed or switched off), the other source may take over as soon as its
  numbers are not lower for the parts both report. A stored part that no
  current reading reports any more is dropped seven days after the change; a
  part that is reported waits until it reaches its number. A shown number
  never goes backwards.
- The last shown numbers and source are stored in Home Assistant and a source
  change is written immediately. After a restart the source shown last goes
  first. Other changes are written at most about a minute after the first
  one, and later changes never postpone that save; only an abrupt stop within
  that minute can leave an older count in the store. Removing the integration
  entry removes the stored counts.
- Diagnostics show per counter measurement the shown source, the number of
  source switches and of prevented switches, and evidence for comparable
  counter pairs: equal or different, largest relative difference in percent,
  finest reported step of each source and the receive-time gap. Absolute counts
  are never included.

## Writes

Settings are only written through the cloud, under the same lock as polling,
and only the cloud read-back confirms a write. Local readings are never used to
check, draft or confirm a write, and controls such as climate, water heater,
number, select, switch and time keep showing cloud values.

After a confirmed write, a local reading of the same circuit (for example every
reading under `/heatingCircuits/hc1` after a change of its manual room setpoint)
that was received before the confirmation no longer wins against a cloud
reading received after it. A running local source is asked once to read the
paired readings of that circuit again, within its existing explicit-read limits,
so sensors are current again within seconds. When the written setting itself
has a local reading, the first local reading after the confirmation is compared
with the confirmed value; diagnostics count agreeing, differing, incomparable
and unchecked (no local reading within ten minutes) read-backs per measurement.
A difference is only counted: the command is never repeated. The rule applies
to settings written through the controls; program and holiday changes do not
start it, their effect follows the regular reads.

## Readings and operation

Readings and operation stay separate. Sensors keep showing readings during a
cloud outage, from the local connection or within the cloud validity limit.
Controls such as climate, water heater, number, select, switch and time are
unavailable without a current cloud cycle, so nothing looks operable when a
command cannot be sent. Only one value inside a control follows the sensor: the
current hot-water temperature of the water heater uses the same paired local
reading as the hot-water temperature sensor; its mode, target and writes stay on
the cloud.

Sources are never mixed inside one entity, but separate entities each show
their own latest valid reading. Related values from different sources can
therefore briefly disagree: for example, the compressor power read locally
every minute already shows 0 W while an operating state the cloud reports only
every few minutes still shows heating. Both are correct for their own time;
grouping them on one source would give up the faster local readings.

When settings cannot be changed, the "Operate appliance" dialog names the
reason instead of showing an empty list: a sign-in is required, the cloud
access is stopped, the cloud is not reachable, a pause after a rate limit or
after repeated errors (with its approximate end), or values that are not
current yet. Without a cloud access it explains that the local access only
reads. The diagnostic entity "Cloud control available" (disabled by default)
shows the same state, with the reason and the end of a pause as attributes, for
dashboards and automations. It also covers a rate-limit pause of the whole
account and re-evaluates itself when a pause ends. While the cloud access needs
a new sign-in or is stopped, the cloud source and its entities, including this
one, are not provided; the dialog then names the reason.

## Per-source entities and the shown source

Each reading keeps exactly one regular entity. With an enabled local access,
each simple paired sensor reading that is not a counter also gets two
diagnostic entities, "... (local)" and "... (cloud)", disabled by default. They
show one source alone, independent of the local values option, for
troubleshooting and comparison graphs; a setting that can be changed through
the cloud may appear in "(local)" as read locally, and nothing is written
there. They have no state class, so they never create a second long-term
statistic or energy series next to the regular entity. Counters, calculated
values and readings split into several parts get none; boolean readings have no
comparable local value yet. If the local access is removed or switched off, the
two entities are no longer provided and can be deleted in Home Assistant.
Removing the local access never deletes or moves regular entities or the
device. Adding the same gateway again uses the same identities, so names and
activation choices of the per-source entities are kept.

Paired regular sensors with an enabled local access carry the attribute
`source` (`local` or `cloud`) for the value they show. A change of the source
is recorded in the history like any attribute change.

## Disable, hide or switch off

- Disabling an entity removes its state and recording; polling continues,
  because other entities and the comparison still use the data.
- Hiding an entity only keeps it out of automatic dashboards; its state and
  recording continue.
- Switching off an access in the connection settings stops its requests.
- Disabling the system's device in Home Assistant disables all of its
  entities; the accesses keep running. To stop the requests, switch off the
  accesses; to stop everything, disable the whole integration entry.

The local access keeps polling while the local values option is off, so the
comparison and diagnostics stay current and switching the option on takes
effect at once. Activation choices are kept: the integration only re-enables
entities it disabled itself and never enables the per-source entities.

## When the cloud stops

A short overview of both accesses and of operation without the cloud is in
[cloud and local access](operation-without-cloud.md).

Whenever the cloud is paused, rate-limited or unreachable, paired readings
with a current local value show it, independent of **Use local values for
existing entities**.

If the cloud access of a system needs a new sign-in, is switched off in the
connection settings or cannot sign in at start, and the system has an enabled
local access, its entities stay in place:

- paired readings keep showing current local values, also with **Use local
  values for existing entities** off, and so do the per-source entities and
  the readings only the local access provides;
- paired counters continue from the local access under the
  [counter rule](#counters), so they never count backwards;
- everything else and all controls are unavailable; a control reports why
  (sign-in required or cloud stopped) and no cloud request is sent;
- at start without a working sign-in, or with the cloud access switched off,
  the entities come from the stored inventory, if the cloud confirmed it
  within the last 30 days; with the access switched off, nothing is requested
  from the cloud at all, and the current faults come from the local error
  list;
- switching the cloud access off while Home Assistant runs keeps the running
  entities, and from then on their faults also come from the local error list,
  without a restart; while a new sign-in is required, or the cloud does not
  answer, the cloud stays responsible for the faults, because it is expected
  back (the fault entities are unavailable meanwhile);
- once the cloud answers again, it takes over without new entities.

For a temporary pause, switch the cloud access off rather than removing it.

A cloud that does not answer for a cycle or two leaves no error in the log:
the failed cycles of an outage are debug messages. Once the outage has lasted
five minutes and at least three cycles in a row have failed, one error names
the failure (for example `no usable resources (timeout)`), how long it has
lasted and how many cycles failed; the rest of that outage stays at debug
level. The warning when the protective pause starts and the info line when
the cloud answers again stay as before. Only a cycle that actually asked the
cloud ends an outage: a minute in which nothing was due, for example because
the shown readings come from the local access, neither counts as a failure nor
starts the grace period again.

### A system set up with local access only

A system added with **Local access only** has no cloud access from the start.
It is shown as described for a removed cloud access below: read-only from its
local readings, under its gateway ID (the same ID the cloud uses), with the
manufacturer and controller model its gateway reported during the setup.

#### Adding the cloud later

The cloud access can be added later in the same entry's options (**Connect
cloud access**), or, if the entry already has a cloud access, by selecting the
system in that access's system selection. Selecting the system joins it with
the cloud as the same system; a second system is never created. Selections
that do not include such a system are saved as before, without any request:

- before saving, the cloud's system information and the system information
  read through the running local access are compared; the gateway IDs must be
  the same and at least one module must match exactly. The gateway ID alone
  never joins a system;
- the local access must be switched on, running and reachable. Otherwise the
  dialog asks to switch it on and make it reachable, or to set it up again,
  and saves nothing. While the regular local reads keep it busy, the
  comparison waits for its turn a few times; if it stays busy, the dialog asks
  to try again shortly;
- if the two do not describe the same system, the selection shows an error
  and saves nothing. Deselecting the system keeps it as it is, read-only from
  its local access;
- after joining, the cloud takes over the same entities, devices and fault
  list, adds its controls, and the local access continues; it restarts once
  because its system changed. The comparison is stored, so setting up the
  local access again later needs no new cloud comparison. Local-only sensors
  of readings the cloud now offers continue as the cloud's sensors, with their
  recorded history, where that is unambiguous (see [entities](entities.md)).

A system set up locally in one entry cannot be added through the cloud in
another entry; add the cloud access in the entry that holds the system. Only
one cloud access can be configured across all entries for now, so if another
entry already holds the account's cloud access, adding a cloud access to the
entry with the locally set up system is refused.

### Removing the cloud access

Removing the cloud access deletes its sign-in and its link to the systems. The
entities stay in Home Assistant:

- a system that was selected and has an enabled local access keeps showing its
  local readings, read-only and without controls, as described in
  [without the cloud for more than 30 days](#without-the-cloud-for-more-than-30-days),
  under the same entities; this also applies after a restart; while its local
  access is switched off the entities are unavailable, and they show local
  readings again once it is switched on;
- every other system keeps its entities without current values;
- after a new sign-in that selects the same system, the cloud takes over the
  same entities again and continues the fault list from the local one; a
  sign-in that does not select it leaves it showing its local readings. If no
  comparison of the system with the cloud is stored, the same fresh
  comparison as for [adding the cloud later](#adding-the-cloud-later) is
  required first.

A system that was deselected or forgotten before the access was removed stays
hidden.

Without a local access, or after the local access is switched off, the
entities of that system are unavailable as before. Water heater and climate
entities are controls and follow the cloud; their current temperature remains
visible through the corresponding sensor.

### Without the cloud for more than 30 days

The stored inventory is used for up to 30 days after the cloud last confirmed
it. Nothing confirms it while the cloud access is switched off, needs a new
sign-in or the cloud does not answer. Once it is older than 30 days, the
system's entities come from its local readings alone:

- with the cloud access switched off, or for a system started from the
  inventory because a new sign-in was required, at that moment while Home
  Assistant runs;
- otherwise (for example a cloud that stops answering or needs a new sign-in
  after it had answered) at the next start.

Then:

- every readable local reading becomes an entity; identifiers, addresses,
  names and network data never do;
- readings the cloud offered keep their entities and IDs, so their history
  continues; the other readings are their own local-only sensors, as
  described in
  [readings only the local access provides](entities.md#readings-only-the-local-access-provides);
- the entities are read-only: there are no controls, nothing is written, and
  they request nothing from the cloud (a cloud access that is switched on
  keeps trying to reach it as before);
- a reading that is not current is unavailable, but its entity stays; a
  number reading that reports an error marker shows no value;
- counters continue only from a count shown before: without one they stay
  unavailable, and they never show a number below the last one shown;
- the current faults come from the local error list;
- the descriptions of the cloud's readings (type, unit, allowed values,
  limits, never a value) are kept until the system is deselected or its
  gateway is forgotten; a description is used only where it fits the local
  answer.

A required sign-in stays required, and its repair stays open. The entities
from the local readings stay until the cloud answers again; then the cloud
takes over the same entities, and its fault list continues from the local
one. A fault that changes in the short time between the cloud's start and its
first answer is announced once, by the cloud.

## Decision matrix

| Cloud reading | Local reading | Option | Shown |
| --- | --- | --- | --- |
| current | current | on | local |
| current | current, different value | on | local |
| current | missing or expired | on | cloud |
| current | invalid, other type or unit | on | cloud |
| current | binding disabled | on | cloud |
| current | current | off | cloud |
| expired | current | on | local |
| expired | missing | on | unavailable |
| no answer (timeout, 429, 5xx) | current | on | local |
| no answer (timeout, 429, 5xx) | missing | on | last cloud value until its limit |
| ended (403/404, invalid data) | current | on | local |
| ended (403/404, invalid data) | missing | on | unavailable |
| sign-in required or access switched off, local access enabled | current | on | local |
| sign-in required or access switched off, local access enabled | missing | on | unavailable |
| no answer, paused or rate-limited | current | off | local |
| sign-in required or access switched off, local access enabled | current | off | local |
| connection stopped without an enabled local access | any | on | unavailable |
| resource withdrawn | any | on | unavailable |
| expired | current | off | unavailable |

Each row is covered by a test. Diagnostics show per installation how many
eligible entity sources use local or cloud values and why cloud values are
used (`cloud_reasons`).
