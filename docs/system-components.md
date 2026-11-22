# System components

## English

A heating system reports a list of its components: the control unit of the
heat generator, the system controller, room units, modules and the gateway
itself. The cloud reports this list as `/system/info`, the local access as
`/system/basicInfo`. Each entry carries a type number (`Id`) and a place
(`Tok`):

- The **type number** names the kind of component, usually its control unit,
  not the sales model. One number can stand for several product lines, and it
  does not tell whether a heat pump uses air, brine or water. I never derive a
  model name or a heat source type from it.
- The **place** is the position of the component in the system, for example
  the control unit of the heat generator (8), the system controller (16), the
  remote control of heating circuit 1 (56) or the hybrid manager (83). Several
  components can share a place, for example the indoor and the outdoor unit of
  a heat pump at place 8.

The integration ships a table of these numbers, `system_components.json`. It
has a version of its own (`table_version`); every change of its content
raises it. The type numbers and names in this table are as used in the
manufacturer apps. Names are shown as they are and never translated. The
format of the table is described by the schema
[`schemas/system-components.v1.json`](schemas/system-components.v1.json).

### Component names

Type number 103 stands for two components; both names are shown. Type number
17 has no name; it is the row of the gateway itself. Type number 0 marks a row
without a valid type. A type number that is not in the table is shown with
its number only.

| Type number | Name | Basis |
|---|---|---|
| 0 | `INVALID` | `name` |
| 3 | `RT800` | `name` |
| 4 | `HMI800_3_FOSSIL` | `name` |
| 5 | `MR100` | `name` |
| 6 | `EMSMasterGateway` | `name` |
| 7 | `HMI550_CN` | `name` |
| 8 | `xCU_THH` | `observed` |
| 9 | `Hydrogen_burner_module` | `name` |
| 10 | `XLOW_MULTIPURPOSE` | `name` |
| 11 | `ACU_LOP23G` | `name` |
| 12 | `ACU_LOP22D` | `name` |
| 13 | `ACU_LOE` | `name` |
| 14 | `EEBUS_GW` | `name` |
| 15 | `EasyReceiver` | `name` |
| 16 | `XCU_CS` | `rule` |
| 17 | – | `observed` |
| 64 | `UBA3` | `name` |
| 65 | `BFE1_RC10` | `name` |
| 66 | `BFE2_RC20` | `name` |
| 67 | `BFA1_RC30` | `name` |
| 68 | `BCxx_BC10` | `name` |
| 69 | `MM10` | `name` |
| 70 | `GWP` | `name` |
| 71 | `WM10` | `name` |
| 72 | `MC10` | `name` |
| 73 | `SM10` | `name` |
| 74 | `EM10` | `name` |
| 75 | `SAFE` | `name` |
| 76 | `ES73` | `name` |
| 77 | `M300` | `name` |
| 78 | `M400` | `name` |
| 79 | `M100` | `name` |
| 80 | `M200` | `name` |
| 81 | `Cascade_KSE` | `name` |
| 82 | `CM10` | `name` |
| 83 | `VM10` | `name` |
| 84 | `Service_key` | `name` |
| 85 | `DBA` | `name` |
| 86 | `Easycom` | `name` |
| 87 | `RC34` | `name` |
| 88 | `Easycom_Pro` | `name` |
| 89 | `Gateway` | `name` |
| 90 | `BC10_USA` | `name` |
| 91 | `RC10_USA` | `name` |
| 92 | `RC20_USA` | `name` |
| 93 | `UM10` | `name` |
| 94 | `RC20F` | `name` |
| 95 | `RFM20` | `name` |
| 96 | `HTIII` | `name` |
| 97 | `RT10_Fx10` | `name` |
| 98 | `TD200_Fx100` | `name` |
| 99 | `AM10` | `name` |
| 100 | `ZR01` | `name` |
| 101 | `IPM1` | `name` |
| 102 | `ISM1` | `name` |
| 103 | `IPM2`, `ISM2` | `name` |
| 104 | `IUM1` | `name` |
| 105 | `FW100_Fx100` | `name` |
| 106 | `FW200_Fx100` | `name` |
| 107 | `FR100_Fx100` | `name` |
| 108 | `FR110_Fx100` | `name` |
| 109 | `FB10_Fx10` | `name` |
| 110 | `FB100_Fx100` | `name` |
| 111 | `FR10_Fx10` | `name` |
| 112 | `RC10V2` | `name` |
| 113 | `RC20V2` | `name` |
| 114 | `BC15` | `name` |
| 115 | `UBA3_50` | `name` |
| 116 | `FW500_Fx100` | `name` |
| 117 | `Modulink250_Receiver` | `name` |
| 118 | `BRM10` | `name` |
| 119 | `FR110F_RM` | `name` |
| 120 | `FR110F_EM` | `name` |
| 121 | `MCC_ICM` | `name` |
| 122 | `CBN_CUx_platform` | `name` |
| 123 | `CBS_CUx_platform` | `name` |
| 124 | `HIN_CUx_platform` | `name` |
| 125 | `HIS_Trim_Full_IO_CUx_platform` | `name` |
| 126 | `UBA_HT_Heatronic_for_Buderus_devices` | `name` |
| 127 | `IGM1` | `name` |
| 128 | `Buderus_4000_System` | `name` |
| 129 | `IEM` | `name` |
| 130 | `HRU_Heat_Recovery_Unit` | `name` |
| 131 | `MC100` | `name` |
| 132 | `MX25_Junkers` | `name` |
| 133 | `MC110_Buderus` | `name` |
| 147 | `FR50_Fx100` | `name` |
| 148 | `eS63_Room_Controller_Sieger` | `name` |
| 149 | `HKM63_Gateway_EMS_EBV_Modules` | `name` |
| 150 | `PM10` | `name` |
| 151 | `RC25` | `name` |
| 152 | `HIS_Skin_Mid_Combi_Full_CUx_platform` | `name` |
| 153 | `HIS_Skin_Low_Full_CUx_platform` | `name` |
| 154 | `CBS_Regular_CUx_platform` | `name` |
| 155 | `MX15_MC10_Junkers` | `name` |
| 156 | `eS79_RC35_Sieger` | `name` |
| 157 | `LOW` | `name` |
| 158 | `MID` | `name` |
| 159 | `HC_ClipIn` | `name` |
| 160 | `HC_A` | `name` |
| 161 | `HC_B` | `name` |
| 162 | `SOLAR_CLIPIN` | `name` |
| 163 | `SOLAR_A` | `name` |
| 164 | `SOLAR_B` | `name` |
| 165 | `xLow` | `name` |
| 166 | `Bx15_Junkers` | `name` |
| 167 | `CBS_Hybrid_CUx_platform` | `name` |
| 168 | `IHM` | `name` |
| 169 | `BC40_HIS_for_FB` | `name` |
| 170 | `MC40` | `name` |
| 171 | `EMS2OT_converter_slave` | `name` |
| 172 | `CUHP` | `name` |
| 189 | `iCOM_KM200_KM50` | `name` |
| 190 | `HIS_X_Line_CUx_platform` | `name` |
| 191 | `FR120` | `name` |
| 192 | `FW120` | `name` |
| 193 | `HC_Z` | `name` |
| 194 | `BC100_HMI_of_control_unit_MC100` | `name` |
| 195 | `ACU_MH` | `name` |
| 196 | `HMI_Skin_Top_based_on_CU_PL` | `name` |
| 197 | `HMI_Skin_Mid_based_on_CU_PL` | `name` |
| 198 | `NSC_Switch_A` | `name` |
| 199 | `xLow_Sensor_No_HMI` | `name` |
| 200 | `xLOW_HMDTY` | `name` |
| 201 | `Connected_Boiler_clip_in_IFP` | `name` |
| 202 | `RRC_Remote_Room_Controller` | `name` |
| 203 | `RRC2_Remote_Room_Controller2` | `name` |
| 204 | `NSC_Pool_Module` | `name` |
| 205 | `EMS2OT_converter_master` | `name` |
| 206 | `EMS2IRT_converter_UBA1_UBA1_5` | `name` |
| 207 | `MID_SOLAR` | `name` |
| 208 | `CU_DNA_1_0` | `name` |
| 209 | `HMI_Skin_Low_based_on_CU_DNA_1` | `name` |
| 210 | `NSC_MHSMC` | `name` |
| 211 | `EMS2ON_OFF_converter_master` | `name` |
| 212 | `NSC_BG_Clipin_RF` | `name` |
| 213 | `NSC_IO_Module` | `name` |
| 214 | `CR10M_RF` | `name` |
| 215 | `CR20D_RF` | `name` |
| 216 | `CR50E_RF` | `name` |
| 217 | `GHPSC` | `name` |
| 218 | `NSC_RF_Coordinator` | `name` |
| 219 | `HDU` | `name` |
| 220 | `HMI_Xs_belt` | `name` |
| 221 | `Fuel_Cell_CU` | `name` |
| 222 | `iCom_prof_Web_KM300` | `name` |
| 223 | `CMS_burner_module` | `name` |
| 224 | `DNA1_HMI` | `name` |
| 225 | `HPC_Florida_Heat_Pump` | `name` |
| 226 | `EMS_Dongle_EDO` | `name` |
| 227 | `LOW_RF` | `name` |
| 228 | `AHSM` | `name` |
| 229 | `DNA2_HMI_ST` | `name` |
| 230 | `DNA1_HMI_FB` | `name` |
| 231 | `HRV_CU` | `name` |
| 233 | `MID_HRV_Standalone` | `name` |
| 234 | `ACU_LO_Pneumatic_with_internal_HMI` | `name` |
| 235 | `TIMER_PLUX` | `name` |
| 236 | `eQ3_PLUX` | `name` |
| 237 | `BG_PLUX` | `name` |
| 238 | `DeltaDore_PLUX` | `name` |
| 239 | `Pneumatic_burner_module` | `name` |
| 240 | `HMI800HP_LIGHT` | `rule` |
| 241 | `HMI300` | `name` |
| 242 | `CMS_enhanced_burner_module` | `name` |
| 243 | `EM100_UM100` | `name` |
| 244 | `DeltaDore_xLow_RF_no_timer` | `name` |
| 245 | `DeltaDore_xLow_RF_timer_enabled` | `name` |
| 246 | `DeltaDore_Low_RF` | `name` |
| 247 | `EMSLEAKS` | `name` |
| 248 | `HYBRID_MOD_HYBRID_MOD_LIN` | `rule` |
| 249 | `NextGenControl_xLowRF` | `name` |
| 250 | `NextGenControl_LowRF` | `name` |
| 251 | `OIL_BURNER_MODULE` | `name` |
| 252 | `K30_PLUX` | `name` |
| 253 | `HMI800_2_HP` | `observed` |
| 254 | `ACU_HTG` | `name` |
| 65281 | `XCU_SRH` | `observed` |

### Places

The key is the fixed name of a place in the integration; the name is shown as
it is. – means that the place has no name of its own. Places 24 to 27 are the
remote controls of heating circuits 1 to 4 at an older address range; place 42
is a fresh water station.

| Place | Key | Name | Basis |
|---|---|---|---|
| 8 | `heat_generator_control` | `APPLIANCE_CU` | `rule` |
| 9 | `basic_controller` | – | `rule` |
| 16 | `system_controller` | `SYSTEM_CONTROLLER` | `rule` |
| 17 | `pool` | `POOL` | `rule` |
| 24 | `remote_hc1_legacy` | – | `rule` |
| 25 | `remote_hc2_legacy` | – | `rule` |
| 26 | `remote_hc3_legacy` | – | `rule` |
| 27 | `remote_hc4_legacy` | – | `rule` |
| 42 | `fresh_water` | – | `rule` |
| 48 | `solar` | `SOLAR` | `rule` |
| 56 | `remote_hc1` | `HC1_REMOTE` | `name` |
| 57 | `remote_hc2` | `HC2_REMOTE` | `name` |
| 58 | `remote_hc3` | `HC3_REMOTE` | `name` |
| 59 | `remote_hc4` | `HC4_REMOTE` | `name` |
| 72 | `gateway` | `INET` | `observed` |
| 75 | `gateway_service` | – | `observed` |
| 80 | `radio` | `RF` | `name` |
| 81 | `ventilation` | `HRV` | `rule` |
| 83 | `hybrid_manager` | `HYBRID` | `rule` |

A row at place 72 or 75, or a row with type number 17, is the gateway itself.
It is never counted as a component of the heating system.

### Basis of an entry

Every entry of the table states how it is known:

| Basis | Meaning |
|---|---|
| `name` | Only the name is known. The entry is shown and never changes what the integration does. |
| `rule` | A fixed rule of the integration uses the entry, for example a hybrid manager (type number 248 or 16) at the place of the hybrid manager (83). Conditions of a rule, such as a minimum software version, are part of the rule, not of the table. |
| `observed` | Seen in the component list of a real system. The entry carries a short note, for example "seen on a K40 gateway". |
| `documented` | Stated in a manufacturer document. The entry carries the address of that document. |

What the integration does follows only from entries with the basis `rule`,
`observed` or `documented` and from what the devices report, never from a
name. Most entries are known by their name only. A diagnostics download with
a component list helps to raise an entry to `observed`.

The table gives these roles; a role at a place only applies at that place:

| Type number | Place | Role | Basis |
|---|---|---|---|
| 8 | 8 | `heat_source` | `observed` |
| 16 | 83 | `hybrid_manager` | `rule` |
| 17 | 72 | `gateway` | `observed` |
| 17 | 75 | `gateway` | `observed` |
| 248 | 83 | `hybrid_manager` | `rule` |
| 253 | 16 | `system_controller` | `observed` |
| 65281 | 8 | `outdoor_unit` | `observed` |

Type number 240 carries the mark `hmi800_hp_light` (basis `rule`): it is the
controller HMI800 HP Light, for which the holiday rules of iCom gateways in
[older holiday formats](legacy-holidays.md) apply.

### Plant kind

From the reported type values and the component list, the integration works
out what kind of plant a gateway runs. In this release the plant kind is
shown in the diagnostics only; it changes no entity, name, control or
request.

| Value | Meaning |
|---|---|
| `heat_pump` | The plant reports a heat pump (heat source type `Heatpump`, a heat pump type such as `air_water`, or the system type `heatpump_single`/`heatpump_cascade`). |
| `hybrid` | A heat pump and a boiler work together (see the evidence below). |
| `boiler` | The plant reports a gas, oil or other boiler only. |
| `ventilation` | No heat appliance; a ventilation unit (Modbus bus or a component at the ventilation place 81). |
| `none` | No heat appliance and no ventilation unit. |
| `null` | Not known; everything behaves as before. |

`fuel` (`gas`, `oil` or `unknown`) belongs to a boiler or a hybrid system.
`cascade` is `true` when the system type is `heatpump_cascade`.

Evidence (why the plant has this kind):

| Code | Meaning |
|---|---|
| `reported_system_type` | The system type (`/system/type`) says so. |
| `reported_heat_source_type` | A heat source type (`/heatSources/hsN/type`, `/heatSources/type`) says so. |
| `reported_heat_pump_type` | A heat pump type (`heatPumpType`) is reported. |
| `reported_outdoor_status` | The hybrid control reports its outdoor unit as connected. |
| `hybrid_manager_component` | A hybrid manager is listed at the hybrid place 83 (type 248 from version 06.01 on, or type 16), on a K30 gateway from firmware 08.02.02 or a K40 gateway from firmware 11.00.00 on. Only the first row at place 83 counts. |
| `heat_source_types_combined` | One heat source reports a heat pump and another one a boiler. |
| `hybrid_resources` | The resources of a hybrid control are present. |
| `hybrid_manager_signals` | The signals of a hybrid manager are present. |
| `stored` | The result rests on a value kept from an earlier reading, for example after a restart before the component list is read again. |

Conflicts (shown in the diagnostics only):

| Code | Meaning |
|---|---|
| `system_type_vs_heat_source_type` | The system type and the heat source types contradict each other; the plant kind stays unknown. |
| `components_vs_outdoor_status` | A hybrid manager is listed, but a boiler type is reported with the outdoor unit not connected; the hybrid manager does not count. |
| `heat_pump_component_on_boiler_plant` | Heat pump components are listed, but only a boiler type is reported. |
| `hybrid_name_rule_only` | Only the names of paths or signals or a type text point to a hybrid system. The plant still counts as hybrid, as before. |
| `cloud_local_components_differ` | The cloud and the local access list different components (the gateway's own rows are not compared). |
| `unknown_type_value` | A type value outside the known ones (for example `eBoiler`); it does not count. |
| `component_rule_skipped` | A row at the hybrid place does not meet the rule (other gateway, firmware or version too old, or another type). |

Component names never decide anything. Only entries whose basis is
`rule`, `observed` or `documented` can give a component a role.

### Stored data

The integration keeps one row per gateway in its saved calculated values
(`.storage/bosch_buderus_heating.calculated_values`, key `<gateway>:plant`):
the component lists of the cloud and of the local access (place, type
number, version, product name, hardware type, update type, software
identifier and article number of the model), when each component was first
seen and, for 30 days, when it disappeared, the reported type values, the
gateway class and firmware, the inputs of the hybrid manager rule and the
plant kind with its evidence and conflicts. The row is written only when its
content changes. After a restart it gives the plant kind at once; an outage
changes nothing, only a new reading that says something else does.

The row never contains serial numbers, UUIDs or the `No` field. It is
deleted with the gateway when no entry uses the gateway any more. Its form
is described in [`schemas/plant-store-row.v1.json`](schemas/plant-store-row.v1.json).

### Privacy

- Read and kept: type number, place number, version, product name, hardware
  type, update type, software identifier and the article number of the model
  of each component. A text equal to a serial number of the same row, or a
  name made only of digits, is left out.
- Never read or kept: `No`, `ProductSerialNumber`, `ModuleSerialNumber`,
  `ProductUuid`.
- Versions, software identifiers and article numbers appear in the stored
  row and in the diagnostics (block `plant`, see
  [diagnostics](diagnostics.md#plant-model)), never in entity attributes or
  the log.
- The anonymized inventory keeps only the plant kind.

## Deutsch

Eine Heizungsanlage meldet eine Liste ihrer Komponenten: die Steuerung des
Wärmeerzeugers, den Systemregler, Raumeinheiten, Module und das Gateway
selbst. Die Cloud meldet diese Liste als `/system/info`, der lokale Zugang als
`/system/basicInfo`. Jeder Eintrag hat eine Typnummer (`Id`) und einen Platz
(`Tok`):

- Die **Typnummer** bezeichnet die Art der Komponente, meist ihre Steuerung,
  nicht das Verkaufsmodell. Eine Nummer kann für mehrere Produktreihen
  stehen, und sie sagt nicht, ob eine Wärmepumpe Luft, Sole oder Wasser nutzt.
  Einen Modellnamen oder eine Wärmequellenart leite ich daraus nie ab.
- Der **Platz** ist die Stelle der Komponente in der Anlage, zum Beispiel die
  Steuerung des Wärmeerzeugers (8), der Systemregler (16), die Fernbedienung
  von Heizkreis 1 (56) oder der Hybrid-Manager (83). Mehrere Komponenten
  können sich einen Platz teilen, zum Beispiel Innen- und Außeneinheit einer
  Wärmepumpe auf Platz 8.

Die Integration bringt eine Tabelle dieser Nummern mit,
`system_components.json`. Sie hat eine eigene Version (`table_version`), die
mit jeder inhaltlichen Änderung steigt. Die Typnummern und Namen dieser
Tabelle stehen hier so, wie in den Hersteller-Apps verwendet. Namen
werden unverändert gezeigt und nie übersetzt. Das Format der Tabelle
beschreibt das Schema
[`schemas/system-components.v1.json`](schemas/system-components.v1.json).

### Komponentennamen

Die vollständige Tabelle der Typnummern steht im englischen Teil unter
[Component names](#component-names); Nummern, Namen und Grundlagen sind
dieselben. Typnummer 103 steht für zwei Komponenten; beide Namen werden
gezeigt. Typnummer 17 hat keinen Namen; sie ist die Zeile des Gateways selbst.
Typnummer 0 kennzeichnet eine Zeile ohne gültigen Typ. Eine Typnummer, die
nicht in der Tabelle steht, wird nur mit ihrer Nummer gezeigt.

### Plätze

Der Schlüssel ist der feste Name eines Platzes in der Integration; der Name
wird unverändert gezeigt. – bedeutet, dass der Platz keinen eigenen Namen hat.
Die Plätze 24 bis 27 sind die Fernbedienungen der Heizkreise 1 bis 4 in einem
älteren Adressbereich; Platz 42 ist eine Frischwasserstation.

| Platz | Schlüssel | Name | Grundlage |
|---|---|---|---|
| 8 | `heat_generator_control` | `APPLIANCE_CU` | `rule` |
| 9 | `basic_controller` | – | `rule` |
| 16 | `system_controller` | `SYSTEM_CONTROLLER` | `rule` |
| 17 | `pool` | `POOL` | `rule` |
| 24 | `remote_hc1_legacy` | – | `rule` |
| 25 | `remote_hc2_legacy` | – | `rule` |
| 26 | `remote_hc3_legacy` | – | `rule` |
| 27 | `remote_hc4_legacy` | – | `rule` |
| 42 | `fresh_water` | – | `rule` |
| 48 | `solar` | `SOLAR` | `rule` |
| 56 | `remote_hc1` | `HC1_REMOTE` | `name` |
| 57 | `remote_hc2` | `HC2_REMOTE` | `name` |
| 58 | `remote_hc3` | `HC3_REMOTE` | `name` |
| 59 | `remote_hc4` | `HC4_REMOTE` | `name` |
| 72 | `gateway` | `INET` | `observed` |
| 75 | `gateway_service` | – | `observed` |
| 80 | `radio` | `RF` | `name` |
| 81 | `ventilation` | `HRV` | `rule` |
| 83 | `hybrid_manager` | `HYBRID` | `rule` |

Eine Zeile auf Platz 72 oder 75 oder eine Zeile mit Typnummer 17 ist das
Gateway selbst. Sie zählt nie als Komponente der Heizungsanlage.

### Grundlage eines Eintrags

Jeder Eintrag der Tabelle nennt, woher er bekannt ist:

| Grundlage | Bedeutung |
|---|---|
| `name` | Nur der Name ist bekannt. Der Eintrag wird gezeigt und ändert nie, was die Integration tut. |
| `rule` | Eine feste Regel der Integration verwendet den Eintrag, zum Beispiel ein Hybrid-Manager (Typnummer 248 oder 16) auf dem Platz des Hybrid-Managers (83). Bedingungen einer Regel, etwa eine Mindestversion der Software, gehören zur Regel, nicht zur Tabelle. |
| `observed` | In der Komponentenliste einer echten Anlage gesehen. Der Eintrag hat eine kurze Notiz, zum Beispiel "seen on a K40 gateway". |
| `documented` | In einer Unterlage des Herstellers genannt. Der Eintrag enthält die Adresse dieser Unterlage. |

Was die Integration tut, ergibt sich nur aus Einträgen mit der Grundlage
`rule`, `observed` oder `documented` und aus den Meldungen der Geräte, nie
aus einem Namen. Die meisten Einträge sind nur über ihren Namen bekannt. Ein
Diagnose-Download mit Komponentenliste hilft, einen Eintrag auf `observed` zu
heben.

Die Tabelle vergibt diese Rollen; eine Rolle auf einem Platz gilt nur dort:

| Typnummer | Platz | Rolle | Grundlage |
|---|---|---|---|
| 8 | 8 | `heat_source` | `observed` |
| 16 | 83 | `hybrid_manager` | `rule` |
| 17 | 72 | `gateway` | `observed` |
| 17 | 75 | `gateway` | `observed` |
| 248 | 83 | `hybrid_manager` | `rule` |
| 253 | 16 | `system_controller` | `observed` |
| 65281 | 8 | `outdoor_unit` | `observed` |

Typnummer 240 trägt die Markierung `hmi800_hp_light` (Grundlage `rule`): Sie
ist der Regler HMI800 HP Light, für den die Urlaubsregeln für iCom-Gateways
unter [ältere Urlaubsformate](legacy-holidays.md) gelten.

### Anlagenart

Aus den gemeldeten Typwerten und der Komponentenliste bestimmt die
Integration, welche Art von Anlage an einem Gateway hängt. In diesem Release
erscheint die Anlagenart nur in der Diagnose; sie ändert keine Entität,
keinen Namen, kein Bedienelement und keine Anfrage.

| Wert | Bedeutung |
|---|---|
| `heat_pump` | Die Anlage meldet eine Wärmepumpe (Wärmequellentyp `Heatpump`, einen Wärmepumpentyp wie `air_water` oder den Systemtyp `heatpump_single`/`heatpump_cascade`). |
| `hybrid` | Wärmepumpe und Kessel arbeiten zusammen (siehe Belege unten). |
| `boiler` | Die Anlage meldet nur einen Gas-, Öl- oder sonstigen Kessel. |
| `ventilation` | Kein Wärmeerzeuger; ein Lüftungsgerät (Modbus-Bus oder eine Komponente am Lüftungsplatz 81). |
| `none` | Weder Wärmeerzeuger noch Lüftungsgerät. |
| `null` | Nicht bekannt; alles verhält sich wie bisher. |

`fuel` (`gas`, `oil` oder `unknown`) gehört zu einem Kessel oder einer
Hybridanlage. `cascade` ist `true`, wenn der Systemtyp `heatpump_cascade` ist.

Belege (warum die Anlage diese Art hat):

| Code | Bedeutung |
|---|---|
| `reported_system_type` | Der Systemtyp (`/system/type`) sagt es. |
| `reported_heat_source_type` | Ein Wärmequellentyp (`/heatSources/hsN/type`, `/heatSources/type`) sagt es. |
| `reported_heat_pump_type` | Ein Wärmepumpentyp (`heatPumpType`) wird gemeldet. |
| `reported_outdoor_status` | Die Hybridsteuerung meldet ihre Außeneinheit als verbunden. |
| `hybrid_manager_component` | Am Hybridplatz 83 steht ein Hybrid-Manager (Typ 248 ab Version 06.01 oder Typ 16), auf einem K30-Gateway ab Firmware 08.02.02 oder einem K40-Gateway ab Firmware 11.00.00. Es zählt nur die erste Zeile mit Platz 83. |
| `heat_source_types_combined` | Eine Wärmequelle meldet eine Wärmepumpe, eine andere einen Kessel. |
| `hybrid_resources` | Die Ressourcen einer Hybridsteuerung sind vorhanden. |
| `hybrid_manager_signals` | Die Signale eines Hybrid-Managers sind vorhanden. |
| `stored` | Das Ergebnis stützt sich auf einen Wert aus einer früheren Lesung, zum Beispiel nach einem Neustart, bevor die Komponentenliste wieder gelesen ist. |

Widersprüche (nur in der Diagnose):

| Code | Bedeutung |
|---|---|
| `system_type_vs_heat_source_type` | Systemtyp und Wärmequellentypen widersprechen sich; die Anlagenart bleibt unbekannt. |
| `components_vs_outdoor_status` | Ein Hybrid-Manager steht in der Liste, aber ein Kesseltyp wird mit nicht verbundener Außeneinheit gemeldet; der Hybrid-Manager zählt nicht. |
| `heat_pump_component_on_boiler_plant` | Wärmepumpen-Komponenten stehen in der Liste, gemeldet wird aber nur ein Kesseltyp. |
| `hybrid_name_rule_only` | Nur Namen von Pfaden oder Signalen oder ein Typtext deuten auf eine Hybridanlage. Die Anlage zählt weiter als Hybrid, wie bisher. |
| `cloud_local_components_differ` | Cloud und lokaler Zugang listen verschiedene Komponenten (die Zeilen des Gateways selbst werden nicht verglichen). |
| `unknown_type_value` | Ein Typwert außerhalb der bekannten (zum Beispiel `eBoiler`); er zählt nicht. |
| `component_rule_skipped` | Eine Zeile am Hybridplatz erfüllt die Regel nicht (anderes Gateway, Firmware oder Version zu alt oder ein anderer Typ). |

Namen von Komponenten entscheiden nie etwas. Nur Einträge mit der Basis
`rule`, `observed` oder `documented` können einer Komponente eine Rolle geben.

### Gespeicherte Daten

Die Integration hält je Gateway eine Zeile in ihren gespeicherten berechneten
Werten (`.storage/bosch_buderus_heating.calculated_values`, Schlüssel
`<gateway>:plant`): die Komponentenlisten von Cloud und lokalem Zugang (Platz,
Typnummer, Version, Produktname, Hardwaretyp, Update-Typ, Software-Kennung und
Artikelnummer des Modells), wann jede Komponente zuerst gesehen wurde und, für
30 Tage, wann sie verschwand, die gemeldeten Typwerte, Gateway-Klasse und
-Firmware, die Eingaben der Hybrid-Manager-Regel sowie die Anlagenart mit
Belegen und Widersprüchen. Die Zeile wird nur geschrieben, wenn sich ihr Inhalt
ändert. Nach einem Neustart liefert sie die Anlagenart sofort; ein Ausfall
ändert nichts, nur eine neue Lesung mit anderer Aussage.

Die Zeile enthält nie Seriennummern, UUIDs oder das Feld `No`. Sie wird mit
dem Gateway gelöscht, sobald kein Eintrag das Gateway mehr nutzt. Ihr Aufbau
steht in [`schemas/plant-store-row.v1.json`](schemas/plant-store-row.v1.json).

### Datenschutz

- Gelesen und gespeichert: Typnummer, Platznummer, Version, Produktname,
  Hardwaretyp, Update-Typ, Software-Kennung und die Artikelnummer des Modells
  jeder Komponente. Ein Text, der einer Seriennummer derselben Zeile gleicht,
  oder ein Name nur aus Ziffern wird weggelassen.
- Nie gelesen oder gespeichert: `No`, `ProductSerialNumber`,
  `ModuleSerialNumber`, `ProductUuid`.
- Versionen, Software-Kennungen und Artikelnummern stehen in der
  gespeicherten Zeile und in der Diagnose (Block `plant`, siehe
  [Diagnose](diagnostics.md#plant-model)), nie in Entitätsattributen oder im
  Log.
- Das anonymisierte Inventar behält nur die Anlagenart.
