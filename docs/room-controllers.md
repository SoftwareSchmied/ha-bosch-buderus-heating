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

- the zone readings (room temperature, active setpoint, zone names);
- switching a zone between manual mode and the schedule (`userMode`
  `manual`/`clock`);
- changing the manual setpoint of a zone.

Everything else on this page is **expected, untested**: it is built and
tested with simulated data in the shapes of the observed TC100.2 answers and
of constructed controller data, but nobody has tried it on a real controller
yet. The CT200 as a whole is expected, untested. The diagnostics mark every
room controller function as `confirmation: unconfirmed` until a test on a
real device confirms it. I would be glad about reports from owners, ideally
with a diagnostics download (see [diagnostics](diagnostics.md)).

| Function | Status |
| --- | --- |
| Zone readings, manual/schedule mode, manual setpoint | Confirmed on a TC100.2 |
| Temporary setpoint, its clear button, zone details | Expected, untested |
| Program selection and editing switch points | Expected, untested |
| Away mode, absence calendar, boost, fireplace function | Expected, untested |
| Hot water | Expected, untested |
| Heating circuit settings | Expected, untested |
| System readings, fault and maintenance sensors | Expected, untested |
| Energy history in the statistics | Expected, untested |
| Radio devices and their settings | Expected, untested |
| CT200 (every function) | Expected, untested; no local access |

## How changes are written

Every change goes through the same path: the integration reads the current
value and its limits again, refuses a value outside them before sending,
sends the change exactly once and reads it back after 1, 2, 4 and 8 seconds.
A room controller applies a change with a delay. If it has not confirmed the
change by then, a message says that the change was sent but is not
confirmed yet; a later regular reading that shows it counts as confirmed.
A change is never resent on its own.

A setting the controller reports as used but currently unavailable stays
visible and is locked: a change is refused with a message that names what
is locked (the heating mode of a zone, another zone setting, a heating
circuit, hot water, a radio device or another setting). A setting that is
unavailable and not marked as used gets no control at all.

## Zones

Every zone that reports a heating mode and a room temperature gets a climate
entity.

- `heat` is the manual mode, `auto` the schedule. Room controllers have no
  off mode. *Confirmed on a TC100.2.*
- In manual mode a new target changes the manual setpoint. *Confirmed on a
  TC100.2.*
- **Temporary setpoint.** In the schedule mode a new target sets a temporary
  setpoint. It lasts only until the next switch point of the schedule; the
  attribute `override_until_next_switch_point` says so. It must be at least
  the lowest manual setpoint of the zone. If the controller ends it at once,
  for example because a switch point was reached, a message of its own says
  so. *Expected, untested.*
- **Clear temporary setpoint.** A button per zone ends an active temporary
  setpoint while the zone follows its schedule. *Expected, untested.*
- A mode together with a temperature changes the mode first and the target
  only after the controller has confirmed the mode.
- **Zone details** *(expected, untested):* sensors for the next setpoint,
  the minutes until it and the zone status (idle, heat request, zone error,
  communication error or warning, low battery); the valve opening and the
  optimum start with its heat-up rate (disabled by default); a binary sensor
  "Window open".
- **Open window detection** *(expected, untested):* a switch per zone and one
  for the system, and the system's open window duration and temperature
  where the controller allows writing them (disabled by default).

A room controller with zone climates gets no climate entity for its heating
circuit as well.

## Programs (heating schedules)

*Expected, untested.*

- **Program selection.** A select per zone chooses the zone's program. Its
  options are the program numbers, so automations keep working when a program
  is renamed; the names are in the attributes `program_names` and
  `program_name`. Changing the program does not switch the zone to the
  schedule. On a large system the program list is read together with the
  zones and searched again if it is missing.
- **Editing switch points.** Under **Configure → RRC heating schedules** you
  can change the time and temperature of an existing switch point of a week
  or day program. A change affects every zone that uses the program. New
  programs and added or deleted switch points are not supported.

## Away mode, absences and special days

*Expected, untested.*

- **Away mode** is a switch, the **away temperature** a number.
- **Absences and special days** appear as a calendar of whole days, in the
  controller's time zone. You can create, change and delete absences there.
  A day that runs like another weekday ("Like Monday") can only be deleted.
  A name has at most 32 bytes (fewer characters with umlauts or emoji),
  events may not overlap, not even on their first or last day, and the end
  may not be in the past. Recurring events, descriptions and locations are
  not supported.
- A change of the events is sent once. If the controller does not confirm it
  in time, the next change is refused for up to ten minutes until the first
  one shows, so an event is never created twice.

## Boost and fireplace function

*Expected, untested.*

- **Boost:** a switch, the boost temperature and the boost duration. They
  write the controller's boost settings as a whole. The rooms of a boost are
  chosen on the controller; without a room the boost is refused. A boost in a
  form the integration does not know completely is shown but not changed.
- **Fireplace function:** a switch, disabled by default.

## Hot water

*Expected, untested.*

- The operating mode (off, comfort, own program, and eco levels only where
  the controller offers them) as a select and as a water heater entity.
- The comfort temperature as a number.
- **Extra hot water:** a switch and its duration in minutes.
- **Thermal disinfection** (disabled by default): a switch, the weekday or
  every day, the start time and the result of the last run.
- **Current hot water schedule level** (disabled by default): the level of
  the hot water program right now, calculated from its switch points and the
  controller's clock.

## Heating circuit

*Expected, untested.*

- Summer/winter switching and night setback by outdoor temperature (off or
  automatic), with their outdoor temperature thresholds.
- The season mode, only when the controller offers at least two modes, the
  heat-up speed (slow, normal, fast) and the setpoint optimization.
- A sensor for the operating season (heating, cooling, idle).
- **For specialists, disabled by default:** the heating curve (base and end
  point), the minimum and maximum supply temperature and the room influence.
  Enable them only if you know these settings. The base point stays below
  the end point and the minimum supply temperature not above the maximum;
  before a change both limits are read again, and nothing is sent if one of
  them changed.
- Installer settings (control strategy, heating system type, room control
  type) are never written.

## System, faults and maintenance

*Expected, untested.*

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
- **Maintenance messages:** a notification with the numeric class 18 or 20
  counts as maintenance on every gateway, not as a fault. Under **Configure →
  Notifications** an option, off by default, also shows warnings and
  maintenance in the bell notification. See [faults](faults.md).

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
- The first import runs 15 minutes after the start, then every three hours,
  with at most eight requests per run.
- To remove the statistics, delete them under **Developer tools →
  Statistics**.

## Radio devices

Every radio device in the controller's device list becomes its own device
below the controller, named after its type (for example "Radiator
thermostat 2"), with its zone as the suggested area.

- **Read** *(expected, untested):* "Battery low" (enabled), "Radio signal"
  (disabled by default) and the firmware version; for radiator thermostats
  also the valve opening, the maximum valve opening, the temperature at the
  valve, the valve state and the whisper mode (quiet valve motor operation),
  all disabled by default, and a binary sensor "Valve problem" (enabled).
- **Settings** *(expected, untested, disabled by default):* the child lock,
  the temperature offset (only while the device uses it) and the
  sensitivity of the open window detection (high, medium, low).
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
  rule.
- **Never read:** the radio key and radio identifier of each device, user,
  installer, password, location and phone data.
- **Energy history:** at most eight requests every three hours.

On the large synthetic test controller with all functions active, the setup
needs 21 batch and 7 single requests, and one hour of regular polling about
150 requests. See [request behavior](request-behavior.md) for the general
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

**Bestätigt** ist an einem TC100.2 bisher: das Lesen der Zonen
(Raumtemperatur, aktiver Sollwert, Zonennamen), das Umschalten zwischen
manuellem Betrieb und Zeitprogramm (`userMode` `manual`/`clock`) und das
Ändern des manuellen Sollwerts. **Alles andere ist erwartet, ungetestet**,
ebenso der CT200 insgesamt:

- **Zonen:** vorübergehender Sollwert bis zum nächsten Schaltpunkt mit
  Aufheben-Taste, nächster Sollwert, Minuten bis dahin, Zonenstatus,
  Ventilöffnung, optimierter Start, Fenster offen, Fenster-offen-Erkennung.
- **Zeitprogramme:** Programmauswahl je Zone und Ändern vorhandener
  Schaltpunkte unter **Konfigurieren → RRC-Zeitprogramme**.
- **Abwesenheit:** Schalter und Abwesenheitstemperatur; Kalender
  „Abwesenheiten und Sondertage“ mit ganzen Tagen, Abwesenheiten anlegen,
  ändern und löschen, Wochentagstermine nur löschen; Boost (Schalter,
  Temperatur, Dauer; Räume am Regler) und Kaminfunktion.
- **Warmwasser:** Betriebsart, Komforttemperatur, Extra-Warmwasser mit
  Dauer, thermische Desinfektion, aktuelle Programmstufe.
- **Heizkreis:** Sommer-/Winterumschaltung, Nachtabsenkung, Saisonbetrieb,
  Aufheizgeschwindigkeit, Sollwertoptimierung, Betriebssaison; Heizkurve,
  Vorlaufgrenzen und Raumeinfluss nur für Fachkundige und standardmäßig
  deaktiviert.
- **System:** Anzeige- und Ursachencode, Anlagendruck, Raumluftfeuchte,
  verriegelnde Störung (bis zum manuellen Reset), blockierende Störung,
  Wartung fällig, Nachfüllen erforderlich; eigene Texte für
  Funkgerätestörungen; Klasse 18/20 als Wartung; Warnungen und Wartung
  optional in der Glocken-Benachrichtigung.
- **Energiehistorie:** Tageswerte als Statistiken mit dem Zusatz
  „(kWh erwartet)“, weil die Einheit nicht bestätigt ist; Gas nur mit
  bestätigter Einheit; Auswahl im Energie-Dashboard selbst, Löschen unter
  **Entwicklerwerkzeuge → Statistiken**; alle drei Stunden höchstens acht
  Anfragen.
- **Funkgeräte:** Batterie, Funksignal, Firmware, Ventilwerte und
  Flüstermodus (nur lesen), Ventilproblem; Kindersicherung,
  Temperatur-Offset und Empfindlichkeit der Fenster-offen-Erkennung
  (standardmäßig deaktiviert). Ventilkalibrierung und Flüstermodus werden nie
  geschrieben.

Jede Änderung wird genau einmal gesendet und nach 1, 2, 4 und 8 Sekunden
zurückgelesen. Die Discovery liest höchstens 512 Pfade, die Einstellungen
der Funkgeräte haben einen eigenen Deckel von 192 Pfaden. Eine
HTTP-500-Antwort auf eine Katalog- oder Zusatzabfrage gilt als „fehlt“ und
beendet die Discovery nicht.
