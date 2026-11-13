# Irish (ga) glossary

Draft, awaiting review by a native speaker. Based on the glossary in
[`docs/translations.md`](../translations.md); Home Assistant core terms are
used where core already has them (marked HA) and where they carry the right
meaning.

Address: second person singular as in Home Assistant core ("déan iarracht
arís", "do ghléas"). Sentence case. Button names quoted in texts:
**Cuir isteach** (Submit, HA config flow button), **Cuir i bhfeidhm**
(Apply). Date format of notifications: `%d/%m/%Y`

Generated names are built from parts ("Ciorcad téimh 1 – Teocht an
tsreafa"), so names and group names stand alone in the nominative.

| English | Irish |
| --- | --- |
| heat pump | caidéal teasa (HA) |
| heating circuit (HC) | ciorcad téimh (CT) (HA) |
| hot water (DHW) | uisce te (UT) (HA) |
| extra hot water | uisce te breise |
| heat generator, heat source | foinse teasa |
| boiler | coire (HA) |
| burner | dóire |
| auxiliary heater | téitheoir cúnta |
| electric heater | téitheoir leictreach |
| compressor | comhbhrúiteoir (HA) |
| pump | caidéal |
| valve | comhla |
| cylinder, storage tank | sorcóir |
| buffer tank | umar maolánach |
| defrost | díreo (HA) |
| heating (mode) | téamh (HA) |
| cooling (mode) | fuarú (HA) |
| supply (flow) temperature | teocht an tsreafa (HA) |
| return temperature | teocht fillte (HA) |
| outdoor temperature | teocht lasmuigh (HA) |
| room temperature | teocht an tseomra (HA) |
| setpoint | socphointe (HA) |
| target temperature | teocht sprice (HA) |
| operating mode | mód oibríochta (HA) |
| schedule, time program | sceideal (HA) |
| holiday | laethanta saoire (HA); saoire in compounds |
| holiday period | tréimhse saoire |
| away mode | mód as baile (HA) |
| silent mode | mód ciúin (HA) |
| thermal disinfection | díghalrú teirmeach |
| ventilation | aeráil (HA) |
| brine | sáile |
| refrigerant | cuisneán |
| water softener | bogthóir uisce |
| system, installation (plant) | córas (HA); installation: suiteáil |
| room controller, room device | rialaitheoir seomra, gléas seomra |
| zone | crios (HA) |
| fault | locht (HA) |
| error | earráid (HA) |
| notification (from the appliance) | teachtaireacht |
| notification (in Home Assistant) | fógra (HA) |
| sign-in | síniú isteach (HA) |
| sign in again | sínigh isteach arís |
| reauthenticate | athfhíordheimhnigh (HA) |
| account | cuntas |
| gateway | geata (HA) |
| local access | rochtain áitiúil |
| cloud access | rochtain néil |
| request | iarratas |
| polling | fiosrú |
| live values | luachanna beo |
| reading, value | léamh, luach |
| connection | nasc (HA) |
| certificate | deimhniú (HA) |
| firmware | dochtearraí (HA) |
| entity | aonán (HA) |
| device | gléas (HA) |
| integration | comhtháthú (HA) |
| repair (Home Assistant) | deisiúchán (HA) |
| diagnostics | diagnóisic |
| options | roghanna (HA) |
| reconfigure | athchumraigh (HA) |
| discovery | aimsiú |
| wallbox | luchtaire balla |
| electricity consumption | ídiú leictreachais |
| energy | fuinneamh |
| run time | am oibríochta |
| starts | tosuithe |
| dew point | pointe drúchta (HA) |
| system pressure | brú an chórais |
| photovoltaics | fótavoltach (PV) |
| solar (thermal) | teas gréine |
| switch point | pointe lasctha |
| polling profile | próifíl fiosraithe |
| cloud-friendly | Spárálach leis an néal |
| fan level | leibhéal an fhean |
| Apply (button) | Cuir i bhfeidhm |
| OK (button) | Ceart go leor (HA) |
| reload | athlódáil (HA) |
| on, off (state) | ar siúl, as (HA) |
| Comfort (preset) | Compord |

Room controller terms added in 0.9.0-alpha.5:

| English | Irish |
| --- | --- |
| temporary setpoint | socphointe sealadach |
| open window detection | brath fuinneoige oscailte |
| valve opening | oscailt na comhla |
| optimum start | tosú optamach |
| heating curve | cuar téimh |
| base point / end point (heating curve) | bunphointe / críochphointe |
| night setback | íslithe oíche |
| fireplace function | feidhm teallaigh |
| boost | Boost (unchanged) |
| radiator valve | comhla teirmeastait radaitheora |
| whisper mode (radiator valve) | oibriú ciúin na comhla |
| locking / blocking fault | locht a éilíonn athshocrú / locht blocála |
| heat-up rate | ráta teasaithe |
| heat request (zone) | éileamh teasa |
| event (calendar) | imeacht |
| absence | neamhláithreacht |
| child lock | glas leanaí |
| season mode | mód séasúir |
| boiler (Heizgeraet) | coire |
| electric heater (Elektroheizstab) | téitheoir leictreach |
| energy statistics, unit not confirmed | (kWh, gan deimhniú) |

Core terms not taken over: "vótaíocht" for polling (it means voting),
"Díreoite" for the defrost state, and "lucht leanúna" for a fan (it means
followers); "fean" is used instead.

Unchanged: Home Comfort, Eco, Eco+, Boost, SG Ready, Smart Function, Power
Guard, PV, SingleKey ID, product and brand names, codes such as VR1 or TC0.
