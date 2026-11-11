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
| `auto` | Auto | Exposes `currentRoomSetpoint` as the temperature attribute; changes disabled |
| `off` | Off | No temperature command |

Only advertised, understood codes appear. An unknown current mode makes
the climate unavailable; the original select retains additional advertised
codes. A temperature call never changes a mode. Combined mode/temperature
calls are rejected before any write; select the mode separately first.
Bounds and units come from that circuit. I convert temperatures and steps
between the reported unit and Home Assistant's configured unit.
Home Assistant's standard thermostat does not display a target control in
automatic mode because this mode does not support temperature commands.
The reported target remains available through the climate's `temperature`
attribute and the existing current-target sensor.

I show room temperature only from that circuit's valid `roomtemperature`
resource. I do not substitute a flow temperature, infer active heating from
a temperature difference or claim that a winter-mode flag proves heat output.
No heating-action state is invented.

Cooling is outside this climate implementation. A reported cooling-only
circuit, active cooling or stale/unknown heating context makes the climate
unavailable. Existing individual controls remain available under their own
contracts. The mode, target and known heating context are checked again on
the appliance before a climate write. Scheduling, temporary overrides and
cooling setpoints are not changed by this climate.
This includes the central actual heating/cooling status when reported, even
when the heating circuit has no individual status resource.

Automated tests include a complete Home Assistant browser flow against a
simulated gateway, covering repeated applies, retained invalid drafts,
uncertain writes and recovery through regular polling.
I installed this build locally on Home Assistant 2026.10.3 and tested my first
heating circuit in manual mode. Applying 19.0 to 18.5 °C in the controls dialog
was confirmed by appliance read-back and both the number and climate entities.
In the repeated test, I used the native climate dialog's plus button to restore
19.0 °C. I confirmed both 18.5 °C and the restored 19.0 °C in MyBuderus.
A conditional, timed restore in Home Assistant also protected both tests
independently of the browser; it restored the value during the first test.
This does not confirm mode changes, other circuits or other appliances.

This practical check also caught lost keyboard focus after Apply. I corrected
it and verified Escape directly after Apply and Reload values in the real
browser and the automated Home Assistant test, without an extra click.

### Heating and hot water schedules

Since 0.9.0-alpha.6, **Configure > Heating and hot water schedules** changes
the weekly programs A and B of a heating circuit and program A of the hot
water. I list a program only when the gateway reports it as writable, it
passes the strict program check, its type is a level program (`level`), and
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

The program is read again just before sending. If it differs from what the
dialog showed, nothing is sent. The whole week goes out once as a raw JSON
array, the way the manufacturer's apps write it, and is read back; the
confirmed program is shown at once. A refusal of the gateway changes
nothing. After a lost answer or a server error the outcome is resolved only
by reading, never by sending again; until then the program counts as not
current. The success message repeats the previous switch points, so they
can be set back by hand. Diagnostics count the writes and the switch points
per day, without times or levels.

The selection **Active schedule** switches between programs A and B. It
appears only when the gateway allows both, the current one among them, and
both programs can be read and are valid. The options are `a` and `b`; the
reported names are in the attributes `program_names` and `program_name`.
With a single program, as on my heat pump, there is no selection.

So far tested with simulated data only: both functions are expected to work
but untested on a real gateway. A test on my heat pump (heating circuit 1,
program A, and hot water program A) is planned. Program B, iCom gateways and
programs with absolute temperatures stay expected, untested.

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
Ich habe diesen Stand lokal auf Home Assistant 2026.10.3 installiert und meinen
ersten Heizkreis im manuellen Betrieb geprüft. Die Änderung von 19,0 auf 18,5 °C
im Bedienungsdialog wurde durch Rücklesen an der Anlage sowie durch Einzelregler
und Klimaentität bestätigt. Bei der Wiederholung habe ich über die
Plus-Schaltfläche der nativen Klimaentität wieder 19,0 °C eingestellt.
Sowohl 18,5 °C als auch die wiederhergestellten 19,0 °C habe ich in MyBuderus
bestätigt. Eine zusätzliche bedingte Rückstellung direkt in Home Assistant
sicherte beide Tests unabhängig vom Browser ab; beim ersten Test stellte sie
den Wert zurück. Betriebsartenwechsel, andere Kreise und andere Anlagen sind
damit nicht praktisch bestätigt.

Dabei fiel außerdem ein verlorener Tastaturfokus nach Übernehmen auf. Ich habe
ihn korrigiert und Escape direkt nach Übernehmen und Werte neu laden im realen
Browser sowie im automatisierten Home-Assistant-Test ohne weiteren Klick geprüft.

### Zeitprogramme Heizung und Warmwasser

Seit 0.9.0-alpha.6 ändert **Konfigurieren > Zeitprogramme Heizung und
Warmwasser** die Wochenprogramme A und B eines Heizkreises und das Programm A
des Warmwassers. Angeboten wird ein Programm nur, wenn das Gateway es als
schreibbar meldet, es die strenge Programmprüfung besteht, es ein
Stufenprogramm (`level`) ist und jede verwendete Stufe zu den gemeldeten
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

Direkt vor dem Senden wird das Programm erneut gelesen. Weicht es vom Stand
im Dialog ab, wird nichts gesendet. Die ganze Woche geht einmal als rohes
JSON-Array hinaus, so wie die Hersteller-Apps sie schreiben, und wird
zurückgelesen; das bestätigte Programm erscheint sofort. Lehnt das Gateway
ab, ändert sich nichts. Nach einer verlorenen Antwort oder einem
Serverfehler klärt nur das Lesen den Ausgang, nie ein zweites Senden; bis
dahin gilt das Programm als nicht aktuell. Die Erfolgsmeldung wiederholt die
bisherigen Schaltpunkte, damit man sie von Hand zurückstellen kann. Die
Diagnose zählt die Schreibvorgänge und die Schaltpunkte pro Tag, ohne
Uhrzeiten und Stufen.

Die Auswahl **Aktives Zeitprogramm** wechselt zwischen den Programmen A und
B. Sie entsteht nur, wenn das Gateway beide erlaubt, das aktuelle darunter
ist und beide Programme lesbar und gültig sind. Die Optionen sind `a` und
`b`; die gemeldeten Namen stehen in den Attributen `program_names` und
`program_name`. Mit nur einem Programm, wie an meiner Wärmepumpe, gibt es
keine Auswahl.

Bisher nur mit simulierten Daten getestet: Beide Funktionen sollten
funktionieren, sind aber an einem echten Gateway ungetestet. Ein Test an
meiner Wärmepumpe (Heizkreis 1, Programm A, und Warmwasserprogramm A) ist
geplant. Programm B, iCom-Gateways und Programme mit absoluten Temperaturen
bleiben erwartet, ungetestet.
