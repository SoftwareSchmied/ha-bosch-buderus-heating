# WE800i: Prüfanleitung / Validation guide

## Deutsch

Vielen Dank für die bereits bereitgestellte Diagnose und die Angaben zum WE800i.

Der Melder von [Issue #60](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/60)
hat inzwischen Salzfüllstand, verfügbare Weichwassermenge, Durchfluss,
Regenerationsaktivität und Restdauer bis zur nächsten Regeneration mit MyBuderus
verglichen und bestätigt. Die App zeigt ebenfalls keine Phase oder genauen
Zeitstempel der letzten beziehungsweise nächsten Regeneration. Nach dem
Update auf 0.8.3 hat der Melder am 2026-11-04 bestätigt, dass die Salzwarnung
verfügbar ist und den Ersatz-Schwellenwert nutzt. Die Steuerungen (Regeneration
starten, Urlaubsmodus) sind standardmäßig deaktiviert und noch nicht geprüft.

### Anzeigen vergleichen

1. **v0.8.3** in HACS installieren und Home
   Assistant neu starten. Die tatsächlich installierte Version notieren.
   Die Enthärterunterstützung und die Phasenkorrektur sind in dieser Version enthalten.
2. Beim richtigen Gateway die elf Ressourcen aus der Tabelle unten und die
   zusätzliche Anzeige „Salzvorrat niedrig“ prüfen. Auch deaktivierte Entitäten
   ansehen. Bitte je Anzeige unterscheiden: vorhanden, deaktiviert, unbekannt,
   nicht verfügbar oder ganz fehlend.
3. HA und MyBuderus möglichst zeitnah vergleichen. Bei Unterschieden beide
   Anzeigen einschließlich Einheit und Uhrzeit notieren oder Screenshots
   bereitstellen. Die App kann beispielsweise Minuten anzeigen, während HA
   dieselbe Restzeit in Sekunden angibt. Eine reguläre Regeneration kann für
   die Beobachtung von Phase, Fortschritt und Restzeit abgewartet werden.
4. Danach unter **Einstellungen > Geräte & Dienste > Bosch/Buderus Home Comfort >
   Dreipunktmenü > Diagnose herunterladen** einen frischen Export erstellen.
   Die JSON-Datei vor dem Teilen auf persönliche Angaben prüfen und zusammen
   mit der Version und den beobachteten Unterschieden an Issue #60 anhängen.

Die abgeleitete Salzwarnung vergleicht die Ganzzahlanteile von
Füllstand und Schwelle desselben Kreises. Bei 20,9 Prozent und einer Schwelle
von 20 Prozent ist sie bereits aktiv. Ohne Schwellenressource gilt die
Ersatzschwelle von 20 Prozent. Ungültige oder veraltete bekannte Quellen
bleiben unbekannt. Für diese Prüfung muss kein Salz entnommen und keine
Schwelle geändert werden. W006 allein bestätigt diese Salzwarnung nicht.

### Nur bei gesondert vereinbarten Schreibtests

Die vorbereiteten Bedienelemente bleiben standardmäßig deaktiviert. Ein späterer
Schreibtest dokumentiert für jede einzelne Funktion: Ausgangswert, gewählten
Wert, Ergebnis der Rückleseprüfung, Anzeige in MyBuderus und gegebenenfalls die
Wiederherstellung des Ausgangswertes. Das betrifft Salz-Warnschwelle,
Urlaubsmodus und geplante Regeneration.

„Regeneration starten“ löst einen realen Geräteablauf aus. Eine Wiederherstellung
des ursprünglichen Zustands darf hierfür nicht versprochen werden. Der Start
wird nur gesondert vereinbart; diese Anleitung fordert ihn nicht an.

## English

Thank you for the diagnostics and the details about the WE800i.

The reporter in [issue #60](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/60)
has now confirmed that salt level, available soft water, flow, regeneration
activity and time until the next regeneration match MyBuderus. The app also
does not show a phase or exact last/next regeneration timestamps. After the
update to 0.8.3, the reporter confirmed on 2026-11-04 that the low-salt
indicator is available and uses the fallback threshold. The controls (start
regeneration, holiday mode) are disabled by default and not checked yet.

### Compare the readings

1. Install **v0.8.3** through HACS and restart Home Assistant. Record the installed
   version. This release fixes startup on HA 2026.9.2 and includes the existing
   low-salt fallback and phase correction.
2. On the correct gateway, check the eleven resources below and the additional
   “Low salt” indicator. Include disabled entities. Please distinguish between
   present, disabled, unknown, unavailable and missing.
3. Compare HA and MyBuderus at approximately the same time. For differences,
   note both readings, units and the time, or provide screenshots. A remaining
   duration displayed in minutes by the app may represent the same value as
   HA seconds. The next regular regeneration can be used to observe phase,
   progress and remaining time.
4. Download fresh diagnostics through **Settings > Devices & services >
   Bosch/Buderus Home Comfort > three-dot menu > Download diagnostics**. Check the
   JSON file for personal information before sharing it, then attach it to
   issue #60 with the version and observed differences.

The derived low-salt indicator compares the current level and threshold from
the same circuit. It is active when the level is at or below the threshold.
There is no need to remove salt or change the threshold for this check.
Missing inputs remain unknown; W006 alone does not establish a low-salt state.

### Only for separately agreed write tests

Prepared controls stay disabled by default. Any later write test should record
the original value, chosen value, read-back result, MyBuderus display and, where
applicable, restoration of the original value for each function. This applies
to the salt threshold, holiday mode and scheduled regeneration.

“Start regeneration” initiates a real appliance process. Restoration of the
previous state cannot be promised. Starting it requires a separately agreed
test; this guide does not request a regeneration start.

## Vergleichsliste / Comparison list

The paths below are relative to the actual `/swCircuits/{sw}/` circuit.
Die Pfade beziehen sich auf den tatsächlich vorhandenen Kreis `/swCircuits/{sw}/`.

| Ressource / Resource | Anzeige / Display | Vergleich / Comparison |
|---|---|---|
| `sensor/saltLevel` | Salzfüllstand / Salt level | Prozent / Percent |
| `sensor/saltLevelAlarm` | Salz-Warnschwelle / Salt alert threshold | Prozent / Percent |
| `sensor/availableWater` | Verfügbares Weichwasser / Available soft water | Liter oder m³; verbleibender Vorrat / Litres or m³; remaining volume |
| `sensor/waterFlow` | Wasserdurchfluss / Water flow | Liter pro Minute / Litres per minute |
| `regeneration/enabled` | Regeneration aktiv / Regeneration active | Aktiver oder inaktiver Zustand / Active or inactive |
| `regeneration/phase` | Regenerationsphase / Regeneration phase | Bekannte Phase, nicht mit aktiv/inaktiv gleichsetzen / Known phase, distinct from active/inactive |
| `regeneration/totalProgressStatus` | Regenerationsfortschritt / Regeneration progress | Prozent / Percent |
| `regeneration/totalRemainingTime` | Verbleibende Regenerationszeit / Regeneration remaining time | Dauer mit Einheit; ohne Einheit Sekunden / Duration and unit; seconds if omitted |
| `regeneration/timeUntilNext` | Zeit bis zur nächsten Regeneration / Time until next regeneration | Dauer mit Einheit; ohne Einheit Tage / Duration and unit; days if omitted |
| `regeneration/singleRegAppoint` | Geplante Regeneration / Scheduled regeneration | Zeitabstand, keine Uhrzeit; ohne Einheit Tage / Offset, not a time of day; days if omitted |
| `holidayMode/enabled` | Urlaubsmodus / Holiday mode | Aktiv oder inaktiv / Active or inactive |
| Aus beiden Salzressourcen abgeleitet / Derived from both salt resources | Salzvorrat niedrig / Low salt | Füllstand ≤ Schwelle / Level ≤ threshold |

## Offener Verbrauchsumfang / Outstanding consumption scope

`availableWater` liefert die noch verfügbare Weichwassermenge. `waterFlow`
liefert den aktuellen Durchfluss. Daraus berechne ich in diesem Paket weder
einen Tagesverbrauch noch einen Gesamtverbrauch. Verbrauchsauswertungen
stammen aus `/recordings`; diesen Ausbau habe ich mit P15 abgewählt.
Eine erfolgreiche Prüfung der Anzeigen oder der Phasenkorrektur löst daher
nicht automatisch den gesamten Verbrauchswunsch aus Issue #60.

`availableWater` reports the remaining soft-water volume; `waterFlow` reports
the current flow. This package calculates neither daily nor total consumption
from them. Consumption reporting comes from `/recordings`; that work was
cancelled with P15. Successful reading or phase checks do not automatically
resolve the complete consumption request in issue #60.
