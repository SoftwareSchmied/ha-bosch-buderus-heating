# Resource names and languages / Ressourcennamen und Sprachen

## English

I corrected all 29 German fallback names identified in finding B03. Both
languages now share one path-specific mapping
in `resource_catalog.py`. Matching keeps the parent path and replaces only one
circuit or zone ID. Unknown paths retain their existing fallback.

I keep entity names and logical circuit/group names in the Home Assistant
system language. Home Assistant 2026.10.1 also loads native entity-name
translations in that language and stores the original name centrally.
Moving these dynamic names into translation JSON would not make them follow
each profile.

The profile language controls translated dialog headings, field labels and
known enum option labels. Generated circuit choices under “Apply for” use the
system language too. A custom HA entity name takes precedence. Names supplied
by the installation are retained except for the existing translation of
recognized vendor defaults.

After changing the system language, reload the integration or restart Home
Assistant to refresh generated names. A profile-language change alone does
not rename entities. I add no registry migration: existing unique IDs,
entity IDs, user names and user-disabled entries stay in place. Statistics
continue to use the existing entity IDs. Newly created entities can naturally
receive an ID derived from the current system-language name.

The holiday form continues to show date and time beside each other, without
a seconds input, and a delete button for an existing period.

## Deutsch

Ich habe die 29 englischen Ersatznamen aus Befund B03 ergänzt. Die gemeinsame
Zuordnung berücksichtigt den vollständigen Pfad.
Dadurch wird etwa `gas` im Energiepreis-Pfad als „Gaspreis“ bezeichnet,
ohne andere Gaswerte umzubenennen.

Entitätsnamen, logische Gruppen und erzeugte Kreisauswahlen folgen der
Home-Assistant-Systemsprache. Auch die native Übersetzung von Entitätsnamen
arbeitet in HA 2026.10.1 auf dieser Grundlage. Die Profilsprache bestimmt
übersetzte Dialogüberschriften, Feldnamen und bekannte Auswahltexte.
Deshalb kann ein englischer Urlaubsdialog die deutsche Kreisauswahl
„Heizkreis 1“ enthalten.

Eigene Entitätsnamen haben Vorrang. Von der Anlage übernommene Namen bleiben
ebenfalls erhalten; bekannte allgemeine Herstellernamen werden weiterhin
wie bisher übersetzt. Nach einem Wechsel der Systemsprache aktualisiert
ein Neuladen der Integration oder ein HA-Neustart die erzeugten Namen.
Die Profilsprache allein löst keine Umbenennung aus.

Ich ändere keine Registry-Identitäten und ersetze keine selbst vergebenen
Namen. Bestehende IDs, vom Nutzer deaktivierte Entitäten und die Zuordnung
zu Statistikreihen bleiben erhalten. Bei neu angelegten Entitäten kann die
aktuelle Systemsprache wie üblich auch die erstmalige Entitäts-ID prägen.

## Reviewed names / Geprüfte Namen

`*` denotes exactly one advertised circuit or zone ID.
`*` steht für genau eine gemeldete Kreis- oder Zonenkennung.

| Path / Pfad | Deutsch | English |
|---|---|---|
| `/heatSources/hybrid/controlStrategy` | Hybrid-Regelungsstrategie | Hybrid control strategy |
| `/heatSources/hybrid/outdoorStatus` | Verbindungsstatus der Außeneinheit | Outdoor unit connection status |
| `/heatSources/hybrid/outdoorVariant` | Variante der Außeneinheit | Outdoor unit variant |
| `/heatSources/hybrid/reminderDate` | Energiepreis-Erinnerung: Datum | Energy price reminder date |
| `/heatSources/hybrid/reminderEnable` | Energiepreis-Erinnerung aktiviert | Energy price reminder enabled |
| `/heatSources/hybrid/reminderLapsed` | Energiepreis-Erinnerung fällig | Energy price reminder due |
| `/heatingCircuits/*/cooling/roomTempSetpoint` | Kühlen: Raum-Solltemperatur | Cooling: room setpoint |
| `/heatingCircuits/*/cooling/temperatureLevels/on` | Kühlen: Temperaturstufe | Cooling: target temperature level |
| `/heatingCircuits/*/suWiCoolingThreshold` | Kühlbetrieb ab Außentemperatur | Start cooling at outdoor temperature |
| `/heatingCircuits/*/suWiSwitchMode` | Sommer-/Winterumschaltung | Summer/winter switching |
| `/heatingCircuits/*/suWiThreshold` | Heizbetrieb bis Außentemperatur | Stop heating at outdoor temperature |
| `/heatingCircuits/*/temporaryRoomSetpoint` | Vorübergehender Raum-Sollwert | Temporary room setpoint |
| `/pool/setpointTemp` | Pool-Solltemperatur | Pool target temperature |
| `/solarCircuits/*/pumpModulation` | Modulation der Solarpumpe | Solar pump modulation |
| `/system/energyTariff/electricity` | Strompreis | Electricity price |
| `/system/energyTariff/gas` | Gaspreis | Gas price |
| `/system/energyTariff/oil` | Ölpreis | Oil price |
| `/system/energyTariff/pv` | PV-Strompreis | PV electricity price |
| `/system/lowNoise/mode` | Leiser Betrieb: Betriebsart | Low-noise operation mode |
| `/system/seasonOptimizer/coolingThreshold` | Kühlbetrieb ab Außentemperatur | Start cooling at outdoor temperature |
| `/system/seasonOptimizer/heatingThreshold` | Heizbetrieb bis Außentemperatur | Stop heating at outdoor temperature |
| `/ventilation/*/sensors/supplyTemp` | Zulufttemperatur | Supply air temperature |
| `/system/awayMode/temperature` | Abwesenheitstemperatur | Away temperature |
| `/dhwCircuits/*/sensor/airBoxTemperature` | Lufttemperatur im Gerätegehäuse | Casing air temperature |
| `/dhwCircuits/*/sensor/exhaustFlueGasTemperature` | Abgastemperatur | Exhaust gas temperature |
| `/dhwCircuits/*/sensor/externalTankTemperature` | Externe Speichertemperatur | External tank temperature |
| `/dhwCircuits/*/sensor/heatExchangerFlueGasTemperature` | Abgastemperatur am Wärmetauscher | Heat exchanger flue gas temperature |
| `/dhwCircuits/*/sensor/heatExchangerTemperature` | Wärmetauschertemperatur | Heat exchanger temperature |
| `/dhwCircuits/*/monitorValues` | Warmwasser-Messwerte | Hot water monitoring values |

## Terminology references / Begriffszuordnung

The names above describe the same resource in both languages.

- The six hybrid paths belong to the hybrid heat-source resource.
  `outdoorStatus` reports `connected` or `notConnected`.
  “Outdoor status” therefore describes the outdoor-unit connection.
  The reminder resources refer to reminders to update energy prices.
- `cooling/temperatureLevels/on` is a numeric temperature resource. Its label
  must not imply an on/off control. `suWiThreshold` and
  `suWiCoolingThreshold` are the outdoor-temperature thresholds for stopping
  heating and starting cooling.
- The pool, solar, energy-tariff, low-noise, season-optimizer and ventilation
  names follow their respective path contexts.
- The RRC away temperature identifies the away-mode temperature. WDDW2 sensor
  resources distinguish casing-air, exhaust-gas and heat-exchanger
  temperatures. The box air temperature refers to the appliance casing.
- WDDW2 `monitorValues` contains collected readings. The plural name
  deliberately identifies a collection. A translated name does not prove that
  this resource is a scalar sensor. Its response shape and extraction belong
  to P03.
- I rechecked the 46 text pairs T001 to T046. The mapping
  `eco = Eco+`, `low = Eco`, `high = Comfort / Komfort` remains correct.
  “Sole ansaugen” describes English “Brine draw”. Regeneration-code casing is
  handled by P04 in this beta. T047 to T049 remain unchanged.

## Validation / Prüfung

I ran 1,727 automated test cases, including six new cases for path context
and real HA platform reloads in both system-language directions.
Combined line and branch coverage is 96.70%. Ruff, formatting and strict
Mypy passed. The 29 original name rows were evaluated in DE and EN;
all 398 JSON keys, placeholders and the English source catalog agree.

The HA reload tests retain custom names, registry IDs, entity IDs,
user-disabled entries and the start counter's identity and state class.
The full suite also includes the existing Recorder tests.

I also checked the actual HA 2026.10.1 frontend (20260930.3) in Microsoft Edge,
using an isolated local instance and synthetic PointT resources:

| System language | Profile language | Generated names and circuit choices | Holiday dialog and Comfort option |
|---|---|---|---|
| German | English | German | English |
| English | German | English | German |

Both browser runs retained the same registry IDs, entity IDs, custom name
“Mein Wohnzimmer” and user-disabled entity. The date and minute clock remained
beside each other; the existing-period delete button was visible. I inspected
the screenshots as well as the actual fields and registry entries.

Ich habe beide Sprachkombinationen im tatsächlichen HA-Frontend mit
synthetischen Daten geprüft. Eigene Namen, IDs und die Deaktivierung blieben
erhalten. Der Urlaubsdialog behält Datum und Uhrzeit nebeneinander und den
Löschbutton. Diese Prüfung ersetzt keinen Test an der realen Heizungsanlage.
P02 ist in v0.8.2-beta.5 enthalten. Eine Installation auf der realen Anlage
wurde für diese Prüfung nicht vorgenommen.

## HA sources / HA-Quellen

- [Entity-platform translations, HA 2026.10.1](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/helpers/entity_platform.py):
  `async_load_translations` uses `hass.config.language`.
- [Entity names](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/helpers/entity.py):
  `name` and `_name_internal` select the provided/translated name.
- [Entity registry](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/helpers/entity_registry.py):
  stored original names, user overrides and stable entity identity.
