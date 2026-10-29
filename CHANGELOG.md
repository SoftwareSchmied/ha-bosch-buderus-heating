# Changelog

All notable changes will be documented here. The project follows Semantic
Versioning after its first tagged preview.

## [Unreleased]

### Read-only time programs

Heating and hot-water time programs (`switchPrograms/A`, also `B` where a
gateway reports it) are shown as read-only diagnostic sensors (#95). The state
is the level the program currently selects, for example **Heating** or
**Reduced** for a heating circuit and **Eco** or **Off** for hot water. The
attributes list the weekly switch points as local wall-clock times
(`HH:MM level`) and the limits the device reports: total switch points,
switch points per day and the time raster in minutes.

The shape is validated strictly: weekdays `Mo`…`Su`, minutes after midnight
within the raster, level keys of the referenced temperature levels and the
reported limits. Unexpected shapes create no sensor. The program is read in the
15/30-minute slow group; the state changes locally at the next switch point
without an extra cloud request. Hot-water program A is probed directly because
the program list can be forbidden while the program itself is readable. Editing
switch points is not offered. Switch times are not written to diagnostics.

The structure was confirmed on a Logatherm WLW186i-12 TP70 with MX400; the
circulation program returned HTTP 403 there and is not shown. The fixture
contains constructed times only.

#### Deutsch

Zeitprogramme für Heizung und Warmwasser (`switchPrograms/A`, sowie `B`, wenn
ein Gateway es meldet) erscheinen als schreibgeschützte Diagnose-Sensoren
(#95). Der Zustand ist die Stufe, die das Programm gerade wählt, z. B.
**Heizen** oder **Absenken** beim Heizkreis und **Eco** oder **Aus** beim
Warmwasser. Die Attribute zeigen die wöchentlichen Schaltpunkte als lokale
Uhrzeiten (`HH:MM Stufe`) und die vom Gerät gemeldeten Grenzen: Schaltpunkte
insgesamt, Schaltpunkte pro Tag und das Zeitraster in Minuten.

Die Struktur wird streng geprüft; unerwartete Formen erzeugen keinen Sensor.
Gelesen wird in der langsamen Gruppe, der Zustand wechselt lokal zum nächsten
Schaltpunkt ohne zusätzliche Cloud-Abfrage. Schaltpunkte lassen sich nicht
bearbeiten und erscheinen nicht in der Diagnose. Bestätigt an einer
Logatherm WLW186i-12 TP70 mit MX400; das Zirkulationsprogramm lieferte dort
HTTP 403 und wird nicht angezeigt.

## [0.8.2] - 2026-10-27

I am releasing **Bosch/Buderus Home Comfort 0.8.2 as Stable/Latest**. It brings
together the changes from all seven 0.8.2 betas, the compatible maintenance
updates from PRs #90, #91 and #92, and the sign-in guide improvements from #93.

I compared the installation archive with the published Beta.7 package: both
contain the same 97 files, and only the manifest version differs. The stable
release retains Beta.7's integration behavior.

### Updating from 0.8.1

I recommend a full Home Assistant backup before updating because this release
includes substantial changes to discovery, connections, entity types and
value conversions. Update the existing integration through HACS, restart
Home Assistant and reload the browser. Check the readings and controls used
by your automations after the update.

The display name changes from **Bosch/Buderus Heating** to
**Bosch/Buderus Home Comfort**. The repository, integration domain and ZIP
filename stay the same. Existing entry titles, entity IDs and custom names
are retained.

### Everyday heating and hot-water controls

I added **Configure > Operate appliance**, with heating-circuit, hot-water and
system tabs. Edits remain a draft until **Apply**; **Discard** and **Escape**
leave without applying the draft. Additional settings stay available, and
device-page number controls use input fields instead of sliders.

Eligible PointT circuits also gain native climate and water-heater entities.
The hot-water entity combines measured temperature, the reported active
target and offered operating modes. Its **active target is read-only**;
the separate Eco, Eco+ and Comfort start-temperature controls retain their
existing meaning. Native and individual entity controls write immediately.
I validate current permissions, options and limits and confirm writes through
read-back. Several edits remain separate writes, so an earlier confirmed
change stays applied if a later one fails.

### Discovery, readings and connection recovery

I improved recovery of manual setpoints and start counters, including
independent general and per-heat-source counters and whitespace units.
Existing entities recover within the normal discovery and polling limits.

I keep shared token refresh running when a waiting request is cancelled and
preserve account authentication state across retries and reloads. Delayed
responses cannot overwrite a newer sign-in, rejected refresh tokens are not
repeatedly submitted by the same token manager, and account pauses survive
OAuth request limits. Reconfiguration uses the update listener for reloads,
addressing the reported Home Assistant 2026.12 warning.

I also included corrections for partial updates, stale values, concurrent
changes, write confirmation, and temperature, runtime and energy conversions.
Diagnostics retain schema 27. These fixes do not establish the cause of every
reported outage; issue #49 remains open for follow-up.

### Additional supported device families

I included the Midea/RWH water-heater, Midea Climate Series, seven Air purifier
families and Dry 4000i/6000i/8000i adapters, plus read-only PointT wallbox
monitoring. Controls follow each appliance's reported capabilities. Room-zone
controls, editing of existing RRC weekly-program points, ICOM holiday periods
and the separate WDDW2 holiday mode are also included.

These additional families still need broader physical-device confirmation.
WE800i controls remain disabled by default pending field feedback. Wallbox
commands, consumption history and regeneration logs are not included.

### Setup, HACS and maintenance

The integration is available through the regular HACS catalog. I corrected
the README image and expanded the sign-in guide with **Preserve log**, Console
filter checks, fresh sign-in attempts and help for a missing redirect address.
The ten-minute attempt lifetime and the details needed for troubleshooting
are explained without asking users to share credentials. An automatic sign-in
helper is not included, and the empty-Console report is not yet confirmed as
resolved.

The earlier HACS download problem involving `Got more than 8190 bytes when
reading` was addressed upstream by a GitHub change and Home Assistant
2026.10.4, according to the [HACS maintainer's update](https://github.com/hacs/integration/issues/5643#issuecomment-6395219761).
The proposed HACS patch was closed without merging. If that exact error
persists on an older Home Assistant version, update Home Assistant to
2026.10.4 or newer and retry; this integration does not patch HACS.

I updated Hypothesis, the Home Assistant test package, Ruff and the pinned
GitHub Actions. I retained pytest 9.0.3 because the updated Home Assistant
test package requires it. The integration's runtime dependencies are unchanged.

### Validation and remaining field checks

I verified **3,190 automated test cases** on Home Assistant 2026.10.2 with
**96.78% combined statement and branch coverage**, including parametrized
variants. Ruff, formatting, strict Mypy, reproducible packaging and isolated
ZIP imports passed. The isolated runtime dependency audit found no known
vulnerabilities. The final GitHub checks and published-package comparison are
required before completing this release and are recorded in
[Creating a release](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.2/docs/release.md#082-verification).

On my Buderus installation, the earlier Beta.7 checks confirmed a manual
heating target change and restoration, plus hot water Auto to Off and back
to Auto, through the new controls. I checked both directions in MyBuderus
and restored the original settings. I performed no additional installation
or appliance writes for this publication. Other appliances, operating modes,
real outage recovery and long-term stability still need their own checks.

## [0.8.2-beta.7] - 2026-10-25

> [!IMPORTANT]
> **Known HACS download issue**
>
> If HACS reports `Could not download` and the log contains `Got more than 8190 bytes when reading`, an oversized GitHub response header can block the download. I reproduced this with HACS 2.0.5 while installing Beta.6. Updating this integration does not fix HACS itself.
>
> See [HACS issue #5643](https://github.com/hacs/integration/issues/5643) and the [proposed correction in PR #5644](https://github.com/hacs/integration/pull/5644), which remains open as of 25 October 2026. If you apply that local HACS correction, restart Home Assistant before retrying the download.

> [!IMPORTANT]
> **Please test with caution: this beta includes the major changes introduced in v0.8.2-beta.6.**
> In Beta.6, I added new device families and Home Assistant device classes and substantially reworked connections, discovery, state processing and value conversions. Beta.7 builds on those changes and adds the controls described below. Regressions can also affect existing installations. I recommend a full Home Assistant backup before updating and a deliberate check of readings and controls afterwards. **v0.8.1 remains Stable/Latest.**

### Easier everyday controls

- I added **Configure > Operate appliance**, with tabs for heating circuits, hot water and the system. Changes remain a draft until **Apply**. **Discard** or **Escape** leaves without applying the draft, and additional settings stay visible. Escape also works after applying changes or reloading values.
- I made the device page easier to scan with shorter labels, relevant icons and number inputs in place of sliders. Individual entity controls still write immediately. Existing entity IDs, custom names and the single device per heating gateway are retained.
- I added a native climate entity for each eligible PointT heating circuit. Manual mode allows validated target changes. Automatic mode displays the schedule's target without changing the schedule. Cooling and unknown operating contexts are not presented as heating controls.
- I added a native water-heater entity for each eligible PointT hot-water circuit, combining its measured temperature, reported active target and offered operating modes. **The active target is read-only.** The separate Eco, Eco+ and Comfort start-temperature controls keep their existing meaning.

I check current values, permissions and metadata before writing and confirm changes by reading them back. Unconfirmed results remain unavailable until regular polling confirms them. Applying several settings sends separate writes; an earlier confirmed change stays applied if a later change fails.

### More reliable sign-in and connection recovery

I keep token refresh running when a waiting request is cancelled and retain the shared account authentication state across retries and reloads. Delayed responses cannot overwrite a newer sign-in. OAuth request limits keep their existing account pause, and a rejected refresh token is not repeatedly submitted by the same token manager.

Reauthentication and configuration changes now use Home Assistant's update listener for reloads, addressing the reported 2026.12 warning. Ordinary token rotation does not reload the integration. These corrections address reproduced software defects; they do not establish the cause of every reported outage or close issue #49.

### New display name

I changed the visible name to **Bosch/Buderus Home Comfort** to reflect the wider range of devices. The repository, Home Assistant domain, component folder and ZIP filename remain unchanged. Existing entry titles and custom device/entity names are retained. Update the existing installation through HACS.

I also corrected the README logo for the HACS description and updated the English and German operating guides.

### Validation and remaining checks

The combined suite passed **3,190 automated test cases**, including separately counted parameter variants, with **96.78% combined statement and branch coverage**. Ruff, formatting, strict Mypy, the JavaScript checks, both complete Home Assistant browser tests, reproducible packaging and isolated ZIP imports passed. GitHub also checks HACS, Hassfest, dependencies and CodeQL.

On my Buderus installation with Home Assistant 2026.10.3, I changed the manual heating target from 19.0 to 18.5 °C through the controls dialog and restored 19.0 °C through the native climate entity. I also changed hot water from Auto to Off and back to Auto through the native water-heater dialog. I checked both directions in MyBuderus and restored all changed settings. These tests confirm those operations on my installation; other appliances, modes and real outage scenarios still need their own checks.

The new device families introduced in Beta.6 still need broader field confirmation. WE800i controls remain disabled by default pending feedback in issue #60. P15, consumption history and regeneration logs remain excluded. **An automatic sign-in helper is not included in this release.** Initial sign-in still uses the documented SingleKey ID procedure; a helper is being investigated for a later beta.

Select **v0.8.2-beta.7** explicitly in HACS, update the existing integration, restart Home Assistant and reload the browser. Check readings before testing controls individually. If something fails, record the exact model and steps and download diagnostics before reloading the integration. Review diagnostics for personal information before sharing them.

## [0.8.2-beta.6] - 2026-10-24

### Deutsch

**Bitte mit Vorsicht testen: Diese Beta enthält sehr viele Änderungen.**
Ich habe neue Gerätefamilien und Home-Assistant-Geräteklassen ergänzt und
größere Teile von Verbindung, Discovery, Zustandsverarbeitung und
Werteumrechnung umgebaut. Dadurch können auch bei bereits eingerichteten
Anlagen Fehler auftreten. Ich empfehle ein vollständiges Home-Assistant-Backup
vor dem Update und eine bewusste Prüfung der Anzeigen und Bedienfunktionen
danach. **v0.8.1 bleibt Stable/Latest.**

#### Neue Geräte und Funktionen

- Ich habe Midea- und RWH-Warmwassergeräte als native Warmwasserentitäten
  ergänzt, mit Temperaturen, angebotenen Betriebsarten und Sollwerten.
  Zusätzliche Anzeigen für Urlaub, Desinfektion und Heizstäbe sind lesend.
- Ich habe die Midea Climate Series als native Klimaentitäten mit
  Temperaturen, Betriebsarten, Lüfterstufen und Schwenkrichtungen ergänzt.
- Ich habe sieben Air-Luftreinigerfamilien als native Lüfter und Dry 4000i,
  6000i und 8000i als native Luftentfeuchter eingebunden. Je nach Gerät stehen
  zusätzliche Messwerte, Betriebsarten, Sollwerte und Einstellungen bereit.
- Ich habe acht reine Anzeigen für erkannte PointT-Wallboxes ergänzt.
  Wallbox-Bedienfunktionen sind noch nicht enthalten.
- Ich habe Raumzonen-Bedienelemente für Heizen und Kühlen sowie RRC-Zonen,
  die Auswahl vorhandener Heizprogramme und Dialoge zum Bearbeiten
  bestehender Wochenschaltpunkte ergänzt.
- Ich habe ICOM-Urlaubszeiten in Kalender und Dialog eingebunden. WDDW2
  erhält einen eigenen Ein-/Aus-Urlaubsmodus ohne Kalenderzeiten.

Die neuen Geräte erscheinen nur, wenn sie über das Konto erreichbar sind,
erkannt und bei der Einrichtung oder Neukonfiguration ausgewählt werden.
Die einzelnen Anzeigen und Bedienelemente hängen von den tatsächlich
gemeldeten Fähigkeiten ab. Ich behalte ein Anlagengerät je bestehendem
PointT-Heizungs-Gateway bei. Zusätzliche Cloud-Geräte und Wallboxes erhalten
eigene Gerätekennungen.

#### Umbau und Korrekturen

- Ich habe eine zusätzliche Cloud-Verbindung mit Geräteauswahl, gemeinsamer
  Token-Erneuerung, Wiederverbindung und getrennten Verbindungsanzeigen
  ergänzt. Ein Ausfall dieser optionalen Verbindung blockiert die Einrichtung
  einer erreichbaren Heizung nicht.
- Ich habe die Verarbeitung von Teilmeldungen und die Aktualitätsprüfung
  einzelner Werte korrigiert. Die Annahme eines Schreibauftrags gilt erst
  mit einem passenden gemeldeten Zustand als bestätigt. Abgebrochene
  RRC-Schreibzugriffe hinterlassen keinen vermeintlich bestätigten Zustand.
- Ich habe Laufzeit-, Energie- und Stromumrechnungen sowie Taupunkt und
  Temperaturdifferenzen korrigiert. Aktuelle Metadaten werden berücksichtigt;
  ungeklärte oder ungültige Werte bleiben unbekannt. Frühere Statistiken
  werden nicht automatisch berichtigt.
- Ich habe die Salzwarnung und die Behandlung kleiner Rundungsabweichungen
  bei Warmwasser-Sollwerten korrigiert. Die Grenz- und Schrittweitenprüfung
  bleibt erhalten.
- Ich habe Diagnose-Schema 27 und die deutschen und englischen Texte
  aktualisiert. Die neuen Diagnoseangaben enthalten keine aktuellen
  Anlagenwerte und lösen keine zusätzlichen Diagnoseabfragen aus.
- Ich habe GitHub Discussions und eine Vorlage für Installationsberichte
  verlinkt. Nachvollziehbare Fehler bleiben in Issues.

#### Prüfstand und Update

Ich habe **3.036 automatisierte Testfälle** geprüft, einschließlich separat
gezählter Testvarianten. Die kombinierte Code- und Verzweigungsabdeckung
beträgt **96,83 %**. Ruff, Formatprüfung, striktes Mypy, reproduzierbare
Paketerstellung und Import aus einer isolierten ZIP-Installation sind
bestanden. Deutsche und englische Texte haben dieselben 585
Übersetzungsschlüssel und passende Platzhalter.

Diese Ergebnisse ersetzen keine Prüfung am Gerät. Für die neuen
Gerätefamilien fehlen weiterhin praktische Bestätigungen von Anzeigen,
Wiederverbindung und Schreibwirkung. Die WE800i-Bedienelemente bleiben
standardmäßig deaktiviert; dafür wird weiterhin die Rückmeldung aus
Issue #60 benötigt. Verbrauchshistorie und Regenerationsprotokoll sind
nicht enthalten.

Ich empfehle, Beta.6 ausdrücklich in HACS auszuwählen, nach dem Update
Home Assistant neu zu starten und den Browser neu zu laden. Bitte zuerst
die Werte prüfen und Bedienfunktionen bewusst einzeln testen, bevor
Automationen darauf aufbauen. Bei Problemen helfen die genaue
Gerätebezeichnung, die beobachteten Schritte und ein frischer Diagnoseexport,
der vor Veröffentlichung auf persönliche Angaben geprüft wurde.

### English

**Please test with caution: this beta contains a large number of changes.**
I added new device families and Home Assistant device classes and substantially
reworked connections, discovery, state processing and value conversions.
These changes may also cause regressions on existing installations. I
recommend a full Home Assistant backup before updating and a deliberate
check of readings and controls afterwards. **v0.8.1 remains Stable/Latest.**

#### New devices and functions

- I added native water-heater entities for Midea and RWH devices, with
  temperatures, offered operating modes and targets. Additional holiday,
  disinfection and heater indicators are read-only.
- I added native climate entities for the Midea Climate Series, with
  temperatures, operating modes, fan stages and swing directions.
- I added native fans for seven Air purifier families and native
  dehumidifiers for Dry 4000i, 6000i and 8000i. Additional readings, modes,
  targets and settings depend on the device.
- I added eight read-only sensors for identified PointT wallboxes.
  Wallbox controls are not included.
- I added heating and cooling zone controls, RRC zones, selection of existing
  heating programs and dialogs for editing existing weekly switch points.
- I added ICOM holiday periods to the calendar and dialog. WDDW2 has a
  separate on/off holiday mode without calendar dates.

New devices appear only when they are reachable through the account,
identified and selected during setup or reconfiguration. Individual readings
and controls depend on their reported capabilities. I retain one system
device per existing PointT heating gateway. Additional cloud appliances and
wallboxes receive separate device identities.

#### Restructuring and corrections

- I added a separate cloud connection with device selection, shared token
  refresh, reconnection and individual connection indicators. An outage of
  this optional connection does not block setup of a reachable heating system.
- I corrected partial-message processing and freshness tracking for each
  field. An accepted write request is confirmed only by a matching reported
  state. Cancelled RRC writes no longer leave an apparently confirmed state.
- I corrected duration, energy and current conversions, dew point and
  temperature differences. Current metadata is honored; unresolved or
  invalid readings remain unknown. Earlier statistics are not corrected
  automatically.
- I corrected the salt warning and tiny floating-point conversion errors
  in water-heater targets. Range and step validation remain in place.
- I updated diagnostics to schema 27 and revised German and English text.
  The new diagnostic fields contain no current appliance values and do not
  trigger additional diagnostic requests.
- I linked GitHub Discussions and an installation-report template.
  Reproducible bugs continue to be tracked in Issues.

#### Validation and updating

I checked **3,036 automated test cases**, including separately counted
parameter variants, with **96.83% combined statement and branch coverage**.
Ruff, format validation, strict Mypy, reproducible packaging and import
from an isolated ZIP installation passed. German and English text use the
same 585 translation keys with matching placeholders.

These results do not replace physical-device testing. Readings, reconnection
and write effects still need field confirmation for the new device families.
WE800i controls remain disabled by default, pending the response in Issue #60.
Consumption history and regeneration logs are not included.

I recommend explicitly selecting Beta.6 in HACS, restarting Home Assistant
after the update and reloading the browser. Please check readings first and
test controls individually before relying on them in automations. For problems,
the exact device model, reproduction steps and a fresh diagnostic export
are useful. Check the export for personal information before publishing it.

## [0.8.2-beta.5] - 2026-10-23

I am releasing the current device-support improvements and the first three
completed comparison packages together as a beta.

- I added initial WE800i discovery and readings for salt, available soft water,
  flow, regeneration and holiday mode. Known regeneration phases also accept
  upper- and mixed-case spellings. Unknown or invalid values remain unknown.
- I prepared controls for the salt threshold, holiday mode and regeneration
  scheduling, plus a separate regeneration start button. They are disabled by
  default pending physical-device confirmation. Writes require current device
  metadata and read-back; an uncertain command is not blindly repeated.
- I added six heating/cooling readings for each reported room zone and the
  DHW device type. Existing entities recover through regular polling after
  invalid responses, with their identities preserved.
- I corrected the WDDW2 safety-temperature state and added scalar start
  counters for individual heat sources. These counters remain separate from
  structured part counters and their statistics.
- I filled 29 German resource-name gaps, clarified ambiguous names and checked
  system-language entity names alongside profile-language dialogs.
  Diagnostics schema 17 explains validation and entity registration without
  exposing measurements or private identifiers.

I ran **1,767 automated test cases**, including parameterized variants, with
**96.70% combined statement and branch coverage**. Ruff, formatting and strict
Mypy passed. Tests include reproducible packaging and an isolated ZIP import.
The language checks include the actual Home Assistant frontend with synthetic
data. These checks do not establish physical-device compatibility.

Physical WE800i and WDDW2 confirmation, the additional zone readings and real
writes with the new softener controls remain outstanding. Available soft water
is the remaining volume, not consumption. Consumption from `/recordings`
remains deferred, and **issue #60 stays open**. The
[WE800i guide](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.2-beta.5/docs/water-softener-validation.md)
describes the requested checks.

Select **0.8.2-beta.5** explicitly in HACS, update and restart Home Assistant.
**0.8.1 remains Stable/Latest.**

### Deutsch

Ich veröffentliche den bisherigen Ausbau der Geräteunterstützung und die
ersten drei abgeschlossenen Vergleichspakete gemeinsam als Beta.

- Die erste WE800i-Unterstützung ergänzt Salzfüllstand, verfügbares Weichwasser,
  Durchfluss, Regeneration und Urlaubsmodus. Bekannte Regenerationsphasen werden
  auch bei großer oder gemischter Schreibweise erkannt. Ungültige Werte bleiben
  unbekannt.
- Salz-Warnschwelle, Urlaubsmodus, Regenerationsplanung und eine eigene Starttaste
  sind vorbereitet. Diese Bedienelemente bleiben bis zur Gerätebestätigung
  standardmäßig deaktiviert. Schreibzugriffe benötigen aktuelle Metadaten und
  eine Rücklesebestätigung; unsichere Befehle werden nicht blind wiederholt.
- Sechs zusätzliche Heiz-/Kühlanzeigen je gemeldeter Raumzone und der
  Warmwasser-Gerätetyp sind ergänzt. Nach ungültigen Antworten erholen sich
  dieselben Entitäten über die regulären Abfragen.
- Der WDDW2-Sicherheitszustand wird als Binärsensor angezeigt. Einzelne
  Wärmeerzeuger unterstützen zusätzlich skalare Startzähler mit eigener
  Identität und Statistik, getrennt von den strukturierten Teilzählern.
- Ich habe 29 deutsche Ressourcennamen ergänzt, mehrdeutige Namen präzisiert
  und System- sowie Profilsprache geprüft. Diagnose-Schema 17 beschreibt
  Validierung und Registrierung ohne Messwerte oder private Kennungen.

**1.767 automatisierte Testfälle** einschließlich parametrisierter Varianten
bestehen bei **96,70 % kombinierter Code- und Verzweigungsabdeckung**. Ruff,
Formatprüfung und striktes Mypy sind erfolgreich. Paketbau und isolierter
ZIP-Import sind geprüft. Die Sprachprüfung im tatsächlichen HA-Frontend
verwendet synthetische Daten.

Die Bestätigung am WE800i und an WDDW2-Anlagen, die zusätzlichen Zonenanzeigen
und reale Schreibtests der neuen Enthärterbedienung stehen noch aus.
Verfügbares Weichwasser bezeichnet den verbleibenden Vorrat. Die
Verbrauchsauswertung aus `/recordings` bleibt zurückgestellt; **Issue #60 bleibt
offen**. Die oben verlinkte Prüfanleitung ist auf Deutsch und Englisch verfügbar.

Bitte **0.8.2-beta.5** in HACS auswählen, aktualisieren und Home Assistant neu
starten. **0.8.1 bleibt Stable/Latest.**

## [0.8.2-beta.4] - 2026-10-22

I am incorporating CHLINDE's fix for start counters that report a whitespace-only
unit. The validation introduced in beta.2 rejected these otherwise valid
responses, leaving an existing sensor unknown or preventing a new sensor from
appearing.

- Ignore surrounding whitespace when validating the general counter and the
  counters of individual heat sources. Missing units and the dimensionless
  `other` unit remain supported; actual measurement units remain rejected.
- Preserve numeric validation, device error markers, entity identities,
  statistics metadata, recovery rules and polling intervals. This correction
  adds no cloud requests or write operations.
- Extend regression tests to cover both counter formats, malformed units,
  metadata changes, unavailable readings and recovery of the same entities.
  Home Assistant setup and reload tests also cover whitespace units with two
  synthetic gateways.

Thank you, @CHLINDE, for the correction, the regression tests and the confirmation
that the counter displays its value again on your Buderus G125 BE ECO-21. That
confirmation covers the reported counter; recovery scenarios and multiple heat
sources still need confirmation on physical installations.

Select **0.8.2-beta.4** explicitly in HACS, update and restart Home Assistant.
**0.8.1 remains Stable/Latest.** The
[English](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.2-beta.4/docs/start-counters.md)
and [German](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.2-beta.4/docs/start-counters.de.md)
guides describe the accepted units and the scope of validation.

## [0.8.2-beta.3] - 2026-10-21

I am releasing this beta with the HACS documentation updates from PR #73.
**0.8.1 remains the stable release and Latest.**

I updated the installation instructions following the integration's acceptance
into the default HACS catalog on 2026-10-20 through
[hacs/default#10040](https://github.com/hacs/default/pull/10040). Thank you to
the HACS maintainers for reviewing and accepting the submission.

Bosch/Buderus Heating can now be found directly in HACS search. I replaced the
temporary custom-repository instructions with search troubleshooting, marked
the catalog review as complete in the project status, and added a HACS badge.

Runtime behavior is unchanged from 0.8.2-beta.2. This includes the general
start counter, counter validation and recovery. Only the manifest version
changes inside the installation archive; the documentation is available in
the repository. The new version allows a regular HACS update.

Select this beta explicitly in HACS and restart Home Assistant after updating.
The revised counter recovery and multi-source behavior still need field
confirmation. I performed no physical appliance writes for this release.

## [0.8.2-beta.2] - 2026-10-21

I am releasing this beta with the start-counter improvements from PR #72.
**0.8.1 remains the stable release and Latest.**

I expose the general start counter reported by systems such as the Buderus
G125 BE ECO-21. Thank you, CHLINDE, for reporting the missing counter in
Issue #64, providing the initial implementation and testing its visibility.

I revised the counter handling in PR #72 so that general and per-heat-source
counters retain independent entities. A missing response or another counter
no longer causes the general entity to be hidden or removed. Existing IDs,
custom names and activation choices are preserved across reloads.

I use the same path validation for sensor creation and recovery. A supported
source ID containing a dot or a non-ASCII character can no longer be missed
when restoring an existing sensor after a failed startup read.

I validate whole-number counts and the resource's declared numeric error
markers consistently. Missing or invalid readings remain unknown, conflicting
parts are rejected, and unknown fields cannot create extra counter entities.
The general resource uses `floatValue`; per-source counters use `emonValue`.

I restore exact registered counter paths during bounded discovery and recover
known or advertised leaves through normal polling. The interval remains
15 minutes with the standard profile or 30 minutes with the cloud-friendly
profile. Recovering a previously omitted path can add reads; existing batch
limits, path budgets, forbidden-resource pauses and cloud backoff still apply.

I added schema 15 diagnostics with counter validation reasons and recovery
status, without exporting counts or raw error-marker values. The
[implementation report](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.2-beta.2/docs/start-counters.md) and its
[German version](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.2-beta.2/docs/start-counters.de.md) explain the previous weaknesses,
the changes and the scope of validation.

Select this beta explicitly in HACS and restart Home Assistant after updating.
Physical validation of the revised recovery and multi-source behavior is
still pending. CHLINDE confirmed visibility with the original implementation;
that feedback does not establish the revised recovery behavior on an appliance.

I verified 1,460 automated test cases with 96.64% combined statement and branch
coverage. Ruff, formatting and strict Mypy passed. The tests include actual
Home Assistant registry and recorder behavior, plus deterministic packaging
and an isolated import from the generated archive. Parametrized variants are
counted separately.

## [0.8.2-beta.1] - 2026-10-20

I am releasing this beta with the manual-setpoint recovery from PR #71 and the
pool-heating start counter correction from PR #70. **0.8.1 remains the current
stable release.**

I extended the manual-setpoint recovery contributed by CHLINDE. When a reported
heating circuit is in Manual and its setpoint is missing or unavailable, I allow
one series of up to three targeted reads, waiting 3, 10 and 20 seconds in turn.
Repeated failure polls do not start another series until the value has recovered
or the circuit has left Manual. Thank you, CHLINDE, for the initial recovery
implementation and tests.

I keep a setpoint missing at startup eligible for the normal control-group reads
while its circuit is in Manual. A later successful response adds its supported
entities and normal polling without a restart. Existing forbidden-resource
pauses, authentication handling and cloud rate limits remain effective. I also
stop pending recovery on unload and keep a targeted success from clearing an
unrelated failed polling cycle. See [request behavior](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.2-beta.1/docs/request-behavior.md)
for timing, request limits and the scope of the APK comparison.

I register the pool-heating start counter (`numberOfStarts` → `pool`) even
when the gateway's initial payload is empty or partial, with German and English
names. Values still depend on gateway support; polling is unchanged.

I verified the combined changes with 1,350 automated test cases on Home Assistant
2026.10.1 and 96.55% combined statement and branch coverage. Ruff, formatting and
strict Mypy passed. The tests include parametrized variants and an isolated
installation of the generated release archive. An isolated runtime dependency
audit found no known vulnerabilities.

I have not performed physical appliance writes for this beta. Recovery on the
affected installation still needs confirmation. After selecting the beta in
HACS and updating, restart Home Assistant.

## [0.8.1] - 2026-10-20

I am releasing 0.8.1 as the current stable version. It includes the holiday
dialog changes, the timezone corrections from the HACS review, and the startup
recovery and season-switching features from the 0.8.1 betas.

I unified holiday creation and editing in the existing integration dialog:

- Name, period and supported heating settings appear together. Each date has
  its clock beside it, including on mobile screens. Clock fields omit seconds.
- New dates start empty with an editable 08:00 clock. Existing holidays keep
  their saved times. Quarter-hour times remain required for changed endpoints.
- An end before or equal to the start produces a specific error at the end
  field. Entered values remain available for correction.
- A red **Delete holiday period completely** button opens confirmation directly
  from the edit form, even with an incomplete draft. Cancelling preserves the
  draft. Before deletion, I check that the saved holiday still matches the
  confirmed details.

I also corrected midnight writes to use 00:00 on the selected date. Differing
read-back values are shown without reporting a successful save. An unresolved
creation request cannot send a second POST during the loaded integration
session. This protection is shared by the dialog and calendar; it resets on
reload or restart. Existing timestamp values and unrelated concurrent changes
remain protected. The dialogs add no cloud requests when opened.

I resolved named gateway and per-period holiday zones asynchronously before
parsing or publishing updates, including write confirmation and adjusted
results. Notifications use Home Assistant's already loaded local zone. Invalid
zone names are cached within the existing bounds. Thank you, Frenck, for
identifying the blocking timezone lookups in the HACS review.

I included the beta improvements to startup recovery, temporary-failure handling
and supplementary metadata reads, with stable entity identities. The central
**Season switching** control uses only the options and write permission offered
by the installation. See [request behavior](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.1/docs/request-behavior.md)
and [season switching](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.1/docs/season-switching.md).

I included the compatible dependency and CI updates from PRs #61, #62 and #63.
Pytest stays at 9.0.3 because the updated Home Assistant test package requires it.

I verified 1,287 automated test cases on Home Assistant 2026.10.1 with 96.45%
combined statement and branch coverage, plus 366 targeted compatibility cases
on Home Assistant 2026.9.4. Ruff, formatting, strict Mypy and the isolated runtime
dependency audit passed. Local browser component checks covered desktop and
mobile layout, keyboard activation, repeated clicks, invalid drafts and recovery
after errors. These counts include parametrized variants.

Earlier checks on my installation confirmed the revised dialog layout and one
midnight edit to an existing test holiday. I performed no additional appliance
writes for this combined release verification; creation and deletion were not
repeated on the appliance. Broader installation and long-term recovery testing
remain open.

After updating through HACS, restart Home Assistant and reload the browser so
the revised dialog controls are loaded.

## [0.8.1-beta.2] - 2026-10-11

I added a **Season switching** control so the central heating season can be
changed directly in Home Assistant when the installation exposes it. A heating
circuit can remain in summer pause even with its operation mode set to Manual;
the new control exposes the separate central season setting.

- I use the options and write permission reported for
  `/system/seasonOptimizer/mode`. Known options have German and English labels:
  Heating/cooling off, Automatic, Heating only and Cooling only. Cooling is not
  offered on installations that do not advertise it. Options update during
  normal polling, and writes to unoffered options are rejected.
- I kept the control scoped to its gateway. Heating-circuit modes, hot-water
  settings and heating/cooling thresholds are not written by this control.
  Each change uses the existing single PUT and mandatory read-back.
- I translated the actual `heating` and `cooling` season states and named the
  existing status sensor **Current heating/cooling status**. It remains separate
  from the requested setting, with its existing identity and activation choice.
  Its default is still disabled; it can be enabled in the entity settings.
- I kept existing polling intervals. There are no additional background cloud
  requests. Diagnostics explain the new control's eligibility without exporting
  current settings or unknown option text.

My local records from 2026-10-11 show the underlying season setting changing
from `off` to `forcedHeat`, followed by actual season status `heating` and
heating-circuit status `heating_manual_on`. These records concern the existing
appliance setting. The new Home Assistant select still needs a physical test
after installation; cooling has not been tested on that installation.

After updating through HACS, restart Home Assistant and look for
**Season switching** (German: **Saisonumschaltung**) under the heating system.
Heating only enables
heating; demand and normal controller conditions still determine compressor
operation. See [Season switching](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.1-beta.2/docs/season-switching.md) for details.

I verified 1,125 automated test cases with 96.26% combined statement and branch
coverage, including 24 new season-control cases. Formatting, linting, strict
type checking and the runtime dependency audit passed. These counts include
parametrized variants and do not replace testing the new control on an installation.

This release is a pre-release. **0.8.0 remains Stable/Latest.**

## [0.8.1-beta.1] - 2026-10-11

I am releasing this beta to make PointT startup more resilient to temporary
failures and reduce unnecessary cloud reads. Functional discovery now runs
before reviewed supplementary metadata. If an informational request fails,
the integration can finish loading its usable resources and recover the
missing supplementary data later. **0.8.0 remains Stable/Latest.**

### More resilient startup and recovery

- I stopped requesting the unused `/gateway/thirdPartyLicenseInformation`
  document. A failure on that endpoint can no longer abort discovery. This
  does not establish the original cause of every connection failure in #49.
- I separated reviewed informational leaves from functional discovery, with
  one shared **30-second supplementary budget per integration entry**, after
  functional discovery for all selected gateways. Unsent work remains pending.
  The exact paths and startup order are documented in the
  [request guide](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.1-beta.1/docs/request-behavior.md).
- Temporary failures retry after **1, 2, 5, 15 and then 30 minutes** between
  failed attempts, using existing coordinator wake-ups. Further failures
  retain the 30-minute delay. Recovery uses at most one batch of 30 paths per
  entry per minute, after normal gateway work, without immediate fallback
  GETs or extra network retries. Healthy gateways take turns.
- Expected metadata sensors returning 403, 404 or 406 retry after 30 minutes.
  Unsupported diagnostic-only candidates do not repeat continuously. Cloud
  rate-limit pauses take priority, and invalid credentials still trigger
  reauthentication. The shared 512-path and depth-eight limits remain.
- Existing metadata sensors retain their identities and user settings while
  unavailable. They recover on a valid response without a restart. Newly
  recovered supported resources can add entities during normal operation.
  This does not replace rediscovery after adding a previously unknown circuit.

### Fewer unnecessary requests

- I changed valid read-only device product names and installed refrigerant-
  circuit counts from 15/30-minute polling to **one successful read per
  integration load**. "Once" means after success, not one attempt forever.
  An explicit reload or Home Assistant restart reads them again; automatic
  retries of unfinished setup retain the cache when configuration and
  credentials are unchanged. Ordinary token refresh does not clear it.
- Administrative supplementary values do not create entities. Diagnostic
  downloads and debug status logging use local state and add no cloud requests.
- In a local HTTP test with 30 fast paths and two metadata leaves, steady
  polling used **60 instead of 64 requests per hour** in the standard profile
  and **30 instead of 32** in the cloud-friendly profile. Startup and failure
  recovery are excluded from those figures. Savings depend on batch layout;
  fewer path reads do not always mean fewer separate HTTP requests.
- Operating-value intervals and write validation/read-back remain unchanged.
  Supplementary success never refreshes unrelated values or confirms that a
  heating fault has cleared.

### Diagnostics and maintainability

- Schema **14** distinguishes omitted, deferred, failed and recovered reads,
  actual read times, earliest retry times and logical versus physical requests.
  A narrow allowlist makes known technical metadata visible without exposing
  current measurements, settings, personal names, network identifiers or tokens.
  The structural inventory export also removes these technical values.
- I separated supplementary request handling from the Home Assistant
  coordinator. Queue/cache changes use defined methods and immutable status
  views. Tests cover recovery without restart, cancellation, authentication,
  rate limits, wrong-resource replies and stable entity identity.

### Included maintenance updates

- **PR #55:** Ruff pre-commit hooks 0.16.10.
- **PR #56:** Hypothesis 6.168.5, Mypy 2.4.0, the Home Assistant test package
  0.13.370 and Ruff 0.16.10. I retained **Pytest 9.0.3** because the Home
  Assistant test package requires it. The proposed 9.1.1 pin makes the
  dependency set impossible to install and is not part of this beta.
- **PR #57:** Pinned CodeQL initialization and analysis actions 4.38.3.
- I aligned form schemas with the validation backend used by the installed
  Home Assistant, including its 2026.10 transition to Probatio. Required fields
  and suggested holiday values use matching marker types. This compatibility
  choice is isolated from the PointT client and adds no cloud requests.
- This beta includes the existing 0.8.0 fault notifications, entity-preserving
  cleanup, sign-in recovery and icon changes. Those are inherited from the
  stable release; the new runtime focus is startup recovery and request use.

### Validation and beta testing

I ran **1,101 automated test cases** with the pinned development environment
and again with released Home Assistant **2026.10.0**. Both passed with
**96.23% combined statement and branch coverage**. Another 167 targeted form,
notification-options and holiday-write checks passed on HA **2026.9.4**.
Formatting, linting, strict typing and the isolated dependency audit passed;
the audit found no known vulnerabilities.

Local verification and the publication checklist are recorded in
[the release report](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/blob/v0.8.1-beta.1/docs/release.md).
The new request policy still needs field confirmation. Automated tests and a
local HTTP server do not establish long-term stability on a physical system.

Enable beta versions in HACS, select **0.8.1-beta.1** and
restart Home Assistant. Check the expected entities, including disabled ones,
and observe whether missing supplementary values recover on their own after
a temporary failure. Heating does not need to be enabled for these read checks.
Do not deliberately interrupt the appliance or change heating settings just
to test recovery. If something remains missing, provide a fresh diagnostic
export and the time of the problem after reviewing it for personal information.

## [0.8.0] - 2026-10-10

I am releasing 0.8.0 with heating fault notifications, a refreshed icon and
fixes for disappearing solar entities and reauthentication. This stable
release brings together the changes from beta.1 through beta.3. Apart from
the version number, the integration code and packaged assets match beta.3.

Thank you to ph15ch and lanebe for reporting the problems in #48 and #49
and providing the details that helped me investigate them.

### Heating fault notifications

- I added persistent notifications to the Home Assistant bell. Notifications
  are enabled by default and can be configured for each installation,
  including while the cloud connection is unavailable.
- Messages include fault codes, known descriptions and the times when Home
  Assistant first observed the fault and confirmed its resolution. Active
  and resolved incidents appear separately, with up to 25 resolved incidents
  retained in the visible message.
- Dismissed incidents are remembered across restarts. New incidents,
  confirmed recurrences and increased severity can show the message again.
  Failed readings and changes in fault classification do not imply resolution.
- I improved recovery after interrupted setup or storage failures and
  prevented duplicate notification observers during overlapping starts.
  Notifications use the existing fault data and add no cloud requests.
- Retained message details are cleared on dismissal, disabling notifications,
  unloading or restart. They are not a permanent fault history. Dismissing
  a message does not acknowledge or reset a fault on the appliance, and bell
  notifications do not automatically send mobile push notifications.

### Solar entities and sign-in

- **Solar entities (#48):** I fixed startup cleanup deleting supported
  entities below an unrecognized directory. The same protection covers
  supported descendants in other subsystems. Existing entity IDs, custom
  names and choices to disable entities are preserved across reloads.
  Solar entities remain attached to the gateway device.
- **Reauthentication (#49):** I added a clear explanation and a fresh sign-in
  link when an attempt exceeds its ten-minute limit or must be restarted
  after an interrupted token exchange. Reauthentication updates the existing
  integration entry.
- **SingleKey ID request limits:** A waiting step respects the required
  pause. Submitting again during the pause does not send another token
  request, and a fresh sign-in link is provided afterward.
- **Refresh tokens:** I prevent explicitly empty replacement tokens from
  overwriting saved credentials. If the server omits a replacement token,
  the existing token is retained.

### Appearance and maintenance

- I added a heat pump icon with three blue-to-red thermal waves, with light
  and dark variants at standard and double resolution. Home Assistant loads
  the matching icon from the integration package.
- I updated Hypothesis to 6.168.3, the Home Assistant test package to 0.13.367,
  Ruff and its pre-commit hook to 0.16.9, the pinned CodeQL actions to 4.38.2
  and the hassfest action. Pytest remains at 9.0.3 to satisfy the Home
  Assistant test package's compatibility requirement.

### Validation and field status

- I ran 1,003 automated test cases on Home Assistant 2026.9.4 and 2026.10.0.
  Both runs passed with 95.95% combined statement and branch coverage.
  Formatting, linting, strict typing and the dependency audit also passed;
  the audit found no known vulnerabilities.
- I have tested the notification feature extensively on my own installation
  and closed #36 as implemented and tested locally. Beta.3 also appeared
  to work in my initial local check.
- A 48-hour connection test and the specific solar restart checks remain
  unconfirmed. The original cause of the connection loss in #49 has not
  been established. I am keeping #48 and #49 open for the remaining field
  feedback.

### Upgrade

Install **0.8.0** through HACS and restart Home Assistant. This is a stable
release, so selecting beta versions is no longer necessary.

When updating from 0.7.2, an existing active heating fault may immediately
appear in the Home Assistant bell because notifications start enabled.
Under **Settings > Devices & services > Bosch/Buderus Heating > Configure**,
choose **Notifications** to select the installations to notify for.
Holiday configuration has a separate entry in the same menu.

For installations with solar entities, check that the expected entities
remain after restart and reload and that their values update. If a new
sign-in is requested, use the latest link in the dialog and observe the
connection afterward. A fresh diagnostic export and the time of any
failure will help with remaining problems. Review diagnostics for personal
information before sharing and do not include sign-in links or tokens.

## [0.8.0-beta.3] - 2026-10-09

I prepared this beta to address disappearing solar entities and sign-in attempts
that could get stuck during reauthentication. It also includes the maintenance
updates from PRs #45, #46 and #47, along with the fault notifications and icon
variants from the earlier 0.8.0 betas. Version 0.7.2 remains Stable/Latest.

Thank you to ph15ch and lanebe for reporting the problems in #48 and #49 and
providing the details that helped me investigate them.

### Fixed

- **Solar entities disappearing after discovery (#48, PR #50):** I fixed startup
  cleanup removing supported entities below an unrecognized directory. Solar
  entities now survive this cleanup, including after restart or reload. The
  same protection covers supported descendants in other subsystems. Existing
  entity IDs, custom names and choices to disable entities are preserved.
  Solar entities remain attached to the existing gateway device.
- **Expired sign-in attempts (#49, PR #51):** I added a clear explanation and a
  fresh sign-in link when an attempt exceeds its ten-minute limit or must be
  restarted after an interrupted token exchange. Reauthentication continues
  to update the existing integration entry.
- **SingleKey ID request limits:** I added a waiting step that respects the
  required pause. Submitting again during the pause does not send another
  token request. A fresh sign-in link is provided after the wait.
- **Refresh-token validation:** I prevent explicitly empty replacement refresh
  tokens from overwriting saved credentials. If the server omits a replacement
  token, the existing token is retained.

### Maintenance

- I updated Hypothesis to 6.168.3, the Home Assistant test package to 0.13.367
  and Ruff to 0.16.9. I kept pytest at 9.0.3 because the Home Assistant test
  package requires that version; the originally proposed 9.1.1 update could
  not be installed with it.
- I aligned the Ruff pre-commit hook with 0.16.9 and updated the pinned
  hassfest action used by CI and releases. These are development and
  validation updates, with no new heating controls.

### Validation

- I ran 1,003 automated test cases on Home Assistant 2026.9.4 and 2026.10.0.
  Both runs passed with 95.95% combined statement and branch coverage. The
  cases include discovery, entity cleanup, restart and reload
  behavior, saved entity preferences, expired sign-in attempts, token
  responses and request-limit waiting.
- I checked formatting, linting and strict typing, and the dependency audit
  found no known vulnerabilities. The release workflow repeats these checks
  and validates HACS compatibility and hassfest before publication.
- These automated checks do not confirm the original cause of the connection
  loss in #49 or prove that a connection will remain stable for 48 hours.
  Field validation of both fixes is still pending.

### Upgrade and local testing

Enable beta versions for this integration in HACS, select **0.8.0-beta.3**,
install the update and restart Home Assistant.

1. **Solar entities (#48), if present on the installation:** Check the entities
   assigned to the gateway, including disabled entities. Confirm that the
   expected solar entities remain after a Home Assistant restart and after
   reloading the integration, and that available values continue to update.
2. **Reauthentication (#49):** If Home Assistant requests a new sign-in, complete
   it using the latest link shown in the dialog. Confirm that the existing
   devices and entities remain, then observe the connection for at least
   48 hours. There is no need to delete and recreate the integration to test it.
3. **Earlier beta features:** When a fault occurs and clears during normal
   operation, check that its code, known description and observation and
   resolution times remain visible in the notification. Retained details
   are cleared on dismissal, disabling notifications, unloading or restart.

If a problem remains, record when it happened and provide a fresh diagnostic
export after the update, reviewed for personal information before sharing.
Do not include sign-in links or tokens.

I am keeping #48 and #49 open until the fixes have been confirmed on the
affected installations. I will assess the local results and any remaining
beta issues before publishing 0.8.0 as stable.

## [0.8.0-beta.2] - 2026-10-03

This beta keeps fault details visible after a fault has cleared and improves
notification recovery after interrupted setup or storage failures. It includes
the bell notifications and new icon from beta.1. Version 0.7.2 remains stable.

### Fixed

- Keep fault codes, subcodes and known descriptions in visible notifications
  after confirmed resolution. Active and resolved faults appear separately
  when several faults end at different times.
- Show the first Home Assistant observation and confirmation of resolution in
  the configured time zone, including the applicable UTC offset. Repeated
  incidents of the same code retain separate observation times.
- Distinguish a fault reclassified as a warning, maintenance or information
  from a confirmed resolution. Changed classification alone is not an all-clear.
- Retain the latest 25 resolved incidents within the visible message, explaining
  any older omissions. Details are cleared on dismissal, disabling notifications,
  unloading or restart. No permanent fault history or additional cloud requests
  are introduced; existing event delivery and dismissal persistence are preserved.
- Keep notification changes and resolution events working if a storage write
  cannot be queued; retry queueing on a normal update. A failed baseline save
  during unload no longer skips the separate dismissal save.
- Clean up already started notification observers when setup is cancelled, and
  prevent a stopped or concurrently started manager from registering late or
  duplicate observers. An unreadable baseline allows fresh fault detection.

I tested beta.1 on my installation: notifications appeared when faults occurred
and changed to resolved when they cleared. I still need to check the retained
fault details in Home Assistant after installing this fix.

### Validation

- 945 automated test cases passed, including 44 added cases, with 95.77%
  combined statement and branch coverage. Ruff formatting and linting and
  strict Mypy passed.
- Cases cover retained details, partial resolution, recurrence, reclassification,
  failed reads, dismissal, bounded retention, restart, event delivery and the
  repeated hour during a daylight-saving clock change.
- Restart and failure tests include four real SIGKILL boundaries in separate
  Linux processes using Home Assistant storage, corrupt JSON, a failed disk
  replacement, eight persisted-state restart scenarios, normal final-write
  shutdown, repeated reloads and cancelled or overlapping starts.
- An abrupt termination before persistence can lose a recent dismissal or first
  observation time. Recovery uses the last completed files and does not infer
  resolution from a failure. Physical power loss is not simulated.

### Upgrade and beta testing

Enable beta versions for this integration in HACS, select 0.8.0-beta.2, install
the update and restart Home Assistant. When a fault occurs and clears during
normal operation, check that its code, known description and observation and
resolution times remain visible. With multiple faults, active and resolved
incidents should appear separately.

The message retains up to 25 resolved incidents. These details are cleared on
dismissal, disabling notifications, unloading or restart; this is not a permanent
fault history. Dismissing the message does not acknowledge or reset the fault
on the appliance. Issue #36 remains open until the remaining checks are complete.

## [0.8.0-beta.1] - 2026-10-03

This beta adds heating fault notifications to the Home Assistant bell and a
new integration icon for light and dark backgrounds. It also includes the
reviewed maintenance updates from PRs #37, #38 and #39. Version 0.7.2 remains
the stable release.

Thank you for suggesting the notification feature and helping test the
integration on real installations.

### Added

- Show persistent Home Assistant notifications for heating system faults, enabled
  by default and configurable for each installation. Notifications share the
  existing fault state without additional cloud requests or appliance writes.
- Remember dismissed incidents across restarts. New incidents, confirmed
  recurrences and increased severity can show the notification again. Visible
  messages update after confirmed resolution; uncertain states never report
  an all-clear.
- Separate local notification settings from holiday configuration. Notification
  options remain accessible while offline and preserve existing settings.

### Appearance

- Use the new heat pump icon with three blue-to-red thermal waves.
- Include matching light and dark variants at standard and double resolution.
  Home Assistant loads the icons from the integration package. The README
  selects the matching SVG for the reader's color scheme.

### Maintenance

- Update Ruff and its pre-commit hook to 0.16.8, Hypothesis to 6.168.1, and the
  Home Assistant test package to 0.13.366.
- Retain pytest 9.0.3 to satisfy the Home Assistant test package's exact
  dependency requirement.
- Update both pinned CodeQL actions to 4.38.2.

### Compatibility

- Existing fault sensors, events, entity identities and polling intervals remain
  unchanged. Bell notifications and dismissals are shared by Home Assistant
  users. Notification text follows the system language; options follow the
  profile language.
- This feature still requires testing in the actual Home Assistant frontend.
  Automated tests cover dismissal, restart, multiple installations, partial
  responses, recurrence, severity changes and option updates.

### Validation

- 901 automated test cases passed, including 46 additional cases, with 95.80%
  combined statement and branch coverage. Formatting, Ruff and strict Mypy
  passed with the combined feature and maintenance changes.
- Package construction and isolated imports passed. The isolated runtime
  dependency audit found no known vulnerabilities.

### Upgrade and beta testing

Select version 0.8.0-beta.1 in HACS with beta versions enabled, install it and
restart Home Assistant. Under Settings > Devices & services > Bosch/Buderus
Heating > Configure, choose Notifications to select the installations to
notify for. Holiday periods has its own entry in the same menu. An existing
active fault can appear immediately because notifications start enabled.

Check the icon in light and dark themes, the configuration menu, and any
notifications arising during normal operation. Dismissing a Home Assistant
message does not acknowledge or reset the fault on the appliance. Bell
notifications do not automatically send mobile push notifications.

I have not performed a physical appliance write or deliberately triggered a
fault for this release. Actual Home Assistant and HACS frontend confirmation
remains outstanding, and issue #36 stays open for that feedback. The holiday
input corrections inherited from 0.7.2 also still need field confirmation.

## [0.7.2] - 2026-09-20

This update corrects two holiday-dialog problems reported after 0.7.1: an
invalid time such as 22:38 showed an unrelated settings error, and the new
holiday option could remain English in a German profile.

Thank you for testing again and providing the screenshots. They helped me
identify both remaining problems.

### Fixes

- Show the quarter-hour requirement at the affected start or end field. Minutes
  must be 00, 15, 30, or 45, with seconds 00. The form also explains this before
  submission. Times are never rounded automatically.
- Preserve the name and dates while correcting an input. The subsequent settings
  step displays the entered holiday name.
- Report the same translated time error when creating or updating a holiday
  through the calendar, before sending a write.
- Translate the new holiday option through Home Assistant's selector translations,
  using the profile language independently of the server language. German and
  English are provided, with Home Assistant's English fallback for other languages.
- When several gateways can create holidays, ask which installation to use.
  A gateway that disappears during selection is not silently replaced by another.
- Update the opening instructions to include creation through this dialog.

### Validation

- 855 automated test cases passed, including 31 added cases and parametrized
  variants, with 95.70% combined statement and branch coverage.
- Tests cover the reported 22:38 input, invalid seconds and fractional seconds,
  both endpoints, correction followed by a simulated write, profile
  translations with a different server language, and multiple gateway selection.
- Accepted quarter-hour times are checked in European, American, and Nepalese
  time zones, including UTC calendar inputs.
- Ruff formatting and linting, strict Mypy, and the isolated dependency audit
  passed. No known dependency vulnerabilities were found.

### Upgrade

Update through HACS and restart Home Assistant. Version 0.7.2 gives installations
already running 0.7.1 a distinct update; the published 0.7.1 files are unchanged.
Existing entity IDs, diagnostics schema 13, supported operating modes, and write
confirmation checks are unchanged. No additional cloud requests are introduced.
I have not performed a physical holiday write for this release. Confirmation in
the user's browser and on the installation remains outstanding.

## [0.7.1] - 2026-09-20

This update fixes holiday creation when Home Assistant combines its automatic
date value with a second time, producing an `Invalid datetime specified` error.
It also checks holiday times around clock changes and includes the reviewed
maintenance updates from PRs #31, #32, and #33.

Thank you for the screenshots, version details, and time spent checking the
holiday dialog. They helped me reproduce the exact input failure.

### Fixes and behavior changes

- Start and end now begin empty in the holiday creation dialog. Selecting both
  dates avoids malformed date and datetime defaults in affected Home Assistant
  frontends. Both fields remain required.
- Preserve the holiday name and dates after a validation error. Datetime
  suggestions use the format expected by the selector, including after an
  offset-aware input is converted to the heating system's time zone.
- Remove the redundant holiday label from the creation instructions so a global
  Home Assistant language setting cannot insert English text into that otherwise
  German description.
- Reject start or end times that do not exist or occur twice during a clock
  change. The options dialog and calendar report a translated error before a
  write. Periods may still span a clock change with unambiguous endpoints.

### Maintenance

- Update Ruff and its pre-commit hook to 0.16.6, Hypothesis to 6.168.0, and the
  Home Assistant test package to 0.13.364.
- Keep pytest at 9.0.3, as required by that Home Assistant test package. The
  proposed pytest 9.1.1 update prevented the test dependencies from installing.
- Update the pinned hassfest action and CodeQL action to their reviewed versions.
  All three maintenance PRs passed the required checks before merging.

### Safety and compatibility

- Device identifiers, entity unique IDs, existing holiday identifiers, discovery,
  polling intervals, and diagnostics schema 13 are unchanged.
- Supported modes and limits still come from the installation. The fixes add no
  cloud requests. Writes retain their existing confirmation checks and are not
  blindly repeated.
- Date-only gateways retain their date selectors and inclusive PointT end-date
  conversion. Datetime gateways retain 15-minute steps and midnight handling.
- PointT holiday timestamps have no UTC offset or fold marker. A repeated local
  time is therefore rejected even when the calendar supplies an explicit offset.

### Validation

- 824 automated test cases passed with 95.69% combined statement and branch
  coverage. The count includes 34 added cases and separately parametrized variants.
- Ruff formatting and linting, strict Mypy, and an isolated dependency audit
  passed. No known dependency vulnerabilities were found.
- The new regression cases reproduced the faulty initial fields and acceptance
  of invalid local clock times before the fixes.
- Verification covers serialized Home Assistant form schemas, required fields,
  date-only and datetime creation, preservation of corrected inputs, and rejected
  options/calendar operations without a write.
- Isolated execution of the original Frontend 20260826.7 functions covered ten
  language regions, five date-format settings, four time-format settings, six
  time zones, and six hours. All 7,200 combinations preserved the machine-readable
  date and time. This is a function-level check, not a browser or physical-device
  test.
- Clock-change cases include Europe, North America, and Lord Howe's half-hour
  changes, alongside fixed-offset and distant time zones.

### Upgrade notes and limitations

- Update through HACS and restart Home Assistant. No removal or reconfiguration
  of the integration is needed. Select both dates when creating a holiday.
- Review the selected modes before confirming the second step. Holiday times
  still need to follow the installation's 15-minute steps.
- If an endpoint falls into a skipped or repeated clock-change interval, choose
  another time. The integration does not silently move it or choose an occurrence.
- I have not performed a real holiday write on an installation for this release.
  Confirmation of the corrected dialog in the user's browser remains outstanding.

## [0.7.0] - 2026-09-10

With 0.7.0, I am releasing the improvements from the 0.7 beta series as a stable
update. This release adds a calculated dew point, improves discovery and controls
for installations with multiple heating circuits, and fixes missed polling
intervals and misleading fault-parser diagnostics.

Thank you to everyone who shared diagnostics, repeated tests, and detailed
feedback. I especially appreciate the patience while I worked through the
missing controls on the second heating circuit.

### New since 0.6.0

- Calculate a dew point locally for each heating circuit that provides compatible
  room-temperature and humidity measurements. Source readings and the Magnus
  calculation parameters are available as attributes.
- Adapt supported controls to the options and numeric limits advertised by each
  installation and circuit. Newly eligible controls for already discovered
  resources can appear during polling, with existing entity IDs retained.
- Improve discovery of additional heating circuits by processing advertised
  references first, known core control paths next, and other optional paths last.
  Unreadable parent directories no longer prevent checks of known circuit paths.
- Add explicit holiday creation with the modes, date formats, and temperature
  limits advertised by the installation. Holiday edits check for conflicting
  changes made in another app.
- Expand diagnostics through schema 13 with discovery outcomes per circuit,
  reasons for control eligibility, and local resource pauses. Current values,
  credentials, and private identifiers remain excluded.
- Recognize additional app-defined operating states and schedule types, with
  German and English translations.

### Fixes

- Avoid skipping a polling group when Home Assistant's rounded timer calls the
  coordinator just before its exact deadline. A maximum one-second tolerance
  applies only to group scheduling. This fixes the observed gaps of about two
  minutes between reads intended to run every minute.
- Preserve the last actual fault-parser result during unrelated polling. A
  temperature read no longer replaces `empty`, `ok`, or `partial` with `not_run`.
  Read failures remain visible separately and cannot confirm an all-clear.
- Recover previously discovered resources after a temporary 404 during their
  next regular group poll, preserving their entity identity.
- Retain active faults through incomplete responses, lost sources, and restarts;
  resolution still requires consecutive valid confirmations.
- Preserve unavailable and stale states after interrupted batches and resume
  deferred reads without starving later groups.
- Verify scalar and holiday writes by reading back their results, including
  ambiguous connection failures, without blindly repeating a mutation.
- Improve account-wide rate-limit handling, concurrent token refreshes,
  reauthentication checks, and energy-counter interpretation.

### Behavior changes

- Live values return to their intended cadence. Request totals may therefore
  increase compared with a run that was skipping scheduled reads. Existing bulk
  limits and the standard and cloud-friendly polling profiles remain in place.
- Unsupported optional fault paths are still checked again after their 24-hour
  capability pause. Historical failure lists are checked at startup only.
- `parser_status` now consistently describes the last actual parsing attempt.
  It is not a current connectivity indicator. Diagnostic fields and schema 13
  are unchanged from Beta.7.
- Calculated environmental energy uses the `total` state class; direct cumulative
  energy counters retain `total_increasing`.

### Safety and compatibility

- Existing device identifiers, entity unique IDs, and user registry settings are
  retained. Control options, limits, and write permissions still come from the
  corresponding resource.
- Rate-limit backoff, gateway circuit-breaker deadlines, and resource pauses
  keep their full duration. Timer tolerance never shortens these safeguards.
- The calculated dew point and diagnostic export add no cloud requests or writes.

### Validation

- 790 automated test cases passed with 95.67% combined statement and branch
  coverage. This count includes separately parametrized cases.
- 21 added cases cover Home Assistant's actual timer scheduling, the one-second
  boundary, both polling profiles, unchanged backoff deadlines, delayed cycles,
  daily fault-source probing and recovery, and preservation of parser results.
- The new regressions reproduced both faults on Beta.7 before the corrections.
  The scheduler used by the test environment also matches Home Assistant 2026.9.1.
- Ruff formatting and linting, strict Mypy, and the isolated dependency audit
  passed. No known dependency vulnerabilities were found.
- Release-package tests verify deterministic archives, a component-root manifest,
  matching version metadata, and import from an isolated installation. Publication
  also requires the protected-branch checks and the release workflow validation.

### Upgrade notes and limitations

- Restart Home Assistant after updating so discovery runs with the new version.
  There is no need to enable beta releases in HACS or remove the integration.
  Check disabled entities if a supported installer control is not visible.
- The reporter in issue #23 confirmed that all expected controls, including HC2,
  appeared and that changed values were reflected in the official Buderus app.
  The report does not list every tested setting or confirm that all original
  values were restored.
- The timing and parser corrections have automated regression coverage. I have
  not yet completed a physical-installation observation period for these two
  changes. Additional Bosch systems, gateway variants, and extended operation
  still need broader field testing.
- The calculated dew point is not the controller's internal cooling limit and
  does not include an unknown installer safety offset. Holiday reads and writes
  remain separate cloud operations rather than an atomic transaction.

## [0.7.0-beta.7] - 2026-09-07

This beta combines improved heating-circuit discovery with recovery after a
temporary 404 response. Install the update and restart Home Assistant once so
discovery can find additional controls. Availability still depends on the
resources and write permissions supplied by the installation.

### Fixed

- Try the known operating-mode and temperature-setting paths for each advertised
  heating circuit, even when its circuit or temperature-level directory returns
  403, 404, or 406. Process reported references first, these core paths second,
  and other optional probes last. Keep the existing batch, depth, and path bounds
  and avoid duplicate reads or guessed circuit numbers.
- Retry previously discovered resources after a 404 at their next regular group
  poll instead of pausing them for 24 hours. Mark their values unavailable until
  a valid response restores the same entities without a restart. Preserve
  polling intervals, global rate-limit backoff, and other resource pauses.
- Keep unsuccessful optional control probes out of recurring polling. Controls
  continue to use their own circuit's options, numeric limits, and write rights,
  with one PUT followed by read-back verification.

### Diagnostics

- Schema 13 identifies core catalog probes and counts catalog paths and
  successful discoveries per circuit. Show local polling-pause state and remaining
  seconds per resource without additional requests or raw current values.

### Validation

- 769 automated test cases passed with 95.65% combined statement and branch
  coverage. This includes synthetic regressions for unreadable parent
  directories, path limits, source promotion, and missing circuits.
- Combined lifecycle tests cover discovery, creation of HC2 controls, repeated
  404 responses, recovery at the next group poll, stable entity identity, and
  one verified write to the correct circuit.
- The affected installation still needs a practical test. No physical HC2
  write was performed during development.

### Testing this beta

Thank you for taking the time to test this again. I would appreciate one
combined check after installing Beta.7 and restarting Home Assistant:

1. Check the HC2 controls, including disabled entities and the configuration
   section, and compare the available options with MyBuderus.
2. If a familiar, suitable setting is available, note its original value,
   change it once, check that MyBuderus shows the change, and restore the
   original value. Please include whether both changes were confirmed.
3. Download fresh diagnostics from Settings, Devices & services, Bosch/Buderus
   Heating, the three-dot menu, Download diagnostics. Review the JSON for
   personal information before attaching it to issue #23, and mention any
   controls that MyBuderus offers but Home Assistant still lacks.

Heating does not need to be switched on for discovery. There is no need to
provoke an error: if a 404 happens naturally, the integration now retries the
known resource during its normal polling cycle.

## [0.7.0-beta.6] - 2026-09-06

This beta adapts supported controls to each installation's advertised options
and limits, and fixes additional discovery, polling, and write-confirmation
failures. Install the beta and restart Home Assistant once to load the updated
integration. Newly eligible discovered controls then appear during polling.

### Changed

- Derive each supported scalar control's options and bounds from its own current
  resource metadata. Accept enum subsets and new vendor codes independently
  for every gateway and circuit; preserve exact wire spelling through translated
  options, including colliding aliases. Reference-device numeric ranges and
  undocumented step restrictions no longer reject valid targets.
- Add newly eligible discovered controls during polling while preserving entity
  IDs and user registry settings. Keep legacy switches and offer variant enum
  sets through a separate select. Missing values remain unknown while valid
  replacement values remain writable.
- Diagnose capabilities and current-state issues separately with schema 12;
  unknown option texts remain redacted. Numeric steps are identified as UI hints.
- Allow explicit holiday creation through Configure with per-device modes,
  date/date-time selectors, and advertised temperature limits.

- Include the Ruff hook update from PR #25 and the compatible test-tool updates
  from PR #26. Keep pytest 9.0.3 as required by the Home Assistant test plugin.

### Fixed

- Prevent hidden HTTP connection retries and redirects from repeating PointT
  writes. Preserve Home Assistant's shared session and middleware; read retries
  remain bounded and every HTTP attempt is counted in request diagnostics.
- Mark resources skipped by an interrupted polling cycle unavailable and stale,
  resume them before earlier batches, and advance independently completed polling
  groups. Deferred fault reads cannot preserve an unverified all-clear.
- Remove dynamic device and circuit identifiers from diagnostic resource names
  as well as resource paths.
- Confirm scalar and holiday writes after an ambiguous connection or service
  failure using bounded read-back attempts without repeating the mutation.
- Handle invalid cloud resource paths as protocol errors, allowing resource
  fallback while retaining healthy items from the same bulk response.
- Update numeric control bounds from current validated metadata during polling.
- Reject conflicting resource IDs in direct and bulk responses, preventing a
  different heating circuit from falsely confirming a write.
- Preserve the original holiday-form baseline across polling updates and check
  holiday modes against the current configuration before sending mutations.
- Reject unadvertised holiday defaults; support explicit selections instead.
- Use config-entry-scoped device lookups for Home Assistant compatibility and
  prevent preview-device migration from merging devices into another entry.

### Validation

- 751 automated tests pass with 95.64% coverage, including branch coverage.
- Regression cases cover disconnected HTTP writes, measured read retries,
  redirects, deferred polling and faults, normal polling cadence after local
  path errors, diagnostic names, malformed references, and changing bounds.
- Installation-variant regressions cover independent gateways/circuits, new
  enum codes and reversible aliases, live control creation, switch compatibility,
  missing current values, numeric units and bounds, conflicting response IDs,
  concurrent holiday edits, and explicit date-only/date-time holiday creation.
- Ruff formatting and linting, strict Mypy, and release-package tests pass.
- Validation used synthetic data and local HTTP servers; real-system testing
  remains outstanding.

## [0.7.0-beta.5] - 2026-09-06

This beta restores heating-circuit operation-mode controls when an installation
offers fewer than all three known modes. Diagnostics now explain why a discovered
resource does or does not qualify for a control, helping investigate remaining
missing settings on installations with multiple heating circuits.

### Fixed

- Create heating-circuit operation-mode controls when PointT advertises a
  supported subset such as `manual` and `auto`. Each circuit keeps its own
  options; newly advertised known modes appear during normal polling and
  withdrawn modes disappear. Unknown or unadvertised modes cannot be written.
- Reject non-finite numeric bounds and resources without a current scalar value
  consistently when deciding whether to create numeric controls.

### Diagnostics

- Schema 11 explains scalar-control eligibility per resource using the same
  checks as entity creation. It reports specific rejection reasons, known
  advertised options, numeric capability bounds, policy limits and steps, and
  default activation, including disabled installer controls.
- Current measurements and settings, unknown option text, credentials, and
  device identifiers remain excluded. Downloading diagnostics adds no requests.

### Safety and compatibility

- Only known operation modes currently advertised by the individual heating
  circuit are offered. Write permissions, resource types, and the single-PUT
  transaction with mandatory read-back remain enforced.
- Other enum controls retain their existing option requirements. Numeric
  safety limits and installer-control defaults are unchanged.
- Diagnostics include capability metadata and fixed rejection codes, not current
  settings or unknown option text. Existing entity and device IDs are preserved.

### Validation

- 687 automated tests pass with 95.59% coverage, including branch coverage.
- Regression tests cover independent hc1/hc2 options, metadata changes,
  confirmed subset writes, rejection of unadvertised or unknown values, and
  diagnostic agreement with entity creation without leaking private values.
- Ruff formatting and linting and strict Mypy validation pass.
- The isolated dependency audit found no known vulnerabilities. The local
  release archive, manifest version, and SHA-256 checksum were verified.

### Upgrade notes and limitations

- Restart Home Assistant after updating so previously excluded controls can be
  created. Existing entity and device identifiers and polling intervals remain
  unchanged; write permissions and PUT/read-back validation still apply.
- The operation-mode correction covers the reproduced metadata mismatch. The
  remaining controls on the installation in issue #23 still need a field test;
  the additional diagnostics explain other capability rejections.

## [0.7.0-beta.4] - 2026-09-06

This beta improves discovery on installations with multiple circuits and makes
missing resources easier to investigate. Advertised resources are processed
before optional probes, and diagnostics show discovery outcomes for each circuit.

### Fixed

- Prioritize resources explicitly referenced by PointT ahead of optional app-path
  probes, so fallback traffic from one subsystem cannot prevent later heating
  circuits or other advertised resources from being discovered.
- Remove the global 30-item discovery fallback budget. Every recoverable item
  within the existing bounded discovery graph can receive one individual read;
  rate limits still stop discovery immediately.
- Process references fully before optional probes, in individual batches of at
  most 30 paths. A bad batch no longer replaces successful or unsent batches
  with individual reads. Request-wide service and connection failures stop the
  run without continuing through the remaining fallbacks.

### Diagnostics

- Report discovery scheduling, results, fallback outcomes, completion state,
  and per-subsystem counters. Canonical logical paths such as
  `/heatingCircuits/hc2/operationMode` remain visible, while device identifiers
  and unrecognized dynamic IDs remain redacted.
- Distinguish logical discovery calls from HTTP retries, include failed paths
  per circuit, and report authentication, rate-limit, transport, depth, and
  resource-limit aborts accurately.

### Validation

- 647 automated tests pass with 95.47% coverage, including branch coverage.
- Regression tests cover second-circuit discovery under a tight path budget,
  malformed middle batches, local resource failures, and request-wide aborts.
- Ruff formatting and linting and strict Mypy validation pass.

### Upgrade notes and limitations

- Restart Home Assistant after updating to run discovery again. Existing entity
  and device identifiers are preserved.
- Discovery may use additional partly filled batches to finish advertised
  references before optional probes. Runtime polling intervals are unchanged.
- The reported missing-second-circuit case still needs confirmation on the
  affected physical installation; this beta includes synthetic regression
  coverage and the diagnostics needed for that field check.

## [0.7.0-beta.3] - 2026-09-05

This beta improves fault reporting, calendar writes, cloud error recovery, and
energy statistics. It prevents incomplete or stale cloud responses from being
treated as confirmed state and protects holiday edits against changes made in
another app.

### Fixed

- Keep active faults when a previously readable source becomes unavailable or
  returns a missing or malformed list; only explicit healthy responses can
  confirm resolution.
- Mark unrecovered resources stale after a failed bulk request while preserving
  their last successful values.
- Preserve account request-counter registry entries and user customizations when
  cleaning up deselected gateways.
- Require a complete, valid holiday list before confirming calendar mutations,
  including deletions.
- Read back apparently unchanged controls before skipping a write, and recheck
  live write permissions when another app has changed the value.
- Extract individual energy counters from typed PointT energy items.
- Stop discovery and polling fallbacks on bulk-item rate limits, and share the
  cloud backoff across all gateway reads, writes, chunks, and queued requests
  belonging to one account client.
- Reject reauthentication with an account that has none of the configured
  gateways, preserving the existing credentials.
- Redact device identifiers in unavailable-resource debug messages.
- Retain privacy-safe fault-source evidence across restarts, require consecutive
  successful confirmations, and avoid reporting health from invalid fault data.
- Retain stale snapshots after fully failed polls so capability pauses cannot
  restore an unconfirmed value as fresh.
- Read the live holiday list before creation and editing. Merge only changed
  fields, preserve external changes, and reject conflicting concurrent edits.
- Retain the account rate-limit deadline across failed setup attempts and
  coalesce concurrent authentication refreshes for a rejected access token.
- Treat calculated environmental energy as a net total so asynchronous input
  updates do not trigger false meter resets in Home Assistant statistics.
- Enforce the response-size limit while reading decompressed response chunks.

### Behavior changes

- Calendar creation and editing read the current holiday list before writing.
  Apparently unchanged controls are also read back before a write is skipped.
  These checks add requests when performing the affected user actions.
- An account rate limit pauses requests across its gateways and remains in
  effect when Home Assistant retries a failed integration setup.
- Calculated environmental energy uses the `total` state class; raw cumulative
  energy counters retain `total_increasing`.

### Validation

- 604 automated tests pass with 95.36% coverage, including branch coverage.
- Regression tests cover incomplete fault data, restarts, concurrent calendar
  changes, failed polling, rate limits, concurrent token refreshes, and oversized
  responses.
- Ruff formatting and linting and strict Mypy validation pass.

### Upgrade notes and limitations

- Restart Home Assistant after updating. Existing entity and device identifiers
  are retained.
- Previously stored active faults without source evidence are retained until
  they are observed again, preventing an unverified fault clearance on upgrade.
- Calendar reads and writes are separate cloud requests. A change made by
  another app between those requests cannot be excluded atomically.
- These fixes have automated regression coverage; validation against a real
  heating installation remains necessary for this beta.

## [0.7.0-beta.2] - 2026-09-04

This beta closes enum gaps found while investigating an upstream report about
the `absolute` heating-circuit schedule type. A complete comparison of the
corresponding models in MyBuderus and HomeCom Easy found several additional
PointT values that are now handled and translated explicitly.

### Fixed

- Added the app-confirmed `absolute` and `levels` values to
  `/heatingCircuits/{hc}/switchProgramMode`.
- Added exact app mappings for heating and cooling control types, hot water
  following the heating schedule, heat-pump and heat-source variants, system
  layouts, energy-management activity, and the PV contact state.
- Accepted the enum-name spellings used by the app model for the gateway data
  processing states while retaining the already observed PointT spellings.
- Added English and German state translations for every newly recognized
  canonical value.

### Behavior changes

- Schedule-type sensors now recognize all app-defined modes: `level`, `levels`,
  and `absolute`, even when the gateway does not advertise a complete enum
  list.
- PointT's legacy or product-specific spellings are normalized at the Home
  Assistant boundary without changing the underlying cloud data.
- Schedule type and technical configuration sensors remain read-only; this
  release does not add schedule editing or additional PointT writes.

### Security and compatibility

- Existing entity unique IDs, device identifiers, history, automations, and
  dashboards remain unchanged.
- Polling intervals, cloud-request counts, credentials, and write permissions
  are unchanged.
- Unrecognized future enum values continue to use the existing safe fallback
  instead of being passed to Home Assistant as an invalid enum state.

### Validation

- 547 automated tests pass with 95.16% branch coverage, including the
  app-confirmed raw values, canonical states, enum options, and matching German
  and English translations.
- Ruff formatting and linting and strict Mypy validation pass.

### Upgrade notes and limitations

- Restart Home Assistant after updating so frontend state translations are
  reloaded.
- The schedule type is still informational. Full schedule editing requires a
  separately verified PointT schema and remains outside this beta.

## [0.7.0-beta.1] - 2026-09-03

This beta adds a transparent calculated dew point for heating circuits
that report both room temperature and relative humidity, without adding PointT
traffic. It is intended for comparison with the controller's internal dew-point
display before the feature is included in a stable release.

### Added

- A **Dew point (calculated)** temperature sensor for every heating circuit
  with compatible `roomtemperature` and `actualHumidity` capabilities.
- Source temperature, source humidity, calculation method, and Magnus
  coefficients as attributes of the calculated dew-point sensor.
- A `value_source` attribute on total electricity consumption to distinguish a
  direct PointT value from the existing complete compressor-plus-auxiliary-
  heater fallback.

### Behavior changes

- Purely derived entity names consistently use the suffix **(calculated)** in
  English and **(berechnet)** in German. The existing system-pressure status
  now follows this convention.
- The dew-point sensor becomes unavailable with either source resource and
  never substitutes a room setpoint for a measured room temperature.

### Security and compatibility

- The calculation runs locally from already polled values and creates no new
  cloud request or write.
- Existing device identifiers and entity unique IDs remain unchanged. The
  system-pressure status receives only a display-name clarification.
- Invalid, non-finite, sentinel, or implausible input values produce no
  calculated measurement.

### Validation

- 504 automated tests pass with 95.13% branch coverage.
- Ruff formatting and linting and strict Mypy validation pass.
- Automated tests cover the Magnus calculation, invalid inputs, exact
  same-circuit capability matching, availability, Home Assistant metadata,
  attributes, and direct-versus-calculated electricity provenance.

### Upgrade notes and limitations

- The calculated dew point is not the controller's private dew-point value or
  cooling-flow setpoint and does not include an unknown installer safety
  offset or minimum-flow-temperature limit.
- The entity is created only where PointT exposes both required measurements
  with the expected type and unit.

## [0.6.0] - 2026-08-30

Version 0.6.0 makes PointT cloud traffic and response behavior observable
without exposing installation data or increasing the request rate.

### Added

- Rolling request metrics for the current and previous 59 clock-minute
  buckets, including successes, failures, exact HTTP statuses, request types,
  retries, fallbacks, rate limits, and bulk-item outcomes.
- Successful-response-time statistics for the last hour: average,
  approximate p95, maximum, latest attempt, and sample count.
- A memory-only diagnostics log containing up to 250 individual HTTP attempts
  from the last 60 minutes. Each entry records a sequence number, age, method,
  request type, outcome, response time, retry state, and safe bulk summaries.
- Three integration-wide diagnostic sensors for total requests, requests in
  the last hour, and average response time in the last hour.

### Behavior changes

- Retries and bounded single-resource fallbacks are identified on the actual
  HTTP attempt instead of being represented only by aggregate counters.
- Successful PointT bulk envelopes keep their outer HTTP status separate from
  aggregated `serverStatus` and inner gateway response statuses.
- All three new diagnostic entities are disabled by default and cause no
  additional cloud requests when enabled.

### Security and compatibility

- Recent request diagnostics never retain URLs, resource paths, gateway or
  config-entry identifiers, tokens, request bodies, response payloads, or
  resource values.
- History is bounded by age and count, held only in memory, and cleared on
  every Home Assistant restart.
- Existing device identifiers, entity IDs, polling intervals, controls, and
  dashboards remain unchanged.

### Validation

- 483 automated tests pass with 95.18% branch coverage, including rolling
  expiry, bounded history, retry and fallback accounting, bulk status
  separation, privacy, and disabled-by-default entity behavior.
- Ruff formatting and linting and strict Mypy type checking pass.

### Upgrade notes and limitations

- Restart Home Assistant after updating.
- Enable the optional request diagnostic sensors manually under **Settings →
  Devices & services → Entities** if continuous visibility is wanted.
- The p95 value is a bounded histogram estimate, not a raw-request percentile.
- Metrics restart at zero when Home Assistant restarts and are not a billing
  or provider quota counter.

## [0.5.5] - 2026-08-30

Version 0.5.5 gives known PointT percentage measurements their correct Home
Assistant semantics without changing discovery or cloud traffic.

### Added

- Humidity device classes for supported heating-circuit, room-zone,
  room-device, and ventilation humidity resources.
- Battery device classes for supported room-device battery levels.

### Behavior changes

- Pump modulation, fan output, radio signal, update progress, and power
  reduction remain generic percentages because they have different semantics.

### Security and compatibility

- Device-class selection requires both an exact allowlisted PointT path and the
  expected percent unit. Existing entity IDs, device identifiers, polling, and
  availability behavior remain unchanged.

### Validation

- 479 automated tests pass with 95.14% branch coverage.
- Ruff formatting and linting pass.
- Strict Mypy type checking passes.

### Upgrade notes and limitations

- No migration or reconfiguration is required.
- The change annotates only resources exposed by the connected installation;
  it cannot make a humidity or battery resource appear when PointT returns it
  as unsupported.

## [0.5.4] - 2026-08-28

Version 0.5.4 improves K30 compatibility when PointT accepts a
bulk request but reports temporary failures for every resource inside it.

### Added

- Diagnostics separately count bulk `serverStatus` and inner gateway-response
  status classes.
- Diagnostics group bounded individual fallback requests by a privacy-safe
  reason.

### Fixed

- Discovery and polling now retry individually when a correctly formed bulk
  item reports a temporary 5xx failure, not only when its payload is malformed.

### Behavior changes

- Bulk remains the default. Individual fallback stays bounded and still does
  not run for unsupported resources, authentication failures, or rate limits.

### Security and compatibility

- The new diagnostics contain only aggregate status classes and reasons. They
  contain no gateway identifiers, resource values, paths, or raw responses.
- Existing entity IDs, device identifiers, history, automations, and dashboards
  remain unchanged.

### Validation

- 469 automated tests pass with 95.13% branch coverage.
- Ruff formatting and linting pass.
- Strict Mypy type checking passes.

### Upgrade notes and limitations

- Affected MX300/K30 installations should retry setup after updating and share
  only reviewed, redacted diagnostics if discovery still fails.

## [0.5.3] - 2026-08-28

Version 0.5.3 improves compatibility with evolving PointT enum values and
makes incomplete but supported capabilities easier to diagnose.

### Added

- Diagnostics identify supported scalar capabilities that currently provide no
  value.
- Diagnostics count distinct undeclared enum values without retaining or
  exposing the values themselves.

### Fixed

- Enum values first reported after entity creation can no longer produce an
  invalid Home Assistant sensor state. The entity reports `unknown` until the
  integration knows the value.
- The PointT heat-source type `gas_boiler` is normalized to the existing
  translated **Boiler** state (**Heizkessel** in German).

### Behavior changes

- One privacy-safe warning is logged per affected entity, using only a
  normalized resource-path template.
- A heat-source type describes that heat source only and is not used to infer
  the type of the complete installation.

### Security and compatibility

- Raw unknown enum values, gateway identifiers, and resource values remain
  excluded from logs and diagnostics.
- Existing entity IDs, device identifiers, history, automations, and dashboards
  remain unchanged.

### Validation

- 466 automated tests pass with 95.16% branch coverage.
- Ruff formatting and linting pass.
- Strict Mypy type checking passes.
- German, English, and source translation catalogs are covered by repository
  tests.
- Dependency audit reports no known vulnerabilities.
- Hassfest, HACS validation, and CodeQL pass in the protected release workflow.

### Upgrade notes and limitations

- Restart Home Assistant after updating.
- Previously unknown enum values remain unavailable until they are explicitly
  understood and translated by the integration.
- No migration is required.

## [0.5.2] - 2026-08-28

Version 0.5.2 improves compatibility with MX300 installations using K30
gateways when individual PointT resources are available but the corresponding
bulk response cannot be processed.

### Added

- Privacy-safe diagnostics are now available while initial setup is still
  retrying.
- Request diagnostics distinguish HTTP-successful bulk items whose payload
  could not be parsed.

### Fixed

- Malformed successful bulk items and unreadable bulk envelopes are recovered
  through strictly bounded individual resource reads.
- MX300/K30 installations with divergent PointT bulk responses can complete
  discovery.
- Diagnostics no longer fail because runtime data is unavailable during the
  first setup attempt.

### Why bulk requests remain the default

A heating installation can expose dozens or hundreds of resources. PointT bulk
requests retrieve up to 30 resources with one HTTP request, reducing cloud
traffic, update latency, and the risk of rate limiting.

Individual requests are therefore used only as a bounded compatibility
fallback. Successfully parsed bulk resources are not requested again
individually.

### Behavior changes

- Discovery uses no more than 30 individual fallback reads in total.
- Normal polling uses no more than 5 fallback reads per affected bulk chunk.
- Unsupported resources returning 403, 404, or 406 do not trigger fallback
  requests.
- No entities, controls, entity IDs, or device identifiers are changed.

### Security and compatibility

- Debug output contains only normalized path templates, HTTP status, and parser
  error categories.
- Tokens, gateway identifiers, raw payloads, and resource values remain
  excluded.
- Authentication and rate-limit handling are unchanged.
- Existing Home Assistant history, automations, and dashboards are preserved.

### Validation

- 463 automated tests pass with 95.13% branch coverage.
- Ruff formatting and linting pass.
- Strict Mypy type checking passes.
- Dependency audit reports no known vulnerabilities.
- Hassfest, HACS validation, and CodeQL pass.

### Upgrade notes and limitations

- Restart Home Assistant after updating.
- An entry currently in `setup_retry` will retry setup automatically.
- The exact cloud-side response difference could not be reproduced locally.
  Confirmation on the affected MX300/K30 installation is still required.

## [0.5.1] - 2026-08-23

Version 0.5.1 corrects how Home Assistant identifies the controller and the
PointT communication gateway of an installation.

### Added

- Controller-aware device identification for MX300 and MX400 modules reported
  in structured `/system/info` data.
- Regression coverage for a real-world MX400 system reported through a
  K40/K40RF gateway and for ambiguous multi-controller data.

### Changed

- Home Assistant now identifies an MX300 or MX400 controller from structured
  `/system/info` module data instead of presenting a K30/K40 communication
  gateway as the heating-system model. K-series gateway identity and hardware
  remain available through diagnostics and gateway resources. Ambiguous or
  missing controller data falls back to a neutral Bosch/Buderus Heating device.
- Development tooling updates Hypothesis to 6.165.10, Mypy to 2.3.1, and Ruff
  plus its pre-commit hook to 0.16.3. Pytest remains at the Home Assistant test
  framework's required 9.0.3 release.

### Security and compatibility

- Device and entity identifiers remain unchanged, preserving existing entity
  IDs, history, automations, and dashboards.
- Recognition uses a strict MX300/MX400 allowlist. A K30/K40 gateway name alone
  is never used to infer a controller model, and conflicting controller data is
  not guessed.
- No additional cloud requests, permissions, stored credentials, or writable
  resources are introduced.

### Validation

- 453 automated tests pass with 95.21% branch coverage.
- Ruff formatting and linting and strict Mypy type checking pass.
- The model-selection test mirrors the observed MX400 plus K40RF structure and
  verifies that the gateway hardware remains available.

### Upgrade notes and limitations

- Restart Home Assistant after upgrading so the device registry receives the
  corrected model and name.
- Only MX300 and MX400 are currently recognized as controller models. Unknown,
  missing, or ambiguous controller information is displayed as the neutral
  Bosch/Buderus Heating device while gateway details remain available in
  diagnostics.

## [0.5.0] - 2026-08-23

Version 0.5.0 expands heat-pump monitoring with optional PointT
resources confirmed in both MyBuderus and HomeCom Easy while retaining strict
capability and schema checks.

### Added

- Optional operating values for emergency and standby mode, the PV contact,
  Smart Function, primary auxiliary-heater state, and passive-cooling inlet
  temperature.
- Per-source current power, power percentage, defrost state, and brine inlet
  and outlet temperatures for every heat-source ID reported by PointT.
- Optional read-only capabilities for heating-circuit boost and cooling,
  fresh-water stations, per-source energy, hybrid systems, low-noise and power
  limiting, solar, pool, ventilation, room zones, room devices, photovoltaics,
  and gateway-provided variable-tariff state.
- English and German names and state translations based on the official app
  terminology.
- A capability-gated Silent Mode select with Off, Automatic, and On options.
- A capability-gated auxiliary-heater mode select with Off, On, and Auto.
- A maximum-supply-temperature number control with whole-degree steps and an
  installation-specific range read dynamically from PointT. A broad 0–100 °C
  plausibility envelope rejects corrupt metadata. It is disabled by default
  because it is an installer-level setting.

### Changed

- Discovery combines gateway references with a bounded official-app path
  catalog. Per-source probes are derived dynamically and do not assume `hs1`
  or `hs2`.
- Unsupported optional paths are discarded after the initial probe and never
  enter recurring polling.
- Fault discovery now derives failure paths only from real `hs<number>` heat
  sources. Ordinary heat-source resources no longer cause pointless
  `activefailure` and `failurelist` probes.
- Optional energy totals, water/gas consumption, volume flow, operating hours,
  power, pressure, humidity, and temperature values use native Home Assistant
  units and state classes when the PointT unit is known.

### Security and compatibility

- Optional resources remain read-only unless they have an explicit write
  policy. Silent Mode and auxiliary-heater mode require their exact writable
  `stringValue` schemas and complete allowed-value sets. Maximum supply
  temperature requires a writable Celsius number with complete gateway limits
  inside a broad 0–100 °C plausibility envelope.
- All three controls use the existing single-PUT transaction with mandatory,
  staggered read-back and no blind write retry.
- Entities are created only when the returned PointT type and unit match a
  known schema. Unexpected schemas remain excluded and visible in redacted
  capability diagnostics.
- Existing entity IDs, devices, and controls are unchanged. New values use the
  existing cadence groups and do not create new write permissions.

### Validation

- 451 automated tests pass with 95.20% branch coverage.
- The development build loads successfully on the physical Buderus K40 test
  system. That profile additionally exposes power-limitation and silent-mode
  state; unsupported optional families remain absent without setup errors.
- The K40 Silent Mode completed a physical `off` → `auto` → `off` write and
  read-back round trip. The original state was restored successfully.
- The K40 auxiliary-heater mode completed a physical `off` → `auto` → `off`
  write and read-back round trip. The heater itself remained off and the
  original setting was restored.
- The K40 maximum supply temperature completed a physical `40` → `41` → `40`
  °C write and read-back round trip. The original setting was restored.
- Ruff formatting and linting and strict Mypy type checking pass.

### Upgrade notes and limitations

- Restart Home Assistant after installing this release so discovery can
  probe the additional paths.
- Every value is optional and appears only when the connected gateway supports
  it. Historical `/recordings` data and the separate shared-energy tariff
  service remain intentionally excluded until an opt-in request and statistics
  model is defined.

## [0.4.1] - 2026-08-23

Version 0.4.1 fixes editing and moving ordinary holiday periods from Home
Assistant and makes the detailed holiday configuration match the terminology
and limits used by MyBuderus and HomeCom Easy.

### Fixed

- Editing, moving, or deleting an ordinary holiday no longer fails with
  **Recurring holidays are not supported** when Home Assistant supplies its
  technical event identifier and the empty recurrence range used for a single
  event.
- Actual recurrence ranges such as `THISANDFUTURE` and recurrence rules such
  as `RRULE` remain explicitly rejected because PointT holiday periods do not
  support recurring events.
- A date or time-only change continues to preserve the existing heating,
  hot-water, ventilation, circuit-assignment, thermal-disinfection, and
  temperature settings.

### Changed

- **Constant temperature** now appears directly below the central-heating
  mode in the **Configure holiday** dialog, before the hot-water settings.
- Constant-temperature input is restricted to the 5–30 °C range supported by
  the official apps. Stricter limits reported by the connected gateway still
  take precedence.
- English and German holiday labels now follow the terms extracted from
  MyBuderus and HomeCom Easy, including **Apply for**, **Central Heating**,
  **Constant temperature**, **Hot Water**, **Ventilation**, **Setback**,
  **OFF with Thermal Disinfection**, and their German equivalents.
- A new step-by-step guide explains how to create, configure, move, rename,
  and delete holiday periods and why calendar details and heating-system
  settings use separate Home Assistant dialogs.

### Validation

- 417 automated tests pass with 95.03% coverage.
- Ruff formatting and linting and strict Mypy type checking pass.
- The updated integration was installed on the physical Buderus K40 test
  system; Home Assistant's configuration check and restart completed
  successfully.

### Upgrade notes

- No entity IDs, devices, or existing dashboard references are changed.
- Restart Home Assistant after installing or updating the integration.

## [0.4.0] - 2026-08-22

Version 0.4.0 turns the previously read-only PointT holiday calendar into a
safe, fully confirmed control. Holiday dates can be managed in Home
Assistant's calendar, while the new integration dialog exposes the additional
heating settings that the standard calendar editor cannot represent.

### Added

- Capability-gated create, edit, and delete support for the **Holiday periods**
  calendar using Home Assistant's standard calendar interface.
- Exact PointT holiday payloads derived from MyBuderus and HomeCom Easy,
  including date mode, affected circuits, heating, hot-water, ventilation,
  thermal-disinfection, fixed-temperature, and encoded-name fields.
- Privacy-safe diagnostics indicating whether holiday calendar writes are
  currently available. The diagnostics schema is now version 5.
- A separate **Configure holiday** dialog for circuit assignments, heating,
  hot-water and ventilation modes, thermal disinfection, and constant room
  temperature. Its fields and choices follow the live PointT configuration.

### Changed

- Editing a holiday changes only its dates and, where PointT supports cloud
  names, its name. All circuit assignments and operating-mode settings are
  preserved from the latest confirmed PointT value.
- New holidays use the same conservative defaults as the official apps:
  all advertised circuits, fixed-temperature heating at 17 °C when supported,
  hot water off, ventilation off when supported, and thermal disinfection on
  when supported.
- PointT date-only end dates and Home Assistant's exclusive calendar end are
  converted in both directions without shifting the visible holiday period.
- Home Assistant's technical `recurrence_id` is accepted for ordinary event
  edits and deletions. Actual recurrence ranges and `RRULE` values remain
  unsupported.

### Security and compatibility

- Holiday writes are enabled only while the read-only list and the dedicated
  write configuration are available, current, and schema-valid.
- POST, PUT, and DELETE requests are each sent at most once. A timed-out
  mutation is resolved by bounded read-back and is never blindly repeated.
- Every mutation must be confirmed from `/holidayMode/list`; otherwise Home
  Assistant reports it as unsuccessful.
- Detailed holiday settings are accepted only from the options advertised by
  `/holidayMode/configuration`, and successful writes preserve the period's
  name and dates.
- Recurrence, location, and description fields are rejected because PointT
  does not support them. Unknown modes, malformed IDs, unsupported time steps,
  and incomplete existing periods are never written.

### Validation

- Creating, editing, configuring, and deleting a holiday was verified against
  a physical Buderus K40 installation. A date or time-only edit preserved all
  circuit assignments, modes, and temperatures.
- Multiple periods and Europe/Berlin daylight-saving transitions are covered
  by automated tests.
- 414 tests pass with 95.02% coverage. Ruff, Mypy, CodeQL, Hassfest, HACS
  validation, and the dependency audit pass.

### Upgrade notes

- No entity IDs or existing dashboard references are changed.
- Holiday controls appear only when the connected gateway advertises the
  required PointT resources. Unsupported fields remain hidden.
- Restart Home Assistant after installing or updating the integration.

## [0.3.0] - 2026-08-22

### Added

- Optional read-only holiday support for `/holidayMode/list`,
  `/holidayMode/configuration`, and `/holidayMode/activeModes`.
- A **Holiday mode active** binary sensor and a read-only **Holiday periods**
  calendar when the connected gateway exposes the corresponding resources.
- A **Next holiday** timestamp sensor showing the current or next period on
  the device page, with its end and status available as attributes.
- Tolerant handling of multiple periods, date-only and timestamp values,
  gateway or Home Assistant time zones, incomplete entries, and empty lists.

### Changed

- Holiday entities now use terminology aligned with MyBuderus and HomeCom Easy:
  **Holiday mode active**, **Next holiday**, and **Holiday periods**, with
  corresponding German translations.
- The capability roadmap now reflects read-only holiday support as released.

### Security and compatibility

- Holiday mode remains separate from the existing Away mode.
- No holiday write, edit, or delete action is exposed before physical
  write/read-back verification.
- Unsupported holiday paths are discarded during capability discovery and
  therefore add no recurring requests.
- Diagnostics contain only support and parser counters, never holiday names,
  dates, raw payloads, or other user-entered details.
- The diagnostics schema is version 4 and exposes only bounded holiday support
  and parser-health metadata.

### Validation

- Verified all three holiday resources on a physical Buderus K40 installation
  with a configured MyBuderus holiday period.
- Confirmed discovery, parsing, calendar output, active-state handling, and the
  next-period timestamp in Home Assistant.

## [0.2.2] - 2026-08-22

This release aligns heating/cooling and circuit states with the enums embedded
in MyBuderus and HomeCom Easy. It replaces previously inferred labels with the
terminology and raw values used by both official apps.

### Added

- A read-only **Heating/cooling switching** entity is created for every
  dynamically discovered `/heatingCircuits/{hc}/suWiSwitchMode` resource.
  Supported states are idle, automatic switching, heating only, and cooling
  only. The entity is omitted when the gateway does not expose the resource.
- Complete English and German state labels for all APK-confirmed heating-
  circuit and hot-water status values, including enabled, disabled, automatic,
  manual, cooling, holiday, extra hot water, and thermal disinfection states.

### Fixed

- **Current heating/cooling status** now uses the PointT values `off`, `forced`,
  and `cooling` instead of the previously inferred summer/winter enum.
- **Heating/cooling support** now recognizes `heat`, `cool`, and `heatCool`;
  camel-case values are normalized only at the Home Assistant boundary.
- **Season optimization** now recognizes `off`, `automatic`, `forcedHeat`, and
  `forcedCool`, with app-aligned labels for automatic switching, heating only,
  and cooling only.
- **Heating circuit status** now includes the APK-confirmed `ch_enabled` value
  in addition to disabled, emergency, floor-drying, summer-pause, boost, away,
  holiday, manual heating/cooling, and automatic heating states.
- **Hot water status** now uses the complete app enum: enabled, disabled,
  automatic, off, Eco, Eco+, Comfort, extra hot water, away, holiday, floor
  drying, and thermal disinfection.

### Compatibility

- Existing config entries, devices, entity IDs, history, dashboards, and
  automations are preserved; no migration is required.
- Only displayed state values and translations change where an older inferred
  enum did not match the official apps.
- The new entity remains read-only. Heating/cooling switching is not exposed as
  a control until a physical write and read-back test confirms safe behavior.
- Unsupported optional paths are ignored after capability discovery and do not
  add recurring cloud requests.

### Quality

- 315 automated tests passed with 95.63% coverage.
- Ruff formatting and linting, Mypy, CodeQL, hassfest, HACS validation, and the
  dependency audit passed.
- The release contains a reproducible integration ZIP and a SHA-256 checksum.

## [0.2.1] - 2026-08-20

### Added

- App-aligned operating states for current heat demand, compressor, electric
  auxiliary heater, central heating, seasonal optimization, data processing,
  and iSRC support.
- German and English state labels based on the terminology used by MyBuderus
  and HomeCom Easy, including blocked, defrost, home cooling, and pool modes.

### Enhanced

- Stable entities are now created for known array and multipart resources even
  when their startup payload is empty or temporarily incomplete.
- Dynamic PointT discovery is complemented by a bounded set of optional status
  resources used by the official apps. Unsupported resources remain ignored
  and do not add recurring cloud requests.
- Starts and operating times consistently provide total, heating, cooling, and
  hot-water entities, ready for values that become available after startup.

## [0.2.0] - 2026-08-19

### Added

- A guarded calculated environmental-energy sensor based on the complete
  PointT total energy balance.
- Active PointT fault and notification support with aggregate problem and count
  entities, appeared/resolved events, restart-safe deduplication, adaptive
  polling, tolerant parsing, and privacy-safe diagnostics.

### Changed

- Energy entities now use consistent context-first German and English names,
  including explicit calculated-value labeling and cooling-energy terminology.
- A direct PointT `electricity` counter and its component fallback now share a
  single total-consumption entity instead of creating duplicate entities.
- Default entity activation now follows an explicit user-facing policy:
  measurements, operating states, energy counters, starts, operating hours,
  TC3, pressure, and controls are enabled; sensitive, technical, and duplicate
  read-only entities remain opt-in.

## [0.1.0] - 2026-08-17

### Added

- Reproducible HACS release archives with a SHA-256 checksum and a tag-gated
  prerelease workflow that repeats all release checks before publishing.
- Capability-based firmware schema checks with an actionable rediscovery
  Repair when known PointT types or units change.
- A local exporter that turns the already-redacted Home Assistant diagnostics
  into a smaller, value-free installation inventory for compatibility work.
- Anonymous energy-counter reset counts in diagnostics while retaining Home
  Assistant's native `total_increasing` reset handling.
- Phase 0 repository and Home Assistant integration skeleton.
- Architecture, privacy, fixture, contribution, and security policies.
- Automated linting, type checking, tests, Home Assistant/HACS validation,
  dependency updates, and CodeQL analysis.
- Repository security baseline with CODEOWNERS, dependency auditing, protected
  branch rules, private vulnerability reporting, and squash-only merges.
- Internal, Home Assistant-independent PointT client with OAuth/PKCE helpers,
  serialized token refresh, typed reads, bounded bulk requests, error mapping,
  and diagnostics redaction.
- Home Assistant config flow for brand selection, SingleKey ID redirect
  validation, gateway discovery and selection, retry, and reauthentication.
- Typed config-entry runtime data with serialized token persistence and gateway
  availability checks.
- Gateway devices and a batch-based data coordinator with resource-level
  availability, last-good values, and separate fast and energy polling.
- Read-only sensors for central temperatures, modulation, system pressure,
  operating status, and direct PointT energy counters.
- Illustrated German beginner setup guide covering installation, SingleKey ID,
  the app redirect, gateway selection, troubleshooting, and safe support
  requests.
- Complete privacy-classified catalog of the 94 resources observed on the
  first K40 profile, including dynamic circuit rules, user-facing terminology,
  and staged write eligibility.
- Bounded batch discovery of the live PointT reference tree, dynamic devices
  for every discovered circuit and read-only entities for safe scalar and
  boolean resources.
- Cloud-friendly polling groups with 60-second live values, five-minute
  settings and energy counters, 15-minute long-term values, static startup
  data, last-good preservation, and bounded rate-limit backoff.
- Migration of early preview logical-device identifiers without losing entity
  IDs, history, areas, or dashboard references.
- Reference catalog of all current entities with PointT write metadata, Home
  Assistant access status, and polling frequency.
- Resource-level attempt, freshness, source, error, and failure state; bounded
  negative pauses for 403/404/504; a five-path core fallback after a batch
  failure; and a gateway circuit breaker after repeated complete failures.
- Gateway serial number, firmware, hardware, and model identifiers in Home
  Assistant device information, plus opt-in diagnostics for serial number,
  UUID, country, and sanitized system information.
- Six pressure-limit sensors from `/heatSources/systemPressureRange`, including
  support for the structured `values` object returned by PointT.
- A serialized PointT write transaction with strict live-capability validation,
  non-retried PUT requests, mandatory read-back, translated HA errors, and a
  heating-circuit operation-mode select verified on the reference K40.
- Dynamic, metadata-bounded controls for heating setpoints, hot-water mode and
  temperatures, extra hot water, fault reduction, and away mode while keeping
  administrative and installer resources read-only.

### Changed

- Use empirically confirmed one-degree steps for hot-water temperature controls
  while retaining half-degree steps for heating-circuit setpoints.
- Localize generated entity and group names from the configured Home Assistant
  system language, while preserving genuinely user-defined circuit names.
- Clarify the three hot-water temperature levels as Comfort, Eco, and Eco+
  start temperatures; document the related PointT-blocked stop temperatures
  and charging deltas from the appliance expert menu.
- Consolidate each gateway into one Home Assistant device and prefix entity
  names with dynamic groups such as `Heating circuit 1 –` and `Heat pump –`.
- Classify the six pressure-limit entities as diagnostics instead of primary
  operating sensors.
- Organize service counters and TC3 as diagnostics and outdoor temperature and
  away mode as primary values.
- Use MyBuderus labels for heating and hot-water temperature levels and decode
  PointT's UTF-16/Base64 configured names for display.
- Retire the empty switch-program placeholder and the duplicate aggregate
  start counter while preserving the detailed compressor counters, and omit
  optional circuit-name sensors when no name is configured.
- Represent static system information as a readable module-and-version text
  instead of an unhelpful module count while retaining sanitized attributes.
- Align German heating labels with the resolved MyBuderus and HomeCom Easy 5.0
  language resources while keeping clearer energy terminology where needed.
- Use the detected Bosch or Buderus brand for device manufacturers and gateway
  names, and identify PointT heat-pump sources as `Heat pump`.
- Redrew the brand icon with a compact symmetric three-blade fan and added its
  SVG source.
- Replaced the planned external Python client distribution with an internal
  `pointt` package that ships with the integration.
- Aligned the development and test environment with Home Assistant 2026.8 on
  Python 3.14.2.
- Expanded setup, reauthentication, retry, and error messages in English and
  German with step-by-step instructions.
