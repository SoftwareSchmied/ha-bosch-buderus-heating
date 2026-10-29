# Resource contracts and implementation inventory

I reconciled the previous 359 definitions with all 361 comparison rows for P03.
The two additional entries are generic execution classes that receive a path
from their caller. They are not two newly discovered devices or entities.

I preserve each model row and its previous path. Explicit dynamic arguments,
fixed model paths, query arguments and corrected compositions are recorded
separately in [the machine-readable inventory](app-resource-inventory.json).
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

| ID | Family | Model | Path template | Disposition | Follow-up |
|---|---|---|---|---|---|
| R001 | connectkeyprovider | `ConnectKeyConfiguringResource$UpdateTriggerRequest` | `/gateway/update/triggerRequest` | excluded | device confirmation as applicable |
| R002 | connectkeyprovider | `ConnectKeyDevicesResource$ActualHumidity` | `/devices/device{controllerNumber}/actualHumidity` | supported_read | device confirmation as applicable |
| R003 | connectkeyprovider | `ConnectKeyDevicesResource$DevicesList` | `/devices/list` | directory | device confirmation as applicable |
| R004 | connectkeyprovider | `ConnectKeyDevicesResource$Errors` | `/devices/device{controllerNumber}/errors` | missing_contract | P03 |
| R005 | connectkeyprovider | `ConnectKeyDevicesResource$InclusionTime` | `/devices/inclusionTime` | directory | device confirmation as applicable |
| R006 | connectkeyprovider | `ConnectKeyDevicesResource$InclusionWhiteList` | `/devices/inclusionWhitelist` | directory | device confirmation as applicable |
| R007 | connectkeyprovider | `ConnectKeyDevicesResource$RfTimeOfConnectionLost` | `/devices/device{controllerNumber}/rfTimeofConnectionLost` | supported_read | device confirmation as applicable |
| R008 | connectkeyprovider | `ConnectKeyDevicesResource$RoomTemperature` | `/devices/device{controllerNumber}/roomtemperature` | supported_read | device confirmation as applicable |
| R009 | connectkeyprovider | `ConnectKeyDevicesResource$VersionFirmware` | `/devices/device{controllerNumber}/versionFirmware` | supported_read | device confirmation as applicable |
| R010 | connectkeyprovider | `ConnectKeyDhwCircuitResource$Dhw1ReduceTempOnAlarm` | `/dhwCircuits/dhw1/reduceTempOnAlarm` | supported_read | device confirmation as applicable |
| R011 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwActualTemperature` | `/dhwCircuits/dhw{circuitNumber}/actualTemp` | supported_read | device confirmation as applicable |
| R012 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwCharge` | `/dhwCircuits/dhw{circuitNumber}/charge` | supported_read | device confirmation as applicable |
| R013 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwChargeDuration` | `/dhwCircuits/dhw{circuitNumber}/chargeDuration` | supported_read | device confirmation as applicable |
| R014 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwChargeRemainingTime` | `/dhwCircuits/dhw{circuitNumber}/chargeRemainingTime` | supported_read | device confirmation as applicable |
| R015 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwCurrentFriwaSupplyTemperature` | `/dhwCircuits/dhw1/currentFriwaSupplyTemperature` | supported_read | device confirmation as applicable |
| R016 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwCurrentSetpoint` | `/dhwCircuits/dhw{circuitNumber}/currentSetpoint` | supported_read | device confirmation as applicable |
| R017 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwCurrentTemperatureLevel` | `/dhwCircuits/dhw{circuitNumber}/currentTemperatureLevel` | supported_read | device confirmation as applicable |
| R018 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwFriwaPrimaryPumpModulation` | `/dhwCircuits/dhw1/friwaPrimaryPumpModulation` | supported_read | device confirmation as applicable |
| R019 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwName` | `/dhwCircuits/dhw{circuitNumber}/name` | supported_read | device confirmation as applicable |
| R020 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwOperationMode` | `/dhwCircuits/dhw{circuitNumber}/operationMode` | supported_read | device confirmation as applicable |
| R021 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwOutTemp` | `/dhwCircuits/dhw{circuitNumber}/outTemp` | supported_read | device confirmation as applicable |
| R022 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwSingleChargeSetpoint` | `/dhwCircuits/dhw{circuitNumber}/singleChargeSetpoint` | supported_read | device confirmation as applicable |
| R023 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwSwitchProgramA` | `/dhwCircuits/dhw{circuitNumber}/switchPrograms/A` | supported_read | read only; other models and editing |
| R024 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwTemperatureLevels` | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels` | missing_contract | P03 |
| R025 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwTemperatureLevelsEco` | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels/eco` | supported_read | device confirmation as applicable |
| R026 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwTemperatureLevelsHigh` | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels/high` | supported_read | device confirmation as applicable |
| R027 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwTemperatureLevelsLow` | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels/low` | supported_read | device confirmation as applicable |
| R028 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwTemperatureLevelsOff` | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels/off` | supported_read | device confirmation as applicable |
| R029 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwType` | `/dhwCircuits/dhw{circuitNumber}/dhwType` | supported_read | device confirmation as applicable |
| R030 | connectkeyprovider | `ConnectKeyDhwCircuitResource$DhwVolumeFlow` | `/dhwCircuits/dhw{circuitNumber}/volumeFlow` | supported_read | device confirmation as applicable |
| R031 | connectkeyprovider | `ConnectKeyDhwCircuitResource$OverallStatus` | `/dhwCircuits/dhw{circuitNumber}/overallStatus` | supported_read | device confirmation as applicable |
| R032 | connectkeyprovider | `ConnectKeyGatewayResource$DataProcessingStatus` | `/gateway/dataProcessing/status` | supported_read | device confirmation as applicable |
| R033 | connectkeyprovider | `ConnectKeyGatewayResource$DateTime` | `/gateway/dateTime` | excluded | device confirmation as applicable |
| R034 | connectkeyprovider | `ConnectKeyGatewayResource$EthIpv4` | `/gateway/eth/ip/ipv4` | excluded | device confirmation as applicable |
| R035 | connectkeyprovider | `ConnectKeyGatewayResource$EthIpv6` | `/gateway/eth/ip/ipv6` | excluded | device confirmation as applicable |
| R036 | connectkeyprovider | `ConnectKeyGatewayResource$EthMac` | `/gateway/eth/mac` | excluded | device confirmation as applicable |
| R037 | connectkeyprovider | `ConnectKeyGatewayResource$SerialId` | `/gateway/serialId` | excluded | device confirmation as applicable |
| R038 | connectkeyprovider | `ConnectKeyGatewayResource$Uuid` | `/gateway/uuid` | excluded | device confirmation as applicable |
| R039 | connectkeyprovider | `ConnectKeyGatewayResource$VersionFirmware` | `/gateway/versionFirmware` | supported_read | device confirmation as applicable |
| R040 | connectkeyprovider | `ConnectKeyGatewayResource$VersionHardware` | `/gateway/versionHardware` | supported_read | device confirmation as applicable |
| R041 | connectkeyprovider | `ConnectKeyGatewayResource$WifiIpv4` | `/gateway/wifi/ip/ipv4` | excluded | device confirmation as applicable |
| R042 | connectkeyprovider | `ConnectKeyGatewayResource$WifiIpv6` | `/gateway/wifi/ip/ipv6` | excluded | device confirmation as applicable |
| R043 | connectkeyprovider | `ConnectKeyGatewayResource$WifiMac` | `/gateway/wifi/mac` | excluded | device confirmation as applicable |
| R044 | connectkeyprovider | `ConnectKeyGatewayResource$WifiSsid` | `/gateway/wifi/ssid` | excluded | device confirmation as applicable |
| R045 | connectkeyprovider | `ConnectKeyHeatSourcesResource$ActualHeatDemand` | `/heatSources/actualHeatDemand` | missing_contract | P03 |
| R046 | connectkeyprovider | `ConnectKeyHeatSourcesResource$ActualModulation` | `/heatSources/actualModulation` | supported_read | device confirmation as applicable |
| R047 | connectkeyprovider | `ConnectKeyHeatSourcesResource$ActualSupplyTemperature` | `/heatSources/actualSupplyTemperature` | supported_read | device confirmation as applicable |
| R048 | connectkeyprovider | `ConnectKeyHeatSourcesResource$AdditionalHeater$OperationMode` | `/heatSources/additionalHeater/operationMode` | supported_read | device confirmation as applicable |
| R049 | connectkeyprovider | `ConnectKeyHeatSourcesResource$BrineCircuit$CollectorInflowTemp` | `/heatSources/hs{hsNumber}/brineCircuit/collectorInflowTemp` | supported_read | device confirmation as applicable |
| R050 | connectkeyprovider | `ConnectKeyHeatSourcesResource$BrineCircuit$CollectorOutflowTemp` | `/heatSources/hs{hsNumber}/brineCircuit/collectorOutflowTemp` | supported_read | device confirmation as applicable |
| R051 | connectkeyprovider | `ConnectKeyHeatSourcesResource$CHStatus` | `/heatSources/chStatus` | supported_read | device confirmation as applicable |
| R052 | connectkeyprovider | `ConnectKeyHeatSourcesResource$CompressorStatus` | `/heatSources/compressor/status` | supported_read | device confirmation as applicable |
| R053 | connectkeyprovider | `ConnectKeyHeatSourcesResource$CurrentEmergencyMode` | `/heatSources/currentEmergencyMode` | supported_read | device confirmation as applicable |
| R054 | connectkeyprovider | `ConnectKeyHeatSourcesResource$DefrostActive` | `/heatSources/hs{hsNumber}/defrostActive` | supported_read | device confirmation as applicable |
| R055 | connectkeyprovider | `ConnectKeyHeatSourcesResource$EHeaterStatus` | `/heatSources/Source/eHeater/status` | supported_read | device confirmation as applicable |
| R056 | connectkeyprovider | `ConnectKeyHeatSourcesResource$EmStatus` | `/heatSources/emStatus` | supported_read | device confirmation as applicable |
| R057 | connectkeyprovider | `ConnectKeyHeatSourcesResource$EmonLifetime$ChConsumption` | `/heatSources/emon/chConsumption` | supported_read | device confirmation as applicable |
| R058 | connectkeyprovider | `ConnectKeyHeatSourcesResource$EmonLifetime$CoolingConsumption` | `/heatSources/emon/coolingConsumption` | supported_read | device confirmation as applicable |
| R059 | connectkeyprovider | `ConnectKeyHeatSourcesResource$EmonLifetime$DhwConsumption` | `/heatSources/emon/dhwConsumption` | supported_read | device confirmation as applicable |
| R060 | connectkeyprovider | `ConnectKeyHeatSourcesResource$EmonLifetime$PoolConsumption` | `/heatSources/emon/poolConsumption` | supported_read | device confirmation as applicable |
| R061 | connectkeyprovider | `ConnectKeyHeatSourcesResource$EmonLifetime$TotalConsumption` | `/heatSources/emon/totalConsumption` | supported_read | device confirmation as applicable |
| R062 | connectkeyprovider | `ConnectKeyHeatSourcesResource$HPType` | `/heatSources/hs{hsNumber}/heatPumpType` | supported_read | device confirmation as applicable |
| R063 | connectkeyprovider | `ConnectKeyHeatSourcesResource$Hybrid$ActiveHeatSource` | `/heatSources/hybrid/activeHeatSource` | supported_read | device confirmation as applicable |
| R064 | connectkeyprovider | `ConnectKeyHeatSourcesResource$Hybrid$ControlStrategy` | `/heatSources/hybrid/controlStrategy` | supported_read | device confirmation as applicable |
| R065 | connectkeyprovider | `ConnectKeyHeatSourcesResource$Hybrid$OutdoorStatus` | `/heatSources/hybrid/outdoorStatus` | supported_read | device confirmation as applicable |
| R066 | connectkeyprovider | `ConnectKeyHeatSourcesResource$Hybrid$OutdoorVariant` | `/heatSources/hybrid/outdoorVariant` | supported_read | device confirmation as applicable |
| R067 | connectkeyprovider | `ConnectKeyHeatSourcesResource$Hybrid$ReminderDate` | `/heatSources/hybrid/reminderDate` | supported_read | device confirmation as applicable |
| R068 | connectkeyprovider | `ConnectKeyHeatSourcesResource$Hybrid$ReminderEnable` | `/heatSources/hybrid/reminderEnable` | supported_read | device confirmation as applicable |
| R069 | connectkeyprovider | `ConnectKeyHeatSourcesResource$Hybrid$ReminderLapsed` | `/heatSources/hybrid/reminderLapsed` | supported_read | device confirmation as applicable |
| R070 | connectkeyprovider | `ConnectKeyHeatSourcesResource$Info` | `/heatSources/info` | directory | device confirmation as applicable |
| R071 | connectkeyprovider | `ConnectKeyHeatSourcesResource$NumberOfStarts` | `/heatSources/hs1/numberOfStarts` | supported_read | device confirmation as applicable |
| R072 | connectkeyprovider | `ConnectKeyHeatSourcesResource$PassiveCooling$InflowTemp` | `/heatSources/passiveCooling/inflowTemp` | supported_read | device confirmation as applicable |
| R073 | connectkeyprovider | `ConnectKeyHeatSourcesResource$PvContactState` | `/heatSources/pvContactState` | supported_read | device confirmation as applicable |
| R074 | connectkeyprovider | `ConnectKeyHeatSourcesResource$ReturnTemperature` | `/heatSources/returnTemperature` | supported_read | device confirmation as applicable |
| R075 | connectkeyprovider | `ConnectKeyHeatSourcesResource$StandbyMode` | `/heatSources/standbyMode` | supported_read | device confirmation as applicable |
| R076 | connectkeyprovider | `ConnectKeyHeatSourcesResource$SystemPressure` | `/heatSources/systemPressure` | supported_read | device confirmation as applicable |
| R077 | connectkeyprovider | `ConnectKeyHeatSourcesResource$SystemPressureRange` | `/heatSources/systemPressureRange` | missing_contract | P03 |
| R078 | connectkeyprovider | `ConnectKeyHeatSourcesResource$Type` | `/heatSources/hs1/type` | supported_read | device confirmation as applicable |
| R079 | connectkeyprovider | `ConnectKeyHeatSourcesResource$WorkingTime$TotalSystem` | `/heatSources/workingTime/totalSystem` | supported_read | device confirmation as applicable |
| R080 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$ActiveSwitchProgram` | `/heatingCircuits/hc{circuitNumber}/activeSwitchProgram` | supported_read | device confirmation as applicable |
| R081 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$ActualHumidity` | `/heatingCircuits/hc{circuitNumber}/actualHumidity` | supported_read | device confirmation as applicable |
| R082 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$BoostDuration` | `/heatingCircuits/hc{circuitNumber}/boostDuration` | supported_read | device confirmation as applicable |
| R083 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$BoostMode` | `/heatingCircuits/hc{circuitNumber}/boostMode` | supported_read | device confirmation as applicable |
| R084 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$BoostRemainingTime` | `/heatingCircuits/hc{circuitNumber}/boostRemainingTime` | supported_read | device confirmation as applicable |
| R085 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$BoostTemperature` | `/heatingCircuits/hc{circuitNumber}/boostTemperature` | supported_read | device confirmation as applicable |
| R086 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$ChName` | `/heatingCircuits/hc{circuitNumber}/name` | supported_read | device confirmation as applicable |
| R087 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$ControlType` | `/heatingCircuits/hc{circuitNumber}/controlType` | supported_read | device confirmation as applicable |
| R088 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$Cooling$ControlType` | `/heatingCircuits/hc{circuitNumber}/cooling/controlType` | supported_read | device confirmation as applicable |
| R089 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$Cooling$OperationMode` | `/heatingCircuits/hc{circuitNumber}/cooling/operationMode` | supported_read | device confirmation as applicable |
| R090 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$Cooling$RoomTempSetpoint` | `/heatingCircuits/hc{circuitNumber}/cooling/roomTempSetpoint` | supported_read | device confirmation as applicable |
| R091 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$Cooling$SwitchPrograms$A` | `/heatingCircuits/hc{circuitNumber}/cooling/switchPrograms/A` | missing_contract | P07 |
| R092 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$Cooling$TemperatureLevels$On` | `/heatingCircuits/hc{circuitNumber}/cooling/temperatureLevels/on` | supported_read | device confirmation as applicable |
| R093 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$CurrentRoomSetpoint` | `/heatingCircuits/hc{circuitNumber}/currentRoomSetpoint` | supported_read | device confirmation as applicable |
| R094 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$CurrentSuWiMode` | `/heatingCircuits/hc{circuitNumber}/currentSuWiMode` | supported_read | device confirmation as applicable |
| R095 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$HeatCoolMode` | `/heatingCircuits/hc{circuitNumber}/heatCoolMode` | supported_read | device confirmation as applicable |
| R096 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$HeatingType` | `/heatingCircuits/hc{circuitNumber}/heatingType` | supported_read | device confirmation as applicable |
| R097 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$ManualRoomSetpoint` | `/heatingCircuits/hc{circuitNumber}/manualRoomSetpoint` | supported_read | device confirmation as applicable |
| R098 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$OperationMode` | `/heatingCircuits/hc{circuitNumber}/operationMode` | supported_read | device confirmation as applicable |
| R099 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$OverallStatus` | `/heatingCircuits/hc{circuitNumber}/overallStatus` | supported_read | device confirmation as applicable |
| R100 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$RoomTemperature` | `/heatingCircuits/hc{circuitNumber}/roomtemperature` | supported_read | device confirmation as applicable |
| R101 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$SetpointOptimization` | `/heatingCircuits/hc{circuitNumber}/setpointOptimization` | supported_read | device confirmation as applicable |
| R102 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$SuWiCoolingThreshold` | `/heatingCircuits/hc{circuitNumber}/suWiCoolingThreshold` | supported_read | device confirmation as applicable |
| R103 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$SuWiSwitchMode` | `/heatingCircuits/hc{circuitNumber}/suWiSwitchMode` | supported_read | device confirmation as applicable |
| R104 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$SuWiThreshold` | `/heatingCircuits/hc{circuitNumber}/suWiThreshold` | supported_read | device confirmation as applicable |
| R105 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$SwitchProgram$A` | `/heatingCircuits/hc{circuitNumber}/switchPrograms/A` | supported_read | read only; other models and editing |
| R106 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$SwitchProgram$B` | `/heatingCircuits/hc{circuitNumber}/switchPrograms/B` | missing_contract | P07 |
| R107 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$SwitchProgram$NameA` | `/heatingCircuits/hc{circuitNumber}/switchPrograms/nameA` | supported_read | device confirmation as applicable |
| R108 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$SwitchProgram$NameB` | `/heatingCircuits/hc{circuitNumber}/switchPrograms/nameB` | supported_read | device confirmation as applicable |
| R109 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$SwitchProgramMode` | `/heatingCircuits/hc{circuitNumber}/switchProgramMode` | supported_read | device confirmation as applicable |
| R110 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$TemperatureLevels$Comfort2` | `/heatingCircuits/hc{circuitNumber}/temperatureLevels/comfort2` | supported_read | device confirmation as applicable |
| R111 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$TemperatureLevels$Eco` | `/heatingCircuits/hc{circuitNumber}/temperatureLevels/eco` | supported_read | device confirmation as applicable |
| R112 | connectkeyprovider | `ConnectKeyHeatingCircuitResource$TemporaryRoomSetpoint` | `/heatingCircuits/hc{circuitNumber}/temporaryRoomSetpoint` | supported_read | device confirmation as applicable |
| R113 | connectkeyprovider | `ConnectKeyHolidayModeResource$DeleteHolidayMode` | `/holidayMode/{holidayModeId}` | supported_transaction | device confirmation as applicable |
| R114 | connectkeyprovider | `ConnectKeyHolidayModeResource$HolidayActiveModes` | `/holidayMode/activeModes` | directory | device confirmation as applicable |
| R115 | connectkeyprovider | `ConnectKeyHolidayModeResource$HolidayConfiguration` | `/holidayMode/configuration` | directory | device confirmation as applicable |
| R116 | connectkeyprovider | `ConnectKeyHolidayModeResource$HolidayList` | `/holidayMode/list` | directory | device confirmation as applicable |
| R117 | connectkeyprovider | `ConnectKeyHolidayModeResource$PostHolidayMode` | `/holidayMode` | supported_transaction | device confirmation as applicable |
| R118 | connectkeyprovider | `ConnectKeyNotificationsResource$Notifications` | `/notifications` | directory | device confirmation as applicable |
| R119 | connectkeyprovider | `ConnectKeyPoolResource$AdditionalHeater$PoolMode` | `/pool/additionalHeater/poolMode` | supported_read | device confirmation as applicable |
| R120 | connectkeyprovider | `ConnectKeyPoolResource$CurrentTemp` | `/pool/currentTemp` | supported_read | device confirmation as applicable |
| R121 | connectkeyprovider | `ConnectKeyPoolResource$Enabled` | `/pool/enabled` | supported_read | device confirmation as applicable |
| R122 | connectkeyprovider | `ConnectKeyPoolResource$SetpointTemp` | `/pool/setpointTemp` | supported_read | device confirmation as applicable |
| R123 | connectkeyprovider | `ConnectKeyRecordingsResource$EmonTotalBurner` | `/recordings/heatSources/emon/total/burner?interval={interval}` | history | P15 |
| R124 | connectkeyprovider | `ConnectKeyRecordingsResource$EmonTotalElectricity` | `/recordings/heatSources/emon/total/electricity?interval={interval}` | history | P15 |
| R125 | connectkeyprovider | `ConnectKeyRecordingsResource$EmonTotalOutputProduced` | `/recordings/heatSources/emon/total/outputProduced?interval={interval}` | history | P15 |
| R126 | connectkeyprovider | `ConnectKeyRecordingsResource$EmonTotalOutputProducedHsX` | `/recordings/heatSources/hs{hsNumber}/emon/total/outputProduced?interval={interval}` | history | P15 |
| R127 | connectkeyprovider | `ConnectKeyRecordingsResource$EmonTotalSolar` | `/recordings/heatSources/emon/total/solar?interval={interval}` | history | P15 |
| R128 | connectkeyprovider | `ConnectKeySolarCircuitsResource$CollectorTemperature` | `/solarCircuits/sc1/collectorTemperature` | supported_read | device confirmation as applicable |
| R129 | connectkeyprovider | `ConnectKeySolarCircuitsResource$DhwTankBottomTemperature` | `/solarCircuits/sc1/dhwTankBottomTemperature` | supported_read | device confirmation as applicable |
| R130 | connectkeyprovider | `ConnectKeySolarCircuitsResource$MaxCylinderTemperature` | `/solarCircuits/sc1/maxCylinderTemperature` | supported_read | device confirmation as applicable |
| R131 | connectkeyprovider | `ConnectKeySolarCircuitsResource$MaxTemperatureReached` | `/solarCircuits/sc1/maxTemperatureReached` | supported_read | device confirmation as applicable |
| R132 | connectkeyprovider | `ConnectKeySolarCircuitsResource$PumpModulation` | `/solarCircuits/sc1/pumpModulation` | supported_read | device confirmation as applicable |
| R133 | connectkeyprovider | `ConnectKeySolarCircuitsResource$SolarYield` | `/solarCircuits/sc1/solarYield` | supported_read | device confirmation as applicable |
| R134 | connectkeyprovider | `ConnectKeySystemResource$AwayMode` | `/system/awayMode/enabled` | supported_read | device confirmation as applicable |
| R135 | connectkeyprovider | `ConnectKeySystemResource$Brand` | `/system/brand` | supported_read | device confirmation as applicable |
| R136 | connectkeyprovider | `ConnectKeySystemResource$Bus` | `/system/bus` | supported_read | device confirmation as applicable |
| R137 | connectkeyprovider | `ConnectKeySystemResource$Country` | `/system/country` | excluded | device confirmation as applicable |
| R138 | connectkeyprovider | `ConnectKeySystemResource$DateTime` | `/system/dateTime` | excluded | device confirmation as applicable |
| R139 | connectkeyprovider | `ConnectKeySystemResource$EnergyTariff$Electricity` | `/system/energyTariff/electricity` | supported_read | device confirmation as applicable |
| R140 | connectkeyprovider | `ConnectKeySystemResource$EnergyTariff$Gas` | `/system/energyTariff/gas` | supported_read | device confirmation as applicable |
| R141 | connectkeyprovider | `ConnectKeySystemResource$EnergyTariff$Oil` | `/system/energyTariff/oil` | supported_read | device confirmation as applicable |
| R142 | connectkeyprovider | `ConnectKeySystemResource$EnergyTariff$Pv` | `/system/energyTariff/pv` | supported_read | device confirmation as applicable |
| R143 | connectkeyprovider | `ConnectKeySystemResource$GlobalSeasonOptimizer$CurrentMode` | `/system/globalSeasonOptimizer/currentMode` | supported_read | device confirmation as applicable |
| R144 | connectkeyprovider | `ConnectKeySystemResource$Info` | `/system/info` | excluded | device confirmation as applicable |
| R145 | connectkeyprovider | `ConnectKeySystemResource$IsrcInstallationStatus` | `/system/iSRC/installationStatus` | supported_read | device confirmation as applicable |
| R146 | connectkeyprovider | `ConnectKeySystemResource$IsrcSupportStatus` | `/system/iSRC/supportStatus` | supported_read | device confirmation as applicable |
| R147 | connectkeyprovider | `ConnectKeySystemResource$LowNoise$Duration` | `/system/lowNoise/duration` | missing_contract | P03 |
| R148 | connectkeyprovider | `ConnectKeySystemResource$LowNoise$Mode` | `/system/lowNoise/mode` | supported_read | device confirmation as applicable |
| R149 | connectkeyprovider | `ConnectKeySystemResource$PowerGuard` | `/system/powerGuard/active` | supported_read | device confirmation as applicable |
| R150 | connectkeyprovider | `ConnectKeySystemResource$PowerLimitation` | `/system/powerLimitation/active` | supported_read | device confirmation as applicable |
| R151 | connectkeyprovider | `ConnectKeySystemResource$SeasonOptimizer$CoolingThreshold` | `/system/seasonOptimizer/coolingThreshold` | supported_read | device confirmation as applicable |
| R152 | connectkeyprovider | `ConnectKeySystemResource$SeasonOptimizer$HeatingThreshold` | `/system/seasonOptimizer/heatingThreshold` | supported_read | device confirmation as applicable |
| R153 | connectkeyprovider | `ConnectKeySystemResource$SeasonOptimizer$Mode` | `/system/seasonOptimizer/mode` | supported_read | device confirmation as applicable |
| R154 | connectkeyprovider | `ConnectKeySystemResource$Sensors$TemperaturesOutdoorT1` | `/system/sensors/temperatures/outdoor_t1` | supported_read | device confirmation as applicable |
| R155 | connectkeyprovider | `ConnectKeySystemResource$SilentMode$Enabled` | `/system/silentMode/enabled` | supported_read | device confirmation as applicable |
| R156 | connectkeyprovider | `ConnectKeySystemResource$SilentMode$PowerReduction` | `/system/silentMode/powerReduction` | missing_contract | P03 |
| R157 | connectkeyprovider | `ConnectKeySystemResource$SilentMode$StartTime` | `/system/silentMode/startTime` | missing_contract | P03 |
| R158 | connectkeyprovider | `ConnectKeySystemResource$SilentMode$StopTime` | `/system/silentMode/stopTime` | missing_contract | P03 |
| R159 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$ChCurrentSetpoint` | `/system/variableTariff/ch/currentSetpoint` | supported_read | device confirmation as applicable |
| R160 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$ChHighPriceDelta` | `/system/variableTariff/ch/highPriceDelta` | supported_read | device confirmation as applicable |
| R161 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$ChLowPriceDelta` | `/system/variableTariff/ch/lowPriceDelta` | supported_read | device confirmation as applicable |
| R162 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$ChMidPriceSetpoint` | `/system/variableTariff/ch/midPriceSetpoint` | supported_read | device confirmation as applicable |
| R163 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$ChOptimization` | `/system/variableTariff/ch/optimization` | supported_read | device confirmation as applicable |
| R164 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$ChStatus` | `/system/variableTariff/ch/status` | supported_read | device confirmation as applicable |
| R165 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$CurrentPriceCategorization` | `/system/variableTariff/currentPriceCatagorization` | supported_read | device confirmation as applicable |
| R166 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$DhwCurrentOpMode` | `/system/variableTariff/dhw/currentOpmode` | supported_read | device confirmation as applicable |
| R167 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$DhwHighPriceEnable` | `/system/variableTariff/dhw/highPriceEnable` | supported_read | device confirmation as applicable |
| R168 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$DhwLowPriceEnable` | `/system/variableTariff/dhw/lowPriceEnable` | supported_read | device confirmation as applicable |
| R169 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$DhwOptimization` | `/system/variableTariff/dhw/optimization` | supported_read | device confirmation as applicable |
| R170 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$DhwStatus` | `/system/variableTariff/dhw/status` | supported_read | device confirmation as applicable |
| R171 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$PriceInfo` | `/system/variableTariff/priceInfo` | missing_contract | P03 |
| R172 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$SupportStatus` | `/system/variableTariff/supportStatus` | missing_contract | P03 |
| R173 | connectkeyprovider | `ConnectKeySystemResource$VariableTariff$TariffId` | `/system/variableTariff/tariffId` | supported_read | device confirmation as applicable |
| R174 | connectkeyprovider | `ConnectKeyVentilationResource$ExhaustFanLevel` | `/ventilation/zone1/exhaustFanLevel` | supported_read | device confirmation as applicable |
| R175 | connectkeyprovider | `ConnectKeyVentilationResource$FilterRemainingTime` | `/ventilation/zone1/filter/remainingTime` | supported_read | device confirmation as applicable |
| R176 | connectkeyprovider | `ConnectKeyVentilationResource$MaxIndoorAirQuality` | `/ventilation/zone1/maxIndoorAirQuality` | supported_read | device confirmation as applicable |
| R177 | connectkeyprovider | `ConnectKeyVentilationResource$MaxRelativeHumidity` | `/ventilation/zone1/maxRelativeHumidity` | supported_read | device confirmation as applicable |
| R178 | connectkeyprovider | `ConnectKeyVentilationResource$OperationMode` | `/ventilation/zone1/operationMode` | supported_read | device confirmation as applicable |
| R179 | connectkeyprovider | `ConnectKeyVentilationResource$SensorsSupplyTemp` | `/ventilation/zone1/sensors/supplyTemp` | supported_read | device confirmation as applicable |
| R180 | connectkeyprovider | `ConnectKeyVentilationResource$SwitchProgram` | `/ventilation/zone1/switchPrograms/cp` | missing_contract | P07 |
| R181 | connectkeyprovider | `ConnectKeyVentilationResource$VentilationLevels` | `/ventilation/zone1/ventilationLevels` | missing_contract | P03 |
| R182 | connectkeyprovider | `ConnectKeyZonesResource$AverageActualHumidity` | `/zones/zone{zoneNumber}/averageActualHumidity` | supported_read | P05 |
| R183 | connectkeyprovider | `ConnectKeyZonesResource$AverageCurrentTemperature` | `/zones/zone{zoneNumber}/averageCurrentTemperature` | supported_read | P05 |
| R184 | connectkeyprovider | `ConnectKeyZonesResource$ChildLock` | `/zones/zone{zoneNumber}/childLock` | supported_read | P05 |
| R185 | connectkeyprovider | `ConnectKeyZonesResource$Configuration` | `/zones/configuration` | directory | device confirmation as applicable |
| R186 | connectkeyprovider | `ConnectKeyZonesResource$Cooling$ManualRoomSetpoint` | `/zones/zone{zoneNumber}/cooling/manualRoomSetpoint` | supported_control | device confirmation as applicable |
| R187 | connectkeyprovider | `ConnectKeyZonesResource$Cooling$OperationMode` | `/zones/zone{zoneNumber}/cooling/operationMode` | supported_control | device confirmation as applicable |
| R188 | connectkeyprovider | `ConnectKeyZonesResource$Cooling$TemporaryRoomSetpoint` | `/zones/zone{zoneNumber}/cooling/temporaryRoomSetpoint` | supported_read | P05 |
| R189 | connectkeyprovider | `ConnectKeyZonesResource$CurrentRoomSetpoint` | `/zones/zone{zoneNumber}/currentRoomSetpoint` | supported_read | P05 |
| R190 | connectkeyprovider | `ConnectKeyZonesResource$Heating$ManualRoomSetpoint` | `/zones/zone{zoneNumber}/heating/manualRoomSetpoint` | supported_control | device confirmation as applicable |
| R191 | connectkeyprovider | `ConnectKeyZonesResource$Heating$OperationMode` | `/zones/zone{zoneNumber}/heating/operationMode` | supported_control | device confirmation as applicable |
| R192 | connectkeyprovider | `ConnectKeyZonesResource$Heating$TemporaryRoomSetpoint` | `/zones/zone{zoneNumber}/heating/temporaryRoomSetpoint` | supported_read | P05 |
| R193 | connectkeyprovider | `ConnectKeyZonesResource$SwitchPrograms$Cooling` | `/zones/zone{zoneNumber}/switchPrograms/cooling` | missing_contract | P07 |
| R194 | connectkeyprovider | `ConnectKeyZonesResource$SwitchPrograms$Heating` | `/zones/zone{zoneNumber}/switchPrograms/heating` | missing_contract | P07 |
| R195 | connectkeyprovider | `ConnectKeyZonesResource$ZonesList` | `/zones/list` | directory | device confirmation as applicable |
| R196 | connectkeyprovider | `DhwCircuits` | `/dhwCircuits` | directory | device confirmation as applicable |
| R197 | connectkeyprovider | `HeatingCircuits` | `/heatingCircuits` | directory | device confirmation as applicable |
| R198 | connectkeyprovider | `SolarCircuits` | `/solarCircuits` | directory | device confirmation as applicable |
| R199 | icomprovider | `IcomCircuitsResource$Dhw` | `/dhwCircuits` | directory | device confirmation as applicable |
| R200 | icomprovider | `IcomCircuitsResource$Heating` | `/heatingCircuits` | directory | device confirmation as applicable |
| R201 | icomprovider | `IcomCircuitsResource$Solar` | `/solarCircuits` | directory | device confirmation as applicable |
| R202 | icomprovider | `IcomCoolingCircuitsResource$RoomTempSetpoint` | `/heatingCircuits/hc{circuitNumber}/cooling/roomTempSetpoint` | supported_read | device confirmation as applicable |
| R203 | icomprovider | `IcomDhwCircuitsResource$ActualTemperature` | `/dhwCircuits/dhw{circuitNumber}/actualTemp` | supported_read | device confirmation as applicable |
| R204 | icomprovider | `IcomDhwCircuitsResource$ExtraDhwDuration` | `/dhwCircuits/dhw{circuitNumber}/chargeDuration` | supported_read | device confirmation as applicable |
| R205 | icomprovider | `IcomDhwCircuitsResource$ExtraDhwSetpoint` | `/dhwCircuits/dhw{circuitNumber}/singleChargeSetpoint` | supported_read | device confirmation as applicable |
| R206 | icomprovider | `IcomDhwCircuitsResource$ExtraDhwState` | `/dhwCircuits/dhw{circuitNumber}/charge` | supported_read | device confirmation as applicable |
| R207 | icomprovider | `IcomDhwCircuitsResource$HolidayModeActivated` | `/dhwCircuits/dhw{circuitNumber}/holidayMode/activated` | missing_contract | P07 |
| R208 | icomprovider | `IcomDhwCircuitsResource$OperationMode` | `/dhwCircuits/dhw{circuitNumber}/operationMode` | supported_read | device confirmation as applicable |
| R209 | icomprovider | `IcomDhwCircuitsResource$SwitchProgram` | `/dhwCircuits/dhw{circuitNumber}/switchPrograms/A` | missing_contract | P07 |
| R210 | icomprovider | `IcomDhwCircuitsResource$TemperatureLevelHigh` | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels/high` | supported_read | device confirmation as applicable |
| R211 | icomprovider | `IcomDhwCircuitsResource$TemperatureLevelLow` | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels/low` | supported_read | device confirmation as applicable |
| R212 | icomprovider | `IcomDhwCircuitsResource$TemperatureLevels` | `/dhwCircuits/dhw{circuitNumber}/temperatureLevels` | missing_contract | P03 |
| R213 | icomprovider | `IcomGatewayResource$DateTime` | `/gateway/DateTime` | excluded | device confirmation as applicable |
| R214 | icomprovider | `IcomGatewayResource$Uuid` | `/gateway/uuid` | excluded | device confirmation as applicable |
| R215 | icomprovider | `IcomGatewayResource$VersionFirmware` | `/gateway/versionFirmware` | supported_read | device confirmation as applicable |
| R216 | icomprovider | `IcomHeatSourcesResource$HsType` | `/heatSources/hs1/type` | supported_read | device confirmation as applicable |
| R217 | icomprovider | `IcomHeatSourcesResource$Info` | `/heatSources/info` | directory | device confirmation as applicable |
| R218 | icomprovider | `IcomHeatingCircuitsResource$ActiveSwitchProgram` | `/heatingCircuits/hc{circuitNumber}/activeSwitchProgram` | supported_read | device confirmation as applicable |
| R219 | icomprovider | `IcomHeatingCircuitsResource$ControlType` | `/heatingCircuits/hc{circuitNumber}/controlType` | supported_read | device confirmation as applicable |
| R220 | icomprovider | `IcomHeatingCircuitsResource$CurrentRoomSetpoint` | `/heatingCircuits/hc{circuitNumber}/currentRoomSetpoint` | supported_read | device confirmation as applicable |
| R221 | icomprovider | `IcomHeatingCircuitsResource$CurrentSuWiMode` | `/heatingCircuits/hc{circuitNumber}/currentSuWiMode` | supported_read | device confirmation as applicable |
| R222 | icomprovider | `IcomHeatingCircuitsResource$HolidayModeActivated` | `/heatingCircuits/hc{circuitNumber}/holidayMode/activated` | missing_contract | P07 |
| R223 | icomprovider | `IcomHeatingCircuitsResource$ManualRoomSetpoint` | `/heatingCircuits/hc{circuitNumber}/manualRoomSetpoint` | supported_read | device confirmation as applicable |
| R224 | icomprovider | `IcomHeatingCircuitsResource$OperationMode` | `/heatingCircuits/hc{circuitNumber}/operationMode` | supported_read | device confirmation as applicable |
| R225 | icomprovider | `IcomHeatingCircuitsResource$RoomTemperature` | `/heatingCircuits/hc{circuitNumber}/roomtemperature` | supported_read | device confirmation as applicable |
| R226 | icomprovider | `IcomHeatingCircuitsResource$SuWiSwitchMode` | `/heatingCircuits/hc{circuitNumber}/suWiSwitchMode` | supported_read | device confirmation as applicable |
| R227 | icomprovider | `IcomHeatingCircuitsResource$SwitchProgramA` | `/heatingCircuits/hc{circuitNumber}/switchPrograms/A` | missing_contract | P07 |
| R228 | icomprovider | `IcomHeatingCircuitsResource$SwitchProgramB` | `/heatingCircuits/hc{circuitNumber}/switchPrograms/B` | missing_contract | P07 |
| R229 | icomprovider | `IcomHeatingCircuitsResource$SwitchProgramMode` | `/heatingCircuits/hc{circuitNumber}/switchProgramMode` | supported_read | device confirmation as applicable |
| R230 | icomprovider | `IcomHeatingCircuitsResource$TemperatureLevels` | `/heatingCircuits/hc{circuitNumber}/temperatureLevels` | missing_contract | P03 |
| R231 | icomprovider | `IcomHeatingCircuitsResource$TemperatureLevelsComfort` | `/heatingCircuits/hc{circuitNumber}/temperatureLevels/comfort2` | supported_read | device confirmation as applicable |
| R232 | icomprovider | `IcomHeatingCircuitsResource$TemperatureLevelsEco` | `/heatingCircuits/hc{circuitNumber}/temperatureLevels/eco` | supported_read | device confirmation as applicable |
| R233 | icomprovider | `IcomHeatingCircuitsResource$TemporaryRoomSetpoint` | `/heatingCircuits/hc{circuitNumber}/temporaryRoomSetpoint` | supported_read | device confirmation as applicable |
| R234 | icomprovider | `IcomHolidayModesResource$DhwMode` | `/system/holidayModes/hm{holidayNumber}/dhwMode` | missing_contract | P07 |
| R235 | icomprovider | `IcomHolidayModesResource$FixTemperature` | `/system/holidayModes/hm{holidayNumber}/fixTemperature` | missing_contract | P07 |
| R236 | icomprovider | `IcomHolidayModesResource$HcMode` | `/system/holidayModes/hm{holidayNumber}/hcMode` | missing_contract | P07 |
| R237 | icomprovider | `IcomHolidayModesResource$StartStop` | `/system/holidayModes/hm{holidayNumber}/startStop` | missing_contract | P07 |
| R238 | icomprovider | `IcomNotificationsResource$Notifications` | `/notifications` | directory | device confirmation as applicable |
| R239 | icomprovider | `IcomSystemResource$BusType` | `/system/bus` | supported_read | device confirmation as applicable |
| R240 | icomprovider | `IcomSystemResource$Country` | `/system/busReq?emsData=0804840401` | excluded | device confirmation as applicable |
| R241 | icomprovider | `IcomSystemResource$Info` | `/system/info` | excluded | device confirmation as applicable |
| R242 | icomprovider | `IcomVentilationResource$ExhaustFanLevel` | `/ventilation/zone1/exhaustFanLevel` | supported_read | device confirmation as applicable |
| R243 | icomprovider | `IcomVentilationResource$OperationMode` | `/ventilation/zone1/operationMode` | supported_read | device confirmation as applicable |
| R244 | rrcprovider | `ActualTemperature` | `/dhwCircuits/dhw{circuitId}/actualTemp` | supported_read | device confirmation as applicable |
| R245 | rrcprovider | `AwayMode` | `/system/awayMode/enabled` | supported_read | device confirmation as applicable |
| R246 | rrcprovider | `AwayTemperature` | `/system/awayMode/temperature` | supported_read | device confirmation as applicable |
| R247 | rrcprovider | `ClockProgram` | `/zones/zn{zoneId}/clockProgram` | supported_read | device confirmation as applicable |
| R248 | rrcprovider | `ControlKey` | `/heatingCircuits/hc{circuitId}/control` | missing_contract | P06 |
| R249 | rrcprovider | `FirmwareVersion` | `/gateway/versionFirmware` | supported_read | device confirmation as applicable |
| R250 | rrcprovider | `HeatSourceType` | `/heatSources/type` | supported_read | device confirmation as applicable |
| R251 | rrcprovider | `HmipDeviceList` | `/devices/list` | directory | device confirmation as applicable |
| R252 | rrcprovider | `HmipDeviceName` | `/devices/dev{devId}/name` | missing_contract | P06 |
| R253 | rrcprovider | `HmipDeviceZone` | `/devices/device{devId}/zone` | missing_contract | P06 |
| R254 | rrcprovider | `HotWaterOperationMode` | `/dhwCircuits/dhw{circuitId}/operationMode` | supported_read | device confirmation as applicable |
| R255 | rrcprovider | `HotWaterSystem` | `/dhwCircuits/dhw{circuitId}/hotWaterSystem` | missing_contract | P06 |
| R256 | rrcprovider | `HotWaterWeeklyPrograms` | `/dhwCircuits/dhw{circuitId}/programs/pg{programId}/week` | missing_contract | P06 |
| R257 | rrcprovider | `Icon` | `/zones/zn{zoneId}/icon` | missing_contract | P06 |
| R258 | rrcprovider | `Icons` | `/gateway/ui/icons` | missing_contract | P06 |
| R259 | rrcprovider | `Info` | `/heatSources/info` | directory | device confirmation as applicable |
| R260 | rrcprovider | `Location` | `/system/location/coordinates` | excluded | device confirmation as applicable |
| R261 | rrcprovider | `Name` | `/zones/zn{zoneId}/name` | missing_contract | P06 |
| R262 | rrcprovider | `OpenWindowDetection` | `/zones/zn{zoneId}/openWindowDetection/enabled` | missing_contract | P06 |
| R263 | rrcprovider | `RrcChClockProgramDayResource` | `/programs/pg{programId}/{dayOfWeek}` | supported_transaction | P06 |
| R264 | rrcprovider | `RrcChClockProgramNameResource` | `/programs/pg{programId}/name` | missing_contract | P06 |
| R265 | rrcprovider | `RrcChClockProgramResource` | `/programs/pg{programId}` | directory | device confirmation as applicable |
| R266 | rrcprovider | `RrcChClockProgramsResource` | `/programs/list` | directory | device confirmation as applicable |
| R267 | rrcprovider | `RrcChClockWeekProgramResource` | `/programs/pg{programId}/week` | supported_transaction | P06 |
| R268 | rrcprovider | `RrcChPrograms` | `/programs` | directory | device confirmation as applicable |
| R269 | rrcprovider | `Steps` | `/gateway/wizardStepsDone` | missing_contract | P06 |
| R270 | rrcprovider | `Time` | `/gateway/time/current` | missing_contract | P06 |
| R271 | rrcprovider | `TimeZone` | `/gateway/time/timeZone` | missing_contract | P06 |
| R272 | rrcprovider | `UserMode` | `/zones/zn{zoneId}/userMode` | supported_read | device confirmation as applicable |
| R273 | rrcprovider | `Uuid` | `/gateway/uuid` | excluded | device confirmation as applicable |
| R274 | rrcprovider | `Zone` | `/zones/zn{zoneId}` | directory | device confirmation as applicable |
| R275 | rrcprovider | `ZoneClockOverrideTemperature` | `/zones/zn{zoneId}/clockOverride/temperatureHeating` | supported_read | P06 |
| R276 | rrcprovider | `ZoneList` | `/zones/list` | directory | device confirmation as applicable |
| R277 | rrcprovider | `ZoneManualTemperature` | `/zones/zn{zoneId}/manualTemperatureHeating` | supported_read | device confirmation as applicable |
| R278 | rrcprovider | `ZoneTemperatureActual` | `/zones/zn{zoneId}/temperatureActual` | supported_read | device confirmation as applicable |
| R279 | rrcprovider | `ZoneTemperatureHeatingSetpoint` | `/zones/zn{zoneId}/temperatureHeatingSetpoint` | supported_read | device confirmation as applicable |
| R280 | rrcprovider | `Zones` | `/zones` | directory | device confirmation as applicable |
| R281 | watersoftenerprovider | `WaterSoftenerApplianceResource$ApplianceVersion` | `/system/appliance/versionFirmware` | supported_read | device confirmation as applicable |
| R282 | watersoftenerprovider | `WaterSoftenerApplianceResource$ModelType` | `/system/appliance/model` | supported_read | device confirmation as applicable |
| R283 | watersoftenerprovider | `WaterSoftenerExecuteResource` | `None` | separate_package | P04 |
| R284 | watersoftenerprovider | `WaterSoftenerConfiguringResource$FactoryReset` | `/gateway/factoryReset` | excluded | device confirmation as applicable |
| R285 | watersoftenerprovider | `WaterSoftenerConfiguringResource$UpdateTriggerRequest` | `/gateway/update/triggerRequest` | excluded | device confirmation as applicable |
| R286 | watersoftenerprovider | `WaterSoftenerGatewayResource$DateTime` | `/gateway/DateTime` | excluded | device confirmation as applicable |
| R287 | watersoftenerprovider | `WaterSoftenerGatewayResource$SerialId` | `/gateway/serialId` | excluded | device confirmation as applicable |
| R288 | watersoftenerprovider | `WaterSoftenerGatewayResource$TimeZone` | `/gateway/tzInfo/timeZone` | supported_read | device confirmation as applicable |
| R289 | watersoftenerprovider | `WaterSoftenerGatewayResource$Uuid` | `/gateway/uuid` | excluded | device confirmation as applicable |
| R290 | watersoftenerprovider | `WaterSoftenerGatewayResource$VersionFirmware` | `/gateway/versionFirmware` | supported_read | device confirmation as applicable |
| R291 | watersoftenerprovider | `WaterSoftenerGatewayResource$VersionHardware` | `/gateway/versionHardware` | supported_read | device confirmation as applicable |
| R292 | watersoftenerprovider | `WaterSoftenerGatewayResource$WifiMac` | `/gateway/wifi/mac` | excluded | device confirmation as applicable |
| R293 | watersoftenerprovider | `WaterSoftenerGatewayResource$WifiSsid` | `/gateway/wifi/ssid` | excluded | device confirmation as applicable |
| R294 | watersoftenerprovider | `WaterSoftenerHolidayResource$HolidayMode` | `/swCircuits/sw1/holidayMode/enabled` | supported_read | device confirmation as applicable |
| R295 | watersoftenerprovider | `WaterSoftenerRecordingsResource$DailyRecordingsResource` | `/recordings/swCircuits/sw1/sensor/water?interval=31D` | history | P15 |
| R296 | watersoftenerprovider | `WaterSoftenerRecordingsResource$MonthlyRecordingsResource` | `/recordings/swCircuits/sw1/sensor/water?interval=24M` | history | P15 |
| R297 | watersoftenerprovider | `WaterSoftenerRegenerationResource$RegenerationEnabled` | `/swCircuits/sw1/regeneration/enabled` | supported_read | device confirmation as applicable |
| R298 | watersoftenerprovider | `WaterSoftenerRegenerationResource$RegenerationPhase` | `/swCircuits/sw1/regeneration/phase` | supported_read | device confirmation as applicable |
| R299 | watersoftenerprovider | `WaterSoftenerRegenerationResource$RegenerationSchedule` | `/swCircuits/sw1/regeneration/singleRegAppoint` | supported_read | device confirmation as applicable |
| R300 | watersoftenerprovider | `WaterSoftenerRegenerationResource$RegenerationTimeUntilNext` | `/swCircuits/sw1/regeneration/timeUntilNext` | supported_read | device confirmation as applicable |
| R301 | watersoftenerprovider | `WaterSoftenerRegenerationResource$RegenerationTotalProgressStatus` | `/swCircuits/sw1/regeneration/totalProgressStatus` | supported_read | device confirmation as applicable |
| R302 | watersoftenerprovider | `WaterSoftenerRegenerationResource$RegenerationTotalRemainingTime` | `/swCircuits/sw1/regeneration/totalRemainingTime` | supported_read | device confirmation as applicable |
| R303 | watersoftenerprovider | `WaterSoftenerRegenerationResource$RegenerationsLog` | `/swCircuits/sw1/regeneration/log` | history | P15 |
| R304 | watersoftenerprovider | `WaterSoftenerSensorResource$AvailableWater` | `/swCircuits/sw1/sensor/availableWater` | supported_read | device confirmation as applicable |
| R305 | watersoftenerprovider | `WaterSoftenerSensorResource$SaltLevel` | `/swCircuits/sw1/sensor/saltLevel` | supported_read | device confirmation as applicable |
| R306 | watersoftenerprovider | `WaterSoftenerSensorResource$SaltLevelAlarm` | `/swCircuits/sw1/sensor/saltLevelAlarm` | supported_read | device confirmation as applicable |
| R307 | watersoftenerprovider | `WaterSoftenerSensorResource$WaterFlow` | `/swCircuits/sw1/sensor/waterFlow` | supported_read | device confirmation as applicable |
| R308 | watersoftenerprovider | `WaterSoftenerSystemResource$Brand` | `/system/brand` | supported_read | device confirmation as applicable |
| R309 | watersoftenerprovider | `WaterSoftenerSystemResource$Notifications` | `/notifications` | directory | device confirmation as applicable |
| R310 | wddw2provider | `ActualPower` | `/heatSources/hs1/actualPower` | supported_read | device confirmation as applicable |
| R311 | wddw2provider | `AirBoxTemperature` | `/dhwCircuits/dhw1/sensor/airBoxTemperature` | supported_read | device confirmation as applicable |
| R312 | wddw2provider | `ApplianceVersion` | `/system/appliance/versionFirmware` | supported_read | device confirmation as applicable |
| R313 | wddw2provider | `AtmosphericPressure` | `/dhwCircuits/dhw1/sensor/atmosphericPressure` | supported_read | device confirmation as applicable |
| R314 | wddw2provider | `Brand` | `/system/brand` | supported_read | device confirmation as applicable |
| R315 | wddw2provider | `BusType` | `/system/bus` | supported_read | device confirmation as applicable |
| R316 | wddw2provider | `Charge` | `/dhwCircuits/dhw1/charge` | supported_read | device confirmation as applicable |
| R317 | wddw2provider | `CurrentSetpoint` | `/dhwCircuits/dhw1/currentSetpoint` | supported_read | device confirmation as applicable |
| R318 | wddw2provider | `DateTime` | `/gateway/DateTime` | excluded | device confirmation as applicable |
| R319 | wddw2provider | `ElectricityRecordings` | `/recordings/heatSources/hs1/sensor/electricity?interval={interval}` | history | P15 |
| R320 | wddw2provider | `ElectricityTotalConsumption` | `/heatSources/electricityTotalConsumption` | supported_read | device confirmation as applicable |
| R321 | wddw2provider | `Enabled` | `/system/appliance/enabled` | supported_read | device confirmation as applicable |
| R322 | wddw2provider | `ExhaustFlueGasTemperature` | `/dhwCircuits/dhw1/sensor/exhaustFlueGasTemperature` | supported_read | device confirmation as applicable |
| R323 | wddw2provider | `ExternalTankTemperature` | `/dhwCircuits/dhw1/sensor/externalTankTemperature` | supported_read | device confirmation as applicable |
| R324 | wddw2provider | `Wddw2ExecuteResource` | `None` | separate_package | P07 |
| R325 | wddw2provider | `FactoryReset` | `/gateway/factoryReset` | excluded | device confirmation as applicable |
| R326 | wddw2provider | `FanSpeed` | `/dhwCircuits/dhw1/sensor/fanSpeed` | supported_read | device confirmation as applicable |
| R327 | wddw2provider | `GasFlow` | `/dhwCircuits/dhw1/sensor/gasFlow` | supported_read | device confirmation as applicable |
| R328 | wddw2provider | `GasRecordings` | `/recordings/heatSources/hs1/sensor/gas?interval={interval}` | history | P15 |
| R329 | wddw2provider | `GasTotalConsumption` | `/heatSources/gasTotalConsumption` | supported_read | device confirmation as applicable |
| R330 | wddw2provider | `HardwareFirmware` | `/gateway/versionHardware` | supported_read | device confirmation as applicable |
| R331 | wddw2provider | `HeatExchangerFlueGasTemperature` | `/dhwCircuits/dhw1/sensor/heatExchangerFlueGasTemperature` | supported_read | device confirmation as applicable |
| R332 | wddw2provider | `HeatExchangerTemperature` | `/dhwCircuits/dhw1/sensor/heatExchangerTemperature` | supported_read | device confirmation as applicable |
| R333 | wddw2provider | `HolidayMode` | `/system/holidayMode` | missing_contract | P07 |
| R334 | wddw2provider | `InletTemperature` | `/dhwCircuits/dhw1/inletTemperature` | supported_read | device confirmation as applicable |
| R335 | wddw2provider | `LearningWeek` | `/dhwCircuits/dhw1/learningWeek` | supported_read | device confirmation as applicable |
| R336 | wddw2provider | `ManualSetpoint` | `/dhwCircuits/dhw1/manualsetpoint` | supported_read | device confirmation as applicable |
| R337 | wddw2provider | `ModelType` | `/system/appliance/model` | supported_read | device confirmation as applicable |
| R338 | wddw2provider | `MonitorValues` | `/dhwCircuits/dhw1/monitorValues` | missing_contract | P03 |
| R339 | wddw2provider | `Notifications` | `/notifications` | directory | device confirmation as applicable |
| R340 | wddw2provider | `NumberOfShowersAvailable` | `/dhwCircuits/dhw1/numberOfShowersAvailable` | missing_contract | P03 |
| R341 | wddw2provider | `NumberOfStarts` | `/heatSources/hs1/numberOfStarts` | supported_read | device confirmation as applicable |
| R342 | wddw2provider | `OperationHours` | `/heatSources/hs1/operationHours` | supported_read | device confirmation as applicable |
| R343 | wddw2provider | `OperationMode` | `/dhwCircuits/dhw1/operationMode` | supported_read | device confirmation as applicable |
| R344 | wddw2provider | `OperationSetpoints` | `/dhwCircuits/dhw1/operationSetpoints` | missing_contract | P03 |
| R345 | wddw2provider | `OutletTemperature` | `/dhwCircuits/dhw1/outletTemperature` | supported_read | device confirmation as applicable |
| R346 | wddw2provider | `PowerPercentage` | `/heatSources/hs1/powerPercentage` | supported_read | device confirmation as applicable |
| R347 | wddw2provider | `Recirculation` | `/dhwCircuits/dhw1/recirculation/enabled` | supported_read | device confirmation as applicable |
| R348 | wddw2provider | `SafetyTemperature` | `/dhwCircuits/dhw1/safetyTemperature` | supported_read | device confirmation as applicable |
| R349 | wddw2provider | `SerialId` | `/gateway/serialId` | excluded | device confirmation as applicable |
| R350 | wddw2provider | `SwitchProgram` | `/dhwCircuits/dhw1/switchPrograms/program1` | missing_contract | P07 |
| R351 | wddw2provider | `SystemUnits` | `/system/systemOfUnits` | supported_read | device confirmation as applicable |
| R352 | wddw2provider | `ThermalDisinfection` | `/dhwCircuits/dhw1/tdMode` | supported_read | device confirmation as applicable |
| R353 | wddw2provider | `TimeZone` | `/gateway/tzInfo/timeZone` | supported_read | device confirmation as applicable |
| R354 | wddw2provider | `UpdateTriggerRequest` | `/gateway/update/triggerRequest` | excluded | device confirmation as applicable |
| R355 | wddw2provider | `Uuid` | `/gateway/uuid` | excluded | device confirmation as applicable |
| R356 | wddw2provider | `VersionFirmware` | `/gateway/versionFirmware` | supported_read | device confirmation as applicable |
| R357 | wddw2provider | `WaterFlow` | `/dhwCircuits/dhw1/sensor/waterFlow` | supported_read | device confirmation as applicable |
| R358 | wddw2provider | `WaterRecordings` | `/recordings/dhwCircuits/dhw1/sensor/water?interval={interval}` | history | P15 |
| R359 | wddw2provider | `WaterTotalConsumption` | `/dhwCircuits/waterTotalConsumption` | supported_read | device confirmation as applicable |
| R360 | wddw2provider | `WifiMac` | `/gateway/wifi/mac` | excluded | device confirmation as applicable |
| R361 | wddw2provider | `WifiSsid` | `/gateway/wifi/ssid` | excluded | device confirmation as applicable |
