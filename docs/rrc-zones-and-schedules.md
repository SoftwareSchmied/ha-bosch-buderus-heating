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
TC100.2 does, I offer exactly `manual` and `clock`, as the controller's
app does; another current mode keeps the select unavailable. An empty or
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
Temporary overrides remain read-only because expiry and cancellation are
not yet sufficiently established.

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
TC100.2, biete ich wie die App des Reglers genau `manual` und `clock`
an; eine andere aktuelle Betriebsart lässt die Auswahl unverfügbar.
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
Vorübergehende Sollwerte bleiben lesend, bis Ablauf und Aufhebung geklärt sind.

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
