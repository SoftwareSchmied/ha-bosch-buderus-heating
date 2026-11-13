# Conversion audit, P16

I completed this bounded review of the local implementation through P14 on
24 October 2026. The base commit is
`c6f298705aeab609f7049973b4869a590f8dad71`. At that checkpoint, the manifest still said
`0.8.2-beta.5`. I included the implementation and subsequent review fixes in
v0.8.2-beta.6. The original checkpoint figures below remain historical.

## Results

I reviewed all 118 original rules and all 250 outstanding numeric candidates.
The current inventory contains 132 rules. Of the candidates, 115 reuse known
rules, 82 concern display geometry, 13 concern technical control flow and 40
contribute to additional domain rules. These counts are not counts of defects.
U119 also retains the charging-duration contract established in P13.

| Current rule outcome | Count |
|---|---:|
| Correct | 9 |
| Already corrected by earlier packages | 6 |
| Corrected in P16 | 8 |
| Intentional display or validation difference | 49 |
| Outside the implemented scope | 56 |
| Evidence incomplete, outside the implemented scope | 3 |
| Code correct, earlier report corrected | 1 |

I assigned every rule its current rationale, code and tests and classified
every candidate. A referenced boundary test is not a test of an unimplemented
formula. The original review of published Beta.5 remains a separate
historical record.

I subsequently corrected U100 to compare the integer parts of the salt level
and threshold and to use the default for an absent threshold. It now counts as
correct rather than a validation difference. The original P16 validation
figures below remain historical.
The [follow-up corrections](corrected-values.md) also cover receive
formats, partial reports, sign-in isolation and the identified write edge cases.

## Corrections in the implemented scope

- I aligned reviewed unit spellings across catalog admission, sensor
  interpretation and numeric controls. `degC` and `degF` identify temperature;
  reviewed duration spellings identify their native seconds, minutes, hours
  or days. Matching spelling does not turn Fahrenheit into Celsius.
- I interpret scalar energy units explicitly: `Wh`/`wh`, `kWh`/`KWh` and
  `kBtu`. Since HA has no native kBtu energy unit, I convert it once using
  `1 kBtu = 0.29307107 kWh`. Native scalar Wh readings retain Wh.
- I normalize multipart `emonValue` readings with the collection's unit:
  `1000 Wh` becomes `1 kWh`, while `1 kWh` stays `1 kWh`. Unknown explicit
  units, invalid numbers, reported error values and contradictory duplicate
  parts remain unknown. A valid sibling remains available. The established
  legacy contract for a collection with no unit remains kWh and is identified
  explicitly in diagnostics. It is not a general missing-unit fallback.
- I evaluate the current metadata after every update. Scaling and labels
  change together; invalid metadata makes affected readings unknown and a
  valid subsequent response recovers the same entity. The dew-point
  calculation converts the current temperature to Celsius before evaluating
  the formula and requires percent humidity.
- I classify tariff setpoint deltas as temperature differences. With an HA
  display-unit override, a difference of `2 °C` becomes `3.6 °F`, without an
  absolute-temperature offset. Existing DE/EN names already describe the
  same difference and need no wording change.

Native numeric controls keep their reported value, limits and step. Their
accepted write and read-back remain in the same device unit. Existing BACON
temperature conversion and step conversion are covered by their family tests.
No new device command, discovery expansion or polling interval was added.
Entity identifiers and the choice of one device per heating gateway remain.

The `emonValue` evidence has one unit for the measurement model. It does not
justify arbitrary units inside each part. A part carrying its own `unit` or
`unitOfMeasure` is rejected until that separate contract is established.
Mixed value/list containers and reference collections are also rejected.

Diagnostics schema **27** reports the energy unit source, conversion factor,
valid-part count and bounded rejection reasons. It contains no readings,
dynamic part names or unknown unit strings and causes no extra requests.
Previously stored statistics are not rewritten. A corrected scale or unit
can require a review of earlier statistics in Home Assistant.

## Report correction and remaining evidence

I corrected U085 in the current report: the **first** wallbox meter component
is divided by 1000 for kWh; the **last** component is already kW. P13 already
implements that order correctly. Its energy reset period remains unconfirmed,
so the wallbox energy sensor still has no state class.

U076 needs the tariff's external unit, U084 needs the charging log's unit and
time basis, and U132 needs the source composition and time basis of aggregated
efficiency. These functions are not implemented. Matching device responses,
firmware context and units would be required before adding them.

I cancelled P15 by choice: consumption history and regeneration logs are not
part of this work. Current available soft water remains a direct stock reading,
not consumption calculated from flow. Issue #60 still needs the reporter's
device feedback. Other family-specific exclusions remain as documented in
their package reports.

## Language and measurement semantics

| Meaning | Deutsch | English | Unit / basis |
|---|---|---|---|
| High-price temperature difference | Sollwertänderung bei hohem Preis | High-price setpoint delta | temperature difference |
| Low-price temperature difference | Sollwertänderung bei niedrigem Preis | Low-price setpoint delta | temperature difference |
| Available soft water | Verfügbares Weichwasser | Available soft water | direct remaining stock, l or m³ |
| Water flow | Wasserdurchfluss | Water flow | flow rate, l/min |
| Operating duration | Betriebsstunden | Operating hours | h, including fractions |
| Wallbox energy / power | Energie / Leistung | Energy / Power | kWh / kW; reset period unconfirmed |

These meanings stay consistent between DE and EN. UI rounding, unit
preferences and the spelling of a native unit do not redefine the measurement.

## Validation and limits

**2,971 automated cases passed**, including parameterized variants and **42
additional cases**, with **96.85% combined statement and branch coverage**.
Ruff, formatting for 202 files and strict Mypy for 79 source files passed.
The suite includes packaging and isolated-installation checks. New cases
exercise collection-unit conversion, invalid/error values, duplicate parts,
metadata changes, recovery, real HA unit conversion and simulated native
writes. Existing family suites cover limits, steps and reported-state
confirmation. Eleven before/after probes also compare the earlier helpers
with the corrected behavior. All added device inputs are synthetic.

I did not install this stand on a real device, perform physical writes,
publish a release or run remote PR checks. The review covers the supported
contracts. It is not proof of every native, dynamic or server-side
conversion, nor of every device or firmware.

## Deutsch

Ich habe alle 118 Ausgangsregeln und 250 offenen Kandidaten geprüft. Der
aktuelle Bestand enthält 132 Regeln mit Begründung, Code- und Testzuordnung.
Ich habe Einheiten-Aliase, die Energieumrechnung strukturierter Werte, die
Auswertung geänderter Einheiten und Temperaturdifferenzen korrigiert. Eine
Differenz von 2 °C ergibt bei einer Fahrenheit-Anzeige 3,6 °F. Strukturierte
1000 Wh ergeben 1 kWh. Ungültige Werte und unbelegte Einheiten bleiben
unbekannt. Diagnose-Schema 27 erklärt die Entscheidung ohne Anlagenwerte.

Beim Wallbox-Messwert war die frühere Beschreibung vertauscht; der Code aus
P13 war bereits korrekt. Gerätebestätigung und Energie-Zeitraum bleiben dort
offen. P15 habe ich abgewählt. Die vorhandene Weichwassermenge ist weiterhin
ein Vorrat, keine Verbrauchshistorie.

2.971 automatisierte Fälle bestehen bei 96,85 % kombinierter Abdeckung.
Die 42 zusätzlichen Fälle verwenden synthetische Eingaben. Ruff,
Formatprüfung und striktes Mypy bestehen. Alte Statistikwerte werden nicht
automatisch korrigiert. Veröffentlichung und reale Anlagenprüfungen sind
nicht Teil dieses Abschlusses.
