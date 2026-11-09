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

Last updated: 2026-11-09.

## Tested heaters

| Heater | Type | Controller | Gateway / internet module | Confirmed features |
|---|---|---|---|---|
| Buderus Logatherm WLW176i-12 TP70 with outdoor unit WLW-10 MB AR (my own reference installation) | air-to-water heat pump (monobloc) | – | MX400, K40RF (firmware 15.00.01) | readings, heating setpoint, hot water mode, holidays, time programs (read-only), faults and notifications, energy counters, local access, operation without cloud |
| Buderus Logatherm WLW186i-12 TP70 | air-to-water heat pump (monobloc) | Logamatic BC400 | MX400, K40RF (firmware 15.00.01) | readings, time programs (read-only) |
| Buderus Logatherm WLW186i-12 E | air-to-water heat pump (monobloc) | – | MX400 | readings (two heating circuits), heating controls |
| Buderus Logatherm WLW196i AR | air-to-water heat pump (split) | – | MX300, K30 | readings, holidays (read-only) |
| Buderus Logamax plus GB172 | gas condensing boiler, wall-hung | Logamatic RC310 | Logamatic web KM200 (V2), iCom_Low_NSC_V1 | readings |
| Buderus Logano plus GB212-22 H | gas condensing boiler, floor-standing | Logamatic TC100.2 (rrc2, firmware 05.04.00) | built-in Wi-Fi | readings, faults and notifications, RRC zones (only with a local patch) |
| Buderus Logano G125 BE ECO-21 | oil boiler with burner (low-temperature, cast iron) | – | – | start counter (with the reporter's own fix) |

"–" means not stated in the report.

## Open confirmations

These features and device families have no confirmation from a real
installation yet:

- the 0.9 alphas on any installation other than mine, with other gateways,
  firmware versions or device families, and a longer run on mine;
- for the local access: further heating circuits, solar, ventilation and room
  zones, radio devices and their zones, signing in with the details printed on
  the device, a real certificate change, removing and adding the local access
  again, real network failures, long-term stability, and gateways or firmware
  other than K40RF 15.00.01;
- the same fault reported by the cloud and the local error list, and bell
  notifications without the cloud;
- warnings, maintenance messages, critical and historical faults;
- RRC zone controls and RRC weekly-program editing without a patch; so far the
  only zone report needs a local patch;
- heating mode changes and other heating circuits on my system, and hot-water
  modes other than Auto and Off;
- clock changes for the time programs, which are covered by automated tests
  only;
- the estimated energy from the current power on a heat source with a gas
  burner (#107);
- hybrid HM200 signals;
- the controls of the WE800i water softener (start regeneration, holiday mode,
  salt threshold);
- the other device families: water heaters, air
  conditioners, air purifiers, dehumidifiers and read-only wallboxes;
- token renewal over long periods and endurance runs;
- installations of the second supported brand: all reports so far come from
  systems of one brand.
