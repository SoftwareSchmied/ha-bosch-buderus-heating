# Validity of readings

Sensors and binary sensors that show a single reading display it only while it
is valid. The rule is the same for both connections: a reading stays valid for
three polling intervals of its group, counted from the time it was received.
Each cloud interval is extended by the duration of a polling cycle, at most by
one interval, because the next poll starts one interval after the previous
cycle finished. It is the longest cycle since the reading was last received,
including a cycle still in progress, so a slow failed cycle followed by a
quick one does not end a reading a few seconds before the next answer.

| Cloud group | Standard profile | Cloud-friendly profile |
| --- | --- | --- |
| Live values | 3 minutes | 6 minutes |
| Control settings, energy counters, notifications | 15 minutes | 30 minutes (notifications 15) |
| Slow values | 45 minutes | 90 minutes |
| Static values such as firmware or brand | no limit | no limit |

The group is the one the reading is actually polled in. Local readings follow
their own polling classes in the same way; see [local polling](local-polling.md).

While an entity shows the local value of a paired reading, the cloud reads
that reading only every 15 minutes (see
[source selection](source-selection.md#cloud-reads-while-local-values-are-shown)).
Its cloud value then stays valid for those 15 minutes plus the limit of its
group, for example 18 minutes for a live value, so the slower rate alone
never makes it expire. When the entity shows the cloud value again, the limit
of its group counts from that moment, until the next cloud read, but never
beyond those 15 minutes plus the limit.

## Failed requests

- If there is no answer for the reading itself, the last good value stays
  displayed until its limit: timeouts and connection errors, rate limits (429),
  server errors (5xx), a rejected authorization (401), an unusable or truncated
  response, a reading missing from the response and readings not reached in an
  interrupted cycle. An HTTP 200 whose text, float or integer value is null
  counts as a reading missing from the response while a good value is stored:
  it does not replace that value, so the hot water water heater, for example,
  does not turn unavailable at once.
- An explicit answer for the reading ends it immediately: forbidden (403), not
  found (404), not acceptable (406), other client errors, invalid or unreadable
  data for the reading (also from the follow-up single read) or an unconfirmed
  write. It
  stays ended until the reading is read successfully again; a later failed
  request does not bring the old value back.
- A rejected sign-in starts reauthentication. With separately managed
  connections the cloud connection stops and its readings end at once,
  otherwise they end at their limit. A stopped connection is never shown again,
  also not through local values.
- During a long cloud pause, readings therefore expire after three intervals
  at the latest, instead of being shown with an unknown age.
- With an enabled local access, a current local reading of a paired reading
  keeps the sensor available while the whole cloud cycle fails or pauses,
  whether **Use local values for existing entities** is on or off.

A cloud cycle stops starting new requests after 50 seconds. Readings it did
not reach keep their value within its limit and are read first in the next
cycle. After a timeout or a network failure, the bounded follow-up single reads
stop as well instead of waiting for each one. After a firmware update, readings
the gateway does not report right away are kept and polled as before; their own
answers decide whether they end.

Expiry becomes visible on time: a timer re-evaluates the affected entities
without any additional request.

A cloud reading whose receive time lies more than one minute in the future no
longer counts, for example after the system clock was set back. A smaller
correction is not detected and extends the reading by at most that amount.

Which clock counts: local readings, request pauses and the silence limit of the
local error list use a clock that never jumps. The validity of cloud readings,
the age of the stored inventory (30 days, also for the dormant view of a
switched-off cloud access) and "last seen" times use the calendar time of Home
Assistant, because they must survive a restart. A clock change therefore shifts
these limits by the size of the change; the case of a clock set back is limited
as described above.

## Display values of several readings

Some entities only show cloud readings, without local values, and are no
control:

- **System pressure status (calculated):** the system pressure and its limits
  each keep their last good value within their validity limit;
- **Holiday mode active**, **Next holiday** and the **Holiday periods** calendar:
  the holiday readings within their validity limit. The calendar offers to
  create, change or delete a holiday only while its readings are current and
  the last cloud cycle succeeded;
- **System fault**, **Active faults** and **Active notifications:** the last
  fault state that a complete read confirmed, while later reads only got no
  answer, for three intervals of the fault list (15 minutes, 3 minutes while
  a notification is active), extended like a reading by the longest cycle
  since. An explicit answer for a fault source (for example 403), invalid
  entries, a rejected sign-in or a stopped cloud connection end it at once.
  Nothing is resolved or announced meanwhile.

The fault sources are read again in the first cycle that answers after such a
failure, after the other readings of that cycle, also when they are not due.
A gateway timeout (504) pauses them for 15 minutes only when other readings of
the same cycle answered: then they are slow themselves. While nothing answers,
nothing is requested for them in addition.

## A short cloud blip

A single failed refresh (a slow or unavailable sign-in service, a gateway
timeout, a server error) no longer turns the following entities `unavailable`
for a minute. The same rules hold for every cloud family:

- the generic controls (numbers, selects, switches, program names), the
  hot-water entity, the current COP, the primary-circuit spread and the
  calculated performance factors (since 0.9.2);
- the room controllers (TC100.2, CT200): the boost switch and its numbers, the
  operating season, the hot-water sensors and the disinfection time, the window
  sensitivity and valve problems of the radio devices, the heating program
  selection and the event calendar. The zone, radio device, system and
  heating circuit entities already stayed shown by the validity limit above and
  keep doing so;
- the air conditioners and wallboxes of a PointT gateway: all their sensors,
  controls and the wallbox history;
- the additional cloud devices of the second cloud (air conditioners, water
  heaters, dehumidifiers, air purifiers). The 90-second grace window of a planned
  token renewal is part of this rule now (see below).

They keep showing their last values while at most **three refreshes in a row
failed** (about three minutes with the one-minute refresh) and the outage began
less than **five minutes** ago. A fourth failed refresh, or the five minutes, ends
this; the entities are then `unavailable` as before and return with the first
refresh that answers. A reading that failed alone while the refresh as a whole
worked is shown with its last good value as long as it failed at most three
times in a row and is within its validity limit above.

For the additional cloud devices the refresh is the connection: a planned token
renewal starts the outage without counting a failure, a lost connection or a
failed reconnect (no connection, server error, request limit, pause) counts one.
The devices keep their availability and their last readings under the same
limits; the first message of the new connection ends the outage. A rejected
sign-in, a changed account, a stopped source, a rejected subscription or an
unexpected error ends it at once.

This only concerns what is shown. A change is never sent from a held value: it
needs a successful current cloud cycle and a current reading, and the setting is
read from the gateway again right before the write. While values are held, a
change is refused with the usual message that the cloud is not reachable, and
nothing is sent. This holds for every family above, including the structured
changes of the room controllers (boost, events, program selection) and the
commands of the wallbox and the air conditioners. Nothing is held after a
rejected sign-in, a sign-in that has to be repeated, a stopped cloud connection or
an unexpected error, and an explicit answer for a reading or for the whole request
(for example 403 or 404) is never held.

Expected, not yet observed on a real installation for the room controllers, air
conditioners, wallboxes and the additional cloud devices; it is covered by tests
that run real cycles with a simulated clock and by tests of each family.

The access token is renewed ten minutes before it ends (it was two). A request
that finds the renewal due goes on with the current token, and the renewal runs
in the background; if it fails or is slow, the next attempts follow after 30
seconds, doubling, and at the latest halfway to the end of the current token. The
refresh token is still presented only once per attempt, the token request
still has its 90 seconds, and a token the cloud refused is never reused.
Only the last 30 seconds of the token wait for the renewal as before.

Diagnostics show `cloud_hold` (limits, whether values are held now, failed
refreshes in a row, holds started and expired) and `last_failed_refresh` (time,
category, and per failed reading the path template, HTTP status, category and
count; at most ten entries, no identifiers) in each gateway `runtime`. The room
controller entities share the `cloud_hold` of the gateway `runtime`; the air
conditioners, the wallboxes and the additional cloud devices each have their own
`cloud_hold` in their report (see [diagnostics](diagnostics.md)).

## What keeps the strict rule

The climate entity of a heating circuit shows its mode, target and room
temperature like the sensors, as long as each reading is within its validity
limit, so a failed or older cloud cycle alone does not make it unavailable.
Changing it still requires current readings of a ready cloud. Every write
requires a successful current cloud cycle; this includes the away mode switch.
Controls and hot-water entities of the generic kind follow the blip rule
above for their display. Other control families (room controller entities,
zone and air-conditioner controls, wallbox controls) are not held and follow
the last cloud cycle as before. The dew point and the low-salt warning
keep following the last cloud cycle and their stored readings as before,
without local values. Nothing is restored as a current reading after a
restart.

Diagnostics show the base limits per group as `validity_limits_seconds` and the
number of readings that are currently past their limit as `resources_expired`
in each gateway `runtime`.
