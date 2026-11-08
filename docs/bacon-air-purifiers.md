# Additional cloud air purifiers

I added support in v0.8.2-beta.6 for seven connected Air purifier families
through the existing [account connection](bacon-connection.md). Each selected,
identified device receives a native Home Assistant fan. Optional readings and
controls depend on that device's current metadata and reported values.

## Identification

| Family | Case-sensitive `appliance.type` prefixes | Filter warning / replace |
| --- | --- | --- |
| Air 1001i | `Air 1001i`, `air_1001i_v2` | At or below 30% / 0% |
| Air 2000i | `air_2000i` | At or below 50% / 20% |
| Air 2001i | `Air 2001i`, `air_2001i_v2` | At or below 50% / 20% |
| Air 4000i | `air_4000i` | At or below 50% / 20% |
| Air 4001i | `Air 4001i`, `air_4001i_v2` | At or below 50% / 20% |
| Air 6000i | `air_6000i` | At or below 50% / 20% |
| Air 6001i | `Air 6001i`, `air_6001i_v2` | At or below 50% / 20% |

These identifiers establish the family contract. They do not establish that
every model offers all the functions below. A custom device name does not
prove compatibility. The unconnected Air 4000 is not inferred from Air 4000i.

## Native controls

| HA function | Reported state field | Behavior |
| --- | --- | --- |
| Fan on/off | `powerEnabled` | Changes only power and retains the stored preset |
| Fan preset | `opMode` | Offered subset of `auto`, `manual`, `silent` |
| Fan stage, boxed number | `fanSpeed` | Positive integer stage with the device's minimum, maximum and step |
| Child lock | `childLockEnabled` | Independent boolean switch |
| Display | `displayEnabled` | Independent boolean switch |
| Light ring | `lightEnabled` | Independent boolean switch |
| Ambient light in standby | `ambientLightOnInStandby` | Independent boolean switch |
| Ambient light in silent mode | `ambientLightOnInSleep` | Independent boolean switch |
| Ambient light color | `lightMode` | Offered colors: warm/neutral/cool white, blue, purple, orange, red |
| Ambient light brightness | `lightIntensity` | Integer percentage, only with explicit `%` metadata |

Fan stage is a discrete setting. The fan has no percentage control or invented
percentage display. Change the preset to Manual for manual operation; changing
the separate stage sends only that stage and preserves the current preset.
Selecting a preset alone also preserves power. The HA turn-on service can
combine power and a requested preset in one validated patch. Turning on
without a preset requires a known, currently offered stored preset.

Each additional control needs explicit `ro=false`, its own metadata and a
valid current value. Missing `ro` means read-only. Integer limits and a valid
grid are required; omitted step means 1 under the integer contract. Inputs
outside the grid are rejected without rounding. Local numeric envelopes are
1..100 for fan stages and 0..100% for brightness. These are implementation
guards, not manufacturer claims about the available levels.

The color select implements the separate color command. It does not offer
the `off` or `auto` light-ring mode commands, whose full behavior also changes
other light settings. In either of those modes, no color is selected. Timers,
the combined light-ring mode change and filter reset remain unavailable.
The existence of the `filterReset` state field does not establish a complete
reset procedure, so it cannot be written through a generic service either.

## Readings and units

| Reading | Sensor field | Interpretation |
| --- | --- | --- |
| Air quality | `airQuality` | 1 good, 2 fair, 3 poor, 4 bad; unitless category |
| PM2.5 | `pm2_5`, alias `pm2.5` | Integer concentration; explicit µg/m³ or mg/m³, normalized to µg/m³ |
| TVOC value | `tvoc` | Unscaled integer without a fabricated concentration unit or AQI class |
| Room temperature | `roomTemperature` | Floating-point Celsius/Fahrenheit reading, normalized to Celsius |
| Relative humidity | `relHumidity` | Floating-point value with explicit `%` metadata |
| Remaining filter life | `filterCapacity` | Remaining percentage, 0..100; not accumulated operating hours |
| Filter status | `filterCapacity` | Derived from the family-specific thresholds above |

Air quality is read independently. I do not substitute PM2.5 or TVOC for it.
PM2.5 accepts `µg/m3`, `µg/m³`, `mg/m3` and `mg/m³`; milligrams are multiplied
by 1,000 once. Both spellings of the PM2.5 field are supported. Conflicting
values or metadata under the two names make the reading unavailable.

Temperature accepts `C`, `degC`, `Celsius`, `°C`, `degF` and `Fahrenheit`.
Home Assistant handles the selected display unit. The local room-temperature
guard is -50..70 °C. Humidity and filter percentage remain within 0..100.
Filter metadata may omit its unit because the field's percentage meaning is
established independently; a conflicting hours/days unit is rejected. Air
quality and TVOC require no declared unit. An unknown physical TVOC unit is
not silently discarded. Malformed, missing, nonfinite and out-of-bound
values remain unavailable instead of becoming zero. Filter life uses
measurement statistics, never a cumulative counter or a predicted end date.

## Freshness, writes and diagnostics

Metadata, state and sensor messages use the shared connection. New metadata
can add optional entities while running. Existing identities survive missing
capabilities, reconnects and reloads, including user names and disabled flags.
Online status comes from the newest information message and stays usable
while a non-retained device message on any channel arrived within the last
hour. Reported state fields expire after 15 minutes and sensor fields after
one hour; old, duplicate and retained sensor messages do not refresh
readings. Sensor envelopes contain timestamped items; the newest item extends
non-null fields in the existing sample. Omitted fields retain their own age
and expire independently. A model or connection change prevents reuse of
fields from the previous device or connection.

Every write reads fresh state under the device lock and rechecks the exact
model, current metadata, rights, options and bounds. Only requested fields
are sent. A newer matching reported state confirms the result. Desired-only
acknowledgements, rejection, cancellation, timeout and disconnect never cause
replay. Uncertainty disables state controls until a subsequent read resolves
it; independent fresh sensor observations remain usable. The transport has
no proven compare-and-swap token, so convergence does not establish exclusive
ownership of a simultaneous external change to the same field.

Diagnostics schema 24 adds aggregate `air_purifiers` counts per known family:
identified devices, current metadata and current sensor streams. It adds no
requests and includes no raw model strings, IDs, measured values, settings or
names. These counts describe data reception, not the HA entity registry.

## Validation and remaining device checks

The `air_1001i` and `air_6001i` fixtures are explicitly synthetic. Their basic
and extended capabilities are test inputs, not observations of those retail
models. Tests exercise all seven family mappings and filter thresholds,
individual metadata, units and aliases, invalid values, late discovery,
changing rights and limits, concurrency, lost confirmations and reconnects.
Native HA services exercise fan, number, switch and select in German and
English, translations and registry continuity. Both profiles also use the
real aiomqtt MQTT 5 codec against a local WebSocket broker.

The account checked in P08 contained no Bacon devices. I have not installed
P11 on a real air purifier or changed physical settings. Each family still
needs confirmation of identification, actual capabilities, units, message
frequency, displayed values and the effects of its controls. This package
has not been released.

## Deutsch

Ich habe die sieben oben aufgeführten Air-Familien in v0.8.2-beta.6 ergänzt. Ein/Aus
und die angebotenen Betriebsarten erscheinen am nativen Lüfter. Die genaue
Lüfterstufe ist als separates Zahlenfeld bedienbar. Für manuellen Betrieb
ist zuerst Manuell zu wählen. Der Stufenwechsel erhält die Betriebsart;
der reine Betriebsartenwechsel erhält den Ein-/Aus-Zustand.

Je nach gemeldeter Ausstattung erscheinen Kindersicherung, Display,
Lichtring, Ambientebeleuchtung im Bereitschafts- oder Ruhemodus, Lichtfarbe
und Helligkeit. Alle Bedienelemente benötigen aktuelle, passende Metadaten
und ausdrückliche Schreibrechte. Helligkeit wird nur mit gemeldeter
Prozenteinheit angeboten. Die Farbe kann unabhängig gesetzt werden; der
kombinierte Lichtring-Moduswechsel, Timer und Filterreset bleiben offen.

Luftqualität ist eine Qualitätsstufe, PM2,5 ein Messwert mit belegter Einheit
und TVOC ein unskalierter Zahlenwert ohne erfundene Konzentrationseinheit.
Raumtemperatur und relative Luftfeuchtigkeit behalten ihre jeweiligen
Einheiten. Die Filterrestlebensdauer ist ein verbleibender Prozentwert.
Die Warnschwellen unterscheiden sich zwischen Air 1001i und den sechs
anderen Familien wie in der Tabelle angegeben. Daraus werden weder
Betriebsstunden noch ein voraussichtlicher Wechseltermin berechnet.

Ungültige oder veraltete Werte bleiben nicht verfügbar. Neue Fähigkeiten
können während des Betriebs hinzukommen, ohne bestehende Entitäts-IDs oder
Benutzereinstellungen zu ersetzen. Schreibzugriffe prüfen den aktuellen
Gerätestand erneut, verändern nur die angeforderten Felder und benötigen
eine gemeldete Zustandsbestätigung. Ein unbestätigter Befehl wird nicht
wiederholt. Diagnose-Schema 24 enthält ausschließlich zusammengefasste
Familien- und Aktualitätszähler für diesen Bereich.

Ich habe die Software mit synthetischen Profilen, nativen HA-Diensten in
DE/EN und einem lokalen MQTT-Broker geprüft. Die tatsächlich vorhandene
Ausstattung und die Wirkung der Bedienung müssen je Familie noch an einem
Gerät bestätigt werden. Die Implementierung ist in Beta.6 enthalten.
