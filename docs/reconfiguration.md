# Reconfiguration and resource rediscovery

Bosch/Buderus Heating discovers the resources of the selected heating system
dynamically during initial setup and whenever the integration is reloaded. A
new discovery is useful after a firmware update or after changes to heating
circuits, domestic hot water, heat generators, or gateways.

## Open reconfiguration

1. In Home Assistant, open **Settings → Devices & services**.
2. Find **Bosch/Buderus Heating**.
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
the apps use different OAuth configurations.

## Polling profiles

| Profile | Operating values | Controls and energy | Slow counters |
|---|---:|---:|---:|
| Standard | 1 minute | 5 minutes | 15 minutes |
| Cloud-friendly | 2 minutes | 10 minutes | 30 minutes |

Static information is read during discovery and is not polled regularly.
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
