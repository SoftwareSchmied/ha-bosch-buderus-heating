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

The live comparison on August 16, 2026 processed all 94 reference-advertised
readable resources without parser errors. The final entity count is dynamic
and also includes any available documented status paths that the gateway does
not advertise in that tree.
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
  as readable entities. Sixteen settings have an explicitly validated Home
  Assistant control; administrative values remain locked.

## Maturity and default activation

| Maturity | Meaning | Home Assistant representation |
|---|---|---|
| `observed` | Seen in the resource tree, but its meaning is not sufficiently established | No entity; redacted diagnostics only |
| `understood` | Type and functional meaning established | Entity possible; default activation follows the user relevance described below |
| `verified` | Sufficiently confirmed for normal display | Read-only entity, enabled by default |
| `write_verified` | Write and read-back confirmed on real equipment | Control entity; default activation follows user relevance |

Heating-circuit operation mode, Silent Mode, auxiliary-heater operation mode,
and maximum supply temperature have been write-confirmed on the reference
system. Other released controls use the same tested write/read-back service and
are additionally constrained by current gateway metadata. Their individual
effects have not all been tested on physical equipment.

Default activation is defined explicitly:

- **Enabled:** temperatures and operating states, all available energy
  counters, starts, operating hours, TC3, system pressure, the derived pressure
  status, and released everyday switches, number controls, and selects.
- **Disabled:** serial number, gateway UUID, country, detailed system
  information, individual raw software-update fields, technical device and
  configuration values, the installer-level maximum-supply-temperature
  control, and read-only sensors whose value is already represented by a
  control.
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
| Live values | 60 seconds | Dynamic operating states and measurements |
| Notifications | 5 minutes normally; 60 seconds while active | 4 aggregate entities when supported |
| Settings | 5 minutes | 19 entities |
| Energy | 5 minutes | up to 15 entities |
| Long-term values | 15 minutes | Starts, operating times, and slow measurements |
| Static | at startup only | 20 known possible entities |

Due groups are combined into batch requests of no more than 30 resources.
HTTP 429 pauses further requests temporarily. Successful partial values and
the most recent valid state are retained during partial failures.

## Optional request diagnostics

The following integration-wide entities are created once per config entry and
are disabled by default. They are not assigned to an individual gateway,
because their values cover every gateway selected for that account.

| Entity | HA type | Value | Default | Additional cloud requests |
|---|---|---|:---:|:---:|
| PointT API requests total | Diagnostic sensor | Actual HTTP attempts since the integration started | Disabled | None |
| PointT API requests – last hour | Diagnostic sensor | Actual HTTP attempts in rolling minute buckets | Disabled | None |
| PointT API response time – last hour | Duration diagnostic sensor | Average successful response time; p95, maximum, latest, and sample count as attributes | Disabled | None |

Bulk calls, single reads, fallback reads, writes, and retries are counted by
the HTTP transport. A retry therefore adds another request. The full sanitized
recent-attempt list is available in downloaded Home Assistant diagnostics, not
as entity attributes. See [Diagnostics and request metrics](diagnostics.md).

## Faults and notifications

Fault entities are created once per gateway and become available when at least
one supported PointT current-notification source is found. They summarize the
active list and do not create an entity for every possible manufacturer code.

| Entity | HA type | Source | Default | Polling |
|---|---|---|:---:|---:|
| System fault | Problem binary sensor | `/notifications` and supported component fault resources | Enabled | 5 min; 60 s while active |
| Active faults | Sensor | Normalized active fault list | Enabled | 5 min; 60 s while active |
| Active notifications | Sensor | All normalized current notifications | Enabled | 5 min; 60 s while active |
| System notifications | Event entity | `appeared` and `resolved` lifecycle transitions | Enabled | event driven from each poll |

Warnings and maintenance messages contribute to **Active notifications** but
do not by themselves activate **System fault**. Unknown classifications are
treated conservatively as faults. A missing entry is considered resolved only
after two complete successful reads; a timeout, rate limit, malformed entry,
or partial batch response never clears an existing fault.

The integration persists only the normalized active baseline needed to avoid
duplicate events after a Home Assistant restart. No raw cloud response is
stored. Timestamps marked `home_assistant_observed` are observation times and
must not be confused with the exact start or end time shown by the appliance.

## Holiday periods

Holiday support is optional and is created only when the gateway returns at
least one of the documented PointT resources. It is deliberately separate from
`/system/awayMode/enabled`.

| Entity | HA type | PointT resource | PointT reports writable | Controllable in HA | Polling |
|---|---|---|:---:|:---:|---:|
| Holiday mode active | Binary sensor | `/holidayMode/activeModes`, with validated-period fallback | No assumption | No | 5 min; 10 min with cloud-friendly profile |
| Next holiday | Timestamp sensor | `/holidayMode/list`, `/holidayMode/configuration` | No assumption | No | 5 min; 10 min with cloud-friendly profile |
| Holiday periods | Calendar | `/holidayMode/list`, `/holidayMode/configuration`; writes use `/holidayMode[/<id>]` | Capability-dependent | Create, edit, delete when safely advertised | 5 min; 10 min with cloud-friendly profile; immediate read-back after writes |

The calendar supports multiple periods. PointT timestamps retain their offset;
naive timestamps use the gateway time zone where available and otherwise the
Home Assistant time zone. Invalid or incomplete periods are ignored
individually, so one malformed entry cannot hide the valid entries. Date-only
end dates are treated as inclusive, matching the app wording.
The **Next holiday** sensor shows the start of the current or next period on
the device page. Its attributes contain the end, whether the period is
currently active, and whether it is an all-day period.

Create, edit, and delete actions appear only when the list and configuration
resources are current and the schema is fully understood. Editing in the
calendar preserves all PointT behavior fields and changes only dates and the
supported cloud name. New periods use the official-app defaults for affected
circuits and holiday behavior. Recurrence, descriptions, and locations are not
supported.

The integration's **Configure** dialog edits the PointT-specific fields that
the standard Home Assistant calendar cannot display: circuit assignments,
heating mode, hot-water mode, ventilation mode, thermal disinfection, and the
constant temperature. Both fields and choices are derived dynamically
from `/holidayMode/configuration`; unsupported choices are omitted. Every
mutation is sent once and requires a confirmed list read-back. Home Assistant
diagnostics contain only resource support, write availability, and parser
counters—not holiday dates, names, or raw configuration data.

## Gateway and system

| Entity | HA type | PointT resource / subvalue | PointT reports writable | Controllable in HA | Polling |
|---|---|---|:---:|:---:|---:|
| Brand | Diagnostic sensor | `/gateway/brand` | No | No | startup only |
| Serial number (disabled) | Diagnostic sensor | `/gateway/serialId` | No | No | startup only |
| Gateway UUID (disabled) | Diagnostic sensor | `/gateway/uuid` | No | No | startup only |
| Date and time | Diagnostic sensor | `/gateway/dateTime` | No | No | 15 min |
| Software prefix | Diagnostic sensor | `/gateway/swPrefix` | No | No | startup only |
| Data processing status (disabled) | Diagnostic sensor | `/gateway/dataProcessing/status` | No | No | startup only |
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
| Central heating status | Diagnostic sensor | `/heatSources/chStatus` | No | No | 60 s |
| Compressor status | Sensor | `/heatSources/compressor/status` | No | No | 60 s |
| Auxiliary-heater status | Sensor | `/heatSources/Source/eHeater/status` | No | No | 60 s |
| Primary auxiliary-heater status | Sensor | `/heatSources/additionalHeater/primary/status` | No | No | 60 s |
| Auxiliary-heater operation mode | Diagnostic sensor (disabled) and select | `/heatSources/additionalHeater/operationMode` | Yes | Yes | 5 min; staggered read-back after change |
| Primary auxiliary-heater type (disabled) | Diagnostic sensor | `/heatSources/additionalHeater/primary/type` | No | No | startup only |
| Emergency mode | Sensor | `/heatSources/currentEmergencyMode` | No | No | 60 s |
| Supply temperature | Sensor | `/heatSources/actualSupplyTemperature` | No | No | 60 s |
| Energy management status | Sensor | `/heatSources/emStatus` | No | No | 60 s |
| Flame status | Sensor | `/heatSources/flameStatus` | No | No | 60 s |
| Return temperature | Sensor | `/heatSources/returnTemperature` | No | No | 60 s |
| Passive-cooling inlet temperature | Sensor | `/heatSources/passiveCooling/inflowTemp` | No | No | 60 s |
| PV contact status | Sensor | `/heatSources/pvContactState` | No | No | 60 s |
| Smart Function active | Sensor or binary sensor | `/heatSources/smartFunction/active` | No | No | 60 s |
| Smart Function enabled (disabled) | Diagnostic sensor or binary sensor | `/heatSources/smartFunction/enabled` | Capability-dependent | No | 5 min |
| Standby mode | Sensor | `/heatSources/standbyMode` | No | No | 60 s |
| System pressure | Sensor | `/heatSources/systemPressure` | No | No | 60 s |
| System pressure status (calculated) | Derived status sensor | System pressure and `/heatSources/systemPressureRange` | No | No | with system pressure |
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
| Season optimization (disabled) | Diagnostic sensor | `/system/globalSeasonOptimizer/currentMode` | No | No | 60 s |
| iSRC support (disabled) | Diagnostic sensor | `/system/iSRC/supportStatus` | No | No | startup only |
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

Some K40 gateways serve the status paths above without listing them in the
PointT reference tree. The integration probes only this bounded, documented
set during discovery. Unsupported paths are ignored and are not added to
recurring polling. Known array and multipart entities are defined from their
stable schema, so an empty startup value does not make an entity disappear for
the entire Home Assistant session.

## Heating circuits

The reference system creates 14 core entities. Optional app capabilities add
entities only when the gateway returns them. An empty optional name produces
no entity; a configured name is added dynamically.

| Entity | HA type | PointT resource | PointT reports writable | Controllable in HA | Polling |
|---|---|---|:---:|:---:|---:|
| Active schedule | Sensor | `/heatingCircuits/{hc}/activeSwitchProgram` | Yes | No | 5 min |
| Control type | Sensor | `/heatingCircuits/{hc}/controlType` | Yes | No | 5 min |
| Desired temperature | Sensor | `/heatingCircuits/{hc}/currentRoomSetpoint` | No | No | 60 s |
| Current heating/cooling status | Sensor | `/heatingCircuits/{hc}/currentSuWiMode` | No | No | 60 s |
| Heating/cooling switching | Sensor | `/heatingCircuits/{hc}/suWiSwitchMode` | No | No | 60 s |
| Heating/cooling support | Sensor | `/heatingCircuits/{hc}/heatCoolMode` | No | No | 60 s |
| Heating system | Diagnostic sensor | `/heatingCircuits/{hc}/heatingType` | No | No | startup only |
| Dew point (calculated) | Derived temperature sensor | `roomtemperature` + `actualHumidity` | No | No | with both 60 s inputs |
| Manual setpoint | Sensor and number control | `/heatingCircuits/{hc}/manualRoomSetpoint` | Yes | Yes | 5 min; read back after change |
| Maximum supply temperature | Sensor (disabled) and number control (disabled) | `/heatingCircuits/{hc}/maxFlowTemp` | Yes | Yes | 5 min; staggered read-back after change |
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

The calculated dew point is created only when the same heating circuit exposes
both `/heatingCircuits/{hc}/roomtemperature` in °C and
`/heatingCircuits/{hc}/actualHumidity` in percent. It uses the Magnus formula
with `a = 17.62` and `b = 243.12 °C`, causes no additional cloud requests,
and becomes unavailable when either source is unavailable. Its attributes show
the two source measurements and formula constants. It is not the controller's
cooling-flow setpoint and does not apply an unknown installer safety offset.

## Domestic hot water

These 16 core entities are created for every available domestic-hot-water
circuit. Optional fresh-water-station and service values are added only when
reported.

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

The maximum-supply-temperature control adopts the minimum and maximum reported
by each individual gateway instead of assuming the 30–60 °C range observed on
the reference system. A broad 0–100 °C plausibility envelope only rejects
obviously corrupt metadata. The control uses whole-degree steps and is disabled
by default because it is an installer-level limit rather than an everyday
setpoint.

The K40 also referenced stop temperatures (`highStop`, `lowStop`,
`ecoStop`) and charging deltas (`highChargingDelta`,
`lowChargingDelta`, `ecoChargingDelta`), but PointT returned HTTP 403 for
them. They cannot be exposed as Home Assistant entities even though they are
visible in the local expert menu.

## Heat sources

The core counters and information are created for every available heat source.
Optional values are omitted when the corresponding PointT path is unavailable
or its live schema does not match the documented type and unit.

| Entity | HA type | PointT resource / subvalue | PointT reports writable | Controllable in HA | Polling |
|---|---|---|:---:|:---:|---:|
| Heat-pump type | Diagnostic sensor | `/heatSources/{hs}/heatPumpType` | No | No | startup only |
| Current power | Sensor | `/heatSources/{hs}/actualPower` | No | No | 60 s |
| Current power percentage | Sensor | `/heatSources/{hs}/powerPercentage` | No | No | 60 s |
| Defrost active | Binary sensor or status sensor | `/heatSources/{hs}/defrostActive` | No | No | 60 s |
| Brine outlet temperature | Sensor | `/heatSources/{hs}/brineCircuit/collectorInflowTemp` | No | No | 60 s |
| Brine inlet temperature | Sensor | `/heatSources/{hs}/brineCircuit/collectorOutflowTemp` | No | No | 60 s |
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

The brine names use the heating-system perspective: `collectorInflowTemp` is
the fluid leaving the heat pump for the ground collector, while
`collectorOutflowTemp` is the fluid returning to the heat pump. Per-source
paths are probed only for IDs actually reported below `/heatSources`.

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

The total-electricity entity reports `value_source: direct` when PointT
provides `electricity`. When it falls back to the complete sum of `compressor`
and `eheater`, it reports `value_source: calculated` and documents the formula
in its `calculation` attribute. Its entity name remains stable because the
source may change between responses.

## Optional app capabilities

The following read-only families are probed from the path catalog embedded in
MyBuderus and HomeCom Easy. They are not assumed to exist. A missing path,
HTTP 403/404 response, or incompatible value type creates no entity and no
recurring request. New fields remain diagnostic until their semantics have
been confirmed on physical equipment.

| Area | PointT resources | Typical entities | Polling |
|---|---|---|---:|
| Heating circuit | `/heatingCircuits/{hc}/actualHumidity`, `actualSupplyTemperature`, `roomtemperature`, `awayTemperature` | Humidity and temperature sensors | 60 s |
| Heating-circuit boost | `boostMode`, `boostDuration`, `boostRemainingTime`, `boostTemperature` | Boost state, duration, remaining time, and temperature | 60 s or 5 min when writable |
| Heating-circuit cooling | `cooling/controlType`, `operationMode`, `outdoorThreshold`, `roomTempSetpoint`, `manualRoomSetpoint`, `temporaryRoomSetpoint`, `temperatureLevels/on` | Cooling status, thresholds, and setpoints | 60 s or 5 min when writable |
| Heating-circuit details | `openWindowDetection/enabled`, `openWindowDetection/status`, `operationSetpoints`, `pumpModulation`, `setpointOptimization`, `suWiThreshold`, `suWiCoolingThreshold`, `temporaryRoomSetpoint` | Status, modulation, thresholds, and structured scalar values | 60 s to 15 min |
| Domestic hot water | `/dhwCircuits/{dhw}/manualsetpoint`, `operationSetpoints`, `learningWeek`, `safetyTemperature`, `waterTotalConsumption` | Setpoints, configuration, and consumption | 5 to 15 min; consumption 5 min |
| Fresh-water station | `currentFriwaSupplyTemperature`, `friwaPrimaryPumpModulation`, `inletTemperature`, `outletTemperature`, `outTemp`, `volumeFlow`, `numberOfShowersAvailable` | Temperatures, pump output, flow, and available showers | 60 s |
| Hot-water service sensors | `/dhwCircuits/{dhw}/sensor/{name}` | Air-box, flue-gas, tank, and heat-exchanger temperatures; pressure, fan, gas, and water flow | 60 s to 15 min |
| Heat source | `/heatSources/{hs}/electricityTotalConsumption`, `operationHours`, `emon/totalConsumption` | Electricity, operating hours, and per-source energy counters | 5 to 15 min |
| Hybrid system | `/heatSources/hybrid/*` | Active source, bivalence point, strategy, outdoor state, variant, and reminder state | 60 s to 15 min |
| Pool via heat source | `/heatSources/poolTemperature`, `poolSetpointTemperature`, `poolStatus` | Pool temperature, target, and state | 60 s |
| System | `/system/healthStatus`, `appliance/*`, `iSRC/installationStatus`, `sensors/temperatures/*` | Health, equipment metadata, installation status, and temperatures | 60 s or startup only |
| Noise and power | `/system/lowNoise/*`, `silentMode/*`, `powerGuard/active`, `powerLimitation/active` | Operating state, configured time/duration, power reduction, and a capability-gated Silent Mode select | 60 s to 15 min; read-back after a Silent Mode change |
| Variable tariff | `/system/variableTariff/ch/*`, `dhw/*`, `currentPriceCatagorization`, `priceInfo`, `tariffId` | Current category, optimization state, price-dependent setpoints, and status | 60 s to 15 min |
| Solar | `/solarCircuits/{sc}/collectorTemperature`, `dhwTankBottomTemperature`, `maxCylinderTemperature`, `maxTemperatureReached`, `pumpModulation`, `solarYield` | Temperatures, pump output, limit state, and yield | 60 s; yield 5 min |
| Pool | `/pool/currentTemp`, `setpointTemp`, `enabled`, `additionalHeater/poolMode` | Temperature, target, enabled state, and auxiliary-heater mode | 60 s to 15 min |
| Ventilation | `/ventilation/operationModes/manual/fanSetpoint`, `/ventilation/{zone}/*` | Fan levels, filter time, air-quality/humidity limits, mode, and supply temperature | 60 s to 15 min |
| Room zones | `/zones/{zone}/*` | Average temperature/humidity, target, child lock, heating/cooling mode and temporary setpoint | 60 s to 15 min |
| Room devices | `/devices/{device}/*` | Room temperature/humidity, battery, connection state, errors, and static device information | 60 s to startup only |
| Photovoltaics | `/pv/enable`, `/pv/surplusAvailable` | Enabled and surplus states | 60 s to 15 min |

Known percentage resources receive a semantic Home Assistant device class only
when both their exact PointT path and `%` unit match. This covers heating-circuit,
zone, room-device, and ventilation humidity values as well as room-device battery
levels. Pump modulation, fan output, radio signal, update progress, and power
reduction remain generic percentages because they represent different physical
or technical meanings. Bosch and Buderus use the same PointT paths; an entity is
still created only when the connected installation actually exposes the path.

The catalog does not automatically turn a newly discovered writable field into
a Home Assistant control. Controls require a separate allowlist, safe limits,
an exact value mapping, and a successful write/read-back test.

Three optional resources are released as controls after their live contracts
were confirmed on the K40 reference system. Silent Mode requires the complete
`off`, `auto`, `on` value set. Auxiliary-heater operation mode requires
`off`, `manual`, `auto`. Maximum supply temperature requires complete numeric
PointT limits within a broad 0–100 °C plausibility envelope. Matching read-only sensors remain available as disabled
diagnostic entities; the maximum-supply-temperature number is also disabled by
default because it changes an installer-level limit.

Historical `/recordings/...` resources are deliberately not represented by
ordinary state entities. Home Assistant already records the corresponding live
states; importing vendor history requires a separate opt-in design with date
ranges, deduplication, statistics semantics, and a strict request budget.

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
