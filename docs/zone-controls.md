# ConnectKey zone controls

I added operating-mode and manual-temperature controls for each reported
ConnectKey zone, separately for heating and cooling. Four of the six reviewed
fields can now be controlled. The two temporary-temperature fields remain
readings because their schedule-dependent expiry and cancellation are not
fully implemented and verified.

## Supported commands

The prefix is `/zones/zone{zoneNumber}`. Zone identifiers come from discovery.
The following contracts use the existing PointT gateway resource endpoint.

| Path suffix | Request body | Current requirements |
|---|---|---|
| `/heating/operationMode` | `{"value":"manual"}` | Scalar `stringValue`, writable, known offered mode |
| `/cooling/operationMode` | `{"value":"manual"}` | Same contract, using this cooling resource's options |
| `/heating/manualRoomSetpoint` | `{"value":20.5}` | Scalar `floatValue`, writable, unit `C`, finite bounds |
| `/cooling/manualRoomSetpoint` | `{"value":24}` | Same contract, using this cooling resource's bounds |

The examples are synthetic. They are not defaults or instructions to apply
these temperatures. Only offered `off`, `manual` and `auto` options are
enabled, including valid subsets. Unknown options cannot be written. An
unknown or no longer advertised current mode temporarily blocks the control.

The manual value is stored for manual operation. Changing it does not switch
the zone to manual, change heating/cooling selection or alter another zone.
Selecting a mode sends only that mode command. I require each resource's
current permission and metadata rather than borrowing settings from a
heating circuit or another zone.

The numeric control uses the current `stepSize`, or a 0.5 °C UI increment
when it is absent. Invalid advertised steps block the control. This UI
increment does not establish an API quantization rule: a finite service
value within the current bounds can be sent without automatic rounding.
Error markers, invalid samples, compound responses, missing bounds and
unexpected units block writes. The narrow reviewed unit is `C`; no
conversion from an unverified zone wire unit is inferred.

## Confirmation and entity lifecycle

Each transaction sends at most one PUT and then reads the same resource.
A lost PUT response triggers read-back, never an automatic second PUT.
The existing bounded read-back attempts allow a delayed result. Matching
numbers in the wrong zone, wrong unit or an invalid response do not confirm
success. A valid confirmation remains useful if the device simultaneously
removes write permission; the control then becomes unavailable.

Transactions share the coordinator lock. A queued operation rechecks the
latest resource after the preceding confirmation. Authentication and rate
limits follow the existing integration behavior.

Regular polling can add a control when valid metadata arrives later.
Invalid or stale responses disable its operation. A successful update
restores the same entity. New controls have their own `:control` identity
and names such as “Heating: Set manual temperature”. Existing reading
identities, names and default activation remain unchanged. User-defined
names and disabled states are not migrated or overwritten.

Diagnostics schema 18 uses the existing control section to show eligibility,
known options, bounds, UI step and rejection reasons. Temporary overrides
report `zone_override_lifecycle_unverified`. Diagnostics add no cloud
requests and expose no readings or unknown mode text.

## Boundaries and evidence

Mode and temperature selection each use a single-value PUT. The temporary
setter also uses a single float, but its presentation additionally depends on
the active thermal mode, schedule and current switch point. A numeric command alone does not
prove complete override behavior, so no temporary control is released.

RRC paths under `/zones/zn{id}`, child locks, pairing, weekly programs and a
combined climate entity are outside this package. Discovery roots, identifier
selection, limits and polling intervals are unchanged.

The [synthetic fixture](../tests/fixtures/contracts/connectkey/zone-controls.json)
describes two zones with different heating/cooling options, bounds and steps.
[Tests](../tests/test_zone_controls.py) cover individual commands, metadata
changes, invalid responses, delayed confirmations, concurrency, recovery
and German/English names and native HA option translations.

Physical-device confirmation is still outstanding. A future permitted test
should compare each reported zone with MyBuderus, record the original mode and
setpoint, verify each selected command in that same zone and restore the
original settings. I performed no installation or physical writes for P05.

Validation: **1,902 automated cases passed**, including parameterized variants,
with **96.72% combined statement and branch coverage**. Ruff, formatting and
strict Mypy passed. The suite includes reproducible packaging and isolated
ZIP import. These checks do not establish physical-device compatibility.

## Deutsch

Ich habe für jede gemeldete ConnectKey-Zone Betriebsart und manuellen Sollwert
ergänzt, jeweils getrennt für Heizen und Kühlen. Damit sind vier der sechs
untersuchten Felder bedienbar. Die beiden vorübergehenden Sollwerte bleiben
Anzeigen, weil ihr Ende und ihre Rücknahme vom Zeitprogramm abhängen und noch
nicht vollständig umgesetzt und geprüft sind.

Die Auswahl enthält nur die von dieser Ressource angebotenen bekannten
Optionen `off`, `manual` und `auto`. Teilmengen sind möglich. Unbekannte
Optionen bleiben gesperrt. Ein unbekannter oder nicht mehr angebotener
aktueller Modus sperrt das Bedienelement vorübergehend.

Jeder manuelle Sollwert verwendet seine eigenen aktuellen Grenzen und die
Einheit `C`. Die Schrittweite stammt aus `stepSize`; ohne diese Angabe
beträgt der Anzeigeschritt 0,5 °C. Eine fehlerhafte Angabe sperrt die Bedienung.
Der Anzeigeschritt ist keine nachgewiesene Vorgabe für Schreibwerte:
Serviceaufrufe können gültige Zwischenwerte ohne Rundung senden.
Fehlerkennwerte, ungültige Antworten, fehlende Grenzen und ungeklärte
Einheiten erlauben keinen Schreibzugriff.

Das Ändern des manuellen Sollwerts schaltet die Betriebsart nicht um.
Auch eine Betriebsartenänderung sendet nur diesen einen Befehl. Eine
erfolgreiche Rückleseantwort muss zur gleichen Zone passen und einen gültigen
Wert mit passender Einheit enthalten. Nach einem unklaren PUT-Ergebnis lese
ich zurück; der PUT wird nicht automatisch wiederholt. Gleichzeitig
angeforderte Änderungen werden nacheinander mit den dann aktuellen
Metadaten geprüft. Anmeldung und Anfragelimits verwenden die vorhandene
Fehlerbehandlung.

Fehlende Schreibmetadaten können während des regulären Pollings ein
Bedienelement nachträglich verfügbar machen. Nach fehlerhaften oder
veralteten Antworten bleibt es gesperrt, bis gültige Daten eintreffen.
Die neuen Namen enthalten „einstellen“. Anzeigen, bestehende Kennungen,
eigene Namen und Deaktivierungen bleiben erhalten.

Die bestehende Steuerungsdiagnose in Schema 18 zeigt Eignung, Optionen,
Grenzen, Anzeigeschritt und Ablehnungsgrund ohne Messwerte, unbekannte
Optionstexte oder zusätzliche Abfragen. RRC-Zonen, Kindersicherung, Kopplung,
Wochenprogramme und eine gemeinsame Klimaentität gehören nicht zu P05.
Discovery-Grenzen und Abfrageintervalle bleiben erhalten.

Ich habe die Befehle, Fehlerfälle, Erholung und Übersetzungen mit ausdrücklich
synthetischen Daten geprüft. Die Bestätigung am Gerät steht aus. Bei einem
später beauftragten Praxistest müssen Zone und App-Rückmeldung einzeln
verglichen und die ursprünglichen Einstellungen anschließend
wiederhergestellt werden. Ich habe für P05 keine Anlage verändert.

**1.902 automatisierte Testfälle** einschließlich parametrisierter Varianten
bestehen bei **96,72 % kombinierter Code- und Verzweigungsabdeckung**. Ruff,
Formatprüfung und striktes Mypy bestehen. Reproduzierbarer Paketbau und
isolierter ZIP-Import sind in der Suite enthalten. Eine Bestätigung am
Gerät ersetzen diese Prüfungen nicht.
