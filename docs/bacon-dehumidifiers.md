# Additional cloud dehumidifiers

I added support in v0.8.2-beta.6 for connected Dry 4000i, 6000i and 8000i
devices through the existing [account connection](bacon-connection.md).
An identified, selected device receives a native Home Assistant humidifier
with the dehumidifier device class. Its actual capabilities determine the
available modes, target range and additional readings or controls.

## Identification and controls

| Family | Case-sensitive `appliance.type` prefixes |
| --- | --- |
| Dry 4000i | `Dry 4000i`, `dry_4000i_v2` |
| Dry 6000i | `Dry 6000i`, `dry_6000i_v2` |
| Dry 8000i | `Dry 8000i`, `dry_8000i_v2` |

A type prefix establishes a family contract, not the presence of every
optional function. Device names and similar model numbers do not establish
compatibility. Existing PointT humidity sensors do not become dehumidifiers.

| HA control | State field | Rules |
| --- | --- | --- |
| Dehumidifier on/off | `powerEnabled` | Boolean; switching power preserves the program and settings |
| Operating mode | `opMode` | Offered subset of `auto`, `night`, `laundry` |
| Target humidity | `humiditySetpoint` | Integer percentage within current minimum, maximum and step |
| Child lock | `childLockEnabled` | Separate switch if explicitly writable and currently reported |
| Display | `displayEnabled` | Separate switch if explicitly writable and currently reported |

Each write requires current metadata and explicit `ro=false`. Missing rights
mean read-only. The target contract establishes percentage even if its unit
is omitted; a conflicting unit is rejected. Integer bounds must fit within
0..100%. An omitted step means 1. The maximum is limited to the last valid
grid point. Out-of-grid requests are rejected without rounding. Reported
targets outside that range remain unknown.

Target changes are admitted in Auto and Night. Laundry is a separate program;
its stored target remains readable but is not editable through this adapter.
Selecting Laundry preserves the device's existing laundry timer. It does not
promise continuous operation or reset the deadline. The HA humidifier platform
does not provide a feature flag to disable just the target control; requests
are still checked and rejected when target editing is unavailable.

Laundry timer changes, on/off scheduling, fan and pump commands are not
offered. A writable field alone does not establish the complete action
contract. Timer lifecycle and user-facing constraints need further work.
There is no generic write service that bypasses these restrictions.

## Readings and operating state

| Reading | Source | Interpretation |
| --- | --- | --- |
| Relative humidity | Sensor `relHumidity` | Integer 0..100 with explicit `%` unit, independent of target |
| Room temperature | Sensor `roomTemperature` | Numeric Celsius/Fahrenheit reading, normalized to Celsius |
| Water tank full | Sensor `waterTankFull` | Explicit boolean |
| Defrosting | Sensor `defrostingActive` | Explicit boolean |
| Condensate pump running | Sensor `condensatePumpRunning` | Explicit boolean, observation only |
| Idle state | Sensor `idleState` | Explicit boolean, no humidity comparison |
| Reported device error | Information `error` | Object means reported error; explicit null means none |

Temperature accepts `C`, `degC`, `Celsius`, `°C`, `degF` and `Fahrenheit`.
Home Assistant handles the selected display unit. The local plausible room
temperature envelope is -50..70 °C. Missing values, unknown units, invalid
types and values outside declared bounds remain unavailable. Unlike the Air
purifier contract, Dry relative humidity is an integer value.

The operating-status sensor prioritizes a reported error, full tank and
defrosting, then power off, then the explicit idle report. Normal operation
requires current state and sensor data, known mode, explicit error clearance,
and explicit false reports for tank-full and defrosting. HA action is `idle`
or `drying` only under those conditions. `drying` describes the reported
operating state; it is not a separate compressor or condensate-flow reading.
I never infer operation or reaching the target from humidity alone.

Blocked or unknown states have no normal HA action. When power is explicitly
off, HA action is `off`; the separate status and binary sensors can still
show a full tank or reported error. Unknown error codes are not silently
classified as harmless. Only error presence is stored, not codes or text.
Missing, malformed or retained error information remains unknown.

## Freshness, writes and diagnostics

The existing information, metadata, sensor and shadow streams are reused.
No additional cloud queries or polling intervals are introduced. Online
status comes from the newest information message and stays usable while a
non-retained device message on any channel arrived within the last hour.
State and sensor freshness are required independently; reported state fields
expire after 15 minutes and sensor fields after one hour. Old, duplicate and
retained sensor
messages cannot refresh readings. Sensor envelopes contain timestamped items;
the newest item extends non-null fields in the existing sample. Omitted fields
retain their own age and expire independently. Model and connection changes
prevent reuse of fields from the previous device or connection.

Each write reads fresh state under the device lock and rechecks the exact
model, current rights, capabilities, options and range. It sends only one
requested field and requires a newer matching reported state. A desired-only
acknowledgement is insufficient. Cancellation, rejection, timeout and
disconnect never cause replay. A later read can resolve uncertainty.
There is no proven compare-and-swap token; confirmation shows convergence,
not exclusive ownership of a concurrent external change to the same field.

New capabilities can add entities during operation. Existing IDs, device
association, user names and disabled settings survive reconnects and reloads.
Diagnostics schema 25 adds aggregate `dehumidifiers` counts by known family
for identification, current metadata and current sensor streams. Counts
describe reception, not the HA entity registry. No values, errors, settings,
names, raw model strings or identifiers are included.

## Validation and physical checks

The `dry_4000i` and `dry_8000i` fixtures are synthetic capability profiles,
not captures of retail devices. Tests cover all six prefixes, different
ranges and modes, measured versus target humidity, units, invalid data,
unknown errors, idle and blocked states, fresh write validation, external
changes, missing/delayed confirmations, cancellation and recovery. Native
HA services, DE/EN translations and registry continuity are tested. Both
profiles also use the real aiomqtt MQTT 5 codec with a local WebSocket broker.

The account checked in P08 had no Bacon devices. I have not installed P12 on
a physical Dry device or changed its settings. Each family still needs
confirmation of actual capabilities, message frequency, units, displayed
states and control effects. This package has not been released.

## Deutsch

Ich habe Dry 4000i, 6000i und 8000i in v0.8.2-beta.6 mit einem nativen Entfeuchter-
Bedienelement ergänzt. Ein/Aus, angebotene Betriebsarten und Ziel-Luftfeuchte
verwenden die aktuellen Rechte und Grenzen des Geräts. Kindersicherung und
Display erscheinen nur dann als eigene Schalter, wenn das Gerät sie als
beschreibbar meldet und einen Ein/Aus-Wert liefert. Messfeuchte und
Sollwert bleiben getrennt.

Raumtemperatur, Feuchte, voller Wasserbehälter, Abtauen, Ruhezustand und
laufende Kondensatpumpe werden nur aus passenden Meldungen übernommen.
Gerätefehler bleiben auch bei unbekannten Fehlercodes sichtbar. Fehlende
Angaben werden nicht zu Entwarnungen oder zum vermeintlich erreichten
Sollwert. Normaler Betrieb setzt vollständige, aktuelle Zustandsmeldungen
voraus und ist kein separater Nachweis eines laufenden Kompressors.

Zielwerte lassen sich in Automatik und Nachtmodus ändern. Im Wäscheprogramm
bleibt der gespeicherte Zielwert lesbar. Der Moduswechsel erhält den
vorhandenen Wäschetimer. Timeränderungen, Zeitprogramme sowie Lüfter- und
Pumpenbefehle bietet die Integration nicht an. Ein sichtbares
Zielwertfeld in HA umgeht diese Einschränkungen nicht.

Ich prüfe vor jedem Befehl den aktuellen Gerätestand erneut und ändere nur
das angeforderte Feld. Die Bestätigung muss als neuer gemeldeter Zustand
eintreffen; unbestätigte Befehle werden nicht wiederholt. Diagnose-Schema 25
ergänzt ausschließlich zusammengefasste Familien- und Aktualitätszähler.
Bestehende Kennungen und Benutzereinstellungen bleiben erhalten.

Die Softwareprüfung verwendet synthetische Profile, native HA-Dienste in
Deutsch und Englisch und einen lokalen MQTT-Broker. Die Bestätigung an
echten Geräten steht für alle drei Familien aus. Die Implementierung ist
in Beta.6 enthalten.
