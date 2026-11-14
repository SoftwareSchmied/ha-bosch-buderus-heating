# Additional cloud water heaters

I added support in v0.8.2-beta.6 for two separately identified Bacon water
heater families. This builds on the [account connection](bacon-connection.md).
It does not change PointT DHW circuits or WDDW2 devices.

## Supported scope

Each selected, identified device receives one native Home Assistant
`water_heater` entity. It shows current temperature, target temperature and
operation mode when valid data is available. The same entity provides the
supported target and mode controls. There are no duplicate number/select
controls.

| Family | Case-sensitive appliance type prefixes | Known operation codes |
| --- | --- | --- |
| Midea | `Hot Water Series`, `dhw_midea_unknown_v2` | `eHeater`, `economy`, `hybrid`, `smart` |
| RWH | `Heat Pump Water Heater`, `dhw_heatpump`, `hpwh_vw_v1`, `hpwh_fs_lpp_v1`, `hpwh_fs_mpp_v1` | `comfort`, `dual`, `eco` |

The prefixes classify the reported appliance type, not its user-defined title.
Only known modes present in this device's `opMode.enum` are offered.
Unknown codes do not become write options. A missing power state is unknown,
not off. The HA states and service values are stable:

| HA service value | English | Deutsch | Wire effect |
| --- | --- | --- | --- |
| `off` | Off | Aus | `powerEnabled=false` only |
| `electric` | Electric heater | Elektroheizung | Midea `opMode=eHeater` |
| `economy` | Economy | Sparbetrieb | Midea `opMode=economy` |
| `hybrid` | Hybrid | Hybrid | Midea `opMode=hybrid` |
| `smart` | Smart | Smart | Midea `opMode=smart` |
| `comfort` | Comfort | Komfort | RWH `opMode=comfort` |
| `dual` | Dual | Dual | RWH `opMode=dual` |
| `eco` | Eco | Eco | RWH `opMode=eco` |

Every non-off operation also sets `powerEnabled=true`. Temperature changes
write only `tempSetpoint`. Combined temperature/mode service requests are
rejected before writing; use the separate services so each change is confirmed.
Both controls require explicit `ro=false` for
every changed field. An omitted `ro` is read-only.

## Readings, limits and freshness

- State comes from `state.reported`; current temperature and six status flags
  come from `topics/sensor`. Desired state is never displayed as confirmed.
- Metadata comes from `shadows.state` and `topics.sensor` in `topics/meta`.
  Temperature units must explicitly be `C` or `°C`. Values are integer
  Celsius degrees without a scaling factor. Home Assistant handles Fahrenheit;
  the displayed step is converted as an interval too.
- Targets require integer minimum and maximum metadata. An omitted step means
  one degree, as defined by the integer metadata contract. The minimum anchors
  the step grid. Values are never rounded to fit.
- A reported `setTempMax` can only reduce the permitted maximum. Invalid
  limits disable the target control. The local 0..100 °C guard rejects
  implausible temperatures and common integer error patterns; it is not a
  claim about manufacturer limits or a list of all possible error sentinels.
- Missing, null, malformed and incompatible fields stay unknown. Missing
  status flags are never treated as false.
- Online status comes from the newest device-information message. It stays
  usable while a non-retained device message on any channel arrived within
  the last hour; an explicit offline message applies immediately. Reported
  state fields expire after 15 minutes, sensor fields independently after one
  hour. Sensor timestamps establish
  order only; their epoch unit is not assumed. Duplicate, older and retained
  sensor messages cannot refresh a measurement.
- Retained metadata may describe the current subscription's capabilities.
  It cannot establish connectivity or fresh measurements. After a reconnect
  of the integration, earlier state and sensor fields stay visible within
  their own age limits; after a membership loss or a re-added device, all
  required streams must be received again. Changing the reported model
  discards the former model's cached state, metadata and sensor data.

The six optional read-only indicators are energy saving, standard mode,
thermal disinfection, holiday mode, upper electric heater and lower electric
heater. Each is created only when its field is declared in sensor metadata.
Later capabilities are added without restarting. Existing entities retain
their identity when capabilities disappear and recover.

I have not added holiday scheduling, boost, disinfection start/cancel or
disinfection-temperature controls. The inspected DHW command contract covers
operation mode and target temperature; these additional actions need their
own complete command and lifecycle evidence. A status indicator is not a
promise that the corresponding action can be started from Home Assistant.

## Writes and diagnostics

Each write reads state under the existing device lock and rechecks the family,
current metadata, rights, options, limits and connectivity. Pending desired
changes block another command. Only a newer matching reported state confirms
the operation. Timeout, rejection, disconnect or ambiguous results do not
trigger a replay. The central entity becomes unavailable after an unconfirmed
write until a regular state read resolves uncertainty.

Schema 22 adds counts of identified Midea/RWH devices and devices with current
metadata or sensor streams. It exports no temperatures, options, names, model
strings, device identifiers or raw messages. These counts describe received
data, not the HA entity registry. Diagnostics make no additional requests.

## Validation and remaining device checks

I tested synthetic responses for both families, malformed and changing
metadata, independent freshness, ordering, wrong-family commands, confirmed
and unconfirmed writes, HA services, DE/EN translations, Fahrenheit,
reconnection and registry continuity. MQTT tests use the real aiomqtt codec
against a local WebSocket broker and verify the outgoing family commands.

The actual account had no Bacon devices during the P08 connection check.
There is no physical-device validation for either family. In particular,
the observed type prefixes, actual metadata units/limits, live-message
frequency, displayed temperatures and physical write effects still need
confirmation on each family. No real device settings were changed for P09.

## Deutsch

Ich habe Midea- und RWH-Warmwassergeräte in v0.8.2-beta.6 ergänzt. Jede Familie wird
anhand des gemeldeten Gerätetyps erkannt und erhält ihre eigenen Betriebsarten.
Diese Geräteanbindung verändert keine vorhandenen PointT-Warmwasserkreise
oder WDDW2-Geräte.

Eine zentrale Warmwasserentität zeigt gültige Ist- und Solltemperaturen sowie
die Betriebsart. Nur tatsächlich angebotene bekannte Optionen und ausdrücklich
beschreibbare Felder lassen sich ändern. Temperaturen benötigen eine passende
Celsius-Einheit und die gemeldeten Grenzen. Die Schrittweite wird geprüft;
eine fehlende Schrittweite bedeutet laut Metadatenvertrag ein Grad.
Ein gemeldetes `setTempMax` kann die Obergrenze zusätzlich einschränken.
Die eigene Schutzgrenze 0..100 °C ist keine Herstellerangabe. Fehlerhafte oder
fehlende Werte bleiben unbekannt. Auch Fahrenheit und dessen Schrittweite
sind berücksichtigt.

Energiesparen, Standardbetrieb, Desinfektion, Urlaub und die beiden Heizstäbe
erhalten bei passender Metadatendeklaration eigene Statusanzeigen.
Urlaubsplanung, Boost sowie das Starten oder Abbrechen der Desinfektion und
deren Temperatureinstellung bleiben offen. Dafür fehlt ein vollständig
belegter Bedienablauf im geprüften Warmwasservertrag.

Ich prüfe vor jedem Befehl erneut den aktuellen Zustand und die erlaubten
Einstellungen. Temperatur und Betriebsart werden einzeln geändert; kombinierte
Anfragen werden vor dem Schreiben verständlich abgewiesen. Erst der neuere gemeldete Gerätezustand bestätigt eine Änderung.
Ein unklarer Ausgang führt zu keiner automatischen Wiederholung. Veraltete
Messwerte, gespeicherte Sensormeldungen und fehlende Angaben werden nicht als
frischer Zustand dargestellt. Nach einem Wiederverbinden der Integration
bleiben die letzten Werte innerhalb ihrer Altersgrenzen sichtbar; nach einer
Geräteentfernung oder einem erneut hinzugefügten Gerät müssen die benötigten
Daten erneut eintreffen. Namen, IDs und deaktivierte Entitäten bleiben erhalten.

Diagnose-Schema 22 ergänzt nur Familien- und Aktualitätszähler ohne
Anlagenwerte, Gerätekennungen oder zusätzliche Anfragen.

Ich habe beide Familien mit synthetischen Daten, den echten
Home-Assistant-Plattformen und einem lokalen MQTT-Testserver geprüft.
Geräteprüfungen stehen für beide Familien aus. Das beim
vorherigen Verbindungstest genutzte Konto meldete keine entsprechenden Geräte. Die tatsächlichen
Metadaten, Meldeintervalle, Temperaturen und Schreibwirkungen müssen noch an
realen Geräten bestätigt werden.
