<p align="center">
  <img src="docs/assets/icon.svg" width="128" height="128" alt="Bosch/Buderus Heating icon">
</p>

<h1 align="center">Bosch/Buderus Heating</h1>

<p align="center">
  Connect compatible Bosch and Buderus heating systems to Home Assistant through the PointT Cloud API.
</p>

<p align="center">
  <a href="https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/actions/workflows/ci.yml"><img src="https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/actions/workflows/codeql.yml"><img src="https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/actions/workflows/codeql.yml/badge.svg" alt="CodeQL"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT License"></a>
</p>

> [!NOTE]
> The illustrated [setup guide](docs/setup.md) explains installation and
> sign-in step by step.

Bosch/Buderus Heating is an unofficial Home Assistant custom integration. It
discovers the capabilities exposed by a heating system instead of assuming a
fixed device layout. Heating circuits, hot-water circuits, heat generators,
energy counters, and available controls are therefore created dynamically.

## Built for real heating systems

Heating installations rarely have identical layouts. This integration treats
the PointT resource tree as the source of truth: it discovers the connected
system, creates only entities that are actually supported, and keeps the
original API path behind every value documented. One heating circuit works;
additional circuits, heat generators, hot-water circuits, and gateways follow
the same dynamic model without requiring model-specific entity lists.

Reliability is designed into the complete data path:

- **Capability based:** entities and controls follow the resources and metadata
  reported by the installation, not assumptions based only on a product name.
- **Transparent:** the entity catalog maps human-readable names to their exact
  PointT paths and raw fields, including every calculated energy value.
- **Conservative when writing:** limits and allowed values are validated again
  immediately before a change; the request is never retried blindly and must
  be confirmed by a separate read-back.
- **Efficient in the cloud:** resources with similar update needs are fetched
  together, while live values, settings, energy counters, and static data use
  different polling intervals.
- **Resilient:** partial responses are preserved, temporary failures retain the
  last confirmed state, unknown enum values do not break an update, and rate
  limits trigger bounded backoff.
- **Supportable without exposing the home:** diagnostics show capability and
  request results while excluding credentials, raw measurements, stable device
  identifiers, and user-defined names.
- **Understandable:** German and English terminology is based on the heating
  apps and technical documentation, with dynamic names such as
  `Heating circuit 1 – Operation mode` that remain clear on larger
  installations.

The result is an integration that aims to remain predictable not only during
initial setup, but also when the cloud is slow, a resource is unavailable, or
the installation exposes a combination of capabilities that has not been seen
before.

## Features

- Automatic discovery of supported PointT resources
- Support for multiple gateways and dynamically discovered circuits
- Temperatures, system pressure, operating states, instantaneous heat-source
  power, brine temperatures, starts, and operating hours
- Capability-gated read-only monitoring for optional hybrid, low-noise,
  power-limit, solar, pool, ventilation, room-zone, room-device, photovoltaic,
  fresh-water-station, and variable-tariff resources
- Active system faults, warnings, and maintenance notifications with lifecycle
  events for Home Assistant automations
- Optional holiday status and calendar with support for multiple periods,
  gateway-aware time zones, and capability-gated create, edit, and delete
  actions, kept separate from Away mode
- Cumulative electricity, produced heat, cooling energy, and calculated
  environmental energy
- Heating-circuit and hot-water controls exposed only when PointT reports them
  as writable
- Silent Mode control with Off, Automatic, and On when the gateway advertises
  the exact writable capability
- Auxiliary-heater mode control with Off, On, and Auto, plus a guarded
  maximum-supply-temperature control for supported heating circuits
- Mandatory read-back after every write; writes are never retried blindly
- Separate polling intervals for live, configuration, energy, long-term, and
  static values
- Last confirmed values remain available during temporary cloud failures
- Reauthentication, gateway selection, resource rediscovery, and selectable
  polling profiles
- Privacy-preserving Home Assistant diagnostics with capability counters,
  rolling 60-minute request and latency metrics, and a bounded sanitized log
  of individual HTTP attempts
- German and English entity names and state translations

Normal measurements and everyday controls are enabled on a fresh installation.
The installer-level maximum-supply-temperature control, identity data, detailed
system information, raw software-update fields, and read-only copies of an
existing control remain available only as opt-in diagnostics.
Unknown or privacy-sensitive resources never become entities.

## Compatibility

The integration uses the cloud interface shared by the Bosch and Buderus
heating apps. Compatibility depends on the resources exposed for the specific
account, gateway, controller, and heating system.

| Status | Systems |
|---|---|
| Tested | Buderus heat-pump installation with an MX400 controller connected through a gateway reported by PointT as K40/K40RF |
| Designed for | Compatible Bosch/Buderus systems using MX300 or MX400 controllers and K30/K30RF or K40/K40RF gateways |
| Optional when reported by PointT | Solar, pool, ventilation, room zones, room devices, PV status, hybrid and tariff resources |
| Not covered | Air conditioners, wallboxes, Matter/MQTT devices, gateway network administration, and local LAN access |

An unlisted model may still work because discovery is capability based. A
listed gateway does not by itself guarantee that every entity or control is
available. Missing capabilities are simply omitted without affecting the rest
of the integration.

Home Assistant shows a uniquely identified MX300/MX400 controller as the
system model. K30/K40 describes the PointT communication gateway, not the
controller, and remains visible in diagnostics and the gateway hardware data.

> [!IMPORTANT]
> The PointT Cloud API is not a publicly guaranteed API. Cloud-side changes may
> affect the integration. Keep the original Bosch or Buderus app available for
> commissioning and service work.

## Installation

### 1. Install HACS if necessary

Follow the official [HACS installation guide](https://www.hacs.xyz/docs/use/download/download/),
restart Home Assistant, and complete the HACS setup under
**Settings → Devices & services**.

### 2. Install Bosch/Buderus Heating

1. Open **HACS** in Home Assistant.
2. Search for **Bosch/Buderus Heating**.
3. Open the matching repository and select **Download**.
4. Restart Home Assistant when prompted.

If the repository is not yet listed in the default HACS catalog, open the
three-dot menu in HACS, select **Custom repositories**, add
`https://github.com/SoftwareSchmied/ha-bosch-buderus-heating` as an
**Integration**, and then repeat the search. The
[illustrated setup guide](docs/setup.md#repository-does-not-appear-in-hacs-search)
shows the complete route.

### 3. Add the integration

1. Open **Settings → Devices & services**.
2. Select **Add integration**.
3. Search for **Bosch/Buderus Heating**.
4. Select the brand of the app already connected to the heating system.
5. Sign in with the same SingleKey ID used by that app.
6. Select the gateway or gateways Home Assistant should use.

SingleKey ID returns the authorization result to a Bosch/Buderus app address
rather than a normal web page. A desktop browser cannot open that address
directly. The setup flow and the
[illustrated setup guide](docs/setup.md#sign-in-with-singlekey-id)
explain how to copy the complete return address safely from Chrome. Home
Assistant never asks for the SingleKey ID password.

> [!CAUTION]
> The return address contains a short-lived one-time authorization code. Never
> post it in an issue, chat, screenshot, or log.

## Entities

Entities are created from the capabilities reported by PointT. Their names
begin with the logical part of the heating system, which keeps related values
together even when several circuits exist. Examples include:

- `Heating circuit 1 – Operation mode`
- `Heating circuit 2 – Desired temperature`
- `Hot water – Actual temperature`
- `Heat pump – System pressure`
- `Total – Electricity consumption`

The [entity and polling catalog](docs/entities.md) documents every entity in
the reference installation, its PointT resource, write status, default state,
and polling interval.

### System faults and notifications

When the installation exposes PointT notifications, the integration creates:

- **System fault**, a problem binary sensor for faults, critical faults, and
  conservatively classified unknown notifications;
- **Active faults**, the number of currently active system problems;
- **Active notifications**, the number of all current notifications, including
  warnings and maintenance;
- **System notifications**, an event entity that emits `appeared` and
  `resolved` transitions.

Fault details are kept as bounded attributes instead of creating a separate
entity for every possible code. Known, independently worded summaries are
shown when their meaning has been verified. Unknown codes remain visible and
are never guessed. The integration observes when a fault first appears or
disappears in PointT; these Home Assistant observation times are not presented
as exact appliance timestamps.

The appliance display and the official
[Bosch error-code search](https://www.bosch-homecomfort.com/de/de/wohngebaeude/service-und-support/bosch-fehlercode-suche/)
remain the authoritative sources for detailed service information. Home
Assistant does not replace a qualified technician or manufacturer diagnosis.

### Holiday periods

When the gateway exposes PointT holiday resources, the integration creates a
**Holiday mode active** binary sensor and a **Holiday periods** calendar.
The **Next holiday** timestamp sensor makes the upcoming start visible on the
device page. Multiple configured periods are shown individually. Date-only values,
timestamps, gateway time zones, empty lists, and incomplete entries are handled
without affecting the rest of the integration.

Holiday mode is not the same as the existing **Away mode** switch. Holiday
periods may contain separate behavior for heating, hot water, and ventilation;
Away mode is a different PointT setting.

If the gateway advertises a current and complete holiday schema, the standard
Home Assistant calendar dialog also allows you to create, move, rename, and
delete periods. Open **Calendar**, select **Holiday periods**, and add or edit
an event. Use only a title plus start and end; recurrence, location, and
description are not supported by PointT.

When an existing period is edited, its heating, hot-water, ventilation,
thermal-disinfection, constant-temperature, and circuit-assignment settings are
preserved. A newly created period uses the defaults also used by MyBuderus and
HomeCom Easy: all advertised circuits, 17 °C constant-temperature heating when
supported, hot water off, ventilation off when supported, and thermal
disinfection on when supported.

To adjust these details in Home Assistant, open **Settings → Devices &
services → Bosch/Buderus Heating → Configure**, select the holiday, and then
choose its assigned circuits, heating mode, hot-water mode, ventilation mode,
thermal-disinfection setting, and constant temperature. The dialog is
generated from `/holidayMode/configuration`: options not advertised by the
connected system are not shown. Dates and the name remain in the calendar
dialog. Every change is read back from PointT before Home Assistant accepts it
as successful.

The step-by-step [holiday-period guide](docs/holiday-periods.md) shows how to
create, configure, move, rename, and delete a period and explains every
available heating-system setting.

### Energy counters

All energy entities are cumulative counters in kWh. They do not represent
instantaneous power in kW.

| Entity | PointT source |
|---|---|
| `Total – Electricity consumption` | `totalConsumption → electricity`, otherwise `compressor + eheater` |
| `Total – Heat-pump electricity consumption` | `totalConsumption → compressor` |
| `Total – Electric auxiliary-heater consumption` | `totalConsumption → eheater` |
| `Total – Heat produced` | `totalConsumption → outputProduced` |
| `Total – Environmental energy (calculated)` | `outputProduced - compressor - eheater` |
| `Heating – …` | corresponding values from `chConsumption` |
| `Hot water – …` | corresponding values from `dhwConsumption` |
| `Cooling – …` | corresponding values from `coolingConsumption` |

The calculated environmental-energy entity is only created when all required
counters are present and valid. An incomplete or negative balance is not
reported as a measurement.

## Controls and safety

The integration can expose supported operating modes, temperature setpoints,
extra hot water, and away mode. Availability is determined for each
installation from the current PointT metadata.

Before a change is sent, the integration verifies that the resource is still
writable and that the requested value is within the advertised limits. Every
successful request is followed by a bounded read-back. If PointT does not
confirm the result, Home Assistant reports the change as unsuccessful instead
of assuming it worked.

Hot-water temperature controls use whole-degree steps where required by the
tested installation. Heating-circuit setpoints retain the step size reported
by PointT.

## Polling and cloud load

The coordinator groups resources by how quickly they need to change:

| Data | Default interval |
|---|---:|
| Live operating values | 60 seconds |
| Notifications without an active notification | 5 minutes |
| Notifications while at least one is active | 60 seconds |
| Settings and energy counters | 5 minutes |
| Starts and operating hours | 15 minutes |
| Static device information | At startup |

Reads are batched where possible. Rate-limit responses trigger bounded
backoff, and temporary failures do not replace the last confirmed value with
invented data. Polling profiles can be changed through **Reconfigure**; see
[Reconfiguration and rediscovery](docs/reconfiguration.md).

## Diagnostics and support

Home Assistant can generate a redacted diagnostic report from the integration
menu. It contains capability schemas, availability, polling results, and
request counters, but excludes credentials, raw values, stable identifiers,
and user-defined names.

Before opening an
[issue](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues):

1. Check the [troubleshooting section](docs/setup.md#troubleshooting).
2. Reload the integration and reproduce the problem once.
3. Download the integration diagnostics as described in the
   [diagnostics guide](docs/diagnostics.md).
4. Remove anything you do not want to share and attach the report to the issue.

Never publish access tokens, refresh tokens, complete redirect addresses,
gateway IDs, serial numbers, network identifiers, or unredacted API responses.

## Documentation

| Document | Contents |
|---|---|
| [Setup guide](docs/setup.md) | Illustrated installation and SingleKey ID walkthrough |
| [Entities and polling](docs/entities.md) | Entity names, API resources, write access, and intervals |
| [Faults and notifications](docs/faults.md) | Fault entities, lifecycle behavior, and automation examples |
| [Holiday periods](docs/holiday-periods.md) | Creating calendar entries and configuring heating behavior |
| [Resource catalog](docs/resource-catalog.md) | Observed PointT resources and terminology |
| [Reconfiguration](docs/reconfiguration.md) | Gateway selection, rediscovery, and polling profiles |
| [Diagnostics](docs/diagnostics.md) | Redacted diagnostics and request metrics |
| [Anonymized inventory](docs/anonymized-inventory.md) | Value-free compatibility inventory for maintainers |
| [Roadmap](docs/roadmap.md) | Remaining compatibility and release work |

## Development

The development toolchain requires Python 3.14.2 or newer.

```bash
python -m venv .venv
python -m pip install -e ".[test]"
ruff format --check .
ruff check .
mypy
pytest
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting code or real-device
fixtures. Contributions for additional Bosch/Buderus installations are
welcome when they follow the repository's privacy and fixture rules.

## License and trademarks

The project is licensed under the [MIT License](LICENSE).

This is an independent community project. It is not affiliated with, endorsed
by, or supported by Bosch, Buderus, Bosch Home Comfort, or Home Assistant.
Names and trademarks are used only to describe compatibility.
