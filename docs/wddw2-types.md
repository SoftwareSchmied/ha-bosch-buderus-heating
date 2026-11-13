# P01: WDDW2-Typen

## Deutsch

Ich habe die Befunde B01 und B02 im Code korrigiert.
Die Änderung ist zusammen mit den Geräteerweiterungen aus PR #76, #77
und #78 in v0.8.2-beta.5 enthalten.

### Sicherheitstemperatur

`/dhwCircuits/dhw1/safetyTemperature` liefert einen Textwert. Sein Code `on`
oder `off` meldet, ob die Sicherheitstemperatur aktiv ist. Der eigentliche
Temperaturwert wird
an anderer Stelle ermittelt. Der Ressourcenname allein rechtfertigt daher
keine Temperaturanzeige.

Ich zeige die bekannten Zustände `on` und `off` als Binärsensor
„Sicherheitstemperatur aktiv“. Unbekannte Zustände bleiben unbekannt.
Wertelisten, Referenzen, Objekte, andere Ressourcentypen und physikalische
Einheiten werden für diesen Vertrag abgewiesen. Es entsteht kein
Schreibzugriff. Ein bereits entdeckter Pfad bleibt nach fehlerhaften Antworten
in der regulären Abfragegruppe und kann sich ohne Neustart erholen.

### Startzähler

Der WDDW2-Startzähler liefert einen einfachen Zahlenwert. Am
Wärmeerzeugerpfad wird dagegen `emonValue` mit Teilwerten verwendet. Ich
unterstütze beide Formen getrennt.

| Datenquelle | Beispiel für die eindeutige Identität |
|---|---|
| Allgemeiner Skalar | `gateway:heatSources:numberOfStarts` |
| Skalar eines Wärmeerzeugers | `gateway:heatSources:hs1:numberOfStarts` |
| Strukturiertes Gesamtfeld | `gateway:heatSources:hs1:numberOfStarts:total` |
| Strukturierter Heizungsanteil | `gateway:heatSources:hs1:numberOfStarts:ch` |

Die Identitäten verwenden ausschließlich tatsächlich gelesene oder bereits
registrierte Pfade. Gleiche Zahlen werden nicht als gleichbedeutende Quellen
behandelt. Der Skalar erbt keine Statistik eines Teilzählers. Registrierung,
eigene Namen und Deaktivierungen bleiben bei einem Formatwechsel erhalten.
Nach einem Neustart werden die registrierten Formen unabhängig wiederhergestellt;
ein Platzhalter enthält keinen Messwert.

Die bestehende Prüfung endlicher, nicht negativer, ganzzahliger Werte gilt
auch für den neuen Skalar. Fehlerkennwerte, falsche Einheiten und gemischte
skalare/strukturierte Antworten ergeben keinen gültigen Zählerstand.
Die [Zählerdokumentation](start-counters.de.md) beschreibt die gemeinsamen Regeln.

### Prüfungen und Grenzen

Die [Fixtures](../tests/fixtures/wddw2/synthetic-readings.json) sind ausdrücklich
synthetisch. Die Beispielwerte und Antwortdaten stammen nicht aus einer realen
Anlage.
Die [Regressionstests](../tests/test_wddw2_types.py) prüfen Zustände,
fehlerhafte Antworten, getrennte Identitäten und Wiederherstellung.
Die erweiterten [Zählertests](../tests/test_start_counters.py) verwenden außerdem
Home-Assistant-Einrichtung, erneutes Laden und eine Recorder-Datenbank.

Diagnose-Schema 17 ergänzt die gemeinsame Prüfung des Sicherheitszustands.
Es enthält feste Ablehnungsgründe und Angaben zur Gültigkeit, aber weder
den aktuellen Zustand noch unbekannte Zustandstexte. Die Startzählerdiagnose
unterscheidet weiterhin Skalar und Teilwerte ohne deren Messwerte.

Ich habe keine realen Geräte-Schreibtests durchgeführt. Die Bestätigung der
neuen Anzeigen auf einer WDDW2-Anlage steht aus. Discovery-Grenzen, reguläre
Abfrageintervalle und vorhandene Schreibregeln bleiben erhalten.

### Lokale Prüfung am 23.10.2026

Ich habe beide Ausgangsfehler zunächst mit zwei fehlschlagenden
Regressionstests reproduziert. Nach der Korrektur bestehen 388 gezielte
Testfälle. Die vollständige Suite besteht mit 1.721 Fällen und 96,70 %
kombinierter Code- und Verzweigungsabdeckung unter Python 3.14.2 und
Home Assistant 2026.10.1. Die Zahl enthält parametrisierte Testvarianten.
Ruff, Formatprüfung und striktes Mypy bestehen ebenfalls.

Die vollständige Prüfung umfasst reproduzierbaren Paketbau und Import aus
einer isolierten ZIP-Installation. Für die Linux-Prüfung habe ich die
Git-Verzeichnisse des Windows-Worktrees ausdrücklich zugeordnet.
Diese Ergebnisse beschreiben den lokalen Paketabschluss. Die gemeinsame
Beta-Prüfung ist im [Release-Bericht](release.md) dokumentiert.

## English

I corrected findings B01 and B02 together with the device extensions from
#76, #77 and #78. The combined changes are included in v0.8.2-beta.5.

The DHW safety-temperature resource reports a text value. Its `on` or `off`
code indicates whether the safety temperature is active. The actual
temperature comes from a separate calculation. I therefore expose the known `on/off` codes as the
read-only binary sensor “Safety temperature active”. Unknown states remain
unknown. References, value arrays, objects, other resource types and physical
units are rejected. Discovered paths retain regular polling after malformed
responses and can recover without a restart.

The WDDW2 per-source counter is a plain number, while the corresponding
heat-source path uses `emonValue` parts. I accept both formats with
separate identities, as shown above. A source scalar never inherits the
multipart total's statistics. Existing IDs, user names and disabled entities
are preserved. Both registered forms can be restored after a restart; only
the form matching the current response produces a value.

The same finite, non-negative, whole-number validation, error markers and
dimensionless-unit checks apply to the new scalar. Mixed scalar/structured
responses do not produce valid counts. See the
[start-counter contract](start-counters.md) for the shared rules.

The linked fixtures explicitly identify their synthetic provenance. Regression
tests cover states, invalid payloads, identities and recovery. Extended
counter tests also exercise Home Assistant setup, reload and recorder statistics.
Diagnostics schema 17 reports fixed safety-state validation reasons without
the state or unknown text. Counter diagnostics retain separate scalar/part
results without counts.

I have not performed physical-device write tests. Confirmation of these new
readings on a WDDW2 installation remains outstanding. Discovery limits,
regular polling intervals and existing write policies are preserved.

Local validation on 23 October 2026: both initial defects were reproduced by
two failing regression tests. After the correction, 388 targeted cases and
the full suite of 1,721 cases pass, with 96.70% combined line and branch
coverage on Python 3.14.2 and Home Assistant 2026.10.1. Counts include
parameterized cases. Ruff, formatting and strict Mypy pass. The full suite
also checks reproducible packaging and an isolated ZIP import. Linux tests
use explicit Git directory mappings for the Windows worktree. These results
describe the local package checks. The combined beta verification is documented
in the [release report](release.md).
