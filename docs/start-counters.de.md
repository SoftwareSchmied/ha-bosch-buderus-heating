# Startzähler: Änderungen und Prüfung

Ich habe die Zählerbehandlung in PR #72 überarbeitet. Der allgemeine Startzähler
und die Zähler einzelner Wärmeerzeuger behalten jeweils ihre eigene Datenquelle.
Die ausführlichen technischen Regeln stehen auch in der
[englischen Dokumentation](start-counters.md).

## Was an der bisherigen Lösung problematisch war

| Schwäche | Auswirkung | Meine Änderung |
| --- | --- | --- |
| Die Integration zeigte den allgemeinen Startzähler bisher nicht an. | Bei Anlagen mit ausschließlich diesem Zähler fehlte der Sensor. | Ich zeige das unterstützte skalare Format als Diagnosesensor an. |
| Der erste PR-Entwurf blendete ihn bei einem vorhandenen Einzelzähler aus. | Ein anderer Zähler oder ein ungeeignetes Datenformat konnte ihn verdrängen. | Ich prüfe beide Formate getrennt und behalte unterstützte Quellen unabhängig voneinander. |
| Die Bereinigung löschte den Registrierungseintrag bei fehlender Ressource oder vorhandenem Einzelzähler. | Ein vorübergehender Fehler konnte eigene Namen und Einstellungen des Sensors verlieren lassen. | Ich entferne diese Löschregel und erhalte bestehende Identitäten und Aktivierungszustände. |
| Die Auswahl berücksichtigte nur gerade verfügbare Ressourcen. | Ein Ausfall konnte während des Betriebs einen zusätzlichen allgemeinen Sensor entstehen lassen. | Ich lege jede unterstützte Quelle unabhängig an und verhindere doppelte Entitäten anhand ihrer festen Identität. |
| Ganzzahlige und gebrochene Zahlendarstellungen wurden teilweise unterschiedlich behandelt; gemeldete Fehlerkennwerte fehlten. | Ungültige Zahlen konnten erscheinen, gültige Zahlen konnten verschwinden. | Eine gemeinsame Zählerprüfung wertet Datenformat, Zahl und Fehlerkennwerte aus. |
| Unbekannte Teilfelder konnten Sensoren erzeugen; bei mehrfachen Angaben gewann der erste Wert. | Unerwartete oder widersprüchliche Angaben konnten angezeigt werden. | Ich beschränke die Auswertung auf bekannte Teile und weise widersprüchliche Angaben zurück. |
| Nach einem fehlgeschlagenen Start fehlte teilweise die regelmäßige Abfrage. | Ein erhaltener Registrierungseintrag allein konnte sich nicht erholen. | Ich berücksichtige registrierte Pfade beim Start und nehme zulässige Wiederherstellungen in die normale Abfrageplanung auf. |
| Die Wiederherstellung akzeptierte weniger ID-Schreibweisen als die Sensorerstellung. | Ein bereits angelegter Zähler konnte bei einer ID mit Punkt oder Nicht-ASCII-Zeichen nach einem fehlgeschlagenen Start dauerhaft fehlen. | Ich verwende dieselbe Pfadprüfung für Erstellung und Wiederherstellung und prüfe den vollständigen Ablauf mit der tatsächlichen Discovery. |

## Gültige Werte und unveränderte Zuordnung

Der allgemeine Pfad `/heatSources/numberOfStarts` verwendet `floatValue`.
Ab 0.9.0 wird sein Sensor deaktiviert angelegt, wenn die Anlage genau einen
Wärmeerzeuger meldet: Er zählt dann dieselben Starts wie dessen `total`.
Bestehende Entitäten behalten ihre Einstellung.
Die Pfade einzelner Wärmeerzeuger unterstützen einen skalaren `floatValue`
bei WDDW2 sowie `emonValue` bei ConnectKey mit den bekannten
Teilen `total`, `ch`, `cooling`, `dhw` und `pool`. Fehlende Teilwerte bleiben
unbekannt. Ein einfacher Zahlenwert am Einzelpfad wird nicht in mehrere
Teilzähler umgedeutet. Die WDDW2-Erweiterung ist in v0.8.2-beta.5 enthalten.
Zuordnung und Prüfung stehen im [Bericht zu P01](wddw2-types.md).

Ich akzeptiere endliche, nicht negative, ganzzahlige Zahlen ohne Einheit oder
mit der einheitenlosen Angabe `other`. `1234` und `1234.0` ergeben denselben
Zählerstand. Zeichenketten, Wahrheitswerte und Bruchteile werden zurückgewiesen.
Die Obergrenze `2**53 - 1` verhindert Genauigkeitsverluste in nachgelagerter
Zahlenspeicherung; sie ist keine behauptete Grenze der Heizungsanlage.

Ab 0.8.2-beta.4 entferne ich vor der Einheitenprüfung umgebende Leerzeichen.
Ein einzelnes Leerzeichen, Tabulatoren und geschützte Leerzeichen gelten als
leere Einheit. Auch `other` mit umgebenden Leerzeichen wird akzeptiert.
Tatsächliche Einheiten wie `C` und `kWh`, unbekannte Texte und Einheitenfelder
mit anderen Datentypen bleiben unzulässig. Das gilt für den allgemeinen
`floatValue`-Zähler und die unterstützten `emonValue`-Zähler einzelner
Wärmeerzeuger. Die Einheitenprüfung anderer Sensoren bleibt unverändert.

Die optionalen Metadaten `state` können Zahlen ausdrücklich als Fehlerwerte
kennzeichnen: `short`, `open`, `overflow`, `underflow` und `invalid`. Solche
Werte zeige ich nicht als Startzahl an. Deshalb kann `32767` je nach Metadaten
gültig oder ungültig sein. Eine normale Null bleibt gültig; `off: 0` allein ist
kein Fehlerkennwert. Fehlerhafte bekannte Metadaten oder mehr als 32 Einträge
führen zu einem unbekannten Messwert. Die Metadaten werden mit jeder Antwort
ersetzt und nicht unbegrenzt angesammelt.

Ich berechne keinen Gesamtwert aus Teilzählern und ersetze eine fehlende Quelle
nicht durch eine andere. Gleiche Zahlen beweisen nicht dieselbe Bedeutung.
Die Nummer eines Wärmeerzeugers beweist auch nicht, dass es sich um einen
Kessel oder eine Wärmepumpe handelt.

Bestehende Sensor-IDs, eigene Namen und Aktivierungszustände bleiben erhalten.
Der neue Skalar verwendet `gateway:heatSources:hsN:numberOfStarts`.
Er übernimmt weder die Identität noch die Statistik des strukturierten
Gesamtfelds `gateway:heatSources:hsN:numberOfStarts:total`. Wenn ein Gerät
beide Formate gemeldet hat, bleiben beide registrierten Identitäten auch
nach einem Neustart erhalten. Nur das zur aktuellen Antwort passende Format
liefert einen Wert.
Ein erfolgreicher Abruf ohne brauchbare Zahl ergibt `unbekannt`, ein
fehlgeschlagener Abruf `nicht verfügbar`. Es wird keine künstliche Null
veröffentlicht. Die Zustandsklasse bleibt `TOTAL_INCREASING`. Einen kleineren
gültigen Rohwert gebe ich unverändert weiter; Home Assistant verwendet dafür
seine normale Behandlung von Zählerrücksetzungen.

## Wiederherstellung und Cloud-Anfragen

Ich ordne vorhandene Entitäten ausschließlich ihrem Konfigurationseintrag und
Gateway zu. Ihre exakten Ressourcenpfade werden bei der begrenzten Discovery
berücksichtigt. Die Registrierung ersetzt keine aktuelle Antwort der Anlage.
Die Schreibweise einer bereits akzeptierten Wärmeerzeuger-ID bleibt erhalten,
einschließlich Punkten und Nicht-ASCII-Zeichen. Pfadwechsel über `..`, leere
IDs, Abfrageparameter und mehrdeutige Entitätskennungen werden weiterhin
abgewiesen. Die Varianten in den Tests sind synthetisch und belegen keine
bestimmte ID-Schreibweise auf einer realen Anlage.

Registrierte Pfade und ausdrücklich gemeldete, bereits versuchte Referenzen
können anschließend über die normale langsame Abfragegruppe wiederhergestellt
werden: alle 15 Minuten im Standardprofil oder alle 30 Minuten im sparsamen
Profil. Fehlgeschlagene optionale Suchpfade werden dadurch nicht dauerhaft
abgefragt. Grenzen bleiben 512 unterschiedliche Pfade, acht Discovery-Ebenen
und höchstens 30 Pfade pro Sammelabruf.

Ein bereits bestätigter oder registrierter Zähler kann nach einer 404 beim
nächsten fälligen Abruf wieder erscheinen. Für eine erstmals gemeldete, bisher
nie erfolgreich gelesene Ressource bleibt die bestehende 24-Stunden-Pause
nach einer 404 erhalten. Berechtigungsfehler, Dienstausfälle und Anfragelimits
behalten die vorgesehenen Pausen. Zusätzliche Wiederholungstimer entstehen nicht.

Die Wiederherstellung eines bisher ausgelassenen Pfads kann zusätzliche
Abfrageeinträge und gegebenenfalls zusätzliche Sammelanfragen verursachen.
Ich behaupte deshalb nicht, dass die Änderung grundsätzlich keine weiteren
Cloud-Anfragen erzeugt. Die Tests prüfen Fälligkeit und Paketgrößen ausdrücklich.

## Diagnose und Aussagekraft der Tests

Diagnoseschema 15 zeigt die Prüfung des Datenformats, Ablehnungsgründe für
Messwerte und den Wiederherstellungsstatus. Aktuelle Zählerstände, konkrete
Fehlerkennzahlen und unbekannte Feldinhalte werden dabei nicht exportiert.

Ich habe zunächst 20 gezielte Regressionstests am ursprünglichen PR-Stand
ausgeführt. Davon schlugen 17 fehl. Damit sind die Ausgangsfehler konkret
reproduziert. Die erweiterten Tests prüfen zusätzlich unter anderem
Neustarts, zwei Gateways, die tatsächlichen Home-Assistant-Zustände,
Registrierungseinträge, Profilintervalle, Anfragenlimits und Fehlerpausen.
Die verwendeten Gerätedaten sind synthetisch.

Im erneuten Review habe ich zusätzlich die unterschiedliche Behandlung von
IDs nachgewiesen. Fünf Fälle scheiterten vor der Korrektur. Die ergänzten
Tests prüfen erzeugte Sensoridentitäten, unveränderte Pfadgrenzen und die
Wiederherstellung nach 403, 404 und 406 mit der tatsächlichen Discovery.
Die 403-Pause bleibt dabei wirksam; andere fällige Ressourcen werden weiter
abgefragt.

Ein zusätzlicher Test verwendet den Home-Assistant-Recorder mit einer
Datenbank im Arbeitsspeicher. Die Werte 100, 105 und 110 ergeben trotz eines
zwischenzeitlichen Fehlerkennwerts und Ausfalls einen Zuwachs von 10. Nach
einem Rücksprung auf 3 ergibt die normale Rücksetzungsbehandlung einen
gesamten Zuwachs von 13. Damit prüfe ich die tatsächlich gespeicherte Statistik.

Die lokale Gesamtprüfung am 21.10.2026 besteht mit 1.460 Testfällen und
96,64 % kombinierter Code- und Verzweigungsabdeckung unter Python 3.14.2.
Gegenüber dem ursprünglichen PR sind das 100 zusätzliche Fälle einschließlich
separat gezählter Testvarianten. Das neue Modul für die Zählerauswertung erreicht
100 % Code- und Verzweigungsabdeckung. Ruff, Formatprüfung und striktes Mypy
bestehen. Die Tests prüfen außerdem den reproduzierbaren Paketbau und den
Import aus einer isolierten Installation des erzeugten ZIPs.

CHLINDE hat die Sichtbarkeit des Zählers mit der ursprünglichen Umsetzung
bestätigt. Die in beta.2 ergänzte Prüfung wies später die Einheit seiner Anlage
zurück, weil sie aus einem einzelnen Leerzeichen bestand. In
[PR #75](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/pull/75)
hat CHLINDE die Prüfung korrigiert und bestätigt, dass der Zähler seiner
Buderus G125 BE ECO-21 wieder einen Wert anzeigt. Vielen Dank, CHLINDE, für
die genaue Fehlerbeschreibung, die Korrektur, die Regressionstests und den
Test an deiner Anlage.

## Prüfung der Korrektur in Beta.4

Ich habe die Regressionstests für beide Zählerformate erweitert. Sie prüfen
das Einlesen, die Sensorerstellung und die Diagnose sowie falsche Datentypen
im Einheitenfeld. Weitere Fälle kombinieren Leerzeichen mit ungültigen Zahlen
oder Gerätefehlerwerten. Die Tests wechseln zwischen gültigen und ungültigen
Einheiten, simulieren eine 404-Antwort und prüfen die anschließende Erholung
derselben Entitäten. Einrichtung und erneutes Laden prüfen außerdem die
tatsächlichen Home-Assistant-Zustände und Registrierungseinträge mit
Leerzeichen-Einheiten und zwei synthetischen Gateways.

Die Einheitenkorrektur erzeugt keine zusätzlichen Anfragen, Wiederholungen oder
Schreibzugriffe. Discovery-Grenzen, Abfrageintervalle und Fehlerpausen bleiben
erhalten. Ein erfolgreicher Abruf ohne gültigen Zählerstand bleibt `unbekannt`,
ein fehlgeschlagener Abruf `nicht verfügbar`. Eine künstliche Null entsteht
in keinem dieser Fälle.

CHLINDEs Anlagentest bestätigt den gemeldeten Zähler. Die verschiedenen
Wiederherstellungsfälle und mehrere Wärmeerzeuger habe ich nicht an realen
Anlagen geprüft. Die Testdaten sind synthetisch. Die abschließenden lokalen
Prüfergebnisse stehen in der PR-Beschreibung; GitHub-Prüfungen gelten jeweils
für den dort tatsächlich geprüften Commit.
