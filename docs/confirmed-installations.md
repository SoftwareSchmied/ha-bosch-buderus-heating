# Confirmed installations

This page lists the heaters on which the integration has been tested, based on
users' reports and on my own reference installation, together with the
features they confirmed. It contains only what was actually reported, worded
no more strongly than the report itself. A feature that is not listed was not
reported, which does not mean it does not work.

To add your installation, please post it in the
[0.9 alpha feedback discussion](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/111)
if you are testing a 0.9 alpha, or in
[Share your installation](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/81)
for any other version. Please report bugs as
[issues](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/new/choose),
and keep serial numbers, gateway IDs and account details out of your post.

Thank you to everyone who has shared their system and test results.

Last updated: 2026-11-12.

## Tested heaters

| Manufacturer | Model | Variants | Type | Controller | Gateway / internet module | Confirmed features |
|---|---|---|---|---|---|---|
| Buderus | Logatherm WLW176i | WLW176i-12 TP70, WLW-10 MB AR | air-to-water heat pump (monobloc) | Logamatic BC400 | MX400, K40RF | readings, heating setpoint, hot water mode, holidays, time programs (read-only), faults and notifications, energy counters, local access, operation without cloud |
| Buderus | Logatherm WLW186i | WLW186i-12 TP70, WLW186i-12 E (outdoor unit WLW MB-5 AR) | air-to-water heat pump (monobloc) | Logamatic BC400 | MX400, K40RF | readings, heating controls, time programs (read-only), local access (WLW186i-12 E) |
| Buderus | Logatherm WLW196i | WLW196i AR | air-to-water heat pump (split) | – | MX300, K30 | readings, holidays (read-only) |
| Buderus | Logamax plus GB172 | – | gas condensing boiler, wall-hung | Logamatic RC310 | Logamatic web KM200 | readings |
| Buderus | Logano plus GB212 | GB212-22 H | gas condensing boiler, floor-standing | Logamatic TC100.2 | built-in Wi-Fi | readings, faults and notifications, RRC zones from 0.9.0-alpha.4 without a local patch: room temperature, active setpoint, zone names, operating mode and manual setpoint changed from Home Assistant (also shown in the manufacturer's app); with 0.9.0-alpha.5 (controller firmware 05.04.00), each change checked in the manufacturer's app and set back: program switch, temporary setpoint in the schedule mode with its clear button, `heat` with a target and back to `auto`, open window detection of a zone off and on, a one-day absence created and deleted, hot water mode `ownprogram` → `high` → `ownprogram`, boost on and off, away mode on and off with a refused zone target while it is on; heating schedule selection on all five zones with the controller's program names; readings that look right: zone climates with next setpoint and zone status, window sensors, system pressure, outdoor temperature, humidity, display and cause code, boost settings, away temperature, hot water mode and temperature, 8 radiator thermostats as separate devices in the right areas with battery and valve state |
| Buderus | Logano G125 BE | G125 BE ECO-21 | oil boiler with burner | – | – | start counter (with the reporter's own fix) |

The Logatherm WLW176i is my own reference installation. "–" means not stated
in the report.
