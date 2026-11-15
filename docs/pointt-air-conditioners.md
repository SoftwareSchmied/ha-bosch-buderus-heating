# Air conditioners on a PointT gateway

Expected, untested on a real installation. I built this support in
0.9.2-beta.2 from the behaviour of the vendor app and checked it only with
simulated data in automated tests. I am happy about feedback from anyone who
has such an air conditioner in the Bosch or Buderus app.

Some air conditioners appear in the account's PointT gateway list as their
own gateway with the device type `rac`. This is a **different backend** from
the [air conditioners of the second cloud](bacon-air-conditioners.md) (Midea
Climate Series): the unit is read and written through normal PointT resources
below `/airConditioning`. The same physical unit is listed either here or
there, never in both; each keeps its own stable IDs per gateway.

## Detection

Once per start, the integration reads `/system/bus` and
`/system/appliance/model`:

| `/system/bus` | `/system/appliance/model` | Result |
| --- | --- | --- |
| `Midea` | `rac-midea-gatewayconnector` | Midea unit with gateway connector |
| `Midea` | anything else, also empty | Midea unit |
| `Echonet` | any | Sharp unit |
| `No_Bus`, empty or unknown | any | no air conditioner, nothing is built |

The gateway's reported device type decides alone; a product name is never
matched. A gateway that answers nothing is detected again in the next cycle.
An air conditioner has no heating discovery, no local access and no
inventory view while the cloud is unavailable.

## Readings

One bulk request every 60 seconds (120 seconds with the cloud-friendly
profile) reads a fixed list: power, mode, target and room temperature, fan,
louvres, spot air direction, the five extra functions, both timers, the
schedule settings and the program configuration, the two function lists and
the solar operation state, and from the second cycle on every program the
unit offers as active program (at most nine, in the same request). A
path the unit does not answer leaves its function out. When the unit answers
no path at all (offline), every entity becomes unavailable.

## Entities

| Entity | Path | Default |
| --- | --- | --- |
| Climate (named after the device) | `acControl`, `operationMode`, `temperatureSetpoint`, `roomTemperature`, `fanSpeed`, `airFlowVertical`, `airFlowHorizontal`, `ecoMode` | enabled |
| Switches Full power, Sleep mode, Frost-protection heating (8 °C for Midea, 10 °C for Sharp), Air purification | `fullPowerMode`, `sleepMode`, `setbackMode`, `airPurificationMode` | disabled |
| Select Spot air direction | `quickAirFlows` | disabled |
| Numbers Switch-on timer and Switch-off timer (minutes, 0 stops the timer) | `timers/on`, `timers/off` | disabled |
| Switch Schedule enabled, select Active schedule, button End temporary override | `switchPrograms/enabled`, `switchPrograms/activeProgram`, `switchPrograms/temporarySetting/enabled` | disabled |
| Sensors Schedule 1 to 9 (the mode a program sets now, switch points as attributes) | `switchPrograms/program1` ... `program9` | disabled |
| Binary sensor Solar operation (diagnostic, read only) | `/pv/enable` | disabled |

- **Climate:** modes `heat_cool` (auto), `cool`, `heat`, `dry` and `fan_only`
  come only from the unit's `allowedValues`; `internalCleaning` is never
  offered. `off` switches the unit off through `acControl`; the stored mode
  stays. Fan modes `auto`, `quiet`, `low`, `medium` (`mid`), `high` and
  `turbo`, vertical and horizontal louvres with the unit's own positions (on
  an on/off unit the standard `on`/`off`), and the preset `eco`. The target
  temperature uses the reported limits and step (1 °C without a step) and is
  offered only in Celsius, while the value lies within the limits. There is
  no `hvac_action`: a mode does not show whether the compressor runs.
- **Extra controls** exist only when the unit reports their path as writable
  and, for the functions, lists them in `standardFunctions` or
  `advancedFunctions`. Ionizer and similar functions the app cannot switch
  over PointT are never created.
- **Timers:** only in steps of 30 minutes from the reported minimum, or 0 to
  stop. A timer can switch the unit on or off later without anyone present.
- **Frost-protection heating** keeps heating while the unit is otherwise off.
- **Schedules:** switching the schedule on or off, choosing the active
  program and ending a temporary change. The switch points of the active
  program (day, time, mode, setpoint, fan) are shown in the attribute
  `switch_points` of the Active schedule select (kept out of the recorder);
  a malformed program shows none. The app keeps program names on the phone
  only, so none are shown.
- **Schedule sensors:** one per program the unit offers, showing the mode
  the program sets now (`off` before the first switch point of a day, as in
  the app), with the switch points, the scheduled temperature and fan
  speed, whether it is the active program and whether it can be changed.
  A program with a point in an unknown form gets no sensor.

## Changing switch points

The switch points of a program can be changed in **Configure > Schedules**
(the same dialog as for the heating programs) and with the action
`set_switch_points` on a schedule sensor. A switch point holds a mode and,
depending on the mode, a temperature and a fan speed; in the action its
level is written as `<mode> [<temperature>] [<fan speed>]`, for example
`cool 24.0 auto`, `fanOnly low` or `off`. Each setting applies until the
next switch point of the same day; before the first switch point of a day
the unit is off.

- A program is offered for changes only when the unit reports it as
  writable and the program configuration (`switchPrograms/configuration`)
  is readable and writable. The modes, their temperature range and the fan
  speeds come from that configuration; the number of switch points in all
  and per day and the time raster from the program, else from the
  configuration. Without them the program is shown, never changed.
- A temperature is needed exactly where a mode reports a range, within it
  and on its step (the configured step, else the step of the target
  temperature, else whole degrees), and only in Celsius between 10 and
  40 °C. A fan speed only from the mode's list. `off` carries neither.
  `internalCleaning` is never offered. A week without any switch point is
  not sent; switch the schedule off instead.
- The change reads the program and its configuration fresh, compares the
  program once more right before sending, sends the whole week once as
  `{"switchPoints": [...]}` and reads it back after 1, 2, 4 and 8 seconds.
  Nothing is sent again; a lost answer is resolved by reading. A program
  changed in the meantime (for example in the app) is refused.
- Expected, untested on a real installation: the form of a program follows
  the vendor app, which reads it without checking its type; a program in
  another form is shown with a note in the diagnostics or not at all.

## Changes

Each action writes exactly one path: a fresh reading of that path (and of
power and solar state for a mode change), exactly one PUT, a read-back after
1, 2, 4 and 8 seconds, never a second PUT. Values outside the reported limits,
off the step or not in `allowedValues` are refused before anything is sent.
A setting the unit reports with `used=false` is refused; `available=false`
removes the control.

- **Mode change while switched off:** refused with "switch it on first". The
  mode is never sent together with a power change.
- **Mode with a temperature in one call:** refused; change them separately.
- **Solar operation:** while `/pv/enable` reads `on`, the modes auto and heat
  are refused, as in the app. Without a readable solar state nothing is
  blocked. The solar settings themselves are never written.
- A change that was sent but not confirmed in time shows its own message; it
  may still take effect and is not sent again.

## Diagnostics

`pointt_air_conditioners` lists for each unit the detected family, the
detection state, freshness, readable and missing paths, writable paths,
offered controls, the number of listed functions, whether the active program
was read and the count of sent and refused changes. `programs_read` counts
the programs with a current reading, `program_edit_issues` lists why they
cannot be changed (for example `program_configuration_missing`,
`configuration_not_writable`, `program_limits_missing`,
`program_modes_unknown`, `point_out_of_range`, `not_writable`) or
`editable`, and `program_writes` counts the program changes (`sent`,
`confirmed`, `refused`, `failed`). No values, program IDs or names, serial
numbers or identifiers are included.

## Deutsch

Erwartet, ungetestet an einer echten Anlage. Ich habe die Unterstützung in
0.9.2-beta.2 nach dem Verhalten der Hersteller-App gebaut und nur mit
simulierten Daten in automatisierten Tests geprüft. Über Rückmeldungen von
Besitzern solcher Klimageräte freue ich mich.

Klimageräte, die in der PointT-Gateway-Liste mit dem Gerätetyp `rac`
erscheinen, sind ein **eigenes Backend**, getrennt von den
[Klimageräten der Zweit-Cloud](bacon-air-conditioners.md). Die Erkennung
liest einmal je Start `/system/bus` und `/system/appliance/model` (Midea,
Midea mit Gateway-Connector, Sharp über Echonet; alles andere ist kein
Klimagerät). Ein Bündelabruf je Minute liest eine feste Pfadliste.

Die Climate-Entität (aktiv, Name des Geräts) schaltet ein und aus,
wählt Betriebsart, Solltemperatur, Lüfterstufe, Lamellen und das Preset
`eco`, alles nur aus den gemeldeten Werten. Zusätzlich gibt es, standardmäßig
deaktiviert, Schalter für Volllast, Schlafmodus, Frostschutz-Heizen (8 bzw.
10 °C) und Luftreinigung, die Auswahl Spot-Luftführung, Einschalt- und
Ausschalt-Timer (30-Minuten-Schritte, 0 stoppt), Zeitprogramm aktiv, aktives
Zeitprogramm, die Taste "Temporäre Abweichung beenden" und den lesenden
Sensor Solarbetrieb. Die Schaltpunkte des aktiven Programms stehen nur lesend
im Attribut `switch_points` der Auswahl "Aktives Zeitprogramm". Je Programm
gibt es einen standardmäßig deaktivierten Sensor "Zeitprogramm 1" bis "9" mit
der Betriebsart, die das Programm gerade setzt.

Schaltpunkte lassen sich unter **Konfigurieren > Zeitprogramme** und mit der
Aktion `set_switch_points` ändern. Ein Schaltpunkt hat eine Betriebsart und,
je nach Betriebsart, Temperatur und Lüfterstufe; in der Aktion als
`cool 24.0 auto`, `fanOnly low` oder `off`. Er gilt bis zum nächsten
Schaltpunkt desselben Tages, vor dem ersten Schaltpunkt eines Tages ist das
Gerät aus. Bearbeitbar ist ein Programm nur, wenn Programm und
Programmkonfiguration als schreibbar gemeldet sind und die Konfiguration die
Betriebsarten mit Bereichen und Lüfterstufen sowie die Grenzen nennt. Die
Änderung liest Programm und Konfiguration frisch, vergleicht das Programm
direkt vor dem Senden noch einmal, sendet die ganze Woche genau einmal und
liest sie zurück; nichts wird wiederholt.

Jede Aktion schreibt genau einen Pfad: frisch lesen, genau ein PUT,
Rücklesen nach 1, 2, 4 und 8 Sekunden, kein zweiter Versand. Ein
Moduswechsel bei ausgeschaltetem Gerät wird abgelehnt ("zuerst einschalten").
Im Solarbetrieb sind Automatik und Heizen gesperrt wie in der App.
