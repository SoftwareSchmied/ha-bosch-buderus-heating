# Cloud pauses after rate limits

When the Bosch cloud answers with HTTP 429, I stop all cloud requests of the
affected saved cloud access until the requested time has passed. This covers
regular polling, device checks, reconnection, additional cloud devices and
token renewal. Local access to the gateway is independent and keeps running.

## Requested duration

- `Retry-After` is read as seconds or as an HTTP date. The SingleKey ID token
  endpoint, PointT and additional cloud devices use the same interpretation.
- The requested time is honored in full, with a minimum of 60 seconds. A safety
  bound of 24 hours only protects against nonsensical values.
- Without a usable value, 300 seconds apply. A rate-limited item inside a
  batch response also uses 300 seconds.
- A new rate limit can extend a running pause but never shortens it.

## Restarts

The pause belongs to one saved cloud access, identified by the configuration
entry and its connection. It is stored in
`.storage/bosch_buderus_heating.cloud_pauses` and loaded before the first cloud
request after a Home Assistant restart.

Real time that passed while Home Assistant was stopped is deducted. A restored
pause is never longer than the saved remaining time; if the system clock moved
backwards, the saved remaining time applies. A forward clock jump during the
downtime cannot be told apart from real downtime.

A replacement sign-in for the same access keeps its pause. Removing an access
or the whole entry deletes its pause. A newly added access receives a new
connection ID and therefore never inherits a pause from an earlier one.

## Diagnostics

Each cloud connection reports `cloud_pause` with `active`,
`remaining_seconds`, `restored_after_restart` and `persistence_available`.
Diagnostics make no cloud request and contain no account identifiers.
