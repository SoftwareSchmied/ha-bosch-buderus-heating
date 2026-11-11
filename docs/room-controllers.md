# Room controllers (TC100.2, CT200)

Room controllers of the RRC 2.0 platform, such as the TC100.2 and the CT200,
connect only through the cloud. The integration recognises them by their
resources (device type `rrc2`, product type `RRC 2.0` or RRC zones), never by
a model name. This page describes what the integration offers for them in
0.9.0-alpha.5, what has been confirmed on a real controller and what is
expected but untested. The technical details of the zones, schedules and
discovery are in [RRC zones and schedules](rrc-zones-and-schedules.md).

## What is confirmed

So far a TC100.2 has confirmed with this integration:

- with 0.9.0-alpha.4: the zone readings (room temperature, active setpoint,
  zone names), switching a zone between manual mode and the schedule
  (`userMode` `manual`/`clock`) and changing the manual setpoint of a zone;
- with 0.9.0-alpha.5 (controller firmware 05.04.00, a system with five
  zones), the following changes, each checked in the manufacturer's app and
  set back afterwards:
  - switching the program of a zone and back;
  - a temporary setpoint in the schedule mode, and its clear button; Home
    Assistant showed the new target after about a minute;
  - the climate to `heat` with a target and back to `auto`: the mode changed
    first, then the target;
  - turning the open window detection of a zone off and on;
  - creating a one-day absence in the calendar and deleting it again;
  - the hot water mode `ownprogram` → `high` → `ownprogram`;
  - the boost on and off;
  - the away mode on and off; while it was on, a new zone target was refused
    with the message that the away mode is active;
- with 0.9.0-alpha.5 as well: the discovery completes, and every zone gets
  its program select with the program names from the controller;
- readings that look right with 0.9.0-alpha.5: the climate of each zone with
  its next setpoint and zone status, the window sensors, the system pressure,
  the outdoor temperature, the humidity, the display and cause code, the
  boost settings, the away temperature, and the hot water mode and
  temperature. The eight radiator thermostats of that system appear as
  separate devices in the right areas, with their battery and valve state.

Everything else on this page is **expected, untested**: it is built and
tested with simulated data in the shapes of the observed TC100.2 answers and
of constructed controller data, but nobody has tried it on a real controller
yet. The CT200 as a whole is expected, untested. The diagnostics name the
changes confirmed above for their function group
(`confirmation: partly_confirmed`, `confirmed_functions`,
`confirmed_on: tc100_2`); every other group stays
`confirmation: unconfirmed`. A confirmation on the TC100.2 does not confirm
a CT200. I would be glad about reports from owners, ideally with a
diagnostics download (see [diagnostics](diagnostics.md)).

| Function | Status |
| --- | --- |
| Zone readings, manual/schedule mode, manual setpoint | Confirmed on a TC100.2 |
| Mode together with a target (`heat` with a target, back to `auto`) | Confirmed on a TC100.2 |
| Temporary setpoint and its clear button | Confirmed on a TC100.2 |
| Advance next switch point | Expected, untested |
| Reading the follow-up values about five seconds after a zone change | Expected, untested |
| Zone details: next setpoint, zone status, window sensor | Readings look right on a TC100.2 |
| Zone details: minutes until the next setpoint, valve opening, optimum start | Expected, untested |
| Open window detection of a zone | Off and on confirmed on a TC100.2 |
| Open window detection of the system, its duration and temperature | Expected, untested |
| Away mode | On and off confirmed on a TC100.2, also the refused zone target while it is on; the away temperature reading looks right |
| Zone locks for standby, zone or communication errors | Expected, untested |
| Program selection | Confirmed on a TC100.2, with the program names on every zone |
| Editing switch points | Expected, untested |
| Absence calendar | Creating and deleting a one-day absence confirmed on a TC100.2; changing an event and special days expected, untested |
| Boost | On and off confirmed on a TC100.2; the boost settings readings look right |
| Fireplace function | Expected, untested |
| Hot water | Changing the mode (`ownprogram`, `high`) confirmed on a TC100.2; mode and temperature readings look right; the other hot water settings expected, untested |
| Heating circuit settings | Expected, untested |
| System readings | Display and cause code, system pressure, humidity and outdoor temperature look right on a TC100.2 |
| Fault and maintenance sensors | Expected, untested |
| Energy history in the statistics | Expected, untested |
| Radio devices | Devices, areas, battery and valve state look right on a TC100.2 |
| Settings of the radio devices | Expected, untested |
| CT200 (every function) | Expected, untested; no local access |

## How changes are written

Every change goes through the same path: the integration reads the current
value and its limits again, refuses a value outside them before sending,
sends the change exactly once and reads it back after 1, 2, 4 and 8 seconds.
A room controller applies a change with a delay. If it has not confirmed the
change by then, a message says that the change was sent but is not
confirmed yet; a later regular reading that shows it counts as confirmed.
A change is never resent on its own.

For every room controller setting, "reads again" means one request to the
controller right before the change, also when the last poll is only a few
minutes old. A changed value does not block the change; a setting that is
now unavailable or not used is refused, and the entity shows the new state
at once. If this read fails, nothing is sent and the last reading stays.

The controller marks each setting as used and available:

- **Not available** (whether used or not): the setting gets no control; an
  existing entity becomes unavailable.
- **Available but not used:** the control stays visible with its value, but
  a change is refused before anything is sent, with a message that names
  the setting (away mode, open window detection, hot water, a radio device
  or another setting). An example is the open window detection of a zone
  that the controller does not use at the moment.
- The settings of the **heating circuit** do not depend on "used": a setting
  that is not used stays changeable, and one that is used but unavailable
  stays visible and is locked with a message that names the heating circuit.
  The season mode and the setpoint optimization are the exception: they
  follow the two rules above, so a setting that is not available has no
  control and one that is not used stays visible and refuses a change
  unsent. The heating mode of a zone keeps its own rule (locked, for example
  while the system cools).

Readings are not affected by these flags. All of this is expected,
untested: which settings a real controller marks as not used, and when, is
still to be confirmed.

If a change stays unconfirmed, its value is read again with the next
regular cycle (within a minute) instead of with its own, slower group, so a
zone's target control is back quickly. If the controller ends a replaced
temporary setpoint at once, its reading of 0 stays valid and is shown.

A confirmed change of a zone (its target, temporary setpoint, mode, program
or open window detection) also changes values the controller works out
from it: the active setpoint, the zone status, the next setpoint, the
minutes until it and the zone list. They are read once about five seconds
after the change instead of only with the next regular cycle about a minute
later. Several changes in a row share this one read, which follows the last
of them. It runs as part of a regular cycle, so a pause after a rate limit
or the protection pause holds it back until the cloud can be read again;
nothing is written again. *Expected, untested:* whether a controller has
taken over the new values after five seconds; if not, the next regular
cycle shows them.

For the boost, only what a change depends on must still be as shown: the
mode and the rooms. Otherwise the change is refused with a message that the
boost changed on the controller.

## Zones

Every zone that reports a heating mode and a room temperature gets a climate
entity. It stays available while the mode is valid; if the controller does
not use the room temperature at the moment, the current temperature is
unknown and the zone can still be controlled.

- `heat` is the manual mode, `auto` the schedule. Room controllers have no
  off mode. *Confirmed on a TC100.2.*
- In manual mode a new target changes the manual setpoint. *Confirmed on a
  TC100.2.*
- **Temporary setpoint.** In the schedule mode a new target sets a temporary
  setpoint. It lasts only until the next switch point of the schedule; the
  attribute `override_until_next_switch_point` says so. It must be at least
  the lowest manual setpoint of the zone. If the controller ends it at once,
  for example because a switch point was reached, a message of its own says
  so. *Confirmed on a TC100.2*; the case of a setpoint the controller ends at
  once is expected, untested. Like every room controller setting, the
  temporary setpoint follows the used and available flags: reported as not
  used, a change is refused unsent; reported as not available, the zone
  takes no target in the schedule mode. *Expected, untested:* the TC100.2
  sends no such flags for it, so whether a controller reports it as not used
  in the schedule mode is not known. If a temporary setpoint is refused with
  the message that the controller does not use it, I would be glad about a
  report with a diagnostics download.
- **Clear temporary setpoint.** A button per zone ends an active temporary
  setpoint while the zone follows its schedule. *Confirmed on a TC100.2.*
- **Advance next switch point** *(expected, untested)*. A button per zone
  that reports its next setpoint sets the temporary setpoint to that next
  setpoint, so the zone heats up or lowers its temperature now instead of at
  the next switch point. It lasts until that switch point. The button is
  available only while the zone follows its schedule and reports a next
  setpoint that the temporary setpoint can take (not "none", within its
  limits and steps, at least the lowest manual setpoint). The mode and the
  next setpoint are read again right before the change; if the next setpoint
  changed on the controller since the last reading, nothing is sent. The
  attribute `next_setpoint_advanced` (on the button, and on the zone climate
  while a temporary setpoint is active) is `true` while the temporary
  setpoint equals the next setpoint. The clear button ends it early.
- A mode together with a temperature changes the mode first and the target
  only after the controller has confirmed the mode. *Confirmed on a
  TC100.2.*
- **Locked zones:** while the away mode is active, a new target or clearing
  the temporary setpoint is refused with a message that names the away mode;
  the mode can still be changed. *The refused target is confirmed on a
  TC100.2; the rest is expected, untested.* A zone that the
  controller reports with a zone or communication error, or in standby,
  takes no change of its mode or its target at all; in standby its action
  is `off`. This also applies to the zone's own mode select and manual
  setpoint number. Nothing is sent in these cases. The attribute
  `control_lock` names the lock (`away`, `standby`, `unreachable`) and
  `away_mode` the state of the away mode.
- **Shown target** *(expected, untested):* the attribute `active_setpoint`
  is the setpoint the controller applies. While the zone is locked, has an
  open window or runs a boost, the target shown is this active setpoint;
  otherwise it is the manual or temporary setpoint just set. If a change
  stays unconfirmed, the zone keeps its target control and its limits until
  the value is read again within a minute; a further change waits for that
  reading.
- **Zone details:** sensors for the next setpoint, the minutes until it and
  the zone status (idle, heat request, zone error, communication error or
  warning, low battery); the valve opening and the
  optimum start with its heat-up rate (disabled by default); a binary sensor
  "Window open". The window sensor follows the special function the zone
  list reports for the zone (`window_open`); only where the list does not
  say (an older controller, or another function such as a boost) it reads
  the zone's open window status, whose values `open` and `closed` are a
  guess. The zone list is read every five minutes for this. *The next
  setpoint, the zone status and the window sensor look right on a TC100.2;
  the other details are expected, untested.*
- **Open window detection:** a switch per zone and one for the system, and
  the system's open window duration and temperature where the controller
  allows writing them (disabled by default). A zone switch is refused while
  the controller does not offer the system's open window detection (not
  available). *Turning a zone's detection off and on is confirmed on a
  TC100.2; the system's settings are expected, untested.*

A room controller with zone climates gets no climate entity for its heating
circuit as well.

## Programs (heating schedules)

- **Program selection** *(confirmed on a TC100.2: switching a program and
  back, the select on every zone with the program names from the
  controller)*. A select per zone chooses the zone's program. Its options
  are the program numbers, so automations keep working when a program is
  renamed; the names are in the attributes `program_names` and
  `program_name`. Changing the program does not switch the zone to the
  schedule. A selection and a changed switch point are read back like every
  other change (1, 2, 4 and 8 seconds); if the controller has not confirmed
  them by then, the message says that the change was sent but is not
  confirmed yet. On a large system the program list is read together with the
  zones and searched again if it is missing.
- **Editing switch points** *(expected, untested)*. Under **Configure → RRC
  heating schedules** you can change the time and temperature of an existing
  switch point of a week or day program. A change affects every zone that
  uses the program. New programs and added or deleted switch points are not
  supported.

## Away mode, absences and special days

- **Away mode** is a switch, the **away temperature** a number. *Turning the
  away mode on and off is confirmed on a TC100.2; the away temperature
  reading looks right, changing it is expected, untested.*
- **Absences and special days** *(creating and deleting a one-day absence
  confirmed on a TC100.2; everything else expected, untested)* appear as a
  calendar of whole days, in the controller's time zone. You can create,
  change and delete absences there. A day that runs like another weekday
  ("Like Monday") can only be deleted. A name has at most 32 bytes (fewer
  characters with umlauts or emoji), events may not overlap, not even on
  their first or last day, and the end may not be in the past. Events lie
  between 2016 and 2099. Recurring events, descriptions and locations are
  not supported.
- If one event list cannot be read for a moment (the controller answers 500
  at times), its events stay in the calendar from the last valid reading as
  long as any reading stays valid (three poll intervals), so a running
  absence does not end in Home Assistant. The attribute `stale` is `true`
  meanwhile, and the calendar cannot be changed until every list reads
  again.
- An event list that the controller names but that got no answer at the
  start (for example a sporadic 500) does not stop the setup. It is not
  taken as missing: it is searched again after the start like other missing
  parts, and until it reads the calendar cannot be changed, because an event
  in it could overlap a new one. The same applies to a list that the
  discovery asked for without an answer; a controller without one of the
  three lists therefore shows its events but cannot change them.
- A change of the events is sent once. A new event is recognised by its
  name, dates and type, also if the controller gives it the number of an
  older event. A change only needs the lists it can show in; another list
  that cannot be read for a moment does not leave it unconfirmed. If the
  controller does not confirm it in time, the next change is refused for up
  to ten minutes until the first one shows, so an event is never created
  twice.

## Boost and fireplace function

*Turning the boost on and off is confirmed on a TC100.2, and the boost
settings readings look right; everything else is expected, untested.*

- **Boost:** a switch, the boost temperature and the boost duration. The
  switch writes the controller's boost settings as a whole: a start sends
  the temperature, the duration and the rooms together with "on", a stop the
  same with "off". The temperature and duration numbers send nothing by
  themselves: while the boost is off, a new value is kept (also across a
  restart), shown, and sent with the next start; while the boost runs, a
  change is refused with a message of its own. Turning it on while it runs
  sends nothing. *Expected, untested:* whether the controller would also
  take a new temperature or duration alone is not known, so it is never
  sent alone.
- **Rooms of a boost:** chosen on the controller. A start heats only the
  chosen rooms that still exist and that the controller allows for the
  boost; without such a room, or if the controller names no allowed rooms,
  the boost is refused. A boost in a form the integration does not know
  completely is shown but not changed.
- **Fireplace function:** a switch, disabled by default.

## Hot water

*Changing the mode (`ownprogram` → `high` → `ownprogram`) is confirmed on a
TC100.2, and the mode and temperature readings look right; everything else
is expected, untested.*

- The operating mode as a select and as a water heater entity. A controller
  that lists its modes offers exactly those. One that lists none offers
  off, comfort and own program; while it reports one of the eco levels
  (eco or eco+, the levels of a heat pump), it offers these two as well.
  If the controller reports its heat source as a heat pump or a hybrid
  system (`/heatSources/type`), all five modes are always offered, so eco
  can be chosen again after comfort. On other systems, after you switch away
  from an eco level, it is offered again only once the controller reports
  it. A controller that sends an empty list of modes
  keeps the mode as a reading only, so nothing is guessed; the diagnostics
  count such lists (`operation_mode_options_empty`). Both cases are expected,
  untested. If the controller reports the hot water mode as not available,
  there is no water heater entity.
- The comfort temperature as a number.
- **Extra hot water:** a switch and its duration in minutes. The switch does
  not turn on while the duration is 0; set the duration first. In
  **Operate appliance** the duration is written before the switch.
- **Thermal disinfection** (disabled by default): a switch, the weekday or
  every day, the start time (in five-minute steps, unless the controller
  reports another step) and the result of the last run.
- **Current hot water schedule level** (disabled by default): the level of
  the hot water program right now, calculated from its switch points and the
  controller's clock. It shows the levels as the program names them; with a
  boiler, eco and eco+ may mean off.

## Heating circuit

*Expected, untested.*

- Summer/winter switching and night setback by outdoor temperature (off or
  automatic), with their outdoor temperature thresholds, disabled by
  default.
- The season mode, only when the controller offers at least two modes and
  reports its current mode among them, and the setpoint optimization. Both
  are everyday settings and enabled by default. The setpoint optimization is
  a switch for the values `on` and `off`; a controller that reports `true` or
  `false` instead shows the value without a switch.
- A sensor for the operating season (heating, cooling, idle).
- **For specialists, disabled by default:** the heating curve (base and end
  point), the minimum and maximum supply temperature and the room influence.
  Enable them only if you know these settings. The base point and the
  minimum supply temperature stay at or below the end point and the maximum
  supply temperature; before a change these limits are read again, and
  nothing is sent if one of them changed. In **Operate appliance** several
  limits can be changed in one step: the order of the writes keeps them in
  order, and a step that would break it sends nothing.
- The heating curve, the supply limits, the room influence and the
  summer/winter and night settings only act with weather-compensated
  control. While the circuit uses another control (control strategy other
  than `weather`), a change is refused with a message, and **Operate
  appliance** does not show these settings.
- Installer settings (control strategy, heating system type, room control
  type) are never written.
- The heat-up speed is switched off by the manufacturer and is never
  written; it is at most a disabled reading.

## System, faults and maintenance

*The display and cause code, the system pressure and the humidity look right
on a TC100.2; everything else is expected, untested.*

- Sensors for the display code, the cause code, the system pressure (only
  with the unit bar) and its status, and the indoor humidity. The
  maintenance request text, the controller's air, chip and board
  temperatures and the firmware update state are disabled by default.
- Binary sensors **Locking fault** (the appliance is locked out until a
  manual reset), **Blocking fault** (clears on its own), **Maintenance due**
  and **Refill needed**.
- **Fault texts:** radio device faults get their own texts (lost
  connection, low battery, valve adaptation pending, valve too stiff, valve
  travel too large or too small, valve in a wrong position), other radio
  device faults the text "Radio device fault (code …)". Cause codes for a
  too low water pressure read "Water pressure too low".
- **Fault times:** a time the controller sends without a time zone is read
  as local time in Home Assistant's time zone, as the controller shows it;
  a time with a zone keeps it. *Expected, untested.*
- **Maintenance messages:** a notification with the numeric class 18 or 20
  counts as maintenance on every gateway, not as a fault. Under **Configure →
  Notifications** an option, off by default, also shows warnings and
  maintenance in the bell notification. See [faults](faults.md).
- **Display levels:** a notification that the controller marks for experts
  or installers (display level 0, 2 or 4, numeric class below 18) stays
  listed under Active notifications, but does not turn on System fault and
  does not show in the bell. See
  [display levels on room controllers](faults.md#display-levels-on-room-controllers).

## Energy history

*Expected, untested.* The unit kWh is assumed but not confirmed on a real
controller.

The controller's daily energy history is imported into the Home Assistant
statistics, not as entities. The names end with "(kWh expected)" to show
that the unit is not confirmed. They cover the electricity and heat of the
heat pump, the boiler and the electric heater for heating and hot water, the
cooling of the heat pump, the solar electricity produced and the daily mean
outdoor temperature. Gas
values are imported only when the controller confirms the unit kWh.

- Choose the statistics yourself in the **Energy** dashboard; nothing is
  added there automatically.
- Only completed days are imported. A first filling reaches back at most
  400 days; changed days are recalculated up to 31 days back.
- Every run reads again from the last day already read, so a day that was
  still running when it was read is completed later. Controllers from
  firmware 5.3.0 are asked by date (the first time without a date), older
  ones by entry number. A day without a record, a full history that drops
  its oldest day and a history that starts again do not stop the import.
- Older firmware reports two values under other keys (`eCH`, `eHW`). On a
  heat pump or hybrid system they count as the heat pump's electricity; on
  a boiler, without a heat source or with an unknown heat source type they
  count as gas and follow the gas rule: without the confirmed unit kWh they
  are not imported. The heat source type is read once, only when such keys
  appear; until it is known, these days wait on the controller. If the
  controller answers its type with a server error, the days wait and every
  run reads the type again, for up to 24 hours from the first error; the
  waiting also continues across a restart. Only then are the days imported
  without these two values, nothing is assumed, and the next run still reads
  the type again. A refused type (for example HTTP 404) counts as an unknown
  type at once.
- If the import starts over, for example after the integration was added
  again or its stored import state could not be read, the statistics
  continue from their last recorded value; recorded days are not written
  again, so a sum never runs backwards.
- The first import runs 15 minutes after the start, then every three hours,
  with at most eight requests per run. The shared transport may repeat one
  of them once after a server error or a timeout.
- To remove the statistics, delete them under **Developer tools →
  Statistics**.
- The statistic IDs (`bosch_buderus_heating:rrc_<token>_<value>`) and the
  name of the stored import state contain a token of the gateway, never its
  identifier. The token is an HMAC-SHA256 of the gateway ID keyed with the
  instance ID of your Home Assistant, so the ID cannot be found back from the
  database or an export alone. It stays the same when the integration is
  added again and after a restored backup; on another installation the same
  gateway gets other statistic IDs.

Expected, untested: whether the history lists the running day, whether a
query by date answers from that day on or only for a day with a record,
what the first answer without a date contains, and which heat source
types report the older keys. The import is built to lose no day in each of
these cases.

## Radio devices

Every radio device in the controller's device list becomes its own device
below the controller, named after its type (for example "Radiator
thermostat 2"), with its zone as the suggested area. *On a TC100.2 the
eight radiator thermostats appear as separate devices in the right areas,
and their battery and valve state look right.*

- **Read** *(otherwise expected, untested):* "Battery low" (enabled),
  "Radio signal" (disabled by default) and the firmware version; for
  radiator thermostats also the valve opening, the maximum valve opening,
  the temperature at the valve, the valve state and the whisper mode (quiet
  valve motor operation), all disabled by default, and a binary sensor
  "Valve problem" (enabled). It is on while the valve reports that it is too
  tight, that its adjustment range is too big or too small, that it is in an
  error position, or that its adaption waits (for example because the
  thermostat is not mounted correctly); its attribute `valve_problems` names
  the states.
- **Settings** *(expected, untested, disabled by default):* the child lock,
  the temperature offset (only while the device uses it) and the
  sensitivity of the open window detection (high, medium, low). The
  sensitivity offers only the levels that lie within the limits and on the
  step the device reports; with limits that leave fewer than two levels
  there is no selection, and the diagnostics count it as
  `window_sensitivity_too_few_options`. The real limits are not confirmed
  yet.
- **Never written:** the valve calibration and the whisper mode. Adding,
  removing and pairing radio devices stays on the controller.

The device settings are read every 15 minutes.

## Requests to the cloud

The integration keeps the load on the cloud bounded:

- **Discovery** reads at most 512 paths, in batches of at most 30 and at
  most eight levels deep. On a room controller the zone, program and device
  lists are read first. The setting leaves of the radio devices have a cap of
  their own of 192 paths outside the 512: the first leaf of every device
  comes first, then the second, so the cap drops the least important leaves
  of all devices instead of whole devices.
- **HTTP 5xx rule.** These controllers answer HTTP 500 for a path they do
  not have. Such an answer to a catalog or optional probe counts as
  "missing" and does not count towards the three 5xx answers in a row that
  end the discovery. For optional probes and zone detail leaves, an item
  answered with 5xx in a batch is not read again on its own. Roots,
  advertised references and a failing request as a whole keep the outage
  rule. The rule applies only while the cloud answers: a batch of nothing
  but such probes (for example the radio device settings) carries one path
  read before; if neither that path nor any other answers, the 5xx answers
  count as an outage, not as missing parts. A 5xx
  answer below `/events` or `/energy` never ends the discovery. An event
  list the controller names is not taken as missing after a 5xx answer; it
  is searched again after the start.
- **Never read:** the radio key and radio identifier of each device, user,
  installer, password, location and phone data, and the single event
  entries (`/events/evN`, `/events/activeEvent`): the event lists hold the
  events, and the entries are only written. An inventory stored by an
  earlier test version drops them.
- **Energy history:** at most eight requests every three hours.

On the large synthetic test controller with all functions active, the setup
needs 21 batch and 7 single requests, and one hour of regular polling about
150 requests (152 measured). The zone list read every five minutes joins
batches that are sent anyway: it adds about 11 path reads per hour but no
request. See [request behavior](request-behavior.md) for the general
rules.

## Sign-in, local access and older firmware

Sign in with the SingleKey ID of the app in which the controller is set up,
and choose the brand of that app in the setup. Whether this sign-in reads a
CT200 is still to be confirmed; if not, the diagnostics show
`access_reason: scope_or_permission_denied`.

There is **no local access** for these room controllers. A room controller
with older firmware does not appear in the account's device list until its
firmware has been updated and it has been set up in the current app with the
SingleKey ID.

## Deutsch

Raumregler der Plattform RRC 2.0 wie TC100.2 und CT200 sind nur über die
Cloud angebunden; einen lokalen Zugang gibt es nicht. Ich erkenne sie an
ihren Ressourcen, nie an einem Modellnamen.

**Bestätigt** ist an einem TC100.2 bisher:

- mit 0.9.0-alpha.4: das Lesen der Zonen (Raumtemperatur, aktiver Sollwert,
  Zonennamen), das Umschalten zwischen manuellem Betrieb und Zeitprogramm
  (`userMode` `manual`/`clock`) und das Ändern des manuellen Sollwerts;
- mit 0.9.0-alpha.5 (Reglerfirmware 05.04.00, fünf Zonen), jeweils in der
  App des Herstellers geprüft und danach zurückgestellt: Programm wechseln
  und zurück; vorübergehender Sollwert im Zeitprogramm samt Aufheben-Taste
  (Home Assistant zeigte das neue Ziel nach etwa einer Minute); Klima auf
  `heat` mit Ziel und zurück auf `auto` (erst die Betriebsart, dann das
  Ziel); Fenster-offen-Erkennung einer Zone aus und ein; eintägige
  Abwesenheit im Kalender anlegen und löschen; Warmwasser-Betriebsart
  `ownprogram` → `high` → `ownprogram`; Boost ein und aus; Abwesenheit ein
  und aus, dabei wurde ein neues Zonenziel mit der Meldung zur aktiven
  Abwesenheit abgelehnt;
- ebenfalls mit 0.9.0-alpha.5: Die Discovery läuft vollständig durch, und
  jede Zone bekommt ihre Programmauswahl mit den Programmnamen des Reglers;
- Messwerte, die stimmig aussehen: Klima je Zone mit nächstem Sollwert und
  Zonenstatus, Fenstersensoren, Anlagendruck, Außentemperatur,
  Luftfeuchte, Anzeige- und Ursachencode, Boost-Einstellungen,
  Abwesenheitstemperatur, Warmwasser-Betriebsart und -Temperatur; die acht
  Heizkörperthermostate erscheinen als eigene Geräte in den richtigen
  Bereichen, mit Batterie- und Ventilzustand.

Die Funktionen im Überblick; **was oben nicht als bestätigt genannt ist,
ist erwartet, ungetestet**, ebenso der CT200 insgesamt:

- **Zonen:** vorübergehender Sollwert bis zum nächsten Schaltpunkt mit
  Aufheben-Taste, nächster Sollwert, Minuten bis dahin, Zonenstatus,
  Ventilöffnung, optimierter Start, Fenster offen (aus der Sonderfunktion
  der Zonenliste, sonst aus dem Fensterstatus der Zone),
  Fenster-offen-Erkennung (der Zonenschalter nur, solange der Regler die
  Erkennung des Systems anbietet). Die Taste **Nächsten Schaltpunkt
  vorziehen** setzt den vorübergehenden Sollwert auf den nächsten Sollwert,
  nur im Zeitprogramm und nur mit einem nächsten Sollwert, den der
  vorübergehende Sollwert annehmen kann; das Attribut
  `next_setpoint_advanced` zeigt das an. Nach einer bestätigten Änderung
  einer Zone lese ich aktiven Sollwert, Zonenstatus, nächsten Sollwert, die
  Minuten bis dahin und die Zonenliste etwa fünf Sekunden später einmal
  nach, mehrere Änderungen hintereinander teilen sich diese eine Lesung.
  Auch der vorübergehende Sollwert folgt den Kennzeichen „genutzt“ und
  „verfügbar“; ob ein Regler ihn im Zeitprogramm als nicht genutzt meldet,
  ist offen (der TC100.2 sendet dafür keine Kennzeichen). Bei aktiver
  Abwesenheit lehne ich einen
  neuen Sollwert und das Aufheben ab, die Betriebsart bleibt änderbar; eine
  Zone mit Zonen- oder Kommunikationsfehler oder im Standby nimmt keine
  Änderung an. Das Attribut `control_lock` nennt die Sperre,
  `active_setpoint` den Sollwert, den der Regler anwendet; bei Sperre,
  offenem Fenster oder Boost zeigt die Zone ihn als Ziel. Ohne nutzbare
  Raumtemperatur bleibt die Zone bedienbar.
- **Zeitprogramme:** Programmauswahl je Zone und Ändern vorhandener
  Schaltpunkte unter **Konfigurieren → RRC-Zeitprogramme**.
- **Abwesenheit:** Schalter und Abwesenheitstemperatur; Kalender
  „Abwesenheiten und Sondertage“ mit ganzen Tagen zwischen 2016 und 2099,
  Abwesenheiten anlegen, ändern und löschen, Wochentagstermine nur löschen;
  ist eine Terminliste kurz nicht lesbar, bleiben ihre Termine aus der
  letzten gültigen Lesung sichtbar (Attribut `stale`), geändert wird dann
  nichts; Boost (Schalter, Temperatur, Dauer; Räume am Regler): Temperatur
  und Dauer gehen nur zusammen mit dem Start hinaus, bei ausgeschaltetem
  Boost merke ich sie als Vorgabe für den nächsten Start, bei laufendem
  Boost wird eine Änderung abgelehnt; ein Start heizt nur Räume, die es noch
  gibt und die der Regler für den Boost erlaubt; Kaminfunktion.
- **Warmwasser:** Betriebsart, Komforttemperatur, Extra-Warmwasser mit
  Dauer, thermische Desinfektion (Startzeit in 5-Minuten-Schritten),
  aktuelle Programmstufe. Ohne angebotene Liste biete ich Aus, Komfort und
  Eigenes Programm an, solange der Regler Eco oder Eco+ meldet auch diese
  beiden; meldet der Regler als Wärmeerzeuger eine Wärmepumpe oder ein
  Hybridsystem, immer alle fünf, sodass Eco nach Komfort wieder wählbar
  ist; eine leere Liste lässt die Betriebsart nur lesbar. Extra-Warmwasser
  schaltet nicht mit der Dauer 0 ein.
- **Heizkreis:** Sommer-/Winterumschaltung und Nachtabsenkung mit ihren
  Schwellen (standardmäßig deaktiviert), Saisonbetrieb (nur mit mindestens
  zwei angebotenen Betriebsarten und wenn die aktuelle darunter ist) und
  Sollwertoptimierung als alltägliche Einstellungen standardmäßig aktiviert,
  Betriebssaison; Heizkurve, Vorlaufgrenzen und Raumeinfluss nur für
  Fachkundige und standardmäßig deaktiviert. Fußpunkt und minimale
  Vorlauftemperatur bleiben nicht über Endpunkt und maximaler
  Vorlauftemperatur. Diese Einstellungen wirken nur bei witterungsgeführter
  Regelung; sonst wird eine Änderung abgelehnt. Die Aufheizgeschwindigkeit
  ist vom Hersteller abgeschaltet und wird nie geschrieben.
- **System:** Anzeige- und Ursachencode, Anlagendruck, Raumluftfeuchte,
  verriegelnde Störung (bis zum manuellen Reset), blockierende Störung,
  Wartung fällig, Nachfüllen erforderlich; eigene Texte für
  Funkgerätestörungen; Klasse 18/20 als Wartung; Warnungen und Wartung
  optional in der Glocken-Benachrichtigung; eine Störungszeit ohne
  Zeitzone gilt als Ortszeit in der Zeitzone von Home Assistant; Meldungen
  für Fachleute oder
  Installateure (Anzeigeebene 0, 2 oder 4) nur unter „Aktive Meldungen“,
  siehe [Störungen](faults.md#display-levels-on-room-controllers).
- **Energiehistorie:** Tageswerte als Statistiken mit dem Zusatz
  „(kWh erwartet)“, weil die Einheit nicht bestätigt ist; Gas nur mit
  bestätigter Einheit; die älteren Schlüssel `eCH`/`eHW` je nach
  Wärmeerzeuger als Wärmepumpenstrom oder als Gas; meldet der Regler seinen
  Wärmeerzeugertyp mit einem Serverfehler, warten diese Tage bis zu 24 Stunden
  ab dem ersten Fehler (auch über einen Neustart) und jeder Lauf liest den Typ
  erneut, erst danach kommen sie ohne diese beiden Werte; die Statistik-IDs
  enthalten statt der Gateway-ID ein Kürzel, das mit der Instanz-ID von Home
  Assistant als Schlüssel gebildet wird (HMAC-SHA256) und nach erneutem
  Hinzufügen und nach einer wiederhergestellten Sicherung gleich bleibt;
  jeder Lauf liest ab dem
  zuletzt gelesenen Tag erneut; ein neu begonnener Import setzt vorhandene
  Statistiken fort; Auswahl im Energie-Dashboard selbst, Löschen unter
  **Entwicklerwerkzeuge → Statistiken**; alle drei Stunden höchstens acht
  Anfragen.
- **Funkgeräte:** Batterie, Funksignal, Firmware, Ventilwerte und
  Flüstermodus (nur lesen), Ventilproblem (auch bei wartender
  Ventiladaption); Kindersicherung, Temperatur-Offset und Empfindlichkeit der
  Fenster-offen-Erkennung (standardmäßig deaktiviert; nur die Stufen, die
  Grenzen und Schrittweite des Geräts erlauben). Ventilkalibrierung und
  Flüstermodus werden nie geschrieben.

Vor jeder Änderung einer Raumregler-Einstellung lese ich den Wert, seine
Grenzen und seine Kennzeichen frisch vom Regler; schlägt das fehl, wird
nichts gesendet und die letzte Lesung bleibt. Jede Änderung wird genau
einmal gesendet und nach 1, 2, 4 und 8 Sekunden zurückgelesen; bleibt sie
unbestätigt, wird der Wert mit dem nächsten Zyklus (innerhalb einer Minute)
erneut gelesen.

Eine Einstellung, die der Regler als **nicht verfügbar** meldet, bekommt
keine Steuerung (eine bestehende Entität wird nicht verfügbar). Eine
**verfügbare, aber nicht genutzte** Einstellung bleibt mit ihrem Wert
sichtbar; eine Änderung wird ohne Senden mit einer eigenen Meldung
abgelehnt. Die Heizkreis-Einstellungen hängen nicht an „genutzt“; eine
genutzte, aber nicht verfügbare bleibt sichtbar und gesperrt. Ausnahmen sind
der Saisonbetrieb und die Sollwertoptimierung: Sie folgen den beiden Regeln
davor. Messwerte
betrifft das nicht. Auch das ist erwartet, ungetestet.

Die Discovery liest höchstens 512 Pfade, die Einstellungen der Funkgeräte
haben einen eigenen Deckel von 192 Pfaden. Eine HTTP-500-Antwort auf eine
Katalog- oder Zusatzabfrage gilt als „fehlt“ und beendet die Discovery
nicht, solange die Cloud antwortet: Ein Abruf, der nur solche Abfragen
enthält, nimmt einen bereits gelesenen Pfad mit; antwortet keiner, gelten die
500-Antworten als Ausfall. Die einzelnen
Ereigniseinträge (`/events/evN`, `/events/activeEvent`) werden nie gelesen.
