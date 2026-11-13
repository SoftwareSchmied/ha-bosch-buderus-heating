# Resource contracts and implementation inventory

I reconciled the previous 359 definitions with all 361 model rows for P03.
The two additional entries are generic execution models that receive a path
from their caller. They are not two newly discovered devices or entities.

I preserve each model row and its previous path. Explicit dynamic arguments,
fixed model paths, query arguments and corrected compositions are recorded
separately in [the machine-readable inventory](resource-inventory.json).
Duplicate model paths remain linked to their original rows.

Each record describes transport, path, response shape, unit policy, scaling,
options, bounds, write checks, time basis, evidence and missing device data.
Synthetic schema probes check catalog consistency. They do not establish
the correctness of all possible values, units or device variants.
The family fixtures contain separately chosen semantic examples.

| Disposition | Model definitions |
|---|---:|
| directory | 28 |
| excluded | 34 |
| history | 11 |
| missing_contract | 45 |
| separate_package | 2 |
| supported_control | 4 |
| supported_read | 233 |
| supported_transaction | 4 |

The [P03 report](resource-contracts.md) describes duration corrections,
collection handling, discovery scenarios and the remaining evidence needs.
The [fixture guide](../tests/fixtures/contracts/README.md) explains provenance.
No real device capture is claimed for these new examples.

| ID | Family | Path template | Disposition | Follow-up |
|---|---|---|---|---|
| R001 | connectkey | `/gateway/update/triggerRequest` | excluded | device confirmation as applicable |
| R002 | connectkey | `/devices/device{controllerNumber}/actualHumidity` | supported_read | device confirmation as applicable |
| R003 | connectkey | `/devices/list` | directory | device confirmation as applicable |
| R004 | connectkey | `/devices/device{controllerNumber}/errors` | missing_contract | P03 |
| R005 | connectkey | `/devices/inclusionTime` | directory | device confirmation as applicable |
| R006 | connectkey | `/devices/inclusionWhitelist` | directory | device confirmation as applicable |
| R007 | connectkey | `/devices/device{controllerNumber}/rfTimeofConnectionLost` | supported_read | device confirmation as applicable |
| R008 | connectkey | `/devices/device{controllerNumber}/roomtemperature` | supported_read | device confirmation as applicable |
| R009 | connectkey | `/devices/device{controllerNumber}/versionFirmware` | supported_read | device confirmation as applicable |
| R010 | connectkey | `/dhwCircuits/dhw1/reduceTempOnAlarm` | supported_read | device confirmation as applicable |
| R011 | connectkey | `/dhwCircuits/dhw{circuitNumber}/actualTemp` | supported_read | device confirmation as applicable |
| R012 | connectkey | `/dhwCircuits/dhw{circuitNumber}/charge` | supported_read | device confirmation as applicable |
| R013 | connectkey | `/dhwCircuits/dhw{circuitNumber}/chargeDuration` | supported_read | device confirmation as applicable |
| R014 | connectkey | `/dhwCircuits/dhw{circuitNumber}/chargeRemainingTime` | supported_read | device confirmation as applicable |
| R015 | connectkey | `/dhwCircuits/dhw1/currentFriwaSupplyTemperature` | supported_read | device confirmation as applicable |
| R016 | connectkey | `/dhwCircuits/dhw{circuitNumber}/currentSetpoint` | supported_read | device confirmation as applicable |
| R017 | connectkey | `/dhwCircuits/dhw{circuitNumber}/currentTemperatureLevel` | supported_read | device confirmation as applicable |
| R018 | connectkey | `/dhwCircuits/dhw1/friwaPrimaryPumpModulation` | supported_read | device confirmation as applicable |
| R019 | connectkey | `/dhwCircuits/dhw{circuitNumber}/name` | supported_read | device confirmation as applicable |
| R020 | connectkey | `/dhwCircuits/dhw{circuitNumber}/operationMode` | supported_read | device confirmation as applicable |
| R021 | connectkey | `/dhwCircuits/dhw{circuitNumber}/outTemp` | supported_read | device confirmation as applicable |
| R022 | connectkey | `/dhwCircuits/dhw{circuitNumber}/singleChargeSetpoint` | supported_read | device confirmation as applicable |
| R023 | connectkey | `/dhwCircuits/dhw{circuitNumber}/switchPrograms/A` | supported_read | read only; other models and editing |
| R024 | connectkey | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels` | missing_contract | P03 |
| R025 | connectkey | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels/eco` | supported_read | device confirmation as applicable |
| R026 | connectkey | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels/high` | supported_read | device confirmation as applicable |
| R027 | connectkey | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels/low` | supported_read | device confirmation as applicable |
| R028 | connectkey | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels/off` | supported_read | device confirmation as applicable |
| R029 | connectkey | `/dhwCircuits/dhw{circuitNumber}/dhwType` | supported_read | device confirmation as applicable |
| R030 | connectkey | `/dhwCircuits/dhw{circuitNumber}/volumeFlow` | supported_read | device confirmation as applicable |
| R031 | connectkey | `/dhwCircuits/dhw{circuitNumber}/overallStatus` | supported_read | device confirmation as applicable |
| R032 | connectkey | `/gateway/dataProcessing/status` | supported_read | device confirmation as applicable |
| R033 | connectkey | `/gateway/dateTime` | excluded | device confirmation as applicable |
| R034 | connectkey | `/gateway/eth/ip/ipv4` | excluded | device confirmation as applicable |
| R035 | connectkey | `/gateway/eth/ip/ipv6` | excluded | device confirmation as applicable |
| R036 | connectkey | `/gateway/eth/mac` | excluded | device confirmation as applicable |
| R037 | connectkey | `/gateway/serialId` | excluded | device confirmation as applicable |
| R038 | connectkey | `/gateway/uuid` | excluded | device confirmation as applicable |
| R039 | connectkey | `/gateway/versionFirmware` | supported_read | device confirmation as applicable |
| R040 | connectkey | `/gateway/versionHardware` | supported_read | device confirmation as applicable |
| R041 | connectkey | `/gateway/wifi/ip/ipv4` | excluded | device confirmation as applicable |
| R042 | connectkey | `/gateway/wifi/ip/ipv6` | excluded | device confirmation as applicable |
| R043 | connectkey | `/gateway/wifi/mac` | excluded | device confirmation as applicable |
| R044 | connectkey | `/gateway/wifi/ssid` | excluded | device confirmation as applicable |
| R045 | connectkey | `/heatSources/actualHeatDemand` | missing_contract | P03 |
| R046 | connectkey | `/heatSources/actualModulation` | supported_read | device confirmation as applicable |
| R047 | connectkey | `/heatSources/actualSupplyTemperature` | supported_read | device confirmation as applicable |
| R048 | connectkey | `/heatSources/additionalHeater/operationMode` | supported_read | device confirmation as applicable |
| R049 | connectkey | `/heatSources/hs{hsNumber}/brineCircuit/collectorInflowTemp` | supported_read | device confirmation as applicable |
| R050 | connectkey | `/heatSources/hs{hsNumber}/brineCircuit/collectorOutflowTemp` | supported_read | device confirmation as applicable |
| R051 | connectkey | `/heatSources/chStatus` | supported_read | device confirmation as applicable |
| R052 | connectkey | `/heatSources/compressor/status` | supported_read | device confirmation as applicable |
| R053 | connectkey | `/heatSources/currentEmergencyMode` | supported_read | device confirmation as applicable |
| R054 | connectkey | `/heatSources/hs{hsNumber}/defrostActive` | supported_read | device confirmation as applicable |
| R055 | connectkey | `/heatSources/Source/eHeater/status` | supported_read | device confirmation as applicable |
| R056 | connectkey | `/heatSources/emStatus` | supported_read | device confirmation as applicable |
| R057 | connectkey | `/heatSources/emon/chConsumption` | supported_read | device confirmation as applicable |
| R058 | connectkey | `/heatSources/emon/coolingConsumption` | supported_read | device confirmation as applicable |
| R059 | connectkey | `/heatSources/emon/dhwConsumption` | supported_read | device confirmation as applicable |
| R060 | connectkey | `/heatSources/emon/poolConsumption` | supported_read | device confirmation as applicable |
| R061 | connectkey | `/heatSources/emon/totalConsumption` | supported_read | device confirmation as applicable |
| R062 | connectkey | `/heatSources/hs{hsNumber}/heatPumpType` | supported_read | device confirmation as applicable |
| R063 | connectkey | `/heatSources/hybrid/activeHeatSource` | supported_read | device confirmation as applicable |
| R064 | connectkey | `/heatSources/hybrid/controlStrategy` | supported_read | device confirmation as applicable |
| R065 | connectkey | `/heatSources/hybrid/outdoorStatus` | supported_read | device confirmation as applicable |
| R066 | connectkey | `/heatSources/hybrid/outdoorVariant` | supported_read | device confirmation as applicable |
| R067 | connectkey | `/heatSources/hybrid/reminderDate` | supported_read | device confirmation as applicable |
| R068 | connectkey | `/heatSources/hybrid/reminderEnable` | supported_read | device confirmation as applicable |
| R069 | connectkey | `/heatSources/hybrid/reminderLapsed` | supported_read | device confirmation as applicable |
| R070 | connectkey | `/heatSources/info` | directory | device confirmation as applicable |
| R071 | connectkey | `/heatSources/hs1/numberOfStarts` | supported_read | device confirmation as applicable |
| R072 | connectkey | `/heatSources/passiveCooling/inflowTemp` | supported_read | device confirmation as applicable |
| R073 | connectkey | `/heatSources/pvContactState` | supported_read | device confirmation as applicable |
| R074 | connectkey | `/heatSources/returnTemperature` | supported_read | device confirmation as applicable |
| R075 | connectkey | `/heatSources/standbyMode` | supported_read | device confirmation as applicable |
| R076 | connectkey | `/heatSources/systemPressure` | supported_read | device confirmation as applicable |
| R077 | connectkey | `/heatSources/systemPressureRange` | missing_contract | P03 |
| R078 | connectkey | `/heatSources/hs1/type` | supported_read | device confirmation as applicable |
| R079 | connectkey | `/heatSources/workingTime/totalSystem` | supported_read | device confirmation as applicable |
| R080 | connectkey | `/heatingCircuits/hc{circuitNumber}/activeSwitchProgram` | supported_read | device confirmation as applicable |
| R081 | connectkey | `/heatingCircuits/hc{circuitNumber}/actualHumidity` | supported_read | device confirmation as applicable |
| R082 | connectkey | `/heatingCircuits/hc{circuitNumber}/boostDuration` | supported_read | device confirmation as applicable |
| R083 | connectkey | `/heatingCircuits/hc{circuitNumber}/boostMode` | supported_read | device confirmation as applicable |
| R084 | connectkey | `/heatingCircuits/hc{circuitNumber}/boostRemainingTime` | supported_read | device confirmation as applicable |
| R085 | connectkey | `/heatingCircuits/hc{circuitNumber}/boostTemperature` | supported_read | device confirmation as applicable |
| R086 | connectkey | `/heatingCircuits/hc{circuitNumber}/name` | supported_read | device confirmation as applicable |
| R087 | connectkey | `/heatingCircuits/hc{circuitNumber}/controlType` | supported_read | device confirmation as applicable |
| R088 | connectkey | `/heatingCircuits/hc{circuitNumber}/cooling/controlType` | supported_read | device confirmation as applicable |
| R089 | connectkey | `/heatingCircuits/hc{circuitNumber}/cooling/operationMode` | supported_read | device confirmation as applicable |
| R090 | connectkey | `/heatingCircuits/hc{circuitNumber}/cooling/roomTempSetpoint` | supported_read | device confirmation as applicable |
| R091 | connectkey | `/heatingCircuits/hc{circuitNumber}/cooling/switchPrograms/A` | missing_contract | P07 |
| R092 | connectkey | `/heatingCircuits/hc{circuitNumber}/cooling/temperatureLevels/on` | supported_read | device confirmation as applicable |
| R093 | connectkey | `/heatingCircuits/hc{circuitNumber}/currentRoomSetpoint` | supported_read | device confirmation as applicable |
| R094 | connectkey | `/heatingCircuits/hc{circuitNumber}/currentSuWiMode` | supported_read | device confirmation as applicable |
| R095 | connectkey | `/heatingCircuits/hc{circuitNumber}/heatCoolMode` | supported_read | device confirmation as applicable |
| R096 | connectkey | `/heatingCircuits/hc{circuitNumber}/heatingType` | supported_read | device confirmation as applicable |
| R097 | connectkey | `/heatingCircuits/hc{circuitNumber}/manualRoomSetpoint` | supported_read | device confirmation as applicable |
| R098 | connectkey | `/heatingCircuits/hc{circuitNumber}/operationMode` | supported_read | device confirmation as applicable |
| R099 | connectkey | `/heatingCircuits/hc{circuitNumber}/overallStatus` | supported_read | device confirmation as applicable |
| R100 | connectkey | `/heatingCircuits/hc{circuitNumber}/roomtemperature` | supported_read | device confirmation as applicable |
| R101 | connectkey | `/heatingCircuits/hc{circuitNumber}/setpointOptimization` | supported_read | device confirmation as applicable |
| R102 | connectkey | `/heatingCircuits/hc{circuitNumber}/suWiCoolingThreshold` | supported_read | device confirmation as applicable |
| R103 | connectkey | `/heatingCircuits/hc{circuitNumber}/suWiSwitchMode` | supported_read | device confirmation as applicable |
| R104 | connectkey | `/heatingCircuits/hc{circuitNumber}/suWiThreshold` | supported_read | device confirmation as applicable |
| R105 | connectkey | `/heatingCircuits/hc{circuitNumber}/switchPrograms/A` | supported_read | read only; other models and editing |
| R106 | connectkey | `/heatingCircuits/hc{circuitNumber}/switchPrograms/B` | missing_contract | P07 |
| R107 | connectkey | `/heatingCircuits/hc{circuitNumber}/switchPrograms/nameA` | supported_read | device confirmation as applicable |
| R108 | connectkey | `/heatingCircuits/hc{circuitNumber}/switchPrograms/nameB` | supported_read | device confirmation as applicable |
| R109 | connectkey | `/heatingCircuits/hc{circuitNumber}/switchProgramMode` | supported_read | device confirmation as applicable |
| R110 | connectkey | `/heatingCircuits/hc{circuitNumber}/temperatureLevels/comfort2` | supported_read | device confirmation as applicable |
| R111 | connectkey | `/heatingCircuits/hc{circuitNumber}/temperatureLevels/eco` | supported_read | device confirmation as applicable |
| R112 | connectkey | `/heatingCircuits/hc{circuitNumber}/temporaryRoomSetpoint` | supported_read | device confirmation as applicable |
| R113 | connectkey | `/holidayMode/{holidayModeId}` | supported_transaction | device confirmation as applicable |
| R114 | connectkey | `/holidayMode/activeModes` | directory | device confirmation as applicable |
| R115 | connectkey | `/holidayMode/configuration` | directory | device confirmation as applicable |
| R116 | connectkey | `/holidayMode/list` | directory | device confirmation as applicable |
| R117 | connectkey | `/holidayMode` | supported_transaction | device confirmation as applicable |
| R118 | connectkey | `/notifications` | directory | device confirmation as applicable |
| R119 | connectkey | `/pool/additionalHeater/poolMode` | supported_read | device confirmation as applicable |
| R120 | connectkey | `/pool/currentTemp` | supported_read | device confirmation as applicable |
| R121 | connectkey | `/pool/enabled` | supported_read | device confirmation as applicable |
| R122 | connectkey | `/pool/setpointTemp` | supported_read | device confirmation as applicable |
| R123 | connectkey | `/recordings/heatSources/emon/total/burner?interval={interval}` | history | P15 |
| R124 | connectkey | `/recordings/heatSources/emon/total/electricity?interval={interval}` | history | P15 |
| R125 | connectkey | `/recordings/heatSources/emon/total/outputProduced?interval={interval}` | history | P15 |
| R126 | connectkey | `/recordings/heatSources/hs{hsNumber}/emon/total/outputProduced?interval={interval}` | history | P15 |
| R127 | connectkey | `/recordings/heatSources/emon/total/solar?interval={interval}` | history | P15 |
| R128 | connectkey | `/solarCircuits/sc1/collectorTemperature` | supported_read | device confirmation as applicable |
| R129 | connectkey | `/solarCircuits/sc1/dhwTankBottomTemperature` | supported_read | device confirmation as applicable |
| R130 | connectkey | `/solarCircuits/sc1/maxCylinderTemperature` | supported_read | device confirmation as applicable |
| R131 | connectkey | `/solarCircuits/sc1/maxTemperatureReached` | supported_read | device confirmation as applicable |
| R132 | connectkey | `/solarCircuits/sc1/pumpModulation` | supported_read | device confirmation as applicable |
| R133 | connectkey | `/solarCircuits/sc1/solarYield` | supported_read | device confirmation as applicable |
| R134 | connectkey | `/system/awayMode/enabled` | supported_read | device confirmation as applicable |
| R135 | connectkey | `/system/brand` | supported_read | device confirmation as applicable |
| R136 | connectkey | `/system/bus` | supported_read | device confirmation as applicable |
| R137 | connectkey | `/system/country` | excluded | device confirmation as applicable |
| R138 | connectkey | `/system/dateTime` | excluded | device confirmation as applicable |
| R139 | connectkey | `/system/energyTariff/electricity` | supported_read | device confirmation as applicable |
| R140 | connectkey | `/system/energyTariff/gas` | supported_read | device confirmation as applicable |
| R141 | connectkey | `/system/energyTariff/oil` | supported_read | device confirmation as applicable |
| R142 | connectkey | `/system/energyTariff/pv` | supported_read | device confirmation as applicable |
| R143 | connectkey | `/system/globalSeasonOptimizer/currentMode` | supported_read | device confirmation as applicable |
| R144 | connectkey | `/system/info` | excluded | device confirmation as applicable |
| R145 | connectkey | `/system/iSRC/installationStatus` | supported_read | device confirmation as applicable |
| R146 | connectkey | `/system/iSRC/supportStatus` | supported_read | device confirmation as applicable |
| R147 | connectkey | `/system/lowNoise/duration` | missing_contract | P03 |
| R148 | connectkey | `/system/lowNoise/mode` | supported_read | device confirmation as applicable |
| R149 | connectkey | `/system/powerGuard/active` | supported_read | device confirmation as applicable |
| R150 | connectkey | `/system/powerLimitation/active` | supported_read | device confirmation as applicable |
| R151 | connectkey | `/system/seasonOptimizer/coolingThreshold` | supported_read | device confirmation as applicable |
| R152 | connectkey | `/system/seasonOptimizer/heatingThreshold` | supported_read | device confirmation as applicable |
| R153 | connectkey | `/system/seasonOptimizer/mode` | supported_read | device confirmation as applicable |
| R154 | connectkey | `/system/sensors/temperatures/outdoor_t1` | supported_read | device confirmation as applicable |
| R155 | connectkey | `/system/silentMode/enabled` | supported_read | device confirmation as applicable |
| R156 | connectkey | `/system/silentMode/powerReduction` | missing_contract | P03 |
| R157 | connectkey | `/system/silentMode/startTime` | missing_contract | P03 |
| R158 | connectkey | `/system/silentMode/stopTime` | missing_contract | P03 |
| R159 | connectkey | `/system/variableTariff/ch/currentSetpoint` | supported_read | device confirmation as applicable |
| R160 | connectkey | `/system/variableTariff/ch/highPriceDelta` | supported_read | device confirmation as applicable |
| R161 | connectkey | `/system/variableTariff/ch/lowPriceDelta` | supported_read | device confirmation as applicable |
| R162 | connectkey | `/system/variableTariff/ch/midPriceSetpoint` | supported_read | device confirmation as applicable |
| R163 | connectkey | `/system/variableTariff/ch/optimization` | supported_read | device confirmation as applicable |
| R164 | connectkey | `/system/variableTariff/ch/status` | supported_read | device confirmation as applicable |
| R165 | connectkey | `/system/variableTariff/currentPriceCatagorization` | supported_read | device confirmation as applicable |
| R166 | connectkey | `/system/variableTariff/dhw/currentOpmode` | supported_read | device confirmation as applicable |
| R167 | connectkey | `/system/variableTariff/dhw/highPriceEnable` | supported_read | device confirmation as applicable |
| R168 | connectkey | `/system/variableTariff/dhw/lowPriceEnable` | supported_read | device confirmation as applicable |
| R169 | connectkey | `/system/variableTariff/dhw/optimization` | supported_read | device confirmation as applicable |
| R170 | connectkey | `/system/variableTariff/dhw/status` | supported_read | device confirmation as applicable |
| R171 | connectkey | `/system/variableTariff/priceInfo` | missing_contract | P03 |
| R172 | connectkey | `/system/variableTariff/supportStatus` | missing_contract | P03 |
| R173 | connectkey | `/system/variableTariff/tariffId` | supported_read | device confirmation as applicable |
| R174 | connectkey | `/ventilation/zone1/exhaustFanLevel` | supported_read | device confirmation as applicable |
| R175 | connectkey | `/ventilation/zone1/filter/remainingTime` | supported_read | device confirmation as applicable |
| R176 | connectkey | `/ventilation/zone1/maxIndoorAirQuality` | supported_read | device confirmation as applicable |
| R177 | connectkey | `/ventilation/zone1/maxRelativeHumidity` | supported_read | device confirmation as applicable |
| R178 | connectkey | `/ventilation/zone1/operationMode` | supported_read | device confirmation as applicable |
| R179 | connectkey | `/ventilation/zone1/sensors/supplyTemp` | supported_read | device confirmation as applicable |
| R180 | connectkey | `/ventilation/zone1/switchPrograms/cp` | missing_contract | P07 |
| R181 | connectkey | `/ventilation/zone1/ventilationLevels` | missing_contract | P03 |
| R182 | connectkey | `/zones/zone{zoneNumber}/averageActualHumidity` | supported_read | P05 |
| R183 | connectkey | `/zones/zone{zoneNumber}/averageCurrentTemperature` | supported_read | P05 |
| R184 | connectkey | `/zones/zone{zoneNumber}/childLock` | supported_read | P05 |
| R185 | connectkey | `/zones/configuration` | directory | device confirmation as applicable |
| R186 | connectkey | `/zones/zone{zoneNumber}/cooling/manualRoomSetpoint` | supported_control | device confirmation as applicable |
| R187 | connectkey | `/zones/zone{zoneNumber}/cooling/operationMode` | supported_control | device confirmation as applicable |
| R188 | connectkey | `/zones/zone{zoneNumber}/cooling/temporaryRoomSetpoint` | supported_read | P05 |
| R189 | connectkey | `/zones/zone{zoneNumber}/currentRoomSetpoint` | supported_read | P05 |
| R190 | connectkey | `/zones/zone{zoneNumber}/heating/manualRoomSetpoint` | supported_control | device confirmation as applicable |
| R191 | connectkey | `/zones/zone{zoneNumber}/heating/operationMode` | supported_control | device confirmation as applicable |
| R192 | connectkey | `/zones/zone{zoneNumber}/heating/temporaryRoomSetpoint` | supported_read | P05 |
| R193 | connectkey | `/zones/zone{zoneNumber}/switchPrograms/cooling` | missing_contract | P07 |
| R194 | connectkey | `/zones/zone{zoneNumber}/switchPrograms/heating` | missing_contract | P07 |
| R195 | connectkey | `/zones/list` | directory | device confirmation as applicable |
| R196 | connectkey | `/dhwCircuits` | directory | device confirmation as applicable |
| R197 | connectkey | `/heatingCircuits` | directory | device confirmation as applicable |
| R198 | connectkey | `/solarCircuits` | directory | device confirmation as applicable |
| R199 | icom | `/dhwCircuits` | directory | device confirmation as applicable |
| R200 | icom | `/heatingCircuits` | directory | device confirmation as applicable |
| R201 | icom | `/solarCircuits` | directory | device confirmation as applicable |
| R202 | icom | `/heatingCircuits/hc{circuitNumber}/cooling/roomTempSetpoint` | supported_read | device confirmation as applicable |
| R203 | icom | `/dhwCircuits/dhw{circuitNumber}/actualTemp` | supported_read | device confirmation as applicable |
| R204 | icom | `/dhwCircuits/dhw{circuitNumber}/chargeDuration` | supported_read | device confirmation as applicable |
| R205 | icom | `/dhwCircuits/dhw{circuitNumber}/singleChargeSetpoint` | supported_read | device confirmation as applicable |
| R206 | icom | `/dhwCircuits/dhw{circuitNumber}/charge` | supported_read | device confirmation as applicable |
| R207 | icom | `/dhwCircuits/dhw{circuitNumber}/holidayMode/activated` | missing_contract | P07 |
| R208 | icom | `/dhwCircuits/dhw{circuitNumber}/operationMode` | supported_read | device confirmation as applicable |
| R209 | icom | `/dhwCircuits/dhw{circuitNumber}/switchPrograms/A` | missing_contract | P07 |
| R210 | icom | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels/high` | supported_read | device confirmation as applicable |
| R211 | icom | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels/low` | supported_read | device confirmation as applicable |
| R212 | icom | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels` | missing_contract | P03 |
| R213 | icom | `/gateway/DateTime` | excluded | device confirmation as applicable |
| R214 | icom | `/gateway/uuid` | excluded | device confirmation as applicable |
| R215 | icom | `/gateway/versionFirmware` | supported_read | device confirmation as applicable |
| R216 | icom | `/heatSources/hs1/type` | supported_read | device confirmation as applicable |
| R217 | icom | `/heatSources/info` | directory | device confirmation as applicable |
| R218 | icom | `/heatingCircuits/hc{circuitNumber}/activeSwitchProgram` | supported_read | device confirmation as applicable |
| R219 | icom | `/heatingCircuits/hc{circuitNumber}/controlType` | supported_read | device confirmation as applicable |
| R220 | icom | `/heatingCircuits/hc{circuitNumber}/currentRoomSetpoint` | supported_read | device confirmation as applicable |
| R221 | icom | `/heatingCircuits/hc{circuitNumber}/currentSuWiMode` | supported_read | device confirmation as applicable |
| R222 | icom | `/heatingCircuits/hc{circuitNumber}/holidayMode/activated` | missing_contract | P07 |
| R223 | icom | `/heatingCircuits/hc{circuitNumber}/manualRoomSetpoint` | supported_read | device confirmation as applicable |
| R224 | icom | `/heatingCircuits/hc{circuitNumber}/operationMode` | supported_read | device confirmation as applicable |
| R225 | icom | `/heatingCircuits/hc{circuitNumber}/roomtemperature` | supported_read | device confirmation as applicable |
| R226 | icom | `/heatingCircuits/hc{circuitNumber}/suWiSwitchMode` | supported_read | device confirmation as applicable |
| R227 | icom | `/heatingCircuits/hc{circuitNumber}/switchPrograms/A` | missing_contract | P07 |
| R228 | icom | `/heatingCircuits/hc{circuitNumber}/switchPrograms/B` | missing_contract | P07 |
| R229 | icom | `/heatingCircuits/hc{circuitNumber}/switchProgramMode` | supported_read | device confirmation as applicable |
| R230 | icom | `/heatingCircuits/hc{circuitNumber}/temperatureLevels` | missing_contract | P03 |
| R231 | icom | `/heatingCircuits/hc{circuitNumber}/temperatureLevels/comfort2` | supported_read | device confirmation as applicable |
| R232 | icom | `/heatingCircuits/hc{circuitNumber}/temperatureLevels/eco` | supported_read | device confirmation as applicable |
| R233 | icom | `/heatingCircuits/hc{circuitNumber}/temporaryRoomSetpoint` | supported_read | device confirmation as applicable |
| R234 | icom | `/system/holidayModes/hm{holidayNumber}/dhwMode` | missing_contract | P07 |
| R235 | icom | `/system/holidayModes/hm{holidayNumber}/fixTemperature` | missing_contract | P07 |
| R236 | icom | `/system/holidayModes/hm{holidayNumber}/hcMode` | missing_contract | P07 |
| R237 | icom | `/system/holidayModes/hm{holidayNumber}/startStop` | missing_contract | P07 |
| R238 | icom | `/notifications` | directory | device confirmation as applicable |
| R239 | icom | `/system/bus` | supported_read | device confirmation as applicable |
| R240 | icom | `/system/busReq?emsData=0804840401` | excluded | device confirmation as applicable |
| R241 | icom | `/system/info` | excluded | device confirmation as applicable |
| R242 | icom | `/ventilation/zone1/exhaustFanLevel` | supported_read | device confirmation as applicable |
| R243 | icom | `/ventilation/zone1/operationMode` | supported_read | device confirmation as applicable |
| R244 | rrc | `/dhwCircuits/dhw{circuitId}/actualTemp` | supported_read | device confirmation as applicable |
| R245 | rrc | `/system/awayMode/enabled` | supported_read | device confirmation as applicable |
| R246 | rrc | `/system/awayMode/temperature` | supported_read | device confirmation as applicable |
| R247 | rrc | `/zones/zn{zoneId}/clockProgram` | supported_read | device confirmation as applicable |
| R248 | rrc | `/heatingCircuits/hc{circuitId}/control` | missing_contract | P06 |
| R249 | rrc | `/gateway/versionFirmware` | supported_read | device confirmation as applicable |
| R250 | rrc | `/heatSources/type` | supported_read | device confirmation as applicable |
| R251 | rrc | `/devices/list` | directory | device confirmation as applicable |
| R252 | rrc | `/devices/dev{devId}/name` | missing_contract | P06 |
| R253 | rrc | `/devices/device{devId}/zone` | missing_contract | P06 |
| R254 | rrc | `/dhwCircuits/dhw{circuitId}/operationMode` | supported_read | device confirmation as applicable |
| R255 | rrc | `/dhwCircuits/dhw{circuitId}/hotWaterSystem` | missing_contract | P06 |
| R256 | rrc | `/dhwCircuits/dhw{circuitId}/programs/pg{programId}/week` | missing_contract | P06 |
| R257 | rrc | `/zones/zn{zoneId}/icon` | missing_contract | P06 |
| R258 | rrc | `/gateway/ui/icons` | missing_contract | P06 |
| R259 | rrc | `/heatSources/info` | directory | device confirmation as applicable |
| R260 | rrc | `/system/location/coordinates` | excluded | device confirmation as applicable |
| R261 | rrc | `/zones/zn{zoneId}/name` | missing_contract | P06 |
| R262 | rrc | `/zones/zn{zoneId}/openWindowDetection/enabled` | missing_contract | P06 |
| R263 | rrc | `/programs/pg{programId}/{dayOfWeek}` | supported_transaction | P06 |
| R264 | rrc | `/programs/pg{programId}/name` | missing_contract | P06 |
| R265 | rrc | `/programs/pg{programId}` | directory | device confirmation as applicable |
| R266 | rrc | `/programs/list` | directory | device confirmation as applicable |
| R267 | rrc | `/programs/pg{programId}/week` | supported_transaction | P06 |
| R268 | rrc | `/programs` | directory | device confirmation as applicable |
| R269 | rrc | `/gateway/wizardStepsDone` | missing_contract | P06 |
| R270 | rrc | `/gateway/time/current` | missing_contract | P06 |
| R271 | rrc | `/gateway/time/timeZone` | missing_contract | P06 |
| R272 | rrc | `/zones/zn{zoneId}/userMode` | supported_read | device confirmation as applicable |
| R273 | rrc | `/gateway/uuid` | excluded | device confirmation as applicable |
| R274 | rrc | `/zones/zn{zoneId}` | directory | device confirmation as applicable |
| R275 | rrc | `/zones/zn{zoneId}/clockOverride/temperatureHeating` | supported_read | P06 |
| R276 | rrc | `/zones/list` | directory | device confirmation as applicable |
| R277 | rrc | `/zones/zn{zoneId}/manualTemperatureHeating` | supported_read | device confirmation as applicable |
| R278 | rrc | `/zones/zn{zoneId}/temperatureActual` | supported_read | device confirmation as applicable |
| R279 | rrc | `/zones/zn{zoneId}/temperatureHeatingSetpoint` | supported_read | device confirmation as applicable |
| R280 | rrc | `/zones` | directory | device confirmation as applicable |
| R281 | watersoftener | `/system/appliance/versionFirmware` | supported_read | device confirmation as applicable |
| R282 | watersoftener | `/system/appliance/model` | supported_read | device confirmation as applicable |
| R283 | watersoftener | `None` | separate_package | P04 |
| R284 | watersoftener | `/gateway/factoryReset` | excluded | device confirmation as applicable |
| R285 | watersoftener | `/gateway/update/triggerRequest` | excluded | device confirmation as applicable |
| R286 | watersoftener | `/gateway/DateTime` | excluded | device confirmation as applicable |
| R287 | watersoftener | `/gateway/serialId` | excluded | device confirmation as applicable |
| R288 | watersoftener | `/gateway/tzInfo/timeZone` | supported_read | device confirmation as applicable |
| R289 | watersoftener | `/gateway/uuid` | excluded | device confirmation as applicable |
| R290 | watersoftener | `/gateway/versionFirmware` | supported_read | device confirmation as applicable |
| R291 | watersoftener | `/gateway/versionHardware` | supported_read | device confirmation as applicable |
| R292 | watersoftener | `/gateway/wifi/mac` | excluded | device confirmation as applicable |
| R293 | watersoftener | `/gateway/wifi/ssid` | excluded | device confirmation as applicable |
| R294 | watersoftener | `/swCircuits/sw1/holidayMode/enabled` | supported_read | device confirmation as applicable |
| R295 | watersoftener | `/recordings/swCircuits/sw1/sensor/water?interval=31D` | history | P15 |
| R296 | watersoftener | `/recordings/swCircuits/sw1/sensor/water?interval=24M` | history | P15 |
| R297 | watersoftener | `/swCircuits/sw1/regeneration/enabled` | supported_read | device confirmation as applicable |
| R298 | watersoftener | `/swCircuits/sw1/regeneration/phase` | supported_read | device confirmation as applicable |
| R299 | watersoftener | `/swCircuits/sw1/regeneration/singleRegAppoint` | supported_read | device confirmation as applicable |
| R300 | watersoftener | `/swCircuits/sw1/regeneration/timeUntilNext` | supported_read | device confirmation as applicable |
| R301 | watersoftener | `/swCircuits/sw1/regeneration/totalProgressStatus` | supported_read | device confirmation as applicable |
| R302 | watersoftener | `/swCircuits/sw1/regeneration/totalRemainingTime` | supported_read | device confirmation as applicable |
| R303 | watersoftener | `/swCircuits/sw1/regeneration/log` | history | P15 |
| R304 | watersoftener | `/swCircuits/sw1/sensor/availableWater` | supported_read | device confirmation as applicable |
| R305 | watersoftener | `/swCircuits/sw1/sensor/saltLevel` | supported_read | device confirmation as applicable |
| R306 | watersoftener | `/swCircuits/sw1/sensor/saltLevelAlarm` | supported_read | device confirmation as applicable |
| R307 | watersoftener | `/swCircuits/sw1/sensor/waterFlow` | supported_read | device confirmation as applicable |
| R308 | watersoftener | `/system/brand` | supported_read | device confirmation as applicable |
| R309 | watersoftener | `/notifications` | directory | device confirmation as applicable |
| R310 | wddw2 | `/heatSources/hs1/actualPower` | supported_read | device confirmation as applicable |
| R311 | wddw2 | `/dhwCircuits/dhw1/sensor/airBoxTemperature` | supported_read | device confirmation as applicable |
| R312 | wddw2 | `/system/appliance/versionFirmware` | supported_read | device confirmation as applicable |
| R313 | wddw2 | `/dhwCircuits/dhw1/sensor/atmosphericPressure` | supported_read | device confirmation as applicable |
| R314 | wddw2 | `/system/brand` | supported_read | device confirmation as applicable |
| R315 | wddw2 | `/system/bus` | supported_read | device confirmation as applicable |
| R316 | wddw2 | `/dhwCircuits/dhw1/charge` | supported_read | device confirmation as applicable |
| R317 | wddw2 | `/dhwCircuits/dhw1/currentSetpoint` | supported_read | device confirmation as applicable |
| R318 | wddw2 | `/gateway/DateTime` | excluded | device confirmation as applicable |
| R319 | wddw2 | `/recordings/heatSources/hs1/sensor/electricity?interval={interval}` | history | P15 |
| R320 | wddw2 | `/heatSources/electricityTotalConsumption` | supported_read | device confirmation as applicable |
| R321 | wddw2 | `/system/appliance/enabled` | supported_read | device confirmation as applicable |
| R322 | wddw2 | `/dhwCircuits/dhw1/sensor/exhaustFlueGasTemperature` | supported_read | device confirmation as applicable |
| R323 | wddw2 | `/dhwCircuits/dhw1/sensor/externalTankTemperature` | supported_read | device confirmation as applicable |
| R324 | wddw2 | `None` | separate_package | P07 |
| R325 | wddw2 | `/gateway/factoryReset` | excluded | device confirmation as applicable |
| R326 | wddw2 | `/dhwCircuits/dhw1/sensor/fanSpeed` | supported_read | device confirmation as applicable |
| R327 | wddw2 | `/dhwCircuits/dhw1/sensor/gasFlow` | supported_read | device confirmation as applicable |
| R328 | wddw2 | `/recordings/heatSources/hs1/sensor/gas?interval={interval}` | history | P15 |
| R329 | wddw2 | `/heatSources/gasTotalConsumption` | supported_read | device confirmation as applicable |
| R330 | wddw2 | `/gateway/versionHardware` | supported_read | device confirmation as applicable |
| R331 | wddw2 | `/dhwCircuits/dhw1/sensor/heatExchangerFlueGasTemperature` | supported_read | device confirmation as applicable |
| R332 | wddw2 | `/dhwCircuits/dhw1/sensor/heatExchangerTemperature` | supported_read | device confirmation as applicable |
| R333 | wddw2 | `/system/holidayMode` | missing_contract | P07 |
| R334 | wddw2 | `/dhwCircuits/dhw1/inletTemperature` | supported_read | device confirmation as applicable |
| R335 | wddw2 | `/dhwCircuits/dhw1/learningWeek` | supported_read | device confirmation as applicable |
| R336 | wddw2 | `/dhwCircuits/dhw1/manualsetpoint` | supported_read | device confirmation as applicable |
| R337 | wddw2 | `/system/appliance/model` | supported_read | device confirmation as applicable |
| R338 | wddw2 | `/dhwCircuits/dhw1/monitorValues` | missing_contract | P03 |
| R339 | wddw2 | `/notifications` | directory | device confirmation as applicable |
| R340 | wddw2 | `/dhwCircuits/dhw1/numberOfShowersAvailable` | missing_contract | P03 |
| R341 | wddw2 | `/heatSources/hs1/numberOfStarts` | supported_read | device confirmation as applicable |
| R342 | wddw2 | `/heatSources/hs1/operationHours` | supported_read | device confirmation as applicable |
| R343 | wddw2 | `/dhwCircuits/dhw1/operationMode` | supported_read | device confirmation as applicable |
| R344 | wddw2 | `/dhwCircuits/dhw1/operationSetpoints` | missing_contract | P03 |
| R345 | wddw2 | `/dhwCircuits/dhw1/outletTemperature` | supported_read | device confirmation as applicable |
| R346 | wddw2 | `/heatSources/hs1/powerPercentage` | supported_read | device confirmation as applicable |
| R347 | wddw2 | `/dhwCircuits/dhw1/recirculation/enabled` | supported_read | device confirmation as applicable |
| R348 | wddw2 | `/dhwCircuits/dhw1/safetyTemperature` | supported_read | device confirmation as applicable |
| R349 | wddw2 | `/gateway/serialId` | excluded | device confirmation as applicable |
| R350 | wddw2 | `/dhwCircuits/dhw1/switchPrograms/program1` | missing_contract | P07 |
| R351 | wddw2 | `/system/systemOfUnits` | supported_read | device confirmation as applicable |
| R352 | wddw2 | `/dhwCircuits/dhw1/tdMode` | supported_read | device confirmation as applicable |
| R353 | wddw2 | `/gateway/tzInfo/timeZone` | supported_read | device confirmation as applicable |
| R354 | wddw2 | `/gateway/update/triggerRequest` | excluded | device confirmation as applicable |
| R355 | wddw2 | `/gateway/uuid` | excluded | device confirmation as applicable |
| R356 | wddw2 | `/gateway/versionFirmware` | supported_read | device confirmation as applicable |
| R357 | wddw2 | `/dhwCircuits/dhw1/sensor/waterFlow` | supported_read | device confirmation as applicable |
| R358 | wddw2 | `/recordings/dhwCircuits/dhw1/sensor/water?interval={interval}` | history | P15 |
| R359 | wddw2 | `/dhwCircuits/waterTotalConsumption` | supported_read | device confirmation as applicable |
| R360 | wddw2 | `/gateway/wifi/mac` | excluded | device confirmation as applicable |
| R361 | wddw2 | `/gateway/wifi/ssid` | excluded | device confirmation as applicable |

## iCom rows R199 to R243 compared with a real gateway

The iCom rows R199 to R243 are a model path list; an iCom gateway can offer
and accept more paths than these rows. For 0.9.0-alpha.6 I compared the
rows with the diagnostic of a real iCom gateway with a boiler controller (EMS 2), evaluated
locally and only as shapes. The table above is unchanged; the model rows keep
their IDs. The differences are listed here, and the synthetic fixture
[`icom/gateway-shapes.json`](../tests/fixtures/contracts/icom/gateway-shapes.json)
holds these shapes with constructed values.

**Present on the real gateway, missing from R199 to R243:**

| Path | Shape on that gateway | Note |
|---|---|---|
| `/system/holidayModes/hm{n}/assignedTo` | `arrayData`, 2 options, writable | used by the [older holiday formats](legacy-holidays.md) |
| `/system/holidayModes/hm{n}/delete` | 403 | deleting a holiday stays unavailable there |
| `/dhwCircuits/dhw1/tdMode` | string, 2 options, writable | like R352 (wddw2) |
| `/dhwCircuits/dhw1/currentSetpoint` | float, °C | like R016 (connectkey) |
| `/dhwCircuits/dhw1/temperatureLevels/off` | float, °C, read-only | like R028 (connectkey) |
| `/heatSources/flameStatus`, `/heatSources/actualModulation` | string with 2 options; float, % | the `hs1` leaves of the same name answer 403 |
| `/heatSources/systemPressure` | float, bar | like R076 (connectkey) |
| `/heatSources/hs1/actualPower` | float, kW | like R310 (wddw2) |
| `/system/healthStatus`, `/gateway/update/status` | string, 3 options each | read-only |
| `/system/sensors/temperatures/chimney`, `return`, `supply_t1`, `supply_t1_setpoint` | float, °C | read-only |
| `/heatingCircuits/hc1/heatCurveMax`, `heatCurveMin` | float, °C, writable with limits | no entity |
| `/heatingCircuits/hc1/roomInfluence` | float, unit `K`, writable with limits | no entity |
| `/heatingCircuits/hc1/suWiThreshold` | float, °C, writable with limits | like R104 (connectkey) |
| `/heatingCircuits/hc1/supplyTemperatureSetpoint` | float, °C, read-only | no entity |

**In R199 to R243, but not answered by that gateway:**

| Row | Path | Answer |
|---|---|---|
| R223 | `/heatingCircuits/hc{n}/manualRoomSetpoint` | 404; the circuit offers `temporaryRoomSetpoint` (writable, with limits) instead |
| R235 | `/system/holidayModes/hm{n}/fixTemperature` | 404 in all five slots |
| R202 | `/heatingCircuits/hc{n}/cooling/roomTempSetpoint` | 404 |
| R211 | `/dhwCircuits/dhw{n}/temperatureLevels/low` | not offered; the levels are `high` and `off` |
| R207, R222 | `/dhwCircuits/dhw{n}/holidayMode/activated`, `/heatingCircuits/hc{n}/holidayMode/activated` | the parent `holidayMode` answers 403 |

The slot parents `/system/holidayModes/hm{n}` answer 403 while their leaves
are readable. Faults come only through `/notifications`; `hs1/activefailure`
and `hs1/failurelist` answer 404. The diagnostic held no active notification,
so the format of an iCom notification entry is still unconfirmed.

**Further iCom paths, not in R199 to R243 (expected, untested):** writing
`temporaryRoomSetpoint` (−1 clears it; written by the heating circuit climate
in automatic mode since 0.9.0, see
[Heating controls](heating-controls.md#temporary-setpoint-in-automatic-mode)),
`roomtemperature`, `charge`,
`chargeDuration` and `singleChargeSetpoint`, the pool values below
`/heatSources`, the energy histories below `/recordings`, `/gateway/serialId`,
`/system/brand`, `suWiThreshold`, `cooling/outdoorThreshold`,
`switchProgramMode` and `/notifications`. Reading most of these works through
the generic discovery already; writing the pool and the energy histories is
not part of 0.9.0.
