# Older holiday formats

I added ICOM holiday periods and the WDDW2 holiday switch to the existing
holiday interface. The formats have separate contracts and write paths.
Physical-device confirmation is still outstanding.

| Format | Reading and interface | Changes |
|---|---|---|
| Modern `/holidayMode/list` | Existing calendar and dialog | Existing collection-based transactions |
| ICOM `/system/holidayModes/hm{id}` | Whole-day calendar events and the existing dialog | Update reported slots; create only in a confirmed free slot; delete only with a writable delete resource |
| WDDW2 `/system/holidayMode` | Holiday switch and a mode-only options dialog | Offered `on` and `off` values with read-back |

## ICOM dates, slots and settings

Slot identifiers come from discovered resources or references. There is no
assumed set of five slots and no guessed circuit assignment. The date string
`2030-10-26/2030-10-28` includes both dates. In Home Assistant the event runs
from 26 October to the exclusive end on 29 October. The dialog uses the same
exclusive end convention. ICOM has no clock fields, quarter-hour requirement
or writable custom holiday name.

The unused-slot markers `2009-01-01/2009-01-01`,
`2016-01-01/2016-01-01` and `2017-01-01/2017-01-01` do not become calendar
events. Missing, invalid or unavailable dates do not make a slot free. These
reserved markers cannot be submitted as a new holiday.

Each slot supplies its own `assignedTo`, `hcMode`, `dhwMode` and optional
`fixTemperature`. Assignment lists and offered modes must be valid.
Temperature readings use unit `C` or `°C` without scaling. Changes require the
resource's write permission, current limits and any advertised step.
The fixed temperature can only be changed with `FIX_TEMP` or
`FIX_TEMPERATURE`. Unsupported fields are omitted. Incomplete contracts
remain readable without enabling calendar writes.

One slot becomes one event, even when several circuits report it as active.
Modern and ICOM identifiers remain separate. Equal dates alone do not prove
that two different source formats refer to the same setting, so they are not
silently merged.

## Transactions and recovery

ICOM uses scalar PUT bodies such as `{"value":"OFF"}`. Assignments use the
distinct `{"values":["hc2"]}` body. Deletion sends
`{"value":"TRUE"}` to the selected slot's `delete` resource. These are
synthetic protocol examples, not commands to apply to an installation.
Deletion requires a writable scalar string resource without a unit. If it
advertises allowed values, they must include the documented `TRUE` command.

Before editing, I reread the known resources of the selected slot. Changed
settings are written in this order: hot-water mode, heating mode, fixed
temperature, assignments, and dates last. Unchanged fields are skipped.
Before the first change I check that every field to be changed is
writable, so a read-only field stops the change before anything is sent.
Each change starts right after a complete read of the slot: the initial
one or the read-back of the previous step. After each change I read the slot
again after 0.5 s, and if the value is not yet there, after 1 and 2 s more.
On a gateway that reports the device type `icom`, the read-backs follow after
1, 2, 3 and 4 s.
A lost response to a change is resolved by this read-back, never by sending
it again. An app change of another field, missing permission, rejected value
or unmatched read-back stops the transaction. No write is retried
automatically and no rollback is attempted. Circuit assignments are compared
as a set and sent in the order the slot advertises them.

A network, authentication or service failure may leave earlier steps applied.
The integration invalidates uncertain readings and ordinary polling restores
them. Successfully reread partial results remain visible. A successful
calendar change is reported only after all requested values match.
PointT has no atomic compare-and-set for this sequence; a concurrent change
inside the remaining request window cannot be ruled out.

The WDDW2 mode is independent of these slots. It neither creates calendar
events nor accepts holiday dates. Unknown current modes, missing permissions,
or unavailable on/off options block its switch.

## Rules from the component list (expected, untested)

The manufacturer's app reads the component list `/system/info` and applies two
rules to holidays. I use only the component type (`Id`) and its token (`Tok`)
of each entry, never a serial number, name or version, and the rules only
remove choices:

- **HMI800 HP Light (`Id` 240):** the app never writes the heating mode
  (`hcMode`) or the circuit assignment (`assignedTo`) of a holiday. With such a
  component the dialog offers only the slot's current heating mode and
  circuits, and the transaction refuses a change of either before the first
  PUT. Dates, hot water mode and fixed temperature stay changeable. A free slot
  without assigned circuits then offers no new holiday.
- **Token 16:** when an entry has token 16, the app does not offer the heating
  circuits of the tokens 24 to 27 (`hc1` to `hc4`). The dialog no longer offers
  these circuits for a holiday; a circuit the slot already has stays.

No system with either case has been observed; both rules are expected,
untested. The diagnostics of an iCom gateway list the `Id`/`Tok` pairs and
whether a rule applies.

## Validation limits

I checked synthetic parser, transaction, HTTP-envelope and Home Assistant
dialog cases. Diagnostics schema 19 reports the detected format without
holiday dates, names, assignments, current modes or temperatures. It adds no
cloud requests. Normal discovery and polling retain their existing bounds.
I have not installed this work on an ICOM or WDDW2 device or performed
physical write tests. Unsupported delete metadata keeps deletion unavailable.

## Deutsch

Ich habe ICOM-Urlaubszeiten und den WDDW2-Urlaubsmodus in die vorhandene
Bedienung eingebunden. Die Bestätigung an echten Geräten steht noch aus.

ICOM speichert ganze Urlaubstage in gemeldeten Speicherplätzen. Das
Geräte-Enddatum ist eingeschlossen. In Kalender und Dialog ist das Enddatum
weiterhin der erste Tag nach dem Urlaub. Deshalb wird aus
`2030-10-26/2030-10-28` ein Zeitraum vom 26. bis zum exklusiven Ende am
29. Oktober. Uhrzeiten, Viertelstunden und frei wählbare Urlaubsnamen sind
für dieses Format nicht vorgesehen.

Ich übernehme Kreise, Betriebsarten, Schreibrechte und Temperaturgrenzen
aus dem jeweiligen Speicherplatz. Die konstante Temperatur nehme ich mit der
Einheit `C` oder `°C` an. Eine fehlende oder fehlerhafte Antwort
gilt nicht als freier Platz. Nur die oben genannten bekannten Leerkennungen
geben einen Platz für das Anlegen frei. Die konstante Temperatur lässt sich
nur zusammen mit der entsprechenden Betriebsart ändern.

Die Löschaktion bleibt als Button im Dialog. Sie erscheint nur, wenn die
zugehörige Löschressource Schreibrechte meldet, und öffnet die Bestätigung.
Die Ressource muss das skalare Textformat ohne Einheit liefern. Falls sie
Optionen meldet, muss der Löschbefehl `TRUE` darin enthalten sein.
Es gibt kein zusätzliches Aktionsmenü. WDDW2 erhält einen Ein-/Aus-Schalter
und einen Dialog für diesen Modus, weil das Gerät dort keine Zeiträume
speichert.

Vor dem ersten Schreiben prüfe ich, ob alle zu ändernden Felder schreibbar
sind; ein nicht schreibbares Feld stoppt die Änderung, bevor etwas gesendet
wird. Jedes geänderte ICOM-Feld schreibe ich direkt nach einem vollständigen
Lesen des Speicherplatzes: dem ersten oder dem Zurücklesen des vorigen
Schritts. Danach lese ich den Platz nach 0,5 s erneut, falls der Wert noch
fehlt, nach weiteren 1 und 2 s; bei einem Gateway mit dem Gerätetyp `icom`
nach 1, 2, 3 und 4 s. Eine verlorene Antwort klärt dieses
Zurücklesen, gesendet wird nie ein zweites Mal. Kreiszuordnungen vergleiche
ich als Menge und sende sie in der gemeldeten Reihenfolge des Platzes. Die
Reihenfolge ist Warmwasser, Heizung, konstante Temperatur, Kreiszuordnung und
zuletzt der Zeitraum. Bei Konflikten oder Fehlern bricht
der Ablauf ab. Bereits ausgeführte Schritte können dabei bestehen bleiben.
Es gibt weder automatische Schreibwiederholungen noch ein eigenmächtiges
Zurücksetzen. Unsichere Anzeigen werden bis zur nächsten erfolgreichen
regulären Abfrage als nicht verfügbar behandelt.

Ich habe die Abläufe mit synthetischen Daten geprüft. Diagnose-Schema 19
nennt die erkannten Formate, enthält aber keine Urlaubszeiten, Namen,
Zuordnungen, aktuellen Betriebsarten oder Temperaturen. Es entstehen dadurch
keine zusätzlichen Cloud-Abfragen. Echte Schreibtests an ICOM- und
WDDW2-Geräten sowie die Bestätigung ihrer jeweiligen Metadaten stehen aus.

Aus der Komponentenliste `/system/info` lese ich nur Typ (`Id`) und Token
(`Tok`) und übernehme zwei Regeln der Hersteller-App, die nur Auswahl
wegnehmen: Bei einem HMI800 HP Light (`Id` 240) bleiben Heizbetriebsart und
Kreiszuordnung eines Urlaubs so, wie der Speicherplatz sie meldet; eine
Änderung daran lehnt die Transaktion vor dem ersten Schreiben ab. Bei Token 16
biete ich die Heizkreise der Tokens 24 bis 27 nicht neu an. Beide Fälle habe
ich an keiner Anlage gesehen; sie sind erwartet, aber ungetestet.
