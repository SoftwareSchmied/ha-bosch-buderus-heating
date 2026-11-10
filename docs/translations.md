# Translations

## Languages

| Language | Code | Status |
| --- | --- | --- |
| English | `en` | Source language, maintained with the code |
| German | `de` | Maintained with the code |
| Swedish | `sv` | Draft, awaiting review by a native speaker |
| Norwegian Bokmål | `nb` | Draft, awaiting review by a native speaker |
| Finnish | `fi` | Draft, awaiting review by a native speaker |
| Danish | `da` | Draft, awaiting review by a native speaker |

The Swedish, Norwegian, Finnish and Danish texts are a careful first draft.
They follow the glossary below, but nobody who speaks these languages as a
mother tongue has checked them yet. Corrections are very welcome (see
[Contributing corrections](#contributing-corrections)).

Any other language shows the English texts.

## Where the texts live

Every language has two data files in
`custom_components/bosch_buderus_heating/`, and both fall back to English:

1. **`translations/<code>.json`: Home Assistant translations.**
   `strings.json` is the English source; each language file has exactly the
   same keys and placeholders. Home Assistant uses them for the setup and
   options dialogs, repairs, error messages, entity names with a
   translation key and state names. They follow the language of the user
   profile.
2. **`i18n/<code>.json`: texts the integration builds itself.**
   - `texts`: entity and device names composed at runtime (for example
     "Heating circuit 1 – Supply temperature"), notification texts,
     connection status lines and a few dialog choices, keyed by the English
     text. The entry `"%Y-%m-%d"` is the date format of notifications.
     German and English of these texts stay next to the code, so `en.json`
     and `de.json` have no `texts`. These texts follow the Home Assistant
     system language (Settings → System → General); after changing it,
     reload the integration so generated names are rebuilt.
   - `dialogs`: the texts of the controls and holiday dialogs, for every
     language including English and German. The server sends them with
     each dialog step, and the dialog picks the language of the user
     profile.

The integration finds the languages by these files; no Python module and no
dialog script names a language. Language tags are matched from the full tag
to the base language: `pt-BR` uses `pt-BR.json` if there is one, else
`pt.json`; `sv-FI` uses Swedish. `no` and Nynorsk (`nn`) use Norwegian
Bokmål. Anything else shows English.

## Adding a language

Add two files, named with the Home Assistant language code (for example
`nl`), and nothing else:

1. `custom_components/bosch_buderus_heating/translations/nl.json`: a copy of
   `strings.json` with every value translated.
2. `custom_components/bosch_buderus_heating/i18n/nl.json`: a copy of
   `i18n/sv.json` with every value of `texts` and `dialogs` translated.
   Keep the keys (the English texts under `texts`) unchanged.

Without the second file the language still works, but built names,
notifications and the two dialogs stay English.

`tests/test_translations.py` picks up a new file automatically. It checks
that each file has exactly the keys of the English source, the same
placeholders (`{name}`, `{}` …), Markdown links and HTML tags, and that the
dialog texts have the same keys in every language.

## Known gaps

- The names of the TC100.2 room controller zones and their settings are
  still German or English only.
- Readings that only the local access provides carry the wording of the
  controller's Info menu in German and English. In the other languages they
  are translations of the English wording, not the controller's own text.
- Values the appliance reports as free text (for example circuit names set
  on the controller) stay as reported.

## Glossary

The drafts use these terms consistently. A correction to one term usually
applies to every text that uses it.

| English | German | Swedish | Norwegian Bokmål | Finnish | Danish |
| --- | --- | --- | --- | --- | --- |
| heat pump | Wärmepumpe | värmepump | varmepumpe | lämpöpumppu | varmepumpe |
| heating circuit (HC) | Heizkreis (HK) | värmekrets (VK) | varmekrets (VK) | lämmityspiiri (LP) | varmekreds (VK) |
| hot water (DHW) | Warmwasser (WW) | varmvatten (VV) | varmtvann (VV) | lämmin käyttövesi (LKV) | varmt brugsvand (VV) |
| extra hot water | Extra-Warmwasser | extra varmvatten | ekstra varmtvann | lisälämmin käyttövesi | ekstra varmt vand |
| heat generator, heat source | Wärmeerzeuger | värmekälla | varmekilde | lämmönlähde | varmekilde |
| auxiliary heater | Zuheizer | tillsatsvärme | tilleggsvarme | lisälämmitin | tilskudsvarme |
| compressor | Verdichter, Kompressor | kompressor | kompressor | kompressori | kompressor |
| defrost | Abtauen | avfrostning | avriming | sulatus | afrimning |
| heating (mode) | Heizen | värme | oppvarming | lämmitys | opvarmning |
| cooling (mode) | Kühlen | kyla | kjøling | jäähdytys | køling |
| supply (flow) temperature | Vorlauftemperatur | framledningstemperatur | turtemperatur | menolämpötila | fremløbstemperatur |
| return temperature | Rücklauftemperatur | returtemperatur | returtemperatur | paluulämpötila | returtemperatur |
| outdoor temperature | Außentemperatur | utomhustemperatur | utetemperatur | ulkolämpötila | udetemperatur |
| room temperature | Raumtemperatur | rumstemperatur | romtemperatur | huonelämpötila | rumtemperatur |
| setpoint | Sollwert | börvärde | settpunkt | asetusarvo | sætpunkt |
| target temperature | Wunschtemperatur, Solltemperatur | önskad temperatur | ønsket temperatur | tavoitelämpötila | ønsket temperatur |
| operating mode | Betriebsart | driftläge | driftsmodus | käyttötila | driftsform |
| schedule, time program | Zeitprogramm | tidsprogram | tidsprogram | aikaohjelma | tidsprogram |
| holiday | Urlaub | semester | ferie | loma | ferie |
| holiday period | Urlaubszeit | semesterperiod | ferieperiode | lomajakso | ferieperiode |
| away mode | Abwesenheit | bortaläge | bortemodus | poissa-tila | fraværstilstand |
| silent mode | Leiser Betrieb | tyst läge | stillemodus | hiljainen tila | lydsvag drift |
| thermal disinfection | Thermische Desinfektion | termisk desinfektion | termisk desinfeksjon | lämpödesinfiointi | termisk desinfektion |
| ventilation | Lüftung | ventilation | ventilasjon | ilmanvaihto | ventilation |
| brine | Sole | köldbärare | kollektorvæske | keruuliuos | brine |
| refrigerant | Kältemittel | köldmedium | kuldemedium | kylmäaine | kølemiddel |
| water softener | Enthärter | avhärdare | vannmykner | vedenpehmennin | blødgøringsanlæg |
| system, installation (plant) | Anlage | anläggning | anlegg | järjestelmä | anlæg |
| room controller, room device | Raumgerät, Raumregler | rumsenhet, rumsregulator | romenhet, romregulator | huonelaite, huonesäädin | rumenhed, rumregulator |
| zone | Zone | zon | sone | vyöhyke | zone |
| fault | Störung | fel | feil | vika | fejl |
| notification (from the appliance) | Meldung | meddelande | melding | ilmoitus | meddelelse |
| notification (in Home Assistant) | Benachrichtigung | avisering | varsel | ilmoitus | notifikation |
| sign-in | Anmeldung | inloggning | pålogging | kirjautuminen | login |
| sign in again | erneut anmelden | logga in igen | logg på igjen | kirjaudu uudelleen | log ind igen |
| gateway | Gateway | gateway | gateway | yhdyskäytävä | gateway |
| local access | lokaler Zugang | lokal åtkomst | lokal tilgang | paikallinen yhteys | lokal adgang |
| cloud access | Cloud-Zugang | molnåtkomst | skytilgang | pilviyhteys | cloudadgang |
| request, polling | Abfrage | förfrågan | forespørsel | kysely | forespørgsel |
| live values | Livewerte | livevärden | sanntidsverdier | reaaliaikaiset arvot | liveværdier |
| reading, value | Messwert, Wert | mätvärde, värde | måleverdi, verdi | mittausarvo, arvo | måleværdi, værdi |
| connection | Verbindung | anslutning | tilkobling | yhteys | forbindelse |
| certificate | Zertifikat | certifikat | sertifikat | varmenne | certifikat |
| firmware | Firmware | firmware | fastvare | laiteohjelmisto | firmware |
| entity | Entität | entitet | entitet | entiteetti | entitet |
| device | Gerät | enhet | enhet | laite | enhed |
| repair (Home Assistant) | Reparatur | reparation | reparasjon | korjaus | reparation |
| diagnostics | Diagnose | diagnostik | diagnostikk | diagnostiikka | diagnosticering |
| options | Optionen | alternativ | alternativer | asetukset | indstillinger |
| reconfigure | neu konfigurieren | konfigurera om | konfigurer på nytt | määritä uudelleen | omkonfigurer |
| wallbox | Wallbox | laddbox | ladeboks | latausasema | ladeboks |
| electricity consumption | Stromverbrauch | elförbrukning | strømforbruk | sähkönkulutus | elforbrug |
| energy | Energie | energi | energi | energia | energi |
| run time | Laufzeit | drifttid | driftstid | käyntiaika | driftstid |
| dew point | Taupunkt | daggpunkt | duggpunkt | kastepiste | dugpunkt |
| system pressure | Systemdruck | systemtryck | systemtrykk | järjestelmän paine | systemtryk |
| photovoltaics | Photovoltaik | solceller | solceller | aurinkosähkö | solceller |
| solar (thermal) | Solar | solvärme | solvarme | aurinkolämpö | solvarme |
| switch point | Schaltpunkt | växlingspunkt | koblingspunkt | kytkentäpiste | koblingspunkt |
| polling profile | Abfrageprofil | förfrågningsprofil | forespørselsprofil | kyselyprofiili | forespørgselsprofil |
| cloud-friendly | Cloud-schonend | molnvänlig | skyvennlig | pilviystävällinen | cloudvenlig |
| fan level | Lüfterstufe | fläktsteg | viftetrinn | puhallintaso | blæserniveau |
| Apply (button) | Übernehmen | Tillämpa | Bruk | Käytä | Anvend |
| reauthenticate | erneut authentifizieren | autentisera igen | autentiser på nytt | todenna uudelleen | godkend igen |

Control names shorten the group, for example "HC 1: Operating mode". The
short forms are given in brackets in the first rows of the table.

Product, standard and preset names stay as they are: Home Comfort, Eco,
Eco+, Boost, SG Ready, Smart Function, Power Guard, PV. "Comfort" is
translated (Komfort, Komfort, Mukavuus, Komfort).

Style: sentence case; Swedish, Norwegian and Danish address the user with
"du", Finnish with the second person singular, as Home Assistant does.
Brand names of manufacturers do not appear in labels.

## Contributing corrections

1. Edit `custom_components/bosch_buderus_heating/translations/<code>.json`
   for dialog, repair and entity texts, or
   `custom_components/bosch_buderus_heating/i18n/<code>.json` for generated
   names, notification texts and the controls and holiday dialogs.
2. Keep the keys, placeholders in braces, Markdown links and line breaks
   exactly as they are; only translate the text.
3. If a glossary term is wrong, please say so in the pull request, so it can
   be changed everywhere at once.
4. Run `pytest tests/test_translations.py` (and `npm test` for the dialogs).
   No other file needs to change.

A short pull request or an issue with the corrected wording is equally
welcome. Please mention whether you are a native speaker.
