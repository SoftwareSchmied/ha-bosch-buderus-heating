# Cloud and local observation pairs

I keep readings from PointT and from the local K40/MX400 interface in one
internal observation format. It records the source, saved access, installation,
functional group, raw path, interpreted value or named parts, unit, validity and
separate request, receive and device times. Missing values, empty answers,
device error values and state markers never become zero. The format contains no
write rights: controls keep using the cloud metadata of the access that writes.

## Verified pair catalog

Two readings are only treated as the same measurement if a versioned catalog
lists their pair. Version 1 contains only identical paths that the official
local API documents and that returned the same type, unit and value through both
interfaces on my installation. Templates such as `{hc}`, `{dhw}` and `{hs}`
apply the same rule to every reported circuit; readings are only paired for the
same reported identifier.

The catalog covers temperatures, setpoints, operating states, pressure,
modulation, electric power, system type and firmware as well as energy, start
and working-time counters. Counters are marked separately because switching
their source needs its own rules. Network addresses, Wi-Fi details, device
lists, configuration objects and notifications are not paired. Readings that
only one interface provides stay single-source.

At runtime, a pair is only compared if both readings are valid and have the
same type, unit and shape. The result is `agree`, `differ` or `incomparable`;
both readings are always kept. Values are never averaged or added, and no source
is selected here.

## Diagnostics

Schema 32 adds `observation_pairs` per installation: catalog version, number of
cloud and local readings in the catalog, observed pairs, results, reasons for
incomparable pairs, counter pairs, pairs with both readings current and the
largest receive-time gap. It contains no values, paths or identifiers and makes
no requests.

## Local values for existing entities

The local connection settings offer **Use local values for existing entities**.
It applies to the installations linked to that local access and is on by
default when a system gets its first local access or a system set up with
local access only joins the cloud; a choice saved before stays. When enabled,
existing sensors and binary sensors of paired readings show
the current local value. If it is outdated or missing, the current cloud value is
shown; if both are outdated, the entity becomes unavailable. Without the cloud
(switched off, a new sign-in needed, paused, rate-limited or unreachable),
current local values are shown whether the option is on or off. See
[validity of readings](reading-validity.md) and
[source selection](source-selection.md).

Counters may use local readings too, but never count backwards when their
source changes; see [source selection](source-selection.md#counters).
Readings that can be adjusted through the cloud, climate and hot-water entities
and all controls keep using the cloud. Type and unit must match the
cloud resource that created the entity. Equivalent spellings of the same unit
count as equal (`C` and `°C`, `min` and `mins`, `wh` and `Wh`); values with
different units such as W and kW are never converted automatically.

Error markers (`open`, `short`, `overflow`, `underflow`, `invalid`) never
supply an entity, and neither does a number that the cloud declares an error
marker. Other named states, for example a setpoint reported as `off` with the
value 0, are ordinary values on both interfaces: the entity shows the same
number as with the cloud alone. Entity IDs, names, devices and history
stay unchanged, and no additional entity or device is created. The selection
starts no requests: both connections keep their own polling, and a new local
reading updates the entities immediately. Switching the option does not restart
either connection.

Diagnostics report per installation whether local values are enabled and how
many eligible entity sources currently use the local or the cloud value.
