# Local setup evidence

I added `local_probe.py` as the shared, side-effect-free foundation for manual
addresses and native gateway discovery. It checks candidate addresses and the
three initial K40/MX400 responses. It does not start discovery, open a connection,
create tokens, alter configuration, register devices or enable local polling.

## Candidate addresses

`LocalEndpoint` normalizes an IPv4 address, IPv6 address or ASCII DNS hostname.
It accepts an optional trailing DNS root dot and brackets around IPv6. DNS
hostnames are case-insensitive. Unicode hostnames must already use their ASCII
DNS representation. An address copied from a browser or typed with a port is
taken as the address alone: spaces at either end, a leading `http://` or
`https://`, closing slashes and one of the ports 80, 443, 9442 or 9443 are left
out (the two service ports stay fixed). Another port, credentials, paths,
ambiguous legacy numeric IPv4 forms, loopback, unspecified and multicast
addresses are rejected.

Link-local IPv6 requires a scope, such as `fe80::7%eth0`. The scope belongs to
the Home Assistant host. This parser checks syntax; the later transport must
validate the actual interface. It must also handle URL encoding of scoped IPv6
explicitly rather than treating this host field as a ready-made URL.

Syntax validation does not resolve DNS or establish local reachability, device
identity or transport trust. DNS resolution and TLS checks remain mandatory in
the eventual client. Global unicast IPv6 addresses are not rejected solely for
being global, since they can address a device on a local network.

The endpoint always assigns token management to HTTPS port **9442** and resource
reads to HTTPS port **9443**. Discovery cannot change those roles.

`parse_discovery_candidate` accepts a projection of the native Home Assistant
Zeroconf result for `_hvac-open-api._tcp.local.`. The future adapter supplies
address strings in the platform's preferred order. The advertised port must be
one of the two known service ports, but it is not used to select the data port.
The official guide does not specify which of them the mDNS announcement uses.

The helper retains usable alternatives, counts unusable candidates and removes
repeated normalized endpoints within one hint. At most 16 advertised addresses
plus the hostname are considered. This bound is defensive, not a vendor limit.
The result has no device identity or persistent discovery ID. Different hints
must not be merged as devices based on matching names or addresses.

## Response assessment

`ProbeContext` records the selected endpoint and the existing `IdentityContext`
before any future I/O. The transport must preserve that original context on
each response. Changing the owner, connection, generation or endpoint makes
earlier responses stale. Retagging old data with a newer context is forbidden.
This context is provenance, not a substitute for a certificate trust binding.

`assess_local_probe` accepts collected `ProbeResponse` objects for:

| Resource | Validation |
| --- | --- |
| `/gateway/brand` | Optional. One of the five documented brands is kept; `unknown`, an empty text, any other brand, another form of the answer or a client error answer (4xx other than 401, 407 and 429, for example 403 or 404) counts as not reported and does not stop the setup. The probe then reads the next resource. A refused token, a pause request, a redirect, a server error (which pauses the gateway's requests) and a transport failure still count. |
| `/gateway/versionFirmware` | Matching resource ID. The version is read by its numbers: one to four dotted numbers, a leading `v` and a suffix after `-`, `+` or a space (for example `16.00.00-rc1`) are accepted; missing numbers count as 0. Another form leaves the version unknown and never stops the setup. The minimum 15.00.01 applies only to a K30/K40-class gateway (see below). |
| `/system/basicInfo` | Local identity parser: completed status (any letter case) and an exact string gateway ID. The timestamp and the `writeable` flag are not used and may have any form. |

The minimum firmware applies when the module list names a K30/K40-class
gateway: the row at a gateway place (`Tok` 72 or 75) reports a hardware name
starting with K30, K40, MX300 or MX400, or, without a hardware name, module
type 17. Other gateways with the same local service count their versions
differently and have no minimum. The running access applies the same rule to
a firmware change.

Responses may arrive in any order. Duplicate responses are rejected rather
than choosing the first or last identity. Missing responses produce an
incomplete result. Explicit errors take precedence over incompleteness.

A completed successful result is named `ready_for_identity_comparison`.
Only that result exposes a parsed gateway identity. The caller must still use
the selected-target matcher, including a verified source contract and checks
for ownership conflicts. The probe does not supply that contract or change
the existing installation key. It requires no cloud login or cloud request.

The reported module count is only the length of the returned list. The existing
identity parser does not interpret module contents. An empty list does not
invalidate an otherwise valid gateway identity, but it proves no connected
appliance or usable functions. No module, heating circuit or solar circuit is
turned into a device by this assessment.

## Failure handling and bounds

Results use fixed codes for missing resources, denied access, rejected tokens,
rate limits, service failures, incomplete system data and old firmware. A redirect is rejected. Transport failures retain a separate
category for DNS, connection, timeout, untrusted or changed certificates.

The core never retries a request. The future transport remains responsible for
request deadlines, Retry-After handling, streaming response limits, certificate
verification, redirects and checking both port roles. Proposed setup deadlines
are not yet implemented by this pure parser.

The JSON parser accepts UTF-8 bytes and checks a maximum of 256 KiB, 32 nesting
levels and 8,192 values. It rejects duplicate keys, nonfinite numbers, numeric
overflow and invalid resource envelopes. These are implementation bounds. The
streaming client must apply the byte limit before allocating an entire body.
Firmware uses three ASCII numeric components of up to six digits each; unknown
suffixes are reported as unverified instead of being silently removed.

Addresses, request provenance, response bodies and gateway IDs are omitted from
the corresponding dataclass representations. Assessments retain only known
brands, parsed versions, fixed issue codes and the private identity object.
Callers must still avoid logging raw inputs or response objects through other
serialization mechanisms.

## Scope and validation

I test manual and discovery inputs against the same endpoint representation,
partial and conflicting responses, changed connection provenance, malformed
JSON and the existing selected-target comparison. The tests use synthetic data.
They do not establish actual network reachability or certificate behavior.

The next work is the local transport and token lifecycle, then the native setup
dialog and resource discovery. Local appliance access remains read-only even
when another resource advertises `writeable: 1`. Functional cloud/local mapping
is a later layer and is not inferred from equal resource paths.

The contract follows the official [K40/MX400 guide](https://github.com/bosch-home-comfort/api-docs/blob/78e36b3b57e11ac6266558acba96c13bee88ceff/docs/k-40-rf.md)
and [OpenAPI specification](https://github.com/bosch-home-comfort/api-docs/blob/78e36b3b57e11ac6266558acba96c13bee88ceff/openapi/k-40-rf.yaml).
See also the [gateway identity contract](gateway-identity.md).
