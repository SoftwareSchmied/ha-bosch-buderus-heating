# RRC zones and heating schedules

I added separate RRC zone readings, controls and an editor for existing
heating schedule points. These functions require compatible resources from
the selected PointT gateway. They do not establish support for every device
marketed with an RRC controller. The read structure (flags, types, units and
limits) has been observed on a TC100.2 with read-only requests;
writes and other models still need physical confirmation.

## Discovery and readings

RRC uses `/zones/zn{id}`, independently of ConnectKey's `/zones/zone{id}`.
I obtain IDs from reported references or the `value` array of a validated
`zoneArray` at `/zones/list`. Only then do I probe `/programs` and
`/programs/list`. Program IDs come from references or the reported
`programArray`. I do not scan a range of guessed device or program IDs.

Known zone fields and the weekly resource of each reported program are
probed even if its directory returns 403, 404 or 406. Discovery retains
its limits of 30 paths per batch, 512 paths overall and eight levels.
Successfully discovered values recover through normal polling. Failed
optional probes do not become recurring requests. Day resources are read
when editing, rather than polling seven days in addition to each week.

| Zone suffix | Meaning | Control |
|---|---|---|
| `temperatureActual` | Measured room temperature | Reading |
| `temperatureHeatingSetpoint` | Current heating target | Reading |
| `manualTemperatureHeating` | Stored manual heating target | Number |
| `clockOverride/temperatureHeating` | Temporary heating target | Reading |
| `userMode` | Manual or scheduled operation | Select |
| `clockProgram` | Selected program ID | Select from the current program list |

I preserve temperature precision. I accept the reviewed Celsius unit
spellings `C`, `°C` and `ºC`, without importing other families' conversion
rules. False or malformed `used`/`available` flags, invalid values, numeric
error markers and incompatible shapes do not produce a usable reading.
I also accept the exact strings `"true"` and `"false"`, which the
TC100.2 sends; other spellings and numbers remain malformed.
The measured temperature and active target are enabled by default; the
remaining reading mirrors are optional. Controls have separate stable IDs.

A zone name from `/zones/zn{id}/name` prefixes the zone's entity names. The
controller sends it in Base64: canonical Base64 that decodes to printable
UTF-8 text is shown decoded, any other text as it is, and an empty or
unprintable name falls back to "RRC zone {id}". Only the names change;
entity IDs and unique IDs stay. Program names and the names inside
`/zones/list` and `/programs/list` are not used.

Only offered, known `manual` and `clock` options can be written. If the
controller sends no `allowedValues` key for `userMode` at all, as the
TC100.2 does, I offer exactly `manual` and `clock`; another current mode
keeps the select unavailable. An empty or
different list keeps the strict rules. A mode reported with `used` true and
`available` false is locked by the controller, for example while the system
cools: the select keeps showing the current mode, and a change is refused
with its own message before anything is sent. This applies only to
`userMode`; other fields with `available` false stay unavailable.

The manual
target uses this resource's current bounds and advertised step. An absent
step uses a 0.5 °C UI increment; it does not prove an additional API rule.
Changing a manual target does not also change the operating mode.
Changing the selected program does not switch the zone into scheduled mode.
On room controllers of the RRC 2.0 platform the zone climate also sets and
clears a temporary setpoint until the next switch point; see
[room controllers](room-controllers.md#zones) for these functions and what
is confirmed.

## Editing an existing weekly program

Open the integration's **Configure** action, then **RRC heating schedules**.
Choose a program, weekday and existing point. The dialog accepts hours and
minutes, with no seconds field. Program IDs identify shared schedules;
changing a schedule affects all zones using it. Weekday names and field
labels follow the Home Assistant profile language, including when it differs
from the system language.

The reviewed format is `weekProgram` with a `value` array. Every point has
`d` (`Mo Tu We Th Fr Sa Su`), `t` (minutes since midnight) and `Th`
(Celsius). `week` supplies all weekdays. A day is written through the
corresponding lowercase English path, such as `/programs/pg3/monday`,
with a body shaped as `{"value":[{"d":"Mo","t":425,"Th":21.5}]}`.
This is a synthetic format example, not a suggested setting.

The editor permits the reviewed five-minute time raster and 5 to 30 °C in
0.5 °C steps. It changes one existing point and preserves the other points
of that day. It does not add or remove points or create programs because
the supported capacity and those lifecycle operations are not established.
Empty, unsorted, duplicate or extended formats remain unchanged. Supported
readable formats can be viewed; unknown fields prevent editing and survive
an unchanged parser/encoder round trip. An unsupported shape is not
presented as a valid schedule.

These are recurring local wall-clock times, without calendar dates or UTC
conversion. A stored Sunday 02:30 remains 02:30 regardless of the HA time
zone. The controller's execution during a clock change still needs device
confirmation. A day with no points is not labelled as heating off.
The holiday calendar and its existing dialog keep their own semantics.

## Write confirmation and remaining limits

I reread live metadata before scalar commands. Program selection rereads
the offered program IDs. Schedule editing rereads the week and the target
day and compares them with the displayed version. A detected app change
stops the write. Polling and writes share the coordinator lock.

Every operation sends at most one PUT and checks the resulting resource.
A lost response is resolved by bounded read-back, without repeating the
PUT. Authentication failures and rate limits stop the operation. An
unconfirmed schedule or selection becomes unavailable until a regular
poll recovers it. There is no documented atomic compare-and-swap mechanism;
a simultaneous external write after preflight remains possible.

Diagnostics schema 20 reports RRC availability and format eligibility without
temperatures, schedules, user names or extra cloud requests. Canonical zone
and program paths remain identifiable; unknown dynamic IDs are redacted.
The schema distinguishes editing existing points from adding new points.

I test these contracts with synthetic multi-zone data, simulated errors,
concurrent edits, native HA dialogs and an actual local HTTP endpoint.
An anonymized fixture records the read structure observed on a
TC100.2. Writes on a physical RRC device remain unverified.

## Room controllers of the RRC 2.0 platform

The TC100.2 belongs to a platform that the cloud reports as product type
`RRC 2.0`; room controllers such as the CT200 belong to it as well. The
integration recognises such a controller only by its resources: a reported
device type `rrc2`, the product type `RRC 2.0` in `/gateway/productType`, or
RRC zones. It never decides by a model name, and a gateway is offered in the
setup whatever its device type. A gateway without a name gets the device name
"Room controller"; its model comes from the product type the cloud reports.

On such a controller the discovery behaves as follows:

- **Lists first.** `/zones/list`, `/programs/list` and `/devices/list` are
  read before every other pending path, within the same limits.
- **Device tree.** Of the `/devices` tree only the device list, for each
  listed device its `versionFirmware` (at most 64 devices) and the known
  setting leaves of its type are read. The probes of other device families
  are not requested. The setting leaves come last, leaf by leaf over all
  devices, and have a cap of their own of 192 paths outside the 512. On a
  TC100.2 with nine radio devices the device tree used 206 of the 512 reads
  before alpha.5.
- **Missing paths.** These controllers answer HTTP 500 for a path they do not
  have. A 500 answer to a catalog or optional probe is taken as "missing"
  and does not count towards the three 5xx answers in a row that end the
  discovery. Roots, advertised references and a failing request as a whole
  keep that rule. A device list that got no answer is searched again like
  the program list.
- **Never read.** `/devices/devN` (it holds the radio key `dlk` and the radio
  identifier `sgtin` of each device), `/devices/productLookup`, every
  `homematicip` subtree, `/gateway/userPassword`, `/gateway/user/*`,
  `/gateway/installer/*`, `/system/location/*` and
  `/system/autoAway/users/list` are never requested, stored, polled or
  exported, on any gateway. A stored inventory of an earlier release that
  still holds them drops them when it is loaded.

### Radio devices (read only)

Every device in `/devices/list` except the controller's own entry becomes its
own Home Assistant device below the controller (`via_device`). Its identifier
is the gateway and the list ID (`<gateway>:devices:device<id>`), never a
radio identifier. A new device is named after its type and list ID, for
example "Radiator thermostat 2"; its zone name is suggested as its area. The
model is the functional type: room controller, radiator thermostat, room
thermostat, battery room thermostat, underfloor heating actuator, radio
repeater, heat generator link, radio receiver, or radio device for any other
type.

| Value | Entity | Default |
|---|---|---|
| `battery` `low` / `ok` / `unknown` | Binary sensor "Battery low" (on, off, unknown) | enabled, diagnostic |
| `signal` 0 to 100 | Sensor "Radio signal" in % | disabled, diagnostic |
| `/devices/device<id>/versionFirmware` | Firmware version of the device and its sensor | as before |

An entity exists only for a value the device reports. The list is read
hourly. A temporary failure keeps the last state within its validity; a
list that is no longer answered makes the devices unavailable. Adding,
removing and pairing devices stays on the controller. The valve readings,
the valve problem sensor and the device settings (child lock, temperature
offset, open window sensitivity) are described in
[room controllers](room-controllers.md#radio-devices); they are expected,
untested.

### Sign-in

These controllers use the same cloud and the same SingleKey ID as the other
heating apps. Sign in with the SingleKey ID of the app in which the
controller is set up, and choose the brand of that app in the setup. The
integration uses its usual sign-in and asks for no further rights; whether
that sign-in reads a CT200 is still to be confirmed. If it does not, the
diagnostics show `access_reason: scope_or_permission_denied`.

### Local access and older firmware

There is no local access for these room controllers. No local interface is
documented for them, and the published local interface covers only the
K40 RF gateway. If a K40 gateway is also
present, its local access stays independent of the room controller.

A room controller with older firmware talks to a different cloud service and
does not appear in the device list of the account. It appears once its
firmware has been updated and it has been set up in the current app with the
SingleKey ID; the setup's "no devices" message says so.

### What is confirmed

The zones of a TC100.2 have been read with this integration (issue 113), and
its manual/schedule mode and manual setpoint have been changed with it. The
radio devices, the device list, the functions of alpha.5 listed in
[room controllers](room-controllers.md) and every CT200 are untested: they are built
on constructed data and the anonymized device tree of a TC100.2, and the
diagnostics report `rrc_profile.confirmation: unconfirmed`. A synthetic
fixture with 11 zones, 12 radio devices and 24 programs
(`tests/fixtures/contracts/rrc/ct200-shapes.json`) checks the discovery
budget, the devices and the privacy rules from discovery to the entities.

## Deutsch

Ich habe RRC-Zonen mit eigenen Anzeigen, Bedienelementen und einem Editor für
vorhandene Schaltpunkte ergänzt. Voraussetzung sind passende Ressourcen am
ausgewählten PointT-Gateway. Damit ist noch keine vollständige Unterstützung
aller Geräte mit RRC-Regelung nachgewiesen. Die gelesene Struktur (Merkmale,
Typen, Einheiten und Grenzen) wurde an einem TC100.2 mit reinen
Leseanfragen beobachtet; Schreibzugriffe und andere Modelle stehen aus.

Die Zonenpfade `/zones/zn{id}` bleiben von ConnectKey-`zone{id}` getrennt.
Zonen- und Programmnummern stammen aus gemeldeten Referenzen oder geprüften
Listen. Erst ein erkannter RRC-Bedarf erlaubt die Programmsuche. Die Grenzen
von 30 Pfaden pro Batch, 512 Pfaden und acht Ebenen bleiben erhalten.
Fehlerhafte Verzeichnisse verhindern die bekannten Abfragen ihrer gemeldeten
Zone nicht. Wochenprogramme werden regulär abgefragt; einzelne Tage lese ich
erst beim Bearbeiten zusätzlich.

Raumtemperatur, aktiver Sollwert, manueller Sollwert und vorübergehender
Sollwert sind getrennte Anzeigen. Ich erhalte die gemeldete Genauigkeit und
prüfe Celsius-Einheit, Fehlerwerte und Verfügbarkeitsmerkmale. Gültig ist ein
Merkmal als JSON-Wahrheitswert oder als exakte Zeichenkette `"true"` bzw.
`"false"`, wie sie der TC100.2 sendet; andere Schreibweisen und Zahlen
bleiben ungültig. Raumtemperatur und aktiver Sollwert sind standardmäßig
aktiviert; weitere Anzeigen bleiben optional. Die Bedienelemente besitzen
eigene stabile IDs.

Der Zonenname aus `/zones/zn{id}/name` steht vor den Namen der Entitäten
dieser Zone. Der Regler sendet ihn in Base64: Gültiges Base64, das sich zu
druckbarem UTF-8-Text dekodieren lässt, zeige ich dekodiert, jeden anderen
Text unverändert; ein leerer oder nicht druckbarer Name wird zu
„RRC-Zone {id}“. Nur die Namen ändern sich, Entitäts-IDs und eindeutige IDs
bleiben. Programmnamen und die Namen in `/zones/list` und `/programs/list`
verwende ich nicht.

Die Betriebsart bietet die gemeldeten bekannten Optionen `manual` und
`clock`. Sendet der Regler für `userMode` gar kein `allowedValues`, wie der
TC100.2, biete ich genau `manual` und `clock` an; eine andere aktuelle
Betriebsart lässt die Auswahl unverfügbar.
Eine leere oder abweichende Liste behält die strengen Regeln. Meldet der
Regler die Betriebsart mit `used` wahr und `available` falsch, sperrt er sie,
etwa während die Anlage kühlt: Die Auswahl zeigt weiter die aktuelle
Betriebsart, eine Änderung wird mit eigener Meldung abgelehnt, bevor etwas
gesendet wird. Das gilt nur für `userMode`; andere Felder mit `available`
falsch bleiben unverfügbar. Der manuelle Sollwert verwendet seine eigenen aktuellen Grenzen
und die gemeldete Schrittweite. Ohne Schrittweite nutzt die Oberfläche
0,5 °C, ohne daraus eine zusätzliche API-Einschränkung abzuleiten.
Das Ändern des Sollwerts schaltet die Betriebsart nicht um. Auch eine
Programmauswahl aktiviert nicht gleichzeitig den Zeitbetrieb.
Auf Raumreglern der Plattform RRC 2.0 setzt und hebt das Zonen-Climate
zusätzlich einen vorübergehenden Sollwert bis zum nächsten Schaltpunkt auf;
siehe [Raumregler](room-controllers.md#deutsch).

Unter **Konfigurieren**, **RRC-Zeitprogramme** lassen sich Programm,
Wochentag und vorhandener Schaltpunkt auswählen. Uhrzeiten haben kein
Sekundenfeld. Die Wochentage und Feldtexte folgen der Profilsprache.
Programmnummern kennzeichnen gemeinsam verwendete Programme: Eine Änderung
betrifft alle zugeordneten Zonen.

Ein Punkt enthält Wochentag, Minuten seit Mitternacht und Celsius-Temperatur.
Der Editor verwendet die belegten fünf Minuten sowie 5 bis 30 °C in
0,5-°C-Schritten. Ich schreibe nur den betroffenen Tag und erhalte dessen
übrige Punkte. Neue Programme sowie zusätzliche oder gelöschte Schaltpunkte
sind mangels belegter Kapazitäts- und Ablaufregeln noch nicht verfügbar.
Unbekannte Zusatzfelder sperren die Bearbeitung und gehen beim unveränderten
Einlesen und Ausgeben nicht verloren. Lesbare Sonderformen bleiben
unverändert anzeigbar; ungültige Formen werden nicht als gültiger Plan gezeigt.

Die Zeiten wiederholen sich in der Ortszeit der Anlage. Ich rechne sie nicht
in UTC um. Auch sonntags gespeicherte 02:30 bleiben bei einer anderen
HA-Zeitzone 02:30. Wie das Gerät während einer Zeitumstellung schaltet,
muss noch praktisch geprüft werden. Ein Tag ohne eigene Schaltpunkte wird
nicht als ausgeschaltete Heizung interpretiert. Urlaubskalender und
Urlaubsdialog bleiben fachlich getrennt.

Vor einem Schreibzugriff prüfe ich die aktuellen Angaben erneut. Beim
Programm vergleiche ich Woche und Tag mit dem angezeigten Stand.
Erkannte Änderungen durch die App stoppen den Vorgang. Jeder Vorgang sendet
höchstens einen PUT und liest anschließend zurück. Eine verlorene Antwort
führt zu begrenzten Rückleseversuchen. Anmeldungsausfälle und Anfragelimits
beenden den Vorgang; unbestätigte Werte bleiben bis zur Erholung unverfügbar.
Eine fremde Änderung zwischen letzter Prüfung und PUT lässt sich ohne
dokumentierte atomare Versionsprüfung weiterhin nicht vollständig verhindern.

Diagnose-Schema 20 zeigt Verfügbarkeit und Formateignung ohne Temperaturen,
Zeitpläne, Namen oder zusätzliche Anfragen. Numerische Zonen- und
Programmpfade bleiben erkennbar, unbekannte dynamische IDs redigiert.
Die Prüfungen verwenden synthetische Daten, einen lokalen HTTP-Endpunkt
und native HA-Dialoge. Eine anonymisierte Fixture hält die an einem
TC100.2 beobachtete Lesestruktur fest. Schreibtests am Gerät stehen aus.

### Raumregler der Plattform RRC 2.0

Raumregler wie der CT200 gehören zur selben Plattform wie der TC100.2
(Produkttyp `RRC 2.0`). Ich erkenne sie nur an ihren Ressourcen: Gerätetyp
`rrc2`, Produkttyp `RRC 2.0` oder RRC-Zonen, nie an einem Modellnamen. Ein
Gateway ohne Namen heißt „Raumregler“. Auf einem solchen Regler liest die
Discovery zuerst Zonen-, Programm- und Geräteliste. Vom Gerätebaum liest sie
nur die Geräteliste, je gelistetem Gerät dessen Firmwareversion (höchstens
64 Geräte) und die bekannten Einstellungsblätter seines Typs (eigener Deckel
von 192 Pfaden außerhalb der 512); Geräteschlüssel (`/devices/devN` mit `dlk` und `sgtin`),
`homematicip`, Benutzer-, Installateur-, Passwort-, Standort- und
Telefondaten werden nie abgefragt, gespeichert oder ausgegeben. Eine
HTTP-500-Antwort auf eine Katalog- oder Zusatzabfrage gilt als „fehlt“ und
beendet die Discovery nicht.

Jedes Funkgerät der Liste außer dem Regler selbst wird ein eigenes Gerät
unter dem Raumregler, benannt nach seinem Typ und seiner Listennummer, etwa
„Heizkörperthermostat 2“, mit dem Zonennamen als vorgeschlagenem Bereich.
„Batterie schwach“ ist aktiviert, „Funksignal“ standardmäßig deaktiviert;
beide sind Diagnosewerte. Ventilwerte, Ventilproblem und die Einstellungen
der Funkgeräte (Kindersicherung, Temperatur-Offset, Empfindlichkeit der
Fenster-offen-Erkennung) beschreibt [Raumregler](room-controllers.md#deutsch);
sie sind erwartet, ungetestet.

Für diese Raumregler gibt es keinen lokalen Zugang. Ein Regler mit älterer
Firmware erscheint erst nach dem Firmware-Update und der Einrichtung in der
aktuellen App mit der SingleKey-ID. Funkgeräte und der CT200 sind mit dieser
Integration noch nicht getestet; die Diagnose meldet
`rrc_profile.confirmation: unconfirmed`.

