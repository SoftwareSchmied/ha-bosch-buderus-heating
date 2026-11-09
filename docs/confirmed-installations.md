# Confirmed installations

This page lists the installations on which users have reported the
integration working, and the features they have checked. It contains only
what was actually reported, worded no more strongly than the report itself.
An empty cell means "not reported", not "does not work". A confirmation
covers the reporter's installation and version; it does not establish that
every feature works on the same model.

To add your installation, please post it in the
[0.9 alpha feedback discussion](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/111)
if you are testing a 0.9 alpha, or in
[Share your installation](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/81)
for any other version. Please report bugs as
[issues](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/new/choose),
and keep serial numbers, gateway IDs and account details out of your post.

Thank you to everyone who has shared their system and test results.

Last updated: 2026-11-09.

## Installations

Installation 1 is my own reference installation, which I use for development
and testing. The others are ordered by the date of their first report.

| No. | Device / heat source | Controller | Gateway / internet module | Access | Integration version | HA version | Source |
|---|---|---|---|---|---|---|---|
| 1 | Buderus Logatherm WLW176i-12 TP70 + WLW-10 MB AR (heat pump), one heating circuit, hot water | MX400 | K40RF, firmware 15.00.01 | cloud and local | 0.8.3; 0.9.0 alphas, most recently a build of alpha.3 | 2026.10.4 | [my report](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/81#discussioncomment-19384201), 2026-11-03; [0.9.0-alpha.3 checks](release.md#090-alpha3-verification), 2026-11-09 |
| 2 | Logatherm WLW196i AR (heat pump), floor cooling, one heating circuit | MX300 | K30 | cloud | 0.5.4, later 0.6.0 and 0.7.0-beta.1 | 2026.8.1 | [haddung](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/7#issuecomment-5464713402), 2026-08-29; [haddung](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/17#issuecomment-5532672603), 2026-09-03 |
| 3 | WLW186i-12 E (heat pump), two heating circuits | not stated | MX400 | cloud | 0.7.0-beta.7 | not stated | [derderimmerlacht](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/23#issuecomment-5575642729), 2026-09-07; [derderimmerlacht](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/23#issuecomment-5615997807), 2026-09-10 |
| 4 | Heat pump, model not stated | not stated | MX400 | cloud | 0.7.2 to 0.8.2, later 0.8.4-beta.1 | 2026.10.0 to 2026.11.0 | [lanebe](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/49#issuecomment-6497630818), 2026-10-29; [lanebe](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/49#issuecomment-6581530225), 2026-11-02 |
| 5 | Gas condensing boiler with solar thermal, model not stated; one heating circuit, hot water, one solar circuit | not stated | MX300 (device class `k30`) | cloud | 0.8.1 | 2026.9.4 (stated with 0.7.2) | [ph15ch](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/48#issuecomment-6382117555), 2026-10-24 |
| 6 | Logawater Soft WE800i (water softener) | none | WX100_V1, firmware 01.00.00 | cloud | 0.8.3 | 2026.9.2 | [marcinchrzest](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/60#issuecomment-6629370010), 2026-11-04 |
| 7 | G125 BE ECO-21 | not stated | not stated | cloud | own fork based on 0.8.1; 0.8.2-beta.3 | not stated | [CHLINDE](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/64#issuecomment-6288347611), 2026-10-20; [CHLINDE](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/64#issuecomment-6308908523), 2026-10-21 |
| 8 | 186i (heat pump) | not stated | not stated | cloud | 0.8.1 | not stated | [Gothmoth88](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/49#issuecomment-6444754846), 2026-10-27 |
| 9 | Logatherm WLW186i-12 TP70 (air-to-water heat pump), one heating circuit | BC400 system control | MX400 / K40RF, firmware 15.00.01 | cloud | 0.8.3-beta.1 | 2026.10.4 (stated with 0.8.2) | [ReuDa](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/95#issuecomment-6554911185), 2026-11-01 |
| 10 | Logatherm WLW186i-12 TP70 (heat pump) | BC400 system control | MX400 | cloud | 0.8.2 | 2026.10.2 | [HaJoSa](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/103#issuecomment-6626259161), 2026-11-04 |
| 11 | GB172, one heating circuit, hot water | RC310 | iCom_Low_NSC_V1; a KM200 V2 is named in [#105](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/105) | cloud | 0.8.3 | 2026.10.4 | [gejomo](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/81#discussioncomment-19383800), 2026-11-03 |
| 12 | GB212-22 H (gas condensing boiler), one heating circuit with five RRC zones, hot water | TC100.2 (rrc2, firmware 05.04.00) | built-in Wi-Fi, MyMode app | cloud | 0.8.3; zone values only with a local patch | 2026.11.0 | [obvius1](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/81#discussioncomment-19501810), 2026-11-09; [obvius1](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/113), 2026-11-09 |

## Features

The columns are the installation numbers from the table above.

- ✅ confirmed by the reporter
- ⚠️ works with limits or only with a patch (see the footnote)
- ❌ reported problem (see the linked issue)

| Feature | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Readings | ✅[^own-readings] | ✅[^k30-readings] | ✅[^hc2-readings] | | ✅[^solar] | | ⚠️[^starts] | | ✅[^reuda-readings] | | ✅[^gejomo-readings] ❌ [#104](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/104) | ✅[^tc-readings] |
| Heating setpoint | ✅[^own-setpoint] | | ✅[^hc2-controls] | | | | | | | | | |
| Hot water mode | ✅[^own-dhw] | | | | | | | | | | | |
| Holidays | ✅[^own-holiday] | ✅[^k30-holidays] | | | | | | | | | | |
| Time programs | ✅[^own-programs] | | | | | | | | ✅[^reuda-programs] | | | |
| RRC zones | | | | | | | | | | | | ❌ [#113](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/113) ⚠️[^rrc-patch] |
| Faults and notifications | ✅[^own-faults] | | | | | | | | | | | ✅ |
| Energy counters | ✅[^own-energy] | | | | | | | | | | | |
| Local access | ✅[^own-local] | | | | | | | | | | | |
| Operation without cloud | ✅[^own-no-cloud] | | | | | | | | | | | |
| Additional device families | | | | | | ✅[^we800i] | | | | | | |

[^own-readings]: Outdoor, supply and return temperatures, system pressure, hot-water temperature, compressor and auxiliary-heater status, modulation, starts and operating hours ([my report](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/81#discussioncomment-19384201)).
[^own-setpoint]: Manual heating target 19.0 → 18.5 → 19.0 °C through the controls dialog and the native climate entity. I checked each change in the official app and restored the original setting. This test ran on 2026-10-24 with HA 2026.10.3 ([heating controls](heating-controls.md)); it does not cover mode changes or other circuits.
[^own-dhw]: Auto → Off → Auto through the native water-heater entity, checked in the official app and restored ([PointT hot water](pointt-hot-water.md)). Other modes are covered by automated tests only.
[^own-holiday]: An edit to an existing holiday period, checked in the official app and restored ([my report](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/81#discussioncomment-19384201)).
[^own-programs]: The heating and hot-water time programs, read-only ([my report](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/81#discussioncomment-19384201)).
[^own-faults]: The active fault 6249 is shown. Bell notifications appeared when faults occurred and changed to resolved when they cleared ([field validation](release.md#field-validation-before-081), [#36](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/36)). Warnings, maintenance messages, critical and historical faults have not occurred on my system ([project status](specification-status.md#status-by-area)).
[^own-energy]: Heat produced and electricity consumption ([my report](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/81#discussioncomment-19384201)).
[^own-local]: With the 0.9 alphas: discovery on the network, adding the local access with an existing token, the identity comparison with the cloud and regular reading of about 180 local resources ([compatibility](compatibility.md#local-access-checked-on-my-system-and-not-verifiable-there)). The local access only reads; controls go through the cloud.
[^own-no-cloud]: I switched the cloud access off and on again while Home Assistant was running. Without the cloud, the local readings stayed available and the fault display came from the gateway's local error list; afterwards no fault was announced twice ([0.9.0-alpha.1 checks](release.md#090-alpha1-verification), [compatibility](compatibility.md#local-access-checked-on-my-system-and-not-verifiable-there)). Bell notifications without the cloud are not tested in practice, because no fault was active.
[^k30-readings]: Setup failed with 0.5.1 and 0.5.3 ([#7](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/7)). With 0.5.4 it completed, and all 111 discovered resources were available and fresh. The supply temperature matched the controller, and the heating-circuit humidity is shown; the calculated dew point matched his own calculation ([#17](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/17)). The controller's cooling flow setpoint and dew point are not available through the cloud on this system.
[^k30-holidays]: The holiday periods are shown as a calendar ([haddung](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/7#issuecomment-5464713402)); changes were not reported.
[^hc2-readings]: The second heating circuit was missing before 0.7.0-beta.4 ([#23](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/23)); with 0.7.0-beta.4 its sensors appear.
[^hc2-controls]: Since 0.7.0-beta.7 the controls of both heating circuits appear. He reported that changed values showed up in the official app and that his automations run without problems; which controls he changed is not stated.
[^solar]: The six solar-circuit entities were missing with 0.7.2 ([#48](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/48)). On 0.8.1 they are created, update regularly and came back after two restarts or reloads.
[^starts]: The start counter appeared only with his own fix in a fork based on 0.8.1 ([#64](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/64)). In 0.8.2-beta.3 it was unavailable; his further fix was merged for 0.8.2-beta.4. There is no report since that change.
[^reuda-readings]: The heating temperature levels, the hot-water start temperature, the heating threshold and the silent-mode times match the controller display.
[^reuda-programs]: The switch points and levels of the read-only heating and hot-water programs match the official app, and the transitions at several switch times happened on time.
[^gejomo-readings]: The outdoor temperature is named as working; controls were not tested. See also the reported problems below.
[^tc-readings]: Outdoor temperature, supply temperature, modulation and hot-water temperature.
[^rrc-patch]: Unpatched 0.8.3 shows only the manual and temporary setpoint per zone. Room temperature, active setpoint, mode and program are missing, because the TC100.2 sends the availability flag as text ([#113](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/113)). With his local patch all zone values appear, and changes of the zone mode and the manual temperature reached the MyMode app and the thermostat. Program selection and hot water were not tested. The patch is not part of a release yet.
[^we800i]: Water softener: salt level, available soft water, water flow, regeneration activity and progress, time until the next regeneration and holiday mode match the official app. With 0.8.3 the low-salt indicator is available, using the fallback threshold. The controls (start regeneration, holiday mode) are disabled by default and were not tested. 0.8.2 did not start on HA 2026.9.2; 0.8.3 fixed this ([#60](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/60)).

### Other reported problems

- **Sign-in, installation 4:** the stored sign-in was rejected in October and
  again on 2026-11-02, and a new sign-in was needed
  ([#49](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/49),
  open). In a separate outage the official app could not connect either; it
  ended after the MX400 was disconnected and plugged in again.
- **Sign-in, installation 8:** the sign-in address did not appear in the
  console of a portable browser; signing in worked on another PC
  ([#49](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/49)).
- **Start-up, installation 11:** after the update to 0.8.3, more than half of
  the entities were not provided for a while; the diagnostics showed many cloud
  timeouts during start-up
  ([#104](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/104),
  open). Later the setup failed with "PointT discovery returned no response";
  restarting the KM200 V2 helped only for a short time
  ([#105](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/105),
  open). Since 0.9.0-alpha.2 the integration starts with the last known setup
  in this case; there is no report from this installation yet.
- **Deprecation warnings, installation 4:** with 0.8.4-beta.1, HA 2026.11.0
  logged deprecation warnings for the integration. After my note that 0.9.0-alpha.3 fixes them, he
  reported that the warnings are gone; the installed version was not stated
  ([#106](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/106),
  open).
- **Circulation pump, installation 10:** the gateway lists the circulation
  pump, but the cloud answers "forbidden" to a normal user account, so the
  integration cannot show it
  ([#103](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/103)).

## Open confirmations

These features and device families have no confirmation from a real
installation yet:

- the 0.9 alphas on any installation other than mine, with other gateways,
  firmware versions or device families, and a longer run on mine
  ([0.9.0-alpha.3 checks](release.md#090-alpha3-verification));
- for the local access: further heating circuits, solar, ventilation and room
  zones, radio devices and their zones, signing in with the details printed on
  the device, a real certificate change, removing and adding the local access
  again, real network failures, long-term stability, and gateways or firmware
  other than K40RF 15.00.01
  ([compatibility](compatibility.md#local-access-checked-on-my-system-and-not-verifiable-there));
- the same fault reported by the cloud and the local error list, and bell
  notifications without the cloud;
- warnings, maintenance messages, critical and historical faults
  ([project status](specification-status.md#status-by-area));
- RRC zone controls and RRC weekly-program editing without a patch; so far the
  only zone report needs a local patch;
- heating mode changes and other heating circuits on my system, and hot-water
  modes other than Auto and Off;
- clock changes for the time programs, which are covered by automated tests
  only;
- the estimated energy from the current power on a heat source with a gas
  burner ([#107](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/107));
- hybrid HM200 signals;
- the controls of the WE800i water softener (start regeneration, holiday mode,
  salt threshold);
- the other [device families](device-support-plan.md): water heaters, air
  conditioners, air purifiers, dehumidifiers and read-only wallboxes;
- token renewal over long periods and endurance runs;
- installations of the second supported brand: all reports so far come from
  systems of one brand.
