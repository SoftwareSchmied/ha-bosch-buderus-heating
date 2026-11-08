# Faults and notifications

Bosch/Buderus Home Comfort reads current PointT notifications without changing or
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
a notification that is still visible changes to a resolved message. Codes,
subcodes and known descriptions remain available in that message. When only
some faults have ended, active faults and confirmed resolutions appear in
separate sections. A fault that is still reported with a changed classification,
such as a warning, remains separately labelled until its absence is confirmed.

Each entry shows when Home Assistant first detected it; resolved entries also
show when Home Assistant confirmed resolution. These are observation times,
not the exact appliance start or repair times. The Home Assistant time zone
and the UTC offset at each observation distinguish repeated hours during a
clock change. A recurrence of the same code keeps its own observation times.

The visible message retains the latest 25 resolved incidents, with a count of
older omitted entries. Those details exist only in memory for that message
and are cleared when it is dismissed, notifications are disabled, or the
integration is unloaded or Home Assistant restarted. This is not a permanent
fault archive. A dismissed notification is not recreated just to announce
resolution. Resolved messages are not restored after a Home Assistant restart.
Already lost details from an older version cannot be recovered by updating.

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

## Restarts, interrupted writes and recovery

A normal Home Assistant shutdown flushes pending fault and dismissal state.
After a restart, restored active faults are labelled unconfirmed until fresh
reads establish their current state. A restart does not preserve a partially
completed resolution check: two complete, valid reads are still required.

State writes are delayed briefly and use Home Assistant's storage helper,
which writes a temporary file before replacing the destination. An abrupt
process termination recovers the last completed files. If a dismissal or a
new incident had not been written yet, the notification may appear again and
its first observation time may be reset. The same limitation applies after
a failed disk write. The two stores are not a single atomic transaction.
Missing or unreadable local storage does not prevent fresh cloud fault
detection, but cannot restore information that was never saved.

Tests terminate separate Linux processes before saving, during the temporary
write, and before and after replacement. They also cover corrupt JSON, disk
write errors, normal shutdown, repeated reloads, cancelled startup, and rapid
notification option changes. These are process and filesystem tests; they do
not simulate physical power loss or a failing storage controller.

## Local error list

With a local access, the gateway's local error list (`/notifications`) is a
second source of the same faults. It is read every minute anyway and uses the
same fields as the cloud, so one fault reported by both appears once and raises
one event. A fault only the local list reports appears as well.

A fault ends only when every source taking part confirms its absence in two new,
complete readings. A late or repeated local reading is no new evidence and
changes nothing, and a local access that is not ready confirms nothing until its
next new reading. While one source still reports a fault, the display counts as confirmed.
The local list never stands in for an unreadable cloud read: it covers only the
system-wide error list, not the cloud's other fault sources.

The local list takes part while it delivers new readings. After 15 minutes
without one (or three local query intervals, if that is longer), for example
during a lasting network problem, the cloud decides alone again, as without a
local access, until the next local reading. This age is measured independently
of clock changes. Switching off or
removing the local access hands the decision back to the cloud at once, and a
gateway that offers no local list leaves it to the cloud from the start. After a
Home Assistant restart, faults only the local list reported stay until the local
list confirms their absence or stays silent for those 15 minutes, so a restart
neither ends nor repeats them.

Without the cloud (a switched-off cloud access, also when it is switched off
while Home Assistant runs, or a cloud that needs a new sign-in or does not
answer once the stored inventory is older than 30 days, see
[source selection](source-selection.md)), the local list decides alone and
the fault entities show its state only while it is current.
Its first complete reading sets the known faults without events; a fault ends
only when it was reported by the local list and is then missing from it, so a
fault only the cloud knew stays until the cloud answers. When the cloud
returns, it continues with these faults. While the cloud starts, the plant
keeps showing the faults it handed over unchanged, for at most 15 minutes, so
a fault that changes in that short time is announced once, by the cloud.
While a cloud that is still switched on needs a new sign-in or does not answer
(and the stored inventory is recent), the cloud stays responsible: the local
list never ends a fault it reported, and the fault entities are unavailable.

The bell notification follows the local list in the same way when the plant
is shown without the cloud, with the same notification setting per
installation. A fault only the cloud knew stays in it as not confirmed, and a
notification you dismissed is not shown again for the same faults when the
cloud takes over.

The room devices' own error lists (`/devices/…/errors`, read locally where a
gateway offers them) are shown as extra information only: the active entries
appear in the `device_errors` attribute of the active faults sensor, as the
device number and the error type (for example `defectiveTempSensor`). They do
not count as faults, raise no event and no notification, and an entry whose
value is neither `active` nor `inactive` (such as a valve status) is not
shown, because its meaning as an error is not documented. They have not been
seen on a real device yet. Where the cloud offers a device error list, its
active rows are read as faults as before; a row marked `inactive` is not a
fault. No Wi-Fi lists, network data, serial numbers
or update logs are read for faults. Diagnostics show the state of the local list
under `local_error_list` (`not_attached`, `success`, `partial`, `no_answer`,
`stale`, `restored`) and the number of faults it reports under `local_faults`.

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
