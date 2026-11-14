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
The one exception is a discovery notice while no entry exists: the integration
is not set up then, so its runtime cannot be ready yet. The notice only offers
to create an entry, and the local setup of that entry checks the boundary as
usual.

An existing entry with the provider available offers local setup in its
options and local connection settings. A native Zeroconf announcement first asks
the user to select an existing owner and installation; it never creates another
entry there or silently changes a saved address (a moved gateway is offered as a
repair note instead, see [a new gateway address](#a-new-gateway-address)). Without any entry, the notice
offers the setup with **Local access only** directly: after the user continues,
it creates the same entry as that choice and opens its local setup, with the
found address offered again. Nothing is created before the user continues. If
an entry was added while the notice was open, continuing asks for the owner
instead. An ignored notice stays ignored.

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
form defaults. The login field shows what is typed; the pass field is masked
with its show switch. Spaces and line breaks at the edges of both are removed
before the check; a value with other characters a label does not have, such as
a space inside it, is refused without a request. A blue-signal checkbox is a
user action, not proof that the gateway has accepted the physical release.

Each case has its own message: input the label cannot have (nothing sent), the
unticked checkbox (nothing sent), Login and Pass rejected by the gateway (400),
sign-in not released or the five minutes after the release passed (412, with
the button instruction of 1 second), a pause the gateway asked for (429, with
the remaining seconds when the gateway names them) and no answer in time. A
certificate that changed during the dialog leads back to its check instead of
repeating the sign-in. The second certificate page is the data port (9443);
its text says why it appears.

Resource trust is checked separately after credentials exist. Previously stored
trust applies only to the same address and port role with the exact full leaf
fingerprint. Every certificate change needs confirmation. No checkbox is selected
in advance. The transport checks the binding again before transmitting credentials.

The read check verifies the three base resources, then the trusted provider must
supply an identity contract for this exact owner, target and gateway. Missing
evidence is explained; the form has no override field or inferred global contract.
Confirmation of the result triggers fresh reads and the guarded native handover.

## A new gateway address

After a DHCP change the gateway announces itself at an address the saved local
access does not know. Names, TXT fields and addresses of an announcement are
never identity; the only proof is the certificate binding confirmed at setup.

1. Only an announcement whose addresses are all unknown to every configured
   access is considered, and only while an enabled local access does not reach
   its gateway (not reachable, certificate changed, or paused after failed
   connections; not during a pause the gateway asked for, never while it is
   connected, and not while it is still starting before a first attempt
   failed). So an access saved with a host name is offered another address
   only once that name no longer reaches the gateway.
2. At most two announced IP addresses (IPv4 first) get one TLS handshake each on
   the data port. Only addresses of a home network are considered: private
   IPv4 (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16), IPv4 link-local
   (169.254.0.0/16), unique local IPv6 (fc00::/7) and IPv6 link-local
   (fe80::/10); a public address is never contacted. Announced names are not
   resolved. No HTTP request is sent and
   no token leaves Home Assistant. Each address is checked at most once in five
   minutes, all announcements together at most eight times in ten minutes.
   Completing the handshake shows that the device holds the certificate's key.
3. Only when the full leaf certificate equals the stored binding of exactly one
   local access, and that access does not reach its gateway, a repair note
   offers the new address. Another certificate, a certificate known to several
   accesses, a switched-off access or one that works leaves the announcement an
   ordinary discovery notice, as before.
4. The proven address is kept in memory only. The note is not persistent: it
   disappears after a restart and as soon as the access reaches its gateway
   again.
5. Confirming the note requires a tick. Right before the change the certificate
   is checked again with a fresh handshake, the access must still not reach its
   gateway, and its saved address and binding must be unchanged. Only then the
   host of the access is replaced; the token, both certificate bindings and the
   stored gateway identity stay. The local access restarts and, as on every
   start, checks the certificate and the gateway identity before reading. A
   pause from the failed connections still runs out first.

Without a note, the address can still be changed with **Change local access**,
which needs a new sign-in or token for a new address.

## Recovery and cancellation

Pending attempts appear before another token is created. A rejected sign-in
(wrong label data, button not pressed, rate limit, full token list) or a request
that was not sent returns to the label form with its reason; the confirmed
certificate stays and nothing has to be discarded. Within one dialog, a
certificate confirmed for an address and port is accepted again for that address
and port when its fingerprint is identical, for example after Continue. Received
tokens can be rechecked; unknown creation outcomes allow only deliberate discard
or continuing later. Discard requires its own confirmation and removes only the local pending
copy. Gateway tokens are never automatically revoked. The options of an existing
local access show the gateway's token list and can revoke an old token of this
integration after a confirmation; see
[tokens on the gateway](local-enrollment.md#tokens-on-the-gateway).

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
