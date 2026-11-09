# Entities and polling

<a id="new-pointt-controls-in-the-development-version"></a>

<a id="new-pointt-controls-in-beta7"></a>

## PointT controls in 0.8.2

The following combined entities are included in stable **0.8.2**. I introduced
them in **0.8.2-beta.7**.
They are not included in the published 0.8.2-beta.6 or 0.8.1 packages.
These PointT entities are separate from the additional-cloud device families
described below.

| Entity | Sources in the same discovered circuit | Supported commands |
| --- | --- | --- |
| Heating-circuit climate | `operationMode`, `manualRoomSetpoint`, `currentRoomSetpoint`, valid `roomtemperature` and known heating context | Offered Off, Heat/manual and Auto modes; manual target only in manual mode |
| PointT hot water | `actualTemp`, `currentSetpoint`, `operationMode` | Offered, understood operating modes; the active target is read-only |

The combined entities use existing polling data and remain on the same appliance
device. They can appear during polling when their required capabilities become
usable. Existing number and select entities keep their IDs and user settings;
the heating mode, manual target and hot-water mode controls move to
**Configuration**. Numeric controls use input fields instead of sliders.

The [controls dialog](heating-controls.md) groups settings and keeps edits as
a draft until Apply. Individual device-page controls write immediately.
The [PointT hot-water guide](pointt-hot-water.md) distinguishes the read-only
active target from the separate Eco, Eco+ and Comfort start temperatures.

On my reference installation, I confirmed a manual target change from 19.0 to
18.5 °C and back through the controls dialog and native climate, and Auto to Off
and back through the native hot-water entity. Both directions were checked in
MyBuderus. Other modes, circuits and appliances are not confirmed by those tests.

## Published device families and reference inventory

The Beta.6 [Midea Climate Series package](bacon-air-conditioners.md) adds one
native climate entity per identified device, with its offered mode, target,
fan and swing controls. Physical-device confirmation is outstanding.

The Beta.6 [Midea/RWH water-heater package](bacon-water-heaters.md) adds one
native water heater per identified device and optional read-only status
indicators. Physical-device checks and additional actions remain open.

The Beta.6 [Bacon foundation](bacon-connection.md) adds a separate
diagnostic connection entity for each explicitly selected additional cloud
device. Measurements and controls depend on the implemented family adapter.
Beta.6 also includes the [Air-family fans](bacon-air-purifiers.md) and
[Dry-family dehumidifiers](bacon-dehumidifiers.md); physical checks remain open.

This list documents the state observed on the reference system on August 16,
2026. The integration creates entities dynamically, so other systems may expose
more, fewer, or different entities.

`{hc}`, `{dhw}`, and `{hs}` represent the heating circuits, domestic-hot-water
circuits, and heat sources that were actually discovered. The corresponding
name group is created for every additional circuit. The entities of each
PointT heating gateway remain on one shared appliance device and use their
group as a display-name prefix. Separate cloud appliances and wallboxes keep
their own device identities.

Generated group and entity names follow the **system language** configured in
Home Assistant. For example, the same entity appears as **System – System
type** in English and with its translated name in other supported languages.
User-defined names such as **Upper floor** are not translated. Names that were
manually changed in Home Assistant also remain unchanged.

All entity and device names follow the system language, including the
readings only the local access provides. State texts (for example "Heating"
or "Off") follow the language of your user profile instead, so a profile in
another language than the system shows names and states in two languages. To
change the system language, open **Settings → System → General → Language**;
the names change after a restart of Home Assistant, the entity IDs stay.

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
`total_increasing`, except calculated environmental energy and the produced
heat or cooling, which use `total`. The environmental energy may decrease when
the source counters update at different times. The produced heat falls briefly
while the heat pump defrosts, because defrosting takes heat back from the
heating water; I observed drops of up to about 1 kWh within a few minutes on my
installation, through the cloud and the local access alike. Such a decrease
must not start a new meter cycle, and Home Assistant records it as a small
negative amount: the energy dashboard can show small negative hourly values of
the produced heat during defrosting. If a `total_increasing` counter drops to a lower
non-negative value after a reset or device replacement, Home Assistant treats
this as a new meter cycle, not negative consumption. For the `total` entities,
such a reset appears as one large negative amount in the long-term statistics
and has to be corrected there. The integration does not alter the measurement, but
records detected drops anonymously as `energy_counter_resets_detected` in
diagnostics; decreases of the produced heat during defrosting appear apart as
`produced_heat_decreases_detected`.

## Meaning of writeability columns

- **PointT reports writable** describes only the cloud resource metadata. It
  does not prove that writing is safe on every system.
- **Controllable in HA** describes the current integration behavior. A safe
  user setting is exposed as a control only when path, data type, write flag,
  and advertised enum options or numeric limits form a valid contract.
  Different devices and circuits can expose different subsets and new codes.
- The reference system reports 24 writable resources. Twenty are represented
  as readable entities. Sixteen settings have an explicitly validated Home
  Assistant control; administrative values remain locked.

## Maturity and default activation

| Maturity | Meaning | Home Assistant representation |
|---|---|---|
| `observed` | Seen in the resource tree, but its meaning is not sufficiently established | A single number, a text with declared values or a true/false value: read-only diagnostic sensor, disabled by default; anything else: no entity, redacted diagnostics only |
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
- **Disabled, diagnostic:** resources the integration does not know yet. Every
  one with a single readable value becomes a sensor named after its path: a
  number with its unit (an unknown unit is not shown), a text with declared
  values as a state, true/false as on/off. Such a sensor is never a control,
  also when the resource is writable, and needs no additional request. It
  keeps its unique ID when a later version knows the resource. A known
  resource in a shape its rules do not expect still shows no value, so its
  entity keeps its unit and statistics.
- **No entity:** Wi-Fi and network data, identifiers, license text,
  credentials, names, free text without declared values, lists, structures and
  configuration that feed other features.

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

In 0.8.1-beta.1, a valid read-only `/devices/{reported-id}/productName` or
`/heatSources/numberOfRefrigerantCircuitsInstalled` is read successfully once
per integration load, with delayed recovery after temporary failures. Existing
diagnostic sensors keep their IDs, names and enable/disable choices; new
supported sensors can appear on recovery. These resources no longer belong to
the 15/30-minute slow group after a valid response. Administrative supplementary
leaves create no entities. The complete path list, exceptions and timing are
documented in [Request behavior and recovery](request-behavior.md).

## Optional request diagnostics

The following integration-wide entities are created once per config entry and
are disabled by default. They are not assigned to an individual gateway,
because their values cover every gateway selected for that account.

| Entity | HA type | Value | Default | Additional cloud requests |
|---|---|---|:---:|:---:|
| PointT API requests total | Diagnostic sensor, state class `total` | Actual HTTP attempts since the integration started | Disabled | None |
| PointT API requests – last hour | Diagnostic sensor | Actual HTTP attempts in rolling minute buckets | Disabled | None |
| PointT API response time – last hour | Duration diagnostic sensor | Average successful response time; p95, maximum, latest, and sample count as attributes | Disabled | None |

Bulk calls, single reads, fallback reads, writes, and retries are counted by
the HTTP transport. A retry therefore adds another request. The full sanitized
recent-attempt list is available in downloaded Home Assistant diagnostics, not
as entity attributes. See [Diagnostics and request metrics](diagnostics.md).

Since 0.9.0-alpha.2 these sensors check their values once a minute and write a
new state only when a shown value changed, at most once per minute; the last
counts are written when Home Assistant stops. The observation time and the
per-category counters (by category, method, type, status and outcome) are
still shown as attributes but no longer recorded, which keeps the database
much smaller. The total request sensor starts again at zero with each start
of the integration; its `last_reset` is that start, so its statistics do not
treat the new start as a drop.

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
end dates are treated as inclusive.
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
| Software update – current progress | Diagnostic sensor | `/gateway/update/status` → `progress.cur_percent` | No | No | 15 min |
| Software update – current step | Diagnostic sensor | `/gateway/update/status` → `progress.cur_step` | No | No | 15 min |
| Software update – total steps | Diagnostic sensor | `/gateway/update/status` → `progress.nsteps` | No | No | 15 min |
| Software update – progress | Diagnostic sensor | `/gateway/update/status` → `progress.percent` | No | No | 15 min |
| Software update – status | Diagnostic sensor | `/gateway/update/status` → `status.value` | No | No | 15 min |
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
| Season switching | Select | `/system/seasonOptimizer/mode` | Capability-dependent | Yes, with advertised options and write permission | 5 min; read back after change |
| Current heating/cooling status (disabled) | Diagnostic sensor | `/system/globalSeasonOptimizer/currentMode` | No | No | 60 s |
| iSRC support (disabled) | Diagnostic sensor | `/system/iSRC/supportStatus` | No | No | startup only |
| Outdoor temperature source | Diagnostic sensor | `/system/sensors/temperatures/outdoorTemperatureSource` | No | No | 15 min |
| Outdoor temperature | Sensor | `/system/sensors/temperatures/outdoor_t1` | No | No | 15 min |
| System type | Diagnostic sensor | `/system/type` | No | No | startup only |
| Support status | Diagnostic sensor | `/system/variableTariff/supportStatus` | No | No | startup only |

The system-pressure sensor also receives the six validated limits as numeric
attributes with the unit in each attribute name:
`technical_minimum_bar`, `shutdown_pressure_bar`, `normal_minimum_bar`,
`normal_maximum_bar`, `upper_pressure_limit_bar`, and
`absolute_maximum_bar`. The derived state is `critical_low`, `low`,
`normal`, `high`, or `critical_high`; Home Assistant displays translated
states. Attributes and status are created only when all six values are numeric,
finite, non-negative, and plausibly ordered. Systems without complete pressure
range information therefore receive no invented limits.

The iSRC installation status, the iSRC support status and the season
switching shown as a read-only sensor show translated states. Device spellings such as `TIMEOUT`, `SUPPORTED` or
`forcedHeat` become the states `timeout`, `supported` and `forced_heat`; an
unknown value stays as reported. Since 0.9.0-alpha.2 the state values of these
entities are therefore different: automations and templates that check the
raw values need the new ones.

The software update status, the outdoor temperature source, the iSRC
installation status and the variable-tariff support status rarely change.
Since 0.9.0-alpha.2 the last two are read at startup only and the first two
with the long-term values (15 minutes, 30 minutes with the cloud-friendly
profile). Lists and containers
that only reveal new devices, zones, solar circuits or ventilation zones and
give no entity (for example `/devices/list` or `/zones/list`) are read hourly.

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
| Time program A (B) | Diagnostic enum sensor | `/heatingCircuits/{hc}/switchPrograms/A` | Yes | No | 15/30 min; scheduled level recalculated locally each minute |
| Heating | Sensor and number control | `/heatingCircuits/{hc}/temperatureLevels/comfort2` | Yes | Yes | 5 min; read back after change |
| Setback | Sensor and number control | `/heatingCircuits/{hc}/temperatureLevels/eco` | Yes | Yes | 5 min; read back after change |

The select is created only when the heating circuit currently reports the
`stringValue` type, writeability, and all three allowed raw values `off`,
`manual`, and `auto`. A change is sent exactly once by PUT and succeeds only
after a separate GET returns the same value. Because the physical K40 system
reported rapid consecutive changes with a delay, the integration performs up
to three staggered read-back checks. It never repeats the PUT. The sequence
**Manual → Auto → Manual** was confirmed.

The read-only schedule-type sensor recognizes all three known modes: `level`
is shown as **Temperature level**, `levels` as
**Temperature Levels**, and `absolute` as **Freely Adjustable Temperatures**.
The latter means that each schedule period can use an independently chosen
temperature. Schedule editing itself remains outside the current Home
Assistant controls.

The read-only time-program sensors show the level scheduled by program A (or B)
for the current local time. They are disabled by default. Their attributes list
the switch points per weekday as local wall-clock times (`HH:MM level`), the
total and per-day limits and the time raster reported by the device. These
calculated values do not confirm which program is selected or whether manual
operation, a holiday or another override is active. Use the separate operating
status and current-setpoint entities for the controller's reported state.

The calculation uses the gateway's time zone when it can be resolved, otherwise
Home Assistant's configured zone. It runs locally each minute, writes state
only when the level changes and makes no cloud request. At a clock change it
reevaluates the schedule for the new local time, including a skipped or repeated
hour. This calculation is covered by software tests; the controller's behavior
during clock changes has not been physically confirmed.

A program with an unexpected shape, no switch points or an unknown setpoint
reference creates no sensor. An existing sensor becomes unknown until a valid
program is read again, while its identity, custom name and activation settings
are preserved, including across a restart. New valid level keys update the enum
options during normal polling; keys without a translation are displayed as
reported. On the reference WLW186i, heating allowed two and hot water six switch
points per day in a 15-minute raster.

Other enum-like diagnostic values are normalized from their exact PointT
spellings. This includes heating and
cooling control types, hot water following the heating schedule, heat-pump and
heat-source variants, system layouts, energy-management activity, and the PV
contact state. Unknown future values remain safely unavailable until their
meaning is known.

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
| Time program A | Diagnostic enum sensor | `/dhwCircuits/{dhw}/switchPrograms/A` | Yes | No | 15/30 min; scheduled level recalculated locally each minute |

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
| Pool-heating starts | Diagnostic sensor | `/heatSources/{hs}/numberOfStarts` → `pool` | No | No | 15 min |
| Condenser outlet temperature (TC3) | Diagnostic sensor | `/heatSources/{hs}/supplyFlowCondenserTemp` | No | No | 15 min |
| System type | Diagnostic sensor | `/heatSources/{hs}/type` | No | No | startup only |
| Total operating time | Diagnostic sensor | `/heatSources/{hs}/workingTime` → `total` | No | No | 15 min |
| Heating operating time | Diagnostic sensor | `/heatSources/{hs}/workingTime` → `ch` | No | No | 15 min |
| Cooling operating time | Diagnostic sensor | `/heatSources/{hs}/workingTime` → `cooling` | No | No | 15 min |
| Hot-water operating time | Diagnostic sensor | `/heatSources/{hs}/workingTime` → `dhw` | No | No | 15 min |

Start-counter entities are registered even when the discovered resource has an
empty or partial startup payload. Pool-heating starts are always created, but
disabled by default, because not every system heats a pool; enable them if
yours does. They are named **Anzahl der Starts Poolheizung** in German and **Number of
starts Pool heating** in English. Find them under **Settings → Devices &
services → Entities**, filtered by this integration. The value stays unknown
until the gateway supplies `pool`. It uses the existing 15-minute request,
with no additional polling.

The brine names use the heating-system perspective: `collectorInflowTemp` is
the fluid leaving the heat pump for the ground collector, while
`collectorOutflowTemp` is the fluid returning to the heat pump. Per-source
paths are probed only for IDs actually reported below `/heatSources`.

## Readings only the local access provides

Every reading the local access offers becomes an entity. Readings that are not
needed in everyday operation are only disabled by default, never left out:
other systems may report values where the reference system shows none.
Identifiers, addresses, network and privacy data are never entities, nor are
structures without a single value that feed other features (for example the
zone configuration or the device lists).

With a local access, some readings exist only locally on many systems: the
cloud does not offer them, but the local access reads them anyway with its
polling plan, so they cause no additional requests. They become their own
sensors, but only for paths the local access of this system could actually
read and only when the cloud offers no resource for the same path. They stay
unavailable without a current local reading; there is no cloud value to fall
back to. A device state such as an open sensor or too little flow is shown as
unknown, never as a number. A reading the local access reports as a number is
offered for that reason, also while it reports an error marker; its value is
then unknown. The same rules apply with and without the cloud. The setting **Use local values for existing
entities** does not affect them. If the cloud offers the same path later (for
example after a new discovery), its cloud entity takes over. When the cloud's
sensors are set up (at a start, a reload or when a system joins its cloud),
before any of them is added, the local-only entity continues as the cloud's
sensor, with its entity ID, name, area, recorded states and statistics, if
exactly one cloud sensor exists for that path, it is not registered yet, the
native unit of the last local reading equals the cloud's (a display unit chosen
in Home Assistant does not matter), the state class stays the same or the
local-only entity had none, and the local-only entity was not disabled by the
integration. A local-only entity that never showed a reading is not continued.
Otherwise, and for a path the cloud starts offering while Home Assistant runs,
the local-only entity is removed as before. For this comparison a local-only
sensor keeps the native unit of its last reading in its entity settings; the
entry drops it when the cloud's sensor continues it. After the local access is removed, the entities are
unavailable and are removed at the next reload or restart; adding the access
again before that keeps their settings. They belong to the system's cloud
access: while that access is stopped (for example waiting for a new sign-in),
they are not provided either, except while the system's entities come from the
stored inventory or from its local readings alone (see
[when the cloud stops](source-selection.md#when-the-cloud-stops)).

| Enabled | Readings |
| --- | --- |
| yes | current supply setpoint, volume flow (L/h), compressor and electric heater power (W), refrigerant circuit status, heating circuit pump, thermal disinfection running, silent mode, utility power block |
| no | cylinder temperature (the same value as the hot water temperature), hot gas, suction gas, high- and low-pressure, liquid line (heating and cooling), compressor and evaporator temperatures; outdoor fan, heat carrier pump and compressor speed (%); additional heater flow temperature; a second compressor power reading and a coarse electric heater power; ventilation readings: exhaust, extract, outdoor and supply air temperatures, indoor, outdoor and highest humidity (%), indoor, outdoor and highest air quality (ppm, no device class), exhaust and supply fan power, summer bypass flap and auxiliary heater power (%), exhaust fan speed (rpm), supply fan speed (no device class or statistics), run time and filter remaining time (min, no statistics) and the exhaust fan level |
| no, diagnostic | the controller's other internal signals (`/signals/…`) with numeric readings: unit taken from the reading, long-term statistics when that unit is one of a measurement (not for run and remaining times), meaning not documented in detail. Identifiers, addresses or keys (for example the energy-management link ID) and free text are never offered. |

The controller's signals are offered as follows. Signals reported as true or
false, also as declared TRUE/FALSE states, are on/off states (`on`/`off`, not
`true`/`false`, since 0.9.0-alpha.2); compressor, hot water valve, four-way valve, electric
heater stages 1 to 3, drip pan and crankcase heaters, heating pump, cooling,
low-noise mode, low heating volume flow and an overheated inverter are enabled.
Signals that declare named states without a unit are states; season mode,
heating/cooling state and smart grid mode are enabled, the holiday slots and
other states disabled. A signal with a unit stays a number: its named marker
states (for example an open sensor) show no value. Damped outdoor temperature,
drip pan temperature, heat output (compressor), expansion valve opening, the
compressor run times for heating, hot water and cooling (shown in hours, no
long-term statistics) and the heating circuit 1 volume flow are enabled. An
**Installation date** sensor is derived from the three installation date
signals (year counted from 2000); the parts stay available, disabled. External
inputs and the room controller assignment are disabled states.

A volume flow too low for its sensor to measure is reported by the device as
its declared low-flow state (for example `lowflow` with the value 65535). The
heat pump volume flow and the heating circuit 1 volume flow then show
"unknown" with the attribute `measuring_range: below` (shown as "Below
measuring range"), so such a flow can be told apart from a missing reading;
a measured flow has no such attribute. A volume flow read through the cloud
access is treated the same way when its reading declares the low-flow state:
"unknown" instead of 65535, with the same attribute.

The two setpoints the controller keeps for an energy manager
(`SRC.CUHP.EM.UsedLowerFlowTempSetp` and `SRC.CUHP.EM.UsedUpperFlowTempSetp`)
show "unknown" while the energy management status reports "not connected":
the controller then reports 0 °C as a placeholder, not a setpoint. The status
counts as its own entity shows it (the local reading first, else a still valid
cloud reading); without a current status the setpoints are shown as reported. The
entities stay; with a connected energy manager they show the reported
setpoints. The hot water "off" value is not affected.

When an existing entity of a signal gets its reviewed form, it keeps its
unique ID and entity ID; its device class, unit or state class may change. A
run time that was shown in seconds keeps that unit (choose hours in the entity
settings if you like); new ones start in hours.

The signal formerly named "Compressor power" is called "Heat output
(compressor)" since 0.9.0-alpha.2: its value matches the heat meter, so it is
the heat output, not the electric power. Its entity ID stays; a name you set
yourself stays as well.

Hybrid systems with an HM200 hybrid manager report additional readings of the
outdoor unit and the hybrid manager through the local access: temperatures of
the outdoor unit, heat pump supply and return, compressor and pump speeds,
valve position, compressor timers and run times, the smart grid mode (blocked,
normal, preferred, forced) and on/off states such as defrosting or the
four-way valve. They are disabled by default and have not been verified on a
real system. The unit and states the gateway reports take precedence; a
reading that is either a temperature or an on/off state becomes one entity,
decided by its answer. The run times have no long-term statistics, like the
other local run times. They are shown on the existing heating device.

The refrigerant circuit status also shows the states the gateway itself
declares (for example off, hot water, pool and pool heating), so it no longer
reads "unknown" while the heat pump is idle. Other local states also accept
the values their gateway declares.

Compare a few values with the heat pump display before relying on them; the
meaning of the technical readings follows the official local API
documentation.

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

### Energy estimated from current power

Some appliances report a heat source's current power
(`/heatSources/{hs}/actualPower`, kW) but no consumption or energy counter.
For each such heat source, and only when neither the heat source nor the
plant has a native counter (`…Consumption` or `emon/…`), the integration
creates **Energy (estimated from current power)**. It adds up the reported
power over time (a left Riemann sum: each reading counts until the next one)
and reports kWh with the state class `total_increasing`. The attributes state
that the value is an estimate, the method, the polling interval and the
longest gap that is still counted.

It is an estimate, not a meter reading:

- Its accuracy depends on the polling interval; short changes between two
  readings are not seen.
- Two readings are only combined when they are at most three polling
  intervals and at most 30 minutes apart. Across a longer gap, an unavailable
  or expired reading, an error value, negative power, or a change between the
  local and the cloud reading, nothing is counted; the total stays where it
  was and counting starts again with the next reading.
- It is not confirmed whether the reported power is the fuel input (for
  example gas) or the delivered heat. The name therefore says neither.
- The total never decreases and survives restarts; the first reading after a
  restart only sets the new starting point.

The entity is disabled by default and counts only from the moment it is
enabled; earlier periods cannot be filled in. To use it in the energy
dashboard, enable it, wait for the first statistics (up to one hour), and add
it, for example, under gas consumption, which accepts kWh. I have not yet
confirmed this entity on a real device.

## Optional capabilities

The following read-only families are probed from a bounded path catalog. They
are not assumed to exist. A missing path,
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
| Pool | `/pool/currentTemp`, `setpointTemp`, `enabled`, `additionalHeater/poolMode` | Temperature, target, enabled state, and auxiliary-heater mode; a target number (°C, reported limits and step) and an on/off switch when the cloud reports them writable (the switch only for exactly `on`/`off`) | 60 s to 15 min; read-back after a change |
| Ventilation | `/ventilation/operationModes/manual/fanSetpoint`, `/ventilation/{zone}/*` | Fan levels, filter time, air-quality/humidity limits, mode, and supply temperature; an exhaust fan level select when the cloud reports it writable and offers only the documented levels `off`, `min`, `red`, `nom`, `max`, `dem` | 60 s to 15 min; read-back after a change |
| Room zones | `/zones/{zone}/*` | Average temperature/humidity, target, child lock, heating/cooling mode and temporary setpoint | 60 s to 15 min |
| Room devices | `/devices/{device}/*` | Room temperature/humidity, battery, connection state, errors, and static device information | 60 s to startup only |
| Photovoltaics | `/pv/enable`, `/pv/surplusAvailable` | Enabled and surplus states | 60 s to 15 min |

Summer bypass activation, the ventilation operating mode and the pool mode
are not offered as controls.

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
