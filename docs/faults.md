# Faults and notifications

Bosch/Buderus Heating reads current PointT notifications without changing or
acknowledging them. The appliance display and manufacturer service information
remain authoritative.

## Entities

- **System fault** is on for a fault, critical fault, or notification whose
  class is unknown. Warnings and maintenance alone leave it off.
- **Active faults** counts the notifications represented by System fault and
  exposes bounded details in its `faults` attribute.
- **Active notifications** counts every current warning, maintenance message,
  fault, critical fault, and unknown notification.
- **System notifications** emits `appeared` and `resolved` events.

Details can include the code, subcode, normalized severity, component class,
summary, and first Home Assistant observation. At most 25 entries are attached
to an entity state; `truncated: true` indicates additional entries.

## Home Assistant bell notifications

Fault notifications are enabled by default. Open **Configure**, then
**Notifications** to turn them off for an installation. With multiple
installations, select those you want notifications for. This local setting
remains accessible while the integration is offline and takes effect without
a reload or an additional cloud request. **Holiday periods** remains available
as a separate configuration menu item.

Each installation has one notification containing its active faults, critical
faults and notifications with an unknown class, matching the System fault
sensor. Warnings and maintenance alone do not create a bell notification.
Details include known descriptions and codes, with a link to the device page.
Unknown codes are not given an invented explanation.

The notification is updated only when its content changes. Dismissing it
suppresses the same incidents across subsequent polls and Home Assistant
restarts. Resolving only part of that dismissed list does not recreate it.
A new incident, a recurrence after confirmed resolution, or a higher severity
can show the notification again. Turning the option off removes the
notification; explicitly enabling it again shows remaining faults.

After two complete, valid confirmations that all reported faults have ended,
a notification that is still visible changes to a resolved message. A dismissed
notification is not recreated just to announce resolution. Resolved messages
are not restored after a Home Assistant restart.

Retained faults whose current status could not be fully confirmed are
explicitly described as last reported faults. Missing, invalid or failed
responses never establish resolution. Notifications use the existing tracker,
polling intervals and read-confirmation rules, and do not acknowledge or reset
anything on the appliance.

Bell notifications and dismissals are shared by all Home Assistant users.
Their text uses the Home Assistant system language, while the configuration
dialog follows the user's profile language. German and English are available.
Mobile push notifications remain available through your own automations.

Only opaque incident fingerprints, dismissed severities and the local option
revision are added to private Home Assistant storage. This does not store
cloud payloads, device identifiers or tokens. Existing sensors and lifecycle
events remain available independently of this option.

## Timing and reliability

PointT is checked every five minutes during normal operation and every minute
while any notification is active. A new notification is emitted once. An
existing notification must be absent from two consecutive complete successful
reads before `resolved` is emitted.

A Home Assistant manual entity refresh makes the coordinator's dynamic groups,
including notifications, immediately due. Cloud backoff and circuit-breaker
protection still take precedence.

Temporary network failures, rate limits, unsupported optional resources,
malformed individual entries, and partial batch responses do not clear an
existing fault. The normalized active baseline is stored privately so a Home
Assistant restart does not repeat events that are still active.

The baseline also stores hashes of the required source paths so a restart
cannot forget an unreadable source. Device identifiers and raw source paths
are not included in that stored evidence. A failed or malformed notification
read restarts the two-read confirmation sequence; unrelated polling does not.
Without a valid baseline, an unreadable response leaves the aggregate state
unavailable instead of reporting that the system is healthy.

Baselines saved by older versions have no source evidence. Their existing
faults remain conservatively retained until reobserved, after which normal
resolution checks apply. Restarting again does not bypass this protection.

PointT did not provide an appliance timestamp in the verified K40 fault case.
In that situation `first_seen` and `observed_at` mean when Home Assistant first
saw the notification. The event reports `time_source:
home_assistant_observed` and does not invent an appliance start or end time.

## Automation examples

Replace the example entity IDs with those created on your system.

### Notify whenever the system enters a fault state

```yaml
automation:
  - alias: "Heating system fault"
    triggers:
      - trigger: state
        entity_id: binary_sensor.heating_system_fault
        from: "off"
        to: "on"
    actions:
      - action: notify.notify
        data:
          title: "Heating system fault"
          message: >-
            {{ state_attr('binary_sensor.heating_system_fault', 'summary')
               or 'Open Home Assistant and the appliance display for details.' }}
```

### React to each newly observed notification

```yaml
automation:
  - alias: "New heating notification"
    triggers:
      - trigger: state
        entity_id: event.heating_system_notifications
    conditions:
      - condition: template
        value_template: >-
          {{ trigger.to_state.attributes.event_type == 'appeared' }}
    actions:
      - action: notify.notify
        data:
          title: "New heating notification"
          message: >-
            {{ trigger.to_state.attributes.summary }}
            {% if trigger.to_state.attributes.code %}
            (code {{ trigger.to_state.attributes.code }})
            {% endif %}
```

## Unsupported installations

The aggregate entities remain unavailable if the gateway exposes no readable
current-notification resource. Other heating entities continue to work.
Optional resources that have never been read successfully and return HTTP 403
or 404 are skipped and retried only after their 24-hour capability pause or a
rediscovery. Previously discovered current-notification resources that return
404 remain eligible for their next regular poll and recover on a valid read.
Historical failure lists are capability-probed at startup but are not polled
repeatedly because no history entity currently consumes them.

Known code summaries are short, independently worded, and limited to verified
cases. Unknown codes remain visible without an invented interpretation. For
detailed service information, use the appliance display and the official
[Bosch error-code search](https://www.bosch-homecomfort.com/de/de/wohngebaeude/service-und-support/bosch-fehlercode-suche/).

> Home Assistant does not replace a qualified technician or manufacturer
> diagnosis. Do not disconnect or modify heating equipment merely to create a
> test fault.
