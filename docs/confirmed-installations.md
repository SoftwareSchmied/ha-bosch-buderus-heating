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

| Manufacturer | Model | Variants | Type | Controller | Gateway / internet module | Confirmed features |
|---|---|---|---|---|---|---|
| Bosch | Compress 5800i AW | CS5800iAW 12 E | air-to-water heat pump (monobloc, heating and cooling) | CR 20 RF wireless room controller | K40RF, firmware 15.00.01 | readings (cloud and local; temperatures, system pressure, starts, operating hours and energy counters compared with a second data source), refrigerant circuit readings, ventilation unit readings, battery state of the wireless room controller, local access |
| Bosch | Compress 7800i LW | CS7800iLW 8 F | brine-to-water heat pump | – | K40RF, firmware 15.00.01 | readings (cloud and local), brine temperatures, hot water, energy counters, compressor and electric heater power, local access |
| Buderus | Logatherm WLW176i | WLW176i-12 TP70, WLW-10 MB AR | air-to-water heat pump (monobloc) | Logamatic BC400 | MX400, K40RF | readings, heating setpoint, hot water mode, holidays, time programs (read-only), faults and notifications, energy counters, local access, operation without cloud |
| Buderus | Logatherm WLW186i | WLW186i-12 TP70, WLW186i-12 TP70 W, WLW186i-12 E | air-to-water heat pump (monobloc) | Logamatic BC400 | MX400, K40RF | readings, heating controls (including a second heating circuit), hot water temperature levels, time programs (read-only), hot water schedule changes (WLW186i-12 E), local access |
| Buderus | Logatherm WLW196i | WLW196i AR | air-to-water heat pump (split) | – | MX300, K30 | readings, holidays (read-only) |
| Buderus | Logamax plus GB172 | – | gas condensing boiler, wall-hung | Logamatic RC310 | Logamatic web KM200 | readings |
| Buderus | Logamax plus GB172i.2 | – | hybrid system (gas condensing boiler, air-to-water heat pump, HYC25 hybrid module) | – | MX300 | readings, hybrid control strategy and energy prices (read-only) |
| Buderus | Logamax plus GBH192i.2 | GBH192i.2-25 H | gas condensing boiler, wall-hung | – | MX300 | readings, energy counters, hot water, faults, holidays (read-only) |
| Buderus | Logano plus GB212 | GB212-22 H | gas condensing boiler, floor-standing | Logamatic TC100.2 (five zones) | built-in Wi-Fi, firmware 05.04.00 | readings, faults and notifications, firmware check, start counter (total), room controller zones (setpoints, modes, schedule selection, temporary setpoint, switch point editing, advance to the next switch point, open window detection, absences), hot water mode, boost, away mode, radiator thermostats |
| Buderus | Logano G125 BE | G125 BE ECO-21 | oil boiler with burner | – | – | start counter (with the reporter's own fix) |

"–" means not stated in the report.

## Additional devices

| Manufacturer | Model | Type | Gateway / internet module | Confirmed features |
|---|---|---|---|---|
| Bosch | Dry 6000i | dehumidifier | cloud account | controls (reported as working, the tested functions were not listed) |
| Buderus | Logawater Soft WE800i | water softener | WX100, firmware 01.00.00 | readings (salt level, available soft water, flow, regeneration state, time until the next regeneration, low salt with the default threshold), holiday mode state; controls not tested |
