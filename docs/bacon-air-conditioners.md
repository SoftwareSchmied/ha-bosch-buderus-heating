# Additional cloud air conditioners

I added support in v0.8.2-beta.6 for the Bacon **Midea Climate Series**.
Each selected, identified device receives one native Home Assistant `climate`
entity through the existing [account connection](bacon-connection.md).
This is separate from PointT heating-circuit cooling and the
[Midea/RWH water heaters](bacon-water-heaters.md).

## Identification and supported functions

The case-sensitive prefixes of `appliance.type` are `Climate Series`,
`ac_186i3`, `ac_midea_unknown`, `ae_700`, `ae_midea_unknown`, `cl_7000i`,
`cl_7100i`, `cl_midea_unknown`, `ui_midea_unknown`, `rac_midea_unknown_v2`
and `climate_series`. These are family identifiers, not a list of physically
tested retail models. Device titles do not establish compatibility. Sharp
and other families need their own proven connection and contract.

| Function | Reported field | Command or interpretation |
| --- | --- | --- |
| Current temperature | Sensor `roomTemperature` | Floating-point reading, explicit unit; no scaling factor |
| Target temperature | State `tempSetpoint` | Integer Celsius value; offered minimum, maximum and step |
| On/off | State `powerEnabled` | Write this boolean only; retain the stored mode |
| Mode | State `opMode` | `auto`, `heat`, `cool`, `dry`, `fan`; the last maps to HA `fan_only` |
| Fan speed | State `fanSpeed` | `auto`, `quiet`, `low`, `medium`, `high`, `turbo` |
| Vertical swing | State `vSwingEnabled` | Independent boolean; HA swing `on` or `off` |
| Horizontal swing | State `hSwingEnabled` | Independent boolean; HA horizontal swing `on` or `off` |
| Eco | State `ecoEnabled` | Independent boolean; HA preset `eco` or `none` on the climate entity |

Only known options present in this device's metadata become writable choices.
Optional functions require their own field declaration, valid current value
and explicit `ro=false`. Missing `ro` means read-only. Capabilities update
without restarting; disappearing capabilities do not create replacement IDs.
The thermostat target control is offered in auto, heat and cool, including
when one of those modes is stored while power is off. As a local control
policy, it is omitted for fan-only and dry. To switch from either of those
modes and set a temperature, change the mode first.

Power and operating mode are separate device commands. Selecting a non-off
HA mode deliberately combines `powerEnabled=true` with the requested
`opMode`. Turning on alone retains a known, offered stored mode. Selecting
off changes only power. A combined HA target/mode request validates every
field before sending one patch. This composition is tested synthetically;
its physical effect still needs device confirmation.

The chosen mode does not prove compressor activity. I do not publish
`hvac_action`, a fabricated idle state, heating activity or cooling activity.
Eco appears as the HA preset `eco` (with `none` for normal operation) only
when the device declares `ecoEnabled` with `ro=false` and reports a boolean.
A preset change sends only `ecoEnabled` and needs the usual reported
confirmation. Other comfort functions such as sleep, turbo power, ionizer,
setback and breeze away, as well as timers, schedules and energy history, are
not offered. The fan speed named `turbo` is distinct from the advanced power
mode and is never changed by the Eco preset.

## Units, validity and command confirmation

Metadata is read from `shadows.state` and `topics.sensor` in `topics/meta`.
Celsius aliases are `C`, `degC`, `Celsius` and the standard `°C` notation.
Sensor units `degF` and `Fahrenheit` are converted to Celsius internally.
The target command contract uses integer Celsius degrees. A Fahrenheit target
metadata declaration is not enabled without a separately verified contract.
Home Assistant may display and accept Fahrenheit; absolute temperatures and
the target step are converted separately using current HA conversion APIs.

Targets require integer limits and step. The integer metadata default is
step 1 when omitted. The minimum anchors the grid; the upper endpoint is
restricted to that grid. Values outside the grid are rejected, never rounded.
Only negligible floating-point conversion noise is tolerated. The local
target guard is 0..50 °C and the room-reading guard is -50..70 °C. These are
implementation plausibility checks, not manufacturer specifications.
Null, malformed, missing and incompatible values stay unknown.

Every command reads fresh state under the device lock, rechecks the exact
model, metadata, rights, limits, options and connectivity, and sends only
the requested fields. It never sends a stale copy of the complete state.
Unrelated app changes, custom names, timers and advanced flags are preserved.
Pending desired changes block another write. Only a newer matching reported
state confirms success; a desired-state acknowledgement is insufficient.
Unconfirmed writes make the entity unavailable until a regular read resolves
the uncertainty. Disconnects, rejections and timeouts never trigger replay.
The protocol has no verified compare-and-swap token: convergence is confirmed,
but simultaneous external writes to the same field cannot be attributed
exclusively to this client.

Availability and freshness follow the connection foundation: the newest
information message sets online status, a non-retained device message on any
channel within the last hour keeps it usable, reported state fields age after
15 minutes and sensor fields after one hour. After a reconnect of the
integration, earlier readings stay visible within those limits; after a
membership loss or a re-added device all needed streams must arrive again.
Old, duplicate or retained sensor messages do not refresh readings. A changed
model clears cached capabilities and values. The same HA entity identity is
retained across reconnects and reloads.

Schema 23 adds aggregate `air_conditioners.midea_climate` counts for identified
devices and current metadata/sensor streams. It includes no temperatures,
settings, model strings, device IDs or raw messages, and makes no extra
requests. These are data-reception counts, not HA registry counts.

## Validation and open device checks

I tested two synthetic profiles labelled `ac_186i3` and `cl_7000i` with different
offered modes, fan options, target grids and swing capabilities. Their
differences are deliberate test inputs, not claims about those retail models.
Tests cover malformed metadata, changing rights and limits, mixed units,
simultaneous changes, missing/duplicate/delayed confirmations, independent
freshness, reconnects and stable registration. Real HA service tests exercise
German/Celsius and English/Fahrenheit, both translation catalogs and entity
or device targeting. Real aiomqtt encoding is tested with both profiles
against a local WebSocket broker. The full suite also checks existing PointT
and water-heater behavior.

The account checked during P08 contained no Bacon devices. I have not installed
this package on a real climate unit or changed physical device settings.
Each actual model still needs confirmation of identification, metadata,
message frequency, readings, mode/target behavior and physical write effects.
The implementation is included in Beta.6; these software tests do not establish
physical-device compatibility.

## Deutsch

Ich habe die Midea Climate Series in v0.8.2-beta.6 als eigene Klimaentität ergänzt.
Sie zeigt die gemeldete Raumtemperatur, den Sollwert und die Betriebsart.
Ein/Aus, Betriebsarten, Lüfterstufen und beide Schwenkrichtungen richten sich
nach den Optionen und Schreibrechten des jeweiligen Geräts. Andere Familien
wie Sharp sind damit noch nicht abgedeckt. Die vorhandene Heizkreis-Kühlung
bleibt getrennt.

Die Betriebsart allein belegt keinen laufenden Kompressor. Deshalb gibt es
keine daraus abgeleitete Anzeige für tatsächliches Heizen oder Kühlen.
In Automatik, Heizen und Kühlen ist der gültige Sollwert einstellbar, auch
bei ausgeschaltetem Gerät mit einer dieser gespeicherten Betriebsarten.
Bei Lüften und Entfeuchten blende ich diesen Regler als eigene Bedienregel
aus. Für einen anschließenden Temperaturwechsel ist zuerst die Betriebsart
umzustellen. Meldet das Gerät `ecoEnabled` als beschreibbar und liefert einen
Ein/Aus-Wert, kannst du Eco über die Voreinstellung `eco` oder `none` der
Klimaentität schalten. Dabei sende ich nur dieses Feld; die Lüfterstufe Turbo
bleibt unverändert. Weitere Sondermodi, Timer, Programme und Verbrauchsverläufe
bietet die Integration nicht an.

Jeder Schreibzugriff prüft die aktuellen Fähigkeiten erneut und ändert nur
die angeforderten Felder. Die Rückmeldung muss den neuen Zustand bestätigen.
Bei unklarem Ausgang wird der Befehl nicht wiederholt. Die gleichzeitige
Änderung desselben Felds durch einen anderen Client lässt sich dem Protokoll
nach nicht eindeutig zuordnen.

Isttemperaturen dürfen mit belegter Celsius- oder Fahrenheit-Einheit kommen.
Für den Gerätesollwert sind derzeit ganze Celsiusgrade mit gültigen Grenzen
und Schrittweiten erforderlich. Home Assistant übernimmt eine gewünschte
Fahrenheit-Anzeige und Eingabe. Ungültige Werte werden nicht gerundet oder
durch Null ersetzt. Die genannten Schutzgrenzen sind keine Herstellerangaben.

Beide Testprofile sind ausdrücklich synthetisch. Ich habe die HA-Bedienung,
DE/EN-Texte, die MQTT-Nachrichten und die Wiederherstellung geprüft.
Die tatsächlichen Modellfähigkeiten und Schreibwirkungen müssen weiterhin
an einem Klimagerät bestätigt werden. Die Implementierung ist in Beta.6 enthalten;
die Softwaretests bestätigen noch keine Kompatibilität am realen Gerät.
