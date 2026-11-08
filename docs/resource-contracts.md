# P03: Discovery and read contracts

I reconciled all 361 resource models against the previous 359-entry
inventory. The two added models are generic execution models with a
caller-supplied path. The inventory preserves every model, 52 groups of
duplicate paths and the reason for each of 137 path changes: 122 explicit
arguments or fixed identifiers, six restored arguments, eight explicit query
arguments and one corrected path composition. The RRC weekly DHW program path
is `/dhwCircuits/dhw{circuitId}/programs/pg{programId}/week`.

The [machine-readable inventory](resource-inventory.json) records each
model's transport, path, shape, unit policy, scaling, options, bounds, write
requirements, time basis, evidence and outstanding data. Its schema probes
are intentionally separate from semantic examples and physical confirmation.
A supported probe establishes acceptance of that schema, not compatibility
with every installation. Query paths remain outside ordinary resource reads.

## Duration corrections

I reproduced incorrect readings for minutes, singular `hour`, unitless WDDW2
operation hours and metadata changes after entity creation. The catalog,
sensor and diagnostics now use one duration assessment.

| Input | Home Assistant result |
|---|---|
| 7,200 `s`, `sec`, `secs` or `seconds` | 2 h |
| 120 `m`, `min`, `mins` or `minutes` | 2 h |
| 2 `h`, `hour` or `hours` | 2 h |
| WDDW2 `operationHours`, 2 with no unit | 2 h under the reviewed hours contract |
| Structured `workingTime`, 7,200 with no unit | 2 h under the existing seconds contract |
| Global `workingTime/totalSystem` with no unit | Unknown until valid metadata arrives |
| Unknown unit, invalid shape or invalid number | Unknown, with a diagnostic reason |

The most recent response controls the conversion. A switch from seconds to
hours does not retain the old scale. Finite, nonnegative values within the
exact JSON integer range are accepted. Device error markers, malformed error
metadata, conflicting parts and mixed envelopes do not become measurements.
A bad part does not invalidate a valid sibling. Zero remains a valid value.
Successful regular polling can create a previously ineligible sensor and
restore an existing one with its identity preserved. Stale readings are
unavailable. These known runtime paths stay in their regular slow poll group.

Existing recorded statistics are not rewritten. A corrected scale may produce
a discontinuity in older histories; historical cleanup needs separate review
of the affected entity and period.

## DHW monitor collection

`/dhwCircuits/dhw1/monitorValues` is a collection of embedded resource
references. It must not create a standalone numeric sensor, including when a
malformed response happens to contain a scalar. The parser retains child IDs
and discovery reads advertised children under the existing allowed root.
A successful direct child response supplies its current value. A missing
child does not receive a guessed value from the collection.

The synthetic example records the embedded shape, but I have no physical
response confirming its complete envelope, per-child errors, duplicate
handling or whether every child also supports direct reads. Embedded-value
fallback remains an explicit evidence gap. P03 does not claim complete WDDW2
monitoring support.

## Discovery and repeatable evidence

The five family folders under [tests/fixtures/contracts](../tests/fixtures/contracts/README.md)
contain small semantic examples. Tests pass their payloads through the real
parser and sensor. They cover durations, energy, starts, volume, flow and
known regeneration phases. The examples are synthetic and make no claim of
physical-device verification.

Existing `test_heating_circuit_discovery.py` and `test_discovery_failures.py`
cover complete references, unreadable HC2 directories with 403/404/406,
catalog-only leaves, multiple reported circuits, absent optional resources,
reference priority, authentication and transport failures, rate limits and
the 30-path batch, 512-path total and eight-level bounds. P03 adds a collection
replay with successful and missing children, and regular-polling duration
creation, invalidation and recovery. No new roots or guessed circuit IDs are
introduced.

Diagnostics schema 18 adds only duration and collection decisions. It exports
no current readings or unknown unit text and makes no additional cloud calls.
The anonymized inventory accepts schema 18 while retaining its own reduced
field allowlist.

## Evidence needed for following packages

P05 now adds four scoped zone controls. Its command contracts, remaining
temporary-override gap and outstanding physical checks are described in
[zone-controls.md](zone-controls.md). The inventory retains the original
P03 read probes alongside the new P05 control assessment.

| Package or family | Needed before extending support |
|---|---|
| P05 zones and climate | Advertised zone hierarchy, current types and units, supported modes, bounds, rights and individual write/read-back confirmation |
| P06 RRC | Actual adapter envelope and argument values, program and zone shapes, units, capabilities and error responses |
| P07 programs | Full anonymized schedule and holiday structures, supported commands, conflict and read-back behavior |
| WDDW2 monitor collection | Full response envelope, child errors and duplicates, direct child-read availability |
| WE800i | Reporter comparison of readings and units, followed separately by permitted control/read-back tests as described in the validation guide |
| P15 recordings | Query interval, timestamp basis, cumulative versus interval semantics, gaps and unit conversions |

Each unresolved inventory record names a follow-up package and its missing
data. A family example is reusable test input, not proof for every model in
that family. No physical installation or write test was performed for P03.

Validation: **1,835 automated cases passed**, including parameterized variants,
with **96.71% combined statement and branch coverage**. Ruff, formatting and
strict Mypy passed. The suite includes reproducible packaging and an isolated
ZIP import. These checks used synthetic data and no real device writes.

## P16 follow-up

I completed a later [conversion audit](conversion-audit.md) with shared unit
aliases and validated multipart energy interpretation. Its schema 27 and
test results supersede the current-state interpretation of this P03 report;
the original P03 evidence and results below remain historical records.

## Deutsch

Ich habe alle 361 Modellzeilen zugeordnet und die Abweichungen zum bisherigen
Inventar einzeln dokumentiert. Die zwei zusätzlichen Einträge sind allgemeine
Befehlsmodelle mit einem übergebenen Pfad. 52 Gruppen gleicher Pfade bleiben
ihren ursprünglichen Modellen zugeordnet. Die Modellzahl sagt nichts über die
Anzahl tatsächlich verfügbarer Entitäten aus.

Ich habe die Umrechnung von Laufzeiten korrigiert. Sekunden, Minuten und
Stunden werden bei jeder Antwort anhand der aktuellen Einheit in Stunden
umgerechnet. Ohne Einheit gelten nur die belegten Ausnahmen: Stunden für
`operationHours` und der bestehende Sekundenvertrag für strukturierte
`workingTime`-Werte. Beim globalen Laufzeitzähler bleibt eine fehlende Einheit
unbekannt. Fehlerhafte Angaben bleiben unbekannt und können sich über die
regulären Abfragen erholen. Veraltete Werte sind nicht verfügbar. Bestehende
Entitätskennungen bleiben erhalten. Frühere Statistikwerte werden nicht
automatisch berichtigt; eine korrigierte Skalierung kann dort als Sprung
sichtbar werden.

Die Warmwasser-Sammelressource erzeugt keinen eigenen Zahlenwert. Ihre
gemeldeten Unterressourcen werden weiter gezielt gelesen. Für eine Übernahme
eingebetteter Werte fehlen noch vollständige Geräteantworten einschließlich
Fehlerfällen und der Bestätigung, welche Einzelabrufe unterstützt werden.

Die wiederverwendbaren Beispiele für fünf Gerätefamilien sind ausdrücklich
synthetisch. Katalog, Sensor und Diagnose teilen die Laufzeitprüfung.
Diagnose-Schema 18 erklärt Einheit, Umrechnung und Ablehnungsgrund ohne
Messwerte, unbekannte Einheitentexte oder zusätzliche Cloud-Abfragen.
Discovery-Grenzen und Abfrageintervalle bleiben erhalten. Der Datenbedarf für
Zonen, RRC, Programme, WE800i, WDDW2 und Aufzeichnungen steht je Modell im
Inventar. Reale Anlagenprüfungen stehen weiterhin aus.

**1.835 automatisierte Testfälle** einschließlich parametrisierter Varianten
bestehen bei **96,71 % kombinierter Code- und Verzweigungsabdeckung**. Ruff,
Formatprüfung und striktes Mypy bestehen. Paketbau und isolierter ZIP-Import
sind in der Suite enthalten. Ich habe keine realen Schreibtests durchgeführt.
