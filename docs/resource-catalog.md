# PointT resource catalog

This catalog describes all 94 resources discovered on the first physical K40
profile on August 15, 2026. It contains no values read from the system, serial
numbers, network data, or other personal identifiers.

The Home Assistant entities currently created from these resources, including
write status and polling interval, are listed in the
[entity and polling catalog](entities.md).

This is an observed device profile, not a fixed list for every system. The
integration must discover each gateway's resource tree dynamically and must
not assume the presence of `hc1`, `dhw1`, or `hs1`.

## Sources and naming

Names and semantics are based on:

1. the PointT resource tree read from physical equipment;
2. the energy semantics verified against MyBuderus, a CSV export, and two
   physical systems in
   [BassXT/buderus#15](https://github.com/BassXT/buderus/pull/15);
3. observed MyBuderus user-facing names;
4. a static comparison of HomeCom Easy 5.0.0 and MyBuderus 5.0.0.

Both app variants contain the same technical resource model and the same
explicit PointT paths. Their differences are limited to brand and product
wording. The integration therefore uses one shared dynamic device and entity
model and applies the detected brand only for display.

The German Android resources were resolved separately and compared with the
English base strings: 5,582 strings per app, including 2,392 related to
heating. The generated working files, APK files, and extracted program code are
not included in this repository.

Path constants found in the apps are only indications of possible features.
They do not prove that a particular heating system exposes a resource. The
dynamically returned resource tree and write metadata of the connected gateway
remain authoritative.

## Legend

- **Read:** suitable for a Home Assistant sensor or diagnostic value.
- **Control:** possible switch, number, or select. A writable PointT field alone
  is not sufficient for release. Every write needs value validation, a write,
  read-back, and confirmation.
- **Diagnostic:** useful for device information or support and normally
  disabled by default.
- **Internal:** required for discovery or calculation but has no separate
  entity.
- **Do not expose:** identity, network, license, time-management, or
  administrative data. These values are not exposed as entities or diagnostics
  even when technically readable.

The code additionally enforces one maturity level for each concrete path:
`observed`, `understood`, `verified`, or `write_verified`. Maturity
determines whether an entity can be exposed safely. Default activation is a
separate user-relevance decision: normal measurements, states, energy values,
and long-term values are enabled; sensitive and technical sensors and sensors
duplicating a control remain disabled. An unknown path stays `observed` and
appears only as a value-free schema in Home Assistant diagnostics. Cloud
writeability never raises maturity by itself.

## Dynamic entity groups

Root resources reference the objects that actually exist:

```text
/heatingCircuits -> hc1, hc2, ...
/dhwCircuits     -> dhw1, dhw2, ...
/heatSources     -> hs1, hs2, ...
```

Current notification discovery additionally reads `/notifications`. Optional
`/heatSources/{hs}/activefailure`, `/heatSources/{hs}/failurelist`, and
`/devices/{device}/errors` paths are probed only for component IDs obtained
from the resource tree or device list. HTTP 403 and 404 responses are treated
as unsupported capabilities and never block the remaining integration.

On the reference K40, `/notifications` returned an `errorList`. The verified
active fault 6249 contained `ccd`, `dcd`, `fc`, `orig`, and `dlv`, but no
human-readable text or timestamp. The legacy heat-source and device-error
paths returned HTTP 404. PointT therefore supplies the current active state on
this profile, while the appliance display remains the source for exact history
and device timestamps.

All entities of a gateway belong to one shared Home Assistant device, for
example **Buderus K40**. Home Assistant does not support true subgroups within a
device, so each entity name has a dynamic prefix such as **Heating circuit 1 –
Operation mode**, **Hot water – Operation mode**, or **Heat pump – Total
operating time**. Gateway and system values start with **Gateway –** and
**System –**, respectively.

No heating-circuit or hot-water entities are created if the corresponding
equipment does not exist. Additional circuits automatically receive their
actual reported number or configured name. Newly added or removed circuits are
added or marked unavailable after rediscovery. IDs come exclusively from
PointT references, never from an assumed number sequence.

In the tables, `{hc}`, `{dhw}`, and `{hs}` represent a dynamically
discovered ID. The reference system reported `hc1`, `dhw1`, and `hs1`.
These IDs remain part of the stable entity identifier but do not create
separate Home Assistant devices.

## Heating circuits

| PointT path | User-facing name | Access | HA mapping / note |
|---|---|---:|---|
| `/heatingCircuits` | Available heating circuits | R | Internal: dynamic discovery |
| `/heatingCircuits/{hc}` | Heating circuit | R | Internal: circuit resources |
| `.../activeSwitchProgram` | Active schedule | R/W | Diagnostic; write only after observing multiple schedules |
| `.../controlType` | Control type | R/W | Diagnostic; safety-relevant configuration, read-only initially |
| `.../currentRoomSetpoint` | Desired temperature | R | Temperature sensor |
| `.../currentSuWiMode` | Summer/winter mode | R | Status sensor |
| `.../heatCoolMode` | Heating/cooling mode | R | Status sensor |
| `.../heatingType` | Circuit heating system | R | Diagnostic, for example underfloor heating or radiators |
| `.../manualRoomSetpoint` | Manual setpoint | R/W | Number in °C |
| `.../maxFlowTemp` | Maximum supply temperature | R/W | Diagnostic number; range validation required |
| `.../name` | Heating-circuit name | R/W | Decoded sensor only when configured; renaming not offered initially |
| `.../operationMode` | Operation mode | R/W | Select: Off, Manual, Auto |
| `.../overallStatus` | Operating status | R | Status sensor |
| `.../switchProgramMode` | Schedule type | R/W | Diagnostic; only temperature-level mode observed so far |
| `.../switchPrograms` | Available schedules | R | Internal: dynamic discovery |
| `.../switchPrograms/{program}` | Schedule | R/W | No separate entity without scalar content; a future editor requires a verified schema |
| `.../switchPrograms/name{program}` | Schedule name | R/W | Readable decoded configuration value |
| `.../temperatureLevels` | Available temperature levels | R | Internal: dynamic discovery |
| `.../temperatureLevels/comfort2` | Heating | R/W | Number in °C, when available |
| `.../temperatureLevels/eco` | Setback | R/W | Number in °C, when available |

Observed `overallStatus` values:

| Raw value | Display |
|---|---|
| `ch_disabled` | Heating disabled |
| `emergency_mode` | Emergency mode |
| `floor_drying` | Screed drying |
| `summer_idle` | Summer idle |
| `boost` | Boost |
| `away` | Away |
| `holiday` | Holiday |
| `cooling_manual_on` | Manual cooling on |
| `cooling_manual_off` | Manual cooling off |
| `heating_manual_on` | Manual heating on |
| `heating_manual_off` | Manual heating off |
| `heating_auto` | Automatic heating |

## Domestic-hot-water circuits

| PointT path | User-facing name | Access | HA mapping / note |
|---|---|---:|---|
| `/dhwCircuits` | Available hot-water circuits | R | Internal: dynamic discovery |
| `/dhwCircuits/{dhw}` | Hot-water circuit | R | Internal: circuit resources |
| `.../actualTemp` | Actual hot-water temperature | R | Temperature sensor |
| `.../charge` | Extra hot water | R/W | Switch/action with start and stop |
| `.../chargeDuration` | Extra hot-water duration | R/W | Number in minutes |
| `.../chargeRemainingTime` | Extra hot-water remaining time | R | Duration sensor in minutes |
| `.../currentSetpoint` | Current hot-water setpoint | R | Temperature sensor |
| `.../currentTemperatureLevel` | Current temperature level | R | Status sensor |
| `.../name` | Hot-water circuit name | R/W | Readable device name; renaming not offered initially |
| `.../operationMode` | Hot-water operation mode | R/W | Select using app names: Off, Eco, Comfort, Auto, Eco+ |
| `.../overallStatus` | Hot-water status | R | Status sensor |
| `.../reduceTempOnAlarm` | Reduce temperature on alarm | R/W | Switch, after physical write test |
| `.../singleChargeSetpoint` | Extra hot-water setpoint | R/W | Number in °C |
| `.../tdMode` | Thermal disinfection | R | Status sensor |
| `.../temperatureLevels` | Available temperature levels | R | Internal: dynamic discovery |
| `.../temperatureLevels/eco` | Eco+ start temperature | R/W | Number in °C |
| `.../temperatureLevels/high` | Comfort start temperature | R/W | Number in °C |
| `.../temperatureLevels/low` | Eco start temperature | R/W | Number in °C |
| `.../temperatureLevels/off` | Off | R | Configuration value in °C |
| `.../highStop`, `.../lowStop`, `.../ecoStop` | Stop temperatures | 403 | Referenced by K40 but not exposed by PointT |
| `.../highChargingDelta`, `.../lowChargingDelta`, `.../ecoChargingDelta` | Charging delta TC1–TW1 | 403 | Expert parameter in K; not exposed by PointT |

PointT codes and app display names for operation mode:

| PointT code | App display |
|---|---|
| `Off` / `off` | Off |
| `low` | Eco |
| `high` | Comfort |
| `ownprogram` | Auto |
| `eco` | Eco+ |

## Heat sources and central measurements

| PointT path | User-facing name | Access | HA mapping / note |
|---|---|---:|---|
| `/heatSources` | Available heat sources | R | Internal: dynamic discovery |
| `/heatSources/actualHeatDemand` | Current heat demand | R | Heating, hot water, and/or frost protection |
| `/heatSources/actualModulation` | Current modulation | R | Measurement in % |
| `/heatSources/actualSupplyTemperature` | Supply temperature | R | Temperature sensor |
| `/heatSources/chStatus` | Central heating status | R | Optional status; probed when omitted from the reference tree |
| `/heatSources/compressor/status` | Compressor status | R | Operating mode including heating, cooling, hot water, defrost, and alarm |
| `/heatSources/Source/eHeater/status` | Auxiliary-heater status | R | Operating mode including heating, hot water, defrost, and blocked states |
| `/heatSources/emStatus` | Energy management status | R | Diagnostic status |
| `/heatSources/flameStatus` | Flame status | R | Status; relevant only to suitable hybrid systems |
| `/heatSources/{hs}` | Heat source | R | Internal: dynamic discovery |
| `/heatSources/{hs}/heatPumpType` | Heat-pump type | R | Diagnostic: air/water, brine/water, or exhaust-air/water |
| `/heatSources/{hs}/numberOfStarts` | Starts by operation | R | Multipart cumulative counter |
| `/heatSources/{hs}/supplyFlowCondenserTemp` | Condenser outlet temperature (TC3) | R | Diagnostic temperature |
| `/heatSources/{hs}/type` | Heat-source type | R | Diagnostic |
| `/heatSources/{hs}/workingTime` | Operating time by operation | R | Multipart cumulative counter |
| `/heatSources/info` | Heat-source information | R | Internal/diagnostic structured object |
| `/heatSources/numberOfStarts` | Total starts | R | No separate entity; identical to `total` of the dynamic heat source |
| `/heatSources/returnTemperature` | Return temperature | R | Temperature sensor |
| `/heatSources/systemPressure` | System pressure | R | Pressure sensor in bar; validated range values as attributes and optional derived status sensor |
| `/heatSources/systemPressureRange` | Permitted pressure range | R | Six static diagnostic sensors in bar |

PointT returns the pressure range as a structured `values` object. The
integration creates one entity for each of `highSystemPressure`,
`absoluteHighPressure`, `lowSystemPressure`,
`shutOfPressureThreshold`, `highPressureThreshold`, and
`lowPressureThreshold`.

## Energy counters

All EMON values are cumulative energy counters in kWh, not current electrical
or thermal power.

| PointT path | Area | Access |
|---|---|---:|
| `/heatSources/emon/totalConsumption` | Total | R |
| `/heatSources/emon/chConsumption` | Heating | R |
| `/heatSources/emon/dhwConsumption` | Hot water | R |
| `/heatSources/emon/coolingConsumption` | Cooling | R |

Each available EMON response can contain these subvalues:

| Raw key | User-facing name | Rule |
|---|---|---|
| `compressor` | Heat-pump electricity consumption | Do not call it “compressor energy”; it may include assigned standby consumption |
| `eheater` | Electric auxiliary-heater consumption | Missing does not mean zero |
| `electricity` | Electricity consumption | Prefer the direct value when available |
| `outputProduced` | Heat or cooling energy produced | Display as “Cooling energy produced” in the cooling area |

Derived total values:

- **Total electricity consumption:** direct `electricity`, or
  `compressor + eheater` only when both components are complete.
- **Total environmental energy:**
  `outputProduced - compressor - eheater`, only for complete, finite,
  non-negative inputs and a non-negative result. The entity is named
  **Total – Environmental energy (calculated)** so it cannot be confused with
  a value supplied directly by PointT.
- Do not calculate environmental energy separately for heating, hot water, or
  cooling. PointT's allocation of standby consumption can create misleading
  negative results for those areas.
- A cooling response with HTTP 200 and only zero-valued counters does not prove
  that cooling equipment exists.

## System values

| PointT path | User-facing name | Access | HA mapping / note |
|---|---|---:|---|
| `/system` | System resources | R | Internal: discovery |
| `/system/awayMode` | Away functions | R | Internal: discovery |
| `/system/awayMode/enabled` | Away mode | R/W | Switch, after physical write test |
| `/system/brand` | System brand | R | Diagnostic |
| `/system/bus` | System bus | R | Diagnostic |
| `/system/country` | System country | R | Diagnostic entity, disabled by default |
| `/system/dateTime` | System time | R/W | Do not expose as a control |
| `/system/globalSeasonOptimizer/currentMode` | Season optimization | R | Optional diagnostic status, disabled by default |
| `/system/iSRC/supportStatus` | iSRC support | R | Static diagnostic status, disabled by default |
| `/system/info` | System information | R | Static text diagnostic sensor with module names and versions; sanitized details as attributes, internal token fields discarded |
| `/system/sensors` | System sensors | R | Internal: discovery |
| `/system/sensors/temperatures` | Temperature sensors | R | Internal: discovery |
| `/system/sensors/temperatures/outdoor_t1` | Outdoor temperature | R | Temperature sensor |
| `/system/sensors/temperatures/outdoorTemperatureSource` | Outdoor temperature source | R | Diagnostic |
| `/system/type` | System type | R | Diagnostic |
| `/system/variableTariff` | Variable electricity tariff | R | Internal: discovery |
| `/system/variableTariff/supportStatus` | Variable-tariff support | R | Diagnostic |

## Gateway values

| PointT path | User-facing name | Access | HA mapping / note |
|---|---|---:|---|
| `/gateway` | Gateway resources | R | Internal: discovery |
| `/gateway/brand` | Gateway brand | R | Device information |
| `/gateway/dateTime` | Gateway time | R | Diagnostic |
| `/gateway/dataProcessing/status` | Data processing status | R | Static diagnostic status, disabled by default |
| `/gateway/serialId` | Serial number | R | Device information and diagnostic entity disabled by default |
| `/gateway/swPrefix` | Software family | R | Diagnostic |
| `/gateway/thirdPartyLicenseInformation` | License information | R | No entity |
| `/gateway/tosAccepted` | Acceptance of terms of service | R/W | Administrative value; never offer as a control |
| `/gateway/tzInfo` | Time-zone information | R | Internal: discovery |
| `/gateway/tzInfo/timeZone` | Time zone | R/W | Diagnostic; never write from Home Assistant |
| `/gateway/update` | Update information | R | Internal: discovery |
| `/gateway/update/status` | Gateway update status | R | Diagnostic |
| `/gateway/uuid` | Gateway UUID | R | Diagnostic entity, disabled by default |
| `/gateway/versionFirmware` | Firmware version | R | Device information and diagnostic entity |
| `/gateway/versionHardware` | Hardware version | R | Device information and diagnostic entity |
| `/gateway/wifi` | Wi-Fi information | R | Do not expose |
| `/gateway/wifi/ip` | IP information | R | Do not expose |
| `/gateway/wifi/ip/ipv4` | IPv4 address | R | Do not expose |
| `/gateway/wifi/mac` | MAC address | R | Do not expose |
| `/gateway/wifi/ssid` | Wi-Fi networks/SSID | R | Do not expose |

## Write release policy

The 24 resources reported as writable by the reference system are not exposed
automatically as controls. The first safe release tier includes:

- operation mode for every dynamically discovered heating circuit;
- manual room setpoint and available temperature levels;
- hot-water operation mode and available temperature levels;
- extra hot water with duration and setpoint;
- away mode.

Schedules, control type, maximum supply temperature, names, and all gateway,
time, and administrative values remain read-only or internal initially. They
need additional device profiles, verified limits, and physical write/read-back
tests.

This release tier is implemented as dynamic selects, number controls, and
switches. A control is created only for an exact path and data-type match.
Select options must be announced completely by the gateway. Number controls
adopt gateway limits only within additional safe bounds. A transaction sends
one PUT without automatic retry and confirms the state using up to three
staggered single-resource requests. The **Manual → Auto → Manual** sequence for
heating-circuit operation mode succeeded on the K40; physical individual tests
for the remaining controls are still pending.
