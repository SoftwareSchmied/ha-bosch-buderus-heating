# Reconfiguration and resource rediscovery

Bosch/Buderus Home Comfort discovers the resources of the selected heating system
dynamically during initial setup and whenever the integration is reloaded. A
new discovery is useful after a firmware update or after changes to heating
circuits, domestic hot water, heat generators, or gateways.

## Operating the appliance or reconfiguring the integration

From **0.8.2-beta.7**, **Configure > Operate appliance**
opens the everyday controls dialog. It does not change the selected gateways
or trigger rediscovery. **Reload values** in that dialog uses the latest cached
polling results; it is not an integration reload or a new cloud request.
See [Heating controls](heating-controls.md).

The **Reconfigure** menu described below manages the integration's gateway
selection and polling profile and reloads its resources. It is already available
in the published releases. Ordinary token renewal does not reload the integration;
the [development recovery changes](connection-recovery.md) retain the shared
token manager across setup retries and reloads when credentials still match.

## Open reconfiguration

1. In Home Assistant, open **Settings → Devices & services**.
2. Find **Bosch/Buderus Home Comfort**.
3. Open the three-dot menu on the integration card.
4. Select **Reconfigure**.

Home Assistant first loads the current gateway list from the PointT cloud. The
next dialog lets you review the app brand, selected gateways, and polling
profile.

## Run a complete discovery

Select **Submit**, even if no setting needs to change. The integration reloads
and reads the complete resource tree for every selected gateway. Newly added
supported values become entities. Registry entries belonging to deliberately
deselected gateways are removed.

Changing between Bosch and Buderus requires a new SingleKey ID sign-in because
the two brands use different OAuth configurations.

## Search the system again

Entries with connections offer **Search the system again** in their options.
It searches the system once more for parts and readings without a reload:
every running cloud access plans one discovery with its next regular update,
and every running local access searches its resources again in its usual
search slots. The search keeps to the usual limits for requests. It never
removes an entity: a reading it does not find again stays and keeps being
read until its own answer says it is gone. While requests are paused (for
example after the cloud asked for a pause, or while the local read budget
pauses), while a search is already running, or when no access is ready, the
dialog says so and starts nothing.

## Polling profiles

| Profile | Operating values | Controls and energy | Slow counters |
|---|---:|---:|---:|
| Standard | 1 minute | 5 minutes | 15 minutes |
| Cloud-friendly | 2 minutes | 10 minutes | 30 minutes |

Static information is read during setup and is not polled regularly. Reviewed
supplementary leaves have their own phase after functional discovery; failed
reads can recover later without a reload. "Once" means one accepted read per
integration load. An explicit reload starts a fresh metadata session, while an
automatic retry of unfinished setup preserves it when the configuration and
credentials are unchanged. An existing account rate-limit pause survives both.
See [Request behavior and recovery](request-behavior.md) for exact paths,
retry delays and limits.

Both profiles use batch requests, partial-failure handling, backoff, negative
caching, and a circuit breaker. The cloud-friendly profile is intended for
installations where the PointT cloud repeatedly reports rate limits.

## Repair notifications

After three rate-limit events during one running Home Assistant session, the
integration creates a notification under **Settings → System → Repairs**. The
offered repair flow can switch the affected account to cloud-friendly polling
and then reload the integration.

Invalid credentials are not treated as a rate limit. Home Assistant uses the
separate **Reauthenticate** flow for that condition.
