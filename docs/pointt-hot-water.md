# PointT hot water

## English

This guide describes the PointT entity included in stable **0.8.2** and
introduced in **0.8.2-beta.7** through
PR #87. The older 0.8.2-beta.6 and 0.8.1 packages do not include this entity.
The Midea/RWH water heaters released in Beta.6 use a separate device adapter.

I added one native Home Assistant water-heater entity per discovered PointT
hot-water circuit. It combines the measured water temperature, the active
setpoint reported by the appliance and the operating mode. It stays on the
existing appliance device and can be used on the device page or a dashboard.

The readings come from that circuit's `actualTemp` and `currentSetpoint`.
Missing or invalid readings remain unknown. I do not substitute the extra
hot-water target, an Eco/Comfort start temperature or another circuit's reading.
Reported Celsius, Fahrenheit and Kelvin values are converted to Home
Assistant's display unit.

The active setpoint is an **indication, not a temperature control**. The
existing supported commands do not directly set `currentSetpoint`.
The individually adjustable `temperatureLevels` are start thresholds; they
remain under **Operate appliance** and in the existing number entities.
A `water_heater.set_temperature` call cannot change those thresholds, start an
extra charge or switch an operating mode. No artificial control limits or step
size are advertised.

Home Assistant's standard more-info dialog shows the current temperature and
mode without a target-temperature control. The read-only target is available
as the entity's `temperature` attribute. A standard tile can show all three
values; replace the example entity ID with the one from your installation:

```yaml
type: tile
entity: water_heater.your_hot_water_circuit
state_content:
  - state
  - current_temperature
  - temperature
```

Tapping the tile opens the native operating-mode dialog.

On the device page, tap the hot-water entity, open **Mode** and select an
offered option. This writes immediately and confirms the result by reading it
back; there is no separate Apply button. To edit several related settings as
a draft, use **Configure > Operate appliance > Hot water** instead.
After a mode change, the active target can temporarily be unknown until the
next regular reading. A confirmed mode does not yet confirm a new target.

### Operating modes

Only understood codes actually offered by the circuit are selectable. The
labels follow the existing PointT selectors and the user's profile language.

| PointT code | English | German |
| --- | --- | --- |
| `Off` | Off | Aus |
| `low` | Eco | Eco |
| `high` | Comfort | Komfort |
| `ownprogram` | Auto | Auto |
| `eco` | Eco+ | Eco+ |
| `HCprogram` | Follow heating schedule | Heizungszeitprogramm folgen |

A read-only operating mode can still be displayed. A missing or unknown
current mode makes the combined entity unavailable. Additional offered codes
remain accessible through the existing select entity under its own contract.
I do not infer heating activity, charging, disinfection or holiday status from
the selected operating mode or from a temperature difference.

### Requests, continuity and validation

Creating and displaying the entity uses cached discovery/polling results.
It adds no discovery paths, polling intervals or regular cloud requests.
Before a mode write, I reread and compare its value and metadata. I use the
existing serialized write transaction with mandatory read-back. A usual change
uses one preflight GET, one PUT and one confirming GET. Existing bounded
read-back retries and account pauses still apply; an uncertain result does
not resend the PUT.

Confirming a mode write does not confirm its resulting setpoint. The combined
entity therefore hides an older setpoint until a regular reading arrives.
Failed or unconfirmed mode writes leave the mode unavailable until recovery
through regular polling.

New entities can appear during polling when their required data becomes usable.
Their identity includes the exact gateway and circuit. Existing sensors,
number controls, select IDs, custom names, disabled states and device
assignments remain intact. The original mode select moves to Configuration,
where it remains available for individual use and existing automations.

Tests cover exact circuit routing, mode subsets, unknown codes, stale/invalid
readings, display units, failed writes, recovery and real Home Assistant
service/registry behavior. These are software tests with synthetic devices.
I also tested the native dialog and the tile above in Home Assistant's browser
frontend in German and English, including a mobile viewport, mode changes,
updated readings and closing with Escape.

This build then ran on a real installation with Home Assistant 2026.10.3.
The native hot-water display was checked on desktop and mobile against the
existing temperature sensors, including the read-only target and the offered
German mode labels. Existing entity identities and customizations were retained.
Auto was then changed to Off and back to Auto using the native water-heater
dialog. Both the new entity and the original mode select confirmed the
changes, and the manufacturer's app showed both Off and the restored Auto. The original operating mode was restored. A conditional, timed
restore in Home Assistant protected the test independently of the browser.
Other modes and other appliances remain covered by synthetic tests only.

## Deutsch

Diese Anleitung beschreibt die PointT-Entität in Stable **0.8.2**, eingeführt
mit **0.8.2-beta.7** und PR #87.
Die älteren Pakete 0.8.2-beta.6 und 0.8.1 enthalten diese PointT-Entität nicht. Die Midea/RWH-Warmwassergeräte aus Beta.6 verwenden
eine eigene Geräteanbindung.

Ich habe für jeden entdeckten PointT-Warmwasserkreis eine gemeinsame
Warmwasserentität ergänzt. Sie zeigt die gemessene Temperatur, den von der
Anlage gemeldeten aktuellen Sollwert und die Betriebsart. Sie bleibt beim
bisherigen Anlagengerät und lässt sich auf der Geräteseite oder in einem
Dashboard verwenden.

Die Temperaturen stammen aus `actualTemp` und `currentSetpoint` desselben
Warmwasserkreises. Fehlende oder ungültige Werte bleiben unbekannt.
Extra-Warmwasser-Sollwert, Starttemperaturen und Werte anderer Kreise dienen
nicht als Ersatz. Die Anzeige berücksichtigt die eingestellte Temperatureinheit.

Der aktuelle Sollwert ist eine **Anzeige ohne direkten Temperaturregler**.
Über die bisher unterstützten Befehle lässt sich `currentSetpoint` nicht direkt
einstellen. Die getrennten Starttemperaturen für Eco, Eco+ und Komfort bleiben
unter **Anlage bedienen** und in den vorhandenen Zahlenentitäten einstellbar.
Ein Temperaturbefehl an die neue Entität verändert weder diese Schwellen noch
die Betriebsart und startet auch keine Extra-Warmwasserbereitung.

Der Standarddialog von Home Assistant zeigt Isttemperatur und Betriebsart.
Der nur lesbare Sollwert steht im Attribut `temperature` zur Verfügung.
Das oben gezeigte Beispiel für eine normale Dashboard-Kachel zeigt alle drei
Angaben gemeinsam. Dafür muss lediglich die Beispiel-Entitäts-ID ersetzt werden.

Tippe auf der Geräteseite auf die Warmwasserentität, öffne **Modus** und wähle
eine angebotene Betriebsart. Die Änderung wird sofort geschrieben und durch
Rücklesen bestätigt; es gibt keinen zusätzlichen Übernehmen-Knopf.
Mehrere zusammengehörige Einstellungen kannst du stattdessen unter
**Konfigurieren > Anlage bedienen > Warmwasser** als Entwurf bearbeiten.
Nach einem Betriebsartenwechsel kann der aktive Sollwert bis zum nächsten
regulären Abruf vorübergehend unbekannt sein. Die bestätigte Betriebsart
bestätigt noch keinen neuen Sollwert.

Die Tabelle oben zeigt die Betriebsarten und ihre deutschen Beschriftungen.
Auswählbar sind nur bekannte Optionen, die der jeweilige Kreis tatsächlich
anbietet und schreiben lässt. Eine nur lesbare Betriebsart bleibt sichtbar.
Eine fehlende oder unbekannte aktuelle Betriebsart macht die gemeinsame
Entität nicht verfügbar. Weitere angebotene Codes bleiben über das bisherige
Auswahlfeld erreichbar. Aus der gewählten Betriebsart wird keine aktive
Aufheizung, Desinfektion oder Abwesenheit abgeleitet.

Die neue Anzeige nutzt vorhandene Daten und erzeugt keine zusätzlichen
regelmäßigen Cloud-Abfragen. Vor einem Betriebsartenwechsel prüfe ich den
aktuellen Wert und die Metadaten erneut. Ein üblicher Wechsel benötigt einen
Prüfabruf, einen Schreibzugriff und einen bestätigenden Abruf. Die bisherigen
begrenzten Rückleseversuche und Abfragepausen bleiben erhalten.

Nach einem bestätigten Betriebsartenwechsel bleibt ein älterer Sollwert in
dieser Entität vorübergehend ausgeblendet, bis ein regulärer Abruf den aktuellen
Wert liefert. Ein fehlgeschlagener oder unbestätigter Schreibzugriff wird nicht
als Erfolg angezeigt. Die Wiederherstellung erfolgt beim regulären Polling.

Neue Entitäten können beim Polling hinzukommen. Bestehende IDs, eigene Namen,
deaktivierte Entitäten und Gerätezuordnungen bleiben erhalten. Das ursprüngliche
Betriebsartenfeld steht unter Konfiguration weiterhin zur Verfügung.

Automatisierte Tests prüfen Datenzuordnung, Einheiten, Übersetzungen,
Schreibfehler, Wiederherstellung und die Home-Assistant-Dienste mit simulierten
Geräten.
Zusätzlich habe ich den nativen Dialog und die oben gezeigte Kachel im
Home-Assistant-Frontend auf Deutsch und Englisch geprüft, einschließlich
Mobilansicht, Betriebsartenwechsel, aktualisierter Werte und Schließen mit Escape.

Danach lief dieser Stand auf einer realen Anlage mit Home Assistant 2026.10.3.
Die native Warmwasseranzeige habe ich auf großem und schmalem
Bildschirm mit den vorhandenen Temperatursensoren abgeglichen, einschließlich
des nur lesbaren Sollwerts und der deutschen Betriebsartenbeschriftungen.
Bestehende Entitätskennungen und eigene Anpassungen blieben erhalten.
Anschließend habe ich über den nativen Warmwasserdialog von Auto auf Aus und wieder zurück auf Auto geschaltet. Sowohl die neue Entität
als auch das ursprüngliche Auswahlfeld bestätigten die Änderungen. Die
Hersteller-App zeigte Aus und das wiederhergestellte Auto ebenfalls. Die ursprüngliche
Betriebsart ist wiederhergestellt. Eine bedingte, zeitgesteuerte Rückstellung
direkt in Home Assistant sicherte den Test unabhängig vom Browser ab.
Andere Betriebsarten und andere Anlagen wurden nur mit simulierten Geräten geprüft.
