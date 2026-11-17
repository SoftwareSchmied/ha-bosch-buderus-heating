# Heating controls

## English

This guide describes the controls included in stable **0.8.2** and introduced
in **0.8.2-beta.7**, including
the locally tested changes from PRs #85 to #87. The older 0.8.2-beta.6 and
0.8.1 packages do not contain these controls.

I added **Settings > Devices & services > Bosch/Buderus Home Comfort > Configure >
Operate appliance**. When an entry contains several PointT gateways, I ask
which appliance to operate first. The dialog groups the discovered controls
into heating circuit, hot water and system tabs. Additional temperature
levels and technical settings stay visible under **More settings** in each tab.
**Escape** closes the dialog just like the header's close button and discards
unsaved edits. If a choice list is open, the first Escape closes that list.

### Everyday use

1. Open **Configure > Operate appliance** and choose the appliance if prompted.
2. Select a heating-circuit, hot-water or system tab and edit the desired fields.
3. Select **Apply** and wait for confirmation. Editing alone does not write.
4. Use **Discard** to undo the draft, or **Escape** to leave without applying it.

For a quick temperature change, open the climate entity from the device page
or a dashboard. Select manual/Heat mode first, then change the target. A mode
change and a temperature change are separate commands and apply immediately.
In Auto, the heating schedule determines the target; the climate does not
override it. A missing room-temperature reading remains unknown.

The hot-water entity opens its own native dialog. It changes the offered
operating mode, while Eco, Eco+ and Comfort start temperatures remain separate
settings. Its active target is a read-only value. See [PointT hot water](pointt-hot-water.md).

### Applying and refreshing drafts

Editing a field changes a draft. **Apply** validates all submitted values
before writing, then confirms each changed setting by reading it back.
**Discard** restores the values displayed when the form opened.
**Reload values** discards the draft and uses the latest regular polling
results. Opening, editing and reloading the dialog do not query the cloud.
German and English labels follow the user's profile language.

Before each write, I also reread the relevant resource and reject a draft
if its value or metadata has changed. Writes are serialized with polling and
other integration writes. This check cannot make cloud writes atomic:
another controller can still act between a read and a write. Multiple
settings are separate transactions. On failure, confirmed earlier changes
remain applied and the last attempted change may have an uncertain outcome.
The dialog explains this and retains the remaining draft.

Since 0.9.2, every control follows the `used` and `available` flags when the
appliance reports them, not only the controls of room controllers. A setting
reported as not available gets no control (an existing entity becomes
unavailable). A setting reported as not used keeps its control, but a change
is refused before anything is sent. Malformed flags give no control. Without
flags nothing changes. The zone controls of a room controller keep their own
flag rules.

The tabs show the heating circuits in numerical order first, then hot water,
then the system. After **Apply** or **Reload values**, the dialog stays on the
open tab; after a successful write it shows the tab of the last confirmed
change. If a value is rejected, the dialog opens its tab and marks the field
until it is edited. If writing stops, it opens the tab of the affected change.
The arrow keys and spinner of a number field use the step of that setting.
An unconfirmed write or failed preflight read makes the affected resource
unavailable until regular polling confirms it again. Reloading the dialog
does not turn its previous cached value into a current reading.

On the device page, I use shorter control names, relevant icons and number
inputs instead of sliders. Home Assistant controls the page layout.
Existing entity IDs, unique IDs, custom names, disabled states, history and
device assignments remain intact. A new installation may generate shorter
default entity IDs from the new names. No registry migration is needed.
Controls on the device page still write individually when edited.
The original heating-circuit mode and manual-target controls are under
Configuration; the climate is the combined everyday control.

### Heating-circuit climates

For the corresponding native hot-water display and operating-mode control,
see [PointT hot water](pointt-hot-water.md).

I add one climate entity per eligible, discovered PointT heating circuit.
Its identity uses the gateway and the exact circuit key, including HC2 and
later circuits. It remains on the existing appliance device. Climate entities
can appear during regular polling when the necessary metadata becomes available.
The original number and select entities remain usable by existing automations.

The supported PointT codes map as follows:

| PointT | Home Assistant | Temperature |
| --- | --- | --- |
| `manual` | Heat | Reads and writes `manualRoomSetpoint` |
| `auto` | Auto | Writes `temporaryRoomSetpoint` when the circuit offers it; otherwise exposes `currentRoomSetpoint` as the temperature attribute, changes disabled |
| `off` | Off | No temperature command |

Only advertised, understood codes appear. An unknown current mode makes
the climate unavailable; the original select retains additional advertised
codes. A temperature call never changes a mode. Combined mode/temperature
calls are rejected before any write; select the mode separately first.
Bounds and units come from that circuit. I convert temperatures and steps
between the reported unit and Home Assistant's configured unit.
Without a writable temporary setpoint, Home Assistant's standard thermostat
does not display a target control in automatic mode because this mode does
not support temperature commands there. The reported target remains
available through the climate's `temperature` attribute and the existing
current-target sensor.

### Temporary setpoint in automatic mode

Since 0.9.0, a heating circuit that reports a writable
`temporaryRoomSetpoint` with limits takes a target in automatic mode as well.
This is meant for heating circuits without a room controller; the zones of a
room controller keep their own climates (see
[Room controllers](room-controllers.md)), and their circuits get no heating
circuit climate.

- **Target.** In automatic mode the climate writes the target to
  `temporaryRoomSetpoint`. The program stays selected; the controller
  replaces the target for now. In manual mode the climate keeps writing
  `manualRoomSetpoint`.
- **Limits and step.** The target lies within the limits the circuit reports
  for the temporary setpoint and on its reported step, counted from the
  reported minimum (0.5 K without a step). Some circuits report -1 or 0 as
  the minimum because these values end the temporary setpoint or switch the
  circuit off for now. The climate never writes them as a target: its lower
  limit is then the minimum of the manual setpoint, and without one the
  climate offers no target in automatic mode. Since 0.9.2 this lower limit
  stays shown while the last reading of the manual setpoint is still valid,
  so one missed reading no longer hides the target; a change still needs a
  current reading.
- **Shown target.** While a temporary setpoint is active (a reading of 0 or
  more), the climate shows it and the attribute `temporary_override` is
  `true`. A negative reading (-1) means none; the climate then shows the
  program's target from `currentRoomSetpoint` and the attribute is `false`.
  A reading of 0 (switched off for now) counts as active, but the climate
  shows the program's target.
- **Clear button.** A button "Clear temporary setpoint" ends an active
  temporary setpoint by writing -1. When the reported limits include -1, it
  must lie on the reported step. Since 0.9.2 I also add the button when the
  limits leave -1 out (for example 5 to 30), because -1 ends a temporary
  setpoint there as well; this button is created disabled, and any negative
  reading afterwards confirms that no temporary setpoint is active. The
  button is available only in automatic mode while a temporary setpoint is
  active. Clearing outside the limits is expected, untested on a real
  device.
- **Write rules.** Each change reads the mode, the temporary setpoint and the
  known heating context on the appliance first and refuses the write when
  one of them has changed. It then sends exactly one request and confirms
  the value by reading it back; it never sends the request again. A
  circuit with the control type `constants` takes no target. Since 0.9.2 the
  control type is read on the appliance together with the other values
  before every climate write (mode, target, temporary setpoint and clear
  button); if it changed since it was shown, nothing is sent. With the
  control type `ISRC` the circuit takes no temporary target and offers no
  clearing; the manual setpoint stays.
- **No entity of its own.** The temporary setpoint gets no number entity and
  no row in the controls dialog. The existing sensor of the temporary
  setpoint stays as it was.

When a temporary setpoint ends by itself (for example at the next switch
point of the program), the climate shows it until the next regular reading
of the temporary setpoint. This whole function is expected, untested on a
real device; reports are welcome.

When the circuit reports the control type `constants` (`controlType`), I
treat it as a circuit that cannot be controlled. Since 0.9.0-alpha.6 the
climate then only shows the circuit: it offers no target, no other mode and
no off command, and a mode or temperature call is refused before any request.
This applies to every gateway that reports this control type. Without a
current reading of `controlType` the last known value applies, so the climate
becomes controllable again only after a reading of another control type.
Since 0.9.2 the manual setpoint, the operating mode and their rows in the
controls dialog follow the same rule: before each change they read the
control type and the setting itself again, nothing is changed on
`constants`, and the limits come from that reading. This is expected,
untested on a real device.

I show room temperature only from that circuit's valid `roomtemperature`
resource. I do not substitute a flow temperature, infer active heating from
a temperature difference or claim that a winter-mode flag proves heat output.
No heating-action state is invented.

Cooling is outside this climate implementation. A reported cooling-only
circuit, active cooling or stale/unknown heating context makes the climate
unavailable. Existing individual controls remain available under their own
contracts. The mode, target and known heating context are checked again on
the appliance before a climate write. Scheduling and cooling setpoints are
not changed by this climate; a temporary setpoint only as described above.
This includes the central actual heating/cooling status when reported, even
when the heating circuit has no individual status resource.

Automated tests include a complete Home Assistant browser flow against a
simulated gateway, covering repeated applies, retained invalid drafts,
uncertain writes and recovery through regular polling.
I installed this build on Home Assistant 2026.10.3 and tested the first
heating circuit of a real installation in manual mode. Applying 19.0 to 18.5 °C in the controls dialog
was confirmed by appliance read-back and both the number and climate entities.
In the repeated test, I used the native climate dialog's plus button to restore
19.0 °C. The manufacturer's app showed both 18.5 °C and the restored 19.0 °C.
A conditional, timed restore in Home Assistant also protected both tests
independently of the browser; it restored the value during the first test.
This does not confirm mode changes, other circuits or other appliances.

This practical check also caught lost keyboard focus after Apply. I corrected
it and verified Escape directly after Apply and Reload values in the real
browser and the automated Home Assistant test, without an extra click.

### Heating-circuit climates in HomeKit

The Home Assistant HomeKit bridge shows a climate entity with two
temperatures: the current temperature and the target. Two things can look
wrong on a plant without room sensors; both come from how the bridge treats
the values, not from a reading of the appliance.

- **21 degrees as the current temperature.** A heating circuit reports a
  current temperature only when the controller has a room temperature for it
  (`roomtemperature`). Without one, the climate reports none, which is what
  Home Assistant expects. As far as I can tell from the HomeKit integration,
  the bridge then keeps a fixed start value of 21 degrees for the current
  temperature, so the large number on the tile is that placeholder. The
  target on the tile is the real one. I do not fill the current temperature
  with the supply or outdoor temperature, because neither is a room
  temperature. How to hide the tile is a setting of the bridge, not of this
  integration.
- **Target at night.** In automatic mode the target is the setpoint the
  controller currently applies (day, night or setback per its program).
  A controller that reports 0 for a circuit that is switched off in that
  period gives a target of 0. As far as I can tell, the bridge ignores a
  target of 0 and keeps the last one it had, so HomeKit can keep showing the
  day value at night. Home Assistant itself shows what the controller
  reports.

Observed in the diagnostics and screenshots of a user's boiler plant; not
tested with a bridge on my side.

### Heating circuit settings: summer/winter, cooling and temperature levels

Since 0.9.2 a heating circuit gets these settings as number and select
entities (category configuration) and as rows in the controls dialog, when
the circuit reports them writable and with limits or options. I tested
them only with simulated data in
automated tests; they are **expected, untested on a real installation**.
Reports are welcome.

| Setting | Path | Entity | Enabled by default |
| --- | --- | --- | --- |
| Summer/winter switching | `suWiSwitchMode` | select | yes |
| Stop heating at outdoor temperature | `suWiThreshold` | number | yes |
| Cooling: room setpoint | `cooling/roomTempSetpoint` | number | no |
| Start cooling at outdoor temperature | `cooling/outdoorThreshold` | number | no |
| Away temperature | `awayTemperature` or `temperatureLevels/awayTemperature` | number | yes |
| Cooling temperature | `cooling/temperatureLevels/on` | number | no |
| Heating temperature, setback temperature | `temperatureLevels/comfort2`, `eco` | number | yes (since 0.9.0) |

- **Summer/winter switching** of one circuit (iCom/KM200 gateways): the
  options are the codes the circuit offers among `automatic`
  (Automatic), `forced` (Heating only), `cooling` (Cooling only) and `off`
  (Off); another code is left out, as in the app. The central season
  switching of a K40 stays as it is (see [season switching](season-switching.md)).
  After switching to cooling only, the circuit's climate becomes
  unavailable while the circuit cools.
- **Heating limit and cooling threshold:** the outdoor temperatures where
  heating stops and cooling starts. The app shows them only in the mode
  automatic; I keep them writable in every mode, they take effect in the
  mode automatic. The cooling threshold exists only while the circuit's
  summer/winter switching offers cooling.
- **Cooling setpoint:** the room target of the cooling (heat pumps with
  cooling only). 0 means "off" in the
  app and is never written; when the circuit reports a minimum of 0 or
  below, the lowest value offered is the first value of the grid above 0.
  Without reported limits there is no entity.
- **Temperature levels:** only the four level names the app knows are
  operated: heating (`comfort2`), setback (`eco`), away (`awayTemperature`)
  and cooling (`on`). A level with another name stays a reading; the
  diagnostics count such names without showing them. A level without a
  value gets no entity. A level value applies to every switch point of the
  weekly programs that uses this level.
- **Steps:** the step the circuit reports, else 1.0 for the thresholds and
  0.5 for the temperatures, counted from the reported minimum. A value off
  the grid or outside the limits is refused, not rounded.
- **Write rules:** each change reads the circuit's control type, the setting
  itself and the settings it depends on again on the appliance, in one
  request, right before its one write: the summer/winter switching for the
  two thresholds, the other levels for the heating, setback and away level.
  If one of them changed since it was shown, nothing is sent. The heating
  and setback levels limit each other (the heating level's minimum follows
  the setback level and the other way round); the limits of a level write
  therefore come from this fresh reading. Since 0.9.2 this also applies to
  the heating and setback levels that exist since 0.9.0. The value is then
  confirmed by reading it back and never sent again.
- **Locks:** with the control type `ISRC` (a room controller runs the
  circuit) none of these settings is changed, as in the app. With the
  control type `constants` no temperature level is changed. A setting the
  circuit reports as not available gets no control; one it reports as not
  used refuses changes.
- **Room controllers:** the circuits of a TC100.2 or CT200 keep their own
  settings (see [room controllers](room-controllers.md)); none of these
  entities is created for them.
- **Only after the room controller check:** these settings are created,
  offered and written only after a finished discovery got a definite answer
  whether the gateway is a room controller (its zone list and product type).
  Until then, for example while a server error leaves that open, they wait,
  so a room controller recognised late never shows them, not even for a
  moment.

### Variable electricity tariff (read only)

Since 0.9.2 an iCom gateway is asked once during discovery whether its
tariff optimisation is on (`/application/tbso/enable`) and for the number
of its tariff (`/application/tbso/electricity/tariffID`). If it answers,
the diagnostic entities **Variable tariff active** and **Tariff ID** appear,
both disabled by default. The tariff's name comes from a separate service
of the manufacturer, which this integration does not use; the number 5
means "no tariff chosen" and gives the attribute `tariff_selected: false`.
A refusal, a missing path or a server error counts as "not there" and does
not affect the rest of the discovery. Nothing is written: in the app,
choosing a tariff takes several requests and also changes the heating
circuits. The tariff readings of a K30/K40 (`/system/variableTariff/*`)
stay as they were since 0.9.0, as disabled diagnostic sensors. Expected,
untested on a real installation.

### Heating and hot water schedules

Since 0.9.0-alpha.6, **Configure > Heating and hot water schedules** changes
the weekly programs A and B of a heating circuit and program A of the hot
water. I list a program only when the gateway reports it as writable and
in use, it passes the strict program check, its type is a level program
(`level`, from a current reading where the circuit reports its type), and
every level it uses belongs to the circuit's reported temperature levels.
The program type itself and the program names are never written.

1. Choose a program. The week shows every day's switch points and the limits
   the gateway reports, for example at most 2 points per day, 14 per week and
   times in 15-minute steps. A heating program that is not the active one,
   and the hot water program, which applies only in the hot water mode Auto,
   say so.
2. Edit a day: change the time or level of a switch point, delete it or add
   one, within the reported limits. Or copy a day onto other days.
3. The changes collect in a draft of the whole week. The last step shows the
   changed days before and after. Only then is the week sent.

The program is read again just before sending, on a heating circuit
together with its program type. If the program differs from what the dialog
showed, or its type is no longer a level program or cannot be read, nothing
is sent and the dialog says why. The whole week goes out once as a raw JSON
array and is read back; the confirmed program is shown at once. A refusal of
the gateway changes nothing. After a lost answer, a server error or an
accepted request whose answer cannot be read, the outcome is resolved only
by reading, never by sending again; until then the program counts as not
current. The success message repeats the previous switch points, so they
can be set back by hand. Diagnostics count the writes and the switch points
per day, without times or levels.

The selection **Active schedule** switches between programs A and B. It
appears only when the gateway allows both, the current one among them, and
both programs can be read and are valid. The options are `a` and `b`; the
reported names are in the attributes `program_names` and `program_name`.
With a single program, as on some heat pumps, there is no selection.

Changing hot water program A was confirmed on a Logatherm WLW186i-12 E with
an MX400: a switch point changed in Home Assistant showed up in the
manufacturer's app, and setting it back there showed up in Home Assistant
shortly afterwards. The heating programs, the selection of the active
schedule, program B, iCom gateways and programs with absolute temperatures
are tested with simulated data only and stay expected, untested.

#### Further program kinds

Since 0.9.2-beta.2 the same dialog also lists these programs, where the
gateway reports them:

| Program | Path | Setpoints | Changeable when |
|---|---|---|---|
| Heating program A/B with temperatures | `/heatingCircuits/{hc}/switchPrograms/A` (`B`) with program type `absolute` | Temperature as text, such as `"21.5"`; `"0.0"` is off | the program reports `switchPointMin`, `switchPointMax` and `switchPointResolution`, and the program type reads `absolute` |
| Cooling program | `/heatingCircuits/{hc}/cooling/switchPrograms/A` | `on` or `off` | the circuit's cooling level `cooling/temperatureLevels/on` can be read |
| Zone programs | `/zones/{zone}/switchPrograms/heating` and `.../cooling` | Temperature as text (or as a number, as read) | range and step are reported |
| Ventilation program | `/ventilation/zone1/switchPrograms/cp` | `min`, `red`, `nom`, `max`, `dem`, `off` | the levels are listed as references of `ventilationLevels`; only those are offered |
| WDDW2 hot water program | `/dhwCircuits/dhw1/switchPrograms/program1` | Whole degrees as text, or `off` | `manualSetpoint` reports its range; the unit is switched on, runs in schedule mode and has a time zone (as in the manufacturer's app) |
| Air conditioner programs (PointT gateway of type `rac`) | `/airConditioning/switchPrograms/program1` ... `program9` | Mode, temperature and fan speed, such as `cool 24.0 auto`, or `off` | the program configuration is readable; see [air conditioners on a PointT gateway](pointt-air-conditioners.md#changing-switch-points) |

Every one of them also needs the reported limits (`maxNbOfSwitchPoints`,
`maxNbOfSwitchPointsPerDay`, `switchPointTimeRaster`); I never assume
default values. A program without them is shown as a sensor,
not offered for changes, and the diagnostics name the reason. A temperature
outside the range or off the step is refused, never rounded. Off (`"0.0"`)
is offered only where the program already holds it. The cooling program is
written as program `A` only, the zone heating and cooling programs
separately. The WDDW2 and air conditioner programs are sent as the object
`{"switchPoints": [...]}`; all others go out as a raw array. A WDDW2 day
without points has no setpoint and an air conditioner is off before the
first point of a day; the others carry the previous day's last point over.
These two are written as the manufacturer's app writes them: a WDDW2 `off`
at 00:00 is left out, and an air conditioner day that follows a day ending
with the unit on gets an explicit `off` at 00:00 (it counts against the
limits).

The sensor of an absolute or zone program shows the temperature scheduled
now, a WDDW2 sensor as well; a switched-off point shows no temperature and
the attribute `scheduled: off`. The attributes also hold `allowed_levels`
(level programs) or `min_setpoint`, `max_setpoint` and `setpoint_step`.

None of these programs has been read or changed on a real installation:
their forms, reading and writing them are expected, untested. A diagnostics file of such a system shows the form
(key names and value types, never values) and helps to confirm it.

#### Weekly view

The week step of the dialog (and the RRC schedule view) shows the week as a
read-only table: seven days from midnight to midnight, one segment per
switch point, its level or temperature as text and colour, the part
carried over from the previous day marked. An RRC day without points says
so and is not shown as off. The view only displays; changes still go
through the dialog steps. If the view cannot be loaded, the text of the
week stays as before. Times are those of the heating system's clock.

#### Program names

The names of programs A and B (`switchPrograms/nameA`, `nameB`) are now
decoded as their `valueConfig` says, so a Base64 name shows as text. Where
the gateway reports a name as writable with a complete coding (`charset`,
`codingType` `base64` or `ascii`, `maxLength`), a text entity **Time
program name** changes it (disabled by default, category configuration).
The name is read again before the single write and compared with the one
shown; the read-back compares the decoded name. Too long names and
characters the coding cannot hold are refused; an empty name is not sent.
iCom gateways get no name control: their app does not write names. Expected,
untested on a real installation.

#### Action `bosch_buderus_heating.set_switch_points`

The integration's first action sets the switch points of one or more
program sensors from automations and scripts:

```yaml
action: bosch_buderus_heating.set_switch_points
target:
  entity_id: sensor.heating_circuit_1_time_program_a
data:
  mode: replace_days  # or replace_week (default)
  switch_points:
    - {day: sa, time: "08:00", level: comfort2}
    - {day: sa, time: "22:30", level: eco}
  validate_only: false
response_variable: result
```

`level` is a level key (see the sensor attribute `allowed_levels`) or, for
programs with temperatures, a number such as `21.5`. Quotes are safest in
YAML; an unquoted `on`/`off` and a time such as `22:00`, which YAML turns
into a boolean and a number, are understood as well. `expected` (optional)
lists the switch points the program must hold now; otherwise nothing is
sent. The base of every change is a fresh reading of the program; it is
read a second time right before the single PUT, and a change in between
sends nothing. The same rules as in the dialog apply. The response holds,
per target, `result` (`sent`, `unchanged` or `validated`), `confirmed`, the
previous and the new switch points by weekday, never a path or ID. Several
targets are handled one after the other; one that fails does not undo
another one. Nothing is repeated automatically; an automation that repeats
a call does so on its own account.

Program sensors of programs the gateway reports as writable are now
enabled by default when they are first registered, so the action finds its
targets; existing entities keep their state. The action is tested with
simulated data only; the write behind it is the dialog's.

### Why is the supply temperature above the setpoint?

While a heat pump heats, its supply temperature often stays a few kelvin
above the current supply setpoint. The sensor **Supply above setpoint
(calculated)** shows this difference (see
[calculated values](calculated-values.md#run-time-per-start-and-supply-above-setpoint)).
In mild weather this is usually normal. Over several mild days (about 4 to
11 °C outdoors) on a real installation, an air-to-water heat pump with one
underfloor circuit and a buffer, the readings showed:

- **Minimum output:** the compressor ran at its lowest speed almost all the
  time it heated. Even that lowest output was more heat than the heating
  curve asked for, so the water warmed up until the floor took the heat
  away. The supply settled about 1 to 3 K above the setpoint (typically
  about 2.4 K). When the setpoint rose or fell by 1 K, neither the
  compressor speed nor the supply changed.
- **Stop:** the heat pump stopped when the supply reached about 4 K above
  the setpoint.
- **Start:** it started again only after the shortfall below the setpoint
  had added up over time (degree minutes, there about 40 K·min). At that
  moment the supply was only 1 to 2 K below the setpoint.
- **Circuit pump and room thermostats:** when many room thermostats closed
  their valves, the circuit pump moved less water, the supply rose faster
  and the heat pump stopped after a short run. Most short runs came from
  this, not from the setpoint.

So a lower heating curve does not lower the supply in mild weather; it
mainly brings the stop point closer, and the heat pump stops more often.
When it gets colder and the house needs more than the minimum output, the
compressor speeds up and the difference shrinks. The start and stop
thresholds and the minimum speed are controller settings that the
interface does not offer (see the next section). These are the values of
one installation; other models and settings can behave differently.

### Values only available in the service menu

Some settings are made in the controller's service menu, usually by the
installer. On heat pumps with a K30/K40 gateway the interface the
integration uses (cloud and local) does not offer them, so the integration
can neither show nor change them:

- the **minimum supply temperature** of a heating circuit (the maximum
  supply temperature is available where the system reports it);
- the **pressure setpoint of the heating circuit pump** (PC1): neither the
  controller signals nor the local or cloud resources of a real
  installation report one;
- the **limits of the auxiliary heater**, such as its maximum power, its
  delay or the outdoor temperature below which it may help; its operating
  mode, status and power are available where reported;
- the **charging delta** of the hot water, that is how far the tank cools
  down before charging starts; the hot water temperatures the system
  reports stay available;
- the **building type**, which the controller uses to damp the outdoor
  temperature;
- the **start and stop thresholds** of the heat pump (degree minutes,
  overshoot) and its minimum compressor speed.

Room controllers of the RRC 2.0 platform report their heating curve and
both supply limits themselves (see [room controllers](room-controllers.md));
hybrid systems with a hybrid manager report their bivalence point.

## Deutsch

Diese Anleitung beschreibt die Bedienung in Stable **0.8.2**, eingeführt mit **0.8.2-beta.7**,
einschließlich der lokal getesteten Änderungen aus PR #85 bis #87.
Die älteren Pakete 0.8.2-beta.6 und 0.8.1 enthalten diese Bedienung nicht.

Ich habe **Einstellungen > Geräte & Dienste > Bosch/Buderus Home Comfort >
Konfigurieren > Anlage bedienen** ergänzt. Bei mehreren PointT-Gateways
erscheint zuerst eine Anlagenauswahl. Reiter trennen die erkannten Heizkreise,
Warmwasser und die Anlage. Weitere Temperaturstufen und technische
Einstellungen bleiben im jeweiligen Reiter unter **Weitere Einstellungen** sichtbar.
**Escape** schließt den Dialog wie das Kreuz in der Kopfzeile und verwirft
ungespeicherte Eingaben. Bei geöffneter Auswahlliste schließt das erste Escape
zunächst die Liste.

### Im Alltag bedienen

1. Öffne **Konfigurieren > Anlage bedienen** und wähle bei Bedarf die Anlage.
2. Wähle den Reiter für Heizkreis, Warmwasser oder Anlage und ändere die Felder.
3. Wähle **Übernehmen** und warte auf die Bestätigung. Eingeben allein schreibt nicht.
4. **Verwerfen** setzt den Entwurf zurück. **Escape** schließt ohne Übernehmen.

Für eine schnelle Temperaturänderung öffnest du die Klimaentität von der
Geräteseite oder einem Dashboard. Wähle zuerst den manuellen Heizbetrieb und
ändere danach den Sollwert. Betriebsart und Temperatur sind getrennte Befehle
und werden sofort angewendet. Im Automatikbetrieb bestimmt das Heizprogramm
den Sollwert; die Klimaentität überschreibt ihn nicht. Eine fehlende
Raumtemperatur bleibt unbekannt.

Die Warmwasserentität öffnet einen eigenen nativen Dialog. Dort änderst du die
angebotene Betriebsart. Die Starttemperaturen für Eco, Eco+ und Komfort bleiben
getrennte Einstellungen. Der aktive Sollwert ist eine reine Anzeige.
Siehe [PointT-Warmwasser](pointt-hot-water.md).

### Entwürfe übernehmen und Werte aktualisieren

Eingaben bleiben zunächst ein Entwurf. **Übernehmen** prüft alle Eingaben,
schreibt die geänderten Einstellungen und bestätigt jede durch Rücklesen.
**Verwerfen** stellt die beim Öffnen angezeigten Werte wieder her.
**Werte neu laden** verwirft den Entwurf und verwendet die Ergebnisse der
regulären Abfragen. Öffnen, Bearbeiten und Neuladen lösen keine Cloud-Abfrage
aus. Die Texte folgen der Profilsprache.

Vor jeder Änderung lese ich die betreffende Ressource erneut. Geänderte
Werte oder Metadaten führen zum Abbruch. Schreiben und Polling der Integration
laufen dabei nacheinander. Andere Steuerungen können trotzdem zwischen
Rücklesen und Schreiben eingreifen. Mehrere Einstellungen werden einzeln
geschrieben. Bei einem Fehler bleiben bereits bestätigte Änderungen wirksam;
die zuletzt versuchte Änderung kann bereits übernommen worden sein.
Der Dialog erklärt das und erhält die verbleibenden Eingaben.

Seit 0.9.2 folgen alle Regler den Kennzeichen `used` und `available`, wenn
die Anlage sie meldet, nicht nur die Regler von Raumreglern. Eine als nicht
verfügbar gemeldete Einstellung erhält keinen Regler (eine bestehende Entität
wird nicht verfügbar). Eine als nicht genutzt gemeldete Einstellung behält
ihren Regler, eine Änderung wird aber vor dem Senden abgewiesen. Fehlerhafte
Kennzeichen ergeben keinen Regler. Ohne Kennzeichen ändert sich nichts. Die
Zonenregler eines Raumreglers behalten ihre eigenen Regeln.

Die Reiter zeigen zuerst die Heizkreise in numerischer Reihenfolge, dann
Warmwasser und zuletzt die Anlage. Nach **Übernehmen** oder **Werte neu laden**
bleibt der geöffnete Reiter erhalten; nach erfolgreichem Schreiben zeigt der
Dialog den Reiter der zuletzt bestätigten Änderung. Wird ein Wert abgelehnt,
öffnet der Dialog dessen Reiter und markiert das Feld, bis du es änderst.
Bricht das Schreiben ab, öffnet er den Reiter der betroffenen Änderung.
Pfeiltasten und Drehfeld eines Zahlenfelds verwenden die Schrittweite der
jeweiligen Einstellung.
Nach einem unbestätigten Schreibversuch oder einer fehlgeschlagenen
Vorabprüfung ist die betroffene Ressource bis zur nächsten erfolgreichen
regulären Abfrage nicht verfügbar. Das Neuladen des Dialogs macht einen
alten gespeicherten Wert nicht wieder zu einem aktuellen Messwert.

Die Geräteseite erhält kürzere Namen, passende Symbole und Zahlenfelder.
Die Anordnung bestimmt Home Assistant. Bestehende Entitäts-IDs, eindeutige
Kennungen, eigene Namen, Deaktivierungen, Historien und Gerätezuordnungen
bleiben erhalten. Bei einer neuen Installation können aus den kürzeren Namen
auch kürzere Standard-Entitäts-IDs entstehen. Eine Registry-Migration ist
nicht erforderlich. Änderungen auf der Geräteseite werden weiterhin einzeln
geschrieben.
Die bisherigen Einzelregler für Heizkreis-Betriebsart und manuellen Sollwert
stehen unter Konfiguration. Die Klimaentität übernimmt die gemeinsame
Alltagsbedienung.

### Klimaentitäten der Heizkreise

Jeder geeignete, tatsächlich erkannte Heizkreis erhält eine Klimaentität
am bisherigen Anlagengerät. Die Kennung enthält Gateway und Kreisschlüssel.
Auch HC2 und später während des Pollings erkannte Fähigkeiten werden
berücksichtigt. Bestehende Einzelentitäten bleiben für Automationen nutzbar.

| PointT | Home Assistant | Temperatur |
| --- | --- | --- |
| `manual` | Heizen | Manuellen Sollwert lesen und ändern |
| `auto` | Automatisch | Aktueller Sollwert als Temperaturattribut, keine Temperaturänderung |
| `off` | Aus | Kein Temperaturbefehl |

Nur angebotene, verstandene Codes werden zugeordnet. Bei unbekannter
aktueller Betriebsart ist die Klimaentität nicht verfügbar. Die ursprüngliche
Auswahl bietet weiterhin zusätzliche gemeldete Codes an.
Temperaturbefehle wechseln niemals die Betriebsart. Kombinierte Befehle für
Betriebsart und Temperatur werden vor dem Schreiben abgewiesen.
Grenzen und Einheiten stammen aus dem jeweiligen Heizkreis.
Home Assistants Standardthermostat zeigt im Automatikbetrieb keinen
Sollwertregler, weil dort keine Temperaturbefehle unterstützt werden.
Der gemeldete Sollwert steht im Attribut `temperature` der Klimaentität
und im bestehenden Sensor für den aktuellen Sollwert zur Verfügung.

Meldet der Heizkreis die Regelungsart `constants` (`controlType`), behandle
ich ihn als nicht bedienbaren Kreis. Seit 0.9.0-alpha.6 zeigt die
Klimaentität den Kreis dann nur an: kein Sollwert, keine andere Betriebsart,
kein Ausschalten; ein Befehl wird vor jeder Anfrage abgewiesen. Das gilt für
jedes Gateway, das diese Regelungsart meldet. Ohne aktuelle Lesung von
`controlType` gilt der zuletzt bekannte Wert; bedienbar wird die
Klimaentität erst wieder nach einer Lesung einer anderen Regelungsart. Seit
0.9.2 folgen der manuelle Sollwert, die Betriebsart und ihre Zeilen im
Bedien-Dialog derselben Regel: Vor jeder Änderung lesen sie die
Regelungsart und die Einstellung selbst neu, bei `constants` wird nichts
geändert, und die Grenzen kommen aus dieser Lesung. Erwartet, aber an einem
echten Gerät ungetestet.

Seit 0.9.2 lese ich `controlType` vor jedem Schreiben der Klimaentität und
der Taste „Vorübergehenden Sollwert aufheben“ zusammen mit Betriebsart und
Heizkontext erneut an der Anlage; hat sich die Regelungsart seit der Anzeige
geändert, wird nichts gesendet. Mit der Regelungsart `ISRC` gibt es keinen
vorübergehenden Sollwert und kein Aufheben; der manuelle
Sollwert bleibt. Die Untergrenze des vorübergehenden Sollwerts aus dem
manuellen Sollwert bleibt sichtbar, solange dessen letzte Lesung gültig ist;
eine Änderung braucht weiter eine aktuelle Lesung. Lassen die gemeldeten
Grenzen den Wert -1 aus (zum Beispiel 5 bis 30), lege ich die Aufheben-Taste
trotzdem an, weil -1 den vorübergehenden Sollwert auch dort beendet; diese Taste
ist standardmäßig deaktiviert, und jeder negative Wert beim Rücklesen
bestätigt das Aufheben. Erwartet, aber an einem echten Gerät ungetestet.

Die Raumtemperatur stammt ausschließlich aus einem gültigen
`roomtemperature`-Messwert dieses Kreises. Vorlauftemperaturen werden nicht
ersatzweise verwendet. Ein aktives Heizen wird nicht aus Temperaturen oder
der Sommer-/Winterumschaltung abgeleitet.

Kühlen gehört nicht zum Umfang dieser Klimaentität. Bei gemeldetem
Kühlbetrieb, einem reinen Kühlkreis oder veralteten beziehungsweise unbekannten
Angaben zum Heiz-/Kühlbetrieb wird sie nicht verfügbar. Die Einzelentitäten
bleiben nach ihren eigenen Regeln nutzbar. Vor einem Klimabefehl werden
Betriebsart, Sollwert und bekannte Angaben zum Heiz-/Kühlbetrieb erneut
gelesen. Zeitprogramme, temporäre Sollwerte und Kühlsollwerte bleiben erhalten.
Auch der zentral gemeldete tatsächliche Heiz-/Kühlstatus wird berücksichtigt,
wenn am Heizkreis selbst keine entsprechende Angabe vorliegt.

Die automatisierten Tests enthalten einen vollständigen Browserdurchlauf
mit Home Assistant und einer simulierten Anlage. Dabei werden wiederholtes
Übernehmen, der Erhalt fehlerhafter Eingaben, unbestätigte Schreibversuche
und die Wiederherstellung durch reguläre Abfragen geprüft.
Ich habe diesen Stand auf Home Assistant 2026.10.3 installiert und den ersten
Heizkreis einer realen Anlage im manuellen Betrieb geprüft. Die Änderung von 19,0 auf 18,5 °C
im Bedienungsdialog wurde durch Rücklesen an der Anlage sowie durch Einzelregler
und Klimaentität bestätigt. Bei der Wiederholung habe ich über die
Plus-Schaltfläche der nativen Klimaentität wieder 19,0 °C eingestellt.
Die Hersteller-App zeigte sowohl 18,5 °C als auch die wiederhergestellten
19,0 °C. Eine zusätzliche bedingte Rückstellung direkt in Home Assistant
sicherte beide Tests unabhängig vom Browser ab; beim ersten Test stellte sie
den Wert zurück. Betriebsartenwechsel, andere Kreise und andere Anlagen sind
damit nicht praktisch bestätigt.

Dabei fiel außerdem ein verlorener Tastaturfokus nach Übernehmen auf. Ich habe
ihn korrigiert und Escape direkt nach Übernehmen und Werte neu laden im realen
Browser sowie im automatisierten Home-Assistant-Test ohne weiteren Klick geprüft.

### Klimaentitäten der Heizkreise in HomeKit

Die HomeKit-Bridge von Home Assistant zeigt eine Klimaentität mit zwei
Temperaturen: aktuelle Temperatur und Zieltemperatur. Auf einer Anlage ohne
Raumfühler können zwei Dinge falsch aussehen; beides liegt an der Behandlung
der Werte durch die Bridge, nicht an einer Lesung des Geräts.

- **21 Grad als aktuelle Temperatur.** Ein Heizkreis meldet eine aktuelle
  Temperatur nur, wenn der Regler dafür eine Raumtemperatur hat
  (`roomtemperature`). Ohne sie meldet die Klimaentität keine, wie es Home
  Assistant erwartet. Soweit ich es aus der HomeKit-Integration lese, behält
  die Bridge dann einen festen Startwert von 21 Grad für die aktuelle
  Temperatur; die große Zahl auf der Kachel ist dieser Platzhalter. Das Ziel
  auf der Kachel ist das echte. Ich fülle die aktuelle Temperatur nicht mit
  Vorlauf- oder Außentemperatur, weil beides keine Raumtemperatur ist. Die
  Kachel auszublenden ist eine Einstellung der Bridge, nicht dieser
  Integration.
- **Ziel in der Nacht.** Im Automatikbetrieb ist das Ziel der Sollwert, den
  der Regler gerade anwendet (Tag, Nacht oder Absenkung laut Programm). Meldet
  der Regler für einen in dieser Zeit abgeschalteten Kreis den Wert 0, ist das
  Ziel 0. Soweit ich es lese, ignoriert die Bridge ein Ziel von 0 und behält
  das letzte, daher kann HomeKit nachts weiter den Tageswert zeigen. Home
  Assistant selbst zeigt, was der Regler meldet.

Beobachtet in der Diagnose und den Screenshots einer Gasheizungsanlage eines
Nutzers; mit einer Bridge bei mir nicht getestet.

### Heizkreis-Einstellungen: Sommer/Winter, Kühlen und Temperaturstufen

Seit 0.9.2 erhält ein Heizkreis diese Einstellungen als Zahlen- und
Auswahlentitäten (Kategorie Konfiguration) und als Zeilen im
Bedienungsdialog, wenn der Kreis sie schreibbar und mit Grenzen oder
Optionen meldet. Ich habe sie nur mit
simulierten Daten in automatisierten Tests geprüft; sie sind **erwartet,
aber an einer echten Anlage ungetestet**. Rückmeldungen sind willkommen.

| Einstellung | Pfad | Entität | Standardmäßig aktiv |
| --- | --- | --- | --- |
| Sommer-/Winterumschaltung | `suWiSwitchMode` | Auswahl | ja |
| Heizbetrieb bis Außentemperatur | `suWiThreshold` | Zahl | ja |
| Kühlen: Raum-Solltemperatur | `cooling/roomTempSetpoint` | Zahl | nein |
| Kühlbetrieb ab Außentemperatur | `cooling/outdoorThreshold` | Zahl | nein |
| Abwesenheitstemperatur | `awayTemperature` oder `temperatureLevels/awayTemperature` | Zahl | ja |
| Kühltemperatur | `cooling/temperatureLevels/on` | Zahl | nein |
| Heiztemperatur, Absenktemperatur | `temperatureLevels/comfort2`, `eco` | Zahl | ja (seit 0.9.0) |

- **Sommer-/Winterumschaltung** eines Kreises (iCom/KM200-Gateways): Die
  Optionen sind die vom Kreis angebotenen Codes aus `automatic`
  (Automatisch), `forced` (Nur Heizbetrieb), `cooling` (Nur Kühlbetrieb)
  und `off` (Aus); andere Codes werden wie in der App ausgelassen. Die
  zentrale Saisonumschaltung einer K40 bleibt unverändert (siehe
  [Saisonumschaltung](season-switching.md)). Nach dem Wechsel auf „Nur
  Kühlbetrieb“ ist die Klimaentität des Kreises nicht verfügbar, solange er
  kühlt.
- **Heizgrenze und Kühlschwelle:** die Außentemperaturen, bei denen das
  Heizen endet und das Kühlen beginnt. Die App zeigt sie nur im Modus
  Automatisch; ich lasse sie in jedem Modus schreibbar, wirksam sind sie im
  Modus Automatisch. Die Kühlschwelle gibt es nur, solange die
  Sommer-/Winterumschaltung des Kreises Kühlen anbietet.
- **Kühl-Sollwert:** die Raum-Solltemperatur der Kühlung (nur Wärmepumpen
  mit Kühlung). 0 bedeutet in der
  App „aus“ und wird nie geschrieben; meldet der Kreis ein Minimum von 0
  oder darunter, ist der kleinste angebotene Wert der erste Rasterwert über
  0. Ohne gemeldete Grenzen gibt es keine Entität.
- **Temperaturstufen:** Bedient werden nur die vier Stufen, die die App
  kennt: Heizen (`comfort2`), Absenken (`eco`), Abwesend (`awayTemperature`)
  und Kühlen (`on`). Eine Stufe mit anderem Namen bleibt ein Messwert; die
  Diagnose zählt solche Namen, ohne sie zu zeigen. Eine Stufe ohne Wert
  erhält keine Entität. Der Wert einer Stufe gilt für alle Schaltpunkte der
  Wochenprogramme, die diese Stufe verwenden.
- **Raster:** der vom Kreis gemeldete Schritt, sonst 1,0 für die Schwellen
  und 0,5 für die Temperaturen, gezählt ab dem gemeldeten Minimum. Ein Wert
  neben dem Raster oder außerhalb der Grenzen wird abgelehnt, nicht
  gerundet.
- **Schreibregeln:** Jede Änderung liest die Regelungsart des Kreises, die
  Einstellung selbst und die Einstellungen, von denen sie abhängt, in einer
  Anfrage direkt vor dem einen Schreiben erneut an der Anlage: die
  Sommer-/Winterumschaltung für die beiden Schwellen, die anderen Stufen für
  Heiz-, Absenk- und Abwesenheitsstufe. Hat sich davon etwas seit der
  Anzeige geändert, wird nichts gesendet. Heiz- und Absenkstufe begrenzen
  sich gegenseitig (das Minimum der Heizstufe folgt der Absenkstufe und
  umgekehrt); die Grenzen beim Schreiben einer Stufe stammen deshalb aus
  dieser frischen Lesung. Seit 0.9.2 gilt das auch für die Heiz- und
  Absenkstufe, die es seit 0.9.0 gibt. Danach wird der Wert durch Rücklesen
  bestätigt und nie erneut gesendet.
- **Sperren:** Mit der Regelungsart `ISRC` (ein Raumregler führt den Kreis)
  wird wie in der App keine dieser Einstellungen geändert. Mit der
  Regelungsart `constants` wird keine Temperaturstufe geändert. Eine als
  nicht verfügbar gemeldete Einstellung erhält keine Steuerung, eine als
  nicht genutzt gemeldete lehnt Änderungen ab.
- **Raumregler:** Die Kreise eines TC100.2 oder CT200 behalten ihre eigenen
  Einstellungen (siehe [Raumregler](room-controllers.md)); für sie entsteht
  keine dieser Entitäten.
- **Erst nach der Raumregler-Prüfung:** Diese Einstellungen entstehen, werden
  angeboten und geschrieben erst, nachdem eine abgeschlossene Erkennung eine
  eindeutige Antwort darauf erhielt, ob das Gateway ein Raumregler ist
  (Zonenliste und Produkttyp). Bis dahin, etwa solange ein Serverfehler das
  offenlässt, warten sie; ein spät erkannter Raumregler zeigt sie so nie, auch
  nicht kurz.

### Variabler Stromtarif (nur lesen)

Seit 0.9.2 fragt die Integration ein iCom-Gateway bei der Erkennung einmal,
ob seine Tarifoptimierung eingeschaltet ist (`/application/tbso/enable`),
und nach der Nummer seines Tarifs (`/application/tbso/electricity/tariffID`).
Antwortet es, erscheinen die Diagnose-Entitäten **Variabler Tarif aktiv**
und **Tarif-ID**, beide standardmäßig deaktiviert. Den Namen des Tarifs
liefert ein eigener Dienst des Herstellers, den diese Integration nicht
nutzt; die Nummer 5 bedeutet „kein Tarif gewählt“ und ergibt das Attribut
`tariff_selected: false`. Eine Ablehnung, ein fehlender Pfad oder ein
Serverfehler gilt als „nicht vorhanden“ und beeinflusst die übrige Erkennung
nicht. Geschrieben wird nichts: In der App braucht die Wahl eines Tarifs
mehrere Anfragen und ändert dabei auch die Heizkreise. Die Tarifwerte einer
K30/K40 (`/system/variableTariff/*`) bleiben wie seit 0.9.0 deaktivierte
Diagnose-Sensoren. Erwartet, aber an einer echten Anlage ungetestet.

### Zeitprogramme Heizung und Warmwasser

Seit 0.9.0-alpha.6 ändert **Konfigurieren > Zeitprogramme Heizung und
Warmwasser** die Wochenprogramme A und B eines Heizkreises und das Programm A
des Warmwassers. Angeboten wird ein Programm nur, wenn das Gateway es als
schreibbar und in Gebrauch meldet, es die strenge Programmprüfung besteht,
es ein Stufenprogramm (`level`) ist (aus einer aktuellen Lesung, wo der
Kreis seine Programmart meldet) und jede verwendete Stufe zu den gemeldeten
Temperaturstufen des Kreises gehört. Die Programmart und die Programmnamen
werden nie geschrieben.

1. Programm wählen. Die Woche zeigt die Schaltpunkte jedes Tages und die
   gemeldeten Grenzen, zum Beispiel höchstens 2 Punkte pro Tag, 14 pro Woche
   und Uhrzeiten im 15-Minuten-Raster. Ein Heizprogramm, das nicht aktiv ist,
   und das Warmwasserprogramm, das nur in der Warmwasser-Betriebsart Auto
   wirkt, weisen darauf hin.
2. Tag bearbeiten: Uhrzeit oder Stufe eines Schaltpunkts ändern, ihn löschen
   oder einen hinzufügen, jeweils in den gemeldeten Grenzen. Oder einen Tag
   auf andere Tage kopieren.
3. Die Änderungen sammeln sich in einem Entwurf der ganzen Woche. Der letzte
   Schritt zeigt die geänderten Tage vorher und nachher. Erst dann wird die
   Woche gesendet.

Direkt vor dem Senden wird das Programm erneut gelesen, bei einem Heizkreis
zusammen mit seiner Programmart. Weicht das Programm vom Stand im Dialog ab,
ist seine Art kein Stufenprogramm mehr oder lässt sie sich nicht lesen, wird
nichts gesendet, und der Dialog nennt den Grund. Die ganze Woche geht einmal als rohes
JSON-Array hinaus und wird zurückgelesen; das bestätigte Programm erscheint
sofort. Lehnt das Gateway ab, ändert sich nichts. Nach einer verlorenen
Antwort, einem Serverfehler oder einer angenommenen Anfrage mit unlesbarer
Antwort klärt nur das Lesen den Ausgang, nie ein zweites Senden; bis dahin
gilt das Programm als nicht aktuell. Die Erfolgsmeldung wiederholt die
bisherigen Schaltpunkte, damit man sie von Hand zurückstellen kann. Die
Diagnose zählt die Schreibvorgänge und die Schaltpunkte pro Tag, ohne
Uhrzeiten und Stufen.

Die Auswahl **Aktives Zeitprogramm** wechselt zwischen den Programmen A und
B. Sie entsteht nur, wenn das Gateway beide erlaubt, das aktuelle darunter
ist und beide Programme lesbar und gültig sind. Die Optionen sind `a` und
`b`; die gemeldeten Namen stehen in den Attributen `program_names` und
`program_name`. Mit nur einem Programm, wie an manchen Wärmepumpen, gibt es
keine Auswahl.

Das Ändern des Warmwasserprogramms A ist an einer Logatherm WLW186i-12 E
mit MX400 bestätigt: Ein in Home Assistant geänderter Schaltpunkt erschien in
der App des Herstellers, und das Zurückstellen dort erschien kurz darauf in
Home Assistant. Die Heizprogramme, die Auswahl des aktiven Zeitprogramms,
Programm B, iCom-Gateways und Programme mit absoluten Temperaturen sind nur
mit simulierten Daten getestet und bleiben erwartet, ungetestet.

#### Weitere Programmarten

Seit 0.9.2-beta.2 bietet derselbe Dialog auch diese Programme an, wo das
Gateway sie meldet: Heizprogramme A/B mit Temperaturen (Programmart
`absolute`, Temperaturen als Text wie `"21.5"`, `"0.0"` ist aus), das
Kühlprogramm `/heatingCircuits/{hc}/cooling/switchPrograms/A` (`on`/`off`,
nur mit lesbarer Kühlstufe `on`), die Zonenprogramme
`/zones/{zone}/switchPrograms/heating` und `.../cooling` (Temperaturen),
das Lüftungsprogramm `/ventilation/zone1/switchPrograms/cp` (nur die als
Referenzen von `ventilationLevels` gemeldeten Stufen) und das
WDDW2-Warmwasserprogramm `/dhwCircuits/dhw1/switchPrograms/program1`
(ganze Grad als Text oder `off`, Bereich aus `manualSetpoint`). Dazu kommen
die Programme von Klimageräten an einem PointT-Gateway
(`/airConditioning/switchPrograms/program1` bis `program9`, Betriebsart,
Temperatur und Lüfterstufe wie `cool 24.0 auto` oder `off`; siehe
[Klimageräte am PointT-Gateway](pointt-air-conditioners.md)).

Änderbar ist ein solches Programm nur mit den gemeldeten Grenzen
(`maxNbOfSwitchPoints`, `maxNbOfSwitchPointsPerDay`,
`switchPointTimeRaster`) und, bei Temperaturen, mit gemeldetem Bereich und
Schritt (`switchPointMin`, `switchPointMax`, `switchPointResolution`).
Ersatzwerte nehme ich nie an. Ohne diese Angaben erscheint das
Programm nur als Sensor, und die Diagnose nennt den Grund. Temperaturen
außerhalb des Bereichs oder neben dem Schritt werden abgelehnt, nie
gerundet; „aus“ (`"0.0"`) nur, wo das Programm es schon enthält. Das
WDDW2-Programm und die Klimageräte-Programme gehen als Objekt
`{"switchPoints": [...]}` hinaus, alle anderen als rohes Array. Wie in der
Hersteller-App lasse ich beim WDDW2-Programm ein `off` um 00:00 weg, und ein
Klimageräte-Tag nach einem Tag, der mit eingeschaltetem Gerät endet, bekommt
ein ausdrückliches `off` um 00:00 (es zählt zu den Grenzen). Das
WDDW2-Programm ist nur änderbar, solange das Gerät eingeschaltet ist, im
Zeitprogrammbetrieb läuft und eine Zeitzone hat.

Keines dieser Programme wurde an einer echten Anlage gelesen oder geändert;
ihre Formen, Lesen und Schreiben sind erwartet, ungetestet. Eine Diagnosedatei zeigt die Form (Schlüsselnamen und
Werttypen, nie Werte).

#### Wochenansicht

Der Wochenschritt des Dialogs (und die RRC-Programmansicht) zeigt die Woche
als nur lesende Tabelle: sieben Tage von Mitternacht bis Mitternacht, ein
Abschnitt je Schaltpunkt mit Stufe oder Temperatur als Text und Farbe, der
Übertrag vom Vortag markiert. Ein RRC-Tag ohne Schaltpunkte wird so
bezeichnet, nicht als aus. Geändert wird weiter über die Dialogschritte.
Lädt die Ansicht nicht, bleibt der bisherige Text. Die Zeiten sind die der
Uhr der Heizungsanlage.

#### Programmnamen

Die Namen der Programme A und B werden jetzt nach ihrem `valueConfig`
dekodiert (ein Base64-Name erscheint als Text). Meldet das Gateway einen
Namen als schreibbar mit vollständiger Kodierung, ändert ihn die
Text-Entität **Name des Zeitprogramms** (standardmäßig deaktiviert). Der Name
wird vor dem einzigen Schreiben frisch gelesen und verglichen; zurückgelesen
wird der dekodierte Name. Zu lange Namen und Zeichen, die die Kodierung nicht
fasst, werden abgelehnt; ein leerer Name wird nicht gesendet. iCom-Gateways
erhalten keine Namenssteuerung. Erwartet, ungetestet an einer echten Anlage.

#### Aktion `bosch_buderus_heating.set_switch_points`

Die erste Aktion der Integration setzt die Schaltpunkte eines oder mehrerer
Programm-Sensoren aus Automationen und Skripten (Beispiel im englischen
Teil). `level` ist eine Stufe (Attribut `allowed_levels`) oder bei
Programmen mit Temperaturen eine Zahl wie `21.5`. Anführungszeichen sind in
YAML am sichersten; ein `on`/`off` oder eine Zeit wie `22:00` ohne
Anführungszeichen, die YAML zu einem Wahrheitswert bzw. einer Zahl macht,
werden ebenfalls verstanden. Grundlage ist immer eine
frische Lesung; direkt vor dem einzigen PUT wird das Programm ein zweites
Mal gelesen, eine Änderung dazwischen sendet nichts. Es gelten dieselben
Regeln wie im Dialog. Die Antwort enthält je Ziel `result` (`sent`,
`unchanged`, `validated`), `confirmed` sowie die bisherigen und neuen
Schaltpunkte, nie Pfade oder Kennungen. Mehrere Ziele werden nacheinander
bearbeitet; ein Fehler macht einen Erfolg nicht rückgängig. Nichts wird
automatisch wiederholt.

Programm-Sensoren schreibbarer Programme sind jetzt beim ersten Anlegen
standardmäßig aktiviert, damit die Aktion ihre Ziele findet; bestehende
Entitäten behalten ihren Zustand. Die Aktion ist nur mit simulierten Daten
getestet; das Schreiben dahinter ist das des Dialogs.

### Warum liegt der Vorlauf über dem Sollwert?

Während eine Wärmepumpe heizt, liegt ihr Vorlauf oft einige Kelvin über dem
aktuellen Vorlauf-Sollwert. Der Sensor **Vorlauf über Sollwert
(berechnet)** zeigt diese Differenz. Bei mildem Wetter ist das meist normal.
An einer echten Anlage, einer Luft-Wasser-Wärmepumpe mit einem
Fußbodenheizkreis und Puffer, zeigten die Messwerte über mehrere milde Tage
(etwa 4 bis 11 °C außen):

- **Mindestleistung:** Der Verdichter lief fast die ganze Heizzeit mit
  seiner kleinsten Drehzahl. Schon diese kleinste Leistung war mehr Wärme,
  als die Heizkurve verlangte; das Wasser erwärmte sich, bis der Fußboden
  die Wärme abnahm. Der Vorlauf pendelte sich etwa 1 bis 3 K über dem
  Sollwert ein (typisch etwa 2,4 K). Stieg oder sank der Sollwert um 1 K,
  änderten sich weder die Drehzahl noch der Vorlauf.
- **Abschalten:** Die Wärmepumpe schaltete ab, wenn der Vorlauf etwa 4 K
  über dem Sollwert lag.
- **Einschalten:** Sie startete erst wieder, nachdem sich die
  Unterschreitung des Sollwerts über die Zeit aufsummiert hatte
  (Gradminuten, dort etwa 40 K·min). In diesem Moment lag der Vorlauf nur
  1 bis 2 K unter dem Sollwert.
- **Heizkreispumpe und Raumthermostate:** Schlossen viele Raumthermostate
  ihre Ventile, förderte die Heizkreispumpe weniger Wasser, der Vorlauf
  stieg schneller und die Wärmepumpe schaltete nach kurzer Laufzeit ab. Die
  meisten kurzen Laufzeiten kamen daher, nicht vom Sollwert.

Eine tiefere Heizkurve senkt den Vorlauf bei mildem Wetter also nicht; sie
rückt vor allem den Abschaltpunkt näher, und die Wärmepumpe schaltet öfter
ab. Wird es kälter und braucht das Haus mehr als die Mindestleistung, dreht
der Verdichter höher und die Differenz wird kleiner. Ein- und
Abschaltschwellen und die Mindestdrehzahl sind Reglereinstellungen, die die
Schnittstelle nicht anbietet (nächster Abschnitt). Die Werte stammen von
einer Anlage; andere Modelle und Einstellungen können sich anders verhalten.

### Werte nur im Servicemenü

Manche Einstellungen werden im Servicemenü des Reglers vorgenommen, meist
vom Fachbetrieb. Bei Wärmepumpen mit K30/K40-Gateway bietet die
Schnittstelle der Integration (Cloud und lokal) sie nicht an; die
Integration kann sie daher weder anzeigen noch ändern:

- die **minimale Vorlauftemperatur** eines Heizkreises (die maximale
  Vorlauftemperatur ist verfügbar, wo die Anlage sie meldet);
- der **Solldruck der Heizkreispumpe** (PC1): Weder die Reglersignale noch
  die lokalen oder Cloud-Ressourcen einer echten Anlage melden einen;
- die **Grenzen des Zuheizers**, etwa seine maximale Leistung, seine
  Verzögerung oder die Außentemperatur, unter der er helfen darf;
  Betriebsart, Status und Leistung sind verfügbar, wo gemeldet;
- das **Ladedelta** des Warmwassers, also wie weit der Speicher abkühlt,
  bevor die Ladung startet; die Warmwassertemperaturen, die die Anlage
  meldet, bleiben verfügbar;
- die **Gebäudeart**, mit der der Regler die Außentemperatur dämpft;
- die **Ein- und Abschaltschwellen** der Wärmepumpe (Gradminuten,
  Überhöhung) und ihre Mindestdrehzahl.

Raumregler der RRC-2.0-Plattform melden ihre Heizkurve und beide
Vorlaufgrenzen selbst (siehe [Raumregler](room-controllers.md));
Hybridanlagen mit Hybridmanager melden ihren Bivalenzpunkt.
