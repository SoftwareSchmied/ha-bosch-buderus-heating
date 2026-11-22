# Lokaler Zugang für iCom-Gateways (Beta)

Beta: Soll funktionieren, ist aber noch nicht an einem echten Gateway getestet.
Ich suche Tester (siehe [Rückmeldung](#rückmeldung)).

iCom-Gateways (KM200, KM100, KM50, IP inside, MB LAN 2 und MB LANi, das
IVT/Bosch-IP-Modul) lassen sich neben der Cloud auch aus dem Heimnetz lesen.
Der lokale Zugang liest über die verschlüsselte Weboberfläche des Gateways auf
Port 80 und liefert Werte wie jeder andere lokale Zugang; er ersetzt die Cloud
nicht. Geschrieben wird weiterhin über die Cloud, mit frischem Lesen und
Rücklesen wie bisher. Englische Fassung: [icom-local-access.md](icom-local-access.md).

## Voraussetzungen

- Das Gateway ist auch mit der Bosch- oder Buderus-App verbunden und in dieser
  Integration über die Cloud-Verbindung eingerichtet. Der lokale Zugang gehört
  immer zu einer solchen Anlage; eine Anlage, die nur lokal existiert, wird für
  iCom-Gateways nicht angeboten.
- Das **Gateway-Passwort** vom Aufkleber auf dem Gerät (Bindestriche und
  Leerzeichen werden ignoriert) und das **private Passwort** (das persönliche
  Passwort des Gateways, 4 bis 16 Zeichen, Groß- und Kleinschreibung zählt):
  das, das du dafür beim Koppeln in der App oder in der alten Geräte-App des
  Gateways festgelegt hast (zum Beispiel MyDevice). Wurde nie eines festgelegt
  oder wurde es zurückgesetzt, lässt sich der lokale Zugang nicht einrichten;
  der Dialog meldet dann, dass auf dem Gateway kein privates Passwort
  gespeichert ist. Lege es einmal in der alten Geräte-App fest, mit dem Handy im
  selben WLAN wie das Gateway, und verwende hier dasselbe. Die häufigste Ursache
  für „ließ sich nicht entschlüsseln“ ist ein privates Passwort, das vom
  Passwort des Gateways abweicht: Bist du unsicher, welches das ist, lege es auf
  dieselbe Weise neu fest. Ein am Display des Heizgeräts eingegebenes Passwort
  beweist dazu nichts.
- Das private Passwort wird genau so verwendet, wie du es eingibst. Lässt sich
  die Antwort des Gateways damit nicht entschlüsseln, probiert die Einrichtung
  dieselbe Antwort auch ohne Leerzeichen am Anfang und Ende des Passworts (ohne
  weitere Anfrage); passt das, wird diese Form gespeichert. Die Diagnose zeigt,
  welche Form gepasst hat (`private_password.variant`: `exact` oder `trimmed`).
- Die Adresse des Gateways im Heimnetz (IP-Adresse oder Hostname). Eine feste
  Adresse (Reservierung im Router) erspart dir eine erneute Einrichtung.

## Einrichten

1. Öffne die Integration, **Konfigurieren**, dann **Verbindungen**.
2. Wähle **Lokalen Zugang einrichten (iCom-Gateway)**.
3. Wähle die Anlage, falls mehrere in Frage kommen.
4. Gib Adresse, Gateway-Passwort und privates Passwort ein.

Die Einrichtung liest einmal die eigene Kennung des Gateways. Ist sie die
Gateway-Kennung, die die Cloud für diese Anlage kennt, wird das Gateway daran
erkannt. Ein falsches Passwort wird gesondert genannt.

Wie die eigene Kennung eines Gateways mit der der Cloud zusammenhängt, ist nicht
für jedes Gateway bekannt. Meldet das Gateway deshalb keine brauchbare Kennung
(gar keine, eine leere oder einen Platzhalter wie -1) oder eine andere als die
der Cloud, vergleicht die Einrichtung, was das Gateway antwortet, mit dem, was
die Cloud gerade für dieselben Pfade gelesen hat: Firmware- und
Hardware-Version, Typ, Marke, Außentemperatur (innerhalb von 2 K, Cloud-Wert
höchstens 15 Minuten alt), Systemdruck und Anzahl der Starts. Stimmen mindestens
zwei Werte überein, mindestens einer davon ein Livewert (Temperatur, Druck oder
Starts), wird das Gateway an seinen Werten erkannt; spätere Prüfungen lesen dann
einen gespeicherten Versionswert statt der Kennung. Ändert sich dieser Wert
später (zum Beispiel nach einem Firmware-Update), entscheiden wieder die Werte
wie bei der Einrichtung; bestätigen sie das Gateway, wird der gespeicherte Wert
erneuert (der Zugang startet damit einmal neu). Nur ein eindeutiger
Widerspruch (andere Version, anderer Typ oder andere Marke oder ein weit
abweichender Livewert) stoppt die Einrichtung als anderes Gateway. Stimmt zu
wenig überein (zum Beispiel, weil die Cloud noch keine aktuellen Werte hat), wird
der Zugang **unbestätigt** eingerichtet: Er liest das Gateway ohne
Kennungsprüfung, eine fehlende oder seltsame Kennung hält ihn also nie an. Der
verwendete Weg steht in der Diagnose (`identity_method`: `uuid`, `values`
oder `unverified`), zusammen damit, was das Gateway als Kennung gemeldet hat
(nur Art, Länge und ob sie nur Ziffern, Hex-Ziffern oder andere Zeichen enthält,
nie ein Zeichen der Kennung), und wie viele Werte übereinstimmten (`setup_check`). Adresse oder
Passwörter änderst du später, indem du den Zugang unter **Verbindungen** öffnest
und **iCom-Zugangsdaten ändern** wählst.

Die lokalen Werte bleiben nach der Einrichtung **ausgeschaltet**. Der Zugang
liest und prüft das Gateway, aber die vorhandenen Entitäten behalten die
Cloud-Werte, bis du unter dem Zugang **Lokale Werte für vorhandene Entitäten
verwenden** einschaltest. Ohne nutzbare Cloud werden lokale Werte trotzdem
verwendet (siehe [Quellenwahl](source-selection.md)).

## Was gelesen wird

Nur die Werte des [Paarkatalogs](observation-pairs.md): dieselben Pfade, die auch die
Cloud für Außentemperatur, Kreis- und Warmwasserstatus, Sollwerte, Modulation,
Flamme, Druck, Vor- und Rücklauftemperatur sowie Energie- und Betriebszähler
bietet. Die Kreise (Heizkreise, Warmwasserkreise, Wärmeerzeuger) stammen aus den
Verzeichnissen des Gateways. Ein Pfad, den das Gateway nicht anbietet, wird erst
nach einer Woche wieder abgefragt. Der lokale Zugang legt keine eigene Entität an
und liest in dieser Version keine Störungen, Zeitprogramme, Urlaubseinstellungen
oder Aufzeichnungen.

Ein lokal gelesener Wert ersetzt den Cloud-Wert nur, wenn Typ und Einheit mit
dem der Cloud übereinstimmen, und nur bei Werten, die der Paarkatalog aufführt.

## Schonender Zugriff

Diese Gateways sind langsam, und mehrere Berichte besagen, dass sie nach Tagen
häufiger Anfragen nicht mehr antworten, bis ihre Stromversorgung unterbrochen
wird. Der lokale Zugang

- sendet immer nur eine Anfrage und lässt danach mindestens eine Sekunde Pause;
- liest Livewerte frühestens alle zwei Minuten, Einstellungen und Energiewerte
  alle fünf, Zähler und Firmware alle 15, beschreibende Werte stündlich und
  Kennungen täglich (die **lokale Abfragefrequenz** des Zugangs kann das nur
  verlangsamen; Werte unter zwei Minuten gelten als zwei Minuten);
- fragt einen Pfad, der mit 404 oder 403 antwortet, eine Woche lang nicht mehr ab;
- pausiert alles Lesen nach zwei fehlgeschlagenen Anfragen in Folge (keine
  Antwort innerhalb von 10 Sekunden, keine Verbindung, ein Serverfehler): zuerst
  eine Minute, dann zwei, fünf, zehn, zwanzig und zuletzt dreißig Minuten, solange
  die Fehler andauern. Eine Überlastungsantwort (429, 503) pausiert mindestens so
  lange wie ihr Retry-After. Ein wieder funktionierender Wert beendet die Folge;
- behält die Werte eines bestätigten Zugangs während der ersten Pause, nimmt sie
  aber nach drei verpassten Zeiträumen oder ab der zweiten Pause zurück;
- hält ganz an, ohne weitere Anfragen, wenn sich zwei Antworten hintereinander
  nicht entschlüsseln lassen (falsches oder geändertes Passwort; eine einzelne,
  auch direkt nach einem Neustart, wird erst nach einer Minute noch einmal
  angesehen). Der Zugang zeigt dann „Anmeldung nötig“ oder „nicht
  erreichbar“, bis du die Daten neu eingibst;
- beurteilt ein Gateway, das an seiner eigenen Kennung erkannt wurde und später
  keine Kennung, eine Kennung ohne Aussage (leer oder ein Platzhalter wie -1)
  oder eine andere Kennung meldet, und eines, das an einem gespeicherten
  Versionswert erkannt wurde und später keinen oder einen anderen meldet (zum
  Beispiel nach einem Firmware-Update), wie bei der Einrichtung an seinen Werten:
  Nur ein eindeutiger Widerspruch pausiert den Zugang (seine Werte werden
  zurückgenommen, nach der Pause wird erneut geprüft); sonst liest er weiter,
  bestätigt durch die Werte oder unbestätigt. Ein Firmware-Update hält den
  Zugang also nie an. Die letzte solche Prüfung steht
  in der Diagnose (`identity_check`); ein unbestätigter Zugang prüft gar keine
  Kennung;
- stellt keine Anfragen, solange keine Cloud-Verbindung das System mehr versorgt
  (zum Beispiel nach dem Entfernen der Cloud-Verbindung), und macht von selbst
  weiter, sobald wieder eine besteht;
- prüft die Kennung des Gateways jede Stunde und nach jeder Pause erneut.

Antwortet das Gateway gar nicht mehr, schalte den Zugang unter **Verbindungen**
aus, unterbrich kurz die Stromversorgung des Gateways und schalte den Zugang
wieder ein.

## Datenschutz und Sicherheit

- Beide Passwörter liegen im Konfigurationseintrag von Home Assistant, wie die
  Tokens der anderen Verbindungen. Sie sind Teil deiner Home-Assistant-Sicherungen.
  Sie werden nirgendwohin gesendet, auch nicht an das Gateway: Den Schlüssel für
  die Verschlüsselung bildet Home Assistant selbst daraus.
- Der Datenverkehr bleibt im Heimnetz. Es ist einfaches HTTP auf Port 80, dessen
  Inhalt mit dem eigenen Verfahren des Herstellers verschlüsselt ist (AES mit
  einem aus den beiden Passwörtern gebildeten Schlüssel); es ist keine
  TLS-Verbindung, und das Verfahren stammt vom Hersteller, nicht von mir.
- Die Diagnose der Integration nennt Zähler, Alter und feste Codes. Sie enthält nie
  ein Passwort, einen Schlüssel, eine Adresse oder eine Gateway-Kennung oder
  Seriennummer.
- Der Zugang liest nur. Er ändert nie etwas am Gateway.

## Was Dialog und Status sagen

| Text | Bedeutung |
|---|---|
| Das Gateway hat nicht geantwortet | Keine Verbindung in der Zeit; Adresse und Port 80 prüfen. |
| Die Antwort ließ sich nicht entschlüsseln | Eines der beiden Passwörter ist falsch oder wurde in der Geräte-App geändert. Meist weicht das private Passwort von dem des Gateways ab; prüfe es in der App, in der du es festgelegt hast. |
| Kein privates Passwort gespeichert | Das Gateway antwortet wie eines, dessen privates Passwort zurückgesetzt wurde. Lege es einmal in der alten Geräte-App fest (Handy im selben WLAN wie das Gateway) und versuche es dann erneut. Auch der Verbindungstest eines bestehenden Zugangs nennt das. |
| Das Gateway ist ausgelastet | Es hat mit einer Überlastungsmeldung geantwortet; später erneut versuchen. |
| Nicht das Gateway der gewählten Anlage | Die eigene Kennung des Gateways ist nicht die der Cloud, und seine Werte widersprechen den Cloud-Werten eindeutig (andere Version, anderer Typ oder andere Marke oder ein weit abweichender Livewert). |

## Rückmeldung

Das Protokoll ist nach öffentlichen Beschreibungen umgesetzt und nur gegen ein
simuliertes Gateway geprüft. Wenn du ein iCom-Gateway hast, probiere die Beta
bitte aus ([Testversionen erhalten](setup.md#deutsch-installation)) und
schreib mir in einem
[Issue](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues)
oder bei [Share your installation](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/81),
was passiert ist, mit der Diagnose der Integration im Anhang. Besonders
interessant: ob die Kennungsprüfung der Einrichtung besteht (und auf welchem Weg, siehe
`identity_method` in der Diagnose), welche Werte
erscheinen und wie lange das Gateway ohne Hänger läuft.

## Nicht enthalten

Schreiben über den lokalen Zugang, Anlagen ohne Cloud-Verbindung, Störungen und
Zeitprogramme aus dem lokalen Zugang, die Cloud-Verbindung des Gateways (XMPP)
und Gateways ohne lokale Schnittstelle.
