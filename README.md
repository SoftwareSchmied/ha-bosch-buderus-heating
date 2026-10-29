<p align="center">
  <img src="https://raw.githubusercontent.com/SoftwareSchmied/ha-bosch-buderus-heating/main/custom_components/bosch_buderus_heating/brand/icon.png" width="128" height="128" alt="Bosch/Buderus Home Comfort icon">
</p>

<h1 align="center">Bosch/Buderus Home Comfort</h1>

<p align="center">
  Connect compatible Bosch and Buderus heating, cooling, hot-water, air-treatment and water-softening devices to Home Assistant, with read-only wallbox monitoring.
</p>

<p align="center">
  <a href="https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/actions/workflows/ci.yml"><img src="https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/actions/workflows/codeql.yml"><img src="https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/actions/workflows/codeql.yml/badge.svg" alt="CodeQL"></a>
  <a href="https://github.com/hacs/default/pull/10040"><img src="https://img.shields.io/badge/HACS-Default-41BDF5.svg" alt="Available in the default HACS catalog"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT License"></a>
</p>

> [!NOTE]
> The illustrated [setup guide](docs/setup.md) explains installation and
> sign-in step by step.

> [!NOTE]
> **0.8.3-beta.1 adds read-only heating and hot-water time programs.** The new
> diagnostic sensors are disabled by default. They show the scheduled level
> and weekly switch points; this feature does not change appliance settings.
> **0.8.2 remains Stable/Latest.** See the
> [beta release notes](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/releases/tag/v0.8.3-beta.1)
> for installation and the requested display checks.

**Bosch/Buderus Home Comfort** is the new display name for **Bosch/Buderus Heating**.
The new name is included in stable **0.8.2**, following its introduction in Beta.7.
The repository, integration domain and existing installation stay the same.
Update the existing integration rather than adding
a second one. Existing saved entry names and custom device/entity names are retained.

**0.8.2 is the current stable version.** I included the changes from the
0.8.2 betas: a tabbed appliance dialog, native PointT heating and hot-water
entities, more reliable discovery and token renewal, additional appliance
families, corrected readings and the Home Comfort display name. This release
also includes the updated sign-in guide and compatible maintenance updates.

Update through HACS, restart Home Assistant and reload the browser. See the
[release notes](CHANGELOG.md#082---2026-10-27),
[holiday guide](docs/holiday-periods.md),
[request and recovery rules](docs/request-behavior.md) and
[season-switching guide](docs/season-switching.md) for details and the remaining
field checks.

> [!WARNING]
> **Upgrading from 0.8.1 includes substantial changes.** I added device families
> and entity types and reworked connections, discovery, state handling and
> conversions during the 0.8.2 betas. Make a full Home Assistant backup before
> updating and check the readings and controls used by your automations.
> Physical-device confirmation is still limited to the operations described below.

The additional water-heater, climate, air-purifier and dehumidifier families,
read-only wallboxes, zone controls and program editing remain included.
Those new families still need broader physical-device confirmation.
WE800i controls remain disabled by default. See the
[0.8.2 release notes](CHANGELOG.md#082---2026-10-27),
[current scope](docs/specification-status.md) and
[WE800i validation guide](docs/water-softener-validation.md).

Bosch/Buderus Home Comfort is an unofficial Home Assistant custom integration.
It discovers PointT heating-system resources and uses separate cloud adapters
for identified additional appliances. Entities follow each system's reported
capabilities; the [compatibility section](#compatibility) documents the supported
families and the limits of their validation.

<a id="unreleased-development-work"></a>

<a id="new-pointt-controls-in-beta7"></a>

## PointT controls in 0.8.2

I have added a tabbed **Operate appliance** dialog, clearer device-page controls,
PointT heating-circuit climates and a native PointT hot-water entity. I also
improved token rotation and connection recovery. These changes are included
in stable **0.8.2** and were introduced in Beta.7. They are not present in
0.8.2-beta.6 or 0.8.1.

The dialog opens under **Settings > Devices & services > Bosch/Buderus Home Comfort >
Configure > Operate appliance**. It keeps edits as a draft until **Apply**.
The device page offers the native climate and hot-water controls; its individual
controls continue to write immediately. Existing IDs and customizations remain.
See [Heating controls](docs/heating-controls.md),
[PointT hot water](docs/pointt-hot-water.md) and
[Connection recovery](docs/connection-recovery.md).

On my Buderus installation, I confirmed a manual heating target change and
restoration through the dialog and climate entity, plus Auto to Off and back
to Auto through the hot-water entity. I checked both directions in MyBuderus
and restored all changed settings. This confirms those paths on my appliance;
other devices and failure scenarios still need their own field checks.
The [project status](docs/specification-status.md) records the release checks
and remaining field validation. The initial sign-in procedure is unchanged;
an automatic sign-in helper is being investigated for a later beta.

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
  installations. The [language guide](docs/resource-names-and-languages.md)
  explains system-language names, profile-language dialogs and custom names.

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
- Home Assistant bell notifications for active faults, enabled by default and
  configurable per installation under **Configure > Notifications**. Dismissed
  incidents stay hidden across polls and restarts; no extra cloud requests are
  needed. See [fault notifications](docs/faults.md) for the full behavior.
- Optional holiday status and calendar with support for multiple periods,
  gateway-aware time zones, and capability-gated create, edit, and delete
  actions, kept separate from Away mode
- Cumulative electricity, produced heat, cooling energy, and calculated
  environmental energy
- A clearly identified calculated dew point when a heating circuit reports
  both room temperature and relative humidity
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
| Included but experimental, field checks pending | Identified Midea/RWH water heaters, Midea Climate Series, seven connected Air purifier families, Dry 4000i/6000i/8000i, and read-only PointT wallboxes |
| Not covered | Matter devices, arbitrary MQTT devices, gateway network administration, and local LAN access |

The additional device families included in 0.8.2 use their own adapters and supported
Home Assistant entity types. Available functions depend on identification,
reported metadata and explicit device selection. See the
[current scope](docs/specification-status.md) for limits and pending checks.

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

<a id="2-install-boschbuderus-heating"></a>

### 2. Install Bosch/Buderus Home Comfort

The integration is available directly in the default HACS catalog.

1. Open **HACS** in Home Assistant.
2. Search for **Bosch/Buderus**. From Beta.7, the display name is
   **Bosch/Buderus Home Comfort**; older packages use **Bosch/Buderus Heating**.
3. Open the matching repository and select **Download**.
4. Restart Home Assistant when prompted.

If the search does not show the integration, follow the
[search troubleshooting steps](docs/setup.md#repository-does-not-appear-in-hacs-search)
in the illustrated setup guide.

### 3. Add the integration

1. Open **Settings → Devices & services**.
2. Select **Add integration**.
3. Search for **Bosch/Buderus**, using the name shown by the installed version.
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
disinfection on when supported. These defaults are checked against the actual
configuration. If a default is not offered, use **Configure → Manage holiday
periods → New holiday** to
choose dates and the modes provided by your installation before creating it.

To manage a holiday, open the integration's **Configure → Manage holiday
periods** dialog. Selecting a saved holiday opens its details directly.
Change its prefilled name, start and end,
assigned circuits, heating mode, hot-water mode, ventilation mode,
thermal-disinfection setting, and constant temperature. Only settings
supported by the connected system are shown.

When creating a holiday here, start and end times default to **08:00** and
remain editable. Both dates must be selected explicitly. Existing holidays
retain their saved times when opened for editing. Creating and editing use
the same form with supported settings together. Each date has its clock
directly beside it. The clock fields omit seconds. An end before or equal to the start produces
a clear message at the end field, while the entered values remain available.

The form saves directly. For an existing holiday, the red **Delete holiday
period completely** button at the bottom opens confirmation without saving
the draft. Check the saved name and dates before confirming. Declining
returns to editing with your unsaved inputs preserved. This also works when
the unsaved dates are incomplete or invalid.
The calendar remains an additional way to create, move, rename, or delete
periods. Every change is read back from PointT before Home Assistant accepts
it as successful. Notification settings remain accessible from the
integration's configuration dialog.

Midnight remains 00:00 on the selected date, without conversion to 24:00 or
rounding. If the system returns different settings, the actual holiday appears
immediately and the dialog shows the requested and returned periods. An
unresolved creation is checked again without sending another creation request
during the same loaded session.

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

Each resource provides its own options and numeric bounds. A circuit offering
only `manual` and `auto` remains writable, and additional advertised enum codes
are offered even when they have no translation yet. Options and limits update
during polling. Newly eligible discovered resources gain controls without a
restart; previously undiscovered paths still require rediscovery.

Numeric increments are UI hints. The observed PointT metadata does not establish
a write step, so a valid target inside the advertised bounds is not rejected
because it differs from an increment used on a reference installation. Legacy
switch IDs remain stable; variants with additional or fewer options also receive
a separate selection control.

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

Reviewed supplementary metadata now has a separate phase after functional
discovery, with one shared 30-second entry budget. Valid read-only product
names and installed refrigerant-circuit counts then stop repeating. Failed
reads remain eligible for bounded recovery; existing sensor identities are
preserved. The unused license document is not requested. See
[Request behavior and recovery](docs/request-behavior.md) for the exact paths,
retry delays, cache lifetime, limits and measured request savings.

Group deadlines allow up to one second of timer-rounding tolerance so a slightly
early Home Assistant callback does not skip the next read. Resource pauses and
rate-limit deadlines retain their full duration.

Reads are batched where possible. Rate-limit responses trigger bounded
backoff, and temporary failures do not replace the last confirmed value with
invented data. Polling profiles can be changed through **Reconfigure**; see
[Reconfiguration and rediscovery](docs/reconfiguration.md).

## Questions, ideas and installation reports

I use [GitHub Discussions](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions)
for questions about setup and everyday use, feature ideas, and shared dashboards
or automations. Start with **Questions and help**, **Ideas and requests**, or
**Installations and examples**. Please write in English so others
can follow the conversation.

I would also like to know which systems are using the integration. Please add
your model and experience to
[Share your installation: which system are you using?](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/81).
The post includes a template. Unknown details can be left out, and there is no
need to change settings just to report your installation. I distinguish reported
installations, checked readings and individually confirmed controls when
assessing compatibility.

Please continue to use
[Issues](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues/new/choose)
for reproducible bugs. The diagnostic steps below help me investigate them.

## Diagnostics and support

Home Assistant can generate a redacted diagnostic report from the integration
menu. It contains capability schemas, availability, polling results, and
request counters and narrowly allowlisted technical metadata. It excludes
credentials, current operating/settings values, stable identifiers and
user-defined names. The [diagnostics guide](docs/diagnostics.md) explains the
technical-value allowlist and the value-free inventory export.

Before opening an
[issue](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues):

1. Check the [troubleshooting section](docs/setup.md#troubleshooting).
2. Download the integration diagnostics before reloading, as described in the
   [diagnostics guide](docs/diagnostics.md), to preserve the current error context.
3. Note the time, exact message and whether MyBuderus is affected too. If a reload
   is needed, capture the original diagnostics first and describe what changed.
4. Remove anything you do not want to share and attach the report to the issue.

Never publish access tokens, refresh tokens, complete redirect addresses,
gateway IDs, serial numbers, network identifiers, or unredacted API responses.

## Documentation

| Document | Contents |
|---|---|
| [Setup guide](docs/setup.md) | Illustrated installation and SingleKey ID walkthrough |
| [Entities and polling](docs/entities.md) | Entity names, API resources, write access, and intervals |
| [Request behavior and recovery](docs/request-behavior.md) | Exact startup order, successful-once reads, retries, limits and cloud-load savings |
| [Additional cloud air conditioners](docs/bacon-air-conditioners.md) | Beta.6 Midea Climate Series controls; physical-device checks open |
| [Additional cloud air purifiers](docs/bacon-air-purifiers.md) | Beta.6 Air-family fans, readings and offered settings; physical-device checks open |
| [Additional cloud dehumidifiers](docs/bacon-dehumidifiers.md) | Beta.6 Dry-family controls and humidity readings; physical-device checks open |
| [PointT wallboxes](docs/pointt-wallboxes.md) | Beta.6 cp0 readings for commodule/commodule2; physical checks and command contracts open |
| [Home Assistant device presentation](docs/device-presentation.md) | One device per heating gateway, logical name groups and preserved user assignments |
| [Heating controls](docs/heating-controls.md) | Tabbed dialog and PointT climates from Beta.7, everyday use and confirmed appliance test |
| [PointT hot water](docs/pointt-hot-water.md) | Native PointT hot-water display from Beta.7, mode control, dashboard example and confirmed appliance test |
| [Connection recovery](docs/connection-recovery.md) | Token-lifecycle corrections from Beta.7, request behavior and remaining field checks |
| [Additional cloud water heaters](docs/bacon-water-heaters.md) | Beta.6 Midea/RWH readings and controls; physical-device tests open |
| [Additional cloud device connection](docs/bacon-connection.md) | Beta.6 connection foundation and outstanding family/device tests |
| [Faults and notifications](docs/faults.md) | Fault entities, lifecycle behavior, and automation examples |
| [Holiday periods](docs/holiday-periods.md) | Creating, editing, and deleting holidays with their heating settings |
| [Older holiday formats](docs/legacy-holidays.md) | ICOM date-only slots, WDDW2 on/off mode, write confirmation and device-check limits |
| [Resource catalog](docs/resource-catalog.md) | Observed PointT resources and terminology |
| [Water softener development](docs/water-softener.md) | Initial WE800i support, evidence and outstanding device verification |
| [Device expansion plan](docs/device-support-plan.md) | Implementation checkpoints for the requested device and program expansion |
| [Zone and DHW additions](docs/zone-and-dhw-fields.md) | Additional heating/cooling zone readings and DHW device types |
| [Zone controls](docs/zone-controls.md) | Heating/cooling modes and manual temperatures, temporary-override limits and outstanding physical checks |
| [App resource inventory](docs/app-resource-inventory.md) | Path model comparison, unresolved formats and adapter boundaries |
| [Reconfiguration](docs/reconfiguration.md) | Gateway selection, rediscovery, and polling profiles |
| [Diagnostics](docs/diagnostics.md) | Redacted diagnostics and request metrics |
| [Anonymized inventory](docs/anonymized-inventory.md) | Value-free compatibility inventory for maintainers |
| [Roadmap](docs/roadmap.md) | Remaining compatibility and release work |
| [Project status](docs/specification-status.md) | Published versions, planned work and the scope of actual validation |

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

For the appliance dialog's browser tests:

```bash
npm ci
npx playwright install --with-deps chromium
npm test
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting code or real-device
fixtures. Contributions for additional Bosch/Buderus installations are
welcome when they follow the repository's privacy and fixture rules.

## License and trademarks

The project is licensed under the [MIT License](LICENSE).

This is an independent community project. It is not affiliated with, endorsed
by, or supported by Bosch, Buderus, Bosch Home Comfort, or Home Assistant.
Names and trademarks are used only to describe compatibility.
