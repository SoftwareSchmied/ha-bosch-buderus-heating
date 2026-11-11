# Slovak glossary (sk)

Reviewed (2026-11-10). The Slovak texts in
`translations/sk.json` and `i18n/sk.json` use these terms consistently; a
correction to one term usually applies to every text that uses it.

Style: sentence case, formal address (vykanie: "Zadajte", "Vyberte"), as Home
Assistant uses for Slovak. Confirmations in the first person avoid gendered forms: "Lokálne prihlásenie je povolené a modrý signál bol vidieť", "Potvrdzujem, že …". Counts are written in a neutral form
("Neuložené zmeny: {count}") so that no plural form is needed. Date format of
notifications: `%d. %m. %Y`.

| English | Slovak |
| --- | --- |
| heat pump | tepelné čerpadlo |
| heating circuit (HC) | vykurovací okruh; in generated names Okruh |
| hot water (DHW) | teplá voda, also in generated names |
| extra hot water | extra ohrev (after the group, "Teplá voda 1: Extra ohrev") |
| heat generator, heat source | zdroj tepla |
| auxiliary heater | prídavný ohrev ("Režim prídavného ohrevu") |
| compressor | kompresor |
| defrost | odmrazovanie |
| heating (mode) | vykurovanie |
| cooling (mode) | chladenie |
| supply (flow) temperature | výstupná teplota ("Limit výstupnej teploty") |
| return temperature | vratná teplota |
| outdoor temperature | vonkajšia teplota |
| room temperature | izbová teplota |
| setpoint | požadovaná hodnota |
| target temperature | požadovaná teplota |
| operating mode | prevádzkový režim |
| schedule, time program | časový program |
| holiday | dovolenka |
| holiday period | obdobie dovolenky |
| away mode | režim neprítomnosti |
| silent mode | tichý režim |
| thermal disinfection | tepelná dezinfekcia |
| ventilation | vetranie |
| brine (heat pump ground circuit) | nemrznúca zmes ("Teplota vstupu nemrznúcej zmesi") |
| brine (salt brine of the water softener) | soľanka |
| refrigerant | chladivo |
| water softener | zmäkčovač vody |
| system, installation (plant) | systém |
| room controller, room device | izbový regulátor, izbová jednotka |
| radiator thermostat | termostatická hlavica |
| zone | zóna |
| fault | porucha |
| notification (from the appliance) | hlásenie |
| notification (in Home Assistant) | upozornenie |
| sign-in | prihlásenie |
| sign in again | prihláste sa znova |
| gateway | brána |
| local access | lokálny prístup |
| cloud access | cloudový prístup |
| request, polling | požiadavka, dopytovanie |
| live values | živé hodnoty |
| reading, value | nameraná hodnota, hodnota |
| connection | pripojenie |
| certificate | certifikát |
| firmware | firmvér |
| entity | entita |
| device | zariadenie |
| appliance | zariadenie, spotrebič |
| repair (Home Assistant) | oprava |
| diagnostics | diagnostika |
| options | možnosti |
| reconfigure | prekonfigurovať |
| reauthenticate | znova overiť |
| wallbox | nabíjacia stanica (wallbox) |
| electricity consumption | spotreba elektriny |
| energy | energia |
| run time | prevádzková doba |
| dew point | rosný bod |
| system pressure | tlak v systéme |
| photovoltaics | fotovoltika |
| solar (thermal) | solárny ohrev |
| switch point | spínací bod |
| polling profile | profil dopytovania |
| cloud-friendly | šetrný ku cloudu |
| fan level | stupeň ventilátora |
| setback (reduced mode) | útlm |
| modulation | modulácia |
| burner | horák |
| boiler (appliance) | kotol |
| hot water tank | zásobník teplej vody |
| bivalence point | bod bivalencie |
| Apply (button) | Použiť |
| OFF / ON (upper-case options and states) | VYP / ZAP |
| auxiliary heater (generated names) | Prídavný ohrev, "Režim prídavného ohrevu" |
| fresh-water station | stanica čerstvej vody ("Primárne čerpadlo stanice čerstvej vody") |
| plan regeneration (water softener) | naplánovanie regenerácie ("Naplánovanie regenerácie") |
| requested supply temperature (signal) | vyžiadaná výstupná teplota ("Vyžiadaná výstupná teplota okruhu 1 (signál)") |

Room controller terms added in 0.9.0-alpha.5 (drafts, awaiting review):

| English | Slovak |
| --- | --- |
| program / schedule | časový program |
| radiator valve | ventil termostatickej hlavice |
| window open detection | detekcia otvoreného okna |
| boost | Boost (funkcia Boost) |
| fireplace mode | funkcia krbu |
| energy history | história energie |
| temporary setpoint | dočasná požadovaná hodnota |
| radio device | rádiové zariadenie |
| heating curve | vykurovacia krivka |
| night setback | nočný útlm |
| season mode | sezónny režim |
| child lock | detský zámok |
| away / absence | neprítomnosť |
| energy statistics, unit not confirmed | (kWh, nepotvrdené) |

Control names write the group out, for example "Okruh 1: Prevádzkový režim"
or "Teplá voda 1: Teplota".
Product, standard and preset names stay as they are: Home Comfort, SingleKey
ID, Eco, Eco+, Boost, SG Ready, Smart Function, Power Guard, PV. "Comfort" is
translated as "Komfort". Brand names of manufacturers do not appear in labels.
