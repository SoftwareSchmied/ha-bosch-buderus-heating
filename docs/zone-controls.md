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
  change with its own message. The flags are checked again on the reading
  of the change's own request, so a flag reported since the last poll also
  refuses it. A target whose flags follow the mode (a manual setpoint not
  used in the mode auto) is checked after the mode is confirmed, and after a
  new mode the targets are read again a few seconds later. A circuit with the
  control type `constants` is only shown.
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

The select and number controls of the zone (below) stay; since 0.9.2 they
follow the climate's write rules: the setting, the circuit's season and
control type are read again before the request, `used`/`available` of the
current reading decide, and a circuit on constant values sends nothing. The
climate is the simpler way to use a zone. The climate of the heating circuit
the zones belong to also stays: it controls the circuit, the zone climates
control the rooms. On a zone-controlled circuit (`controlType` `ISRC`) the
room setpoints are set per zone.

I tested the climate with automated tests and a synthetic fixture in the
shape of a real report; it is expected, but not yet confirmed on a real
installation.

## Radio room devices

Each radio room device below `/devices/<id>` that reports them gets two
diagnostic entities: **Battery low** (binary sensor, enabled; on for
`BAT_LOW` and `BAT_TOO_LOW`, off for `BAT_NORMAL`; any other state such as
`NO_BAT` or `BAT_FOTA_FORBIDDEN` stays unknown and is shown as
`reported_state` when the device offers it) and **Radio signal** (sensor in dB, disabled by
default). A third diagnostic entity, **RF connection lost since**, is a
timestamp: the controller keeps the placeholder `2000-00-00T00:00` while the
connection was never lost, which is no date, so the sensor shows unknown. A
text with a time zone offset keeps it; a text without one is a local time of
the controller and is read in the Home Assistant time zone. The text as
reported is shown as the attribute `raw_value` (not for the placeholder). The sensor keeps the
identity of the text sensor it replaces. Battery states without an alarm
(`BAT_MEDIUM`, `BAT_FOTA_FORBIDDEN`) and for a device without a battery
(`NO_BAT`, `NOT_BAT`) stay unknown with their `reported_state`.

The radio devices of a ConnectKey controller have no name of their own, so
their entities are named after the zone they belong to (the device's `zoneId`
and the zone's name, or "Zone N" when it has none). A zone with several radio
devices adds "Radio device 1", "Radio device 2" … in the order of the device
numbers. A device without a valid zone keeps the name "Room device N".
Identifiers and entity IDs do not change; a new name shows after a reload.
Room controllers keep their own device entities. Expected, not yet confirmed
on a real installation.

### Setpoint after a mode change

A zone offers only the setpoint of its current mode: `manualRoomSetpoint` in
`manual`, `temporaryRoomSetpoint` in `auto`; the other one is answered with
"not found". After the mode of a zone has changed (in the app or here) I read
the setpoint of the new mode again, a short series of at most three reads, and
poll it from then on. At most three series per setpoint and hour and twelve
in all are started, at least ten minutes apart, and each counts against the
discovery allowance. Nothing is written for this. In `off` there is no
setpoint. The climate shows the program target of `auto` from
`currentRoomSetpoint` while no temporary setpoint exists. A mode together with
a temperature in one call is refused unsent when the target of the new mode is
not known yet; set the mode first, then the temperature.

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
  has its children read from it too. The later search for a missing part
  (for example a root that answered 503 at the start) has the same bound.

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

The numeric control uses the current `stepSize`, or 0.5 °C when it is
absent. Invalid advertised steps block the control. Since 0.9.2 the step is
a rule of the write: a value between two steps, counted from the minimum, is
refused before anything is sent. Without `stepSize` the step is 0.5 °C, the
one the manufacturer's app uses for the zone setpoint. The zone climate puts
a value converted from °F on the nearest step.
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
can change the mode and the target, and why not, and under `writes` how many
changes were refused, sent, confirmed by the read-back, unconfirmed or failed
(counts and the last step, no values; the section `zone_setpoint_recovery`
counts the series described above). Diagnostics add no cloud
requests and expose no readings or unknown mode text.

## Boundaries and evidence

Mode and temperature selection each use a single-value PUT. The temporary
setpoint also takes a single float; whether it is active is shown from its
reading (0 or more), not compared with the current switch point, because the
zone's time program is not readable on the known installation.

RRC paths under `/zones/zn{id}`, child locks, pairing and weekly programs are
outside these controls. Identifier selection and polling intervals are
unchanged; for the discovery on large zone controllers (zone sides first, the
allowance of 400 paths beyond the 512) see
[discovery on large zone controllers](#discovery-on-large-zone-controllers).

The [synthetic fixture](../tests/fixtures/contracts/connectkey/zone-controls.json)
describes two zones with different heating/cooling options, bounds and steps.
[Tests](../tests/test_zone_controls.py) cover individual commands, metadata
changes, invalid responses, delayed confirmations, concurrency, recovery
and German/English names and native HA option translations.

Physical-device confirmation is still outstanding. A future permitted test
should compare each reported zone with MyBuderus, record the original mode and
setpoint, verify each selected command in that same zone and restore the
original settings. I made no physical writes for these controls.

The commands, failure cases and recovery are covered by automated tests, with
Ruff, formatting and strict Mypy. These checks do not establish
physical-device compatibility.

## Deutsch

Ich habe für jede gemeldete ConnectKey-Zone Betriebsart und manuellen Sollwert
ergänzt, jeweils getrennt für Heizen und Kühlen. Seit 0.9.2 hat jede Zone
außerdem eine Klimaentität (Betriebsart, Zieltemperatur, Raumtemperatur,
Feuchte). Im Automatikbetrieb schreibt sie den vorübergehenden Sollwert bis
zum nächsten Schaltpunkt; die Taste „Vorübergehenden Sollwert aufheben“
schreibt -1. Die Kühlseite gilt, solange der Heizkreis
der Zone die Kühlsaison meldet. Vor jeder Änderung lese ich Betriebsart,
Ziel, Jahreszeit und Regelungsart des Heizkreises frisch; danach genau ein
PUT und Rücklesen. Die Kennzeichen `used`/`available` entscheiden an dieser
frischen Lesung. Ein Ziel, dessen Kennzeichen mit der Betriebsart wechseln,
prüfe ich nach der bestätigten Betriebsart; nach einer neuen Betriebsart
lese ich die Ziele einige Sekunden später erneut. Auswahl und Zahl der Zone
folgen seit 0.9.2 denselben Schreibregeln. Die Aktion (heizen/kühlen/Leerlauf) ist berechnet: Heizkreis
heizt und Raum unter dem aktiven Sollwert. Funk-Raumgeräte zeigen „Batterie
schwach“ (aktiviert) und das Funksignal in dB (deaktiviert). Seit 0.9.2-beta.3
heißen die Entitäten eines Funk-Raumgeräts nach seiner Zone (`zoneId` und
Zonenname, sonst „Zone N“); hat eine Zone mehrere Funkgeräte, folgt
„Funkgerät 1“, „Funkgerät 2“ … nach der Gerätenummer; ohne gültige Zone bleibt
„Raumgerät N“. Kennungen und Entity-IDs bleiben, der neue Name zeigt sich nach
dem Neuladen. „Funkverbindung unterbrochen seit“ ist ein Zeitstempel; der
Platzhalter `2000-00-00T00:00` („nie unterbrochen“) ist kein Datum und zeigt
unbekannt, ein Text ohne Zeitzone gilt als Ortszeit des Reglers und wird in
der Zeitzone von Home Assistant gelesen; der gemeldete Text steht als Attribut
`raw_value`. Der Sollwert einer Zone besteht nur in ihrer aktuellen
Betriebsart (manuell: `manualRoomSetpoint`, Automatik:
`temporaryRoomSetpoint`); nach einem Moduswechsel lese ich den Sollwert der
neuen Betriebsart in einer kurzen, begrenzten Folge erneut (höchstens drei
Lesungen je Folge, höchstens drei Folgen je Sollwert und Stunde, zwölf
insgesamt, nichts wird geschrieben). Die Diagnose zählt je Zone die
Änderungen (abgelehnt, gesendet, bestätigt, unbestätigt, fehlgeschlagen).
Die Discovery
liest die Betriebsarten und Sollwerte der Zonen jetzt direkt nach den
Zonenverzeichnissen und fragt ein Blatt, das bei einem Funkgerät verweigert
wurde, bei gleichartigen Geräten nicht erneut ab. Sind die 512 gemeinsamen
Pfade verbraucht, lesen optionale Proben und direkte Lesungen unter
verweigerten Verzeichnissen mit einem eigenen Kontingent von höchstens 400
Pfaden weiter; die Suche nach fehlenden Teilen hat dieselbe Grenze. Eine noch
größere Anlage endet mit `resource_limit` (Diagnose `resource_limit_scope`).
Alles ist mit automatisierten Tests geprüft, am Gerät noch nicht bestätigt.

Die Auswahl enthält nur die von dieser Ressource angebotenen bekannten
Optionen `off`, `manual` und `auto`. Teilmengen sind möglich. Unbekannte
Optionen bleiben gesperrt. Ein unbekannter oder nicht mehr angebotener
aktueller Modus sperrt das Bedienelement vorübergehend.

Jeder manuelle Sollwert verwendet seine eigenen aktuellen Grenzen und die
Einheit `C`. Die Schrittweite stammt aus `stepSize`; ohne diese Angabe
beträgt sie 0,5 °C wie in der Hersteller-App. Eine fehlerhafte Angabe sperrt
die Bedienung. Seit 0.9.2 ist die Schrittweite eine Vorgabe für das
Schreiben: Ein Wert zwischen zwei Schritten, gezählt ab dem Minimum, wird
vor jeder Anfrage abgewiesen. Die Zonen-Klimaentität legt einen aus °F
umgerechneten Wert auf den nächsten Schritt.
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
Die Abfrageintervalle bleiben erhalten; die Discovery großer Zonenregler
(Zonenseiten zuerst, Kontingent von 400 Pfaden über die 512 hinaus) ist oben
beschrieben.

Ich habe die Befehle, Fehlerfälle, Erholung und Übersetzungen mit ausdrücklich
synthetischen Daten geprüft. Die Bestätigung am Gerät steht aus. Bei einem
später beauftragten Praxistest müssen Zone und App-Rückmeldung einzeln
verglichen und die ursprünglichen Einstellungen anschließend
wiederhergestellt werden. Ich habe für diese Bedienelemente nichts an einer
Anlage geschrieben.

Befehle, Fehlerfälle und Erholung sind mit automatisierten Tests geprüft,
dazu Ruff, Formatprüfung und striktes Mypy. Eine Bestätigung am Gerät
ersetzen diese Prüfungen nicht.
