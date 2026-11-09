# Managed local access

I connected local setup and the polling service to the existing Home Assistant
configuration entry. A local connection owns its credentials, certificate trust,
request budget, discovery progress and resource observations. Cloud connections
have independent lifecycles and intervals.

This change provides regular local data to the managed source. Existing normal
entities use their cloud adapters unless **local values for existing entities**
are enabled or the cloud cannot be used; then verified [observation pairs](observation-pairs.md) prefer the
current local reading and fall back to the cloud. Local paths do not create additional Home Assistant devices.
A first installation using only local access remains outside this step.

## First link and subsequent starts

The first link to a retained PointT target requires fresh evidence from both
interfaces. Setup reads the local gateway brand, firmware and system information.
The identity policy reads the selected PointT gateway metadata and its original
system information through the existing cloud client and account request limits.

The gateway identifiers must agree exactly. At least one module identified by
its token and ID must have the same exact serial number and hardware identifier.
Floating-point serials, empty or zero identifiers cannot establish a match.
Duplicate module keys or conflicting comparable module identities reject the
link. Additional cloud modules are permitted; firmware versions and current
measurements do not establish identity. This strict policy can refuse a valid
installation whose metadata does not provide enough evidence.

The final save rechecks the selected target and both interfaces. The retained
target records the local identifier, verified-setup evidence and the version of
the comparison contract. It does not store the compared module inventory.
Removing the last connection preserves this relationship and the target.

Once this relationship exists, later local starts and local reconnection do not
need PointT. Every start still reads the local base resources using the saved
token and exact full resource certificate fingerprint. A different local
gateway stops the source. Every certificate change needs explicit confirmation,
including a renewed certificate with the same public key. A token rejection
requires deliberate recovery through local connection settings.

## Polling and recovery

Local connection settings offer a live-value target of 60 to 900 seconds in
60-second steps. Other resource classes keep their longer minimum intervals.
The dialog displays the confirmed resource count and currently available count
at the time it opens. It explains that local observations do not yet replace
normal entity values.

The [polling service](local-polling.md) supplies bounded discovery, fair reading,
independent expiry and resumable progress. All local setup and read operations
use the [shared gateway budget](local-budget.md). No local write capability is
enabled, even when a response reports a writeable flag.

When the gateway cannot be reached at startup (connection, name resolution,
timeout, 429 or 5xx), the shared gateway pause holds 60, 120, 300 and then 600
seconds, growing with every failed check. A longer valid Retry-After is kept
in full; a short or zero one (or a date in the past) never makes the checks
repeat sooner.

Only a rejected token, a changed certificate, a different gateway and firmware
older than the supported minimum stop automatic attempts. Every other startup
problem is tried again: the gateway answers, but not usably. Examples are an
untrusted TLS handshake, an unreadable answer, an HTTP error, a missing setup
resource, system information that is not complete yet, or an unrecognized
brand or firmware. The waits are 60, 120, 300, 600, 1200 and 2400 seconds,
then hourly; a longer Retry-After is kept. Each attempt still goes through the
shared gateway budget and waits for its pauses. From the fifth failed attempt of
one start, about 18 minutes in, a repair note names the cause and its fixed
code. It offers an attempt at once; a running gateway pause is still kept. A
successful start, a stop for one of the reasons above or removing the access
deletes the note.

Disabling, removing or replacing the connection cancels owned work;
late results cannot update another generation. Retained observations do not
become current again merely because a source restarts.

Since 0.9.0-alpha.2 the local access asks less often for values that rarely
change:

- descriptive system metadata of the gateway, for example the time zone,
  the bus and the tariff or iSRC support, is read hourly instead of every
  15 minutes; room-device and zone metadata such as battery level, name and
  assignment stays at 15 minutes;
- the gateway firmware stays at 15 minutes, so a firmware update is still
  noticed quickly;
- the three installation date signals are read once a day;
- device serial numbers are no longer searched or read;
- a path the gateway answers with 403, 404 or 406 is asked again after one
  hour, six hours and then weekly instead of daily. A firmware change, a new
  sign-in or changed connection settings and **Search the system again** still
  ask such paths again at once; a changed answer schema alone keeps the pause.

Because the polling classes changed, the local search starts once from the
beginning after updating to 0.9.0-alpha.2. Known readings stay and are read
as before, learned pauses are kept, and stored records of serial-number paths
are dropped. The search uses the usual search slots, so the gateway answers a
few more search requests until it is complete again. See
[local polling](local-polling.md).

## Diagnostics and validation

Schema 29 reports local source condition, identity confirmation, fixed failure
codes, request counts, discovery progress, remaining pauses and resource
freshness. Each resource shows target and actual intervals, age and availability.
Dynamic device and signal paths use catalog templates. Current values, tokens,
endpoint addresses, gateway identifiers and module identities are omitted.
Generating diagnostics makes no network requests.

Tests cover the actual managed runtime and native setup, fresh PointT evidence,
offline reuse, interval changes, independent cloud operation, invalid identity,
certificate changes, token rejection, expiry and cancellation. Loopback HTTPS
tests use the actual probe and read client. Browser checks use Home Assistant's
frontend with synthetic services in German desktop and English mobile profiles.
These checks do not replace installation and observation on a real gateway.

See [setup dialogs](local-setup-dialogs.md) and
[recoverable completion](local-completion.md).
