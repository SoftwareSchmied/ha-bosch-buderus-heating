# Water softeners / Wasserenthärter

## Deutsch

Ich ergänze mit v0.8.2-beta.5 die Unterstützung für den Buderus
Logawater Soft WE800i aus [Issue #60](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/60).
Grundlage sind v0.8.2-beta.4 und der dort bereitgestellte Diagnoseexport. Die
Diagnose bestätigt die erfolgreiche
Anmeldung und das erkannte Modell `watersoftener`, enthält aber noch keine
Enthärter-Messwerte. Die unten beschriebenen Funktionen sind deshalb noch
nicht an einem echten WE800i bestätigt.

### Erkennung und Anzeigen

Ein ausdrücklich erkanntes Enthärtermodell erhält die dokumentierten
Abfragen unter `/swCircuits/sw1`. Die Hardwarekennung WX100 allein reicht
dafür nicht aus. Weitere Kreise kommen ausschließlich aus gemeldeten
Referenzen. Deren Pfade haben Vorrang vor ergänzenden Abfragen. Die bekannten
Blattressourcen werden auch bei einem unlesbaren übergeordneten Verzeichnis
geprüft. Die Grenzen von 30 Pfaden je Batch, 512 Pfaden und acht Ebenen gelten
weiterhin, ebenso die gemeinsame Abfrage und die Cloud-Sperrzeiten.

Alle Entitäten bleiben unter dem vorhandenen Gateway-Gerät. Es entsteht kein
zusätzliches Gerät pro Enthärterkreis. Bestehende Heizungsentitäten und ihre
Kennungen werden nicht umbenannt.

| Pfad unter `/swCircuits/{sw}/` | Anzeige | Einheit und Bedeutung |
|---|---|---|
| `sensor/saltLevel` | Salzfüllstand | Prozent, nur 0 bis 100 |
| `sensor/saltLevelAlarm` | Salz-Warnschwelle | Prozent, nur 0 bis 100 |
| beide Salzressourcen | Salzvorrat niedrig | Füllstand kleiner oder gleich der gemeldeten Schwelle |
| `sensor/availableWater` | Verfügbares Weichwasser | Liter oder gemeldete Kubikmeter; Vorrat, kein Verbrauchszähler |
| `sensor/waterFlow` | Wasserdurchfluss | Liter pro Minute |
| `regeneration/enabled` | Regeneration aktiv | Nur `on` und `off` werden zu einem Binärzustand |
| `regeneration/phase` | Regenerationsphase | Sieben bekannte Phasen, deutsch und englisch übersetzt |
| `regeneration/totalProgressStatus` | Regenerationsfortschritt | Prozent, nur 0 bis 100 |
| `regeneration/totalRemainingTime` | Verbleibende Regenerationszeit | Gemeldete Zeiteinheit; ohne Einheit Sekunden |
| `regeneration/timeUntilNext` | Zeit bis zur nächsten Regeneration | Gemeldete Zeiteinheit; ohne Einheit Tage |
| `regeneration/singleRegAppoint` | Geplante Regeneration | Gemeldete Zeiteinheit; ohne Einheit Tage |
| `holidayMode/enabled` | Urlaubsmodus | Eigener Zustand des Enthärters |

Fehlt bei Salzfüllstand, Schwelle, Fortschritt, Wasservorrat oder Durchfluss
die Einheit, verwende ich die jeweilige oben genannte Zuordnung. Eine
widersprüchliche ausdrücklich gemeldete Einheit wird abgewiesen.
Fehlende, ungültige und nicht endliche Werte ergeben keinen erfundenen
Nullwert. Eine ausgefallene Quelle wird nicht durch eine andere ersetzt.

Die Salzwarnung verwendet die Ganzzahlanteile von Füllstand und
Schwelle desselben Kreises nach deren Float-Interpretation. Bei 20,9 Prozent
Füllstand und 20 Prozent Schwelle ist sie deshalb bereits aktiv. Fehlt die
Schwellenressource, verwende ich den Ersatzwert von 20 Prozent. Eine
bekannte, aber ungültige, ausgefallene oder veraltete Quelle bleibt unbekannt.
Der gemessene Füllstand behält seine Nachkommastellen. W006 bleibt ohne
zusätzliche Bedeutungszuordnung.
Der separate Code E7 ist kein Beleg für die Bedeutung von W006 und ersetzt
nicht die numerische Salzwarnung.

### Regenerationsphasen in P04

Ich vereinheitliche die Groß-/Kleinschreibung der sieben bekannten Codes
`normal`, `refill`, `brine_preparation`, `brine_draw`, `backwash`, `rinse`
und `end`. Beispielsweise werden `BACKWASH` und `Backwash` als `backwash`
erkannt und weiterhin als „Rückspülen“ angezeigt. Die vorhandenen Enum-Schlüssel,
Übersetzungen und Entitäts-IDs bleiben erhalten.

Die Toleranz gilt ausschließlich für die Schreibweise dieser ASCII-Codes und
erst nach der Prüfung der skalaren Datenform. Zusätzliche Leerzeichen, andere
Trennzeichen und unbekannte Texte werden nicht zu einer Phase umgedeutet.
Fehlende Werte, Objekte, Listen und Referenzen bleiben ungültig. Eine später
wieder gültige Antwort aktualisiert dieselbe Entität im regulären Polling.
Die ursprüngliche API-Antwort bleibt unverändert. Für `on`/`off`-Zustände und
Schreibbefehle gilt weiterhin die bisherige Prüfung.

Die Phasenkorrektur aus P04 ist in v0.8.2-beta.5 enthalten. Eine andere Schreibweise wurde mit
synthetischen Antworten nachgestellt; in Issue #60 ist sie bislang nicht
als tatsächliche Geräteantwort belegt. Die Vorbereitung für den Praxistest
steht in der [WE800i-Prüfanleitung](water-softener-validation.md).

### Vorbereitete Bedienfunktionen

Die folgenden Bedienelemente sind standardmäßig deaktiviert. Für die
Freigabe steht noch die Bestätigung an einem echten Gerät aus. Sie erscheinen
nur bei passenden aktuellen Schreibrechten, Optionen und Grenzen.

| Bedienelement | Verhalten |
|---|---|
| Salz-Warnschwelle | Zahlenfeld mit den angebotenen Grenzen innerhalb von 0 bis 100 Prozent |
| Urlaubsmodus | Schalter für die angebotenen Codes `on` und `off` |
| Regeneration planen | Zahlenfeld in Tagen; Grenzen stammen vom Gerät |
| Regeneration starten | Eigene Taste für den belegten Befehl `on` |

Eine Schrittweite von 1 dient der Bedienung. Sie wird nicht zu einer
erfundenen Protokollgrenze. Gültige Zwischenwerte innerhalb der gemeldeten
Grenzen werden nicht aufgrund dieses Bedienhinweises abgewiesen.
Alle Schreibzugriffe verwenden die vorhandene gemeinsame Prüfung,
Serialisierung und Rücklesebestätigung. Ein Timeout führt zu keinem zweiten
Startbefehl. Eine Abbruchtaste für die Regeneration ist nicht freigegeben,
weil ein angebotener Code `off` allein keinen bestätigten Abbruchablauf belegt.

Ich prüfe für Anzeigen und Bedienelemente dieselbe skalare Datenform.
Referenzen, Wertelisten oder Objekte sperren die betreffenden Bedienelemente
und ihre Schreibzugriffe auch dann, wenn Schreibrechte und Grenzen passen.
Nach einer wieder gültigen Antwort werden dieselben Entitäten wieder bedienbar.

### Diagnose und noch nötige Bestätigung

Das aktuelle Diagnose-Schema 17 enthält die mit Schema 16 ergänzten Angaben
zum erkannten Geräteprofil und den zusätzlichen
Abfragen. Pro Enthärterressource zeigt es Schemafehler, Einheit und die
Gültigkeit des Wertes, aber keinen Messwert oder unbekannten Zustandstext.
Zusätzlich unterscheide ich registrierte, deaktivierte, nicht geladene,
unbekannte und nicht verfügbare Entitäten. Namen und Entitäts-IDs werden nicht
exportiert. Unbekannte dynamische Kreiskennungen werden redigiert. Der Export verursacht
keine zusätzlichen Cloud-Anfragen.

Für die Gerätebestätigung werden ein frischer Diagnoseexport aus diesem
Entwicklungsstand sowie ein Vergleich der verfügbaren Anzeigen mit MyBuderus
benötigt. Schreibtests sollen anschließend einzeln erfolgen, mit dokumentierter
Einstellung, Ergebnis in MyBuderus und gegebenenfalls wiederhergestelltem
Ausgangswert. Eine manuell gestartete Regeneration ist ein realer Geräteablauf.

Der in Issue #60 gewünschte Weichwasserverbrauch ist damit noch nicht
vollständig abgedeckt: Verbrauchswerte stammen aus `/recordings`.
Ich berechne keinen vermeintlichen Gesamtverbrauch aus einzelnen
Durchflussmessungen. Das gesonderte optionale Verbrauchspaket und historische
Messreihen sind nicht Teil dieser Umsetzung. Issue #60 bleibt bis zur
Rückmeldung und Klärung dieses Umfangs offen.

## English

I am adding an initial WE800i implementation based on v0.8.2-beta.4 and the
diagnostics in issue #60. The supplied diagnostics confirm gateway discovery,
but do not contain softener readings. Synthetic tests do not establish
physical-device compatibility.

Explicit softener models receive the documented `/swCircuits/sw1` probes.
Additional circuit IDs must be advertised. References retain priority; an
unreadable parent does not suppress known leaf probes. Shared polling,
backoff, the 30/512/8 discovery limits and one HA device per gateway remain.

Current capabilities cover salt level and threshold, derived low salt,
available soft-water volume, flow, regeneration state/phase/progress/time,
scheduled regeneration and holiday state. Unknown values remain unknown.
Available water is a volume-storage measurement, not cumulative consumption.
Unitless next/scheduled regeneration values are interpreted as days.
Unitless remaining regeneration time uses seconds.
The 20% alarm fallback is not assumed. W006 is not mapped to E7 or low salt.

In P04, I accept lower-, upper- and mixed-case spellings of the seven known
ASCII phase codes: `normal`, `refill`, `brine_preparation`, `brine_draw`,
`backwash`, `rinse` and `end`. For example, `BACKWASH` becomes the existing
`backwash` enum key. Translations and entity IDs stay unchanged.

Scalar-schema validation runs first. Missing values, objects, arrays and
references remain invalid. Extra whitespace, different separators, unknown
codes and non-ASCII lookalikes are not interpreted as known phases. A later
valid sample recovers on the same entity through regular polling. I preserve
the raw API response and the existing rules for on/off states and writes.
The P04 phase correction is included in v0.8.2-beta.5; issue #60 does not yet establish different
phase casing on a physical device. See the
[WE800i validation guide](water-softener-validation.md) for the pending checks.

Prepared controls cover the salt threshold, holiday switch, regeneration
delay in days and a separate regeneration start button. They are disabled by
default pending device verification. Current rights, options and bounds are
required; UI steps do not invent API restrictions. Writes use the shared
transaction and read-back. A timed-out command is not resent. Cancellation
is not offered without a verified command contract.

I use the same scalar-schema checks for readings and controls. References,
value arrays or objects block entity availability and writes even when the
advertised permissions and bounds fit. Existing controls recover when the
resource returns a valid scalar schema again.

The current diagnostics schema 17 retains the profiles, local entity registration
and capability validation introduced in schema 16, without
measurements, unknown texts, credentials or private dynamic IDs. It adds no
cloud calls. Actual display comparison, interruption/recovery checks and
individual write confirmation in MyBuderus remain outstanding. Consumption
from `/recordings` remains a separate optional package; issue #60 is not
considered closed by this initial implementation.

## Evidence references

### P04 validation, 23 October 2026 / Prüfung am 23.10.2026

I ran 1,767 automated test cases with 96.70% combined line and branch coverage.
The 40 additional cases cover all seven phases in three casing variants,
invalid values and schemas, entity recovery, and unchanged binary-state rules.
The full suite also passed the existing softener-write, packaging and isolated
ZIP-import checks. Ruff, formatting and strict Mypy passed. I rechecked the
13 softener reference cases, including the unchanged units. These are
synthetic tests; physical WE800i verification remains outstanding.

Ich habe 1.767 automatisierte Testfälle mit 96,70 % kombinierter Code- und
Verzweigungsabdeckung ausgeführt. 40 zusätzliche Fälle prüfen Schreibweisen,
ungültige Daten, die Erholung derselben Entität und unveränderte Binärzustände.
Die bisherigen Schreib- und Paketprüfungen, Ruff, Formatprüfung und striktes
Mypy bestanden ebenfalls. Die 13 Enthärter-Referenzfälle sind erneut
geprüft. Eine Bestätigung am realen WE800i steht weiterhin aus.

### Implementation notes / Umsetzungshinweise

Physical checks remain separate from these implementation notes.

- The sensor, regeneration and holiday resources under `/swCircuits/{sw}/`
  are handled as separate resource groups.
- Next and scheduled regeneration values are day offsets, not clock times.
  Seven named regeneration phases are known.
- Remaining regeneration time without a unit is interpreted as seconds.
- [Resource implementation inventory](resource-inventory.md).
