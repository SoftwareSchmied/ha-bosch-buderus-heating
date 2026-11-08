# Validity of readings

Sensors and binary sensors that show a single reading display it only while it
is valid. The rule is the same for both connections: a reading stays valid for
three polling intervals of its group, counted from the time it was received.
Each cloud interval is extended by the duration of the last polling cycle, at
most by one interval, because the next poll starts one interval after the
previous cycle finished.

| Cloud group | Standard profile | Cloud-friendly profile |
| --- | --- | --- |
| Live values | 3 minutes | 6 minutes |
| Control settings, energy counters, notifications | 15 minutes | 30 minutes (notifications 15) |
| Slow values | 45 minutes | 90 minutes |
| Static values such as firmware or brand | no limit | no limit |

The group is the one the reading is actually polled in. Local readings follow
their own polling classes in the same way; see [local polling](local-polling.md).

## Failed requests

- If there is no answer for the reading itself, the last good value stays
  displayed until its limit: timeouts and connection errors, rate limits (429),
  server errors (5xx), a rejected authorization (401), an unusable or truncated
  response, a reading missing from the response and readings not reached in an
  interrupted cycle.
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

## What keeps the strict rule

Controls, climate and hot-water entities and every write require a successful
current cloud cycle. Sensors that combine several readings or show other state,
such as the dew point, the pressure status, the low-salt warning, fault
counters, the system fault and holiday sensors, keep following the last cloud
cycle and their stored readings as before, without local values. Nothing is
restored as a current reading after a restart.

Diagnostics show the base limits per group as `validity_limits_seconds` and the
number of readings that are currently past their limit as `resources_expired`
in each gateway `runtime`.
