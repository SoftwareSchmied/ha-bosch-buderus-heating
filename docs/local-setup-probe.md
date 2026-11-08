# Local setup evidence

I added `local_probe.py` as the shared, side-effect-free foundation for manual
addresses and native gateway discovery. It checks candidate addresses and the
three initial K40/MX400 responses. It does not start discovery, open a connection,
create tokens, alter configuration, register devices or enable local polling.

## Candidate addresses

`LocalEndpoint` normalizes an IPv4 address, IPv6 address or ASCII DNS hostname.
It accepts an optional trailing DNS root dot and brackets around IPv6. DNS
hostnames are case-insensitive. Unicode hostnames must already use their ASCII
DNS representation. Whole URLs, credentials, paths, ambiguous legacy numeric
IPv4 forms, loopback, unspecified and multicast addresses are rejected.

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
| `/gateway/brand` | Matching resource ID, read-only string envelope and one of the five documented brands. |
| `/gateway/versionFirmware` | Matching string envelope and numeric version at least 15.00.01. |
| `/system/basicInfo` | Existing strict local identity parser, including completed status and an exact string gateway ID. |

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
rate limits, service failures, incomplete system data, old firmware and unknown
firmware syntax. A redirect is rejected. Transport failures retain a separate
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
