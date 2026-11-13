# Changelog

All notable changes will be documented here. The project follows Semantic
Versioning after its first tagged preview.

## [Unreleased]

- **Release tooling:**
  - The release notes of a version must stand on their own. Publishing stops
    when the CHANGELOG section sends readers to the notes of another version
    further down, because the GitHub release and the update dialog show only
    this one section.
  - The installation archive lists its files in the plain order of their
    paths, so it comes out the same on every platform, and a local build
    refuses to run while the integration directory has uncommitted changes.
  - Only an annotated tag publishes a release, and a stable release only from
    a commit on `main`. The workflow now sets the draft and pre-release state
    explicitly, so an existing draft is published with the right type, and
    two runs of the same tag no longer publish at the same time.
  - I tested these checks with automated tests only; the workflow changes
    take effect with the next release tag.

#### Short cloud outages

On my own plant, two short cloud outages under alpha.6 made entities
unavailable that did not need to be. I replayed both with simulated polling
cycles on the inventory of my plant; the changes below are tested that way,
not yet on the plant itself.

- **Live values no longer end seconds before the cloud answers again:** a
  cloud reading stays valid for three intervals of its group, extended by a
  cycle duration. This is now the longest cycle since the reading was last
  received, not the last one. After a slow failed cycle (10.6 s) and a quick
  one, a live value such as the compressor status ran out 8 s before the
  next answer; now it stays shown.
- **Fault entities survive a short outage:** **System fault**, **Active
  faults** and **Active notifications** keep the last state that a complete
  read confirmed, for 15 minutes after that read (3 minutes while a
  notification is active), as long as the requests only get no answer.
  Nothing is resolved or announced meanwhile. The fault list is read again in
  the first cycle that answers, after the other readings. A gateway timeout
  of the fault list no longer pauses it for 15 minutes when nothing else
  answered either; after a two-minute outage the fault entities were
  unavailable for 17 minutes. A fault list that is slow while the rest
  answers is still paused as before.
- **Display values keep their last value:** **System pressure status
  (calculated)**, **Holiday mode active**, **Next holiday** and the **Holiday
  periods** calendar now keep their last value within its validity limit when
  their readings get no answer, like other sensors. Holiday changes in the
  calendar still need current readings and a successful cloud cycle. The
  away mode switch stays a control and still needs a current reading.
- **Warning for server errors inside an answered batch:** when the cloud
  answers a batch request but readings in it come back with server errors
  (for example gateway timeouts), the log now shows one warning per outage.
  Before, such outages left no log entry.
- **Diagnostics schema 46:** each access and each source lists its recent
  starts with the time and a fixed reason (`start`, `configuration_changed`,
  `after_failure` with the error class, `source_ended`, `access_restarted`,
  `requested`, `other`), so the cause of an unexpected restart of the local
  or the cloud source can be seen in the next diagnostics. The fault section
  shows `state_held`.

### Room controllers and start counters

- **Start counters per heat source on a TC100.2:** the counters of `hs1`
  and `hs2` come as a list with one entry (total, heating, hot water) on
  that controller, which the integration did not read. A list of up to
  eight entries is read now, so total, heating and hot water starts get
  values. I have tested this only with simulated data in the reported form.
- **Missing start counter parts disabled:** a part of a start counter that
  the reading does not contain when its entity is first created, for
  example cooling starts on a gas boiler, is now created disabled, like the
  pool starts. Existing entities keep their setting; you can disable such a
  sensor yourself.
- **Switch points of a TC100.2 editable:** the week programs of that
  controller have an extra whole-number field `name` on each switch point,
  and its day programs have no weekday field, so the switch point dialog
  stayed read-only. The integration now accepts exactly this form and
  nothing else: a change is sent as day, time and temperature for every
  point of that day, without `name`, and before and after the change only
  day, time and temperature are compared. Reading the form is tested with
  simulated data in the shape the diagnostics reported; writing it is
  expected, untested on a real controller, and I would be glad about a
  report. Any other extra field, or a `name` that is not a whole number,
  keeps the program read-only (new diagnostics code `optional_key_type`).
- **Diagnostics:** `rrc_programs.schedules` lists the form of the switch
  points per program kind (key names and value types only, never a value);
  the boost settings report the last reason they had no limits and how long
  ago it was seen; `rrc_profile.confirmation` now matches the confirmed
  functions instead of always saying `unconfirmed`; and the energy history
  names why a cycle without an error imported nothing (`no_records`,
  `no_completed_days`, `all_series_empty`) and counts the daily records it
  read, without values.
- **Advance next switch point confirmed:** the button is now listed as
  confirmed on a TC100.2 in the diagnostics and in the room controller
  documentation. Thank you, @obvius1, for the diagnostics in
  [#122](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/122)
  that showed the counter and program forms, and for checking the button on
  your controller.

## [0.9.0-alpha.6] - 2026-11-12

> [!IMPORTANT]
> **Alpha pre-release for testing.** Make a full Home Assistant backup first.
> From alpha.1 to alpha.5 no new setup is needed; from 0.8.x, the notes of
> alpha.1 below apply as well.

Alpha.6 confirms most room controller functions on a real TC100.2, adds an
editor for the weekly heating and hot water schedules of EMS and iCom
gateways, a load profile for the slower iCom gateways, a read-only firmware
entity and a way back in the menus of the Configure dialog, and enables more
readings of the local access by default. It also brings the fixes of a review
of the whole 0.9 code. What a TC100.2 confirmed is marked as confirmed below.
The schedule editor, the iCom changes, the firmware entity and most fixes I
have tested only with simulated data; they are expected to work but untested
on a real device, and I would be glad about reports.

### What changed since alpha.5

#### Entities

- **Fan and pump speeds enabled:** the readings of the local access for the
  outdoor unit fan speed (PL3) and the primary heating pump speed (PC0), in
  %, are now enabled by default, as promised in
  [#106](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/106).
  On an existing installation they are enabled at the next start, unless
  you disabled them yourself. The compressor speed and the controller
  signals that repeat these readings stay disabled.
- **Heat pump temperatures enabled:** also enabled by default are the
  readings of the local access a hydraulic view of the heat pump needs: the
  primary supply temperature (TC1), the air intake temperature (TL2), and the
  hot gas (TR6), condenser heating (TR3), high-pressure (JR1), low-pressure
  (JR0) and suction line (TR5) temperatures. Existing installations get them
  at the next start unless you disabled them; the entity IDs stay. The
  compressor temperature, the condenser cooling temperature (TR4) and the
  compressor speed, which practically repeats the compressor modulation,
  stay disabled.
- **Confirmed installations:** the Logatherm WLW186i-12 E with the outdoor
  unit WLW MB-5 AR, a Logamatic BC400 and an MX400 gateway now also lists
  the local access as confirmed, in the README and on the
  [separate page](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.6/docs/confirmed-installations.md).
  Thank you, lanebe, for confirming it in
  [#106](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/106).

#### Configure dialog

- **Back and stay in the dialog:** every menu below the main menu of
  **Configure** now ends with "← Back" to the menu above it, and
  **Connections** is a menu of its accesses. A finished action, for example
  a saved connection setting, saved notifications, a saved holiday or
  schedule or a planned search, returns to its menu with the result instead
  of closing the dialog, so several things can be done in one session. The
  settings are saved at once, as before. Signing in again, adding a cloud
  access, the local setup and error messages end the dialog as before. In
  all languages except German and English the new texts are drafts.

#### Room controllers (TC100.2)

Newly confirmed on a real controller, a TC100.2 with firmware 05.04.00 and
five zones, tested with alpha.5. Each change was checked in the
manufacturer's app and set back afterwards:

- **Programs:** the discovery completes, and every zone gets its program
  select with the program names from the controller. Switching the program
  and back works.
- **Temporary setpoint:** a new target in the schedule mode sets it, and the
  clear button removes it again. In that test Home Assistant showed the new
  target after about a minute.
- **Mode with a target:** the climate to `heat` with a target and back to
  `auto` works; the mode changes first, then the target.
- **Open window detection:** turning it off and on for a zone works.
- **Absence calendar:** creating a one-day absence and deleting it again
  works.
- **Hot water:** the mode `ownprogram` → `high` → `ownprogram` works.
- **Boost:** on and off works.
- **Away mode:** on and off works. While it was on, a new zone target was
  refused with the message that the away mode is active.
- **Readings that look right:** the climate of each zone with its next
  setpoint and zone status, the window sensors, the system pressure, the
  outdoor temperature, the humidity, the display and cause code, the boost
  settings, the away temperature, and the hot water mode and temperature.
  The eight radiator thermostats appear as separate devices in the right
  areas, with their battery and valve state.

The room controller page, the confirmed installations and the README now
show this per function. Editing switch points, changing an absence,
special days, the fireplace function, the system's open window detection,
the zone locks other than the away mode, the other hot water settings, the
heating circuit, the fault and maintenance sensors, the energy history, the
settings of the radio devices and the CT200 stay expected, untested. Thank you, @obvius1, for
testing all of this on your TC100.2, checking every change in the app and
reporting it in #122.

#### Room controllers: new and changed

Everything in this block is expected, untested on a real controller. I
tested it with simulated controller data only.

- **Advance next switch point:** a new button per zone sets the temporary
  setpoint to the zone's next setpoint, so the zone heats up or lowers its
  temperature now instead of at the next switch point. It is available only
  in the schedule mode with a next setpoint that the temporary setpoint can
  take. The mode and the next setpoint are read again before the change;
  if the next setpoint changed on the controller, nothing is sent. The
  attribute `next_setpoint_advanced` shows it.
- **Values after a zone change:** after a confirmed change of a zone, the
  active setpoint, the zone status, the next setpoint, the minutes until it
  and the zone list are read once about five seconds later, not only with
  the next cycle about a minute later. Several changes in a row share this
  one read; pauses and the protection pause still apply.
- **Hot water on a heat pump:** if the controller reports its heat source
  as a heat pump or a hybrid system, the hot water mode always offers all
  five modes, so eco can be chosen again after comfort.
- **Diagnostics:** the functions confirmed on the TC100.2 are now named as
  confirmed for their function group; everything else stays unconfirmed.
  Start counters with a form the integration cannot read describe that
  form without values, the boost settings say why they have no limits, the
  active temporary setpoints count only zones in the schedule mode, and the
  holiday block points to the room controller's absence report.
- **Documentation:** the room controller page says that the temporary
  setpoint follows the used and available flags like every other setting;
  whether a controller reports such flags for it is still open.

#### Room controller programs

- **Why a program is read-only:** on a real TC100.2 the program editor showed
  every program read-only, without saying why. The diagnostics now name the
  reason per week and day program, for example a switch point without a day
  or with an additional key, as fixed codes without values. The editor still
  accepts only the form it knows; I will adjust it once a diagnostics file
  shows the real form.

#### Heating and hot water schedules

Expected, untested on a real gateway; so far I have tested both only with
simulated data:

- **Change a schedule:** **Configure > Heating and hot water schedules**
  changes the weekly programs A and B of a heating circuit and program A of
  the hot water. Choose a day, then change, add or delete its switch points
  within the limits the gateway reports, or copy a day onto other days. The
  last step shows the changed days before and after; the whole week is then
  sent once, read back and shown at once. A program that changed in the
  meantime is not overwritten, and nothing is ever sent twice. The program
  type and the program names stay read-only. The program type is read again
  right before sending, so an absolute program is never written as a level
  program; a program whose type has no current reading, or that the gateway
  reports as not used, is not offered. An unreadable answer to an accepted
  request is resolved by reading back. A refusal names its cause: writing
  paused after failed readings, a changed program type or a rule of the
  draft. Program names and level names in the dialog texts show as plain
  text.
- **Active schedule:** a select switches between programs A and B, but only
  where the gateway allows both and both programs can be read. With a
  single program, as on my heat pump, there is none.

#### iCom gateways (KM200, KM100, KM50, MB LAN 2 and others)

These changes apply only to gateways that report the device type `icom`;
every other gateway keeps its behaviour. I tested them with simulated data in
the shapes of a real iCom gateway with a boiler controller. On a real iCom
gateway they are expected, untested.

- **Load profile:** a new cloud connection with an iCom gateway starts with
  the cloud-friendly profile. An existing connection with an iCom gateway
  is switched to it once at the first start of this version, noted once in
  the log at info level. It can be changed in the connection options, and
  a choice made there is never switched again.
  After a cycle with a timeout the next one reads batches of half the size,
  at least 10 paths, and after three good cycles in a row the size doubles
  back step by step to 30. During a long outage the breaker pauses 5, 10, 20
  and then 30 minutes, and starts at 5 minutes again after the first good
  cycle. Writes are read back after 1, 2, 3 and 4 seconds. The timing comes
  from the diagnostics and logs in
  [#104](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/104)
  and [#105](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/105);
  thank you, [gejomo](https://github.com/gejomo), for them.
- **Holidays:** before the first change I now check that every field to be
  changed is writable. A fixed temperature reported in `°C` is accepted like
  `C`. Two new rules from the component list only remove choices: with a
  HMI800 HP Light controller the heating mode and the circuits of a holiday
  stay as the slot reports them, and with token 16 in the component list the
  heating circuits of the tokens 24 to 27 are not offered. I have not seen
  either case on a system; both are expected, untested.
- **Control type `constants`:** the heating circuit climate then only shows
  the circuit and sends no command, on every gateway that reports it. A
  missing or old reading keeps it so until a reading shows another control
  type. Expected, untested on a real device.
- **Diagnostics:** a new `icom` section shows the state of the load profile,
  the component type numbers of `/system/info` (no serial numbers or names),
  the reported bus with a hint for `EMS1_0`, `CAN` and `No_Bus`, and whether a
  holiday rule applies.
- **Inventory:** the [resource inventory](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.6/docs/resource-inventory.md#icom-rows-r199-to-r243-compared-with-a-real-gateway)
  now lists the iCom paths a real gateway offers beyond the rows R199 to R243
  and the rows it does not answer, and a synthetic fixture holds these shapes
  with constructed values.

#### Firmware update

- **Firmware entity:** each cloud gateway gets an update entity
  **Firmware**. It shows the installed gateway firmware and an update the
  cloud offers, with a link to its release notes in the language of Home
  Assistant. It only shows: I left out installing an update and changing the
  gateway's automatic update, so nothing can start a firmware update from
  Home Assistant. The list is read about ten minutes after the start and then
  every six hours; a gateway the list refuses or does not name gets no
  entity. An update counts as offered only with a version other than the
  installed one; the state `none` offers nothing and shows only in the
  `update_state` attribute. After a restart, the entity shows the last
  answer it kept, at most 13 hours old, until the first new read instead of
  being unavailable. I tested it with simulated answers of the cloud's
  firmware update list only; it is expected, untested on a real gateway.
- **Diagnostics:** the new section `firmware_updates` shows the result of the
  last reads, the state and the versions, without the gateway ID or links.

#### Robustness, privacy and texts from the review of 0.9

All of the following is tested with synthetic data only, not on a real
device.

- **Newer firmware:** an added field in the rows of the local signal list,
  the system information or a fault list no longer makes the whole answer
  invalid; required fields and types stay strict.
- **On/off signals as 1.0/0.0:** an on/off signal reported as a float value
  now shows on or off instead of unknown.
- **p95 response time:** the value never exceeds the measured maximum and
  no longer stops at exactly 10000 ms; answers up to the 30-second timeout
  keep their size.
- **Short token lifetimes:** a token answer with a lifetime of 0 or less
  keeps its new refresh token, so the sign-in is not lost; its access token
  counts as expired at once and is renewed once more right away. If that
  answer has no lifetime either, the usual wait after a failed renewal
  applies (30 seconds, doubling up to 10 minutes). A short lifetime no
  longer starts a token renewal before every request.
- **Start values answered as missing:** a registered value that keeps
  answering 403, 404 or 406, for example of a replaced room device, is
  given up after five answers until the next discovery instead of being read
  every 30 minutes for good.
- **Holiday fields:** of several synonyms in a holiday row, the same one is
  now always used, also after a restart.
- **Cloud session:** if a Home Assistant update ever hides the middleware
  chain of the shared HTTP session, cloud requests stop with a clear error
  instead of an unexpected one, and a test notices it early.
- **Privacy:** log lines hide every identifier the diagnostics hide; the
  stored discovery inventory keeps no serial numbers, UUIDs or module data,
  also from older files; a device keeps its known serial number, model and
  software and hardware versions after a start from that inventory.
- **Dialog texts:** holiday names, program names, the gateway model, the
  local address and the gateway ID in dialog descriptions are shown as plain
  text, so a name can no longer add an image or a link. Gateway IDs `.` and `..` are rejected before any
  request.
- **Local setup messages:** more failure causes of the local setup are named
  instead of "unexpected error", for example a system that already has a
  local access, an unverified identity or a full setup journal.
- **Deleting old devices:** a device without any entity, for example a
  heating circuit removed at the controller, can now be deleted on its
  device page. A device that still has an entity, also a disabled one or
  one that is no longer provided, stays until its entities are deleted, so
  no setting of an entity gets lost.
- **Status in the dialogs:** the controls dialog shows "1 unsaved change"
  in the singular, says "No changes, nothing was written." when Apply had
  nothing to write, and asks to close and reopen the dialog when Home
  Assistant no longer knows it (for example after a restart); the holiday
  delete button does the same.
- **Documentation:** the entity page uses the current names of the local
  readings and lists VR0, the valve positions, the compressor start delay
  and the signal entities; it explains that on/off signals are enum sensors
  and that signals no longer listed keep their entity. The pause limits,
  the climate validity, the active entry version 4.2, the version notes and
  the load of the energy history are now described as the code behaves.
- **Tests:** three date-dependent tests run at a fixed time, and every test
  has a time limit of 120 seconds (`pytest-timeout` is now a test
  requirement).

#### Diagnostics

- **Diagnostics schema 45** gathers the new sections of this version:
  `firmware_updates`, `icom` (only for iCom gateways), `switch_program_writes`,
  the reasons why a room controller program stays read-only and the
  confirmation per function group, without values, names or identifiers. See
  [diagnostics](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.6/docs/diagnostics.md).

#### Translations

- **New texts are drafts:** the texts added in this version (the schedule
  editor and the active schedule select, the advance button, the firmware
  entity, the dialog status texts and the back entries and result notes of
  the Configure dialog) are written in English and German; in all other
  languages, Czech and Slovak included, they are drafts and not yet
  reviewed.

#### Fixes

- **Writes with a slow cloud:** a change that has started now has an overall
  deadline of 60 seconds (180 seconds for a holiday change, 200 seconds on
  an iCom gateway, which is read back more slowly) for its checks, its
  request and the read-back. Before, a slow cloud with request retries
  could hold the polling of the gateway and the calling automation for
  several minutes. Right before each request the time left is checked: if
  it no longer covers the request and its read-back, nothing is sent and
  the change is reported as busy, so a slow check can no longer cut a
  request off while it is sent. A change stopped before its request keeps
  its value as it is. At the deadline after a request the change is
  reported as not confirmed, its value counts as not current until it is
  read again, and nothing is sent a second time.
- **Warning after a restart:** Home Assistant no longer logs "Detected
  blocking call to import_module … inside the event loop" for the room
  controller functions. I now load their modules in the background before
  the entity platforms start. The warning was reported in #122.
- **Room controller fault times:** a fault time without a time zone is now
  read as local time in Home Assistant's time zone instead of as UTC, so
  the fault no longer shows hours off. Expected, untested.
- **Room controller heating circuit settings:** after a failed reading of
  the circuit's control strategy, a change of a heating curve or supply
  setting is no longer refused as "changed" until the next poll. The
  limits it is ordered against are still checked with a current reading.
- **Room controller absence calendar:** a changed event no longer counts as
  confirmed while the list it came from cannot be read.
- **Room controller program names in Configure:** the room controller
  heating schedules in the Configure dialog are listed with the program
  names from the controller, as in the zone program select, instead of only
  their numbers.

## [0.9.0-alpha.5] - 2026-11-11

> [!IMPORTANT]
> **Alpha pre-release for testing.** Make a full Home Assistant backup first.
> From alpha.1 to alpha.4 no new setup is needed; from 0.8.x, the notes of
> alpha.1 below apply as well.

Alpha.5 brings the functions of the room controllers of the RRC 2.0 platform
(TC100.2 and CT200) beyond the zone readings, clearer fault and maintenance
notifications, and texts in 19 languages. On a real room controller, a
TC100.2 has so far confirmed the zone readings, switching a zone between
manual mode and the schedule, and changing the manual setpoint. Everything
else for room controllers I have tested only with simulated data, in the
shapes of the observed TC100.2 answers and of a constructed controller with
11 zones, 12 radio devices and 24 programs. It is expected to work but
untested on a real controller; I would be glad about reports from owners.
The new [room controller page](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.5/docs/room-controllers.md) lists every
function with its status.

### What changed since alpha.4

#### Room controllers (TC100.2, CT200)

Confirmed on a real controller:

- **Zone climate:** every zone that reports a heating mode and a room
  temperature now gets a climate entity. `heat` is the manual mode and
  `auto` the schedule; room controllers have no off mode. In manual mode a
  new target changes the manual setpoint. A mode together with a temperature
  changes the mode first and the target only once the controller has
  confirmed the mode. The mode and manual setpoint changes behind it were
  confirmed on a TC100.2 with alpha.4; the climate entity itself is new.
- **Confirmed installations:** the Logano plus GB212 with a Logamatic TC100.2
  is listed in the README and on a
  [separate page](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.5/docs/confirmed-installations.md) with the zones confirmed
  with 0.9.0-alpha.4 without a local patch: room temperature, active
  setpoint, zone names, operating mode and manual setpoint changes from Home
  Assistant. Thank you, @obvius1, for testing this on your system and
  reporting it in #113.

Expected, untested on a real controller:

- **Temporary setpoint:** in the schedule mode a new target sets a temporary
  setpoint until the next switch point of the schedule; the attribute
  `override_until_next_switch_point` says so. A button per zone clears it.
  If the controller ends a replaced temporary setpoint at once, for example
  at a switch point, a message of its own says so. In manual mode the
  attributes no longer report a temporary setpoint, and the clear button is
  only offered where the controller accepts 0.
- **Locked zones:** while the away mode is active, I refuse a new target of
  a zone with a message that names the away mode, instead of sending it. A
  zone the controller reports with a zone or communication error, or in
  standby, takes no change of its mode or target. The attributes
  `control_lock`, `away_mode` and `active_setpoint` show why, and the target
  shown is then the setpoint the controller applies, as also with an open
  window or a boost.
- **Zone climate:** a zone whose room temperature the controller does not
  use stays available with an unknown current temperature. After an
  unconfirmed change the target control stays until the value is read
  again.
- **Zone details:** sensors for the next setpoint (unknown instead of 999),
  the minutes until it, the zone status, the valve opening and the optimum
  start, a binary sensor for an open window, and switches and numbers for
  the open window detection of each zone and of the system. The valve
  opening, the optimum start and the system settings are disabled by
  default. The window sensor now follows the special function the zone list
  reports for each zone, and a zone's open window detection is only changed
  while the system's detection is offered.
- **Programs:** the program selection keeps the program numbers as its
  options, so automations keep working when a program is renamed; the names
  appear as the attributes `program_names` and `program_name`. A selection
  and a changed switch point are now read back with the same patience as
  every other room controller change and are counted in the diagnostics. A program day
  reported as `dayProgram` is read, and its switch points can be edited like
  those of a week program.
- **Away mode, absences and special days:** the away mode is a switch and
  the away temperature a number. The controller's events appear as a
  calendar of whole days in the controller's time zone; absences can be
  created, changed and deleted there, days that run like another weekday
  only deleted. Names have at most 32 bytes, events lie between 2016 and
  2099 and may not overlap, not even on their first or last day. Every
  change reads the lists first and is sent once; until an unconfirmed change
  shows, the next one is refused, so an event is never created twice. A new
  event is recognised by its name, dates and type, also if the controller
  numbers its events anew, and a list that cannot be read for a moment no
  longer leaves a change of another list unconfirmed. If a list cannot be
  read for a moment, its events stay in the calendar from the last valid
  reading, so a running absence does not end in Home Assistant; the
  attribute `stale` shows it. An event list that got no answer at the start
  no longer counts as missing: it is searched again, and until it reads, the
  calendar changes nothing and says so with a message of its own.
- **Boost and fireplace function:** the switch writes the controller's
  boost settings as a whole. I send the boost temperature and duration only
  together with the start: while the boost is off, a new value is kept for
  the next start (also across a restart); while it runs, a change is refused.
  A start heats only the rooms set on the controller that still exist and
  that the boost may heat; without such a room the boost is refused. The
  fireplace function is a switch, disabled by default.
- **Hot water:** the operating mode as a select and a water heater entity,
  the comfort temperature, extra hot water with its duration, the thermal
  disinfection (switch, weekday, start time in five-minute steps and last
  result, disabled by default) and the current level of the hot water
  program (disabled by default). Without a list of modes from the
  controller I offer off, comfort and own program, and eco and eco+ as well
  while the controller reports one of them, so a hot water mode set to eco
  elsewhere keeps its control; an empty list leaves the mode a reading.
  Extra hot water does not turn on with a duration of 0, and in Operate
  appliance the duration is written first. A hot water mode the controller
  reports as not available gets no water heater entity.
- **Heating circuit:** summer/winter switching and night setback by outdoor
  temperature with their thresholds (disabled by default), the season mode
  (only with at least two offered modes and while the current mode is one
  of them), the setpoint optimization and a sensor for the operating
  season. The season mode and the setpoint optimization are everyday
  settings and enabled by default. The heating curve, the supply
  temperature limits and the room influence are disabled by default; enable
  them only if you know these settings. The base point and the minimum
  supply temperature stay at or below the end point and the maximum supply
  temperature; before such a change I read these limits again and send
  nothing if one of them changed. In Operate appliance several limits can be
  changed in one step. All of these settings act only with
  weather-compensated control; with another control a change is refused
  with its own message. The heat-up speed is switched off by the
  manufacturer and never written. Installer settings are never written.
- **Operate appliance:** a locked or unused room controller setting and a
  refused value now get their own message instead of "A setting has
  changed".
- **System readings:** display code, cause code, system pressure and its
  status, indoor humidity, and binary sensors for a locking fault (locked
  out until a manual reset), a blocking fault, maintenance due and refill
  needed. The maintenance request text, the controller's own temperatures
  and the firmware update state are disabled by default.
- **Energy history:** the controller's daily energy values are imported
  into the Home Assistant statistics, for the Energy dashboard. Their names
  end with "(kWh expected)" because the unit kWh is assumed but not
  confirmed; gas values follow only when the controller confirms the unit.
  You choose the statistics in the Energy dashboard yourself and can delete
  them under Developer tools → Statistics. Every run reads again from the
  last day already read, so a running day, a day without a record or a full
  history on the controller costs no day. The older keys `eCH`/`eHW` count
  as heat pump electricity only on a heat pump or hybrid system, otherwise
  as gas under the gas rule. While the controller answers its heat source
  type with a server error, these days wait for up to 24 hours, also across
  a restart, and every run reads the type again, so a short fault costs no
  value; only after that do I import them without these two values instead
  of assuming gas. If the import starts over, the statistics continue from
  their last recorded value instead of starting at zero. The statistic IDs
  contain a token of the gateway keyed with the instance ID of your Home
  Assistant instead of its ID, so the ID cannot be found back from the
  database or an export; the token stays the same when the integration is
  added again or a backup is restored.
- **Radio devices:** every device in the controller's device list becomes
  its own device below the controller, named after its type (for example
  "Radiator thermostat 2") with its zone as the suggested area. It shows
  "Battery low", "Radio signal" (disabled by default) and its firmware; a
  radiator thermostat also its valve readings and whisper mode (disabled by
  default) and a "Valve problem" binary sensor. The child lock, the
  temperature offset and the open window sensitivity can be changed and are
  disabled by default. The valve calibration and the whisper mode are never
  written. "Valve problem" also turns on while a valve adaption waits, and
  the sensitivity offers only the levels its limits and step allow.
- **Settings the controller does not offer or use:** a setting the
  controller reports as not available gets no control, and an existing one
  becomes unavailable. A setting that is available but not used, such as
  the open window detection of a zone, stays visible with its value; a
  change is refused before anything is sent, with a message that names it.
  The heating circuit settings keep their earlier rule, except the season
  mode and the setpoint optimization, which follow this one. Before every
  change of a room controller setting I read it from the controller once
  more, so
  a lock that came after the last poll is found; a value changed in the
  meantime no longer blocks the change, and a failed read sends nothing and
  keeps the last reading. For the boost only the mode and the rooms must
  still be as shown.
- **Unconfirmed changes:** a change that the controller has not confirmed
  yet is read again with the next cycle, within a minute, so a zone's target
  temperature control is back quickly. A temporary setpoint that the
  controller ended at once keeps its valid reading.
- **Recognised by their resources:** a gateway the cloud reports as device
  type `rrc2`, with the product type `RRC 2.0` or with RRC zones is handled
  as a room controller, never by a model name. A controller without a name
  is called "Room controller" and shows the reported product type as its
  model. The CT200 is listed as expected but untested on the
  [compatibility](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.5/docs/compatibility.md) page.
- **Privacy:** the device arrays with the radio key (`dlk`) and radio
  identifier (`sgtin`) of each device, the product lookup, the user,
  installer, password, location and phone resources are never requested,
  stored or exported, on any gateway. A discovery inventory stored by an
  earlier release drops them when it is loaded.
- **Older firmware:** the setup's "no devices" message explains that a room
  controller with older firmware only appears once its firmware has been
  updated and it has been set up in the current app with the SingleKey ID.
- **Diagnostics:** room controllers get the sections `rrc_profile` (profile,
  firmware, numbers of zones, programs and devices, access reason),
  `rrc_programs` (why each zone offers the program selection or not, with
  codes such as `program_list_missing`) and `rrc_features` (read-back timing,
  value-free write counters and counts per function, the state of the energy
  history import). The discovery diagnostics separate single refused paths
  (`some_paths_forbidden`) from a sign-in without the rights for the
  controller (`scope_or_permission_denied`) and count the paths deliberately
  not read. None of this contains names or values, and everything is marked
  `unconfirmed`. The [diagnostics guide](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.5/docs/diagnostics.md) describes them.
  The diagnostics schema stays 44.

#### Faults and notifications

- **Class 18/20 messages count as maintenance:** a notification with the
  numeric class 18 or 20 is a maintenance message on every gateway, no longer
  an unknown problem. It no longer turns on System fault or counts as an
  active fault; Active notifications still counts and lists it, and System
  notifications still reports it.
- **Warnings and maintenance in the bell notification (optional):** under
  **Configure → Notifications** a new option also shows warnings and
  maintenance messages in the bell notification, with the same details,
  dismissal and resolution rules as faults. It is off by default, so nothing
  changes unless you turn it on. With it on, a notification that listed a
  warning or a maintenance message ends with "notifications cleared"
  instead of "faults resolved", and a dismissed warning stays dismissed when
  the option is turned off and on again.
- **No more "Unknown fault" for maintenance and warnings:** a maintenance
  message, warning or information without a text of its own now reads, for
  example, "Maintenance message (code 1234)" or "Warning (code 5000)", in the
  bell, the events and the attributes, in all 19 languages.
- **Notifications for the installer on room controllers:** a notification
  that a room controller marks for experts or installers (display level 0, 2
  or 4, numeric class below 18) is still listed under Active notifications,
  but no longer turns on System fault, counts as an active fault or shows in
  the bell. Expected, untested on a real controller.
- **Faults of radio devices:** on room controllers, the faults of radio
  devices get their own texts (for example "Battery of a radio device low"),
  other ones "Radio device fault (code …)", and a too low water pressure
  reads "Water pressure too low". Expected, untested on a real controller.
- **Clearer messages for locked settings:** when a room controller locks a
  setting, the message now names what is locked (the heating mode of a zone,
  another zone setting, a heating circuit, hot water or a radio device
  setting) instead of always speaking of the zone's heating mode.
- **Where do I find faults?** A new section in the
  [fault documentation](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.5/docs/faults.md#where-do-i-find-faults) and a short
  pointer in the README explain where current faults are (the `faults`
  attribute of Active faults) and where past ones are (history and logbook
  of the System notifications event entity). It includes an automation that
  writes every `appeared` and `resolved` event to the logbook and sends it as
  a push notification. Thank you, @ColinFinck, for pointing out in #120 that
  this was hard to find.

#### Requests and local access

- **Discovery on large room controllers
  ([#122](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/122)):**
  on a TC100.2 with nine radio devices the device tree used 206 of the 512
  discovery reads and the program list was never requested. On a room
  controller the zone, program and device lists are now read first. Of the
  device tree only the device list, one firmware reading per device (at most
  64) and the known setting leaves are read; probes meant for other device
  families are no longer sent. The setting leaves come last, leaf by leaf
  over all devices, with a cap of their own of 192 paths outside the 512, so
  a large controller no longer loses the settings of its last devices.
  Thank you, @obvius1, for the diagnostics that showed this.
- **HTTP 500 for missing paths:** room controllers answer HTTP 500 for a
  path they do not have. Such an answer to a catalog or optional probe now
  counts as "missing" and no longer ends the discovery; for optional probes
  and zone detail leaves a 500 inside a batch answer costs no single request
  either. Roots, advertised references and failing requests keep the outage
  rule. This rule now applies only while the cloud answers: a batch of
  nothing but such probes carries one path read before, so a short outage
  no longer makes every optional part, such as the radio device settings,
  count as missing until the next start. The single event entries
  (`/events/evN`, `/events/activeEvent`) are no longer read at all; the event
  lists hold the events, and an entry answering HTTP 500 could end the setup.
  On my large test tree with all functions active the setup needs 21
  batch and 7 single requests, and an hour of regular polling about 150
  requests.
- **More time for changes on room controllers:** a room controller applies a
  change with a delay. On such controllers I now read a change back after 1,
  2, 4 and 8 seconds instead of 0.5, 1 and 2 seconds, and still send it only
  once. If it is not confirmed by then, a message says that the change was
  sent but is not confirmed yet; a later regular reading can still confirm
  it. Other gateways keep their timing and messages.
- **Energy history:** at most eight requests every three hours, the first
  run 15 minutes after the start; a first filling reaches back at most 400
  days.
- **No local access for room controllers:** they have no local interface;
  the [room controller page](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.5/docs/room-controllers.md#sign-in-local-access-and-older-firmware)
  explains this. A K40 gateway next to them keeps its own local access.
- The README now names the K40/K40RF gateways that the local access
  recognises for IVT, Worcester and Vulcano. Systems of these brands should
  work with the cloud access when **Bosch** is selected, but none has been
  confirmed yet.

#### Translations

- **19 languages:** besides English and German, the integration now speaks
  Swedish, Norwegian Bokmål, Finnish, Danish, French, Spanish, Italian,
  Portuguese, Dutch, Polish, Czech, Slovak, Greek, Bulgarian, Croatian,
  Slovenian and Irish, in the setup and options dialogs, repairs, entity and
  device names, notifications and the controls and holiday dialogs. Brazilian
  Portuguese shows the Portuguese texts and Norwegian Nynorsk the Bokmål
  ones.
- **Czech and Slovak reviewed:** the Czech and Slovak translations were
  reviewed on 2026-11-10. Generated names
  write the group out instead of abbreviating it, for example
  "Okruh 1: Provozní režim" or "Teplá voda 1: Teplota". Slovak uses
  "nemrznúca zmes" for the brine of a heat pump, "prídavný ohrev" for the
  auxiliary heater, "výstupná teplota" and "vratná teplota" for supply and
  return, and "VYP" for the off options of the holiday settings. Czech
  confirmations no longer use gendered forms such as "(a)".
- **The other languages are drafts:** they follow a glossary per language
  (`docs/glossary/<code>.md`, and a shared one for the Nordic languages in
  `docs/translations.md`), but nobody who speaks them as a mother tongue has
  checked them yet. The room controller texts added in alpha.5 are drafts in
  every language, Czech and Slovak included; those two will be reviewed
  separately. Corrections are very welcome, see
  [translations](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.5/docs/translations.md).
- Names and texts that the integration builds itself (for example
  "Heating circuit 1 – Supply temperature") follow the Home Assistant system
  language; any other language still shows English. German and English
  texts are unchanged, so existing entity names stay the same. After
  switching the system language, reload the integration so generated names
  are rebuilt.
- A further language needs only two data files, `translations/<code>.json`
  and `i18n/<code>.json`; the tests take the languages and the expected
  texts from these files.

#### Fixes

- **Select heating schedule on large systems
  ([#113](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/113)):**
  the selection needs the controller's program list. On a large system the
  discovery could reach its limit before it read this list, and a failed
  read was never repeated, so no zone got the selection. The program list
  and the zone list are now read together with the zones, within the same
  limit and without an extra request. If zones select a program but the list
  is still missing or failed, it is read again on its own (at most seven
  attempts within a day). Thank you, @obvius1, for testing alpha.4 on a
  TC100.2 and reporting the missing selection. Not yet confirmed on a real
  controller.
- **Zones in manual mode:** the controller marks the selected program of a
  zone in manual mode as not used. The integration used to drop the value,
  the sensor and the selection then; they now stay, and the program can be
  changed. Not yet confirmed on a real controller.
- **Program list flags:** the integration now ignores the `used` and
  `available` flags of the program list itself; the zone list keeps its
  rules.
- **Temporary heating setpoint:** a value of 0 means that no temporary
  setpoint is active. The sensor now shows unknown instead of 0 °C.
- **No second climate:** a room controller with zone climates no longer
  gets a climate entity for its heating circuit as well.

## [0.9.0-alpha.4] - 2026-11-10

> [!IMPORTANT]
> **Alpha pre-release for testing.** Make a full Home Assistant backup first.
> From alpha.1, alpha.2 or alpha.3 no new setup is needed; from 0.8.x, the
> notes of alpha.1 below apply as well.

Alpha.4 mainly makes 0.9 more robust. The code was reviewed area by area, and
I fixed the problems found, most of them in rare error and outage situations.
It also reads the zones of the TC100.2 room controller, logs unexpected errors
more clearly and handles fault sources, the sign-in and the additional cloud
devices more reliably. Several fixes could only be tested with simulated data
so far; the sections below say where.

### What changed since alpha.3

#### Entities

- **Names as on the controller:** readings that only the local access
  provides now use the wording and sensor codes of the controller's Info menu,
  so both can be compared line by line. Examples: "Primary supply temperature
  (TC1)" instead of "Additional heater flow temperature", "Air intake
  temperature (TL2)" instead of "Evaporator temperature", "Condenser
  temperature heating (TR3)", "Expansion valve VR0/VR1", "Drain pan
  temperature (TA4)". Entity IDs, history and your own names stay.
- The 4-way valve (VR4) shows Heating or Cooling/defrost, the hot water 3-way
  valve (VW1) Heating or Hot water. The states stay `off` and `on`, so
  automations keep working.
- The second expansion valve VR0 is now a reviewed reading (enabled); the
  compressor start delay gets its name and stays disabled. Signals that repeat another entity's
  reading are named after it with "(signal)" and stay disabled by default.
- The names follow the Info menu of my own controller (MX400, firmware
  15.00.01) in German and English.
- **Newly enabled entities also on existing installations:** an entity that
  an earlier version created disabled by default, and that is now enabled by
  default, is enabled at the next start. This brings VR0 to installations
  that already had it as a disabled signal, and applies to readings from the
  cloud as well. An entity you disabled yourself stays disabled. Home
  Assistant reloads the integration once, about 30 seconds after the start.
  If "Enable newly added entities" is switched off for the integration,
  nothing is enabled.
- **No stale unit after a change of form:** a local-only reading that turns
  from a number into a state no longer keeps the unit of the number, which
  made Home Assistant refuse the state. A current number without a known unit
  now shows no unit instead of the previous one; the last unit only stands in
  while there is no reading.
- **Smart grid mode without a state list:** the numbers now count as the
  controller declares them: 0 not used, 1 blocked, 2 normal, 3 preferred, 4
  forced. Before, 0 showed as blocked and 4 as unknown. I could not check an
  answer without a state list yet.
- **Kelvin values are differences:** a cloud reading in K, for example a
  hysteresis or spread, is now a temperature difference. As a temperature,
  Home Assistant showed 5 K as -268.15 °C.
- **Temperatures in °C spelled differently:** a cloud reading that reports
  its unit as "°C" instead of "C" now gets its entity like any other
  temperature.

#### Local values and counters

- **No false states from local texts:** a local text that the cloud does not
  declare for this reading now keeps the cloud state instead of showing
  "unknown". Diagnostics count it as `unknown_entry`, as for lists. The log
  line for an undeclared state no longer names the cloud as its source.
- **A single low counter reading no longer counts the whole meter again:**
  if a paired energy, start or runtime counter drops by more than 10 %
  within one source, the drop is shown only when the next reading confirms
  it. A single zero or low outlier therefore no longer appears as a meter
  reset with the whole count added again in the energy dashboard. A real
  reset still shows, one reading later. Small decreases such as the produced
  heat during a defrost are shown as before.
- **A counter part that is gone for good no longer blocks the counter:** if
  a part of a counter, for example the auxiliary heater, disappears for good
  after a firmware update, it stops binding the counter seven days after the
  shown source stopped reporting it. Before, it kept the counter on the cloud
  forever and made it unavailable during a cloud outage.
- **Shown counters are saved within a minute:** frequent counter changes
  kept postponing the save of the last shown counts. The save now happens at
  most about a minute after the first change.

#### Outages and recovery

- **Clearer log entries for unexpected errors:** about thirty places that
  logged an unexpected error with a bare sentence, for example "The heating
  fault notification could not be updated", now name the error class.
  Internal errors that repeat with every reading are logged once. The error
  text itself stays out of the log, because it may contain account or device
  details or values; with debug logging enabled, the full details follow at
  debug level.
- **Short gateway outages no longer hide values for 15 minutes:** when the
  gateway does not answer at all, the cloud reports a timeout for every
  reading. Each of these readings was then paused for 15 minutes, so the most
  important values stayed unavailable long after the gateway was back. Now
  only the protection pause for the whole gateway applies, no reading is
  paused on its own, and no single reads are sent to the silent gateway. A
  single slow reading next to answered ones is still paused as before.
- **No false "recovered" after a failed cycle:** a cycle that had nothing to
  request right after a failed one reported the cloud as reachable again. The
  connection status, the preference for local values without the cloud and
  the switch of paired counters then flipped back and forth. Such a cycle now
  keeps the failure until the cloud really answers.
- **A cloud outage next to a failing batch is noticed at once:** if one batch
  of readings kept failing while the others answered, a cycle that only had
  to read this batch again counted as fine. During a complete cloud outage the
  integration then stayed "available" and could even report the cloud as
  recovered. Such a cycle now also reads one reading that answered before. If
  that one answers, the failing batch alone still changes nothing; if nothing
  answers, the cycle fails as in any outage.
- **Failed starts wait longer and no longer pile up in memory:** a system
  whose start keeps failing was tried again every minute without end, and
  each attempt stayed in memory until the integration was reloaded. Attempts
  in a row now wait 1, 2, 4, 8, 16 and then 30 minutes; a successful start
  resets this. Discarded attempts are released.
- **Unexpected start errors are visible:** an unexpected error while starting
  or running a source or an access was not logged at all. The first one of a
  series is now logged as a warning with its error type, the details at debug
  level, and the recovery as info. Diagnostics count the failed attempts of
  each access and source with the last error type.

#### Local access

- **The local access no longer gives up at startup:** if the gateway answers
  at startup but not usably, for example because it is still starting, the
  local access tries again on its own instead of staying unavailable until a
  reload. Typical answers are incomplete system information, an error page, an
  unreadable answer or a TLS handshake that fails. The waits grow from one
  minute to one hour. Only a rejected token, a changed certificate, a different
  gateway or too old firmware still stop it.
- After five failed attempts, about 18 minutes in, a repair note names the
  cause and offers to try again at once. It disappears as soon as the local
  access starts.
- **A short pause no longer hides the local values:** one failed request
  pauses the local access for a minute or more. Until now every local value
  disappeared at once, paired sensors jumped to an older cloud value and
  local counters became unavailable. Now values already read stay until their
  normal expiry of three intervals. A rejected token, a changed certificate
  or an unavailable access still end them at once.
- **Faster recovery after a restart:** if the first read of a value fails
  after Home Assistant starts, for example the list of signals, it is asked
  again after 30 seconds and then a little later each time, instead of only
  after its full interval. Before, one failure could keep all signals away
  for 15 minutes and static values such as the hardware version for a day.
- **Pauses grow as intended:** a gateway asking for a pause with a very short
  or zero Retry-After no longer gets asked again every second; the pause
  grows from one minute to ten as for every other failed request. When both
  running reads fail together, that counts as one failure, so a short
  gateway restart no longer leads to a ten-minute pause. A failed read that
  was repeated at once after a timeout is now repeated at once only for the
  first timeout in a row.
- **Fewer false "late" values:** a value read on time no longer shows as late
  for a moment just before its next read. It now becomes late only 10 seconds
  (or a tenth of the interval) after its interval.
- **An unreadable answer stays a problem of its own path:** an answer the
  integration cannot decode no longer pauses all local reads; only that value
  is unavailable, and the log names the kind of path once. Repeated
  unexpected problems are logged as a warning at most once an hour.
- **No false keep-alive warning on a busy start:** a slow answer on a kept
  connection no longer counts as a connection the gateway dropped.
- **A damaged pause file no longer blocks the local access:** if the file
  with the stored pauses of the local access cannot be read, it is kept as a
  backup (`.corrupt`) and the local access starts without it. A file from a
  later release keeps the pauses it can still use.
- The local access needs less processing time per read, which matters most
  on small boards such as a Raspberry Pi.

#### Cloud connection and robustness

- **One entity no longer blocks all others:** if a single entity could not
  be set up, for example because another entry already owns its ID or the
  stored entity list was full, no entity of that kind came for the whole
  system, and the same error came back with every reading. Now only the
  affected entity is skipped (or, for the full list, added anyway), and the
  cause is logged once.
- **Reloading the integration is cleaner:** while Home Assistant unloads the
  entities, no new entity is added any more. If the unload fails, the
  entities that are still loaded are set up again. On Home Assistant before
  2026.10 this included a kind of entity that had already been unloaded; now
  it stays unloaded there as well.
- **Less work per local reading:** a new local reading no longer makes the
  integration check its repairs, notifications and entity lists again; only
  the entities that show the reading are updated. Changes of a connection
  still do all checks.
- **Local readings without the cloud are tried again:** if the view of a
  system with the cloud switched off (or unavailable) could not be built,
  this was only visible in the debug log and was not tried again until a
  restart. Now a warning names it and it is tried again after five minutes.
- **One faulty value no longer stops the discovery:** if the cloud answers a
  single value with a server error such as HTTP 504, also when it is read
  again on its own, the discovery now skips only that value and finds all the
  others. Before, this one value stopped the whole discovery, so a new setup
  never started and a later discovery never found new parts. Timeouts,
  sign-in problems and rate limits still stop the discovery at once, and so
  do three server errors in a row, so a failing cloud is not flooded with
  single reads.
- The search for parts that are missing after a start now keeps to its plan
  when such a value answers with a server error: it ends after one day instead
  of repeating every five minutes.
- A search that gets no answer at all is still repeated without using up its
  plan, but now at most 24 times; then it waits for the next start, which
  still looks for the missing parts.
- **A failing request no longer makes the cloud read everything every
  minute:** when one request of a cycle keeps failing as a whole, for example
  because one value makes the cloud answer with a server error, the values
  of the other requests were read again every minute instead of every 15 or
  60 minutes, and a warning appeared every minute. Now only the failing
  request is repeated, on its own so that it does not take other values down
  with it; the other values keep their interval, and the warning appears once.
- **Refreshing an entity reads everything at most once a minute:** the
  "Update entity" action read the whole system from the cloud every time. An
  automation calling it every few seconds multiplied the cloud requests and
  could trigger the cloud's rate limit. A further refresh within one minute
  (two with the cloud-friendly profile) now reads only the controls.
  Diagnostics count both kinds.
- **Fewer pointless "cloud requests are limited" repairs:** the repair now
  needs three rate limits that the cloud really reported within 24 hours.
  Requests that the integration held back itself during a running pause, for
  example for a second gateway of the same cloud access, no longer count, and
  the pause now starts when the cloud answered. With the cloud-friendly
  profile already in use the repair is not offered, because it would change
  nothing.
- **Repairs ask first and work again:** Home Assistant hands a repair its
  note when it opens, and the repairs took that for the confirmation. The
  repairs for limited cloud requests and for changed capabilities after a
  firmware update ended at once with "The access or heating system has
  changed" and changed nothing; the repair for a local gateway that is not
  ready tried again without asking. Now every repair shows its question first
  and acts only after it is confirmed. I checked this with simulated repairs
  on Home Assistant 2026.5, 2026.9 and 2026.10, not yet on a real
  installation.
- **A gateway missing from the cloud's list still shows its values:** if the
  cloud answers its gateway list without this gateway, also with an empty
  list, the system now starts with its saved inventory as during a cloud
  outage, or shows the local readings when there is no usable inventory. The
  question about an exchanged gateway works as before.
- **"Search the system again" no longer warns about parts it keeps:** a part
  the gateway did not report during a requested search or the search after a
  firmware update stays and is read as before; the warning now names only the
  parts that are really missing.

#### Sign-in

- **No new sign-in for a single refused value:** if the cloud refused just
  one value with HTTP 401, for example the manual room setpoint inside an
  answered request or a single value during the start, the integration asked
  for a new sign-in although the sign-in was working. Such a value is now read
  again with the regular poll, as all other values already were.
- **Only a clear refusal ends the sign-in:** a refusal of the sign-in service
  without a definite reason, for example an HTML page of a firewall, no longer
  ends the sign-in; only a clear answer such as `invalid_grant` does. After a
  rejected sign-in, reloading the entry tries the stored token once more.
- **Less load when the sign-in service is down:** after a failed token
  renewal the next one waits 30 seconds, doubling up to 10 minutes. Before,
  every single request started another renewal, and the failure was counted
  as an outage of the gateway. Only a failure of the sign-in service starts
  this wait, not an unexpected program error.
- **Rotated token kept:** choosing the first separate cloud device exactly
  while a token renewal ran could discard the new token, so the next renewal
  asked for a new sign-in. The renewal is now saved in this case, and the
  selection waits for it. If this wait fails, the selection is still saved,
  with the stored tokens.
- **Fewer permissions:** a new sign-in now asks only for the permissions the
  integration uses. Removing gateways from the account, claiming gateways and
  managing other users are no longer part of it. If the sign-in service does
  not accept this, the dialog shows a new link with the previous full set
  and asks you to open it, instead of reporting a wrong address.
  Existing sign-ins keep their permissions until you sign in again. I have
  not yet confirmed this with a real sign-in.
- The token request no longer follows redirects, so the refresh token is only
  sent to the sign-in service itself, and its answer is read with a size
  limit. A rejection sent in several parts keeps its OAuth error code in the
  diagnostics.
- A pasted app address with a stray `[` or `]` now shows the usual hint for
  an unusable address instead of "Unknown error".
- One unreadable entry in the account's gateway list, for example a new device
  without an ID, no longer blocks adding or changing systems; it is left out
  and counted in the diagnostics.

#### Diagnostics

- **Diagnostics schema 44** counts the failed attempts of each access and
  source with the last error type (see "Outages and recovery") and adds the
  fields below. See
  [diagnostics](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.4/docs/diagnostics.md).
- `energy_counter_resets_detected` no longer counts the produced heat falling
  during a defrost, and a change of the energy unit is no reset any more; the
  values are compared in kWh. Decreases of the produced heat are counted in
  the new `produced_heat_decreases_detected`.
- New fields in each gateway's `runtime`: `rate_limit_events_last_24h`,
  `explicit_refreshes_full`, `explicit_refreshes_control_only` and
  `explicit_refresh_gap_seconds`.

#### Changes to the heating system

- **Half degrees for hot water are refused up front:** the heating system
  rounds hot-water temperatures to whole degrees and heating-circuit setpoints
  to half degrees. A value off this grid, for example 52.5 °C for hot water,
  was sent, stored differently and then reported as not confirmed. Such a
  value is now refused before anything is sent. A thermostat command in °F
  is set to the nearest half degree Celsius. Other settings keep accepting
  every value within the system's limits.
- **Corrupt limits create no control:** a control is only offered when its
  limits lie between −30 and 100 °C or 0 and 2,880 minutes. A placeholder
  maximum such as 6,553.5 °C no longer allows a mistyped 650 °C.
- **Honest message after a sent change:** if the confirming read after a
  change fails, for example because the cloud limits requests, the message
  now says that the change was sent and may already have taken effect. The
  texts no longer speak of an "operating mode" for every setting, and the
  cloud brake and a sign-in request still apply.
- **Nothing becomes unavailable without a sent change:** a failed check
  before the change, a change refused while the cloud brake is active, or a
  time program that is no longer offered left the control or the thermostat
  unavailable until the next reading. These cases change nothing on the
  system and now keep the shown values.
- **Fewer requests per thermostat command:** the checks before a thermostat
  change now read all their values in one request instead of up to seven.
  Repeating an unchanged temperature costs one request instead of seven.
- **No change is sent minutes later:** while a long query of the system was
  running, a change waited without limit and was sent much later. After one
  minute of waiting it is now refused with "Try again in a moment", and
  nothing is sent. A change that has started is never interrupted.
- **Thermostat after a restart in cooling mode:** a heating-circuit thermostat
  that already existed is now provided again, unavailable, when Home Assistant
  starts while the system cools. It no longer appears as "no longer provided"
  for the whole summer.

#### Holidays

- **Creating holidays no longer stays blocked:** when the cloud finally
  refused a new holiday (for example with HTTP 400) or the connection failed
  before anything was sent, every further attempt only checked the old
  request until Home Assistant was restarted. Such a request now ends the
  block at once. A request with an unknown outcome blocks a second holiday for
  10 minutes; if the holiday list then still shows no such entry, the next
  attempt creates it normally.
- **All-day holidays stay all-day:** a whole-day holiday created in the Home
  Assistant calendar showed up as an appointment from 00:00 to 00:00 on
  systems that store times. It is now shown as an all-day event again.
- **Calendar actions hit only the shown holiday:** a calendar entry now
  carries its dates in its ID, and deleting from the calendar first compares
  the holiday with the system, as the options dialog already did. If the app
  replaced the holiday in the meantime, nothing is deleted or changed.
- **Older systems with holiday slots:** the order in which circuits are
  ticked no longer matters; the circuits are compared as a set and sent in
  the order the slot reports. Each step now reads the slot once instead of
  twice and waits briefly for a slow confirmation. A lost response is
  resolved by reading instead of ending the change. I tested this with
  synthetic data only; a test on a real device is still open.

#### Additional cloud devices

These fixes concern air conditioners, air purifiers, dehumidifiers and water
heaters connected through the cloud. I tested them with synthetic devices and
a local test broker; checks with real devices are still open.

- **A refused sign-in no longer stops the heat pump:** when the broker of these
  devices refused the sign-in once, the whole cloud connection asked for a new
  sign-in and the heat pump entities stopped too. The connection now first
  tries once more with a freshly renewed token, as the device list already did.
  Only a second refusal asks for a new sign-in.
- **A short outage of the device list no longer drops the devices:** the
  device list is checked every five minutes. A timeout or server error there
  ended the working connection and made all these devices unavailable for at
  least a minute. The connection now keeps running with the last known devices.
  A newly enabled device is tried again a minute later without disturbing the
  others.
- **No more gap at the hourly token renewal:** the renewal reconnected only
  after a pause of five seconds and a new device list request, so all devices
  were unavailable for a few seconds every hour. It now reconnects at once.
  Diagnostics count these planned renewals separately as `renewals`.
- **The shared cloud pause is no longer extended:** while the cloud access was
  paused after a rate limit, a connection attempt of these devices counted as
  a new rate limit and extended the pause by up to a minute. It now waits for
  the end of the pause; the diagnostics show `paused`.
- **Commands with a late confirmation:** when a device did not confirm a
  command within 20 seconds, its entities stayed unavailable for up to five
  minutes. A plain state read now follows at once, and its answer shows the
  actual state again. The command itself is still never sent twice.
- **No more "busy" because of 22 and 22.0:** a pending value of 22.0 and a
  reported value of 22 counted as different, so every further command failed.
  Numbers are now compared by their value.
- **Offline devices show as disconnected:** a device reported as offline now
  shows "Disconnected" on its connection entity, also after a restart and after
  more than an hour, instead of becoming unavailable.
- **Capabilities are kept:** an information message without a device type
  cleared the model, the capabilities and the state until the next reconnect.
  It now keeps them. After a real model change, I request capabilities and
  state again without reconnecting.
- **Refused subscriptions are visible:** a subscription refused by the broker
  no longer counts as a working connection. It shows as
  `subscription_rejected` in the diagnostics and is retried with a growing
  wait.
- **No silent stop after an unexpected error:** an unexpected error, for
  example while updating an entity, stopped these devices until the next
  reload, without a log entry. The connection now logs the error type once and
  reconnects with a growing wait. Unloading the integration no longer reports
  "A connection resource could not be closed" because of it.

#### Dialogs

- **Cloud setup no longer stays blocked:** a cloud sign-in dialog left behind
  by a reloaded page or a restarted companion app blocked every new cloud
  setup until Home Assistant was restarted ("Another cloud setup is already
  open"). A newly opened cloud setup now replaces the older one; the older
  dialog ends with a note at its next step and saves nothing. A dialog left
  idle for more than 15 minutes counts as closed. Saving still checks that
  only one cloud setup registers.
- **Adding the cloud access later:** in the options dialog "Add cloud
  access", a mistyped redirect address no longer voids a sign-in that was
  already completed in the browser; the same link stays usable. An expired or
  already used sign-in attempt now says so and shows a new link. The step now
  shows the same step-by-step guide as the setup, because both dialogs share
  one sign-in step.
- **A rejected sign-in with the device label no longer restarts the local
  setup:** if the gateway rejects the label data (wrong login or password,
  button not pressed, too many attempts) or the request was not sent, the
  sign-in form returns with the reason. The certificate stays confirmed and
  nothing has to be discarded first. "Continue" after a temporary error no
  longer asks again for a certificate that was already confirmed in the same
  dialog for this address and port, as long as it is unchanged.
- **Removing a local access with a damaged setup record:** an unreadable
  record of pending local sign-ins, for example after going back to an older
  version, blocked removing the local access with the misleading message
  "configuration changed". Removing the access or the whole entry now moves
  the record aside as a `.corrupt` file and continues. A record that cannot be
  read at all now says so.
- **Holiday mode on systems with a single holiday switch:** confirming the
  dialog without a change no longer sends anything, and a change made in the
  app meanwhile is no longer overwritten; the dialog then shows the current
  mode.
- Unexpected errors in the local setup are logged as a warning with their
  error type. The text of an error from outside the integration is no longer
  shown as a misleading reason such as "timeout".
- **Deleting a holiday by keyboard:** pressing Enter on "Delete holiday period
  completely" saved the holiday instead of opening the deletion confirmation.
  Enter and Space now open the confirmation. Saving a holiday without any
  change no longer sends anything to the heating system.
- **Operate appliance keeps your tab:** after Apply or Reload values, the
  dialog jumped back to the first tab. It now stays on the open tab, shows the
  tab of the last confirmed change, and opens the tab of a rejected value,
  whose field is now marked. The tabs are ordered heating circuits (HC2 before
  HC10), hot water, system; before, hot water came first.
- **No more focus jumps:** a click on an empty area of the dialog followed by
  any state change in Home Assistant moved the focus to the first tab and
  scrolled to the top. The focus is now restored only once, when a new form
  appears.
- Number fields in the dialog step by the setting's step, for example 0.5 °C
  instead of 1. If the lowest value of a setting is not on this step, any
  value within the limits can be entered, and the integration checks it;
  before, the browser could refuse a valid value.
- **"Busy" instead of a connection error:** if a running poll or another
  change holds the heating system, the dialogs for the controls, holidays and
  heating schedules now say that nothing was sent and that you can try again
  in a moment. Before, they showed a general error that suggested the change
  might have been made.
- A missing or blocked dialog file no longer stops the whole integration from
  starting; only the affected form is missing, with a warning in the log.

#### Storage and removal

- **A damaged or newer entity inventory no longer stops the integration:**
  if the stored entity inventory could not be read, came from a newer version
  or lacked its storage envelope, the entry ended in a setup error without a
  retry. It now starts with the entities known to Home Assistant and logs the
  cause.
- **Deleting an entry cleans up completely:** its repair notes about
  firmware, rate limits or a changed certificate stayed until the next
  restart. Its fault baselines, notification dismissals, sign-in histories,
  learned local read schedules and estimated energy totals stayed in
  `.storage` and in every backup. They are now deleted with the entry, also
  when it was never loaded. Removing a local access deletes its read schedule,
  and a gateway exchange deletes the old gateway's copies. At every start,
  files of entries deleted earlier are removed; `.corrupt` backups stay.
- A failed configuration migration now names its cause in the log, as a
  static code or the error type.

#### Faults

- **Room device errors from the cloud:** a device error list now counts only
  rows that report an active error as faults. A valve status such as
  `valveTight` no longer shows up as an "Unknown fault" and no longer keeps
  the system fault on. An active error is named by its error type (for example
  `defectiveTempSensor`) instead of "active", and the same error on two room
  devices is now two faults instead of one.
- A device error that was already active before the update keeps its incident:
  it is not announced again, and a notification you dismissed stays dismissed.
  A stored valve status ends once with a normal "resolved" event.
- The rules follow the gateway's documented error list. I have not yet seen a
  cloud answer from a system with radio room devices.
- **System fault no longer stays unavailable on larger systems:** with several
  cloud fault sources, the spreading of cloud reads could read them in
  different cycles. The fault state was then never confirmed, and System
  fault, Active faults and Active notifications stayed unavailable while
  everything was fine. The fault sources are now always read together, in the
  first request of a cycle, and in a request of their own: a failing reading
  elsewhere no longer takes them along every minute. This costs one more
  request every five minutes.
- **A removed room device no longer blocks every fault:** a fault source that
  is gone for good (it answers "not found" for 24 hours and at least twice,
  or complete discoveries no longer find it for that long) no longer keeps
  every fault from ending. Faults only it reported end normally, and a repair
  notice says so. A temporary error such as a timeout, or a refused access
  (403), never counts. A fault restored from an older version's baseline that
  no source reports for a whole day of complete reads now ends as well instead
  of staying forever.
- **System notifications (event) without the cloud:** the event entity now
  also reports the events of the local error list when the plant runs without
  the cloud, and it no longer turns unavailable during a cloud outage, which
  used to show the last event again afterwards. The automation example in the
  fault documentation now ignores the return from `unavailable` and `unknown`;
  I recommend adding the same `not_from` to your own automations on this
  entity.

#### RRC zones

- **Zones of the TC100.2 are read
  ([#113](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/113)):**
  the TC100.2 room controller sends the `used` flag as the text `"true"` or
  `"false"`. Exactly these two texts now count as true and false; numbers and
  other spellings stay invalid. Room temperature, active target, the heating
  mode reading, the selected program and **Select heating schedule** become
  available on this controller. Thank you, @obvius1, for the analysis, the
  fix and the tests in
  [#114](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/pull/114).
- **Heating mode without a list of options:** if the controller sends no
  `allowedValues` for the heating mode at all, as the TC100.2 does, the mode
  select offers Manual and Schedule (`manual`, `clock`), as the controller's
  app does. Only these two can be set. Another current mode keeps the select
  unavailable, and an empty or different list keeps the strict rules.
- **Locked heating mode:** if the controller reports the mode as used but not
  available, for example while the system cools, the select keeps showing the
  current mode. A change is refused with a message that the controller locks
  the mode, and nothing is sent. Other zone values are not affected.
- **Zone names:** a zone name that the controller sends in Base64 now appears
  decoded in the entity names, for example "Living room – Room temperature".
  Any other name is shown as it is. Existing entity IDs, unique IDs and your
  own names stay.
- @obvius1 confirmed with a local patch on a TC100.2 that mode and manual
  temperature changes from Home Assistant reach the app and the thermostat. I
  have not tested this version on a TC100.2 myself; program selection and the
  zone names still need a check on the device.

#### Release checks

- The release workflow now also runs the browser tests of the dialogs before
  it publishes anything. Before, only the pull request ran them, and my
  release documentation for alpha.2 and alpha.3 wrongly listed them as a
  release gate; I corrected that.
- The browser tests for the local-only menu and "Search the system again"
  now also run in CI.

## [0.9.0-alpha.3] - 2026-11-09

> [!IMPORTANT]
> **Alpha pre-release for testing.** Make a full Home Assistant backup first.
> From alpha.1 or alpha.2 no new setup is needed; from 0.8.x, the notes of
> alpha.1 below apply as well.

### What changed since alpha.2

#### Local access

- **Live values every minute again:** two scheduling errors limited the local
  reads to about two per second, so live values arrived about every 76 seconds
  instead of every 60. The scheduler now starts reads 200 ms apart, at most two
  at a time, and computes the next due time from the start of a read. It slows
  down on its own when the gateway itself answers slowly, measured from
  sending a request until its answer arrives (up to one second between reads,
  also for five minutes after a timeout or server error). A read that timed
  out is repeated once at the next opportunity. The pause at the end of each
  minute is 3 instead of 5 seconds. The diagnostics show how long each part of
  a read takes.
- **Connections are kept open:** reads reuse an answered TLS connection for up
  to 4 seconds instead of opening a new one every time. The certificate is
  still checked on every new connection before anything is sent. If a gateway
  closes or drops kept connections repeatedly (3 times within 15 minutes), the
  integration returns to one connection per read for 6 hours, then 12 and
  24 hours, and logs this once.
- Signals whose entities are all disabled, and signals that never become an
  entity, are read hourly. Pairs with the cloud, counters, faults, the
  installation date and signals the integration uses internally keep their
  period. Enabling an entity restores its period within about a second.
- The state shown from the local access is updated per reading instead of
  being rebuilt every time.

#### Cloud

- **Fewer cloud requests:** reads of the 5, 15 and 60-minute groups are spread
  over the regular requests instead of causing extra ones. No value is read
  less often than its period allows.
- While an entity shows the local value, the cloud reads its copy only every
  15 minutes. When the entity switches to the cloud, the copy is read at once
  and then every minute again. Counters, values that can be changed through the
  cloud, the hot-water setpoint, the summer/winter mode and the system pressure
  keep their normal period.
- Control readings whose entities are all disabled are read every 15 minutes;
  holidays, climate, hot water and the holiday calendar keep their period.
- **Log:** an ERROR for a cloud outage now appears only after 5 minutes and at
  least 3 failed cycles, once per outage with its category, duration and number
  of failures.

#### Entities

- **Changed states:** a flow rate below the measuring range (raw value 65535,
  marked `lowflow`) shows unknown with the attribute `measuring_range: below`,
  from the local access and from the cloud. The used lower and upper flow
  setpoints of the energy manager show unknown instead of 0 °C while the
  energy manager is not connected.

#### Diagnostics

- **Diagnostics schema 43** adds the connection reuse of the local access
  (handshakes, reused connections, errors, fallback state, response times by
  connection type), the effective start spacing, readings slowed down because
  their entities are disabled or missing, the effective polling groups and the
  cloud reads of locally shown values. See
  [diagnostics](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.3/docs/diagnostics.md).
- In the comparison of local and cloud readings, a difference with more than
  60 seconds between the two readings now counts as `changed_between_reads`
  instead of `differ`. Write checks still report an unconfirmed write as
  `differ`.

## [0.9.0-alpha.2] - 2026-11-08

> [!IMPORTANT]
> **This is an alpha pre-release for testing. 0.8.3 remains the latest stable
> release.** HACS offers it only when pre-releases are shown for this
> repository. Make a full Home Assistant backup before installing it. Updating
> from alpha.1 needs no new setup; coming from 0.8.x, the notes of alpha.1
> below apply as well.

### What changed since alpha.1

- **Without the cloud, local readings are always used.** When the cloud access
  is switched off, needs a new sign-in, is paused, rate-limited or
  unreachable, a system with a local access shows its paired readings from the
  local access, whatever the option says. With the cloud running, **Use local
  values for existing entities** decides as before. Energy, start and runtime
  counters follow only longer outages: a required sign-in, a switched-off or
  paused cloud at once, an unreachable cloud after 15 minutes; they never
  count backwards. The option is now on by
  default when a system gets its first local access and when a system set up
  locally joins the cloud; stored choices stay. See
  [operation without the cloud](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.2/docs/operation-without-cloud.md).
- **Sign-in:** a new sign-in must use the same account as before; the
  integration compares a hash of the account, no personal data. It is
  accepted when at least one switched-on system or device appears in the
  account. Missing ones stay configured, and a repair note names them. A
  cloud access without any system left can only sign in again with the same
  account; otherwise remove it and add it again. This fixes a lock-out after a device was removed from the
  account. Additional devices can now be forgotten like gateways; a different
  account is still refused. See
  [setup](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.2/docs/setup.md#a-new-sign-in-does-not-find-every-system).
- **More robust local access:** it recovers by itself after unexpected errors
  (retries after 1, 2, 4 and 8 minutes, then at most every 15 minutes); a
  failed save no longer stops the reads; local pauses last at most 24 hours;
  a corrupt schedule file is set aside. Each read uses one TLS connection
  instead of two, which halves the load on the gateway. The polling schedule
  is saved only when it really changes, otherwise at most every 30 minutes,
  which spares SD cards, and the configuration is read again less often.
- **Search the system again** also searches the parts that are ready while
  another part is paused.
- A reading only the local access provides keeps its entity ID and history
  when the cloud starts offering it while Home Assistant is running.
- Counters are no longer held unavailable when the source shown last is gone
  for good; they still never count backwards.
- Entity registry entries of deselected gateways and of resources that can
  never become entities are cleaned up in the managed storage format;
  entries you customised are kept.
- The filter that keeps identifiers, addresses and locations out of entities
  recognises more names.
- The notice for a gateway discovered in the network can be ignored for good.
- An unreadable stored configuration now shows a setup error instead of a
  migration error, and the migration no longer runs at every start. Its
  gateways stay reserved, so a new setup cannot take them over twice.
- A command to an additional device that is interrupted because its access is
  switched off now reports "source stopped" instead of an unconfirmed command.
- **Certificate change of a gateway:** signing in again with the device label
  is now the recommended default; keeping the stored token warns first. See
  [setup](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.2/docs/setup.md#the-local-gateway-certificate-changed).
- A device list that expired during the first setup can be loaded again
  without a new sign-in.
- **New for [#107](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/107):**
  "Energy (estimated from current power)" for heat sources that report a
  current power but no consumption counter. It is disabled by default and only
  an estimate: it does not count across gaps, continues across a switch
  between local and cloud readings, saves its total every minute and only
  counts from the moment it is enabled. I have not confirmed it on a real device yet. Thank you,
  @flame4ever, for the detailed request. See
  [entities](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.2/docs/entities.md#energy-estimated-from-current-power).
- **Texts:** the rate-limit notices no longer name a brand, dialogs use
  neutral wording, and messages say "cloud access" instead of internal names.
- **Release workflow:** only the publishing job has write access.

### Performance and load

- Entities update only when a value, its availability or its source really
  changes, no longer after every local read, and repeated work is cached.
- The request-count sensors write at most once per minute and no longer
  record their large attributes, so the database grows much less. The total
  request sensor counts from the start of the integration (state class `total`).
- Unit, options and limits stay while an entity is briefly unavailable. The
  climate entity stays available while its readings are within their validity
  limit; changes still need the cloud.
- **Fewer requests:** settled cloud values are read once or every 15 minutes,
  containers and lists without an entity hourly. Local paths the gateway does
  not have are asked again weekly after the third answer; a firmware change, a
  new sign-in or changed settings and **Search the system again** still check
  them at once. Descriptive system metadata of the gateway, such as the time
  zone, the bus and tariff or iSRC support, is read hourly, the installation
  date daily; room-device metadata such as battery level and name stays at
  15 minutes; device serial numbers are no longer read.
- **Storage:** compact files, stores load in parallel, "still seen"
  confirmations are saved at most hourly, counter sources after 60 seconds
  (a source change at once), the connection inventory privately and
  atomically. The cloud token is renewed 2 minutes before it expires.
- **Log:** one warning per cloud outage with its category, an info line when
  the cloud or the local access answers again, and the hand-over warning once
  per path.
- **Diagnostics schema 42** adds local condition changes, failure classes,
  response times and reasons for deferred reads, the pair result
  `changed_between_reads`, receive gaps per polling class, the 95th percentile
  of cloud cycles and numbered start-up paths. See
  [diagnostics](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.2/docs/diagnostics.md).
- The working time of a heat source can now come from the local access.
- The controller signal "Compressor power" is now "Heat output (compressor)";
  its entity ID stays. Signal measurements with a unit get long-term statistics.
- **Changed states:** iSRC installation status, iSRC support status, the season
  optimizer mode and controller signals reported as true/false now show
  translated states, for example `timeout` instead of `TIMEOUT`, `forced_heat`
  instead of `forcedHeat` and `on`/`off` instead of `true`/`false`. Please
  update automations and templates that check the raw values.

### Produced heat during defrosting

Since alpha.1 the "Heat produced" energy sensors use the state class `total`,
because the counter falls briefly while the heat pump defrosts. The energy
dashboard can therefore show small negative hourly values during defrosting.
See [entities](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.2/docs/entities.md#published-device-families-and-reference-inventory).

### Related issues

- [#104](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/104)
  and [#105](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/105):
  the start from the saved setup after a cloud timeout from alpha.1 is
  included, together with the more robust local access above. Thank you,
  @gejomo, for the diagnostics and logs.
- [#106](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/106):
  the deprecation warnings stay fixed. Thank you, @lanebe, for the exact
  warnings.
- [#49](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/49):
  the renewal diagnostics stay included, and a new sign-in is no longer
  refused because a system or device is missing from the account. Thank you,
  @lanebe, for the details.
- [#107](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/107):
  new, see above.

## [0.9.0-alpha.1] - 2026-11-08

> [!IMPORTANT]
> **This is an alpha pre-release for testing. 0.8.3 remains the latest stable
> release.** HACS offers it only when pre-releases are shown for this
> repository. Make a full Home Assistant backup before installing it: the
> configuration entry moves to storage format 4, which 0.8.x cannot load. I
> welcome feedback on how it works on your system in the issues.

0.9.0 adds an optional local access to K40/K40RF gateways next to the cloud
access, keeps a system with a local access running without the cloud, makes
the integration more robust when the cloud is slow, paused or unavailable, and
reviews which entities exist and which are enabled by default.

### What is new

- **Optional local access:** a K40/K40RF gateway in the home network can be
  added with the sign-in details from its label, next to the cloud access or
  on its own. The local access only reads; it updates many readings more often
  and adds technical readings the cloud does not offer. Changes always go
  through the cloud.
- **Operation without the cloud:** a system with a local access keeps showing
  its readings when the cloud needs a new sign-in, is paused, switched off or
  removed. Current faults and the bell notifications then follow the gateway's
  local error list. A system set up with local access only can get its cloud
  access later and continues with the same entities.
- **Robustness:** a cloud outage during start-up, parts missing after a start,
  a firmware update, rate-limit pauses and an exchanged gateway no longer need
  a restart or leave entities "not provided". **Search the system again** in
  the options looks for new parts on request.
- **Entity review:** every reading becomes an entity; readings not needed in
  everyday operation are only disabled by default. Signals show on/off or
  named states instead of "unknown", measurements have names and units, and
  cloud resources the integration does not know yet appear as disabled
  diagnostic sensors. New devices are named without the brand.

### Before you update

- Make a full Home Assistant backup. On its first start the configuration
  entry moves to storage format 4; 0.8.3 and older cannot load it afterwards,
  so the backup is the way back.
- Home Assistant 2026.5.0 or newer is required.
- Entity IDs, names, areas and history are kept. New entities appear; some of
  them are disabled by default and can be enabled in the entity settings.

### Hydraulic card preset

The hydraulic card preset keeps working unchanged; the pool-heating start
counter is disabled by default on new installations; systems set up with local
access only are not recognised by the preset yet.

### Related issues

- [#106](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/106): Home Assistant 2026.11 no longer logs deprecation warnings
  for the integration.
- [#104](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/104) and [#105](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/105): when the cloud times out or the discovery
  gets no answer at start-up, a system that was set up before starts from its
  saved setup and searches again for missing parts, instead of leaving its
  entities unavailable.
- [#49](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/49): the sign-in renewal diagnostics from 0.8.4-beta.1 are included
  for every saved cloud access; they help to find out why a renewal was
  rejected.

### Setup and local access

Adding the integration now starts with a choice: cloud access, cloud and local
access (recommended when both are possible), or local access only. The local
access only reads, but updates many readings more often and can add technical
readings the cloud does not offer; the cloud adds controls, time programs,
holidays and system information. With "local access only" the system is set up
from its gateway in the home network and shown read-only under its gateway ID,
with the manufacturer and controller model the gateway reports. See
[setup](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.1/docs/setup.md).

The local setup explains each failure separately (wrong sign-in details, a
refused sign-in service, missing confirmation on the device, a full token list,
a rejected token, a denied resource, an unclear creation result). If the
gateway presents a new certificate, a repair note explains how to confirm it,
and the setup can keep the current token instead of creating a new one. A
gateway that refuses access is shown as "access denied" rather than "not
reachable".

The connection list in the settings now shows the state of every access
(connected, sign-in required, paused until a time, not reachable, disabled),
without naming a brand, and each access shows its last successful request. A
new "Test connection now" reads once on request and names the result or the
cause, without changing settings; opening the dialog never starts a request.

Readings that only the local access provides now become their own sensors:
for example the current supply setpoint, volume flow, compressor and electric
heater power, the refrigerant circuit status and the cylinder temperature.
Technical readings of the refrigerant circuit, the drives and the controller's
internal signals are available as disabled sensors. These sensors are created
only for readings the local access of the system could read, are unavailable
without a current local reading and are removed together with the local
access. See [entities](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.1/docs/entities.md#readings-only-the-local-access-provides).

I added the option **Use local values for existing entities** to the local
connection settings. It is off by default. When enabled, existing sensors of
readings that both connections provide under the same documented resource show
the current local value and fall back to the current cloud value. They stay
available during a cloud outage as long as local readings are current.
Adjustable settings and all controls keep using the cloud. Entity IDs, names,
devices and history are unchanged, no requests are added and switching the
option restarts neither connection. Diagnostics show the number of entity
sources using local or cloud values.

With an enabled local access, simple paired sensor readings that are not
counters get two diagnostic entities, "... (local)" and "... (cloud)", disabled
by default and without long-term statistics, for troubleshooting and
comparison. Paired sensors show the attribute `source` with the source of their
current value.
The documentation now explains the difference between disabling, hiding and
switching off an access.

With local values enabled, readings that both connections provide are now polled
locally at least as often as the cloud polls them: the local interval is
min(local interval, cloud interval). Local values stay first, so a slower local
setting no longer makes a paired sensor update less often than with the cloud
alone. Diagnostics explain why eligible sensors use the cloud. The complete
source selection rules and decision matrix are documented.

Paired readings now treat equivalent unit spellings as equal (`C`, `°C` and
`degC`, `min` and `mins`, `wh` and `Wh`), using the same reviewed aliases as the
cloud contracts; different units are never converted automatically. Local named
states such as a setpoint reported as `off` with the value 0 are shown like the
cloud shows them, while error markers such as an open or shorted sensor, also in
their one-decimal form such as -3276.8, and numbers the cloud declares error
markers are never supplied locally. Diagnostics name the measurements that keep
using the cloud, without values or paths.

With local values enabled, energy, start and runtime counters can now be shown
from the local connection as well. A counter only changes its source when every
number it shows, including total electricity and environmental energy, is at
least the last number shown; otherwise the previous source stays while valid and
the counter is unavailable after that. Falling back to the cloud follows the
same rule. Without stored history the cloud sets the first count, missing parts
keep their last number, the last shown numbers are stored and a source change is
written immediately, so a source change does not make a counter go backwards,
also not after a regular restart. Diagnostics show the shown source, switches,
prevented switches and comparison evidence per counter, without absolute counts.

With local values enabled, a sensor no longer shows a local reading from before
a confirmed change when the cloud has a newer reading of the same circuit, and
the local connection reads the changed circuit again right away. Settings are
still only written and confirmed through the cloud. Diagnostics count local
read-backs that agree or differ with a confirmed change, without values; a
difference never repeats the command.

When settings cannot be changed, the "Operate appliance" dialog now says why
(sign-in required, cloud not reachable, pause after a rate limit or repeated
errors with its end, values not current yet, or only a local access) instead of
an empty list. A new diagnostic entity "Cloud control available", disabled by
default, shows the same state for dashboards and automations. With local values
enabled, the water heater's current temperature uses the same local reading as
the hot-water temperature sensor; its mode, target and writes stay on the cloud.

Showing local values for existing entities no longer re-checks every local
reading against the pair catalog on each state update; in my measurement a
lookup is about 14 times faster, which avoids slow entity updates on smaller
Home Assistant hosts.

### Operation without the cloud

A system without the cloud for more than 30 days (cloud access switched off,
sign-in required or no answer) now keeps its entities from the local readings
alone. Up to then the stored inventory is used as before, with a switched-off
access now taking the current faults from the local error list; once the
inventory is older than 30 days, every readable local reading becomes an
entity (while Home Assistant runs for a switched-off access or a start that
needed a new sign-in, otherwise at the next start), and the stored descriptions of the
cloud's readings (without values) serve where they fit. Readings the
cloud offered keep their entity IDs and history, the others become local-only
sensors, and identifiers or network data never become entities. These entities
are read-only: no controls and no writes, and they request nothing from the
cloud. Counters continue
only from a count shown before and never count backwards, and the current
faults come from the local error list. A required sign-in stays required. The
local entities stay until the cloud answers; then it takes over the same
entities and continues from the local fault list. Local readings reported as
numbers are offered as local-only sensors also while they report an error
marker. Sensors created without the cloud for documented readings without a
reviewed meaning keep their local values once the cloud runs again. See
[source selection](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.1/docs/source-selection.md).

Switching the cloud access off while Home Assistant runs now also takes the
current faults from the local error list at once, without a restart; switched
on again, the cloud continues the same faults without announcing one twice.
The bell notification now follows the local error list whenever a system is
shown without the cloud, with the same notification setting. The active faults
sensor also lists active entries of the room devices' own error lists as extra
information; a device error row the cloud marks as inactive no longer counts
as a fault. The options offer **Search the system again** to look for new
parts through the cloud and local access within the usual limits. Heat demand
and the variable tariff support status now also show the local reading; their
lists previously always fell back to the cloud. Diagnostics
schema 41 describes the systems shown without the cloud. See
[faults](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.1/docs/faults.md) and [reconfiguration](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.1/docs/reconfiguration.md).

Removing the cloud access no longer leaves a system with a local access
without values: if it was selected, it keeps showing its local readings under
the same entities, read-only and without controls, also after a restart, and
again after its local access was switched off and on. Other systems keep their
entities without current values, and a new sign-in that selects the same
system continues the same entities. The remove dialog now explains this and recommends switching
the access off only for a temporary pause. See
[source selection](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.1/docs/source-selection.md).

A system set up with local access only can now get its cloud access later in
the same entry: add the cloud access in the entry's options, or select the
system in the entry's existing cloud access. Before anything is saved, the
cloud's system information is compared with the one read through the running
local access; the gateway ID alone is not enough. The system then continues as
the same system with the same entities, devices and fault list, and gains the
cloud's controls. If the local access is switched off or unreachable, or the
two do not describe the same system, the dialog says so and saves nothing. A
system set up locally in another entry is still refused there, and only one
cloud access can be configured across all entries. If the local access is
busy with its regular reads, the dialog asks to try again shortly. Local-only
sensors of readings the cloud now offers continue as the cloud's sensors with
their entity ID and recorded history where this is unambiguous (exactly one
matching sensor, the same native unit, no change of an existing state class);
otherwise they are removed as before. See
[source selection](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.1/docs/source-selection.md#adding-the-cloud-later).

If the cloud access needs a new sign-in or is switched off, a system with a
local access keeps showing its local values instead of turning every entity
unavailable. The entities stay in place; controls stay unavailable until the
cloud is back and say why. A start without a working sign-in uses the stored
inventory, so the local values appear right away. See
[source selection](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.1/docs/source-selection.md#when-the-cloud-stops).

A system with a local access now also shows its local readings after a Home
Assistant restart while its cloud access is switched off, from the stored
inventory if the cloud confirmed it within the last 30 days, and without any
cloud request.

If the local energy readings of a system contain only the produced heat and
not the consumption parts, the produced heat now continues while the system
is shown from its local readings alone; consumption parts and the values
calculated from them show nothing rather than a wrong value; a part that
reappears below its last shown number is held back until it reaches it.
Serial numbers
in the device information no longer contain control or padding characters;
a serial number made only of padding is not shown.

With a local access, the gateway's local error list feeds the existing fault
display as well: a fault reported by the cloud and the gateway appears once, a
fault only the gateway reports appears too, and a fault ends only when every
source confirms it. A missing or late local answer never clears a fault; a
local list that stays silent for a long time hands the decision back to the
cloud, and a restart neither ends nor repeats faults only the gateway reported.

Sensors and binary sensors of single readings now show a cloud reading for three
polling intervals of its group, the same rule as for local readings: live values
for about 3 minutes, slower groups correspondingly longer and static values
without limit. A timeout, rate limit or server error of a whole request
therefore no longer makes these sensors unavailable at once, while an explicit
answer for the reading, such as not found, still does. During a long cloud pause
readings expire after three intervals at the latest. A timer makes the expiry
visible without an additional request. With local values enabled, a current
local reading now also keeps a sensor available when the whole cloud cycle fails
or pauses; previously this only worked when a single cloud reading failed.
Controls and writes keep requiring a successful current cloud cycle. Diagnostics
report the limits, the number of expired readings and the unit combinations of
pairs that differ only by unit.

With a local access next to a running cloud access, the diagnostics report now
previews operation without the cloud (`local_plant_view`). It works out which
entities the current local readings alone would give, with the same IDs as from
the cloud where the reading is the same, and compares them with the cloud's
entities and the entity registry: matching, only local, only with the cloud, and
differences in unit, device class or allowed values. The preview creates no
entities, sends no requests and lists no values or identifiers. See
[diagnostics](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.1/docs/diagnostics.md).

### Entities

Every reading the local access offers now becomes an entity; readings not
needed in everyday operation are disabled by default instead of left out, and
identifiers, network data and configuration structures never become entities.
Controller signals reported as true/false become on/off states, signals with
named states become states (for example season mode and smart grid mode), a
derived installation date is added, and useful measurements such as the
compressor run times, drip pan temperature and the heating circuit volume
flow get their names and units. Existing entities keep their IDs. Cloud
resources the integration does not know yet also become disabled diagnostic
sensors (numbers, states, on/off), never controls, without additional requests.

The cylinder temperature read through the local access is now disabled
by default for new installations, because it shows the same value as
the hot water temperature. The pool-heating start counter is now disabled by
default (it is still always created). The refrigerant circuit status also shows
the states the gateway declares (off, hot water, pool, pool heating) instead of
"unknown"; other local states accept their declared values as well. New devices
are named without the brand (for example "MX400" or "Wallbox"); existing
devices keep their names, and the manufacturer stays in the device information.
The documentation explains that entity names follow the system language while
state texts follow the profile language. Hybrid systems with an HM200 hybrid
manager get additional local readings of the outdoor unit and the hybrid
manager, including the smart grid mode; they are disabled by default and not
yet verified on a real system.

Produced heat and cooling use the `total` state class. While the heat pump
defrosts, the produced heat falls briefly; Home Assistant now records this as
a small negative amount instead of warning about a counter that is not
strictly increasing, and the long-term statistics continue without a break.

### Cloud connection and robustness

Parts of the system that were missing after a start now come back without a
restart. If the cloud answered a few requests incompletely during start-up,
for example with an error for `/system` or a container without its usual
references, the affected entities used to stay "not provided" until the next
restart. The integration now remembers which resources it found before and
searches missing parts again 5, 15 and 60 minutes after the start and then
every six hours until one day has passed; found parts get their previous
entities back with unchanged entity IDs and settings. A part still missing
after one day is no longer expected at the next start; its entities stay in
the registry. Thank you, [gejomo](https://github.com/gejomo), for the report
and the diagnostics in
[#104](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/104).

A cloud outage while Home Assistant starts no longer leaves a system's entities
"not provided" until its cloud access can start it. The access now starts the
system with the resources from an earlier start, at most 30 days since they were
last confirmed: entities keep their IDs and show "unavailable" until the cloud
answers again, then the values return within one polling cycle. Changes are not
possible while values are unconfirmed, and a rejected sign-in still asks for a
new sign-in. Only temporary failures use the saved resources. Diagnostics show
both under `discovery_recovery` and `startup_inventory`.

A firmware update while Home Assistant keeps running is now noticed: the
firmware versions are read every 15 minutes, and a change leads to one new
discovery, so new capabilities appear without a restart. A known reading that
suddenly reports text instead of a number, or a declared error marker, is no
longer shown as a value: the sensor shows no value and a type change is logged
once and counted in diagnostics. A reading in another unit of the same quantity
(for example °F instead of °C) is shown with the unit it was reported in, and
Home Assistant converts it for display. The local access pauses with a clear
cause if a gateway falls below the minimum firmware. The new
[compatibility page](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.1/docs/compatibility.md) lists the specification version,
the checked systems and how each kind of change is handled. For maintenance, a
new version of the official local API specification can now be compared with
the reviewed one before it is adopted (`scripts/compare_local_api_spec.py`);
the official examples are additionally checked against their meaning.

Slow or failing answers cost less: a cloud cycle stops starting new requests
after 50 seconds and keeps the values it did not reach, follow-up single reads
stop at a clear connection problem, and a search for missing parts that gets no
answer is repeated later without using up an attempt. After a firmware update,
readings the gateway does not report right away are kept, and earlier
"not found" pauses are cleared, for the cloud and for the local access, also
when the update happened while Home Assistant was stopped. The local access
no longer retries continuously at its record limit and no longer treats an
empty signal list as the removal of every signal.

An exchanged gateway can now continue the same system. If a system's gateway
disappears from the cloud access and exactly one new, unassigned gateway
appears instead, Home Assistant shows the repair "Gateway replaced?". It
compares the device modules the new gateway reports with those the system
reported before and offers to continue as the same system, to keep the new
gateway as a separate system, or to decide later. If no device module matches,
or there is nothing to compare, it continues only after a clear warning and a
second confirmation; if the cloud cannot answer at the moment, it asks to try
again later. Continuing keeps entity IDs, history, names, areas and
automations; only the internal unique IDs and the device identifier move to
the new gateway, and active faults and dismissed fault notifications carry
over. A local access of that system belongs to the old gateway and is
removed, so it has to be set up again. A journal lets the next start finish an
interrupted exchange; if that is impossible, the integration does not start
and the log names the reason, instead of creating duplicate entities.
Energy, runtime and start counters keep clean statistics after the exchange,
whether they continue or start again: a counter that starts again begins a new
cycle, and a slightly lower value shows no value until the counter has caught
up, so there are no jumps and no negative consumption. See
[architecture](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.1/docs/architecture.md#gateway-exchange).

Switched-off systems of a cloud access can now be forgotten in the access's
options ("Forget deselected systems"). A forgotten system is no longer listed
there, and a new sign-in no longer fails because it cannot find that system
any more. Its devices and entities stay in Home Assistant until you delete
them.

Additional cloud devices no longer drop out after 15 minutes when they report
only on change. Any non-retained message from the device (information, new
measurements or a newly reported state, but not the answer to the
integration's own state read) now counts as a sign of life; a device stays
available for up to one hour after the last one, and an explicit offline
message still applies immediately. A reconnect of the integration, for example
after a token renewal, no longer makes such devices unavailable for that hour:
the redelivered information restores them while the last sign of life is
recent, their last measurements and reported state stay visible within their
own age limits, and a re-added device takes its online status from it even
with an unchanged timestamp. Measurements expire individually after one
hour, reported state after 15 minutes. Air conditioners that declare Eco as
writable get the preset `eco` on their climate entity, and dehumidifiers that
declare the display as writable get a display switch; both send only that one
field and wait for the reported confirmation. With a local access, the
documented ventilation readings (air temperatures, humidity, air quality, fan
power and speed, bypass flap, auxiliary heater, run and filter times, exhaust
fan level) are offered as disabled local-only sensors; temperature error
markers such as `-32768` show no value. When the cloud reports
them writable, the exhaust fan level becomes a select (only with the
documented levels), the pool target temperature a number with the reported
limits and step, and the pool's enabled state an on/off switch (only for
exactly `on`/`off`), each with read-back confirmation. See [connection](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.1/docs/bacon-connection.md)
and [entities](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.9.0-alpha.1/docs/entities.md).

I made cloud pauses after rate limits complete. A `Retry-After` value is now
honored in seconds or as an HTTP date, in full up to a safety bound of 24 hours
instead of being shortened to one hour. PointT, additional cloud devices and
sign-in use the same rule, and the setup dialog, connection restart and polling
coordinator no longer resume earlier. The pause of a saved cloud access now
survives Home Assistant restarts; real downtime is deducted, but a restored
pause never becomes longer than saved. Removing an access deletes its pause, and
a new access never inherits one. Diagnostics report the pause per cloud
connection. Synthetic HTTP, clock and restart tests cover these cases.

I fixed skipped wallbox polls. Home Assistant can start a scheduled refresh
slightly before the exact section deadline, which previously postponed the
reading by a whole interval, for example 120 instead of 60 seconds for
telemetry. Wallbox sections now use the same one-second tolerance as heating
polling groups. The tolerance applies only to the regular cadence; error and
rate-limit pauses still end only when they have expired. Tests use Home
Assistant's actual timer scheduling in both polling profiles.

I measured cloud polling with up to ten systems and the maximum of 512 resources
each, using the actual coordinator and the shared three-slot request limit
against a local test server. Requests stayed within three parallel slots, all
systems progressed evenly and cycles never overlapped. Diagnostics now show the
longest cycle per gateway, how often a cycle exceeded its interval and how long
requests waited for a free slot. The polling dialogs now explain which values
each interval covers, that active faults are always checked every minute, that
the local interval also sets the minimum spacing of slower values and that
sign-in, device checks and confirmations can add requests.

I made device discovery and replacement sign-in verification share the same
three resource-request slots as the running cloud sources. PointT management
requests now appear in the existing request statistics. Queued checks reject
closed dialogs, changed access and new account pauses before sending a request.
Token renewal runs outside those slots and remains serialized per connection;
local polling keeps its independent budget.

I fixed source invalidation when removing cloud or local access from an
installation that uses both. The other source now keeps its running requests,
observations and listeners. Changing the read order updates the target view
without restarting healthy readers. Identity, binding and credential changes
still reject results from affected old source attempts.

I included the sign-in renewal diagnostics from 0.8.4-beta.1. Each saved cloud
access now keeps its own history of starts, renewals and saves, with the HTTP
status, OAuth error code and sanitized reason of a rejected renewal. Refresh
tokens are recognized only through a local keyed fingerprint. The original cloud
access continues the history file of the beta; removing an access deletes its
history. Diagnostics report it per cloud connection. Sign-in, renewal and
reauthentication behavior are unchanged.

I fixed the loss of the last system-notification event timestamp when a cloud
connection is disabled and reenabled. The original timestamp is retained in HA's
event restore data, including while the entity is unavailable. Restoration does
not emit a new event or change entity IDs. Legacy data remains readable; an
already missing timestamp is not inferred from other dates.

### Compatibility

Home Assistant 2026.5.0 or newer is now required: from that release the setup
dialog can continue directly with the local access of the new entry. Home
Assistant 2026.11 no longer logs deprecation warnings for the heating circuit,
hot-water, air-conditioner and water-heater entities. They now report their
temperatures through the native temperature members introduced in 2026.11; the
old members would stop working in Home Assistant 2027.11. Older releases from
2026.5 keep working unchanged. The automated checks use the official Home
Assistant test package from 2026.11 on and run the full suite on Home Assistant
2026.5.4, 2026.9.2 and 2026.11.0.

### Groundwork for the local access

I added a common internal format for cloud and local readings that keeps their
source, raw path, interpretation, validity and separate timestamps, without any
write rights. A versioned catalog lists the readings that both interfaces
provide under the same documented path with the same type and unit; only those
can be compared. Comparisons report agreement, difference or incomparability and
keep both readings. Missing, empty and error values never become zero.
Diagnostics summarize the pairs per installation.

I added support for numeric local `state` objects alongside the documented
lists, and for `module_id` in update reports. Original fields and exact
identifiers are preserved; missing or conflicting module identifiers remain
invalid. The recorded response variants now pass the local decoder.

Local discovery now suppresses addresses with saved, verified access, including
disabled connections. Saving setup dismisses matching untouched notices; other
gateways and dialogs already advanced by the user are retained. Discovery titles
follow the selected frontend language.

I connected local setup to the managed runtime. The first link compares fresh
PointT and local gateway and module identities; the confirmed relationship is
retained so later local starts work without cloud access. Every start still
checks the local gateway and the saved full certificate fingerprint. Changed
certificates, rejected tokens and conflicting identities stop that local source.

Local settings now expose the live polling interval and current resource counts.
Diagnostics add local conditions, request metrics, discovery progress and
per-resource freshness without values or private identifiers. No additional
devices are inferred from resource paths.

I added fair local scheduling, bounded resource discovery and independent value
expiry. All documented GET templates have explicit polling classes. Search
progress and negative results survive restarts without restoring old values as
fresh; identical requests share work and explicit history reads have fixed
limits. Synthetic installation, TLS, storage and memory tests cover these
boundaries. The managed local source now uses this service.

I added a shared gateway budget for local setup and read clients: two concurrent
reads, at least 250 ms between starts and exclusive setup admission. Complete
retry pauses survive restarts, configuration changes and acknowledged enrollment
attempts. Failed storage blocks further local requests. Local health now remains
separate from configuration validity and historical observations. Synthetic TLS,
storage and runtime tests cover these boundaries.

I added the general local HTTPS read client with full certificate checks before
token transmission, bounded responses and structured path or access failures.
One shared connection budget limits concurrent reads, enforces request spacing
and retains complete retry pauses when a client is replaced. Stale results and
cancelled reads cannot update observations; session cleanup owns only its reads.
Synthetic HTTPS tests cover these behaviors and the shared setup helpers.

I added the complete local resource catalog and immutable response contracts for
all 261 documented K40/MX400 GET path templates. The decoder preserves exact
identifiers, named states, source units and recording groups, validates each
operation's parameters and remains read-only regardless of reported write flags.
Manufacturer examples and synthetic cases cover the response forms and invalid
inputs. Setup and token parsing share the bounded JSON checks.

I added native local setup and recovery dialogs with device-label sign-in and
token import. Discovery and manual addresses use the same checks on an existing
installation. Certificate and save confirmations remain separate even when a
check completes immediately. Resource retry pauses survive reopening the dialog.
The tested runtime and scoped identity provider now enable these dialogs for
an existing installation.

I added recoverable completion for local setup, including imported tokens and
pending enrollments from the previous journal format. A saved completion intent
distinguishes an accepted update from an interrupted one without replaying it.
The extra token copy is cleared only when matching configuration is loaded on a
later process start. Native entry removal clears pending access, and the prepared
format transition restores a running cloud entry after cancellation.

I added local token enrollment with a private recovery journal. A pending attempt
is saved before the single token request; received tokens can be recovered after
a restart without creating another token. Lost responses remain uncertain until
explicitly acknowledged. Passwords are not stored, tokens are not automatically
revoked, and recovery still requires fresh read and identity checks. Synthetic
TLS and storage tests cover cancellation, failed writes and concurrent attempts.
The managed runtime and setup dialogs use this enrollment layer.

I added local credential provenance and a guarded handover for a selected
retained installation. Imported tokens keep unknown scopes separate from
verified read access. Certificate, identity and configuration checks precede one
HA update; existing IDs and unrelated changes are preserved. A local token
replacement invalidates older reads. Version 4.2 readers and isolated HA tests
cover the storage transition, retained cloud refresh and uncertain update
outcomes.

I added a bounded local setup transport with explicit certificate binding for
each port role. It checks the complete certificate before sending a token,
rejects every certificate change and prevents redirects and automatic HTTP
retries. The three read probes have time and response limits and discard stale
results. Synthetic TLS server tests cover these behaviors.

I added a shared local setup probe for manual addresses and discovery hints. It
validates the three K40/MX400 base responses, distinguishes API and transport
failures, and rejects stale or ambiguous evidence before the existing gateway
identity comparison. This parser performs no I/O and never writes to the
appliance.

### Managed connections and storage format 4

I introduced a PointT plant model that keeps device evidence separate from
functional groups. The existing gateway device and entity identities remain in
place. Additional heating circuits and solar groups use their reported
identifiers without creating artificial devices. Device metadata and grouped
names now use the central resource binding.

I also corrected the architecture documentation: the current PointT entry
fingerprint is based on the brand and gateway list, rather than a verified
stable account identity.

I added strict gateway identity parsers and a separate comparison against an
explicitly selected installation. They preserve exact IDs, reject conflicting
aliases and incomplete local responses, and detect stale connection evidence,
competing installations and ownership conflicts. Cloud and local identities
remain separate unless the caller supplies a verified source contract.

I added an immutable configuration snapshot and a pure converter for existing
cloud settings. The new storage separates connections, durable targets and
explicit bindings, retains old identity namespaces and user options, and
supports targets with no remaining connection. Unresolved account routing is not
treated as authenticated identity. Local storage requires a read scope and does
not grant appliance write access. The pure converter has no side effects.

I centralized cloud configuration access and scoped the shared token manager and
account pause to the owner and connection. An explicit replacement login now
advances an authentication epoch, so an older refresh cannot overwrite it even
when the new login returns identical tokens. Rotation preserves concurrent
settings changes; stale configuration dialogs receive a specific message.
The version 4 foundation also distinguishes connection, target and binding
changes and rejects results from outdated request generations.

I implemented the version 4 lifecycle framework with independent connection and
target jobs, guarded retries, connection settings, scoped reauthentication and a
value-free entity inventory. Replacement cloud credentials must confirm every
assigned target, including disabled bindings, before they are saved. Removing the
last connection retains targets and registered entities as unavailable.

I connected the existing PointT client and coordinator to the managed framework.
Gateway sources share account authentication, backoff, request accounting and
concurrency while retaining their existing discovery, polling and write policies.
Stopping a source blocks further requests and confirmation of late results.
Reauthentication replaces its clients without making old consumers valid again.
Fault baselines finish saving before a replacement starts. Tests exercise the
actual client against a local HTTP server, including a confirmed manual room
setpoint write.

I also connected the existing read-only wallbox adapter to the managed PointT
connection. Reported wallboxes keep their own parsing, polling intervals and
eight sensor identities while sharing account services with heating sources.
Stopping a wallbox invalidates its readings and cancels active polls; late
responses cannot revive it. A slow wallbox does not hold the heating startup
queue. Both coordinator types use the same lifecycle guards.

I connected the additional-cloud adapter to the same managed cloud connection.
Its devices share one MQTT session and the cloud login's token refresh, rate
limits and HTTP concurrency. Each device has a separate access view. Disabling
one device cancels its pending operations while peers continue; disabling the
last device closes MQTT without stopping PointT. Replacement access views obtain
fresh metadata, and old views remain invalid after reenabling or a new login.
The existing family validation and reported-state write confirmation remain in
place. Interrupted commands are not replayed. HTTP cancellation also retains its
normal cancellation outcome when a connection stops during a shared request.
I connected all twelve native HA platforms to independently replaceable source
access. Reconnection rebuilds the affected entity objects with the same registry
IDs, names and settings, while unrelated sources continue. Old objects retain
their stopped access. Offline restarts keep registered entities unavailable
without restoring old values or write permissions. Partial setup failures clean
up before retrying; another entry's entity identity cannot be claimed.

I kept request statistics separate for each cloud connection and preserved the
original connection's statistics IDs.

I connected diagnostics, fault notifications, repairs and the custom dialogs to
the prepared managed owner. Diagnostics describe connection and target
relationships with export-local numbers and separate request metrics, without
additional requests or private identifiers. Fault notices survive unavailable,
disabled or removed access as unconfirmed, including an offline restart;
existing dismissals and notification choices remain effective.

Rate-limit repairs change only the affected cloud connection's polling. Firmware
repairs rediscover only their source. Old control, holiday and schedule drafts
cannot write through replacement access, and a changed list cannot redirect a
schedule selection to another installation.

I prepared a guarded storage transition using one HA entry update. It preserves
registry identities and local fault stores, rejects unsupported states and
invalid input, and does not rewrite an already valid version 4 record. A pending
credential rotation blocks the transition until its result is saved in the old
format. Existing account pauses remain. I tested interruption, repeated loading,
earlier preview migrations and restoration of the full previous configuration. A
separate offline replay retained my installation's 138 entities and existing
device assignments using a snapshot with substituted credentials.

I connected the regular version 4 setup and migration dispatch and tested it
through HA's entry lifecycle. Pending token rotation can finish before migration;
late request errors are retrieved after cancelled waiters. Failed setup cleans
up its background work, and failed platform unload is no longer reported as
success. HA requires a restart after migration or unload errors, which the tests
keep separate from ordinary reloads. I also repeated the private 138-entity
probe through HA's regular setup and unload APIs.

I completed the prepared cloud management dialogs for initial setup, reconnection,
device discovery and connection-specific polling profiles. Exact provider
identities retain the existing targets and registry settings. Previously assigned
devices remain selectable when temporarily absent. Aborted authentication,
expired listings and changed configuration cannot silently restore removed access.
I also checked device, entity and statistic identities through regular HA setup
and reconnection against a loopback server.

Additional parallel cloud sign-ins are temporarily blocked until there is a
verified account-identity contract. Existing multiple connections remain
manageable. I activated entry version 4 and automatic conversion through normal
HA setup. Invalid preview storage is rejected before its legacy device migration
changes registry records. Disabled entries migrate when reenabled; already
configured cloud entries remain independent.

An identified wallbox keeps its eight known sensors unavailable after a failed
first service read and recovers them through normal polling. I checked this
alongside migrated solar circuits, two-gateway start counters and retained names.

I also checked the previous v0.8.3-beta.1 code in a separate process. It rejects
version 4 storage and loads the complete restored old configuration. The private
138-entity capture retains its assignments, names and fault stores. A synthetic
online rollback reads again through the previous code and keeps its entity and
device identities. This does not test real token validity, a Home Assistant
backup restoration service or a physical appliance.

## [0.8.4-beta.1] - 2026-11-02

> [!IMPORTANT]
> **This beta adds diagnostics for repeated sign-in requests.** It does not
> change how sign-in, token renewal or reauthentication work. **0.8.3 remains
> Stable/Latest.**

I am publishing this beta to find out why Home Assistant sometimes asks for a
new SingleKey ID sign-in, as reported again in
[#49](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/49).
Thank you, [lanebe](https://github.com/lanebe), for reporting the renewed
sign-in request with the log lines and a screenshot. Until now, the
integration only logged that the refresh token was rejected, not why.

### What is now recorded

- **Reason for a rejected renewal.** The log message and diagnostics show the
  HTTP status, the OAuth error code such as `invalid_grant`, and a shortened,
  sanitized description from the sign-in service. A response that is not an
  OAuth error, such as an HTML page, is identified only by its type.
- **Sign-in history across restarts.** The integration keeps the last 60
  events: starts with the stored sign-in data, each renewal with its trigger,
  duration and result, the access and refresh lifetimes reported by the
  service, whether a new refresh token was issued and whether it was saved.
- **Reuse of a replaced token.** Each refresh token is recognized only through
  a local, keyed fingerprint. The history therefore shows whether a rejected
  token had already been used before or had been replaced by a newer one, for
  example because a save was lost. The log also warns at startup if the stored
  sign-in data is older than the newest token the integration received.

### Privacy

Diagnostics schema 28 adds an `authentication` section with timestamps,
durations, counters, status codes and the sanitized reason. It contains no
tokens, codes, fingerprints, account names or e-mail addresses. The history is
stored locally in Home Assistant's `.storage` folder and is deleted together
with the integration entry. Please still review the file before sharing it.

### How to help

1. Enable pre-release versions for this integration in HACS, select
   **0.8.4-beta.1**, update and restart Home Assistant.
2. Use the integration as usual. The history starts with this version, so a
   rejection that happened earlier cannot be explained retroactively.
3. If Home Assistant asks for a new sign-in, **download the diagnostics first**
   under **Settings → Devices & services → Bosch/Buderus Home Comfort →
   three-dot menu → Download diagnostics**. Then copy the log message starting
   with `SingleKey ID rejected the token refresh`.
4. Sign in again and attach both to #49 after reviewing them.

### Verification

The automated checks passed **3,318 cases on Home Assistant 2026.10.4** and
**3,318 on 2026.9.2**, both with Python 3.14.7 and **96.87% combined statement
and branch coverage**. These are the same cases on two HA versions, including
parametrized variants. The new tests use synthetic tokens and a strict test
sign-in service: a rotated token reused after a restart, rejections with and
without an OAuth error, temporary failures, a skipped save, damaged history
files and removal of the entry. They also check that no token, fingerprint or
key appears in log messages or diagnostics. Ruff, formatting and strict Mypy
passed. Runtime dependencies are unchanged.

I have not installed this beta on a physical system. It does not establish the
cause of the sign-in requests in #49; it records the details of the next
occurrence.

## [0.8.3] - 2026-11-02

I fixed startup on Home Assistant 2026.9.2 by using the public fan, humidifier
and water-heater imports. The previous imports could stop the entire integration
from loading, including installations without an air purifier or dehumidifier.
Thank you to @marcinchrzest for reporting the failure and providing the traceback
in issue #60.

I added a compatibility check against Home Assistant 2026.9.2 and strengthened
the release archive test to import every entity platform. Native platform startup
is also checked for a water softener alone and alongside heating.

This stable release includes the read-only heating and hot-water time-program
sensors from 0.8.3-beta.1. They are disabled by default and show the scheduled
level and weekly switch points. They do not change appliance settings.

I included the compatible maintenance updates from PRs #97, #98 and #99.
Pytest remains at 9.0.3 and pytest-asyncio at 1.4.0 because the updated Home
Assistant test plugin requires those exact versions. Runtime dependencies are
unchanged.

Update through HACS and restart Home Assistant. The WE800i low-salt indicator
can then be checked with the fallback threshold already included in 0.8.2.
Confirmation of that indicator on the reported installation remains open in
issue #60. Thank you to @ReuDa for confirming in issue #95 that the time-program
displays match MyBuderus and switch on time on a WLW186i with MX400. Issue #95
is already closed. Earlier releases and tags are unchanged.

## [0.8.3-beta.1] - 2026-10-29

> [!IMPORTANT]
> **This beta adds read-only time-program sensors.** The new feature does not
> change appliance settings or write switch points. The sensors are disabled
> by default. **0.8.2 remains Stable/Latest.**

I am publishing this beta to test heating and hot-water time programs from
[#96](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/pull/96).
Thank you, [ReuDa](https://github.com/ReuDa), for the implementation and the
initial readings from a Logatherm WLW186i-12 TP70 with MX400.

### What the new sensors show

- Program A, and B where reported, appear as diagnostic sensors for the
  corresponding heating or hot-water circuit.
- The state is the level scheduled for the current local time, such as
  Heating, Reduced, Eco or Off. The attributes list weekly switch points as
  `HH:MM level`, together with the limits reported by the device.
- The calculation uses the gateway's time zone when available, with Home
  Assistant's zone as a fallback. It is refreshed locally each minute without
  an extra cloud request. Programs use the existing 15/30-minute slow polling
  group.
- Hot-water program A is also checked directly when its parent program list
  cannot be read. Unsupported or malformed program data creates no new sensor.

The displayed level is a calculation from that program. It does not confirm
which program is selected or whether a manual setting, holiday or other
override is active. The circulation program is not included. Switch times
are excluded from integration diagnostics.

### Review corrections

I corrected repeated timer callbacks around clock changes and aligned the
calculation with the gateway's local time. Existing entity identities, names
and activation settings now survive temporarily empty or invalid programs.
New valid level keys update after normal polling without a restart. Timers
stop when the data is unavailable and are removed when the entity is unloaded.

### Validation and testing

My local checks passed **3,277 automated cases**, including parametrized
variants, with **96.82% combined statement and branch coverage**. Ruff,
formatting and strict Mypy passed. Regression tests cover both clock changes,
Home Assistant enum-state validation, recovery and preserved user settings.

To try the beta, enable pre-release versions for this integration in HACS,
select **0.8.3-beta.1**, update and restart Home Assistant. Enable the required
**Time program A/B** diagnostic sensors in the entity settings. Compare their
switch points and scheduled levels with the app, including a real switch time.

I have not installed this beta on a physical system or confirmed the
controller's clock-change behavior. The initial device evidence came from
ReuDa's contribution; the corrected display still needs field confirmation.
[#95](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/95)
remains open for that feedback.

## [0.8.2] - 2026-10-27

I am releasing **Bosch/Buderus Home Comfort 0.8.2 as Stable/Latest**. It brings
together the changes from all seven 0.8.2 betas, the compatible maintenance
updates from PRs #90, #91 and #92, and the sign-in guide improvements from #93.

I compared the installation archive with the published Beta.7 package: both
contain the same 97 files, and only the manifest version differs. The stable
release retains Beta.7's integration behavior.

### Updating from 0.8.1

I recommend a full Home Assistant backup before updating because this release
includes substantial changes to discovery, connections, entity types and
value conversions. Update the existing integration through HACS, restart
Home Assistant and reload the browser. Check the readings and controls used
by your automations after the update.

The display name changes from **Bosch/Buderus Heating** to
**Bosch/Buderus Home Comfort**. The repository, integration domain and ZIP
filename stay the same. Existing entry titles, entity IDs and custom names
are retained.

### Everyday heating and hot-water controls

I added **Configure > Operate appliance**, with heating-circuit, hot-water and
system tabs. Edits remain a draft until **Apply**; **Discard** and **Escape**
leave without applying the draft. Additional settings stay available, and
device-page number controls use input fields instead of sliders.

Eligible PointT circuits also gain native climate and water-heater entities.
The hot-water entity combines measured temperature, the reported active
target and offered operating modes. Its **active target is read-only**;
the separate Eco, Eco+ and Comfort start-temperature controls retain their
existing meaning. Native and individual entity controls write immediately.
I validate current permissions, options and limits and confirm writes through
read-back. Several edits remain separate writes, so an earlier confirmed
change stays applied if a later one fails.

### Discovery, readings and connection recovery

I improved recovery of manual setpoints and start counters, including
independent general and per-heat-source counters and whitespace units.
Existing entities recover within the normal discovery and polling limits.

I keep shared token refresh running when a waiting request is cancelled and
preserve account authentication state across retries and reloads. Delayed
responses cannot overwrite a newer sign-in, rejected refresh tokens are not
repeatedly submitted by the same token manager, and account pauses survive
OAuth request limits. Reconfiguration uses the update listener for reloads,
addressing the reported Home Assistant 2026.12 warning.

I also included corrections for partial updates, stale values, concurrent
changes, write confirmation, and temperature, runtime and energy conversions.
Diagnostics retain schema 27. These fixes do not establish the cause of every
reported outage; issue #49 remains open for follow-up.

### Additional supported device families

I included the Midea/RWH water-heater, Midea Climate Series, seven Air purifier
families and Dry 4000i/6000i/8000i adapters, plus read-only PointT wallbox
monitoring. Controls follow each appliance's reported capabilities. Room-zone
controls, editing of existing RRC weekly-program points, ICOM holiday periods
and the separate WDDW2 holiday mode are also included.

These additional families still need broader physical-device confirmation.
WE800i controls remain disabled by default pending field feedback. Wallbox
commands, consumption history and regeneration logs are not included.

### Setup, HACS and maintenance

The integration is available through the regular HACS catalog. I corrected
the README image and expanded the sign-in guide with **Preserve log**, Console
filter checks, fresh sign-in attempts and help for a missing redirect address.
The ten-minute attempt lifetime and the details needed for troubleshooting
are explained without asking users to share credentials. An automatic sign-in
helper is not included, and the empty-Console report is not yet confirmed as
resolved.

The earlier HACS download problem involving `Got more than 8190 bytes when
reading` was addressed upstream by a GitHub change and Home Assistant
2026.10.4, according to the [HACS maintainer's update](https://github.com/hacs/integration/issues/5643#issuecomment-6395219761).
The proposed HACS patch was closed without merging. If that exact error
persists on an older Home Assistant version, update Home Assistant to
2026.10.4 or newer and retry; this integration does not patch HACS.

I updated Hypothesis, the Home Assistant test package, Ruff and the pinned
GitHub Actions. I retained pytest 9.0.3 because the updated Home Assistant
test package requires it. The integration's runtime dependencies are unchanged.

### Validation and remaining field checks

I verified **3,190 automated test cases** on Home Assistant 2026.10.2 with
**96.78% combined statement and branch coverage**, including parametrized
variants. Ruff, formatting, strict Mypy, reproducible packaging and isolated
ZIP imports passed. The isolated runtime dependency audit found no known
vulnerabilities. The final GitHub checks and published-package comparison are
required before completing this release and are recorded in
[Creating a release](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.2/docs/release.md#082-verification).

On my Buderus installation, the earlier Beta.7 checks confirmed a manual
heating target change and restoration, plus hot water Auto to Off and back
to Auto, through the new controls. I checked both directions in MyBuderus
and restored the original settings. I performed no additional installation
or appliance writes for this publication. Other appliances, operating modes,
real outage recovery and long-term stability still need their own checks.

## [0.8.2-beta.7] - 2026-10-25

> [!IMPORTANT]
> **Known HACS download issue**
>
> If HACS reports `Could not download` and the log contains `Got more than 8190 bytes when reading`, an oversized GitHub response header can block the download. I reproduced this with HACS 2.0.5 while installing Beta.6. Updating this integration does not fix HACS itself.
>
> See [HACS issue #5643](https://github.com/hacs/integration/issues/5643) and the [proposed correction in PR #5644](https://github.com/hacs/integration/pull/5644), which remains open as of 25 October 2026. If you apply that local HACS correction, restart Home Assistant before retrying the download.

> [!IMPORTANT]
> **Please test with caution: this beta includes the major changes introduced in v0.8.2-beta.6.**
> In Beta.6, I added new device families and Home Assistant device classes and substantially reworked connections, discovery, state processing and value conversions. Beta.7 builds on those changes and adds the controls described below. Regressions can also affect existing installations. I recommend a full Home Assistant backup before updating and a deliberate check of readings and controls afterwards. **v0.8.1 remains Stable/Latest.**

### Easier everyday controls

- I added **Configure > Operate appliance**, with tabs for heating circuits, hot water and the system. Changes remain a draft until **Apply**. **Discard** or **Escape** leaves without applying the draft, and additional settings stay visible. Escape also works after applying changes or reloading values.
- I made the device page easier to scan with shorter labels, relevant icons and number inputs in place of sliders. Individual entity controls still write immediately. Existing entity IDs, custom names and the single device per heating gateway are retained.
- I added a native climate entity for each eligible PointT heating circuit. Manual mode allows validated target changes. Automatic mode displays the schedule's target without changing the schedule. Cooling and unknown operating contexts are not presented as heating controls.
- I added a native water-heater entity for each eligible PointT hot-water circuit, combining its measured temperature, reported active target and offered operating modes. **The active target is read-only.** The separate Eco, Eco+ and Comfort start-temperature controls keep their existing meaning.

I check current values, permissions and metadata before writing and confirm changes by reading them back. Unconfirmed results remain unavailable until regular polling confirms them. Applying several settings sends separate writes; an earlier confirmed change stays applied if a later change fails.

### More reliable sign-in and connection recovery

I keep token refresh running when a waiting request is cancelled and retain the shared account authentication state across retries and reloads. Delayed responses cannot overwrite a newer sign-in. OAuth request limits keep their existing account pause, and a rejected refresh token is not repeatedly submitted by the same token manager.

Reauthentication and configuration changes now use Home Assistant's update listener for reloads, addressing the reported 2026.12 warning. Ordinary token rotation does not reload the integration. These corrections address reproduced software defects; they do not establish the cause of every reported outage or close issue #49.

### New display name

I changed the visible name to **Bosch/Buderus Home Comfort** to reflect the wider range of devices. The repository, Home Assistant domain, component folder and ZIP filename remain unchanged. Existing entry titles and custom device/entity names are retained. Update the existing installation through HACS.

I also corrected the README logo for the HACS description and updated the English and German operating guides.

### Validation and remaining checks

The combined suite passed **3,190 automated test cases**, including separately counted parameter variants, with **96.78% combined statement and branch coverage**. Ruff, formatting, strict Mypy, the JavaScript checks, both complete Home Assistant browser tests, reproducible packaging and isolated ZIP imports passed. GitHub also checks HACS, Hassfest, dependencies and CodeQL.

On my Buderus installation with Home Assistant 2026.10.3, I changed the manual heating target from 19.0 to 18.5 °C through the controls dialog and restored 19.0 °C through the native climate entity. I also changed hot water from Auto to Off and back to Auto through the native water-heater dialog. I checked both directions in MyBuderus and restored all changed settings. These tests confirm those operations on my installation; other appliances, modes and real outage scenarios still need their own checks.

The new device families introduced in Beta.6 still need broader field confirmation. WE800i controls remain disabled by default pending feedback in issue #60. P15, consumption history and regeneration logs remain excluded. **An automatic sign-in helper is not included in this release.** Initial sign-in still uses the documented SingleKey ID procedure; a helper is being investigated for a later beta.

Select **v0.8.2-beta.7** explicitly in HACS, update the existing integration, restart Home Assistant and reload the browser. Check readings before testing controls individually. If something fails, record the exact model and steps and download diagnostics before reloading the integration. Review diagnostics for personal information before sharing them.

## [0.8.2-beta.6] - 2026-10-24

### Deutsch

**Bitte mit Vorsicht testen: Diese Beta enthält sehr viele Änderungen.**
Ich habe neue Gerätefamilien und Home-Assistant-Geräteklassen ergänzt und
größere Teile von Verbindung, Discovery, Zustandsverarbeitung und
Werteumrechnung umgebaut. Dadurch können auch bei bereits eingerichteten
Anlagen Fehler auftreten. Ich empfehle ein vollständiges Home-Assistant-Backup
vor dem Update und eine bewusste Prüfung der Anzeigen und Bedienfunktionen
danach. **v0.8.1 bleibt Stable/Latest.**

#### Neue Geräte und Funktionen

- Ich habe Midea- und RWH-Warmwassergeräte als native Warmwasserentitäten
  ergänzt, mit Temperaturen, angebotenen Betriebsarten und Sollwerten.
  Zusätzliche Anzeigen für Urlaub, Desinfektion und Heizstäbe sind lesend.
- Ich habe die Midea Climate Series als native Klimaentitäten mit
  Temperaturen, Betriebsarten, Lüfterstufen und Schwenkrichtungen ergänzt.
- Ich habe sieben Air-Luftreinigerfamilien als native Lüfter und Dry 4000i,
  6000i und 8000i als native Luftentfeuchter eingebunden. Je nach Gerät stehen
  zusätzliche Messwerte, Betriebsarten, Sollwerte und Einstellungen bereit.
- Ich habe acht reine Anzeigen für erkannte PointT-Wallboxes ergänzt.
  Wallbox-Bedienfunktionen sind noch nicht enthalten.
- Ich habe Raumzonen-Bedienelemente für Heizen und Kühlen sowie RRC-Zonen,
  die Auswahl vorhandener Heizprogramme und Dialoge zum Bearbeiten
  bestehender Wochenschaltpunkte ergänzt.
- Ich habe ICOM-Urlaubszeiten in Kalender und Dialog eingebunden. WDDW2
  erhält einen eigenen Ein-/Aus-Urlaubsmodus ohne Kalenderzeiten.

Die neuen Geräte erscheinen nur, wenn sie über das Konto erreichbar sind,
erkannt und bei der Einrichtung oder Neukonfiguration ausgewählt werden.
Die einzelnen Anzeigen und Bedienelemente hängen von den tatsächlich
gemeldeten Fähigkeiten ab. Ich behalte ein Anlagengerät je bestehendem
PointT-Heizungs-Gateway bei. Zusätzliche Cloud-Geräte und Wallboxes erhalten
eigene Gerätekennungen.

#### Umbau und Korrekturen

- Ich habe eine zusätzliche Cloud-Verbindung mit Geräteauswahl, gemeinsamer
  Token-Erneuerung, Wiederverbindung und getrennten Verbindungsanzeigen
  ergänzt. Ein Ausfall dieser optionalen Verbindung blockiert die Einrichtung
  einer erreichbaren Heizung nicht.
- Ich habe die Verarbeitung von Teilmeldungen und die Aktualitätsprüfung
  einzelner Werte korrigiert. Die Annahme eines Schreibauftrags gilt erst
  mit einem passenden gemeldeten Zustand als bestätigt. Abgebrochene
  RRC-Schreibzugriffe hinterlassen keinen vermeintlich bestätigten Zustand.
- Ich habe Laufzeit-, Energie- und Stromumrechnungen sowie Taupunkt und
  Temperaturdifferenzen korrigiert. Aktuelle Metadaten werden berücksichtigt;
  ungeklärte oder ungültige Werte bleiben unbekannt. Frühere Statistiken
  werden nicht automatisch berichtigt.
- Ich habe die Salzwarnung und die Behandlung kleiner Rundungsabweichungen
  bei Warmwasser-Sollwerten korrigiert. Die Grenz- und Schrittweitenprüfung
  bleibt erhalten.
- Ich habe Diagnose-Schema 27 und die deutschen und englischen Texte
  aktualisiert. Die neuen Diagnoseangaben enthalten keine aktuellen
  Anlagenwerte und lösen keine zusätzlichen Diagnoseabfragen aus.
- Ich habe GitHub Discussions und eine Vorlage für Installationsberichte
  verlinkt. Nachvollziehbare Fehler bleiben in Issues.

#### Prüfstand und Update

Ich habe **3.036 automatisierte Testfälle** geprüft, einschließlich separat
gezählter Testvarianten. Die kombinierte Code- und Verzweigungsabdeckung
beträgt **96,83 %**. Ruff, Formatprüfung, striktes Mypy, reproduzierbare
Paketerstellung und Import aus einer isolierten ZIP-Installation sind
bestanden. Deutsche und englische Texte haben dieselben 585
Übersetzungsschlüssel und passende Platzhalter.

Diese Ergebnisse ersetzen keine Prüfung am Gerät. Für die neuen
Gerätefamilien fehlen weiterhin praktische Bestätigungen von Anzeigen,
Wiederverbindung und Schreibwirkung. Die WE800i-Bedienelemente bleiben
standardmäßig deaktiviert; dafür wird weiterhin die Rückmeldung aus
Issue #60 benötigt. Verbrauchshistorie und Regenerationsprotokoll sind
nicht enthalten.

Ich empfehle, Beta.6 ausdrücklich in HACS auszuwählen, nach dem Update
Home Assistant neu zu starten und den Browser neu zu laden. Bitte zuerst
die Werte prüfen und Bedienfunktionen bewusst einzeln testen, bevor
Automationen darauf aufbauen. Bei Problemen helfen die genaue
Gerätebezeichnung, die beobachteten Schritte und ein frischer Diagnoseexport,
der vor Veröffentlichung auf persönliche Angaben geprüft wurde.

### English

**Please test with caution: this beta contains a large number of changes.**
I added new device families and Home Assistant device classes and substantially
reworked connections, discovery, state processing and value conversions.
These changes may also cause regressions on existing installations. I
recommend a full Home Assistant backup before updating and a deliberate
check of readings and controls afterwards. **v0.8.1 remains Stable/Latest.**

#### New devices and functions

- I added native water-heater entities for Midea and RWH devices, with
  temperatures, offered operating modes and targets. Additional holiday,
  disinfection and heater indicators are read-only.
- I added native climate entities for the Midea Climate Series, with
  temperatures, operating modes, fan stages and swing directions.
- I added native fans for seven Air purifier families and native
  dehumidifiers for Dry 4000i, 6000i and 8000i. Additional readings, modes,
  targets and settings depend on the device.
- I added eight read-only sensors for identified PointT wallboxes.
  Wallbox controls are not included.
- I added heating and cooling zone controls, RRC zones, selection of existing
  heating programs and dialogs for editing existing weekly switch points.
- I added ICOM holiday periods to the calendar and dialog. WDDW2 has a
  separate on/off holiday mode without calendar dates.

New devices appear only when they are reachable through the account,
identified and selected during setup or reconfiguration. Individual readings
and controls depend on their reported capabilities. I retain one system
device per existing PointT heating gateway. Additional cloud appliances and
wallboxes receive separate device identities.

#### Restructuring and corrections

- I added a separate cloud connection with device selection, shared token
  refresh, reconnection and individual connection indicators. An outage of
  this optional connection does not block setup of a reachable heating system.
- I corrected partial-message processing and freshness tracking for each
  field. An accepted write request is confirmed only by a matching reported
  state. Cancelled RRC writes no longer leave an apparently confirmed state.
- I corrected duration, energy and current conversions, dew point and
  temperature differences. Current metadata is honored; unresolved or
  invalid readings remain unknown. Earlier statistics are not corrected
  automatically.
- I corrected the salt warning and tiny floating-point conversion errors
  in water-heater targets. Range and step validation remain in place.
- I updated diagnostics to schema 27 and revised German and English text.
  The new diagnostic fields contain no current appliance values and do not
  trigger additional diagnostic requests.
- I linked GitHub Discussions and an installation-report template.
  Reproducible bugs continue to be tracked in Issues.

#### Validation and updating

I checked **3,036 automated test cases**, including separately counted
parameter variants, with **96.83% combined statement and branch coverage**.
Ruff, format validation, strict Mypy, reproducible packaging and import
from an isolated ZIP installation passed. German and English text use the
same 585 translation keys with matching placeholders.

These results do not replace physical-device testing. Readings, reconnection
and write effects still need field confirmation for the new device families.
WE800i controls remain disabled by default, pending the response in Issue #60.
Consumption history and regeneration logs are not included.

I recommend explicitly selecting Beta.6 in HACS, restarting Home Assistant
after the update and reloading the browser. Please check readings first and
test controls individually before relying on them in automations. For problems,
the exact device model, reproduction steps and a fresh diagnostic export
are useful. Check the export for personal information before publishing it.

## [0.8.2-beta.5] - 2026-10-23

I am releasing the current device-support improvements and the first three
completed correction packages together as a beta.

- I added initial WE800i discovery and readings for salt, available soft water,
  flow, regeneration and holiday mode. Known regeneration phases also accept
  upper- and mixed-case spellings. Unknown or invalid values remain unknown.
- I prepared controls for the salt threshold, holiday mode and regeneration
  scheduling, plus a separate regeneration start button. They are disabled by
  default pending physical-device confirmation. Writes require current device
  metadata and read-back; an uncertain command is not blindly repeated.
- I added six heating/cooling readings for each reported room zone and the
  DHW device type. Existing entities recover through regular polling after
  invalid responses, with their identities preserved.
- I corrected the WDDW2 safety-temperature state and added scalar start
  counters for individual heat sources. These counters remain separate from
  structured part counters and their statistics.
- I filled 29 German resource-name gaps, clarified ambiguous names and checked
  system-language entity names alongside profile-language dialogs.
  Diagnostics schema 17 explains validation and entity registration without
  exposing measurements or private identifiers.

I ran **1,767 automated test cases**, including parameterized variants, with
**96.70% combined statement and branch coverage**. Ruff, formatting and strict
Mypy passed. Tests include reproducible packaging and an isolated ZIP import.
The language checks include the actual Home Assistant frontend with synthetic
data. These checks do not establish physical-device compatibility.

Physical WE800i and WDDW2 confirmation, the additional zone readings and real
writes with the new softener controls remain outstanding. Available soft water
is the remaining volume, not consumption. Consumption from `/recordings`
remains deferred, and **issue #60 stays open**. The
[WE800i guide](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.2-beta.5/docs/water-softener-validation.md)
describes the requested checks.

Select **0.8.2-beta.5** explicitly in HACS, update and restart Home Assistant.
**0.8.1 remains Stable/Latest.**

### Deutsch

Ich veröffentliche den bisherigen Ausbau der Geräteunterstützung und die
ersten drei abgeschlossenen Korrekturpakete gemeinsam als Beta.

- Die erste WE800i-Unterstützung ergänzt Salzfüllstand, verfügbares Weichwasser,
  Durchfluss, Regeneration und Urlaubsmodus. Bekannte Regenerationsphasen werden
  auch bei großer oder gemischter Schreibweise erkannt. Ungültige Werte bleiben
  unbekannt.
- Salz-Warnschwelle, Urlaubsmodus, Regenerationsplanung und eine eigene Starttaste
  sind vorbereitet. Diese Bedienelemente bleiben bis zur Gerätebestätigung
  standardmäßig deaktiviert. Schreibzugriffe benötigen aktuelle Metadaten und
  eine Rücklesebestätigung; unsichere Befehle werden nicht blind wiederholt.
- Sechs zusätzliche Heiz-/Kühlanzeigen je gemeldeter Raumzone und der
  Warmwasser-Gerätetyp sind ergänzt. Nach ungültigen Antworten erholen sich
  dieselben Entitäten über die regulären Abfragen.
- Der WDDW2-Sicherheitszustand wird als Binärsensor angezeigt. Einzelne
  Wärmeerzeuger unterstützen zusätzlich skalare Startzähler mit eigener
  Identität und Statistik, getrennt von den strukturierten Teilzählern.
- Ich habe 29 deutsche Ressourcennamen ergänzt, mehrdeutige Namen präzisiert
  und System- sowie Profilsprache geprüft. Diagnose-Schema 17 beschreibt
  Validierung und Registrierung ohne Messwerte oder private Kennungen.

**1.767 automatisierte Testfälle** einschließlich parametrisierter Varianten
bestehen bei **96,70 % kombinierter Code- und Verzweigungsabdeckung**. Ruff,
Formatprüfung und striktes Mypy sind erfolgreich. Paketbau und isolierter
ZIP-Import sind geprüft. Die Sprachprüfung im tatsächlichen HA-Frontend
verwendet synthetische Daten.

Die Bestätigung am WE800i und an WDDW2-Anlagen, die zusätzlichen Zonenanzeigen
und reale Schreibtests der neuen Enthärterbedienung stehen noch aus.
Verfügbares Weichwasser bezeichnet den verbleibenden Vorrat. Die
Verbrauchsauswertung aus `/recordings` bleibt zurückgestellt; **Issue #60 bleibt
offen**. Die oben verlinkte Prüfanleitung ist auf Deutsch und Englisch verfügbar.

Bitte **0.8.2-beta.5** in HACS auswählen, aktualisieren und Home Assistant neu
starten. **0.8.1 bleibt Stable/Latest.**

## [0.8.2-beta.4] - 2026-10-22

I am incorporating CHLINDE's fix for start counters that report a whitespace-only
unit. The validation introduced in beta.2 rejected these otherwise valid
responses, leaving an existing sensor unknown or preventing a new sensor from
appearing.

- Ignore surrounding whitespace when validating the general counter and the
  counters of individual heat sources. Missing units and the dimensionless
  `other` unit remain supported; actual measurement units remain rejected.
- Preserve numeric validation, device error markers, entity identities,
  statistics metadata, recovery rules and polling intervals. This correction
  adds no cloud requests or write operations.
- Extend regression tests to cover both counter formats, malformed units,
  metadata changes, unavailable readings and recovery of the same entities.
  Home Assistant setup and reload tests also cover whitespace units with two
  synthetic gateways.

Thank you, @CHLINDE, for the correction, the regression tests and the confirmation
that the counter displays its value again on your Buderus G125 BE ECO-21. That
confirmation covers the reported counter; recovery scenarios and multiple heat
sources still need confirmation on physical installations.

Select **0.8.2-beta.4** explicitly in HACS, update and restart Home Assistant.
**0.8.1 remains Stable/Latest.** The
[English](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.2-beta.4/docs/start-counters.md)
and [German](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.2-beta.4/docs/start-counters.de.md)
guides describe the accepted units and the scope of validation.

## [0.8.2-beta.3] - 2026-10-21

I am releasing this beta with the HACS documentation updates from PR #73.
**0.8.1 remains the stable release and Latest.**

I updated the installation instructions following the integration's acceptance
into the default HACS catalog on 2026-10-20 through
[hacs/default#10040](https://github.com/hacs/default/pull/10040). Thank you to
the HACS maintainers for reviewing and accepting the submission.

Bosch/Buderus Heating can now be found directly in HACS search. I replaced the
temporary custom-repository instructions with search troubleshooting, marked
the catalog review as complete in the project status, and added a HACS badge.

Runtime behavior is unchanged from 0.8.2-beta.2. This includes the general
start counter, counter validation and recovery. Only the manifest version
changes inside the installation archive; the documentation is available in
the repository. The new version allows a regular HACS update.

Select this beta explicitly in HACS and restart Home Assistant after updating.
The revised counter recovery and multi-source behavior still need field
confirmation. I performed no physical appliance writes for this release.

## [0.8.2-beta.2] - 2026-10-21

I am releasing this beta with the start-counter improvements from PR #72.
**0.8.1 remains the stable release and Latest.**

I expose the general start counter reported by systems such as the Buderus
G125 BE ECO-21. Thank you, CHLINDE, for reporting the missing counter in
Issue #64, providing the initial implementation and testing its visibility.

I revised the counter handling in PR #72 so that general and per-heat-source
counters retain independent entities. A missing response or another counter
no longer causes the general entity to be hidden or removed. Existing IDs,
custom names and activation choices are preserved across reloads.

I use the same path validation for sensor creation and recovery. A supported
source ID containing a dot or a non-ASCII character can no longer be missed
when restoring an existing sensor after a failed startup read.

I validate whole-number counts and the resource's declared numeric error
markers consistently. Missing or invalid readings remain unknown, conflicting
parts are rejected, and unknown fields cannot create extra counter entities.
The general resource uses `floatValue`; per-source counters use `emonValue`.

I restore exact registered counter paths during bounded discovery and recover
known or advertised leaves through normal polling. The interval remains
15 minutes with the standard profile or 30 minutes with the cloud-friendly
profile. Recovering a previously omitted path can add reads; existing batch
limits, path budgets, forbidden-resource pauses and cloud backoff still apply.

I added schema 15 diagnostics with counter validation reasons and recovery
status, without exporting counts or raw error-marker values. The
[implementation report](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.2-beta.2/docs/start-counters.md) and its
[German version](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.2-beta.2/docs/start-counters.de.md) explain the previous weaknesses,
the changes and the scope of validation.

Select this beta explicitly in HACS and restart Home Assistant after updating.
Physical validation of the revised recovery and multi-source behavior is
still pending. CHLINDE confirmed visibility with the original implementation;
that feedback does not establish the revised recovery behavior on an appliance.

I verified 1,460 automated test cases with 96.64% combined statement and branch
coverage. Ruff, formatting and strict Mypy passed. The tests include actual
Home Assistant registry and recorder behavior, plus deterministic packaging
and an isolated import from the generated archive. Parametrized variants are
counted separately.

## [0.8.2-beta.1] - 2026-10-20

I am releasing this beta with the manual-setpoint recovery from PR #71 and the
pool-heating start counter correction from PR #70. **0.8.1 remains the current
stable release.**

I extended the manual-setpoint recovery contributed by CHLINDE. When a reported
heating circuit is in Manual and its setpoint is missing or unavailable, I allow
one series of up to three targeted reads, waiting 3, 10 and 20 seconds in turn.
Repeated failure polls do not start another series until the value has recovered
or the circuit has left Manual. Thank you, CHLINDE, for the initial recovery
implementation and tests.

I keep a setpoint missing at startup eligible for the normal control-group reads
while its circuit is in Manual. A later successful response adds its supported
entities and normal polling without a restart. Existing forbidden-resource
pauses, authentication handling and cloud rate limits remain effective. I also
stop pending recovery on unload and keep a targeted success from clearing an
unrelated failed polling cycle. See [request behavior](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.2-beta.1/docs/request-behavior.md)
for timing and request limits.

I register the pool-heating start counter (`numberOfStarts` → `pool`) even
when the gateway's initial payload is empty or partial, with German and English
names. Values still depend on gateway support; polling is unchanged.

I verified the combined changes with 1,350 automated test cases on Home Assistant
2026.10.1 and 96.55% combined statement and branch coverage. Ruff, formatting and
strict Mypy passed. The tests include parametrized variants and an isolated
installation of the generated release archive. An isolated runtime dependency
audit found no known vulnerabilities.

I have not performed physical appliance writes for this beta. Recovery on the
affected installation still needs confirmation. After selecting the beta in
HACS and updating, restart Home Assistant.

## [0.8.1] - 2026-10-20

I am releasing 0.8.1 as the current stable version. It includes the holiday
dialog changes, the timezone corrections from the HACS review, and the startup
recovery and season-switching features from the 0.8.1 betas.

I unified holiday creation and editing in the existing integration dialog:

- Name, period and supported heating settings appear together. Each date has
  its clock beside it, including on mobile screens. Clock fields omit seconds.
- New dates start empty with an editable 08:00 clock. Existing holidays keep
  their saved times. Quarter-hour times remain required for changed endpoints.
- An end before or equal to the start produces a specific error at the end
  field. Entered values remain available for correction.
- A red **Delete holiday period completely** button opens confirmation directly
  from the edit form, even with an incomplete draft. Cancelling preserves the
  draft. Before deletion, I check that the saved holiday still matches the
  confirmed details.

I also corrected midnight writes to use 00:00 on the selected date. Differing
read-back values are shown without reporting a successful save. An unresolved
creation request cannot send a second POST during the loaded integration
session. This protection is shared by the dialog and calendar; it resets on
reload or restart. Existing timestamp values and unrelated concurrent changes
remain protected. The dialogs add no cloud requests when opened.

I resolved named gateway and per-period holiday zones asynchronously before
parsing or publishing updates, including write confirmation and adjusted
results. Notifications use Home Assistant's already loaded local zone. Invalid
zone names are cached within the existing bounds. Thank you, Frenck, for
identifying the blocking timezone lookups in the HACS review.

I included the beta improvements to startup recovery, temporary-failure handling
and supplementary metadata reads, with stable entity identities. The central
**Season switching** control uses only the options and write permission offered
by the installation. See [request behavior](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.1/docs/request-behavior.md)
and [season switching](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.1/docs/season-switching.md).

I included the compatible dependency and CI updates from PRs #61, #62 and #63.
Pytest stays at 9.0.3 because the updated Home Assistant test package requires it.

I verified 1,287 automated test cases on Home Assistant 2026.10.1 with 96.45%
combined statement and branch coverage, plus 366 targeted compatibility cases
on Home Assistant 2026.9.4. Ruff, formatting, strict Mypy and the isolated runtime
dependency audit passed. Local browser component checks covered desktop and
mobile layout, keyboard activation, repeated clicks, invalid drafts and recovery
after errors. These counts include parametrized variants.

Earlier checks on my installation confirmed the revised dialog layout and one
midnight edit to an existing test holiday. I performed no additional appliance
writes for this combined release verification; creation and deletion were not
repeated on the appliance. Broader installation and long-term recovery testing
remain open.

After updating through HACS, restart Home Assistant and reload the browser so
the revised dialog controls are loaded.

## [0.8.1-beta.2] - 2026-10-11

I added a **Season switching** control so the central heating season can be
changed directly in Home Assistant when the installation exposes it. A heating
circuit can remain in summer pause even with its operation mode set to Manual;
the new control exposes the separate central season setting.

- I use the options and write permission reported for
  `/system/seasonOptimizer/mode`. Known options have German and English labels:
  Heating/cooling off, Automatic, Heating only and Cooling only. Cooling is not
  offered on installations that do not advertise it. Options update during
  normal polling, and writes to unoffered options are rejected.
- I kept the control scoped to its gateway. Heating-circuit modes, hot-water
  settings and heating/cooling thresholds are not written by this control.
  Each change uses the existing single PUT and mandatory read-back.
- I translated the actual `heating` and `cooling` season states and named the
  existing status sensor **Current heating/cooling status**. It remains separate
  from the requested setting, with its existing identity and activation choice.
  Its default is still disabled; it can be enabled in the entity settings.
- I kept existing polling intervals. There are no additional background cloud
  requests. Diagnostics explain the new control's eligibility without exporting
  current settings or unknown option text.

My local records from 2026-10-11 show the underlying season setting changing
from `off` to `forcedHeat`, followed by actual season status `heating` and
heating-circuit status `heating_manual_on`. These records concern the existing
appliance setting. The new Home Assistant select still needs a physical test
after installation; cooling has not been tested on that installation.

After updating through HACS, restart Home Assistant and look for
**Season switching** (German: **Saisonumschaltung**) under the heating system.
Heating only enables
heating; demand and normal controller conditions still determine compressor
operation. See [Season switching](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.1-beta.2/docs/season-switching.md) for details.

I verified 1,125 automated test cases with 96.26% combined statement and branch
coverage, including 24 new season-control cases. Formatting, linting, strict
type checking and the runtime dependency audit passed. These counts include
parametrized variants and do not replace testing the new control on an installation.

This release is a pre-release. **0.8.0 remains Stable/Latest.**

## [0.8.1-beta.1] - 2026-10-11

I am releasing this beta to make PointT startup more resilient to temporary
failures and reduce unnecessary cloud reads. Functional discovery now runs
before reviewed supplementary metadata. If an informational request fails,
the integration can finish loading its usable resources and recover the
missing supplementary data later. **0.8.0 remains Stable/Latest.**

### More resilient startup and recovery

- I stopped requesting the unused `/gateway/thirdPartyLicenseInformation`
  document. A failure on that endpoint can no longer abort discovery. This
  does not establish the original cause of every connection failure in #49.
- I separated reviewed informational leaves from functional discovery, with
  one shared **30-second supplementary budget per integration entry**, after
  functional discovery for all selected gateways. Unsent work remains pending.
  The exact paths and startup order are documented in the
  [request guide](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.1-beta.1/docs/request-behavior.md).
- Temporary failures retry after **1, 2, 5, 15 and then 30 minutes** between
  failed attempts, using existing coordinator wake-ups. Further failures
  retain the 30-minute delay. Recovery uses at most one batch of 30 paths per
  entry per minute, after normal gateway work, without immediate fallback
  GETs or extra network retries. Healthy gateways take turns.
- Expected metadata sensors returning 403, 404 or 406 retry after 30 minutes.
  Unsupported diagnostic-only candidates do not repeat continuously. Cloud
  rate-limit pauses take priority, and invalid credentials still trigger
  reauthentication. The shared 512-path and depth-eight limits remain.
- Existing metadata sensors retain their identities and user settings while
  unavailable. They recover on a valid response without a restart. Newly
  recovered supported resources can add entities during normal operation.
  This does not replace rediscovery after adding a previously unknown circuit.

### Fewer unnecessary requests

- I changed valid read-only device product names and installed refrigerant-
  circuit counts from 15/30-minute polling to **one successful read per
  integration load**. "Once" means after success, not one attempt forever.
  An explicit reload or Home Assistant restart reads them again; automatic
  retries of unfinished setup retain the cache when configuration and
  credentials are unchanged. Ordinary token refresh does not clear it.
- Administrative supplementary values do not create entities. Diagnostic
  downloads and debug status logging use local state and add no cloud requests.
- In a local HTTP test with 30 fast paths and two metadata leaves, steady
  polling used **60 instead of 64 requests per hour** in the standard profile
  and **30 instead of 32** in the cloud-friendly profile. Startup and failure
  recovery are excluded from those figures. Savings depend on batch layout;
  fewer path reads do not always mean fewer separate HTTP requests.
- Operating-value intervals and write validation/read-back remain unchanged.
  Supplementary success never refreshes unrelated values or confirms that a
  heating fault has cleared.

### Diagnostics and maintainability

- Schema **14** distinguishes omitted, deferred, failed and recovered reads,
  actual read times, earliest retry times and logical versus physical requests.
  A narrow allowlist makes known technical metadata visible without exposing
  current measurements, settings, personal names, network identifiers or tokens.
  The structural inventory export also removes these technical values.
- I separated supplementary request handling from the Home Assistant
  coordinator. Queue/cache changes use defined methods and immutable status
  views. Tests cover recovery without restart, cancellation, authentication,
  rate limits, wrong-resource replies and stable entity identity.

### Included maintenance updates

- **PR #55:** Ruff pre-commit hooks 0.16.10.
- **PR #56:** Hypothesis 6.168.5, Mypy 2.4.0, the Home Assistant test package
  0.13.370 and Ruff 0.16.10. I retained **Pytest 9.0.3** because the Home
  Assistant test package requires it. The proposed 9.1.1 pin makes the
  dependency set impossible to install and is not part of this beta.
- **PR #57:** Pinned CodeQL initialization and analysis actions 4.38.3.
- I aligned form schemas with the validation backend used by the installed
  Home Assistant, including its 2026.10 transition to Probatio. Required fields
  and suggested holiday values use matching marker types. This compatibility
  choice is isolated from the PointT client and adds no cloud requests.
- This beta includes the existing 0.8.0 fault notifications, entity-preserving
  cleanup, sign-in recovery and icon changes. Those are inherited from the
  stable release; the new runtime focus is startup recovery and request use.

### Validation and beta testing

I ran **1,101 automated test cases** with the pinned development environment
and again with released Home Assistant **2026.10.0**. Both passed with
**96.23% combined statement and branch coverage**. Another 167 targeted form,
notification-options and holiday-write checks passed on HA **2026.9.4**.
Formatting, linting, strict typing and the isolated dependency audit passed;
the audit found no known vulnerabilities.

Local verification and the publication checklist are recorded in
[the release report](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.1-beta.1/docs/release.md).
The new request policy still needs field confirmation. Automated tests and a
local HTTP server do not establish long-term stability on a physical system.

Enable beta versions in HACS, select **0.8.1-beta.1** and
restart Home Assistant. Check the expected entities, including disabled ones,
and observe whether missing supplementary values recover on their own after
a temporary failure. Heating does not need to be enabled for these read checks.
Do not deliberately interrupt the appliance or change heating settings just
to test recovery. If something remains missing, provide a fresh diagnostic
export and the time of the problem after reviewing it for personal information.

## [0.8.0] - 2026-10-10

I am releasing 0.8.0 with heating fault notifications, a refreshed icon and
fixes for disappearing solar entities and reauthentication. This stable
release brings together the changes from beta.1 through beta.3. Apart from
the version number, the integration code and packaged assets match beta.3.

Thank you to ph15ch and lanebe for reporting the problems in #48 and #49
and providing the details that helped me investigate them.

### Heating fault notifications

- I added persistent notifications to the Home Assistant bell. Notifications
  are enabled by default and can be configured for each installation,
  including while the cloud connection is unavailable.
- Messages include fault codes, known descriptions and the times when Home
  Assistant first observed the fault and confirmed its resolution. Active
  and resolved incidents appear separately, with up to 25 resolved incidents
  retained in the visible message.
- Dismissed incidents are remembered across restarts. New incidents,
  confirmed recurrences and increased severity can show the message again.
  Failed readings and changes in fault classification do not imply resolution.
- I improved recovery after interrupted setup or storage failures and
  prevented duplicate notification observers during overlapping starts.
  Notifications use the existing fault data and add no cloud requests.
- Retained message details are cleared on dismissal, disabling notifications,
  unloading or restart. They are not a permanent fault history. Dismissing
  a message does not acknowledge or reset a fault on the appliance, and bell
  notifications do not automatically send mobile push notifications.

### Solar entities and sign-in

- **Solar entities (#48):** I fixed startup cleanup deleting supported
  entities below an unrecognized directory. The same protection covers
  supported descendants in other subsystems. Existing entity IDs, custom
  names and choices to disable entities are preserved across reloads.
  Solar entities remain attached to the gateway device.
- **Reauthentication (#49):** I added a clear explanation and a fresh sign-in
  link when an attempt exceeds its ten-minute limit or must be restarted
  after an interrupted token exchange. Reauthentication updates the existing
  integration entry.
- **SingleKey ID request limits:** A waiting step respects the required
  pause. Submitting again during the pause does not send another token
  request, and a fresh sign-in link is provided afterward.
- **Refresh tokens:** I prevent explicitly empty replacement tokens from
  overwriting saved credentials. If the server omits a replacement token,
  the existing token is retained.

### Appearance and maintenance

- I added a heat pump icon with three blue-to-red thermal waves, with light
  and dark variants at standard and double resolution. Home Assistant loads
  the matching icon from the integration package.
- I updated Hypothesis to 6.168.3, the Home Assistant test package to 0.13.367,
  Ruff and its pre-commit hook to 0.16.9, the pinned CodeQL actions to 4.38.2
  and the hassfest action. Pytest remains at 9.0.3 to satisfy the Home
  Assistant test package's compatibility requirement.

### Validation and field status

- I ran 1,003 automated test cases on Home Assistant 2026.9.4 and 2026.10.0.
  Both runs passed with 95.95% combined statement and branch coverage.
  Formatting, linting, strict typing and the dependency audit also passed;
  the audit found no known vulnerabilities.
- I have tested the notification feature extensively on my own installation
  and closed #36 as implemented and tested locally. Beta.3 also appeared
  to work in my initial local check.
- A 48-hour connection test and the specific solar restart checks remain
  unconfirmed. The original cause of the connection loss in #49 has not
  been established. I am keeping #48 and #49 open for the remaining field
  feedback.

### Upgrade

Install **0.8.0** through HACS and restart Home Assistant. This is a stable
release, so selecting beta versions is no longer necessary.

When updating from 0.7.2, an existing active heating fault may immediately
appear in the Home Assistant bell because notifications start enabled.
Under **Settings > Devices & services > Bosch/Buderus Heating > Configure**,
choose **Notifications** to select the installations to notify for.
Holiday configuration has a separate entry in the same menu.

For installations with solar entities, check that the expected entities
remain after restart and reload and that their values update. If a new
sign-in is requested, use the latest link in the dialog and observe the
connection afterward. A fresh diagnostic export and the time of any
failure will help with remaining problems. Review diagnostics for personal
information before sharing and do not include sign-in links or tokens.

## [0.8.0-beta.3] - 2026-10-09

I prepared this beta to address disappearing solar entities and sign-in attempts
that could get stuck during reauthentication. It also includes the maintenance
updates from PRs #45, #46 and #47, along with the fault notifications and icon
variants from the earlier 0.8.0 betas. Version 0.7.2 remains Stable/Latest.

Thank you to ph15ch and lanebe for reporting the problems in #48 and #49 and
providing the details that helped me investigate them.

### Fixed

- **Solar entities disappearing after discovery (#48, PR #50):** I fixed startup
  cleanup removing supported entities below an unrecognized directory. Solar
  entities now survive this cleanup, including after restart or reload. The
  same protection covers supported descendants in other subsystems. Existing
  entity IDs, custom names and choices to disable entities are preserved.
  Solar entities remain attached to the existing gateway device.
- **Expired sign-in attempts (#49, PR #51):** I added a clear explanation and a
  fresh sign-in link when an attempt exceeds its ten-minute limit or must be
  restarted after an interrupted token exchange. Reauthentication continues
  to update the existing integration entry.
- **SingleKey ID request limits:** I added a waiting step that respects the
  required pause. Submitting again during the pause does not send another
  token request. A fresh sign-in link is provided after the wait.
- **Refresh-token validation:** I prevent explicitly empty replacement refresh
  tokens from overwriting saved credentials. If the server omits a replacement
  token, the existing token is retained.

### Maintenance

- I updated Hypothesis to 6.168.3, the Home Assistant test package to 0.13.367
  and Ruff to 0.16.9. I kept pytest at 9.0.3 because the Home Assistant test
  package requires that version; the originally proposed 9.1.1 update could
  not be installed with it.
- I aligned the Ruff pre-commit hook with 0.16.9 and updated the pinned
  hassfest action used by CI and releases. These are development and
  validation updates, with no new heating controls.

### Validation

- I ran 1,003 automated test cases on Home Assistant 2026.9.4 and 2026.10.0.
  Both runs passed with 95.95% combined statement and branch coverage. The
  cases include discovery, entity cleanup, restart and reload
  behavior, saved entity preferences, expired sign-in attempts, token
  responses and request-limit waiting.
- I checked formatting, linting and strict typing, and the dependency audit
  found no known vulnerabilities. The release workflow repeats these checks
  and validates HACS compatibility and hassfest before publication.
- These automated checks do not confirm the original cause of the connection
  loss in #49 or prove that a connection will remain stable for 48 hours.
  Field validation of both fixes is still pending.

### Upgrade and local testing

Enable beta versions for this integration in HACS, select **0.8.0-beta.3**,
install the update and restart Home Assistant.

1. **Solar entities (#48), if present on the installation:** Check the entities
   assigned to the gateway, including disabled entities. Confirm that the
   expected solar entities remain after a Home Assistant restart and after
   reloading the integration, and that available values continue to update.
2. **Reauthentication (#49):** If Home Assistant requests a new sign-in, complete
   it using the latest link shown in the dialog. Confirm that the existing
   devices and entities remain, then observe the connection for at least
   48 hours. There is no need to delete and recreate the integration to test it.
3. **Earlier beta features:** When a fault occurs and clears during normal
   operation, check that its code, known description and observation and
   resolution times remain visible in the notification. Retained details
   are cleared on dismissal, disabling notifications, unloading or restart.

If a problem remains, record when it happened and provide a fresh diagnostic
export after the update, reviewed for personal information before sharing.
Do not include sign-in links or tokens.

I am keeping #48 and #49 open until the fixes have been confirmed on the
affected installations. I will assess the local results and any remaining
beta issues before publishing 0.8.0 as stable.

## [0.8.0-beta.2] - 2026-10-03

This beta keeps fault details visible after a fault has cleared and improves
notification recovery after interrupted setup or storage failures. It includes
the bell notifications and new icon from beta.1. Version 0.7.2 remains stable.

### Fixed

- Keep fault codes, subcodes and known descriptions in visible notifications
  after confirmed resolution. Active and resolved faults appear separately
  when several faults end at different times.
- Show the first Home Assistant observation and confirmation of resolution in
  the configured time zone, including the applicable UTC offset. Repeated
  incidents of the same code retain separate observation times.
- Distinguish a fault reclassified as a warning, maintenance or information
  from a confirmed resolution. Changed classification alone is not an all-clear.
- Retain the latest 25 resolved incidents within the visible message, explaining
  any older omissions. Details are cleared on dismissal, disabling notifications,
  unloading or restart. No permanent fault history or additional cloud requests
  are introduced; existing event delivery and dismissal persistence are preserved.
- Keep notification changes and resolution events working if a storage write
  cannot be queued; retry queueing on a normal update. A failed baseline save
  during unload no longer skips the separate dismissal save.
- Clean up already started notification observers when setup is cancelled, and
  prevent a stopped or concurrently started manager from registering late or
  duplicate observers. An unreadable baseline allows fresh fault detection.

I tested beta.1 on my installation: notifications appeared when faults occurred
and changed to resolved when they cleared. I still need to check the retained
fault details in Home Assistant after installing this fix.

### Validation

- 945 automated test cases passed, including 44 added cases, with 95.77%
  combined statement and branch coverage. Ruff formatting and linting and
  strict Mypy passed.
- Cases cover retained details, partial resolution, recurrence, reclassification,
  failed reads, dismissal, bounded retention, restart, event delivery and the
  repeated hour during a daylight-saving clock change.
- Restart and failure tests include four real SIGKILL boundaries in separate
  Linux processes using Home Assistant storage, corrupt JSON, a failed disk
  replacement, eight persisted-state restart scenarios, normal final-write
  shutdown, repeated reloads and cancelled or overlapping starts.
- An abrupt termination before persistence can lose a recent dismissal or first
  observation time. Recovery uses the last completed files and does not infer
  resolution from a failure. Physical power loss is not simulated.

### Upgrade and beta testing

Enable beta versions for this integration in HACS, select 0.8.0-beta.2, install
the update and restart Home Assistant. When a fault occurs and clears during
normal operation, check that its code, known description and observation and
resolution times remain visible. With multiple faults, active and resolved
incidents should appear separately.

The message retains up to 25 resolved incidents. These details are cleared on
dismissal, disabling notifications, unloading or restart; this is not a permanent
fault history. Dismissing the message does not acknowledge or reset the fault
on the appliance. Issue #36 remains open until the remaining checks are complete.

## [0.8.0-beta.1] - 2026-10-03

This beta adds heating fault notifications to the Home Assistant bell and a
new integration icon for light and dark backgrounds. It also includes the
reviewed maintenance updates from PRs #37, #38 and #39. Version 0.7.2 remains
the stable release.

Thank you for suggesting the notification feature and helping test the
integration on real installations.

### Added

- Show persistent Home Assistant notifications for heating system faults, enabled
  by default and configurable for each installation. Notifications share the
  existing fault state without additional cloud requests or appliance writes.
- Remember dismissed incidents across restarts. New incidents, confirmed
  recurrences and increased severity can show the notification again. Visible
  messages update after confirmed resolution; uncertain states never report
  an all-clear.
- Separate local notification settings from holiday configuration. Notification
  options remain accessible while offline and preserve existing settings.

### Appearance

- Use the new heat pump icon with three blue-to-red thermal waves.
- Include matching light and dark variants at standard and double resolution.
  Home Assistant loads the icons from the integration package. The README
  selects the matching SVG for the reader's color scheme.

### Maintenance

- Update Ruff and its pre-commit hook to 0.16.8, Hypothesis to 6.168.1, and the
  Home Assistant test package to 0.13.366.
- Retain pytest 9.0.3 to satisfy the Home Assistant test package's exact
  dependency requirement.
- Update both pinned CodeQL actions to 4.38.2.

### Compatibility

- Existing fault sensors, events, entity identities and polling intervals remain
  unchanged. Bell notifications and dismissals are shared by Home Assistant
  users. Notification text follows the system language; options follow the
  profile language.
- This feature still requires testing in the actual Home Assistant frontend.
  Automated tests cover dismissal, restart, multiple installations, partial
  responses, recurrence, severity changes and option updates.

### Validation

- 901 automated test cases passed, including 46 additional cases, with 95.80%
  combined statement and branch coverage. Formatting, Ruff and strict Mypy
  passed with the combined feature and maintenance changes.
- Package construction and isolated imports passed. The isolated runtime
  dependency audit found no known vulnerabilities.

### Upgrade and beta testing

Select version 0.8.0-beta.1 in HACS with beta versions enabled, install it and
restart Home Assistant. Under Settings > Devices & services > Bosch/Buderus
Heating > Configure, choose Notifications to select the installations to
notify for. Holiday periods has its own entry in the same menu. An existing
active fault can appear immediately because notifications start enabled.

Check the icon in light and dark themes, the configuration menu, and any
notifications arising during normal operation. Dismissing a Home Assistant
message does not acknowledge or reset the fault on the appliance. Bell
notifications do not automatically send mobile push notifications.

I have not performed a physical appliance write or deliberately triggered a
fault for this release. Actual Home Assistant and HACS frontend confirmation
remains outstanding, and issue #36 stays open for that feedback. The holiday
input corrections inherited from 0.7.2 also still need field confirmation.

## [0.7.2] - 2026-09-20

This update corrects two holiday-dialog problems reported after 0.7.1: an
invalid time such as 22:38 showed an unrelated settings error, and the new
holiday option could remain English in a German profile.

Thank you for testing again and providing the screenshots. They helped me
identify both remaining problems.

### Fixes

- Show the quarter-hour requirement at the affected start or end field. Minutes
  must be 00, 15, 30, or 45, with seconds 00. The form also explains this before
  submission. Times are never rounded automatically.
- Preserve the name and dates while correcting an input. The subsequent settings
  step displays the entered holiday name.
- Report the same translated time error when creating or updating a holiday
  through the calendar, before sending a write.
- Translate the new holiday option through Home Assistant's selector translations,
  using the profile language independently of the server language. German and
  English are provided, with Home Assistant's English fallback for other languages.
- When several gateways can create holidays, ask which installation to use.
  A gateway that disappears during selection is not silently replaced by another.
- Update the opening instructions to include creation through this dialog.

### Validation

- 855 automated test cases passed, including 31 added cases and parametrized
  variants, with 95.70% combined statement and branch coverage.
- Tests cover the reported 22:38 input, invalid seconds and fractional seconds,
  both endpoints, correction followed by a simulated write, profile
  translations with a different server language, and multiple gateway selection.
- Accepted quarter-hour times are checked in European, American, and Nepalese
  time zones, including UTC calendar inputs.
- Ruff formatting and linting, strict Mypy, and the isolated dependency audit
  passed. No known dependency vulnerabilities were found.

### Upgrade

Update through HACS and restart Home Assistant. Version 0.7.2 gives installations
already running 0.7.1 a distinct update; the published 0.7.1 files are unchanged.
Existing entity IDs, diagnostics schema 13, supported operating modes, and write
confirmation checks are unchanged. No additional cloud requests are introduced.
I have not performed a physical holiday write for this release. Confirmation in
the user's browser and on the installation remains outstanding.

## [0.7.1] - 2026-09-20

This update fixes holiday creation when Home Assistant combines its automatic
date value with a second time, producing an `Invalid datetime specified` error.
It also checks holiday times around clock changes and includes the reviewed
maintenance updates from PRs #31, #32, and #33.

Thank you for the screenshots, version details, and time spent checking the
holiday dialog. They helped me reproduce the exact input failure.

### Fixes and behavior changes

- Start and end now begin empty in the holiday creation dialog. Selecting both
  dates avoids malformed date and datetime defaults in affected Home Assistant
  frontends. Both fields remain required.
- Preserve the holiday name and dates after a validation error. Datetime
  suggestions use the format expected by the selector, including after an
  offset-aware input is converted to the heating system's time zone.
- Remove the redundant holiday label from the creation instructions so a global
  Home Assistant language setting cannot insert English text into that otherwise
  German description.
- Reject start or end times that do not exist or occur twice during a clock
  change. The options dialog and calendar report a translated error before a
  write. Periods may still span a clock change with unambiguous endpoints.

### Maintenance

- Update Ruff and its pre-commit hook to 0.16.6, Hypothesis to 6.168.0, and the
  Home Assistant test package to 0.13.364.
- Keep pytest at 9.0.3, as required by that Home Assistant test package. The
  proposed pytest 9.1.1 update prevented the test dependencies from installing.
- Update the pinned hassfest action and CodeQL action to their reviewed versions.
  All three maintenance PRs passed the required checks before merging.

### Safety and compatibility

- Device identifiers, entity unique IDs, existing holiday identifiers, discovery,
  polling intervals, and diagnostics schema 13 are unchanged.
- Supported modes and limits still come from the installation. The fixes add no
  cloud requests. Writes retain their existing confirmation checks and are not
  blindly repeated.
- Date-only gateways retain their date selectors and inclusive PointT end-date
  conversion. Datetime gateways retain 15-minute steps and midnight handling.
- PointT holiday timestamps have no UTC offset or fold marker. A repeated local
  time is therefore rejected even when the calendar supplies an explicit offset.

### Validation

- 824 automated test cases passed with 95.69% combined statement and branch
  coverage. The count includes 34 added cases and separately parametrized variants.
- Ruff formatting and linting, strict Mypy, and an isolated dependency audit
  passed. No known dependency vulnerabilities were found.
- The new regression cases reproduced the faulty initial fields and acceptance
  of invalid local clock times before the fixes.
- Verification covers serialized Home Assistant form schemas, required fields,
  date-only and datetime creation, preservation of corrected inputs, and rejected
  options/calendar operations without a write.
- Isolated execution of the original Frontend 20260826.7 functions covered ten
  language regions, five date-format settings, four time-format settings, six
  time zones, and six hours. All 7,200 combinations preserved the machine-readable
  date and time. This is a function-level check, not a browser or physical-device
  test.
- Clock-change cases include Europe, North America, and Lord Howe's half-hour
  changes, alongside fixed-offset and distant time zones.

### Upgrade notes and limitations

- Update through HACS and restart Home Assistant. No removal or reconfiguration
  of the integration is needed. Select both dates when creating a holiday.
- Review the selected modes before confirming the second step. Holiday times
  still need to follow the installation's 15-minute steps.
- If an endpoint falls into a skipped or repeated clock-change interval, choose
  another time. The integration does not silently move it or choose an occurrence.
- I have not performed a real holiday write on an installation for this release.
  Confirmation of the corrected dialog in the user's browser remains outstanding.

## [0.7.0] - 2026-09-10

With 0.7.0, I am releasing the improvements from the 0.7 beta series as a stable
update. This release adds a calculated dew point, improves discovery and controls
for installations with multiple heating circuits, and fixes missed polling
intervals and misleading fault-parser diagnostics.

Thank you to everyone who shared diagnostics, repeated tests, and detailed
feedback. I especially appreciate the patience while I worked through the
missing controls on the second heating circuit.

### New since 0.6.0

- Calculate a dew point locally for each heating circuit that provides compatible
  room-temperature and humidity measurements. Source readings and the Magnus
  calculation parameters are available as attributes.
- Adapt supported controls to the options and numeric limits advertised by each
  installation and circuit. Newly eligible controls for already discovered
  resources can appear during polling, with existing entity IDs retained.
- Improve discovery of additional heating circuits by processing advertised
  references first, known core control paths next, and other optional paths last.
  Unreadable parent directories no longer prevent checks of known circuit paths.
- Add explicit holiday creation with the modes, date formats, and temperature
  limits advertised by the installation. Holiday edits check for conflicting
  changes made in another app.
- Expand diagnostics through schema 13 with discovery outcomes per circuit,
  reasons for control eligibility, and local resource pauses. Current values,
  credentials, and private identifiers remain excluded.
- Recognize additional operating states and schedule types, with
  German and English translations.

### Fixes

- Avoid skipping a polling group when Home Assistant's rounded timer calls the
  coordinator just before its exact deadline. A maximum one-second tolerance
  applies only to group scheduling. This fixes the observed gaps of about two
  minutes between reads intended to run every minute.
- Preserve the last actual fault-parser result during unrelated polling. A
  temperature read no longer replaces `empty`, `ok`, or `partial` with `not_run`.
  Read failures remain visible separately and cannot confirm an all-clear.
- Recover previously discovered resources after a temporary 404 during their
  next regular group poll, preserving their entity identity.
- Retain active faults through incomplete responses, lost sources, and restarts;
  resolution still requires consecutive valid confirmations.
- Preserve unavailable and stale states after interrupted batches and resume
  deferred reads without starving later groups.
- Verify scalar and holiday writes by reading back their results, including
  ambiguous connection failures, without blindly repeating a mutation.
- Improve account-wide rate-limit handling, concurrent token refreshes,
  reauthentication checks, and energy-counter interpretation.

### Behavior changes

- Live values return to their intended cadence. Request totals may therefore
  increase compared with a run that was skipping scheduled reads. Existing bulk
  limits and the standard and cloud-friendly polling profiles remain in place.
- Unsupported optional fault paths are still checked again after their 24-hour
  capability pause. Historical failure lists are checked at startup only.
- `parser_status` now consistently describes the last actual parsing attempt.
  It is not a current connectivity indicator. Diagnostic fields and schema 13
  are unchanged from Beta.7.
- Calculated environmental energy uses the `total` state class; direct cumulative
  energy counters retain `total_increasing`.

### Safety and compatibility

- Existing device identifiers, entity unique IDs, and user registry settings are
  retained. Control options, limits, and write permissions still come from the
  corresponding resource.
- Rate-limit backoff, gateway circuit-breaker deadlines, and resource pauses
  keep their full duration. Timer tolerance never shortens these safeguards.
- The calculated dew point and diagnostic export add no cloud requests or writes.

### Validation

- 790 automated test cases passed with 95.67% combined statement and branch
  coverage. This count includes separately parametrized cases.
- 21 added cases cover Home Assistant's actual timer scheduling, the one-second
  boundary, both polling profiles, unchanged backoff deadlines, delayed cycles,
  daily fault-source probing and recovery, and preservation of parser results.
- The new regressions reproduced both faults on Beta.7 before the corrections.
  The scheduler used by the test environment also matches Home Assistant 2026.9.1.
- Ruff formatting and linting, strict Mypy, and the isolated dependency audit
  passed. No known dependency vulnerabilities were found.
- Release-package tests verify deterministic archives, a component-root manifest,
  matching version metadata, and import from an isolated installation. Publication
  also requires the protected-branch checks and the release workflow validation.

### Upgrade notes and limitations

- Restart Home Assistant after updating so discovery runs with the new version.
  There is no need to enable beta releases in HACS or remove the integration.
  Check disabled entities if a supported installer control is not visible.
- The reporter in issue #23 confirmed that all expected controls, including HC2,
  appeared and that changed values were reflected in the official Buderus app.
  The report does not list every tested setting or confirm that all original
  values were restored.
- The timing and parser corrections have automated regression coverage. I have
  not yet completed a physical-installation observation period for these two
  changes. Additional Bosch systems, gateway variants, and extended operation
  still need broader field testing.
- The calculated dew point is not the controller's internal cooling limit and
  does not include an unknown installer safety offset. Holiday reads and writes
  remain separate cloud operations rather than an atomic transaction.

## [0.7.0-beta.7] - 2026-09-07

This beta combines improved heating-circuit discovery with recovery after a
temporary 404 response. Install the update and restart Home Assistant once so
discovery can find additional controls. Availability still depends on the
resources and write permissions supplied by the installation.

### Fixed

- Try the known operating-mode and temperature-setting paths for each advertised
  heating circuit, even when its circuit or temperature-level directory returns
  403, 404, or 406. Process reported references first, these core paths second,
  and other optional probes last. Keep the existing batch, depth, and path bounds
  and avoid duplicate reads or guessed circuit numbers.
- Retry previously discovered resources after a 404 at their next regular group
  poll instead of pausing them for 24 hours. Mark their values unavailable until
  a valid response restores the same entities without a restart. Preserve
  polling intervals, global rate-limit backoff, and other resource pauses.
- Keep unsuccessful optional control probes out of recurring polling. Controls
  continue to use their own circuit's options, numeric limits, and write rights,
  with one PUT followed by read-back verification.

### Diagnostics

- Schema 13 identifies core catalog probes and counts catalog paths and
  successful discoveries per circuit. Show local polling-pause state and remaining
  seconds per resource without additional requests or raw current values.

### Validation

- 769 automated test cases passed with 95.65% combined statement and branch
  coverage. This includes synthetic regressions for unreadable parent
  directories, path limits, source promotion, and missing circuits.
- Combined lifecycle tests cover discovery, creation of HC2 controls, repeated
  404 responses, recovery at the next group poll, stable entity identity, and
  one verified write to the correct circuit.
- The affected installation still needs a practical test. No physical HC2
  write was performed during development.

### Testing this beta

Thank you for taking the time to test this again. I would appreciate one
combined check after installing Beta.7 and restarting Home Assistant:

1. Check the HC2 controls, including disabled entities and the configuration
   section, and compare the available options with MyBuderus.
2. If a familiar, suitable setting is available, note its original value,
   change it once, check that MyBuderus shows the change, and restore the
   original value. Please include whether both changes were confirmed.
3. Download fresh diagnostics from Settings, Devices & services, Bosch/Buderus
   Heating, the three-dot menu, Download diagnostics. Review the JSON for
   personal information before attaching it to issue #23, and mention any
   controls that MyBuderus offers but Home Assistant still lacks.

Heating does not need to be switched on for discovery. There is no need to
provoke an error: if a 404 happens naturally, the integration now retries the
known resource during its normal polling cycle.

## [0.7.0-beta.6] - 2026-09-06

This beta adapts supported controls to each installation's advertised options
and limits, and fixes additional discovery, polling, and write-confirmation
failures. Install the beta and restart Home Assistant once to load the updated
integration. Newly eligible discovered controls then appear during polling.

### Changed

- Derive each supported scalar control's options and bounds from its own current
  resource metadata. Accept enum subsets and new vendor codes independently
  for every gateway and circuit; preserve exact wire spelling through translated
  options, including colliding aliases. Reference-device numeric ranges and
  undocumented step restrictions no longer reject valid targets.
- Add newly eligible discovered controls during polling while preserving entity
  IDs and user registry settings. Keep legacy switches and offer variant enum
  sets through a separate select. Missing values remain unknown while valid
  replacement values remain writable.
- Diagnose capabilities and current-state issues separately with schema 12;
  unknown option texts remain redacted. Numeric steps are identified as UI hints.
- Allow explicit holiday creation through Configure with per-device modes,
  date/date-time selectors, and advertised temperature limits.

- Include the Ruff hook update from PR #25 and the compatible test-tool updates
  from PR #26. Keep pytest 9.0.3 as required by the Home Assistant test plugin.

### Fixed

- Prevent hidden HTTP connection retries and redirects from repeating PointT
  writes. Preserve Home Assistant's shared session and middleware; read retries
  remain bounded and every HTTP attempt is counted in request diagnostics.
- Mark resources skipped by an interrupted polling cycle unavailable and stale,
  resume them before earlier batches, and advance independently completed polling
  groups. Deferred fault reads cannot preserve an unverified all-clear.
- Remove dynamic device and circuit identifiers from diagnostic resource names
  as well as resource paths.
- Confirm scalar and holiday writes after an ambiguous connection or service
  failure using bounded read-back attempts without repeating the mutation.
- Handle invalid cloud resource paths as protocol errors, allowing resource
  fallback while retaining healthy items from the same bulk response.
- Update numeric control bounds from current validated metadata during polling.
- Reject conflicting resource IDs in direct and bulk responses, preventing a
  different heating circuit from falsely confirming a write.
- Preserve the original holiday-form baseline across polling updates and check
  holiday modes against the current configuration before sending mutations.
- Reject unadvertised holiday defaults; support explicit selections instead.
- Use config-entry-scoped device lookups for Home Assistant compatibility and
  prevent preview-device migration from merging devices into another entry.

### Validation

- 751 automated tests pass with 95.64% coverage, including branch coverage.
- Regression cases cover disconnected HTTP writes, measured read retries,
  redirects, deferred polling and faults, normal polling cadence after local
  path errors, diagnostic names, malformed references, and changing bounds.
- Installation-variant regressions cover independent gateways/circuits, new
  enum codes and reversible aliases, live control creation, switch compatibility,
  missing current values, numeric units and bounds, conflicting response IDs,
  concurrent holiday edits, and explicit date-only/date-time holiday creation.
- Ruff formatting and linting, strict Mypy, and release-package tests pass.
- Validation used synthetic data and local HTTP servers; real-system testing
  remains outstanding.

## [0.7.0-beta.5] - 2026-09-06

This beta restores heating-circuit operation-mode controls when an installation
offers fewer than all three known modes. Diagnostics now explain why a discovered
resource does or does not qualify for a control, helping investigate remaining
missing settings on installations with multiple heating circuits.

### Fixed

- Create heating-circuit operation-mode controls when PointT advertises a
  supported subset such as `manual` and `auto`. Each circuit keeps its own
  options; newly advertised known modes appear during normal polling and
  withdrawn modes disappear. Unknown or unadvertised modes cannot be written.
- Reject non-finite numeric bounds and resources without a current scalar value
  consistently when deciding whether to create numeric controls.

### Diagnostics

- Schema 11 explains scalar-control eligibility per resource using the same
  checks as entity creation. It reports specific rejection reasons, known
  advertised options, numeric capability bounds, policy limits and steps, and
  default activation, including disabled installer controls.
- Current measurements and settings, unknown option text, credentials, and
  device identifiers remain excluded. Downloading diagnostics adds no requests.

### Safety and compatibility

- Only known operation modes currently advertised by the individual heating
  circuit are offered. Write permissions, resource types, and the single-PUT
  transaction with mandatory read-back remain enforced.
- Other enum controls retain their existing option requirements. Numeric
  safety limits and installer-control defaults are unchanged.
- Diagnostics include capability metadata and fixed rejection codes, not current
  settings or unknown option text. Existing entity and device IDs are preserved.

### Validation

- 687 automated tests pass with 95.59% coverage, including branch coverage.
- Regression tests cover independent hc1/hc2 options, metadata changes,
  confirmed subset writes, rejection of unadvertised or unknown values, and
  diagnostic agreement with entity creation without leaking private values.
- Ruff formatting and linting and strict Mypy validation pass.
- The isolated dependency audit found no known vulnerabilities. The local
  release archive, manifest version, and SHA-256 checksum were verified.

### Upgrade notes and limitations

- Restart Home Assistant after updating so previously excluded controls can be
  created. Existing entity and device identifiers and polling intervals remain
  unchanged; write permissions and PUT/read-back validation still apply.
- The operation-mode correction covers the reproduced metadata mismatch. The
  remaining controls on the installation in issue #23 still need a field test;
  the additional diagnostics explain other capability rejections.

## [0.7.0-beta.4] - 2026-09-06

This beta improves discovery on installations with multiple circuits and makes
missing resources easier to investigate. Advertised resources are processed
before optional probes, and diagnostics show discovery outcomes for each circuit.

### Fixed

- Prioritize resources explicitly referenced by PointT ahead of optional app-path
  probes, so fallback traffic from one subsystem cannot prevent later heating
  circuits or other advertised resources from being discovered.
- Remove the global 30-item discovery fallback budget. Every recoverable item
  within the existing bounded discovery graph can receive one individual read;
  rate limits still stop discovery immediately.
- Process references fully before optional probes, in individual batches of at
  most 30 paths. A bad batch no longer replaces successful or unsent batches
  with individual reads. Request-wide service and connection failures stop the
  run without continuing through the remaining fallbacks.

### Diagnostics

- Report discovery scheduling, results, fallback outcomes, completion state,
  and per-subsystem counters. Canonical logical paths such as
  `/heatingCircuits/hc2/operationMode` remain visible, while device identifiers
  and unrecognized dynamic IDs remain redacted.
- Distinguish logical discovery calls from HTTP retries, include failed paths
  per circuit, and report authentication, rate-limit, transport, depth, and
  resource-limit aborts accurately.

### Validation

- 647 automated tests pass with 95.47% coverage, including branch coverage.
- Regression tests cover second-circuit discovery under a tight path budget,
  malformed middle batches, local resource failures, and request-wide aborts.
- Ruff formatting and linting and strict Mypy validation pass.

### Upgrade notes and limitations

- Restart Home Assistant after updating to run discovery again. Existing entity
  and device identifiers are preserved.
- Discovery may use additional partly filled batches to finish advertised
  references before optional probes. Runtime polling intervals are unchanged.
- The reported missing-second-circuit case still needs confirmation on the
  affected physical installation; this beta includes synthetic regression
  coverage and the diagnostics needed for that field check.

## [0.7.0-beta.3] - 2026-09-05

This beta improves fault reporting, calendar writes, cloud error recovery, and
energy statistics. It prevents incomplete or stale cloud responses from being
treated as confirmed state and protects holiday edits against changes made in
another app.

### Fixed

- Keep active faults when a previously readable source becomes unavailable or
  returns a missing or malformed list; only explicit healthy responses can
  confirm resolution.
- Mark unrecovered resources stale after a failed bulk request while preserving
  their last successful values.
- Preserve account request-counter registry entries and user customizations when
  cleaning up deselected gateways.
- Require a complete, valid holiday list before confirming calendar mutations,
  including deletions.
- Read back apparently unchanged controls before skipping a write, and recheck
  live write permissions when another app has changed the value.
- Extract individual energy counters from typed PointT energy items.
- Stop discovery and polling fallbacks on bulk-item rate limits, and share the
  cloud backoff across all gateway reads, writes, chunks, and queued requests
  belonging to one account client.
- Reject reauthentication with an account that has none of the configured
  gateways, preserving the existing credentials.
- Redact device identifiers in unavailable-resource debug messages.
- Retain privacy-safe fault-source evidence across restarts, require consecutive
  successful confirmations, and avoid reporting health from invalid fault data.
- Retain stale snapshots after fully failed polls so capability pauses cannot
  restore an unconfirmed value as fresh.
- Read the live holiday list before creation and editing. Merge only changed
  fields, preserve external changes, and reject conflicting concurrent edits.
- Retain the account rate-limit deadline across failed setup attempts and
  coalesce concurrent authentication refreshes for a rejected access token.
- Treat calculated environmental energy as a net total so asynchronous input
  updates do not trigger false meter resets in Home Assistant statistics.
- Enforce the response-size limit while reading decompressed response chunks.

### Behavior changes

- Calendar creation and editing read the current holiday list before writing.
  Apparently unchanged controls are also read back before a write is skipped.
  These checks add requests when performing the affected user actions.
- An account rate limit pauses requests across its gateways and remains in
  effect when Home Assistant retries a failed integration setup.
- Calculated environmental energy uses the `total` state class; raw cumulative
  energy counters retain `total_increasing`.

### Validation

- 604 automated tests pass with 95.36% coverage, including branch coverage.
- Regression tests cover incomplete fault data, restarts, concurrent calendar
  changes, failed polling, rate limits, concurrent token refreshes, and oversized
  responses.
- Ruff formatting and linting and strict Mypy validation pass.

### Upgrade notes and limitations

- Restart Home Assistant after updating. Existing entity and device identifiers
  are retained.
- Previously stored active faults without source evidence are retained until
  they are observed again, preventing an unverified fault clearance on upgrade.
- Calendar reads and writes are separate cloud requests. A change made by
  another app between those requests cannot be excluded atomically.
- These fixes have automated regression coverage; validation against a real
  heating installation remains necessary for this beta.

## [0.7.0-beta.2] - 2026-09-04

This beta closes enum gaps found while investigating an upstream report about
the `absolute` heating-circuit schedule type. Several additional PointT values
are now handled and translated explicitly.

### Fixed

- Added the `absolute` and `levels` values to
  `/heatingCircuits/{hc}/switchProgramMode`.
- Added exact mappings for heating and cooling control types, hot water
  following the heating schedule, heat-pump and heat-source variants, system
  layouts, energy-management activity, and the PV contact state.
- Accepted the alternative enum-name spellings for the gateway data
  processing states while retaining the already observed PointT spellings.
- Added English and German state translations for every newly recognized
  canonical value.

### Behavior changes

- Schedule-type sensors now recognize all known modes: `level`, `levels`,
  and `absolute`, even when the gateway does not advertise a complete enum
  list.
- PointT's legacy or product-specific spellings are normalized at the Home
  Assistant boundary without changing the underlying cloud data.
- Schedule type and technical configuration sensors remain read-only; this
  release does not add schedule editing or additional PointT writes.

### Security and compatibility

- Existing entity unique IDs, device identifiers, history, automations, and
  dashboards remain unchanged.
- Polling intervals, cloud-request counts, credentials, and write permissions
  are unchanged.
- Unrecognized future enum values continue to use the existing safe fallback
  instead of being passed to Home Assistant as an invalid enum state.

### Validation

- 547 automated tests pass with 95.16% branch coverage, including the
  newly handled raw values, canonical states, enum options, and matching German
  and English translations.
- Ruff formatting and linting and strict Mypy validation pass.

### Upgrade notes and limitations

- Restart Home Assistant after updating so frontend state translations are
  reloaded.
- The schedule type is still informational. Full schedule editing requires a
  separately verified PointT schema and remains outside this beta.

## [0.7.0-beta.1] - 2026-09-03

This beta adds a transparent calculated dew point for heating circuits
that report both room temperature and relative humidity, without adding PointT
traffic. It is intended for comparison with the controller's internal dew-point
display before the feature is included in a stable release.

### Added

- A **Dew point (calculated)** temperature sensor for every heating circuit
  with compatible `roomtemperature` and `actualHumidity` capabilities.
- Source temperature, source humidity, calculation method, and Magnus
  coefficients as attributes of the calculated dew-point sensor.
- A `value_source` attribute on total electricity consumption to distinguish a
  direct PointT value from the existing complete compressor-plus-auxiliary-
  heater fallback.

### Behavior changes

- Purely derived entity names consistently use the suffix **(calculated)** in
  English and **(berechnet)** in German. The existing system-pressure status
  now follows this convention.
- The dew-point sensor becomes unavailable with either source resource and
  never substitutes a room setpoint for a measured room temperature.

### Security and compatibility

- The calculation runs locally from already polled values and creates no new
  cloud request or write.
- Existing device identifiers and entity unique IDs remain unchanged. The
  system-pressure status receives only a display-name clarification.
- Invalid, non-finite, sentinel, or implausible input values produce no
  calculated measurement.

### Validation

- 504 automated tests pass with 95.13% branch coverage.
- Ruff formatting and linting and strict Mypy validation pass.
- Automated tests cover the Magnus calculation, invalid inputs, exact
  same-circuit capability matching, availability, Home Assistant metadata,
  attributes, and direct-versus-calculated electricity provenance.

### Upgrade notes and limitations

- The calculated dew point is not the controller's private dew-point value or
  cooling-flow setpoint and does not include an unknown installer safety
  offset or minimum-flow-temperature limit.
- The entity is created only where PointT exposes both required measurements
  with the expected type and unit.

## [0.6.0] - 2026-08-30

Version 0.6.0 makes PointT cloud traffic and response behavior observable
without exposing installation data or increasing the request rate.

### Added

- Rolling request metrics for the current and previous 59 clock-minute
  buckets, including successes, failures, exact HTTP statuses, request types,
  retries, fallbacks, rate limits, and bulk-item outcomes.
- Successful-response-time statistics for the last hour: average,
  approximate p95, maximum, latest attempt, and sample count.
- A memory-only diagnostics log containing up to 250 individual HTTP attempts
  from the last 60 minutes. Each entry records a sequence number, age, method,
  request type, outcome, response time, retry state, and safe bulk summaries.
- Three integration-wide diagnostic sensors for total requests, requests in
  the last hour, and average response time in the last hour.

### Behavior changes

- Retries and bounded single-resource fallbacks are identified on the actual
  HTTP attempt instead of being represented only by aggregate counters.
- Successful PointT bulk envelopes keep their outer HTTP status separate from
  aggregated `serverStatus` and inner gateway response statuses.
- All three new diagnostic entities are disabled by default and cause no
  additional cloud requests when enabled.

### Security and compatibility

- Recent request diagnostics never retain URLs, resource paths, gateway or
  config-entry identifiers, tokens, request bodies, response payloads, or
  resource values.
- History is bounded by age and count, held only in memory, and cleared on
  every Home Assistant restart.
- Existing device identifiers, entity IDs, polling intervals, controls, and
  dashboards remain unchanged.

### Validation

- 483 automated tests pass with 95.18% branch coverage, including rolling
  expiry, bounded history, retry and fallback accounting, bulk status
  separation, privacy, and disabled-by-default entity behavior.
- Ruff formatting and linting and strict Mypy type checking pass.

### Upgrade notes and limitations

- Restart Home Assistant after updating.
- Enable the optional request diagnostic sensors manually under **Settings →
  Devices & services → Entities** if continuous visibility is wanted.
- The p95 value is a bounded histogram estimate, not a raw-request percentile.
- Metrics restart at zero when Home Assistant restarts and are not a billing
  or provider quota counter.

## [0.5.5] - 2026-08-30

Version 0.5.5 gives known PointT percentage measurements their correct Home
Assistant semantics without changing discovery or cloud traffic.

### Added

- Humidity device classes for supported heating-circuit, room-zone,
  room-device, and ventilation humidity resources.
- Battery device classes for supported room-device battery levels.

### Behavior changes

- Pump modulation, fan output, radio signal, update progress, and power
  reduction remain generic percentages because they have different semantics.

### Security and compatibility

- Device-class selection requires both an exact allowlisted PointT path and the
  expected percent unit. Existing entity IDs, device identifiers, polling, and
  availability behavior remain unchanged.

### Validation

- 479 automated tests pass with 95.14% branch coverage.
- Ruff formatting and linting pass.
- Strict Mypy type checking passes.

### Upgrade notes and limitations

- No migration or reconfiguration is required.
- The change annotates only resources exposed by the connected installation;
  it cannot make a humidity or battery resource appear when PointT returns it
  as unsupported.

## [0.5.4] - 2026-08-28

Version 0.5.4 improves K30 compatibility when PointT accepts a
bulk request but reports temporary failures for every resource inside it.

### Added

- Diagnostics separately count bulk `serverStatus` and inner gateway-response
  status classes.
- Diagnostics group bounded individual fallback requests by a privacy-safe
  reason.

### Fixed

- Discovery and polling now retry individually when a correctly formed bulk
  item reports a temporary 5xx failure, not only when its payload is malformed.

### Behavior changes

- Bulk remains the default. Individual fallback stays bounded and still does
  not run for unsupported resources, authentication failures, or rate limits.

### Security and compatibility

- The new diagnostics contain only aggregate status classes and reasons. They
  contain no gateway identifiers, resource values, paths, or raw responses.
- Existing entity IDs, device identifiers, history, automations, and dashboards
  remain unchanged.

### Validation

- 469 automated tests pass with 95.13% branch coverage.
- Ruff formatting and linting pass.
- Strict Mypy type checking passes.

### Upgrade notes and limitations

- Affected MX300/K30 installations should retry setup after updating and share
  only reviewed, redacted diagnostics if discovery still fails.

## [0.5.3] - 2026-08-28

Version 0.5.3 improves compatibility with evolving PointT enum values and
makes incomplete but supported capabilities easier to diagnose.

### Added

- Diagnostics identify supported scalar capabilities that currently provide no
  value.
- Diagnostics count distinct undeclared enum values without retaining or
  exposing the values themselves.

### Fixed

- Enum values first reported after entity creation can no longer produce an
  invalid Home Assistant sensor state. The entity reports `unknown` until the
  integration knows the value.
- The PointT heat-source type `gas_boiler` is normalized to the existing
  translated **Boiler** state (**Heizkessel** in German).

### Behavior changes

- One privacy-safe warning is logged per affected entity, using only a
  normalized resource-path template.
- A heat-source type describes that heat source only and is not used to infer
  the type of the complete installation.

### Security and compatibility

- Raw unknown enum values, gateway identifiers, and resource values remain
  excluded from logs and diagnostics.
- Existing entity IDs, device identifiers, history, automations, and dashboards
  remain unchanged.

### Validation

- 466 automated tests pass with 95.16% branch coverage.
- Ruff formatting and linting pass.
- Strict Mypy type checking passes.
- German, English, and source translation catalogs are covered by repository
  tests.
- Dependency audit reports no known vulnerabilities.
- Hassfest, HACS validation, and CodeQL pass in the protected release workflow.

### Upgrade notes and limitations

- Restart Home Assistant after updating.
- Previously unknown enum values remain unavailable until they are explicitly
  understood and translated by the integration.
- No migration is required.

## [0.5.2] - 2026-08-28

Version 0.5.2 improves compatibility with MX300 installations using K30
gateways when individual PointT resources are available but the corresponding
bulk response cannot be processed.

### Added

- Privacy-safe diagnostics are now available while initial setup is still
  retrying.
- Request diagnostics distinguish HTTP-successful bulk items whose payload
  could not be parsed.

### Fixed

- Malformed successful bulk items and unreadable bulk envelopes are recovered
  through strictly bounded individual resource reads.
- MX300/K30 installations with divergent PointT bulk responses can complete
  discovery.
- Diagnostics no longer fail because runtime data is unavailable during the
  first setup attempt.

### Why bulk requests remain the default

A heating installation can expose dozens or hundreds of resources. PointT bulk
requests retrieve up to 30 resources with one HTTP request, reducing cloud
traffic, update latency, and the risk of rate limiting.

Individual requests are therefore used only as a bounded compatibility
fallback. Successfully parsed bulk resources are not requested again
individually.

### Behavior changes

- Discovery uses no more than 30 individual fallback reads in total.
- Normal polling uses no more than 5 fallback reads per affected bulk chunk.
- Unsupported resources returning 403, 404, or 406 do not trigger fallback
  requests.
- No entities, controls, entity IDs, or device identifiers are changed.

### Security and compatibility

- Debug output contains only normalized path templates, HTTP status, and parser
  error categories.
- Tokens, gateway identifiers, raw payloads, and resource values remain
  excluded.
- Authentication and rate-limit handling are unchanged.
- Existing Home Assistant history, automations, and dashboards are preserved.

### Validation

- 463 automated tests pass with 95.13% branch coverage.
- Ruff formatting and linting pass.
- Strict Mypy type checking passes.
- Dependency audit reports no known vulnerabilities.
- Hassfest, HACS validation, and CodeQL pass.

### Upgrade notes and limitations

- Restart Home Assistant after updating.
- An entry currently in `setup_retry` will retry setup automatically.
- The exact cloud-side response difference could not be reproduced locally.
  Confirmation on the affected MX300/K30 installation is still required.

## [0.5.1] - 2026-08-23

Version 0.5.1 corrects how Home Assistant identifies the controller and the
PointT communication gateway of an installation.

### Added

- Controller-aware device identification for MX300 and MX400 modules reported
  in structured `/system/info` data.
- Regression coverage for a real-world MX400 system reported through a
  K40/K40RF gateway and for ambiguous multi-controller data.

### Changed

- Home Assistant now identifies an MX300 or MX400 controller from structured
  `/system/info` module data instead of presenting a K30/K40 communication
  gateway as the heating-system model. K-series gateway identity and hardware
  remain available through diagnostics and gateway resources. Ambiguous or
  missing controller data falls back to a neutral Bosch/Buderus Heating device.
- Development tooling updates Hypothesis to 6.165.10, Mypy to 2.3.1, and Ruff
  plus its pre-commit hook to 0.16.3. Pytest remains at the Home Assistant test
  framework's required 9.0.3 release.

### Security and compatibility

- Device and entity identifiers remain unchanged, preserving existing entity
  IDs, history, automations, and dashboards.
- Recognition uses a strict MX300/MX400 allowlist. A K30/K40 gateway name alone
  is never used to infer a controller model, and conflicting controller data is
  not guessed.
- No additional cloud requests, permissions, stored credentials, or writable
  resources are introduced.

### Validation

- 453 automated tests pass with 95.21% branch coverage.
- Ruff formatting and linting and strict Mypy type checking pass.
- The model-selection test mirrors the observed MX400 plus K40RF structure and
  verifies that the gateway hardware remains available.

### Upgrade notes and limitations

- Restart Home Assistant after upgrading so the device registry receives the
  corrected model and name.
- Only MX300 and MX400 are currently recognized as controller models. Unknown,
  missing, or ambiguous controller information is displayed as the neutral
  Bosch/Buderus Heating device while gateway details remain available in
  diagnostics.

## [0.5.0] - 2026-08-23

Version 0.5.0 expands heat-pump monitoring with optional PointT
resources while retaining strict capability and schema checks.

### Added

- Optional operating values for emergency and standby mode, the PV contact,
  Smart Function, primary auxiliary-heater state, and passive-cooling inlet
  temperature.
- Per-source current power, power percentage, defrost state, and brine inlet
  and outlet temperatures for every heat-source ID reported by PointT.
- Optional read-only capabilities for heating-circuit boost and cooling,
  fresh-water stations, per-source energy, hybrid systems, low-noise and power
  limiting, solar, pool, ventilation, room zones, room devices, photovoltaics,
  and gateway-provided variable-tariff state.
- English and German names and state translations.
- A capability-gated Silent Mode select with Off, Automatic, and On options.
- A capability-gated auxiliary-heater mode select with Off, On, and Auto.
- A maximum-supply-temperature number control with whole-degree steps and an
  installation-specific range read dynamically from PointT. A broad 0–100 °C
  plausibility envelope rejects corrupt metadata. It is disabled by default
  because it is an installer-level setting.

### Changed

- Discovery combines gateway references with a bounded path catalog. Per-source probes are derived dynamically and do not assume `hs1`
  or `hs2`.
- Unsupported optional paths are discarded after the initial probe and never
  enter recurring polling.
- Fault discovery now derives failure paths only from real `hs<number>` heat
  sources. Ordinary heat-source resources no longer cause pointless
  `activefailure` and `failurelist` probes.
- Optional energy totals, water/gas consumption, volume flow, operating hours,
  power, pressure, humidity, and temperature values use native Home Assistant
  units and state classes when the PointT unit is known.

### Security and compatibility

- Optional resources remain read-only unless they have an explicit write
  policy. Silent Mode and auxiliary-heater mode require their exact writable
  `stringValue` schemas and complete allowed-value sets. Maximum supply
  temperature requires a writable Celsius number with complete gateway limits
  inside a broad 0–100 °C plausibility envelope.
- All three controls use the existing single-PUT transaction with mandatory,
  staggered read-back and no blind write retry.
- Entities are created only when the returned PointT type and unit match a
  known schema. Unexpected schemas remain excluded and visible in redacted
  capability diagnostics.
- Existing entity IDs, devices, and controls are unchanged. New values use the
  existing cadence groups and do not create new write permissions.

### Validation

- 451 automated tests pass with 95.20% branch coverage.
- The development build loads successfully on the physical Buderus K40 test
  system. That profile additionally exposes power-limitation and silent-mode
  state; unsupported optional families remain absent without setup errors.
- The K40 Silent Mode completed a physical `off` → `auto` → `off` write and
  read-back round trip. The original state was restored successfully.
- The K40 auxiliary-heater mode completed a physical `off` → `auto` → `off`
  write and read-back round trip. The heater itself remained off and the
  original setting was restored.
- The K40 maximum supply temperature completed a physical `40` → `41` → `40`
  °C write and read-back round trip. The original setting was restored.
- Ruff formatting and linting and strict Mypy type checking pass.

### Upgrade notes and limitations

- Restart Home Assistant after installing this release so discovery can
  probe the additional paths.
- Every value is optional and appears only when the connected gateway supports
  it. Historical `/recordings` data and the separate shared-energy tariff
  service remain intentionally excluded until an opt-in request and statistics
  model is defined.

## [0.4.1] - 2026-08-23

Version 0.4.1 fixes editing and moving ordinary holiday periods from Home
Assistant and gives the detailed holiday configuration clearer terminology
and consistent limits.

### Fixed

- Editing, moving, or deleting an ordinary holiday no longer fails with
  **Recurring holidays are not supported** when Home Assistant supplies its
  technical event identifier and the empty recurrence range used for a single
  event.
- Actual recurrence ranges such as `THISANDFUTURE` and recurrence rules such
  as `RRULE` remain explicitly rejected because PointT holiday periods do not
  support recurring events.
- A date or time-only change continues to preserve the existing heating,
  hot-water, ventilation, circuit-assignment, thermal-disinfection, and
  temperature settings.

### Changed

- **Constant temperature** now appears directly below the central-heating
  mode in the **Configure holiday** dialog, before the hot-water settings.
- Constant-temperature input is restricted to a 5–30 °C range. Stricter
  limits reported by the connected gateway still take precedence.
- English and German holiday labels now use clear terms, including
  **Apply for**, **Central Heating**,
  **Constant temperature**, **Hot Water**, **Ventilation**, **Setback**,
  **OFF with Thermal Disinfection**, and their German equivalents.
- A new step-by-step guide explains how to create, configure, move, rename,
  and delete holiday periods and why calendar details and heating-system
  settings use separate Home Assistant dialogs.

### Validation

- 417 automated tests pass with 95.03% coverage.
- Ruff formatting and linting and strict Mypy type checking pass.
- The updated integration was installed on the physical Buderus K40 test
  system; Home Assistant's configuration check and restart completed
  successfully.

### Upgrade notes

- No entity IDs, devices, or existing dashboard references are changed.
- Restart Home Assistant after installing or updating the integration.

## [0.4.0] - 2026-08-22

Version 0.4.0 turns the previously read-only PointT holiday calendar into a
safe, fully confirmed control. Holiday dates can be managed in Home
Assistant's calendar, while the new integration dialog exposes the additional
heating settings that the standard calendar editor cannot represent.

### Added

- Capability-gated create, edit, and delete support for the **Holiday periods**
  calendar using Home Assistant's standard calendar interface.
- Exact PointT holiday payloads, including date mode, affected circuits, heating, hot-water, ventilation,
  thermal-disinfection, fixed-temperature, and encoded-name fields.
- Privacy-safe diagnostics indicating whether holiday calendar writes are
  currently available. The diagnostics schema is now version 5.
- A separate **Configure holiday** dialog for circuit assignments, heating,
  hot-water and ventilation modes, thermal disinfection, and constant room
  temperature. Its fields and choices follow the live PointT configuration.

### Changed

- Editing a holiday changes only its dates and, where PointT supports cloud
  names, its name. All circuit assignments and operating-mode settings are
  preserved from the latest confirmed PointT value.
- New holidays use conservative defaults:
  all advertised circuits, fixed-temperature heating at 17 °C when supported,
  hot water off, ventilation off when supported, and thermal disinfection on
  when supported.
- PointT date-only end dates and Home Assistant's exclusive calendar end are
  converted in both directions without shifting the visible holiday period.
- Home Assistant's technical `recurrence_id` is accepted for ordinary event
  edits and deletions. Actual recurrence ranges and `RRULE` values remain
  unsupported.

### Security and compatibility

- Holiday writes are enabled only while the read-only list and the dedicated
  write configuration are available, current, and schema-valid.
- POST, PUT, and DELETE requests are each sent at most once. A timed-out
  mutation is resolved by bounded read-back and is never blindly repeated.
- Every mutation must be confirmed from `/holidayMode/list`; otherwise Home
  Assistant reports it as unsuccessful.
- Detailed holiday settings are accepted only from the options advertised by
  `/holidayMode/configuration`, and successful writes preserve the period's
  name and dates.
- Recurrence, location, and description fields are rejected because PointT
  does not support them. Unknown modes, malformed IDs, unsupported time steps,
  and incomplete existing periods are never written.

### Validation

- Creating, editing, configuring, and deleting a holiday was verified against
  a physical Buderus K40 installation. A date or time-only edit preserved all
  circuit assignments, modes, and temperatures.
- Multiple periods and Europe/Berlin daylight-saving transitions are covered
  by automated tests.
- 414 tests pass with 95.02% coverage. Ruff, Mypy, CodeQL, Hassfest, HACS
  validation, and the dependency audit pass.

### Upgrade notes

- No entity IDs or existing dashboard references are changed.
- Holiday controls appear only when the connected gateway advertises the
  required PointT resources. Unsupported fields remain hidden.
- Restart Home Assistant after installing or updating the integration.

## [0.3.0] - 2026-08-22

### Added

- Optional read-only holiday support for `/holidayMode/list`,
  `/holidayMode/configuration`, and `/holidayMode/activeModes`.
- A **Holiday mode active** binary sensor and a read-only **Holiday periods**
  calendar when the connected gateway exposes the corresponding resources.
- A **Next holiday** timestamp sensor showing the current or next period on
  the device page, with its end and status available as attributes.
- Tolerant handling of multiple periods, date-only and timestamp values,
  gateway or Home Assistant time zones, incomplete entries, and empty lists.

### Changed

- Holiday entities now use clear terminology:
  **Holiday mode active**, **Next holiday**, and **Holiday periods**, with
  corresponding German translations.
- The capability roadmap now reflects read-only holiday support as released.

### Security and compatibility

- Holiday mode remains separate from the existing Away mode.
- No holiday write, edit, or delete action is exposed before physical
  write/read-back verification.
- Unsupported holiday paths are discarded during capability discovery and
  therefore add no recurring requests.
- Diagnostics contain only support and parser counters, never holiday names,
  dates, raw payloads, or other user-entered details.
- The diagnostics schema is version 4 and exposes only bounded holiday support
  and parser-health metadata.

### Validation

- Verified all three holiday resources on a physical Buderus K40 installation
  with a configured MyBuderus holiday period.
- Confirmed discovery, parsing, calendar output, active-state handling, and the
  next-period timestamp in Home Assistant.

## [0.2.2] - 2026-08-22

This release corrects heating/cooling and circuit states. It replaces
previously inferred labels with exact raw values and clearer terminology.

### Added

- A read-only **Heating/cooling switching** entity is created for every
  dynamically discovered `/heatingCircuits/{hc}/suWiSwitchMode` resource.
  Supported states are idle, automatic switching, heating only, and cooling
  only. The entity is omitted when the gateway does not expose the resource.
- Complete English and German state labels for all heating-circuit and
  hot-water status values, including enabled, disabled, automatic,
  manual, cooling, holiday, extra hot water, and thermal disinfection states.

### Fixed

- **Current heating/cooling status** now uses the PointT values `off`, `forced`,
  and `cooling` instead of the previously inferred summer/winter enum.
- **Heating/cooling support** now recognizes `heat`, `cool`, and `heatCool`;
  camel-case values are normalized only at the Home Assistant boundary.
- **Season optimization** now recognizes `off`, `automatic`, `forcedHeat`, and
  `forcedCool`, with clear labels for automatic switching, heating only,
  and cooling only.
- **Heating circuit status** now includes the `ch_enabled` value
  in addition to disabled, emergency, floor-drying, summer-pause, boost, away,
  holiday, manual heating/cooling, and automatic heating states.
- **Hot water status** now uses the complete value set: enabled, disabled,
  automatic, off, Eco, Eco+, Comfort, extra hot water, away, holiday, floor
  drying, and thermal disinfection.

### Compatibility

- Existing config entries, devices, entity IDs, history, dashboards, and
  automations are preserved; no migration is required.
- Only displayed state values and translations change where an older inferred
  enum was incorrect.
- The new entity remains read-only. Heating/cooling switching is not exposed as
  a control until a physical write and read-back test confirms safe behavior.
- Unsupported optional paths are ignored after capability discovery and do not
  add recurring cloud requests.

### Quality

- 315 automated tests passed with 95.63% coverage.
- Ruff formatting and linting, Mypy, CodeQL, hassfest, HACS validation, and the
  dependency audit passed.
- The release contains a reproducible integration ZIP and a SHA-256 checksum.

## [0.2.1] - 2026-08-20

### Added

- Operating states for current heat demand, compressor, electric
  auxiliary heater, central heating, seasonal optimization, data processing,
  and iSRC support.
- German and English state labels, including blocked, defrost, home cooling,
  and pool modes.

### Enhanced

- Stable entities are now created for known array and multipart resources even
  when their startup payload is empty or temporarily incomplete.
- Dynamic PointT discovery is complemented by a bounded set of optional status
  resources. Unsupported resources remain ignored
  and do not add recurring cloud requests.
- Starts and operating times consistently provide total, heating, cooling, and
  hot-water entities, ready for values that become available after startup.

## [0.2.0] - 2026-08-19

### Added

- A guarded calculated environmental-energy sensor based on the complete
  PointT total energy balance.
- Active PointT fault and notification support with aggregate problem and count
  entities, appeared/resolved events, restart-safe deduplication, adaptive
  polling, tolerant parsing, and privacy-safe diagnostics.

### Changed

- Energy entities now use consistent context-first German and English names,
  including explicit calculated-value labeling and cooling-energy terminology.
- A direct PointT `electricity` counter and its component fallback now share a
  single total-consumption entity instead of creating duplicate entities.
- Default entity activation now follows an explicit user-facing policy:
  measurements, operating states, energy counters, starts, operating hours,
  TC3, pressure, and controls are enabled; sensitive, technical, and duplicate
  read-only entities remain opt-in.

## [0.1.0] - 2026-08-17

### Added

- Reproducible HACS release archives with a SHA-256 checksum and a tag-gated
  prerelease workflow that repeats all release checks before publishing.
- Capability-based firmware schema checks with an actionable rediscovery
  Repair when known PointT types or units change.
- A local exporter that turns the already-redacted Home Assistant diagnostics
  into a smaller, value-free installation inventory for compatibility work.
- Anonymous energy-counter reset counts in diagnostics while retaining Home
  Assistant's native `total_increasing` reset handling.
- Phase 0 repository and Home Assistant integration skeleton.
- Architecture, privacy, fixture, contribution, and security policies.
- Automated linting, type checking, tests, Home Assistant/HACS validation,
  dependency updates, and CodeQL analysis.
- Repository security baseline with CODEOWNERS, dependency auditing, protected
  branch rules, private vulnerability reporting, and squash-only merges.
- Internal, Home Assistant-independent PointT client with OAuth/PKCE helpers,
  serialized token refresh, typed reads, bounded bulk requests, error mapping,
  and diagnostics redaction.
- Home Assistant config flow for brand selection, SingleKey ID redirect
  validation, gateway discovery and selection, retry, and reauthentication.
- Typed config-entry runtime data with serialized token persistence and gateway
  availability checks.
- Gateway devices and a batch-based data coordinator with resource-level
  availability, last-good values, and separate fast and energy polling.
- Read-only sensors for central temperatures, modulation, system pressure,
  operating status, and direct PointT energy counters.
- Illustrated German beginner setup guide covering installation, SingleKey ID,
  the app redirect, gateway selection, troubleshooting, and safe support
  requests.
- Complete privacy-classified catalog of the 94 resources observed on the
  first K40 profile, including dynamic circuit rules, user-facing terminology,
  and staged write eligibility.
- Bounded batch discovery of the live PointT reference tree, dynamic devices
  for every discovered circuit and read-only entities for safe scalar and
  boolean resources.
- Cloud-friendly polling groups with 60-second live values, five-minute
  settings and energy counters, 15-minute long-term values, static startup
  data, last-good preservation, and bounded rate-limit backoff.
- Migration of early preview logical-device identifiers without losing entity
  IDs, history, areas, or dashboard references.
- Reference catalog of all current entities with PointT write metadata, Home
  Assistant access status, and polling frequency.
- Resource-level attempt, freshness, source, error, and failure state; bounded
  negative pauses for 403/404/504; a five-path core fallback after a batch
  failure; and a gateway circuit breaker after repeated complete failures.
- Gateway serial number, firmware, hardware, and model identifiers in Home
  Assistant device information, plus opt-in diagnostics for serial number,
  UUID, country, and sanitized system information.
- Six pressure-limit sensors from `/heatSources/systemPressureRange`, including
  support for the structured `values` object returned by PointT.
- A serialized PointT write transaction with strict live-capability validation,
  non-retried PUT requests, mandatory read-back, translated HA errors, and a
  heating-circuit operation-mode select verified on the reference K40.
- Dynamic, metadata-bounded controls for heating setpoints, hot-water mode and
  temperatures, extra hot water, fault reduction, and away mode while keeping
  administrative and installer resources read-only.

### Changed

- Use empirically confirmed one-degree steps for hot-water temperature controls
  while retaining half-degree steps for heating-circuit setpoints.
- Localize generated entity and group names from the configured Home Assistant
  system language, while preserving genuinely user-defined circuit names.
- Clarify the three hot-water temperature levels as Comfort, Eco, and Eco+
  start temperatures; document the related PointT-blocked stop temperatures
  and charging deltas from the appliance expert menu.
- Consolidate each gateway into one Home Assistant device and prefix entity
  names with dynamic groups such as `Heating circuit 1 –` and `Heat pump –`.
- Classify the six pressure-limit entities as diagnostics instead of primary
  operating sensors.
- Organize service counters and TC3 as diagnostics and outdoor temperature and
  away mode as primary values.
- Use clearer labels for heating and hot-water temperature levels and decode
  PointT's UTF-16/Base64 configured names for display.
- Retire the empty switch-program placeholder and the duplicate aggregate
  start counter while preserving the detailed compressor counters, and omit
  optional circuit-name sensors when no name is configured.
- Represent static system information as a readable module-and-version text
  instead of an unhelpful module count while retaining sanitized attributes.
- Refine German heating labels while keeping clearer energy terminology where
  needed.
- Use the detected Bosch or Buderus brand for device manufacturers and gateway
  names, and identify PointT heat-pump sources as `Heat pump`.
- Redrew the brand icon with a compact symmetric three-blade fan and added its
  SVG source.
- Replaced the planned external Python client distribution with an internal
  `pointt` package that ships with the integration.
- Aligned the development and test environment with Home Assistant 2026.8 on
  Python 3.14.2.
- Expanded setup, reauthentication, retry, and error messages in English and
  German with step-by-step instructions.
