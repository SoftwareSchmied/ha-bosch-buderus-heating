# Additional cloud devices: connection foundation

I added a separate Bacon connection for devices listed by the account's
claiming service. The implementation in v0.8.2-beta.6 supplies device
selection, MQTT status and a diagnostic connection entity. Midea/RWH controls and readings are now described in the separate
[water-heater package](bacon-water-heaters.md). Beta.6 also covers
[Midea Climate Series](bacon-air-conditioners.md) and
[seven Air purifier families](bacon-air-purifiers.md) and
[three Dry dehumidifier families](bacon-dehumidifiers.md), with individual scope
and outstanding physical-device checks. Other family packages remain open.
Water heaters, air conditioners, air purifiers and dehumidifiers are not yet
advertised as fully supported.

## Setup and separation

Discovery uses the existing SingleKey ID authorization only when its granted
scope includes Bacon. During operation, both connections share the same token
manager, including serialized refresh and token persistence.

Setup offers gateways and additional devices separately. A Bacon-only account
can be configured. Only returned, explicitly selected identifiers are used.
Entity identities combine a separate namespace with a hash of account subject
and device identifier. PointT identities and settings remain unchanged.
New devices require Reconfigure and explicit selection.

Each selected device receives **Device connection**. An offline status
received on the current connection shows disconnected, also when it is the
retained status redelivered after a restart, and it does not expire. Missing
current observations or removed membership make this entity unavailable, and
so does a connection loss once the short hold has ended (see below).

## Transport contract

| Operation | Contract |
| --- | --- |
| Account devices | HTTPS GET https://claiming.euc1.bacon.bosch-tt-cw.com/v1/users/self/devices, returning a list of identifiers |
| Authentication | Bearer access token for HTTPS and the WebSocket upgrade |
| Broker | broker.euc1.bacon.bosch-tt-cw.com:443, TLS certificate validation, WebSocket /mqtt, MQTT 5 |
| MQTT credentials | Token subject as username, access token as password |
| Client identifier | 64 hexadecimal characters from SHA-256 over a random UUID's bytes |
| Subscription | users/{subject}/#, QoS 1; a reason code of 0x80 or higher is a failure |
| Device information | users/{subject}/devices/{device}/topics/info |
| Opaque metadata and sensor messages | Same device prefix with topics/meta and topics/sensor |
| Read state | Empty payload to shadows/state/get; response on shadows/state/get/accepted |
| Request change | {"state":{"desired":{...}}} to shadows/state/update |
| Confirmation | Newer accepted shadow containing the requested values in state.reported |
| Rejection | State get/update rejected topics, or MQTT/HTTP failure |

The client identifier format was checked with the real service. An arbitrary
text identifier was rejected with reason 135. The documented format connected
with the same account. No embedded application secrets are needed.

Token rotation renews the connection at once, without a pause and with the
device list of the last five minutes; diagnostics count these planned renewals
separately from reconnects. If renewals follow each other within five minutes,
the normal backoff applies. Each connection uses a fresh MQTT client without an
outgoing command queue from an earlier session. A refused MQTT sign-in (reason
134 or 135) is retried once with a refreshed token, as an HTTP 401 of the
device list is. Only a second refusal or another authentication error requests
renewed sign-in. A refused subscription counts as the connection failure
`subscription_rejected`. Connection failures back off, and so do unexpected
errors, which are logged once by their type. Quota errors wait at least 60
seconds; a longer Retry-After in seconds or as an HTTP date is honored up to
24 hours and also pauses the shared cloud access. A request blocked locally by
that shared pause reports `paused` and waits for the end of the pause without
extending it. Configuration retry dialogs enforce their wait too.

Membership and state resynchronize every five minutes. A failed periodic
device list keeps the connection and the last confirmed membership; only an
authentication failure or a changed account still ends it. During a shared
pause the state reads of that round are skipped. A newly enabled device whose
device list request fails is retried after at least one minute without
reconnecting its peers. Token checks and availability updates run every minute.
These are integration policies, not vendor limit claims. Bounds are 128 devices, 128 KiB per message, 16 JSON nesting
levels and an incoming queue of 256 messages.

## State and write rules for family adapters

The P08 foundation stores metadata and sensor objects without assigning units
or controls; P09 interprets the admitted water-heater families and P10 adds
[identified Midea Climate Series devices](bacon-air-conditioners.md). The family adapter must establish their meanings and validate
requested fields against current metadata.

The information timestamp is used only for ordering. Its epoch unit is not
established by P08. The newest information message sets the online or
offline status; an explicit offline message takes effect immediately. An
offline status cannot come with a sign of life of the device, so it stays
shown once received on the current connection, also from a retained message.
Information without a device type keeps the known model. A changed type clears
state, measurements and metadata of the previous model and requests the
device's metadata and state again without reconnecting. Devices
send information only on change and some stay silent for hours, so the
online status of the newest information message is enough for availability, as
in the HomeCom Easy app, which also takes online or offline from the stored
information only. The stored message redelivered on a new connection counts.
There is no limit on its age. The state read every five minutes and the
per-field limits (measurements one hour, reported state 15 minutes) decide how
current values are. Messages from the device itself are still recorded as the
last sign of life, but only for diagnostics: information, a new sensor sample
or a state update with newly reported fields. Retained messages, duplicate
timestamps, metadata, desired-only acknowledgements and the responses to the
integration's own state reads never count as a sign of life. Before this
change a sign of life at most one hour old was required; real units that send
nothing on their own for hours then turned unavailable although the cloud
reported them online. A membership loss or an offline message still makes the device
unavailable; a lost connection only after the short hold described below. A re-added device starts without earlier
readings. A re-added device, or one that returns to the account, takes its
online status from the redelivered information even if its timestamp is
unchanged.

The hourly token renewal reconnects at once. The devices stay available and
entities write no unavailable state, with the readings of the previous
connection, under the same rules as every other cloud family (see
[a short cloud blip](reading-validity.md#a-short-cloud-blip)): the renewal begins
the outage without counting a failure, and every failed connection after it counts
one. The devices stay shown while at most three connections in a row failed and
the outage began less than five minutes ago; the information redelivered on the
new connection ends the outage earlier. A lost connection or a server error,
request limit or pause is counted the same way. Writing is blocked while a device
is only carried over. A fourth failed connection, the five minutes, or no
information from the new connection by then makes the devices unavailable. A
rejected sign-in, a changed account, a stopped source, a rejected subscription,
a membership loss, an offline message or an unexpected error makes them
unavailable at once. Expected, not yet observed on a real installation beyond
the planned renewal.

While the connection stands and a device is selected, I report the account as
an active client with a `POST` (JSON content type, body `{}`) to
`https://claiming.euc1.bacon.bosch-tt-cw.com/v1/users/self/live`, right after
subscribing and then every 300 seconds, as HomeCom Easy does while it is open.
It uses the same token, the shared request slot and the shared cloud pause. A
429 starts the shared pause without ending the connection; a 404 or 405 stops
the report for the rest of that connection, a 401 or 403 for the rest of the
session (a 401 of the report does not renew the token); each is logged once;
a 415 (unsupported media type) is logged once as a warning and the report is
tried again; other failures never end the connection.
This report is expected to make the devices send measurements more often. It is
untested on a real unit; the effect has to be confirmed with a real device.

Sensor envelopes contain an optional deviceType and an items array. Each
item has its own integer timestamp. The newest unambiguous item extends
non-null fields in the current sample. Empty, retained and old messages do
not refresh readings. Each field retains its own observation time and
expires one hour after it was last reported. Reported state fields expire
after 15 minutes; the periodic state read every five minutes keeps them
current. After a restart, the first sample is dropped when its own time (seconds
since 1970) lies more than an hour back, so an old sample is never shown as a
current measurement.

Only the European region of the cloud service is used (the addresses above end
in `euc1`). Air conditioners registered in another region are not supported.

Shadows require integer version and timestamp fields and an object state.
A full get response replaces the reported snapshot; update responses merge
non-null reported fields. Desired-only acknowledgements preserve reported
values and do not refresh them or confirm writes. Each changed requested
field needs a newer reported observation. Duplicate updates cannot extend
its freshness. A full get can refresh unchanged fields and resolve uncertainty.
Conflicting observations of the same field at the same version are rejected.
Old and retained responses cannot confirm commands. New connections cannot
combine partial reports with cached fields from the previous connection.

Each internal write acquires a device lock, reads state and checks current
connectivity. Pending desired changes block another write. Numbers compare by
value, so 22 and 22.0 are equal; a boolean never equals a number. A mandatory family
validator checks permitted options and limits. An already reported requested
value causes no further command.

A PUBACK or accepted desired value is insufficient. A newer reported state
must match the request on the same connection. Rejection, removal, offline
status, cancellation or timeout leaves the outcome unconfirmed. No blind
retransmission follows. Except after a cancellation, a plain state read follows
at once while the connection still holds the device; its full response clears
the uncertainty, as any regular read does. Family adapters
must check state generation and the uncertainty flag before displaying
confirmed values.

No server compare-and-swap or client transaction token has been established.
The check confirms convergence to the requested state, not exclusive ownership
of the change. An external controller can still intervene. No generic Home
Assistant write service is exposed.

## Diagnostics and validation

Schema 21 adds aggregate connection, availability, received-state and metadata
counts (0.9.2 adds `live_report` and `availability` and the 90-second `renewal_grace`,
which is the `cloud_hold` of every cloud family since 0.9.3, see
[diagnostics](diagnostics.md)), error categories and unconfirmed-command counts. Planned token renewals
are counted as `renewals`, apart from `reconnects`. It exports no token,
subject, identifier, model, sensor contents, network information or device name.
Diagnostics cause no cloud requests.

On 24 October 2026 I checked the actual account device-list request
(HTTP 200, empty list), MQTT authentication, subscription and clean disconnect.
I published no MQTT messages. The account has no Bacon device, so real-device
messages and physical writes remain untested.

Automated tests exercise real HTTP requests, the real aiomqtt MQTT 5 codec
against a local WebSocket broker, mixed and Bacon-only setup, token sharing,
identity preservation, redaction, ordering, timeouts and write confirmation.
All device examples in those tests are synthetic.

## Managed connection preparation

The prepared version 4 owner now uses the same MQTT and model contracts through
separate device bindings. One `CloudConnection` owns shared authentication and
account limits; `BaconPool` owns MQTT while any device binding is enabled.
`BaconDeviceAccess` gives each consumer access to its exact current device.
The existing entities accept the `BaconAccess` protocol for both this view and
legacy version 3 accounts.

Disabling one device retires its view, blocks further sends and cancels its
waiting reads and writes. Peer devices keep the connection. Reenabling replaces
the view and requests fresh metadata through a device subscription, even when
disable and enable overlap. Known version and timestamp boundaries reject older
messages. They do not establish causality for unversioned messages arriving on
the shared connection. The existing reported-state confirmation remains the
write contract.

Closing the last device stops MQTT without stopping PointT. Service failures
retry MQTT independently; authentication failures request sign-in for their
shared cloud connection. Before a new login replaces the current one, all
assigned gateways and devices must be verified, including disabled bindings.

Automated tests use actual HTTP and MQTT 5 WebSocket transport against local
servers for the existing warm-water, air-conditioner, purifier and dehumidifier
families. They cover confirmed commands, cancellation, overlapping selection,
membership loss, reconnection, token rotation, replacement login and shutdown.
Native HA platform tests additionally replace the guarded device view and verify
stable entity IDs, independent peers and confirmed commands through HA services
for five synthetic family examples. They do not establish compatibility with
untested appliances. Managed diagnostics now include each connection's
additional-cloud diagnostics separately, without exporting account or device
identities. Normal setup now uses entry version 4 after validated migration.
Local product access remains separate and disabled.

## Deutsch

Ich habe eine getrennte Bacon-Verbindung für weitere Cloud-Geräte ergänzt.
Die Umsetzung in v0.8.2-beta.6 bietet Geräteauswahl, Verbindung
und **Geräteverbindung** als Diagnoseentität. Midea/RWH-Anzeigen und Bedienung
sind inzwischen im [Warmwasserpaket](bacon-water-heaters.md) ergänzt.
Weitere Familien bleiben offen. Warmwassergeräte, Klimageräte, Luftreiniger
und Luftentfeuchter sind dadurch noch nicht vollständig unterstützt.

Ich verwende die vorhandene SingleKey-ID-Anmeldung mit passender Berechtigung
und dieselbe gesicherte Token-Erneuerung. Gateways und weitere Geräte bleiben
getrennt. Auch ein Konto ausschließlich mit weiteren Geräten ist möglich.
Nur gemeldete und ausdrücklich ausgewählte Kennungen werden übernommen.
Bestehende PointT-Entitäten behalten ihre Identität. Weitere Geräte können
über Neukonfigurieren ausgewählt werden.

Die Verbindung nutzt MQTT 5 über einen TLS-gesicherten WebSocket. Die
Verbindungskennung besteht aus 64 hexadezimalen Zeichen, gebildet aus
SHA-256 über zufällige UUID-Bytes. Nach einem Abbruch wird ein neuer Client
angelegt. Alte ausgehende Befehle werden nicht übernommen. Bei Anmeldefehlern
wird eine neue Anmeldung angefordert. Anfragelimits und Wartezeiten gelten
auch bei Fehlern. Geräteliste und Zustand werden alle fünf Minuten abgeglichen.
Scheitert dieser Abgleich der Geräteliste vorübergehend, bleibt die Verbindung
mit den zuletzt bestätigten Geräten bestehen; nur ein Anmeldefehler oder ein
anderes Konto beendet sie. Lehnt der Broker die Anmeldung ab (Code 134 oder
135), versuche ich es zuerst einmal mit einem frisch erneuerten Token. Ein
abgelehntes Abonnement gilt als Verbindungsfehler und wird mit wachsender
Wartezeit wiederholt, ebenso ein unerwarteter Fehler, dessen Typ einmal im
Protokoll erscheint. Eine lokale Sperre während einer geteilten Cloud-Pause
verlängert diese Pause nicht mehr. Die stündliche Token-Erneuerung verbindet
sofort neu, ohne Pause und mit der Geräteliste der letzten fünf Minuten.
Die Grenzen von 128 Geräten, 128 KiB pro Nachricht, 16 JSON-Ebenen und
256 eingehenden Nachrichten sind eigene Schutzgrenzen.

Den Online- oder Offline-Status liefert die neueste Informationsmeldung.
Eine ausdrückliche Offline-Meldung wirkt sofort. Eine auf der aktuellen
Verbindung empfangene Offline-Meldung zeigt das Gerät als getrennt, auch nach
einem Neustart und ohne Ablauf nach einer Stunde. Eine Informationsmeldung ohne
Gerätetyp lässt das bekannte Modell unverändert; nach einem echten
Modellwechsel fordere ich Metadaten und Zustand ohne Neuverbindung erneut an.
Geräte senden Informationen nur bei Änderungen, manche stundenlang nichts. Für
die Verfügbarkeit genügt deshalb der Online-Status der neuesten
Informationsmeldung, wie in der HomeCom-Easy-App; die beim Wiederverbinden erneut
zugestellte gespeicherte Meldung zählt, ohne Altersgrenze. Wie aktuell Werte sind,
bestimmen die Zustandsabfrage alle fünf Minuten und die Fristen je Wert (Messwerte
eine Stunde, gemeldeter Zustand 15 Minuten). Meldungen des Geräts selbst
speichere ich weiter als letztes Lebenszeichen, aber nur für die Diagnose. Antworten
auf meine eigenen Zustandsabfragen, gespeicherte Meldungen und Metadaten zählen
dabei nicht. Früher verlangte ich ein höchstens eine Stunde altes Lebenszeichen;
reale Geräte, die stundenlang nichts von selbst senden, wurden dadurch nicht
verfügbar, obwohl die Cloud sie als online führte. Geräteentfernung oder eine
Offline-Meldung machen das Gerät weiterhin nicht verfügbar, ein
Verbindungsverlust erst nach der unten beschriebenen kurzen Haltezeit. Nach einer
Geräteentfernung oder einem erneut hinzugefügten Gerät beginnt die Anzeige ohne
frühere Werte. Die Einheit des Informationszeitstempels ist noch nicht belegt;
ich verwende ihn nur für die Reihenfolge.

Die stündliche Token-Erneuerung verbindet sofort neu. Die Geräte bleiben mit den
Werten der vorherigen Verbindung verfügbar und die Entitäten schreiben keinen
Zustand „nicht verfügbar“, nach denselben Regeln wie jede andere Cloud-Familie
(siehe [kurze Cloud-Störung](reading-validity.md#a-short-cloud-blip)): Die
Erneuerung beginnt den Ausfall, ohne einen Fehlschlag zu zählen, jede danach
fehlgeschlagene Verbindung zählt einen. Die Geräte bleiben angezeigt, solange
höchstens drei Verbindungen in Folge scheiterten und der Ausfall weniger als
fünf Minuten zurückliegt; die erneut zugestellte Informationsmeldung beendet den
Ausfall früher. Ein Verbindungsverlust oder ein Serverfehler, Anfragelimit oder
eine Pause zählt genauso. Schreiben ist gesperrt, solange ein Gerät nur
übernommen ist. Eine vierte fehlgeschlagene Verbindung, die fünf Minuten oder
fehlende Information der neuen Verbindung bis dahin machen die Geräte nicht
verfügbar. Abgelehnte Anmeldung, geändertes Konto, gestoppte Quelle, abgelehntes
Abonnement, Geräteentfernung, Offline-Meldung oder ein unerwarteter Fehler machen
sie sofort nicht verfügbar. Erwartet, an einer echten Anlage außer der geplanten
Erneuerung noch nicht beobachtet.

Solange die Verbindung steht und ein Gerät ausgewählt ist, melde ich das Konto
als aktiven Client mit einem `POST` (JSON-Inhaltstyp, Körper `{}`) an
`https://claiming.euc1.bacon.bosch-tt-cw.com/v1/users/self/live`, direkt nach dem
Abonnement und dann alle 300 Sekunden, wie HomeCom Easy bei geöffneter App. Dabei
gelten dasselbe Token, der gemeinsame Anfrageplatz und die gemeinsame Cloud-Pause.
Ein 401 erneuert das Token einmal, ein 429 startet die gemeinsame Pause ohne die
Verbindung zu beenden, ein 404, 405, 403 oder ein zweites 401 stoppt die Meldung
für den Rest dieser Verbindung (einmal im Protokoll vermerkt); andere Fehler
beenden die Verbindung nie; ein 415 (nicht unterstützter Medientyp) wird einmal als
Warnung vermerkt und die Meldung später erneut versucht. Erwartet wird, dass die Geräte dadurch Messwerte
häufiger senden. Das ist auf einem echten Gerät ungetestet und muss dort
bestätigt werden.

Nach einem Neustart verwerfe ich die erste Messprobe, wenn ihre eigene Zeit
(Sekunden seit 1970) mehr als eine Stunde zurückliegt; eine alte Probe erscheint
so nie als aktuelle Messung. Genutzt wird nur die europäische Region des
Cloud-Dienstes (die Adressen oben enden auf `euc1`); in einer anderen Region
registrierte Klimageräte werden nicht unterstützt.

Die interne Schreibgrundlage prüft Zustand, offene Änderungen und eine
verpflichtende Prüfung durch den jeweiligen Geräteadapter. Erst ein neuerer
gemeldeter Zustand mit den gewünschten Werten bestätigt das Ergebnis.
Eine reine Empfangsbestätigung genügt nicht. Bei unklarem Ausgang erfolgt
kein zweiter Befehl, sondern sofort eine reine Zustandsabfrage, deren Antwort
die Unsicherheit auflöst. Zahlen vergleiche ich dabei nach ihrem Wert, 22 und
22.0 sind also gleich. Ohne belegte serverseitige Konfliktsperre können
gleichzeitige Änderungen durch andere Bedienungen nicht vollständig
ausgeschlossen werden. Es gibt keinen allgemeinen Schreibdienst.
Die Adapter müssen Verbindungsstand und unbestätigte Ergebnisse beachten.

Diagnose-Schema 21 ergänzt ausschließlich Zähler und Fehlerkategorien.
Zugangsdaten, Gerätekennungen, Namen, Netzwerkdaten und Anlagenwerte bleiben
ausgeschlossen. Die Diagnose erzeugt keine zusätzlichen Anfragen.

Am 24.10.2026 habe ich die Geräteliste mit HTTP 200 und anschließend Anmeldung,
Abonnement und Trennung am echten Broker geprüft. Die Geräteliste war leer.
Ich habe dabei keine MQTT-Nachricht veröffentlicht. Reale Gerätemeldungen und
Schreibtests stehen aus. Die automatisierten Geräteprüfungen verwenden
ausdrücklich synthetische Daten.
