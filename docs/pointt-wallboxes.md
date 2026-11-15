# PointT wallboxes

## English

I added a read-only adapter in v0.8.2-beta.6 for gateways reported as
`commodule` or `commodule2`. It uses the existing PointT account and HTTPS
transport, with a separate parser for wallbox objects. It does not use the
additional-device MQTT connection. A device must be returned by the account
and selected in the integration. Retail names alone do not establish support.

The implemented scope is charge point `cp0`. I have not established discovery
of additional charge points or a complete list of compatible retail models.
The selected gateway retains its device identifier; the eight new sensors use
stable IDs. Existing heating entities and user settings are preserved.

| Reading | Interpretation |
| --- | --- |
| Charging state | Eleven known reported states, kept distinct. A vehicle pause does not prove a full battery. Unknown codes stay unknown. |
| Charging power | Last component of `telemetry.meter`, already in kW. |
| Reported charged energy | First component of `telemetry.meter`, converted from Wh to kWh once. No accumulation or long-term statistics state class. |
| Active charging time | Reported `activeChargingTime` in minutes, without local extrapolation. |
| Reported current limit | `telemetry.limit` in A. This is a limit, not measured current; the separate control **Charging current limit** changes it. |
| Charging start | Reported `chargingStart`, accepted only with an explicit UTC offset. |
| Wallbox role | `conf.wallboxMode`: standalone, leader or follower. This is not the charging strategy. |
| Hardware current limit | `info.hwCurrentLimit` in A, read-only diagnostic sensor. |

Missing, malformed, negative and non-finite numbers do not become zero. The
energy counter may drop between reports without being added to a synthetic
total. Its exact session/reset scope still needs device confirmation, so it
is not offered as a lifetime energy meter for the Energy dashboard.

I read three object endpoints under `rest/v1/cp0`: `telemetry`, `conf` and
`info`. Telemetry follows the 60-second standard or 120-second cloud-friendly
interval. Configuration is read every 5 or 10 minutes; information every 15
or 30 minutes. Home Assistant can start a scheduled poll up to one second
before the exact section deadline; that section is then still read, so a
scheduled telemetry poll is not skipped. This tolerance never ends an error or
rate-limit pause early. Setup makes at most three logical reads per wallbox.
The shared transport retains its bounded read retries, token refresh and account-wide
rate-limit brake. Diagnostics create no extra requests.

A local 403, 404, 406 or malformed object withdraws only that section. It is
retried at its regular interval. Authentication, connection, service and
rate-limit failures stop the wallbox cycle and withdraw its readings.
Authentication requests reauthentication. A temporarily unavailable wallbox
does not prevent a mixed heating account from loading. Readings expire after
twice their section interval; this measures response freshness, not a
guarantee that an upstream service has uncached device data. Polls are
serialized, and recovery updates the same entities without a restart.

Schema 26 diagnostics expose section intervals, freshness and valid/invalid
field counts. They omit readings, dates, gateway and device identifiers,
account data, labels, serial numbers and unknown text. Product and software
information are used only in the ordinary HA device information.

### Managed connection preparation

I connected the existing wallbox coordinator to the prepared version 4 connection
owner. It shares authentication, account backoff, request accounting and
concurrency with heating sources, while keeping its own section polling. A slow
wallbox does not hold heating discovery or supplementary startup reads. Source
shutdown immediately makes its readings unavailable, cancels pending polls and
rejects late replies. Replacement logins must confirm every assigned gateway,
including disabled wallboxes. Sensor identifiers remain unchanged; the
controls added later are described below.

This preparation has tests against a local HTTP server, including separate
wallboxes and mixed heating accounts. Normal version 4 setup now uses this
adapter with the managed entity platforms and dependent services. These tests
do not constitute a comparison with a physical wallbox.

### Remaining work

Not yet tested with a real wallbox. The implementation has synthetic protocol,
HTTP and Home Assistant tests; it has not been compared with a physical
wallbox. Model compatibility, displayed values, cloud response freshness and
energy resets require that comparison.

The controls described below are expected to work but are untested on a
real wallbox: they were checked only with simulated data in automated tests.
Feedback from wallbox owners is very welcome.

### Controls and additional readings (0.9.2-beta.2)

Since 0.9.2-beta.2 a wallbox can also be operated. I could not test the
functions and their rules on a wallbox: expected, untested on a real
wallbox. Every control is **disabled by
default**; enable only what you need.

| Entity | Type | What it does | Exists when |
| --- | --- | --- | --- |
| Start charging | button | `cmd/start` | the wallbox reports its software version |
| Pause charging | button | `cmd/pause` | as above |
| Charging current limit | number (A) | `cmd/limit`, 6 A up to the hardware current limit, whole amperes | the hardware current limit is 6 to 63 A |
| Boost | switch | `cmd/force/limit` with the hardware current limit, off with `cmd/force/limitReset` | software 4.0.0 or newer and `telemetry/forceActive` answers |
| Charging mode | select | `conf/chargingStrategy`: normal (`default`) or solar (`solar-eco`) | software 4.0.0, writable, an external meter is reported |
| Grid supplement for solar charging, Maximum grid supplement, Minimum solar surplus | switch, number (W) | `conf/solarSurplus/*`; 0 to 11 000 W and 0 to 5 000 W in steps of 100 W | software 5.1.0, writable, external meter |
| Prefer home battery | switch | `conf/strategy/powerOffset`: on = -100, off = 0; another value shows as unknown | software 5.0.0, writable, external meter |
| Solar filter time | number (s) | `conf/strategy/filterTime`, 5 to 600 s | as above |
| Current limiter, Power limiter, Current limit phase L1/L2/L3, Power limit | switch, number | `conf/overloadProtection/*`; phases 16 to 63 A, sent as the whole list of three; power 5 000 W up to 3 × 230 V × the meter's maximum current (43 000 W without it) | software 5.0.0, all four values writable, a connected external meter, a reported brand the app offers the function for |
| Wallbox locked | switch | `conf/locked` | writable |
| Display name | text | `conf/label`, 1 to 32 printable characters | writable |
| Electricity price, Feed-in tariff | number | `conf/price`, `conf/feedinPrice`, 0 to 100 in steps of 0.01 | writable; the feed-in tariff with software 5.0.0 and a connected meter |

A reported range or step of a value (`minValue`, `maxValue`, `stepSize`)
narrows the ranges above; a value outside or between the steps is refused,
never rounded.

Additional readings: **External meter** (disconnected, connected, error;
enabled), **Limited by** (the reported limiting source; enabled), **Energy
manager** (enabled), **Grid-friendly charging configured** and **active**,
**Relays available** and **Authentication required** (enabled, from the
objects already read), and, disabled by default, **Boost active**,
**Grid-friendly input active**, **External meter maximum current** and
**Currency symbol**. All are diagnostic entities; unknown codes stay
unknown.

**Rules for every change:**

- Nothing is sent while the cloud is paused, rate limited or unreachable,
  or while sign-in is needed.
- Right before sending, the integration reads the wallbox again
  (`telemetry`, `conf`, `info` and the value itself). If the state no
  longer allows the change, nothing is sent.
- A setting is written only when the wallbox reports it `writeable`, not
  when it reports `available: false`, and not when it reports `used:
  false`.
- Exactly one request per action, never repeated, also not after a timeout.
  The integration then reads every two seconds for up to twenty seconds. If
  the new state does not show, the action is reported as not confirmed; the
  wallbox may still apply it.
- Commands keep five seconds apart. No change runs
  while the wallbox is being read.
- Start is offered only while the vehicle is plugged in and waiting
  (`Preparing`, paused by the vehicle, or paused by the station for a
  limiting source the app would also end); pause only while charging.
- A follower in a load-management group takes no command; a leader only
  start, pause and the charging current. While an energy manager controls
  the wallbox, current, boost, charging mode, solar, overload and price
  settings are refused.
- The charging current can be changed only in normal mode, not during a
  boost and not while a group's load management limits the wallbox. Back to
  normal mode is refused during a boost: end the boost first.
- No extra steps happen on their own: no current limit is set after a
  start (below 6 A the start is refused with a message), and a power limit
  below 5 000 W is not raised automatically.
- Access control is never written: `conf/auth`, RFID cards and their
  teach-in are not part of the integration, and the charge log and energy
  history are not read (personal data).

**Boost warning:** boost charges with the full hardware current limit (up
to about 11 kW or more). Without overload protection, this may exceed the
reserve of the house connection. Enable the boost switch only if the
installation is designed for it.

**Authentication:** a wallbox that requires authentication (`conf.auth`
on) starts only after an `authenticate` command. The integration sends it
only when allowed under **Settings → Wallbox** (off by default). With
software 5.1.0 or newer a start then sends `authenticate` followed by
`start`; with older software it sends `start` alone, as the manufacturer's
app does. Without a known software version nothing is sent. If the
authentication is refused or unclear, no start is sent. The
optional authentication name is the name the vendor app uses; it stays in
the entry options and never appears in diagnostics.

**Single values and requests:** after setup the integration asks each
single value once (at most four per polling cycle) to learn whether it
exists and is writable. A value answered with 403, 404 or 406 is not asked
again until the wallbox reports new software or the integration starts
again. Afterwards only values that an enabled entity shows are read again,
in the configuration interval (5 or 10 minutes). With the default
entities that is only the external meter state.

The diagnostics list the single values by path (known, writable, refused,
polled), whether the firmware is known, and counters of the write
outcomes, never their values, names, labels, brands or prices.

## Deutsch

Ich habe in v0.8.2-beta.6 eine Anbindung mit acht Anzeigen für
die gemeldeten Gatewaytypen `commodule` und `commodule2` ergänzt. Sie nutzt den
vorhandenen PointT-Zugang mit einem eigenen Parser für Wallbox-Antworten.
Das Gerät muss im Konto gemeldet und in der Integration ausgewählt sein.
Ein Verkaufsname allein reicht für die Zuordnung nicht aus. Der zusätzliche
MQTT-Zugang wird hierfür nicht verwendet.

Der Umfang ist auf `cp0` begrenzt. Weitere Ladepunkte und eine vollständige
Liste kompatibler Verkaufsmodelle sind noch nicht belegt. Die Gerätekennung
bleibt erhalten. Die neuen Anzeigen haben stabile IDs; bestehende
Heizungsentitäten und Benutzereinstellungen bleiben bestehen.

| Anzeige | Bedeutung |
| --- | --- |
| Ladezustand | Elf bekannte Zustände bleiben getrennt. Eine Pause durch das Fahrzeug bedeutet nicht automatisch einen vollen Akku. Unbekannte Codes bleiben unbekannt. |
| Ladeleistung | Letzter Bestandteil von `telemetry.meter`, bereits in kW. |
| Gemeldete geladene Energie | Erster Bestandteil von `telemetry.meter`, einmal von Wh nach kWh umgerechnet. Keine Aufsummierung und keine Statistikklasse für Langzeitwerte. |
| Aktive Ladezeit | Gemeldete Minuten aus `activeChargingTime`, ohne eigene Hochrechnung. |
| Gemeldete Stromgrenze | `telemetry.limit` in A. Eine Grenze, kein gemessener Strom; geändert wird sie über die eigene Entität **Ladestrom-Grenze**. |
| Ladebeginn | `chargingStart`, nur mit eindeutigem UTC-Versatz angenommen. |
| Rolle im Wallbox-Verbund | `conf.wallboxMode`: eigenständig, führend oder folgend. Keine Ladestrategie. |
| Hardware-Stromgrenze | `info.hwCurrentLimit` in A als Diagnoseanzeige. |

Fehlende, ungültige, negative oder nicht endliche Zahlen werden nicht als
null dargestellt. Ein sinkender Energiewert wird nicht zu einem eigenen
Gesamtzähler addiert. Wann genau der Wert zurückgesetzt wird, muss noch am
Gerät geprüft werden. Deshalb ist er kein Gesamtverbrauchszähler für das
Energie-Dashboard.

Die drei Objektabfragen unter `rest/v1/cp0` betreffen `telemetry`, `conf` und
`info`. Telemetrie wird je nach Abfrageprofil alle 60 oder 120 Sekunden
gelesen, Konfiguration alle 5 oder 10 Minuten und Geräteinformation alle 15
oder 30 Minuten. Startet Home Assistant einen geplanten Abruf bis zu einer
Sekunde vor dem genauen Termin, wird der Abschnitt trotzdem gelesen; ein
geplanter Telemetrieabruf fällt dadurch nicht aus. Fehler- und
Anfragelimitpausen enden dadurch nicht früher. Beim Start gibt es höchstens drei
logische Abfragen pro Wallbox. Begrenzte HTTP-Lesewiederholungen, Token-Erneuerung und die gemeinsame
Kontosperre bei Anfragelimits bleiben erhalten. Die Diagnose fragt nichts
zusätzlich ab.

403, 404, 406 und fehlerhafte Objekte betreffen nur ihren Abschnitt. Beim
nächsten regulären Termin wird dieser erneut geprüft. Anmelde-, Verbindungs-,
Dienst- und Anfragelimitfehler beenden den Wallbox-Durchlauf und machen seine
Anzeigen unverfügbar. Anmeldefehler lösen eine erneute Anmeldung aus. Eine
vorübergehend nicht erreichbare Wallbox verhindert nicht das Laden eines
gemischten Kontos mit Heizung. Nach zweimal dem jeweiligen Abfrageintervall
gelten Antworten als veraltet. Das beschreibt den Empfang der Antwort und
beweist nicht, dass der vorgeschaltete Dienst ungepufferte Gerätedaten liefert.
Abfragen laufen nacheinander. Die Anzeigen erholen sich mit denselben IDs.

Diagnose-Schema 26 enthält Intervalle, Aktualität und Zähler gültiger oder
ungültiger Felder. Messwerte, Datumsangaben, Kennungen, Kontodaten, Namen,
Seriennummern und unbekannte Texte bleiben ausgeschlossen. Produkt und
Software erscheinen ausschließlich in der gewöhnlichen HA-Geräteinformation.

### Noch offen

Mit einer echten Wallbox ist das noch nicht geprüft. Protokoll, HTTP-Zugang und
HA-Einbindung werden mit synthetischen Antworten geprüft. Ein Geräteabgleich
hat nicht stattgefunden. Modellkompatibilität, Anzeigen, Aktualität der
Cloud-Antworten und Rücksetzungen der Energie benötigen Praxisbestätigung.

Die unten beschriebenen Bedienfunktionen sollten funktionieren, sind an
einer echten Wallbox aber ungetestet: Sie wurden nur mit simulierten Daten in
automatisierten Tests geprüft. Über Rückmeldungen von Wallbox-Besitzern freue
ich mich.

### Bedienung und zusätzliche Anzeigen (0.9.2-beta.2)

Seit 0.9.2-beta.2 lässt sich eine Wallbox auch bedienen. Funktionen und
Regeln konnte ich an keiner Wallbox prüfen: erwartet, ungetestet an einer
echten Wallbox. Alle Bedienelemente
sind **standardmäßig deaktiviert**; bitte nur aktivieren, was gebraucht wird.

| Entität | Typ | Wirkung | Entsteht, wenn |
| --- | --- | --- | --- |
| Ladevorgang starten | Taste | `cmd/start` | die Wallbox ihre Softwareversion meldet |
| Ladevorgang pausieren | Taste | `cmd/pause` | wie oben |
| Ladestrom-Grenze | Zahl (A) | `cmd/limit`, 6 A bis zur Hardware-Stromgrenze, ganze Ampere | die Hardware-Stromgrenze 6 bis 63 A beträgt |
| Boost | Schalter | `cmd/force/limit` mit der Hardware-Stromgrenze, aus mit `cmd/force/limitReset` | Software 4.0.0 oder neuer und `telemetry/forceActive` antwortet |
| Ladebetriebsart | Auswahl | `conf/chargingStrategy`: normal (`default`) oder Solar (`solar-eco`) | Software 4.0.0, schreibbar, ein externer Zähler wird gemeldet |
| Netzzuschuss beim Solarladen, Höchstleistung Netzzuschuss, Mindest-Solarüberschuss | Schalter, Zahl (W) | `conf/solarSurplus/*`; 0 bis 11 000 W und 0 bis 5 000 W in Schritten von 100 W | Software 5.1.0, schreibbar, externer Zähler |
| Heimspeicher bevorzugen | Schalter | `conf/strategy/powerOffset`: an = -100, aus = 0; andere Werte erscheinen als unbekannt | Software 5.0.0, schreibbar, externer Zähler |
| Solar-Filterzeit | Zahl (s) | `conf/strategy/filterTime`, 5 bis 600 s | wie oben |
| Stromgrenzer, Leistungsgrenzer, Stromgrenze Phase L1/L2/L3, Leistungsgrenze | Schalter, Zahl | `conf/overloadProtection/*`; Phasen 16 bis 63 A, immer als ganze Liste aus drei Werten gesendet; Leistung 5 000 W bis 3 × 230 V × Höchststrom des Zählers (ohne Angabe 43 000 W) | Software 5.0.0, alle vier Werte schreibbar, ein verbundener externer Zähler, eine gemeldete Marke, für die die App die Funktion anbietet |
| Wallbox gesperrt | Schalter | `conf/locked` | schreibbar |
| Anzeigename | Text | `conf/label`, 1 bis 32 druckbare Zeichen | schreibbar |
| Strompreis, Einspeisevergütung | Zahl | `conf/price`, `conf/feedinPrice`, 0 bis 100 in Schritten von 0,01 | schreibbar; die Einspeisevergütung mit Software 5.0.0 und verbundenem Zähler |

Meldet ein Wert eigene Grenzen oder Schritte (`minValue`, `maxValue`,
`stepSize`), engen sie die Bereiche oben ein; Werte außerhalb oder zwischen
den Schritten werden abgelehnt, nie gerundet.

Zusätzliche Anzeigen: **Externer Zähler** (nicht verbunden, verbunden,
Fehler; aktiv), **Begrenzt durch** (die gemeldete Begrenzungsquelle; aktiv),
**Energiemanager** (aktiv), **Netzdienliches Laden eingerichtet** und
**aktiv**, **Relais vorhanden** und **Authentifizierung erforderlich**
(aktiv, aus den ohnehin gelesenen Objekten) sowie standardmäßig deaktiviert
**Boost aktiv**, **Netzdienlicher Eingang aktiv**, **Höchststrom externer
Zähler** und **Währungssymbol**. Alle sind Diagnose-Entitäten; unbekannte
Codes bleiben unbekannt.

**Regeln für jede Änderung:**

- Nichts wird gesendet, solange die Cloud pausiert, begrenzt oder nicht
  erreichbar ist oder eine Anmeldung fehlt.
- Unmittelbar vor dem Senden liest die Integration die Wallbox neu
  (`telemetry`, `conf`, `info` und den Wert selbst). Erlaubt der Zustand die
  Änderung nicht mehr, wird nichts gesendet.
- Eine Einstellung wird nur geschrieben, wenn die Wallbox sie als
  `writeable` meldet, nicht bei `available: false` und nicht bei `used: false`.
- Genau eine Anfrage je Aktion, nie wiederholt, auch nicht nach einer
  Zeitüberschreitung. Danach liest die Integration alle zwei Sekunden bis zu
  zwanzig Sekunden lang. Zeigt sich der neue Zustand nicht, wird die Aktion
  als nicht bestätigt gemeldet; die Wallbox kann sie trotzdem noch
  übernehmen.
- Zwischen zwei Befehlen liegen mindestens fünf Sekunden. Während die Wallbox gelesen wird, läuft keine Änderung.
- Starten wird nur angeboten, solange das Fahrzeug angesteckt ist und wartet
  (`Preparing`, vom Fahrzeug pausiert oder von der Ladestation pausiert bei
  einer Begrenzungsquelle, die auch die App beendet); Pausieren nur während
  des Ladens.
- Eine folgende Wallbox im Lastmanagement-Verbund nimmt keinen Befehl an,
  eine führende nur Start, Pause und Ladestrom. Steuert ein Energiemanager
  die Wallbox, werden Ladestrom, Boost, Ladebetriebsart sowie Solar-,
  Überlast- und Preiseinstellungen abgelehnt.
- Der Ladestrom lässt sich nur in der normalen Betriebsart ändern, nicht
  während eines Boosts und nicht, solange das Lastmanagement eines Verbunds
  die Wallbox begrenzt. Der Wechsel zurück in die normale Betriebsart wird
  während eines Boosts abgelehnt: zuerst den Boost beenden.
- Nichts geschieht zusätzlich von selbst: Nach einem Start wird keine
  Stromgrenze gesetzt (unter 6 A wird der Start mit einem Hinweis
  abgelehnt), und eine Leistungsgrenze unter 5 000 W wird nicht
  selbsttätig angehoben.
- Zugangsschutz wird nie geschrieben: `conf/auth`, RFID-Karten und ihr
  Einlernen gehören nicht zur Integration; Ladeprotokoll und
  Energieverlauf werden nicht gelesen (personenbezogene Daten).

**Warnhinweis Boost:** Boost lädt mit der vollen Hardware-Stromgrenze (bis
etwa 11 kW oder mehr). Ohne Überlastschutz kann das die Reserve des
Hausanschlusses übersteigen. Den Boost-Schalter bitte nur aktivieren, wenn
die Installation dafür ausgelegt ist.

**Authentifizierung:** Eine Wallbox, die eine Authentifizierung verlangt
(`conf.auth` an), startet erst nach einem `authenticate`-Befehl. Die
Integration sendet ihn nur, wenn das unter **Einstellungen → Wallbox**
erlaubt ist (standardmäßig aus). Ab Software 5.1.0 sendet ein Start dann
`authenticate` und danach `start`; bei älterer Software sendet er nur
`start`, wie die Hersteller-App. Ohne bekannte Softwareversion wird nichts
gesendet. Wird die
Authentifizierung abgelehnt oder bleibt sie unklar, wird kein Start
gesendet. Der optionale Authentifizierungsname ist der Name, den die
Hersteller-App verwendet; er bleibt in den Optionen des Eintrags und
erscheint nie in der Diagnose.

**Einzelwerte und Anfragen:** Nach der Einrichtung fragt die Integration
jeden Einzelwert einmal ab (höchstens vier je Abfragezyklus), um zu
erfahren, ob es ihn gibt und ob er schreibbar ist. Ein mit 403, 404 oder
406 beantworteter Wert wird erst wieder abgefragt, wenn die Wallbox neue
Software meldet oder die Integration neu startet. Danach werden nur Werte
erneut gelesen, die eine aktivierte Entität anzeigt, im
Konfigurationsintervall (5 oder 10 Minuten). Mit den Standard-Entitäten ist
das nur der Zustand des externen Zählers.

Die Diagnose nennt die Einzelwerte mit ihrem Pfad (bekannt, schreibbar,
abgelehnt, abgefragt), ob die Firmware bekannt ist, und Zähler der
Schreibergebnisse, nie Werte, Namen, Etiketten, Marken oder Preise.
