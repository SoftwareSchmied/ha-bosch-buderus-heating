# Additional cloud air conditioners

I added support in v0.8.2-beta.6 for the Bacon **Midea Climate Series**.
Each selected, identified device receives one native Home Assistant `climate`
entity through the existing [account connection](bacon-connection.md).
This is separate from PointT heating-circuit cooling and the
[Midea/RWH water heaters](bacon-water-heaters.md). Air conditioners that
appear as their own gateway in the PointT gateway list (device type `rac`)
use a different backend, described in
[air conditioners on a PointT gateway](pointt-air-conditioners.md).

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
setback and breeze away, as well as schedules and energy history curves, are
not offered; timers and lifetime energy totals are described below. The fan speed named `turbo` is distinct from the advanced power
mode and is never changed by the Eco preset.

## Timers and energy totals (expected, untested on a real unit)

I added these in 0.9.2-beta.3 from documented and observed device behavior. I
have not run them against a real unit.

**Timers.** Two number entities, *Switch-on timer* and *Switch-off timer*
(minutes, 0 stops the timer), both disabled by default. The unit stores each
timer as an absolute time (`onTimestamp`, `offTimestamp`, whole seconds since
the epoch, 0 for none); the entity shows the minutes that remain (rounded up,
at most 1440) and sends one minute-aligned absolute time. A timer is offered
only when the device declares the field writable (`ro=false`), reports a
valid value and declares no step other than 60 seconds. Without that there is
no entity. Like the app, the integration refuses a time in the past, more than
one day ahead, and a switch-on and a switch-off time in the same minute. A
time less than 10 seconds ahead moves to the next minute, so that it cannot
be in the past when it reaches the unit. A
timer can switch the unit on or off later without anyone present. Every write
reads the state fresh, sends exactly one command, waits for the reported value
and never repeats. Schedules (`racSchedule`) are not offered; Home Assistant
automations can replace them.

**Energy totals.** Six sensors in kWh per unit, all disabled by default:
total electrical energy for heating, cooling, dehumidifying and fan only, and
total thermal output for heating and cooling. They come from one read-only
query to the history service of the same cloud (`history` host, `/graphql`),
the same one the app uses for its totals. They have no `state_class`, because
the range in which a counter may be reset is not documented; they therefore do
not feed the Energy dashboard by themselves. Request budget: at most one query
per unit every 30 minutes (as the app refreshes), none while no energy entity
of that unit is enabled and none while the unit is offline. The query uses
the same sign-in token, request limit and shared pause after a rate limit
(429) as every other request of the account. If the service answers 404, 405,
403 or a second 401, I stop the query for the rest of the session and log it
once; the sensors stay unavailable. After other failures the query waits 5
minutes, then longer each time, at most 30 minutes. Values older than two hours become
unavailable. Warm-water heat pumps and air purifiers get no energy sensors.

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
information message sets online status, which is enough for availability, as
in the HomeCom Easy app (see [the connection foundation](bacon-connection.md)).
Reported state fields age after 15 minutes and sensor fields after one hour;
the integration's own state read every five minutes keeps the state current.
A unit that sends no measurement for more than an hour shows its room
temperature as unknown but stays available while its state is current. After a
reconnect of the integration, earlier readings stay visible within those limits; after a
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
bleibt unverändert. Weitere Sondermodi, Programme und Verbrauchsverläufe
bietet die Integration nicht an; Timer und Gesamtverbrauch stehen unten.

### Timer und Gesamtverbrauch (erwartet, am echten Gerät ungetestet)

Ich habe beides in 0.9.2-beta.3 nach dokumentiertem und beobachtetem
Geräteverhalten ergänzt. Gegen ein echtes Gerät habe ich es nicht laufen lassen.

**Timer.** Zwei Zahlen-Entitäten, *Einschalt-Timer* und *Ausschalt-Timer*
(Minuten, 0 stoppt den Timer), beide standardmäßig deaktiviert. Das Gerät
speichert jeden Timer als absolute Zeit (`onTimestamp`, `offTimestamp`, ganze
Sekunden seit der Epoche, 0 für keinen). Die Entität zeigt die verbleibenden
Minuten (aufgerundet, höchstens 1440) und sendet eine auf die Minute gerundete
absolute Zeit. Ein Timer wird nur angeboten, wenn das Gerät das Feld als
beschreibbar (`ro=false`) meldet, einen gültigen Wert liefert und keine andere
Schrittweite als 60 Sekunden angibt; sonst entsteht keine Entität. Wie die App
lehne ich eine Zeit in der Vergangenheit, mehr als einen Tag Vorlauf und gleiche
Minute für Ein- und Ausschalten ab. Eine Zeit mit weniger als 10 Sekunden
Vorlauf rutscht auf die nächste Minute, damit sie beim Gerät nicht schon
vergangen ist. Ein Timer kann das Gerät später ohne
Anwesenheit ein- oder ausschalten. Jeder Schreibvorgang liest den Zustand
frisch, sendet genau einen Befehl, wartet auf den gemeldeten Wert und
wiederholt nie. Zeitpläne (`racSchedule`) biete ich nicht an; Automationen in
Home Assistant können sie ersetzen.

**Gesamtverbrauch.** Sechs Sensoren in kWh je Gerät, alle standardmäßig
deaktiviert: elektrische Gesamtenergie für Heizen, Kühlen, Entfeuchten und
nur Lüfter sowie abgegebene Wärme gesamt für Heizen und Kühlen. Sie kommen aus
einer rein lesenden Abfrage beim Verlaufsdienst derselben Cloud (Host
`history`, `/graphql`), den auch die App für ihre Summen nutzt. Sie haben keine
`state_class`, weil der Bereich, in dem ein Zähler zurückgesetzt werden darf,
nicht belegt ist; sie speisen das Energie-Dashboard daher nicht von selbst.
Anfragebudget: höchstens eine Abfrage je Gerät alle 30 Minuten (wie die App),
keine, solange keine Energie-Entität dieses Geräts aktiviert ist, und keine,
solange das Gerät offline ist. Die Abfrage nutzt dasselbe Anmelde-Token, die
Anfragebegrenzung und die gemeinsame Pause nach einer Ratenbegrenzung (429)
wie alle anderen Anfragen des Kontos. Antwortet der Dienst mit 404, 405, 403
oder einem zweiten 401, beende ich die Abfrage für den Rest der Sitzung und
schreibe das einmal ins Protokoll; die Sensoren bleiben nicht verfügbar. Werte
älter als zwei Stunden werden nicht verfügbar. Warmwasser-Wärmepumpen und
Luftreiniger erhalten keine Energiesensoren.

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
