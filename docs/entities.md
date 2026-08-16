# Entities and polling

This list documents the state observed on the reference system on August 16,
2026. The integration creates entities dynamically, so other systems may expose
more, fewer, or different entities.

`{hc}`, `{dhw}`, and `{hs}` represent the heating circuits, domestic-hot-water
circuits, and heat sources that were actually discovered. The corresponding
name group is created for every additional circuit. All entities remain
assigned to one shared gateway device and use their group as a display-name
prefix.

Generated group and entity names follow the **system language** configured in
Home Assistant. For example, the same entity appears as **System – System
type** in English and with its translated name in other supported languages.
User-defined names such as **Upper floor** are not translated. Names that were
manually changed in Home Assistant also remain unchanged.

The live comparison on August 16, 2026 processed all 94 readable resources
without parser errors. Known resources can produce up to 91 read-only entities.
Measurements, states, energy counters, and long-term values useful in daily
operation are enabled by default. Technical, sensitive, and control-duplicate
sensors are created disabled by default. Unknown manufacturer extensions
appear only in redacted diagnostics. Ten readable leaf resources intentionally
have no entity because they contain private data, licensed content, or purely
internal values. PointT returned HTTP 403 for another 49 gateway-referenced
paths; the cloud does not expose their content, so they cannot be offered as
entities.

Cumulative energy entities use the Home Assistant state class
`total_increasing`. If a counter drops to a lower non-negative value after a
reset or device replacement, Home Assistant treats this as a new meter cycle,
not negative consumption. The integration does not alter the measurement, but
records detected drops anonymously as `energy_counter_resets_detected` in
diagnostics.

## Meaning of writeability columns

- **PointT reports writable** describes only the cloud resource metadata. It
  does not prove that writing is safe on every system.
- **Controllable in HA** describes the current integration behavior. A safe
  user setting is exposed as a control only when path, data type, write flag,
  range, and allowed options all match exactly.
- The reference system reports 24 writable resources. Twenty are represented
  as readable entities. Thirteen safe user settings are also controllable;
  administrative and installer settings remain locked.

## Maturity and default activation

| Maturity | Meaning | Home Assistant representation |
|---|---|---|
| `observed` | Seen in the resource tree, but its meaning is not sufficiently established | No entity; redacted diagnostics only |
| `understood` | Type and functional meaning established | Entity possible; default activation follows the user relevance described below |
| `verified` | Sufficiently confirmed for normal display | Read-only entity, enabled by default |
| `write_verified` | Write and read-back confirmed on real equipment | Control entity, enabled by default |

The heating-circuit operation mode has been write-confirmed on the reference
system. Other released controls use the same tested write/read-back service and
are additionally constrained by current gateway metadata. Their individual
effects have not all been tested on physical equipment.

Default activation is defined explicitly:

- **Enabled:** temperatures and operating states, all available energy
  counters, starts, operating hours, TC3, system pressure, the derived pressure
  status, and all released switches, number controls, and selects.
- **Disabled:** serial number, gateway UUID, country, detailed system
  information, individual raw software-update fields, technical device and
  configuration values, and read-only sensors whose value is already
  represented by an active control.
- **No entity:** unknown manufacturer extensions, Wi-Fi and network data,
  license text, credentials, and other private or purely internal resources.

Example of an avoided duplicate: **Heating circuit 1 – Manual setpoint** is
available as an active number control. The additional read-only sensor for the
same PointT path remains disabled by default.

Open a disabled entity under **Settings → Devices & services → Entities** to
enable it. An entity deliberately enabled by the user is not disabled again by
an integration update.

## Polling rules

| Group | Frequency | Content on the reference system |
|---|---:|---:|
| Live values | 60 seconds | 25 entities |
| Settings | 5 minutes | 19 entities |
| Energy | 5 minutes | up to 15 entities |
| Long-term values | 15 minutes | 13 entities |
| Static | at startup only | 20 known possible entities |

Due groups are combined into batch requests of no more than 30 resources.
HTTP 429 pauses further requests temporarily. Successful partial values and
the most recent valid state are retained during partial failures.

## Gateway and system

| Entity | HA type | PointT resource / subvalue | PointT reports writable | Controllable in HA | Polling |
|---|---|---|:---:|:---:|---:|
| Brand | Diagnostic sensor | `/gateway/brand` | No | No | startup only |
| Serial number (disabled) | Diagnostic sensor | `/gateway/serialId` | No | No | startup only |
| Gateway UUID (disabled) | Diagnostic sensor | `/gateway/uuid` | No | No | startup only |
| Date and time | Diagnostic sensor | `/gateway/dateTime` | No | No | 15 min |
| Software prefix | Diagnostic sensor | `/gateway/swPrefix` | No | No | startup only |
| Time zone | Diagnostic sensor | `/gateway/tzInfo/timeZone` | Yes | No | 5 min |
| Software update – current progress | Diagnostic sensor | `/gateway/update/status` → `progress.cur_percent` | No | No | 60 s |
| Software update – current step | Diagnostic sensor | `/gateway/update/status` → `progress.cur_step` | No | No | 60 s |
| Software update – total steps | Diagnostic sensor | `/gateway/update/status` → `progress.nsteps` | No | No | 60 s |
| Software update – progress | Diagnostic sensor | `/gateway/update/status` → `progress.percent` | No | No | 60 s |
| Software update – status | Diagnostic sensor | `/gateway/update/status` → `status.value` | No | No | 60 s |
| Firmware version | Diagnostic sensor | `/gateway/versionFirmware` | No | No | startup only |
| Hardware version | Diagnostic sensor | `/gateway/versionHardware` | No | No | startup only |
| Current heat demand | Sensor | `/heatSources/actualHeatDemand` | No | No | 60 s |
| Current modulation | Sensor | `/heatSources/actualModulation` | No | No | 60 s |
| Supply temperature | Sensor | `/heatSources/actualSupplyTemperature` | No | No | 60 s |
| Energy management status | Sensor | `/heatSources/emStatus` | No | No | 60 s |
| Flame status | Sensor | `/heatSources/flameStatus` | No | No | 60 s |
| Return temperature | Sensor | `/heatSources/returnTemperature` | No | No | 60 s |
| System pressure | Sensor | `/heatSources/systemPressure` | No | No | 60 s |
| System pressure status | Derived status sensor | System pressure and `/heatSources/systemPressureRange` | No | No | with system pressure |
| Permitted pressure range – high system pressure | Diagnostic sensor | `/heatSources/systemPressureRange` → `highSystemPressure` | No | No | startup only |
| Permitted pressure range – absolute maximum pressure | Diagnostic sensor | `/heatSources/systemPressureRange` → `absoluteHighPressure` | No | No | startup only |
| Permitted pressure range – low system pressure | Diagnostic sensor | `/heatSources/systemPressureRange` → `lowSystemPressure` | No | No | startup only |
| Permitted pressure range – shutdown pressure | Diagnostic sensor | `/heatSources/systemPressureRange` → `shutOfPressureThreshold` | No | No | startup only |
| Permitted pressure range – upper pressure limit | Diagnostic sensor | `/heatSources/systemPressureRange` → `highPressureThreshold` | No | No | startup only |
| Permitted pressure range – lower pressure limit | Diagnostic sensor | `/heatSources/systemPressureRange` → `lowPressureThreshold` | No | No | startup only |
| Away mode | Sensor and switch | `/system/awayMode/enabled` | Yes | Yes | 5 min; read back after change |
| Brand | Diagnostic sensor | `/system/brand` | No | No | startup only |
| Country (disabled) | Diagnostic sensor | `/system/country` | No | No | startup only |
| System information (disabled) | Text diagnostic sensor with module names, versions, and sanitized attributes | `/system/info` | No | No | startup only |
| System bus | Diagnostic sensor | `/system/bus` | No | No | 15 min |
| Outdoor temperature source | Diagnostic sensor | `/system/sensors/temperatures/outdoorTemperatureSource` | No | No | 60 s |
| Outdoor temperature | Sensor | `/system/sensors/temperatures/outdoor_t1` | No | No | 15 min |
| System type | Diagnostic sensor | `/system/type` | No | No | startup only |
| Support status | Diagnostic sensor | `/system/variableTariff/supportStatus` | No | No | 60 s |

The system-pressure sensor also receives the six validated limits as numeric
attributes with the unit in each attribute name:
`technical_minimum_bar`, `shutdown_pressure_bar`, `normal_minimum_bar`,
`normal_maximum_bar`, `upper_pressure_limit_bar`, and
`absolute_maximum_bar`. The derived state is `critical_low`, `low`,
`normal`, `high`, or `critical_high`; Home Assistant displays translated
states. Attributes and status are created only when all six values are numeric,
finite, non-negative, and plausibly ordered. Systems without complete pressure
range information therefore receive no invented limits.

## Heating circuits

The reference system creates 14 entities. An empty optional name produces no
entity; a configured name is added dynamically.

| Entity | HA type | PointT resource | PointT reports writable | Controllable in HA | Polling |
|---|---|---|:---:|:---:|---:|
| Active schedule | Sensor | `/heatingCircuits/{hc}/activeSwitchProgram` | Yes | No | 5 min |
| Control type | Sensor | `/heatingCircuits/{hc}/controlType` | Yes | No | 5 min |
| Desired temperature | Sensor | `/heatingCircuits/{hc}/currentRoomSetpoint` | No | No | 60 s |
| Summer/winter mode | Sensor | `/heatingCircuits/{hc}/currentSuWiMode` | No | No | 60 s |
| Heating/cooling mode | Sensor | `/heatingCircuits/{hc}/heatCoolMode` | No | No | 60 s |
| Heating system | Diagnostic sensor | `/heatingCircuits/{hc}/heatingType` | No | No | startup only |
| Manual setpoint | Sensor and number control | `/heatingCircuits/{hc}/manualRoomSetpoint` | Yes | Yes | 5 min; read back after change |
| Maximum supply temperature | Sensor | `/heatingCircuits/{hc}/maxFlowTemp` | Yes | No | 5 min |
| Name (only when configured) | Diagnostic sensor | `/heatingCircuits/{hc}/name` | Yes | No | startup only |
| Operation mode | Sensor and select | `/heatingCircuits/{hc}/operationMode` | Yes | Yes | 5 min; staggered read-back after change |
| Operating status | Sensor | `/heatingCircuits/{hc}/overallStatus` | No | No | 60 s |
| Schedule type | Sensor | `/heatingCircuits/{hc}/switchProgramMode` | Yes | No | 5 min |
| Schedule name A | Sensor | `/heatingCircuits/{hc}/switchPrograms/nameA` | Yes | No | 5 min |
| Heating | Sensor and number control | `/heatingCircuits/{hc}/temperatureLevels/comfort2` | Yes | Yes | 5 min; read back after change |
| Setback | Sensor and number control | `/heatingCircuits/{hc}/temperatureLevels/eco` | Yes | Yes | 5 min; read back after change |

The select is created only when the heating circuit currently reports the
`stringValue` type, writeability, and all three allowed raw values `off`,
`manual`, and `auto`. A change is sent exactly once by PUT and succeeds only
after a separate GET returns the same value. Because the physical K40 system
reported rapid consecutive changes with a delay, the integration performs up
to three staggered read-back checks. It never repeats the PUT. The sequence
**Manual → Auto → Manual** was confirmed.

## Domestic hot water

These 16 entities are created for every available domestic-hot-water circuit.

| Entity | HA type | PointT resource | PointT reports writable | Controllable in HA | Polling |
|---|---|---|:---:|:---:|---:|
| Actual hot-water temperature | Sensor | `/dhwCircuits/{dhw}/actualTemp` | No | No | 60 s |
| Extra hot water | Sensor and switch | `/dhwCircuits/{dhw}/charge` | Yes | Yes | 5 min; read back after change |
| Extra hot-water duration | Sensor and number control | `/dhwCircuits/{dhw}/chargeDuration` | Yes | Yes | 5 min; read back after change |
| Extra hot-water remaining time | Sensor | `/dhwCircuits/{dhw}/chargeRemainingTime` | No | No | 60 s |
| Current setpoint | Sensor | `/dhwCircuits/{dhw}/currentSetpoint` | No | No | 60 s |
| Current temperature level | Sensor | `/dhwCircuits/{dhw}/currentTemperatureLevel` | No | No | 60 s |
| Name | Diagnostic sensor | `/dhwCircuits/{dhw}/name` | Yes | No | startup only |
| Operation mode | Sensor and select | `/dhwCircuits/{dhw}/operationMode` | Yes | Yes | 5 min; read back after change |
| Operating status | Sensor | `/dhwCircuits/{dhw}/overallStatus` | No | No | 60 s |
| Reduce temperature on alarm | Sensor and switch | `/dhwCircuits/{dhw}/reduceTempOnAlarm` | Yes | Yes | 5 min; read back after change |
| Extra hot-water setpoint | Sensor and number control | `/dhwCircuits/{dhw}/singleChargeSetpoint` | Yes | Yes | 5 min; read back after change |
| Thermal disinfection | Sensor | `/dhwCircuits/{dhw}/tdMode` | No | No | 60 s |
| Eco+ start temperature | Sensor and number control | `/dhwCircuits/{dhw}/temperatureLevels/eco` | Yes | Yes | 5 min; read back after change |
| Comfort start temperature | Sensor and number control | `/dhwCircuits/{dhw}/temperatureLevels/high` | Yes | Yes | 5 min; read back after change |
| Eco start temperature | Sensor and number control | `/dhwCircuits/{dhw}/temperatureLevels/low` | Yes | Yes | 5 min; read back after change |
| Off | Sensor | `/dhwCircuits/{dhw}/temperatureLevels/off` | No | No | 15 min |

Number controls use the minimum and maximum reported by the gateway. They are
created only when both limits are complete and fall within additional safe
temperature or duration ranges. Heating-circuit setpoints use the 0.5 °C step
confirmed on the reference system. PointT reports no step for hot-water
temperatures; a physical test with 0.5 °C was not confirmed initially and later
appeared rounded to the next whole degree. All hot-water temperature controls
therefore use a 1 °C step.

The K40 also referenced stop temperatures (`highStop`, `lowStop`,
`ecoStop`) and charging deltas (`highChargingDelta`,
`lowChargingDelta`, `ecoChargingDelta`), but PointT returned HTTP 403 for
them. They cannot be exposed as Home Assistant entities even though they are
visible in the local expert menu.

## Heat sources

These 11 entities are created for every available heat source.

| Entity | HA type | PointT resource / subvalue | PointT reports writable | Controllable in HA | Polling |
|---|---|---|:---:|:---:|---:|
| Heat-pump type | Diagnostic sensor | `/heatSources/{hs}/heatPumpType` | No | No | startup only |
| Total starts | Diagnostic sensor | `/heatSources/{hs}/numberOfStarts` → `total` | No | No | 15 min |
| Heating starts | Diagnostic sensor | `/heatSources/{hs}/numberOfStarts` → `ch` | No | No | 15 min |
| Cooling starts | Diagnostic sensor | `/heatSources/{hs}/numberOfStarts` → `cooling` | No | No | 15 min |
| Hot-water starts | Diagnostic sensor | `/heatSources/{hs}/numberOfStarts` → `dhw` | No | No | 15 min |
| Condenser outlet temperature (TC3) | Diagnostic sensor | `/heatSources/{hs}/supplyFlowCondenserTemp` | No | No | 15 min |
| System type | Diagnostic sensor | `/heatSources/{hs}/type` | No | No | startup only |
| Total operating time | Diagnostic sensor | `/heatSources/{hs}/workingTime` → `total` | No | No | 15 min |
| Heating operating time | Diagnostic sensor | `/heatSources/{hs}/workingTime` → `ch` | No | No | 15 min |
| Cooling operating time | Diagnostic sensor | `/heatSources/{hs}/workingTime` → `cooling` | No | No | 15 min |
| Hot-water operating time | Diagnostic sensor | `/heatSources/{hs}/workingTime` → `dhw` | No | No | 15 min |

## Energy counters

All values are cumulative energy in kWh, not instantaneous power.

| Entity | HA type | PointT resource / subvalue | PointT reports writable | Controllable in HA | Polling |
|---|---|---|:---:|:---:|---:|
| Heating – Heat produced | Sensor | `/heatSources/emon/chConsumption` → `outputProduced` | No | No | 5 min |
| Heating – Heat-pump electricity consumption | Sensor | `/heatSources/emon/chConsumption` → `compressor` | No | No | 5 min |
| Heating – Electric auxiliary-heater consumption | Sensor | `/heatSources/emon/chConsumption` → `eheater` | No | No | 5 min |
| Heating – Electricity consumption | Sensor | `/heatSources/emon/chConsumption` → `electricity`, otherwise `compressor + eheater` | No | No | 5 min |
| Cooling – Cooling energy produced | Sensor | `/heatSources/emon/coolingConsumption` → `outputProduced` | No | No | 5 min |
| Cooling – Heat-pump electricity consumption | Sensor | `/heatSources/emon/coolingConsumption` → `compressor` | No | No | 5 min |
| Hot water – Heat produced | Sensor | `/heatSources/emon/dhwConsumption` → `outputProduced` | No | No | 5 min |
| Hot water – Heat-pump electricity consumption | Sensor | `/heatSources/emon/dhwConsumption` → `compressor` | No | No | 5 min |
| Hot water – Electric auxiliary-heater consumption | Sensor | `/heatSources/emon/dhwConsumption` → `eheater` | No | No | 5 min |
| Hot water – Electricity consumption | Sensor | `/heatSources/emon/dhwConsumption` → `electricity`, otherwise `compressor + eheater` | No | No | 5 min |
| Total – Heat produced | Sensor | `/heatSources/emon/totalConsumption` → `outputProduced` | No | No | 5 min |
| Total – Heat-pump electricity consumption | Sensor | `/heatSources/emon/totalConsumption` → `compressor` | No | No | 5 min |
| Total – Electric auxiliary-heater consumption | Sensor | `/heatSources/emon/totalConsumption` → `eheater` | No | No | 5 min |
| Total – Electricity consumption | Sensor | `/heatSources/emon/totalConsumption` → `electricity`, otherwise `compressor + eheater` | No | No | 5 min |
| Total – Environmental energy (calculated) | Sensor | `/heatSources/emon/totalConsumption` → `outputProduced - compressor - eheater` | No | No | 5 min |

Calculated environmental energy is created only when all three required raw
values are present and valid. A negative or incomplete result is not exposed
as a measurement.

## Sensitive diagnostic values

Serial number, gateway UUID, country, and sanitized system information are
created as diagnostic entities disabled by default. They appear only when the
user deliberately enables them. Internal token fields from `/system/info`
are never included. Wi-Fi data, SSIDs, IP and MAC addresses, license text,
terms of use, and additional administrative data remain completely excluded.
Container resources are used only for dynamic discovery.

The **System information** state is a short text made from the product or
module name and version, for example `K40 · Version 15.00.01`. Additional
sanitized details are attributes on the same entity. The resource is read only
at startup.
