# ConnectKey zone controls

I added operating-mode and manual-temperature controls for each reported
ConnectKey zone, separately for heating and cooling. Since 0.9.2 each zone
also has a climate entity that combines mode, target, room temperature and
humidity, and writes the temporary setpoint of the mode `auto` (see
[zone climate](#zone-climate)). The temporary setpoint itself still has no
number entity of its own.

## Zone climate

A zone controller such as an MX400 with radio room thermostats behind a K40
gateway has one climate entity per zone (`<gateway>:zones:zoneN:climate`),
named like the zone. It is created as soon as the zone reports a valid
heating mode, also when the mode is read-only; then it only shows the zone.

| Zone mode | Climate mode | Target |
|---|---|---|
| `off` | Off | none |
| `manual` | Heat (Cool on the cooling side) | `manualRoomSetpoint` of the side |
| `auto` | Auto | the program's `currentRoomSetpoint`; a target set here goes to `temporaryRoomSetpoint` and lasts until the next switch point |

- **Side:** the `heating` leaves normally; the `cooling` leaves while the
  zone's heating circuit reports `currentSuWiMode` `cooling`. Without
  readable cooling leaves the climate is then unavailable.
- **Heating circuit of a zone:** taken from the radio devices of the zone
  (`zoneId` and `assignedHC`). Devices naming different circuits, or a
  circuit the installation does not have, leave the zone without one; when
  no device names the zone, an installation with exactly one heating circuit
  uses that one. Without a circuit the zone stays on the heating side.
- **Changes:** right before its one request each change reads the zone's
  mode, the target, and the circuit's `currentSuWiMode` and `controlType`
  again on the gateway; if one of them changed since it was shown, nothing
  is sent. Then the value is read back; a lost answer is resolved by
  reading, never by a second request. A mode with a temperature writes the
  mode first and the target only after the mode is confirmed; the target is
  checked against the new mode before anything is sent.
- **Flags:** a mode or setpoint the zone reports as not available hides the
  climate or its target; one it does not use now (`used` false) refuses the
  change with its own message. A circuit with the control type `constants`
  is only shown.
- **Clear button:** "Clear temporary setpoint" writes -1 in the mode `auto`
  while a temporary setpoint is active; any
  negative read-back confirms it. When the advertised limits leave -1 out,
  the button is created disabled.
- **Action (calculated):** off in the mode off; otherwise the activity of
  the zone's heating circuit, and heating (cooling) only while the room
  temperature is below (above) the zone's active setpoint, idle otherwise.
  Without the circuit activity, the room temperature or the setpoint it
  stays unknown. The attribute `hvac_action_derived_from` lists the
  readings it combines.
- **Attributes:** `zone_side`, `active_setpoint`, `temporary_override`,
  `child_lock` (read only) and `heating_circuit`.

The select and number controls of the zone (below) stay as they are; the
climate is the simpler way to use a zone. The climate of the heating circuit
the zones belong to also stays: it controls the circuit, the zone climates
control the rooms. On a zone-controlled circuit (`controlType` `ISRC`) the
room setpoints are set per zone.

I tested the climate with automated tests and a synthetic fixture in the
shape of a real report; it is expected, but not yet confirmed on a real
installation.

## Radio room devices

Each radio room device below `/devices/<id>` that reports them gets two
diagnostic entities: **Battery low** (binary sensor, enabled; on for `low`,
off for `ok`, any other text stays unknown and is shown as `reported_state`
when the device offers it) and **Radio signal** (sensor in dB, disabled by
default). Room controllers keep their own device entities. Expected, not yet
confirmed on a real installation.

## Discovery on large zone controllers

A zone controller with many zones and radio devices used up the discovery
budget of 512 paths before the zone modes and setpoints, because the cloud
refuses many paths on the way. Three rules keep them in and let such a
discovery finish:

- The mode and setpoint leaves of every side a zone directory lists
  (`heating`, `cooling`) are read right after the zone directories, even
  when the side directory itself is refused. Zones without a listed side
  keep them as optional probes; the older names `heat`, `cool` and
  `heatCool` stay optional probes.
- Radio devices that list exactly the same children are the same kind of
  device. A leaf the cloud refuses (HTTP 403) on the first of them is not
  requested for the others (`identical_device_refused` in the diagnostics);
  any other answer requests it for every device. A device that lists other
  children keeps its own requests. Leaves of identical devices wait for the
  first answer for one batch at most.

- Once the 512 shared paths are used up, optional probes and direct reads
  below refused containers go on with an allowance of their own of at most
  400 paths (see [request behavior](request-behavior.md#startup-order-and-time-budgets)).
  In the shape of the report that led to these rules (8 zones, 12 radio
  devices, a refused second heating circuit and three refused heat-source
  entries whose names are not `hsN`) the discovery needs about 700 paths and
  finishes with about 190 of the 400. A directory read from the allowance
  has its children read from it too.

A still larger installation stops at `resource_limit`; the diagnostics then
name the limit (`resource_limit_scope`).

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
known options, bounds, UI step and rejection reasons. Since 0.9.2 the
temporary setpoints have a write contract of their own, used only by the
zone climate and its clear button (before: `zone_override_lifecycle_unverified`).
The section `zone_climates` shows per zone whether its climate is shown and
can change the mode and the target, and why not. Diagnostics add no cloud
requests and expose no readings or unknown mode text.

## Boundaries and evidence

Mode and temperature selection each use a single-value PUT. The temporary
setpoint also takes a single float; whether it is active is shown from its
reading (0 or more), not compared with the current switch point, because the
zone's time program is not readable on the known installation.

RRC paths under `/zones/zn{id}`, child locks, pairing and weekly programs are
outside these controls. Identifier selection, the discovery budget and
polling intervals are unchanged.

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
ergänzt, jeweils getrennt für Heizen und Kühlen. Seit 0.9.2 hat jede Zone
außerdem eine Klimaentität (Betriebsart, Zieltemperatur, Raumtemperatur,
Feuchte). Im Automatikbetrieb schreibt sie den vorübergehenden Sollwert bis
zum nächsten Schaltpunkt; die Taste „Vorübergehenden Sollwert aufheben“
schreibt -1. Die Kühlseite gilt, solange der Heizkreis
der Zone die Kühlsaison meldet. Vor jeder Änderung lese ich Betriebsart,
Ziel, Jahreszeit und Regelungsart des Heizkreises frisch; danach genau ein
PUT und Rücklesen. Die Aktion (heizen/kühlen/Leerlauf) ist berechnet: Heizkreis
heizt und Raum unter dem aktiven Sollwert. Funk-Raumgeräte zeigen „Batterie
schwach“ (aktiviert) und das Funksignal in dB (deaktiviert). Die Discovery
liest die Betriebsarten und Sollwerte der Zonen jetzt direkt nach den
Zonenverzeichnissen und fragt ein Blatt, das bei einem Funkgerät verweigert
wurde, bei gleichartigen Geräten nicht erneut ab. Alles ist mit
automatisierten Tests geprüft, am Gerät noch nicht bestätigt.

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
Optionstexte oder zusätzliche Abfragen. RRC-Zonen, Kindersicherung, Kopplung
und Wochenprogramme gehören nicht zu diesen Bedienelementen.
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
