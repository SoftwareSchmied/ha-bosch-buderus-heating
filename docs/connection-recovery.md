# Connection recovery and sign-in lifecycle

## English

The corrections below are included in stable **0.8.2** and were introduced
in **0.8.2-beta.7** through PR #86. They are not
included in the older 0.8.2-beta.6 or 0.8.1 packages.

I reviewed token rotation, temporary connection failures and reauthentication
following [issue #49](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/49).
That report contains an earlier rejected refresh token and a later outage that
also affected MyBuderus. The available evidence does not establish a common
cause or prove that the corrections below explain either incident.

### Changes

- Concurrent callers share one pending refresh, including its failure result.
  Cancelling a caller does not cancel the refresh itself. If the server has
  rotated the credential, the completed response is still saved. Following
  callers wait for that persistence step before receiving the token.
- Setup retries and explicit reloads reuse the existing token manager when the
  stored credentials still match, including reconfiguration of an unloaded
  entry. Authentication state survives Home Assistant removing `runtime_data`
  during unload. An unfinished refresh is not replaced by a second refresh
  using the same credential. Removing the entry clears its account memory.
- A rejected refresh token is not repeatedly submitted by other callers of
  that manager. A new interactive sign-in creates a new manager. A temporary
  connection or service failure remains eligible for a later refresh attempt.
- An OAuth request limit activates the existing shared PointT account pause.
  The pause also applies during setup retries, with or without retained runtime
  data. The reported delay is honored in full, from seconds or an HTTP date,
  with a minimum of 60 seconds and a safety bound of 24 hours. Without a usable
  value, 300 seconds apply. The pause survives Home Assistant restarts for the
  same saved cloud access; see [cloud pauses](cloud-pauses.md).
- A delayed refresh from a previous sign-in cannot overwrite replacement
  credentials or update a removed config entry.
- The config entry's update listener schedules reloads for replacement
  credentials, changed gateway/device selection, brand or polling profile.
  Ordinary token rotation and notification preferences do not reload it.
  Explicit unchanged reconfiguration still runs rediscovery. Repair flows
  schedule one reload, avoiding a second reload from the listener.

I replaced the deprecated reload helper behind the Home Assistant 2026.12
warning. Tests verify the absence of that warning on the current test version;
this is not a claim of complete compatibility with an unreleased HA version.

### Recovery and request cost

I retained the existing polling intervals, bounded read retries, per-resource
pauses and gateway circuit breaker. A synthetic HTTP fixture exercises both
503 responses and dropped connections: three failed polling cycles produce
six HTTP attempts, the five-minute circuit-breaker pause produces none, and
one subsequent successful request restores the same resource without a reload
or reauthentication. These counts describe that fixture, not every installation.

Concurrent expired-token calls share one OAuth exchange. The changes add no
periodic requests or extra discovery pass during ordinary rotation. OAuth
request limits now suppress repeated setup attempts; an explicit configuration
change still causes its intended discovery. No appliance writes are performed
by these recovery tests or by connection recovery itself.

### Connection state and test

Settings → Connections lists every access with its state, for example
"Cloud access (1): connected", "… sign-in required", "… paused until 14:20",
"… not reachable" or "… disabled". Opening an access shows the last successful
request and its polling profile, for the LAN access also the values available
locally, the target interval and whether local values are used. All of this
comes from what the running connections already know; opening the dialog never
starts a request.

These states, the description and the test result are built on the Home
Assistant server and therefore use its server language (Settings → System →
General), like entity names. If your user profile uses a different language,
the rest of the dialog follows your profile while these texts stay in the
server language.

"Test connection now" reads once on explicit request: the cloud access reads
the gateway list, the LAN access reads its system information and checks that
the same gateway answers. During a pause, or when a sign-in or a new setup is
already known to be needed, nothing is sent and the dialog names that cause.
The result names the answer time or the cause (sign-in, not reachable,
different gateway, certificate changed, pause). Settings stay unchanged; like
any regular request, a rejected answer can start a pause or ask for a new
sign-in, and the request is counted in the diagnostics. Source changes of paired readings are logged at debug level
only, without values or identifiers.

### Remaining limits

The tests use synthetic credentials and a local HTTP server. They do not prove
long-term uptime, the cause of issue #49, or recovery of a physical gateway that
remains unreachable. A process crash, power loss or a lost server response can
still prevent receipt or durable storage of a rotated token. A genuinely invalid
refresh token still requires interactive sign-in.

If a real outage recurs, the useful evidence remains a fresh diagnostic export
before reloading, the time of the outage and whether MyBuderus is affected too.
Tokens and sign-in URLs should not be included. Issue #49 remains open pending
the corresponding physical-installation feedback.

## Deutsch

Die folgenden Korrekturen aus PR #86 sind in Stable **0.8.2** enthalten und
wurden mit **0.8.2-beta.7** eingeführt.
Die älteren Pakete 0.8.2-beta.6 und 0.8.1 enthalten sie nicht.

Ich habe Tokenwechsel, vorübergehende Verbindungsfehler und die Wiederanmeldung
anhand von [Issue #49](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/49)
geprüft. Dort sind ein früher abgewiesener Refresh-Token und ein späterer
Ausfall mit ebenfalls betroffener MyBuderus-App beschrieben. Die bisherigen
Angaben belegen weder eine gemeinsame Ursache noch, dass die folgenden
Korrekturen diese konkreten Vorfälle erklären.

### Änderungen

- Gleichzeitige Anfragen teilen eine laufende Token-Erneuerung und deren
  Fehlerergebnis. Wird eine wartende Anfrage abgebrochen, läuft die Erneuerung
  weiter. Ein empfangener neuer Token wird gespeichert, bevor nachfolgende
  Anfragen ihn erhalten.
- Einrichtungswiederholungen und bewusstes Neuladen verwenden bei unveränderten
  Anmeldedaten dieselbe Tokenverwaltung. Das gilt auch beim Neukonfigurieren
  eines entladenen Eintrags. Die Verwaltung bleibt erhalten, wenn Home Assistant
  die Laufzeitdaten beim Entladen entfernt. Eine laufende Erneuerung wird nicht
  durch eine zweite Anfrage mit demselben Refresh-Token ersetzt. Das Entfernen
  des Integrationseintrags löscht dessen zwischengespeicherte Kontodaten.
- Ein abgewiesener Refresh-Token wird von dieser Tokenverwaltung nicht erneut
  gesendet. Eine neue Anmeldung erzeugt eine neue Verwaltung. Nach einem
  vorübergehenden Verbindungs- oder Dienstfehler bleibt ein späterer Versuch
  möglich.
- Ein Anfragelimit bei der Anmeldung aktiviert die bestehende gemeinsame
  PointT-Pause. Sie gilt auch bei Einrichtungswiederholungen und bleibt ohne
  zwischengespeicherte Laufzeitdaten erhalten. Die gemeldete Wartezeit wird als
  Sekundenzahl oder HTTP-Datum vollständig eingehalten, mindestens 60 Sekunden
  und höchstens 24 Stunden. Ohne verwertbare Angabe gelten 300 Sekunden. Die
  Pause bleibt für denselben gespeicherten Cloud-Zugang über Neustarts erhalten.
- Eine verspätete Antwort einer früheren Anmeldung überschreibt keine neuen
  Anmeldedaten und aktualisiert keinen bereits entfernten Integrationseintrag.
- Neue Anmeldedaten, geänderte Geräteauswahl, Marke oder Abfrageprofil lösen
  das Neuladen über den vorgesehenen Änderungslistener aus. Normale Tokenwechsel
  und Benachrichtigungsoptionen führen zu keinem Neuladen. Bewusst bestätigtes
  Neukonfigurieren startet auch ohne Änderungen eine Discovery. Reparaturen
  planen nur einen Neuladevorgang.

Die bisherige Warnung für Home Assistant 2026.12 ist im Test behoben. Das ist
keine vollständige Kompatibilitätsbestätigung für eine noch unveröffentlichte
Home-Assistant-Version.

### Wiederherstellung und Abfragelast

Die regulären Abfrageintervalle, begrenzten Leseversuche, Ressourcenpausen und
die Schutzpause nach wiederholten Gateway-Fehlern bleiben erhalten. Im lokalen
HTTP-Test führen sowohl 503-Antworten als auch Verbindungsabbrüche nach drei
fehlgeschlagenen Zyklen mit insgesamt sechs HTTP-Versuchen zur fünfminütigen
Pause. In dieser Pause wird nichts angefragt. Eine anschließende erfolgreiche
Antwort stellt dieselbe Ressource ohne Neuladen oder Neuanmeldung wieder her.
Diese Zahlen gelten für den Testaufbau, nicht für jede Anlage.

Die Änderungen fügen keine regelmäßigen Abfragen oder Discovery-Durchläufe
beim normalen Tokenwechsel hinzu. Gleichzeitige Erneuerungen teilen eine
OAuth-Anfrage. Bei einem Anfragelimit werden weitere Einrichtungsversuche
gebremst. Eine ausdrücklich geänderte Konfiguration führt weiterhin zur
vorgesehenen Discovery. Die Wiederherstellung führt keine Anlagenbefehle aus.

### Zustand und Verbindungstest

Einstellungen → Verbindungen zeigt jeden Zugang mit seinem Zustand, zum
Beispiel „Cloud-Zugang (1): verbunden“, „… Anmeldung nötig“, „… pausiert bis
14:20“, „… nicht erreichbar“ oder „… deaktiviert“. Beim Öffnen eines Zugangs
stehen die letzte erfolgreiche Abfrage und das Abfrageprofil darüber, beim
LAN-Zugang zusätzlich die lokal verfügbaren Werte, das Zielintervall und ob
lokale Werte verwendet werden. Alles stammt aus dem, was die laufenden Zugänge
bereits wissen; das Öffnen startet keine Abfrage.

Zustände, Beschreibung und Testergebnis entstehen auf dem Home-Assistant-Server.
Sie erscheinen deshalb wie Entitätsnamen in der Serversprache (Einstellungen →
System → Allgemein). Nutzt dein Benutzerprofil eine andere Sprache, folgt der
übrige Dialog deinem Profil, diese Texte bleiben in der Serversprache.

„Verbindung jetzt testen“ liest nur auf ausdrücklichen Klick einmal: der
Cloud-Zugang die Gateway-Liste, der LAN-Zugang seine Systeminformation mit
Prüfung, dass dasselbe Gateway antwortet. Während einer Pause oder wenn eine
Anmeldung beziehungsweise Neueinrichtung bereits bekannt nötig ist, wird nichts
gesendet und der Dialog nennt diese Ursache. Das Ergebnis nennt die Antwortzeit
oder die Ursache (Anmeldung, nicht erreichbar, anderes Gateway, geändertes
Zertifikat, Pause). Einstellungen bleiben unverändert; wie bei jeder normalen
Abfrage kann eine abgelehnte Antwort eine Pause oder eine Neuanmeldung
auslösen, und die Anfrage wird in der Diagnose gezählt. Quellenwechsel gepaarter
Messwerte erscheinen nur im Debug-Protokoll, ohne Werte und Kennungen.

### Verbleibende Grenzen

Die Tests verwenden synthetische Anmeldedaten und einen lokalen HTTP-Server.
Ein Langzeitbetrieb und die Ursache der Vorfälle aus Issue #49 sind damit
nicht bestätigt. Ein dauerhaft unerreichbares Gateway kann dadurch nicht
repariert werden. Bei einem Prozessabsturz, Stromausfall oder einer verlorenen
Serverantwort kann ein neuer Token weiterhin verloren gehen. Ein tatsächlich
ungültiger Refresh-Token erfordert weiterhin eine neue Anmeldung.

Bei einem erneuten realen Ausfall helfen ein frischer Diagnoseexport vor dem
Neuladen, der Zeitpunkt und der gleichzeitige Zustand der MyBuderus-App.
Tokens und Anmeldelinks gehören nicht in diese Angaben. Issue #49 bleibt bis
zur entsprechenden Praxisrückmeldung offen.
