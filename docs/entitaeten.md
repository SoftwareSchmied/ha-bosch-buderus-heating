# Entitäten und Polling

Diese Liste dokumentiert den am 16. August 2026 auf der Referenzanlage
ermittelten Stand. Die Integration erzeugt Entitäten dynamisch. Andere Anlagen
können daher mehr, weniger oder andere Entitäten besitzen.

`{hc}`, `{dhw}` und `{hs}` stehen für die tatsächlich gefundenen Heizkreise,
Warmwasserkreise und Wärmeerzeuger. Für jeden weiteren Kreis wird die passende
Namensgruppe erneut angelegt. Alle Entitäten bleiben einem gemeinsamen
Gateway-Gerät zugeordnet und tragen die Gruppe als Präfix im Anzeigenamen.

Die erzeugten Gruppen- und Entitätsnamen folgen der in Home Assistant
eingestellten **Systemsprache**. Bei Deutsch heißt eine Entität beispielsweise
**Anlage – Anlagentyp**, bei Englisch **System – System type**. Selbst vergebene
Namen wie **Obergeschoss** werden nicht übersetzt. Bereits manuell in Home
Assistant umbenannte Entitäten bleiben ebenfalls unverändert.

Der Live-Abgleich vom 16. August 2026 konnte alle 94 lesbaren Ressourcen ohne
Parserfehler verarbeiten. Bekannte Ressourcen können daraus bis zu 91
read-only Entitäten bilden. Alltagsrelevante Messwerte, Zustände, Energiezähler
und Langzeitwerte sind standardmäßig aktiv. Technische, sensible und zu einem
Bedienelement redundante Sensoren werden standardmäßig deaktiviert angelegt.
Unbekannte Herstellererweiterungen bleiben ausschließlich im
geschwärzten Diagnosebericht. Zehn lesbare Blattressourcen bleiben wegen
Datenschutz, Lizenzinhalt oder rein interner Verwendung ohne eigene Entität.
Weitere 49 vom Gateway referenzierte Pfade
beantwortete PointT mit HTTP 403; deren Inhalt wird von der Cloud nicht
freigegeben und kann deshalb nicht als Entität angeboten werden.

Die kumulierten Energieentitäten verwenden die Home-Assistant-Zustandsklasse
`total_increasing`. Springt ein Zähler nach einem Reset oder Gerätetausch auf
einen kleineren nicht negativen Wert, behandelt Home Assistant dies als neuen
Zählerzyklus und nicht als negativen Verbrauch. Die Integration verändert den
Messwert nicht, zählt erkannte Rücksprünge aber anonym unter
`energy_counter_resets_detected` in den Diagnostics.

## Bedeutung der Schreibangaben

- **PointT meldet schreibbar** beschreibt ausschließlich die Metadaten der
  Cloud-Ressource. Dies ist noch keine Bestätigung, dass das Schreiben auf
  jeder Anlage sicher funktioniert.
- **In HA bedienbar** beschreibt den aktuellen Stand der Integration. Sichere
  Benutzerwerte werden nur bei exakt passendem Pfad, Datentyp, Schreibflag,
  Wertebereich und erlaubten Optionen als Bedienelement angelegt.
- Auf der Referenzanlage melden 24 Ressourcen Schreibfähigkeit. 20 davon
  werden als lesbare Entitäten dargestellt. 13 sichere Benutzerwerte werden
  zusätzlich bedienbar; Verwaltungs- und Installateurswerte bleiben gesperrt.

## Reifegrad und Standardaktivierung

| Reifegrad | Bedeutung | Darstellung in Home Assistant |
|---|---|---|
| `observed` | Im Ressourcenbaum gesehen, Bedeutung noch nicht ausreichend belegt | Keine Entität; nur geschwärzte Diagnostics |
| `understood` | Typ und fachliche Bedeutung geklärt | Entität möglich; Standardaktivierung folgt der unten beschriebenen Nutzerrelevanz |
| `verified` | Für normale Anzeige ausreichend belegt | Read-only Entität, standardmäßig aktiv |
| `write_verified` | Schreiben und Rücklesen real bestätigt | Steuerentität, standardmäßig aktiv |

Die Heizkreis-Betriebsart ist auf der Referenzanlage real schreibbestätigt.
Die weiteren freigegebenen Regler verwenden denselben getesteten
Schreib-/Rücklesedienst und werden zusätzlich gegen die aktuellen
Gateway-Metadaten begrenzt; ihre einzelnen Wirkungen sind noch nicht alle auf
einem realen Gerät durchgetestet.

Die Standardaktivierung ist ausdrücklich festgelegt:

- **Aktiv:** Temperaturen und Betriebszustände, alle verfügbaren
  Energiezähler, Starts, Betriebszeiten, TC3, Systemdruck, der abgeleitete
  Druckstatus und alle freigegebenen Schalter, Zahlenregler und Auswahlfelder.
- **Deaktiviert:** Seriennummer, Gateway-UUID, Land, ausführliche
  Systeminformationen, die einzelnen Rohfelder des Softwareupdate-Status,
  technische Geräte- und Konfigurationswerte sowie Nur-Lese-Sensoren, deren
  Wert bereits durch ein aktives Bedienelement dargestellt wird.
- **Keine Entität:** unbekannte Herstellererweiterungen, WLAN- und
  Netzwerkdaten, Lizenztexte, Zugangsdaten und andere datenschutzkritische oder
  rein interne Ressourcen.

Beispiel für eine vermiedene Doppelung: **Heizkreis 1 – Manueller Sollwert**
steht als aktiver Zahlenregler zur Verfügung. Der zusätzliche read-only Sensor
für denselben PointT-Pfad bleibt standardmäßig deaktiviert.

Eine standardmäßig deaktivierte Entität lässt sich unter **Einstellungen →
Geräte & Dienste → Entitäten** öffnen und dort aktivieren. Eine bereits bewusst
aktivierte Entität wird bei einem Update nicht wieder deaktiviert.

## Polling-Regeln

| Gruppe | Frequenz | Inhalt auf der Referenzanlage |
|---|---:|---:|
| Live-Werte | 60 Sekunden | 25 Entitäten |
| Einstellungen | 5 Minuten | 19 Entitäten |
| Energie | 5 Minuten | bis zu 15 Entitäten |
| Langzeitwerte | 15 Minuten | 13 Entitäten |
| Statisch | nur beim Start | 20 bekannte mögliche Entitäten |

Fällige Gruppen werden zu Sammelabrufen mit höchstens 30 Ressourcen
zusammengefasst. Ein HTTP-429 stoppt weitere Abrufe vorübergehend. Erfolgreiche
Teilwerte und der jeweils letzte gültige Zustand bleiben bei einem Teilausfall
erhalten.

## Gateway und Anlage

| Entität | HA-Typ | PointT-Ressource / Teilwert | PointT meldet schreibbar | In HA bedienbar | Polling |
|---|---|---|:---:|:---:|---:|
| Marke | Diagnosesensor | `/gateway/brand` | Nein | Nein | nur beim Start |
| Seriennummer (deaktiviert) | Diagnosesensor | `/gateway/serialId` | Nein | Nein | nur beim Start |
| Gateway-UUID (deaktiviert) | Diagnosesensor | `/gateway/uuid` | Nein | Nein | nur beim Start |
| Datum und Uhrzeit | Diagnosesensor | `/gateway/dateTime` | Nein | Nein | 15 min |
| Sw Prefix | Diagnosesensor | `/gateway/swPrefix` | Nein | Nein | nur beim Start |
| Zeitzone | Diagnosesensor | `/gateway/tzInfo/timeZone` | Ja | Nein | 5 min |
| Softwareupdate – aktueller Fortschritt | Diagnosesensor | `/gateway/update/status` → `progress.cur_percent` | Nein | Nein | 60 s |
| Softwareupdate – aktueller Schritt | Diagnosesensor | `/gateway/update/status` → `progress.cur_step` | Nein | Nein | 60 s |
| Softwareupdate – Schritte gesamt | Diagnosesensor | `/gateway/update/status` → `progress.nsteps` | Nein | Nein | 60 s |
| Softwareupdate – Fortschritt | Diagnosesensor | `/gateway/update/status` → `progress.percent` | Nein | Nein | 60 s |
| Softwareupdate – Status | Diagnosesensor | `/gateway/update/status` → `status.value` | Nein | Nein | 60 s |
| Firmwareversion | Diagnosesensor | `/gateway/versionFirmware` | Nein | Nein | nur beim Start |
| Hardwareversion | Diagnosesensor | `/gateway/versionHardware` | Nein | Nein | nur beim Start |
| Aktuelle Wärmeanforderung | Sensor | `/heatSources/actualHeatDemand` | Nein | Nein | 60 s |
| Aktuelle Modulation | Sensor | `/heatSources/actualModulation` | Nein | Nein | 60 s |
| Vorlauftemperatur | Sensor | `/heatSources/actualSupplyTemperature` | Nein | Nein | 60 s |
| Energiemanagement-Status | Sensor | `/heatSources/emStatus` | Nein | Nein | 60 s |
| Brennerstatus | Sensor | `/heatSources/flameStatus` | Nein | Nein | 60 s |
| Rücklauftemperatur | Sensor | `/heatSources/returnTemperature` | Nein | Nein | 60 s |
| Systemdruck | Sensor | `/heatSources/systemPressure` | Nein | Nein | 60 s |
| Systemdruckstatus | Abgeleiteter Statussensor | Systemdruck und `/heatSources/systemPressureRange` | Nein | Nein | mit Systemdruck |
| Zulässiger Druckbereich – hoher Systemdruck | Diagnosesensor | `/heatSources/systemPressureRange` → `highSystemPressure` | Nein | Nein | nur beim Start |
| Zulässiger Druckbereich – absoluter Maximaldruck | Diagnosesensor | `/heatSources/systemPressureRange` → `absoluteHighPressure` | Nein | Nein | nur beim Start |
| Zulässiger Druckbereich – niedriger Systemdruck | Diagnosesensor | `/heatSources/systemPressureRange` → `lowSystemPressure` | Nein | Nein | nur beim Start |
| Zulässiger Druckbereich – Abschaltdruck | Diagnosesensor | `/heatSources/systemPressureRange` → `shutOfPressureThreshold` | Nein | Nein | nur beim Start |
| Zulässiger Druckbereich – obere Druckgrenze | Diagnosesensor | `/heatSources/systemPressureRange` → `highPressureThreshold` | Nein | Nein | nur beim Start |
| Zulässiger Druckbereich – untere Druckgrenze | Diagnosesensor | `/heatSources/systemPressureRange` → `lowPressureThreshold` | Nein | Nein | nur beim Start |
| Abwesenheitsmodus | Sensor und Schalter | `/system/awayMode/enabled` | Ja | Ja | 5 min; nach Änderung zurückgelesen |
| Marke | Diagnosesensor | `/system/brand` | Nein | Nein | nur beim Start |
| Land (deaktiviert) | Diagnosesensor | `/system/country` | Nein | Nein | nur beim Start |
| Systeminformationen (deaktiviert) | Text-Diagnosesensor mit Modulnamen, Versionen und bereinigten Attributen | `/system/info` | Nein | Nein | nur beim Start |
| Systembus | Diagnosesensor | `/system/bus` | Nein | Nein | 15 min |
| Quelle der Außentemperatur | Diagnosesensor | `/system/sensors/temperatures/outdoorTemperatureSource` | Nein | Nein | 60 s |
| Außentemperatur | Sensor | `/system/sensors/temperatures/outdoor_t1` | Nein | Nein | 15 min |
| Anlagentyp | Diagnosesensor | `/system/type` | Nein | Nein | nur beim Start |
| Unterstützungsstatus | Diagnosesensor | `/system/variableTariff/supportStatus` | Nein | Nein | 60 s |

Der Systemdrucksensor erhält die sechs validierten Grenzwerte zusätzlich
als numerische Attribute mit der Einheit im Attributnamen:
`technical_minimum_bar`, `shutdown_pressure_bar`, `normal_minimum_bar`,
`normal_maximum_bar`, `upper_pressure_limit_bar` und `absolute_maximum_bar`.
Der abgeleitete Status lautet `critical_low`, `low`, `normal`, `high` oder
`critical_high`; Home Assistant zeigt diese Zustände übersetzt an. Attribute
und Status werden nur erzeugt, wenn alle sechs Werte numerisch, endlich,
nicht negativ und plausibel geordnet sind. Dadurch entstehen bei Anlagen ohne
vollständige Druckbereichsangaben keine erfundenen Grenzwerte.

## Heizkreis

Auf der Referenzanlage entstehen 14 Entitäten. Ein leerer optionaler Name
erzeugt keine Entität; bei gesetztem Namen kommt diese dynamisch hinzu.

| Entität | HA-Typ | PointT-Ressource | PointT meldet schreibbar | In HA bedienbar | Polling |
|---|---|---|:---:|:---:|---:|
| Aktives Zeitprogramm | Sensor | `/heatingCircuits/{hc}/activeSwitchProgram` | Ja | Nein | 5 min |
| Regelungsart | Sensor | `/heatingCircuits/{hc}/controlType` | Ja | Nein | 5 min |
| Wunschtemperatur | Sensor | `/heatingCircuits/{hc}/currentRoomSetpoint` | Nein | Nein | 60 s |
| Sommer-/Winterbetrieb | Sensor | `/heatingCircuits/{hc}/currentSuWiMode` | Nein | Nein | 60 s |
| Heiz-/Kühlbetrieb | Sensor | `/heatingCircuits/{hc}/heatCoolMode` | Nein | Nein | 60 s |
| Heizsystem | Diagnosesensor | `/heatingCircuits/{hc}/heatingType` | Nein | Nein | nur beim Start |
| Manueller Sollwert | Sensor und Zahlenregler | `/heatingCircuits/{hc}/manualRoomSetpoint` | Ja | Ja | 5 min; nach Änderung zurückgelesen |
| Maximale Vorlauftemperatur | Sensor | `/heatingCircuits/{hc}/maxFlowTemp` | Ja | Nein | 5 min |
| Name (nur wenn gesetzt) | Diagnosesensor | `/heatingCircuits/{hc}/name` | Ja | Nein | nur beim Start |
| Betriebsart | Sensor und Auswahlfeld | `/heatingCircuits/{hc}/operationMode` | Ja | Ja | 5 min; nach Änderung gestaffelt zurückgelesen |
| Betriebsstatus | Sensor | `/heatingCircuits/{hc}/overallStatus` | Nein | Nein | 60 s |
| Art des Zeitprogramms | Sensor | `/heatingCircuits/{hc}/switchProgramMode` | Ja | Nein | 5 min |
| Name des Zeitprogramms A | Sensor | `/heatingCircuits/{hc}/switchPrograms/nameA` | Ja | Nein | 5 min |
| Heizen | Sensor und Zahlenregler | `/heatingCircuits/{hc}/temperatureLevels/comfort2` | Ja | Ja | 5 min; nach Änderung zurückgelesen |
| Absenken | Sensor und Zahlenregler | `/heatingCircuits/{hc}/temperatureLevels/eco` | Ja | Ja | 5 min; nach Änderung zurückgelesen |

Das Auswahlfeld entsteht nur, wenn der jeweilige Heizkreis aktuell den Typ
`stringValue`, Schreibbarkeit und alle drei erlaubten Rohwerte `off`, `manual`
und `auto` meldet. Eine Änderung wird genau einmal per PUT gesendet und gilt
erst nach einem separaten GET mit demselben Wert als erfolgreich. Da die reale
K40-Anlage schnelle Folgeänderungen verzögert zurückmeldete, erfolgen bis zu
drei zeitlich gestaffelte Rückleseprüfungen. Dabei wird der PUT niemals
wiederholt. Der Ablauf **Manuell → Auto → Manuell** wurde bestätigt.

## Warmwasser

Diese 16 Entitäten entstehen je vorhandenem Warmwasserkreis.

| Entität | HA-Typ | PointT-Ressource | PointT meldet schreibbar | In HA bedienbar | Polling |
|---|---|---|:---:|:---:|---:|
| Warmwasser-Isttemperatur | Sensor | `/dhwCircuits/{dhw}/actualTemp` | Nein | Nein | 60 s |
| Extra-Warmwasser | Sensor und Schalter | `/dhwCircuits/{dhw}/charge` | Ja | Ja | 5 min; nach Änderung zurückgelesen |
| Dauer Extra-Warmwasser | Sensor und Zahlenregler | `/dhwCircuits/{dhw}/chargeDuration` | Ja | Ja | 5 min; nach Änderung zurückgelesen |
| Restzeit Extra-Warmwasser | Sensor | `/dhwCircuits/{dhw}/chargeRemainingTime` | Nein | Nein | 60 s |
| Aktueller Sollwert | Sensor | `/dhwCircuits/{dhw}/currentSetpoint` | Nein | Nein | 60 s |
| Aktuelles Temperaturniveau | Sensor | `/dhwCircuits/{dhw}/currentTemperatureLevel` | Nein | Nein | 60 s |
| Name | Diagnosesensor | `/dhwCircuits/{dhw}/name` | Ja | Nein | nur beim Start |
| Betriebsart | Sensor und Auswahlfeld | `/dhwCircuits/{dhw}/operationMode` | Ja | Ja | 5 min; nach Änderung zurückgelesen |
| Betriebsstatus | Sensor | `/dhwCircuits/{dhw}/overallStatus` | Nein | Nein | 60 s |
| Temperaturabsenkung bei Störung | Sensor und Schalter | `/dhwCircuits/{dhw}/reduceTempOnAlarm` | Ja | Ja | 5 min; nach Änderung zurückgelesen |
| Solltemperatur Extra-Warmwasser | Sensor und Zahlenregler | `/dhwCircuits/{dhw}/singleChargeSetpoint` | Ja | Ja | 5 min; nach Änderung zurückgelesen |
| Thermische Desinfektion | Sensor | `/dhwCircuits/{dhw}/tdMode` | Nein | Nein | 60 s |
| Eco+ Starttemperatur | Sensor und Zahlenregler | `/dhwCircuits/{dhw}/temperatureLevels/eco` | Ja | Ja | 5 min; nach Änderung zurückgelesen |
| Komfort Starttemperatur | Sensor und Zahlenregler | `/dhwCircuits/{dhw}/temperatureLevels/high` | Ja | Ja | 5 min; nach Änderung zurückgelesen |
| Eco Starttemperatur | Sensor und Zahlenregler | `/dhwCircuits/{dhw}/temperatureLevels/low` | Ja | Ja | 5 min; nach Änderung zurückgelesen |
| Aus | Sensor | `/dhwCircuits/{dhw}/temperatureLevels/off` | Nein | Nein | 15 min |

Zahlenregler übernehmen die vom Gateway gemeldeten Minimal- und Maximalwerte.
Sie erscheinen nur, wenn diese Grenzen vollständig und innerhalb der zusätzlich
festgelegten sicheren Temperatur- beziehungsweise Zeitbereiche liegen.
Heizkreis-Sollwerte verwenden die an der Referenzanlage bestätigte Schrittweite
von 0,5 °C. PointT liefert für Warmwasser-Temperaturen keine Schrittweite; ein
realer Versuch mit 0,5 °C wurde zunächst nicht bestätigt, erschien später aber
auf den nächsten vollen Grad gerundet. Deshalb verwenden alle
Warmwasser-Temperaturregler 1 °C.
Die am K40 zusätzlich referenzierten Stopptemperaturen (`highStop`, `lowStop`,
`ecoStop`) und Ladedeltas (`highChargingDelta`, `lowChargingDelta`,
`ecoChargingDelta`) beantwortet PointT mit HTTP 403. Sie können deshalb trotz
Anzeige im lokalen Expertenmenü nicht als Home-Assistant-Entitäten angeboten
werden.

## Wärmeerzeuger

Diese 11 Entitäten entstehen je vorhandenem Wärmeerzeuger.

| Entität | HA-Typ | PointT-Ressource / Teilwert | PointT meldet schreibbar | In HA bedienbar | Polling |
|---|---|---|:---:|:---:|---:|
| Wärmepumpentyp | Diagnosesensor | `/heatSources/{hs}/heatPumpType` | Nein | Nein | nur beim Start |
| Anzahl der Starts Gesamt | Diagnosesensor | `/heatSources/{hs}/numberOfStarts` → `total` | Nein | Nein | 15 min |
| Anzahl der Starts Heizung | Diagnosesensor | `/heatSources/{hs}/numberOfStarts` → `ch` | Nein | Nein | 15 min |
| Anzahl der Starts Kühlung | Diagnosesensor | `/heatSources/{hs}/numberOfStarts` → `cooling` | Nein | Nein | 15 min |
| Anzahl der Starts Warmwasser | Diagnosesensor | `/heatSources/{hs}/numberOfStarts` → `dhw` | Nein | Nein | 15 min |
| Austrittstemperatur Kondensator (TC3) | Diagnosesensor | `/heatSources/{hs}/supplyFlowCondenserTemp` | Nein | Nein | 15 min |
| Anlagentyp | Diagnosesensor | `/heatSources/{hs}/type` | Nein | Nein | nur beim Start |
| Betriebszeit Gesamt | Diagnosesensor | `/heatSources/{hs}/workingTime` → `total` | Nein | Nein | 15 min |
| Betriebszeit Heizung | Diagnosesensor | `/heatSources/{hs}/workingTime` → `ch` | Nein | Nein | 15 min |
| Betriebszeit Kühlung | Diagnosesensor | `/heatSources/{hs}/workingTime` → `cooling` | Nein | Nein | 15 min |
| Betriebszeit Warmwasser | Diagnosesensor | `/heatSources/{hs}/workingTime` → `dhw` | Nein | Nein | 15 min |

## Energiezähler

Alle Werte sind kumulierte Energie in kWh, keine momentane Leistung.

| Entität | HA-Typ | PointT-Ressource / Teilwert | PointT meldet schreibbar | In HA bedienbar | Polling |
|---|---|---|:---:|:---:|---:|
| Heizung – Erzeugte Wärme | Sensor | `/heatSources/emon/chConsumption` → `outputProduced` | Nein | Nein | 5 min |
| Heizung – Stromverbrauch Wärmepumpe | Sensor | `/heatSources/emon/chConsumption` → `compressor` | Nein | Nein | 5 min |
| Heizung – Stromverbrauch elektrischer Zuheizer | Sensor | `/heatSources/emon/chConsumption` → `eheater` | Nein | Nein | 5 min |
| Heizung – Stromverbrauch | Sensor | `/heatSources/emon/chConsumption` → `electricity`, sonst `compressor + eheater` | Nein | Nein | 5 min |
| Kühlung – Erzeugte Kühlenergie | Sensor | `/heatSources/emon/coolingConsumption` → `outputProduced` | Nein | Nein | 5 min |
| Kühlung – Stromverbrauch Wärmepumpe | Sensor | `/heatSources/emon/coolingConsumption` → `compressor` | Nein | Nein | 5 min |
| Warmwasser – Erzeugte Wärme | Sensor | `/heatSources/emon/dhwConsumption` → `outputProduced` | Nein | Nein | 5 min |
| Warmwasser – Stromverbrauch Wärmepumpe | Sensor | `/heatSources/emon/dhwConsumption` → `compressor` | Nein | Nein | 5 min |
| Warmwasser – Stromverbrauch elektrischer Zuheizer | Sensor | `/heatSources/emon/dhwConsumption` → `eheater` | Nein | Nein | 5 min |
| Warmwasser – Stromverbrauch | Sensor | `/heatSources/emon/dhwConsumption` → `electricity`, sonst `compressor + eheater` | Nein | Nein | 5 min |
| Gesamt – Erzeugte Wärme | Sensor | `/heatSources/emon/totalConsumption` → `outputProduced` | Nein | Nein | 5 min |
| Gesamt – Stromverbrauch Wärmepumpe | Sensor | `/heatSources/emon/totalConsumption` → `compressor` | Nein | Nein | 5 min |
| Gesamt – Stromverbrauch elektrischer Zuheizer | Sensor | `/heatSources/emon/totalConsumption` → `eheater` | Nein | Nein | 5 min |
| Gesamt – Stromverbrauch | Sensor | `/heatSources/emon/totalConsumption` → `electricity`, sonst `compressor + eheater` | Nein | Nein | 5 min |
| Gesamt – Umweltenergie (berechnet) | Sensor | `/heatSources/emon/totalConsumption` → `outputProduced - compressor - eheater` | Nein | Nein | 5 min |

Die berechnete Umweltenergie wird nur angelegt, wenn alle drei benötigten
Rohwerte vorhanden und gültig sind. Ein negatives oder unvollständiges Ergebnis
wird nicht als Messwert ausgegeben.

## Sensible Diagnosewerte

Seriennummer, Gateway-UUID, Land und bereinigte Systeminformationen werden als
standardmäßig deaktivierte Diagnose-Entitäten angelegt. Sie erscheinen erst,
wenn der Benutzer sie bewusst aktiviert. Interne Tokenfelder aus
`/system/info` werden nie übernommen. WLAN-Daten, SSID, IP- und MAC-Adressen,
Lizenztexte, Nutzungsbedingungen sowie weitere Verwaltungsdaten bleiben
vollständig ausgeschlossen. Containerressourcen dienen nur der dynamischen
Erkennung.

Der Zustand von **Systeminformationen** ist ein kurzer Text aus Produkt- oder
Modulname und Version, beispielsweise `K40 · Version 15.00.01`. Weitere
bereinigte Angaben stehen als Attribute an derselben Entität. Die Ressource
wird ausschließlich beim Start gelesen.
