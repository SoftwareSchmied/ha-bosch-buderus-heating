# Confirmed installations

This page lists the heaters on which the integration has been tested, based on
users' reports, together with the
features they confirmed. It contains only what was actually reported, worded
no more strongly than the report itself. A feature that is not listed was not
reported, which does not mean it does not work.

To add your installation, please post it in the
[0.9.x feedback discussion](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/111)
if you use a 0.9 version (let Home Assistant run for a few hours first and
attach the [diagnostics](diagnostics.md#download-diagnostics)), or in
[Share your installation](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/81)
for any other version. Please report bugs as
[issues](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/new/choose),
and keep serial numbers, gateway IDs and account details out of your post.

Thank you to everyone who has shared their system and test results.

Last updated: 2026-11-18.

## Tested heaters

| Manufacturer | Model | Variants | Type | Controller | Gateway / internet module | Confirmed features | Reports |
|---|---|---|---|---|---|---|---|
| Bosch | Compress 5800i AW | CS5800iAW 12 E | air-to-water heat pump (monobloc, heating and cooling) | CR 20 RF wireless room controller | K40RF, firmware 15.00.01 | readings (cloud and local; temperatures, system pressure, starts, operating hours and energy counters compared with a second data source), refrigerant circuit readings, ventilation unit readings, battery state of the wireless room controller, local access | [#111](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/111), [#137](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/137) |
| Bosch | Compress 7800i LW | CS7800iLW 8 F | brine-to-water heat pump | – | K40RF, firmware 15.00.01 | readings (cloud and local), brine temperatures, hot water, energy counters, compressor and electric heater power, local access | [#111](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/111) |
| Bosch | Dry 6000i | – | dehumidifier | – | cloud account | controls (reported as working, the tested functions were not listed) | [#127](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/127) |
| Buderus | Logatherm WLW176i | WLW176i-12 TP70, WLW-10 MB AR | air-to-water heat pump (monobloc) | Logamatic BC400 | MX400, K40RF | readings, heating setpoint, hot water mode, holidays, time programs (read-only), faults and notifications, energy counters, local access, operation without cloud | – |
| Buderus | Logatherm WLW186i | WLW186i-12 TP70, WLW186i-12 TP70 W, WLW186i-12 E | air-to-water heat pump (monobloc) | Logamatic BC400 | MX400, K40RF | readings, heating controls (including a second heating circuit), hot water temperature levels, time programs (read-only), hot water schedule changes (WLW186i-12 E), local access | [#23](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/23), [#95](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/95), [#111](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/111), [#125](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/125) |
| Buderus | Logatherm WLW196i | WLW196i AR | air-to-water heat pump (split) | – | MX300, K30 | readings, holidays (read-only) | – |
| Buderus | Logamax plus GB172 | – | gas condensing boiler, wall-hung | Logamatic RC310 | Logamatic web KM200 | readings | [#81](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/81), [#104](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/104) |
| Buderus | Logamax plus GB172i.2 with Logatherm WLW MB 5 AR | – | hybrid system (gas condensing boiler, air-to-water heat pump, HYC25 hybrid module) | – | MX300 | readings, hybrid control strategy and energy prices (read-only) | [#138](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/138) |
| Buderus | Logamax plus GBH192i.2 | GBH192i.2-25 H | gas condensing boiler, wall-hung | – | MX300 | readings, energy counters, hot water, faults, holidays (read-only) | Home Assistant community |
| Buderus | Logano plus GB212 | GB212-22 H | gas condensing boiler, floor-standing | Logamatic TC100.2 (five zones) | built-in Wi-Fi, firmware 05.04.00 | readings, faults and notifications, firmware check, start counter (total), room controller zones (setpoints, modes, schedule selection, temporary setpoint, switch point editing, advance to the next switch point, open window detection, absences), hot water mode, boost, away mode, radiator thermostats | [#113](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/113), [#122](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/122), [#111](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/111) |
| Buderus | Logano G125 BE | G125 BE ECO-21 | oil boiler with burner | – | – | start counter (with the reporter's own fix) | [#64](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/64) |
| Buderus | Logawater Soft WE800i | – | water softener | – | WX100, firmware 01.00.00 | readings (salt level, available soft water, flow, regeneration state, time until the next regeneration, low salt with the default threshold), holiday mode state; controls not tested | [#60](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/60) |
| Buderus | – | – | gas condensing boiler with solar thermal | – | MX300 | readings, solar circuit (collector and tank temperatures, pump modulation, solar yield) | [#48](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/48) |
| Buderus | – | – | heat pump with cooling | Logamatic | MX300, K30 | readings, heating circuit humidity, cooling room setpoint, calculated dew point (compared with the controller display) | [#7](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/7), [#17](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/17) |
| Buderus | – | – | – | – | MX300, K30, firmware 15.00.01 | readings, second heating circuit (name, targets and status, with 0.9.2-beta.2) | [#130](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/130) |
| Buderus | – | – | – | RT10RF/RC10RF room thermostats (eight zones) | MX400, K40, firmware 15.00.01 | readings, climate entities of both heating circuits (setpoint and mode of the underfloor circuit), zone climate entities (with 0.9.2-beta.2, "seems to work well") | [#131](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/131) |

"–" means not stated in the report. In the last column, "–" means that the report is not a GitHub thread.

## Reported problems and open points

What users reported on their systems, worded as in the reports. An entry is
not a statement that the problem still exists in the latest version.

| System | Reports | Reported |
|---|---|---|
| Bosch Compress 5800i AW 12 E, K40RF, wallbox Power Charge 7000i S (software 5.3.0) | [#111](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/111), [#137](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/137) | With the wallbox on the same account the cloud access status stays at "starting" while the readings work; the environmental energy includes the cooling energy; two entities are named "Heating circuit 1 – Operation mode"; code 3161 is shown as unknown (low battery of the wireless room controller); the wallbox lifetime counter is not available as an energy sensor; the cooling mode and the ventilation settings are read-only. The wallbox controls and the recordings were created but not tested yet (0.9.2-beta.2). |
| Bosch K40RF, local setup | [#139](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/139) | Local setup ended with an invalid schema error because one module entry of the system information has no serial number (0.9.1). The reporter's own local code change worked as a workaround; the issue is open. |
| IVT Geo 600, K40, local access only | [#132](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/132) | The gateway reports its brand as "unknown" and the local setup stopped (0.9.1). With 0.9.2-beta.1 the reporter wrote that it "works better now" and will send diagnostics. |
| Two Bosch air conditioners (Midea climate), cloud | [#136](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/136) | Both devices switch between unavailable and connected although the manufacturer app shows them online (0.9.1); the issue is open. |
| Buderus MX300, two heating circuits | [#130](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/130) | The second heating circuit had no name and no target in Auto (0.9.1). With 0.9.2-beta.2 the reporter wrote "it works" for it and reported a long loading time of the activity log with many unknown entries. |
| Buderus MX400, eight zones | [#131](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/131) | Zones were read-only in 0.9.1. With 0.9.2-beta.2 the battery and radio entities of the room thermostats appear under "room unit X" instead of the room, and the "not connected since" time shows a date in the year 2000. |
| Buderus MX300 hybrid system | [#138](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/138) | Hybrid control strategy and energy prices are read-only; writable entities were requested. |
| Logamax plus GB172, KM200 (iCom) | [#104](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/104), [#105](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/105), [#135](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/135) | Entities were unavailable for some hours and the setup failed at times because the cloud answered slowly or not at all (0.8.3); later the sign-in was lost and needed several attempts. Both issues are closed. |
| Logatherm WLW186i-12 TP70, BC400, MX400 | [#103](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/103) | The circulation pump resource is listed by the gateway but the cloud refuses to read it, so no pump state is available. |
| Buderus system with a MyBuderus WDDW2 provider, model not stated | [#107](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/107) | No gas or electricity total counter and no operating hours are reported by the cloud (seen in diagnostics only). |
| Heat pump system, model not stated | [#120](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/120) | Faults with code and explanation were shown in Home Assistant; the reporter did not find a list of past faults (answered in the documentation). |
