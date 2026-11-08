# Device families

This page gives an overview of the device families the integration supports
and links to their detailed descriptions. What is available on a particular
system depends on the resources its account, gateway and controller report;
missing capabilities are simply omitted.

| Family | Access | Shows | Operates | Details |
| --- | --- | --- | --- | --- |
| Heating systems with MX300/MX400 controllers and K30/K40 gateways (heat pumps, gas condensing boilers) | Cloud; K40 gateways also local | Heating circuits, hot water, heat source, energy and start counters, faults, time programs | Heating-circuit mode and setpoints, hot-water mode and extra hot water, holidays, existing time-program switch points | [Heating controls](heating-controls.md), [hot water](pointt-hot-water.md), [faults](faults.md), [holidays](holiday-periods.md) |
| Room controllers and zones | Cloud | Zone temperatures and states, programs | Zone mode, manual setpoint, program selection, existing switch points | [Zone controls](zone-controls.md), [room controller zones](rrc-zones-and-schedules.md) |
| WDDW2 appliances | Cloud | Heat-source values, start counters, safety temperature | Holiday mode | [WDDW2 types](wddw2-types.md) |
| Water softener WE800i | Cloud | Salt level, soft water, flow, regeneration | Four controls, disabled by default | [Water softener](water-softener.md) |
| Water heaters (Midea/RWH) | Cloud, selected at setup | Temperatures and state | Operation mode and target temperature | [Water heaters](bacon-water-heaters.md) |
| Air conditioners (Climate Series) | Cloud, selected at setup | Temperatures and state | On/off, mode, target temperature and the fan settings the device reports | [Air conditioners](bacon-air-conditioners.md) |
| Air purifiers (seven families) | Cloud, selected at setup | Air quality and filter state | On/off, presets and fan levels the device reports | [Air purifiers](bacon-air-purifiers.md) |
| Dehumidifiers (Dry 4000i/6000i/8000i) | Cloud, selected at setup | Humidity and state | On/off, mode and target humidity the device reports | [Dehumidifiers](bacon-dehumidifiers.md) |
| Wallboxes | Cloud | Charging state and energy | read-only | [Wallboxes](pointt-wallboxes.md) |

The cloud access signs in with an account of the Bosch or Buderus app. The
local access recognises gateways of Bosch, Buderus, IVT, Vulcano and Worcester;
see [compatibility](compatibility.md) for the local access and its checked
firmware.

The [resource inventory](resource-inventory.md) lists every path model with its
disposition. A path appearing there does not by itself make an entity or a
control; the [resource contracts](resource-contracts.md) describe the rules.
