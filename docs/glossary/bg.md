# Bulgarian glossary (bg)

Draft, awaiting review by a native speaker. The Bulgarian texts in
`translations/bg.json` and `i18n/bg.json` use these terms consistently; a
correction to one term usually applies to every text that uses it.

Style: sentence case, formal address (second person plural: "Въведете",
"Изберете"), as Home Assistant uses for Bulgarian. Counts are written in a
neutral form ("Незапазени промени: {count}") so that no plural form is needed.
Date format of notifications: `%d.%m.%Y`.

| English | Bulgarian |
| --- | --- |
| heat pump | термопомпа |
| heating circuit (HC) | отоплителен кръг (ОК) |
| hot water (DHW) | гореща вода; in short forms БГВ (битова гореща вода) |
| extra hot water | екстра гореща вода |
| heat generator, heat source | топлинен източник |
| auxiliary heater | допълнителен нагревател |
| compressor | компресор |
| defrost | размразяване |
| heating (mode) | отопление |
| cooling (mode) | охлаждане |
| supply (flow) temperature | температура на подаване |
| return temperature | температура на връщане |
| outdoor temperature | външна температура |
| room temperature | стайна температура |
| setpoint | зададена стойност |
| target temperature | желана температура |
| operating mode | режим на работа |
| schedule, time program | времева програма |
| holiday | ваканция |
| holiday period | ваканционен период |
| away mode | режим на отсъствие |
| silent mode | тих режим |
| thermal disinfection | термична дезинфекция |
| ventilation | вентилация |
| brine | разсол |
| refrigerant | хладилен агент |
| water softener | омекотител за вода |
| system, installation (plant) | система |
| room controller, room device | стаен регулатор, стайно устройство |
| radiator thermostat | радиаторен термостат |
| zone | зона |
| fault | повреда |
| notification (from the appliance) | съобщение |
| notification (in Home Assistant) | известие |
| sign-in | вход |
| sign in again | влезте отново |
| gateway | шлюз |
| local access | локален достъп |
| cloud access | облачен достъп |
| request, polling | заявка, запитване |
| live values | стойности в реално време |
| reading, value | измерена стойност, стойност |
| connection | връзка |
| certificate | сертификат |
| firmware | фърмуер |
| entity | обект |
| device | устройство |
| appliance | уред |
| repair (Home Assistant) | поправка |
| diagnostics | диагностика |
| options | опции |
| reconfigure | преконфигуриране |
| reauthenticate | повторно удостоверяване |
| wallbox | зарядна станция (wallbox) |
| electricity consumption | потребление на електроенергия |
| energy | енергия |
| run time | време на работа |
| dew point | точка на оросяване |
| system pressure | налягане в системата |
| photovoltaics | фотоволтаици |
| solar (thermal) | соларна система |
| switch point | точка на превключване |
| polling profile | профил на запитванията |
| cloud-friendly | щадящ облака |
| fan level | степен на вентилатора |
| setback (reduced mode) | понижен режим |
| modulation | модулация |
| burner | горелка |
| boiler (appliance) | котел |
| hot water tank | бойлер |
| bivalence point | бивалентна точка |
| Apply (button) | Приложи |

Room controller terms added in 0.9.0-alpha.5:

| English | Bulgarian |
| --- | --- |
| temporary setpoint | временна зададена стойност |
| open window detection | откриване на отворен прозорец |
| valve opening | отваряне на вентила |
| optimum start | оптимизиран старт |
| heating curve | отоплителна крива |
| base point / end point (heating curve) | базова точка / крайна точка |
| night setback | нощно понижение |
| fireplace function | функция камина |
| boost | Boost (unchanged) |
| radiator valve | вентил на радиаторен термостат |
| whisper mode (radiator valve) | тиха работа на вентила |
| locking / blocking fault | повреда, изискваща нулиране / блокираща повреда |
| heat-up rate | скорост на загряване |
| heat request (zone) | заявка за топлина |
| event (calendar) | събитие |
| absence | отсъствие |
| child lock | детска ключалка |
| season mode | сезонен режим |
| boiler (Heizgeraet) | котел |
| electric heater (Elektroheizstab) | електрически нагревател |
| energy statistics, unit not confirmed | (kWh, непотвърдено) |

Control names shorten the group, for example "ОК 1: Режим на работа". The
short form ОК (отоплителен кръг) resembles the Latin "OK"; it is kept because
it is the abbreviation used in Bulgarian heating documentation.
Product, standard and preset names stay as they are: Home Comfort, SingleKey
ID, Eco, Eco+, Boost, SG Ready, Smart Function, Power Guard, PV. "Comfort" is
translated as "Комфорт". Brand names of manufacturers do not appear in labels.
