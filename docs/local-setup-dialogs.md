# Native local setup dialogs

The local setup uses Home Assistant's config and options flows. It
extends the [completion coordinator](local-completion.md) and preserves the
existing entry, installation, device and entity identities.

## Availability and entry points

Local setup requires both storage-format support and a registered
`LocalSetupSupport` provider whose local runtime is ready. The managed runtime
registers its provider during integration setup, with entry version 4.2.
Changing the minor version alone cannot expose these dialogs.
Menus, discovery and direct calls to every setup stage enforce this boundary.

An existing entry with the provider available offers local setup in its
options and local connection settings. A native Zeroconf announcement first asks
the user to select an existing owner and installation. It never creates another
entry or silently changes a saved address; to set up a system without the cloud,
add the integration with **Local access only**.

Adding the integration offers three choices: cloud access, cloud and local
access, and local access only. With cloud and local access, the new entry's
options open directly at the local setup (Home Assistant 2026.5 or newer). With
local access only, an entry without any access is created and its options open
at the local setup of a new system. The installation step also offers **New
system (local only)** in entries that already have systems. A new system needs
no cloud comparison: its gateway must not belong to any configured system, in
this or another entry, under any identity. It is stored as a local installation
with the gateway's local identity, the reported brand and controller model
(never serial numbers) and shown read-only from its local readings. Removing its
local access keeps the entry and the system; the local setup can run again.

The cloud access can join such a system later in the same entry, after a fresh
comparison of the cloud's system information with the one read through the
running local access (see
[adding the cloud later](source-selection.md#adding-the-cloud-later)). The
system then becomes the cloud's object under the same target, and the stored
comparison serves later local setups of it; a local setup attempt started for
the local-only system before cannot complete any more.

The manifest declares `_hvac-open-api._tcp.local.`. Home Assistant dispatches the
announcements; this integration creates no separate scanner. Hints are bounded
to 64 service records and expire after ten minutes. Advertised ports and addresses
are checked by the shared parser. TXT properties and service names are not device
identity. The address field also accepts manual input when discovery is absent.

## Authentication and trust

The user can supply a local token or sign in with the device label. An imported
token does not require the authentication port. Label authentication first
inspects that port's certificate, obtains explicit confirmation and asks for the
physical release. Device login and password are neither saved nor returned as
form defaults. A blue-signal checkbox is a user action, not proof that the gateway
has accepted the physical release.

Resource trust is checked separately after credentials exist. Previously stored
trust applies only to the same address and port role with the exact full leaf
fingerprint. Every certificate change needs confirmation. No checkbox is selected
in advance. The transport checks the binding again before transmitting credentials.

The read check verifies the three base resources, then the trusted provider must
supply an identity contract for this exact owner, target and gateway. Missing
evidence is explained; the form has no override field or inferred global contract.
Confirmation of the result triggers fresh reads and the guarded native handover.

## Recovery and cancellation

Pending attempts appear before another token is created. A rejected sign-in
(wrong label data, button not pressed, rate limit, full token list) or a request
that was not sent returns to the label form with its reason; the confirmed
certificate stays and nothing has to be discarded. Within one dialog, a
certificate confirmed for an address and port is accepted again for that address
and port when its fingerprint is identical, for example after Continue. Received
tokens can be rechecked; unknown creation outcomes allow only deliberate discard
or continuing later. Discard requires its own confirmation and removes only the local pending
copy. Gateway tokens are never automatically revoked.

Resource and authentication `Retry-After` share the persistent
[gateway budget](local-budget.md). Closing or reopening the form, acknowledging
an attempt or restarting HA does not clear a pause. A rate limit without a stated
delay uses a one-minute defensive pause. Network errors are shown as fixed,
translated categories without raw response text or secrets.

Bounded checks finish within the native form request, which displays HA's loading
state. This avoids losing a fast completion before the frontend subscribes to
progress events. The next form receives no previous input, so confirming a
certificate cannot also confirm saving. Refreshing the result does not repeat
reads or save. Closing a dialog invalidates its active proof and cancels its task
while the journal and handover layers finish protected storage and lifecycle
operations.

## Validation

The tests exercise both native HA flow managers, existing-owner discovery, manual
addresses, both authentication methods, a synthetic TLS token endpoint, pauses,
recovery, certificate changes, missing identity evidence and cancellation. Browser
tests use the actual HA frontend with synthetic credentials, simulated transports and the actual
managed runtime. They cover a German desktop profile and an English mobile profile, each
with a different HA system language. No real installation or appliance is used.

The [managed runtime](local-runtime.md) performs the first fresh PointT/local
identity comparison and retains its scoped result for later offline starts.
Regular local reads feed the managed source; combining them with normal entity
values still requires capability mappings.
