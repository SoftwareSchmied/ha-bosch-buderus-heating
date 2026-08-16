# PointT-Ressourcenkatalog

Dieser Katalog beschreibt alle 94 Ressourcen, die am 15. August 2026 auf dem
ersten realen K40-Profil entdeckt wurden. Er enthält keine ausgelesenen Werte,
Seriennummern, Netzwerkdaten oder sonstigen persönlichen Kennungen.

Die daraus aktuell erzeugten Home-Assistant-Entitäten samt Schreibstatus und
Abruffrequenz stehen im [Entitäten- und Pollingkatalog](entitaeten.md).

Der Katalog ist ein beobachtetes Geräteprofil und keine feste Liste für jede
Anlage. Die Integration muss den Ressourcenbaum jedes Gateways dynamisch
entdecken und darf weder `hc1`, `dhw1` noch `hs1` voraussetzen.

## Herkunft und Benennung

Die Bezeichnungen beruhen auf:

1. dem real ausgelesenen PointT-Ressourcenbaum;
2. den in [BassXT/buderus#15](https://github.com/BassXT/buderus/pull/15)
   gegen MyBuderus, CSV-Export und zwei reale Anlagen geprüften
   Energiesemantiken;
3. den bereits beobachteten deutschsprachigen MyBuderus-Bezeichnungen;
4. einem statischen Abgleich von HomeCom Easy 5.0.0 und MyBuderus 5.0.0.

Beide App-Varianten enthalten dasselbe technische Ressourcenmodell und dieselben
expliziten PointT-Pfade. Unterschiede betreffen nur Marken- und Produkttexte.
Die Integration verwendet deshalb eine gemeinsame dynamische Geräte- und
Entitätslogik und setzt die erkannte Marke nur für die Anzeige ein.

Die deutschen Android-Sprachressourcen wurden separat aufgelöst und mit den
englischen Basistexten abgeglichen: 5.582 Texte je App, davon 2.392 mit
Heizungsbezug. Die daraus erzeugten Arbeitsdateien, APK-Dateien und extrahierter
Programmcode werden nicht in dieses Repository aufgenommen.

In der App gefundene Pfadkonstanten sind nur Hinweise auf mögliche Funktionen.
Sie beweisen nicht, dass ein konkretes Heizsystem die jeweilige Ressource
bereitstellt. Maßgeblich bleiben der dynamisch gelieferte Ressourcenbaum und
die Schreibmetadaten des verbundenen Gateways.

## Legende

- **Lesen**: als Home-Assistant-Sensor oder Diagnosewert geeignet.
- **Steuern**: möglicher Schalter, Zahlenwert oder Auswahl. Ein schreibbares
  PointT-Feld allein genügt noch nicht für eine Freigabe. Jeder Schreibvorgang
  braucht Werteprüfung, Schreiben, Rücklesen und Bestätigung.
- **Diagnose**: nützlich für Geräteinformationen oder Support, normalerweise
  standardmäßig deaktiviert.
- **Intern**: für Erkennung oder Berechnung erforderlich, aber keine eigene
  Entität.
- **Nicht veröffentlichen**: Identitäts-, Netzwerk-, Lizenz-, Zeit- oder
  Verwaltungsdaten. Diese Werte werden trotz technischer Lesbarkeit nicht als
  Entitäten oder Diagnosedaten ausgegeben.

Zusätzlich erzwingt der Code für jeden konkreten Pfad einen Reifegrad:
`observed`, `understood`, `verified` oder `write_verified`. Der Reifegrad
entscheidet, ob eine Entität sicher veröffentlicht werden kann. Die davon
getrennte Standardaktivierung folgt der Nutzerrelevanz: normale Messwerte,
Zustände, Energie- und Langzeitwerte sind aktiv; sensible, technische und zu
einem Bedienelement redundante Sensoren bleiben deaktiviert. Ein unbekannter
Pfad bleibt `observed` und erscheint nur schematisch sowie ohne Wert in
HA-Diagnostics. Die Schreibbarkeitsangabe der Cloud erhöht den Reifegrad
ausdrücklich nicht.

## Dynamische Entitätsgruppen

Die Wurzelressourcen liefern Referenzen auf tatsächlich vorhandene Objekte:

```text
/heatingCircuits -> hc1, hc2, ...
/dhwCircuits     -> dhw1, dhw2, ...
/heatSources     -> hs1, hs2, ...
```

Alle Entitäten eines Gateways gehören zu einem gemeinsamen Home-Assistant-
Gerät, beispielsweise **Buderus K40**. Da Home Assistant innerhalb eines Geräts
keine echten Untergruppen anbietet, trägt jeder Entitätsname ein dynamisches
Präfix, etwa **Heizkreis 1 – Betriebsart**, **Warmwasser – Betriebsart** oder
**Wärmepumpe – Betriebszeit Gesamt**. Gateway- und Systemwerte beginnen mit
**Gateway –** beziehungsweise **Anlage –**.

Gibt es keinen Heizkreis oder keine Warmwasserbereitung, entstehen dafür auch
keine Entitäten. Weitere Kreise erhalten automatisch ihre tatsächlich
gelieferte Nummer oder ihren konfigurierten Namen. Neue oder entfernte Kreise
werden bei einer erneuten Erkennung ergänzt beziehungsweise als nicht mehr
verfügbar markiert. Die IDs stammen ausschließlich aus den PointT-Referenzen,
nicht aus einer angenommenen Nummernfolge.

In den Tabellen steht `{hc}`, `{dhw}` oder `{hs}` für die dynamisch gefundene
ID. Auf dem Referenzsystem waren dies `hc1`, `dhw1` und `hs1`. Diese IDs bleiben
Bestandteil der stabilen Entitätskennung, erscheinen aber nicht als separate
Home-Assistant-Geräte.

## Heizkreise

| PointT-Pfad | Verständlicher Name | Zugriff | HA-Abbildung / Hinweis |
|---|---|---:|---|
| `/heatingCircuits` | Vorhandene Heizkreise | R | Intern: dynamische Erkennung |
| `/heatingCircuits/{hc}` | Heizkreis | R | Intern: Ressourcen des Kreises |
| `.../activeSwitchProgram` | Aktives Zeitprogramm | R/W | Diagnose; Schreiben erst mit mehreren beobachteten Programmen |
| `.../controlType` | Regelungsart | R/W | Diagnose; sicherheitsrelevante Konfiguration, zunächst nur lesen |
| `.../currentRoomSetpoint` | Wunschtemperatur | R | Temperatursensor |
| `.../currentSuWiMode` | Sommer-/Winterbetrieb | R | Statussensor |
| `.../heatCoolMode` | Heiz-/Kühlbetrieb | R | Statussensor |
| `.../heatingType` | Heizsystem des Kreises | R | Diagnose, z. B. Fußbodenheizung oder Heizkörper |
| `.../manualRoomSetpoint` | Manueller Sollwert | R/W | Zahlenwert in °C |
| `.../maxFlowTemp` | Maximale Vorlauftemperatur | R/W | Diagnose-Zahlenwert; Grenzprüfung erforderlich |
| `.../name` | Name des Heizkreises | R/W | Dekodierter Sensor nur bei gesetztem Namen; Umbenennen zunächst nicht anbieten |
| `.../operationMode` | Betriebsart | R/W | Auswahl: Aus, Manuell, Auto |
| `.../overallStatus` | Betriebsstatus | R | Statussensor |
| `.../switchProgramMode` | Art des Zeitprogramms | R/W | Diagnose; aktuell nur Temperaturniveau beobachtet |
| `.../switchPrograms` | Verfügbare Zeitprogramme | R | Intern: dynamische Erkennung |
| `.../switchPrograms/{program}` | Zeitprogramm | R/W | Ohne skalaren Inhalt keine eigene Entität; ein späterer Editor braucht ein verifiziertes Schema |
| `.../switchPrograms/name{program}` | Name des Zeitprogramms | R/W | Lesbar dekodierter Konfigurationswert |
| `.../temperatureLevels` | Verfügbare Temperaturniveaus | R | Intern: dynamische Erkennung |
| `.../temperatureLevels/comfort2` | Heizen | R/W | Zahlenwert in °C, nur wenn vorhanden |
| `.../temperatureLevels/eco` | Absenken | R/W | Zahlenwert in °C, nur wenn vorhanden |

Beobachtete Statuswerte für `overallStatus`:

| Rohwert | Anzeige |
|---|---|
| `ch_disabled` | Heizen deaktiviert |
| `emergency_mode` | Notbetrieb |
| `floor_drying` | Estrichtrocknung |
| `summer_idle` | Sommerpause |
| `boost` | Schnellaufheizung |
| `away` | Abwesend |
| `holiday` | Urlaub |
| `cooling_manual_on` | Kühlen manuell aktiv |
| `cooling_manual_off` | Kühlen manuell aus |
| `heating_manual_on` | Heizen manuell aktiv |
| `heating_manual_off` | Heizen manuell aus |
| `heating_auto` | Heizen automatisch |

## Warmwasserkreise

| PointT-Pfad | Verständlicher Name | Zugriff | HA-Abbildung / Hinweis |
|---|---|---:|---|
| `/dhwCircuits` | Vorhandene Warmwasserkreise | R | Intern: dynamische Erkennung |
| `/dhwCircuits/{dhw}` | Warmwasserkreis | R | Intern: Ressourcen des Kreises |
| `.../actualTemp` | Warmwasser-Isttemperatur | R | Temperatursensor |
| `.../charge` | Extra-Warmwasser | R/W | Schalter/Aktion mit Start und Stopp |
| `.../chargeDuration` | Dauer Extra-Warmwasser | R/W | Zahlenwert in Minuten |
| `.../chargeRemainingTime` | Restzeit Extra-Warmwasser | R | Dauersensor in Minuten |
| `.../currentSetpoint` | Aktuelle Warmwasser-Solltemperatur | R | Temperatursensor |
| `.../currentTemperatureLevel` | Aktuelles Temperaturniveau | R | Statussensor |
| `.../name` | Name des Warmwasserkreises | R/W | Lesbarer Gerätename; Umbenennen zunächst nicht anbieten |
| `.../operationMode` | Warmwasser-Betriebsart | R/W | Auswahl mit App-Namen: Aus, Eco, Komfort, Auto, Eco+ |
| `.../overallStatus` | Warmwasserstatus | R | Statussensor |
| `.../reduceTempOnAlarm` | Temperaturabsenkung bei Störung | R/W | Schalter, erst nach realem Schreibtest |
| `.../singleChargeSetpoint` | Solltemperatur Extra-Warmwasser | R/W | Zahlenwert in °C |
| `.../tdMode` | Thermische Desinfektion | R | Statussensor |
| `.../temperatureLevels` | Verfügbare Temperaturniveaus | R | Intern: dynamische Erkennung |
| `.../temperatureLevels/eco` | Eco+ Starttemperatur | R/W | Zahlenwert in °C |
| `.../temperatureLevels/high` | Komfort Starttemperatur | R/W | Zahlenwert in °C |
| `.../temperatureLevels/low` | Eco Starttemperatur | R/W | Zahlenwert in °C |
| `.../temperatureLevels/off` | Aus | R | Konfigurationswert in °C |
| `.../highStop`, `.../lowStop`, `.../ecoStop` | Stopptemperaturen | 403 | Vom K40 referenziert, von PointT nicht freigegeben |
| `.../highChargingDelta`, `.../lowChargingDelta`, `.../ecoChargingDelta` | Ladedelta TC1–TW1 | 403 | Expertenparameter in K; von PointT nicht freigegeben |

PointT-Code und App-Anzeige der Betriebsart:

| PointT-Code | App-Anzeige |
|---|---|
| `Off` / `off` | Aus |
| `low` | Eco |
| `high` | Komfort |
| `ownprogram` | Auto |
| `eco` | Eco+ |

## Wärmeerzeuger und zentrale Messwerte

| PointT-Pfad | Verständlicher Name | Zugriff | HA-Abbildung / Hinweis |
|---|---|---:|---|
| `/heatSources` | Vorhandene Wärmeerzeuger | R | Intern: dynamische Erkennung |
| `/heatSources/actualHeatDemand` | Aktuelle Wärmeanforderung | R | Heizen, Warmwasser und/oder Frostschutz |
| `/heatSources/actualModulation` | Aktuelle Modulation | R | Messwert in % |
| `/heatSources/actualSupplyTemperature` | Vorlauftemperatur | R | Temperatursensor |
| `/heatSources/emStatus` | Energiemanagement-Status | R | Diagnose-Status |
| `/heatSources/flameStatus` | Brennerstatus | R | Status; nur für passende Hybridsysteme relevant |
| `/heatSources/{hs}` | Wärmeerzeuger | R | Intern: dynamische Erkennung |
| `/heatSources/{hs}/heatPumpType` | Wärmepumpentyp | R | Diagnose: Luft/Wasser, Sole/Wasser oder Abluft/Wasser |
| `/heatSources/{hs}/numberOfStarts` | Starts nach Betriebsart | R | Mehrteiliger kumulativer Zähler |
| `/heatSources/{hs}/supplyFlowCondenserTemp` | Austrittstemperatur Kondensator (TC3) | R | Diagnose-Temperatur |
| `/heatSources/{hs}/type` | Wärmeerzeugertyp | R | Diagnose |
| `/heatSources/{hs}/workingTime` | Betriebszeit nach Betriebsart | R | Mehrteiliger kumulativer Zähler |
| `/heatSources/info` | Wärmeerzeugerinformationen | R | Intern/Diagnose, strukturiertes Objekt |
| `/heatSources/numberOfStarts` | Starts gesamt | R | Keine eigene Entität; identisch mit `total` des dynamischen Wärmeerzeugers |
| `/heatSources/returnTemperature` | Rücklauftemperatur | R | Temperatursensor |
| `/heatSources/systemPressure` | Systemdruck | R | Drucksensor in bar; validierte Bereichswerte als Attribute und optionaler abgeleiteter Statussensor |
| `/heatSources/systemPressureRange` | Zulässiger Druckbereich | R | Sechs statische Diagnose-Sensoren in bar |

Der Druckbereich wird von PointT als strukturiertes `values`-Objekt geliefert.
Die Integration legt für `highSystemPressure`, `absoluteHighPressure`,
`lowSystemPressure`, `shutOfPressureThreshold`, `highPressureThreshold` und
`lowPressureThreshold` jeweils eine eigene Entität an.

## Energiezähler

Alle EMON-Werte sind kumulierte Energiezähler in kWh und keine aktuelle
elektrische oder thermische Leistung.

| PointT-Pfad | Bereich | Zugriff |
|---|---|---:|
| `/heatSources/emon/totalConsumption` | Gesamt | R |
| `/heatSources/emon/chConsumption` | Heizung | R |
| `/heatSources/emon/dhwConsumption` | Warmwasser | R |
| `/heatSources/emon/coolingConsumption` | Kühlung | R |

Jede vorhandene EMON-Antwort kann folgende Teilwerte enthalten:

| Rohschlüssel | Verständlicher Name | Regel |
|---|---|---|
| `compressor` | Stromverbrauch Wärmepumpe | Nicht „Kompressorenergie“ nennen; kann zugeordneten Standby-Verbrauch enthalten |
| `eheater` | Stromverbrauch elektrischer Zuheizer | Fehlend ist nicht automatisch null |
| `electricity` | Stromverbrauch | Direkten Wert bevorzugen, falls vorhanden |
| `outputProduced` | Erzeugte Wärme bzw. Kühlenergie | Im Kühlbereich „Erzeugte Kühlenergie“ anzeigen |

Abgeleitete Gesamtwerte:

- **Stromverbrauch gesamt**: direkter `electricity`-Wert oder nur bei
  vollständigen Komponenten `compressor + eheater`.
- **Umweltenergie gesamt**:
  `outputProduced - compressor - eheater`, nur bei vollständigen, endlichen,
  nicht negativen Werten und nicht negativem Ergebnis. Die Entität heißt
  **Gesamt – Umweltenergie (berechnet)**, damit sie nicht mit einem direkt von
  PointT gelieferten Messwert verwechselt wird.
- Keine Umweltenergie pro Heizung, Warmwasser oder Kühlung berechnen. Die
  PointT-Zuordnung von Standby-Verbrauch kann dort irreführende negative
  Ergebnisse erzeugen.
- Ein Kühlbereich mit HTTP 200 und ausschließlich nullwertigen Zählern beweist
  noch keine vorhandene Kühlfunktion.

## Systemwerte

| PointT-Pfad | Verständlicher Name | Zugriff | HA-Abbildung / Hinweis |
|---|---|---:|---|
| `/system` | Systemressourcen | R | Intern: Erkennung |
| `/system/awayMode` | Abwesenheitsfunktionen | R | Intern: Erkennung |
| `/system/awayMode/enabled` | Abwesenheitsmodus | R/W | Schalter, nach realem Schreibtest |
| `/system/brand` | Anlagenmarke | R | Diagnose |
| `/system/bus` | Systembus | R | Diagnose |
| `/system/country` | Anlagenland | R | Diagnose-Entität, standardmäßig deaktiviert |
| `/system/dateTime` | Anlagenzeit | R/W | Nicht als Steuerung veröffentlichen |
| `/system/info` | Systeminformationen | R | Statischer Text-Diagnosesensor mit Modulnamen und Versionen; bereinigte Details als Attribute, interne Tokenfelder werden verworfen |
| `/system/sensors` | Systemsensoren | R | Intern: Erkennung |
| `/system/sensors/temperatures` | Temperatursensoren | R | Intern: Erkennung |
| `/system/sensors/temperatures/outdoor_t1` | Außentemperatur | R | Temperatursensor |
| `/system/sensors/temperatures/outdoorTemperatureSource` | Quelle der Außentemperatur | R | Diagnose |
| `/system/type` | Anlagentyp | R | Diagnose |
| `/system/variableTariff` | Variabler Stromtarif | R | Intern: Erkennung |
| `/system/variableTariff/supportStatus` | Unterstützung variabler Stromtarife | R | Diagnose |

## Gatewaywerte

| PointT-Pfad | Verständlicher Name | Zugriff | HA-Abbildung / Hinweis |
|---|---|---:|---|
| `/gateway` | Gatewayressourcen | R | Intern: Erkennung |
| `/gateway/brand` | Gatewaymarke | R | Geräteinformation |
| `/gateway/dateTime` | Gatewayzeit | R | Diagnose |
| `/gateway/serialId` | Seriennummer | R | Geräteinformation und standardmäßig deaktivierte Diagnose-Entität |
| `/gateway/swPrefix` | Softwarefamilie | R | Diagnose |
| `/gateway/thirdPartyLicenseInformation` | Lizenzinformationen | R | Keine Entität |
| `/gateway/tosAccepted` | Zustimmung zu Nutzungsbedingungen | R/W | Verwaltungswert; niemals als Steuerung anbieten |
| `/gateway/tzInfo` | Zeitzoneninformationen | R | Intern: Erkennung |
| `/gateway/tzInfo/timeZone` | Zeitzone | R/W | Diagnose; nicht aus Home Assistant schreiben |
| `/gateway/update` | Updateinformationen | R | Intern: Erkennung |
| `/gateway/update/status` | Gateway-Updatestatus | R | Diagnose |
| `/gateway/uuid` | Gateway-UUID | R | Diagnose-Entität, standardmäßig deaktiviert |
| `/gateway/versionFirmware` | Firmwareversion | R | Geräteinformation und Diagnose-Entität |
| `/gateway/versionHardware` | Hardwareversion | R | Geräteinformation und Diagnose-Entität |
| `/gateway/wifi` | WLAN-Informationen | R | Nicht veröffentlichen |
| `/gateway/wifi/ip` | IP-Informationen | R | Nicht veröffentlichen |
| `/gateway/wifi/ip/ipv4` | IPv4-Adresse | R | Nicht veröffentlichen |
| `/gateway/wifi/mac` | MAC-Adresse | R | Nicht veröffentlichen |
| `/gateway/wifi/ssid` | WLAN-Netze/SSID | R | Nicht veröffentlichen |

## Schreibfreigabe

Die 24 vom Referenzsystem als schreibbar gemeldeten Ressourcen werden nicht
automatisch als Bedienelemente veröffentlicht. Die erste sichere Freigabestufe
umfasst:

- Betriebsart je dynamisch gefundenem Heizkreis;
- manueller Raumsollwert und vorhandene Temperaturniveaus;
- Warmwasser-Betriebsart und vorhandene Temperaturniveaus;
- Extra-Warmwasser mit Dauer und Solltemperatur;
- Abwesenheitsmodus.

Zeitprogramme, Regelungsart, maximale Vorlauftemperatur, Namen sowie alle
Gateway-, Zeit- und Verwaltungswerte bleiben zunächst nur lesbar oder intern.
Sie benötigen zusätzliche Geräteprofile, Grenzwertnachweise und reale
Schreib-/Rücklesetests.

Diese Freigabestufe ist als dynamische Auswahlfelder, Zahlenregler und Schalter
umgesetzt. Ein Bedienelement entsteht nur bei exakt passendem Pfad und
Datentyp. Auswahlwerte müssen vollständig vom Gateway angekündigt werden;
Zahlenregler übernehmen dessen Grenzen nur innerhalb zusätzlicher sicherer
Grenzen. Die Transaktion verwendet PUT ohne automatische Wiederholung und
bestätigt den Zustand anschließend durch bis zu drei gestaffelte Einzelabrufe.
Der Ablauf **Manuell → Auto → Manuell** der Heizkreis-Betriebsart war auf dem
K40 erfolgreich; für die übrigen Regler stehen reale Einzeltests noch aus.
