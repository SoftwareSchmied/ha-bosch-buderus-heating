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
Optional resources returning HTTP 403 or 404 are skipped and retried only
after the bounded capability pause or a rediscovery.
Historical failure lists are capability-probed at startup but are not polled
repeatedly because no history entity currently consumes them.

Known code summaries are short, independently worded, and limited to verified
cases. Unknown codes remain visible without an invented interpretation. For
detailed service information, use the appliance display and the official
[Bosch error-code search](https://www.bosch-homecomfort.com/de/de/wohngebaeude/service-und-support/bosch-fehlercode-suche/).

> Home Assistant does not replace a qualified technician or manufacturer
> diagnosis. Do not disconnect or modify heating equipment merely to create a
> test fault.
