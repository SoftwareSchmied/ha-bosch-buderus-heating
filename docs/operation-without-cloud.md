# Cloud access, local access and operation without the cloud

## In short

A system can be connected in two ways:

- **Cloud access:** the sign-in of the manufacturer's app. It shows the
  system's readings and lets you operate it: modes, setpoints, hot water,
  time programs and holidays.
- **Local access** (K40/K40RF gateways): a direct connection to the gateway
  in your home network. It **only reads** and cannot change anything, but it
  updates many readings more often and can add technical readings the cloud
  does not offer, for example from the refrigerant circuit or pumps.

Using **both** is recommended. A reading that both deliver is shown once, from
the local access while its value is current, otherwise from the cloud.
Anything you change is always sent through the cloud.

Without the cloud, a system with a local access keeps showing its readings
from the local access, **read-only**: there are no controls, time programs or
holidays. When the cloud comes back, it takes over the same entities; entity
IDs and history stay.

| What you do | What you get |
| --- | --- |
| Set up with cloud and local access | full operation, readings updated often |
| Set up with local access only | readings only, no operation |
| Switch the cloud access off | local readings continue, no operation |
| Remove the cloud access | local readings continue, no operation |
| Add the cloud access later | the same system gains its operation, entities and history stay |

## Details

| Situation | Where it is described |
| --- | --- |
| How a reading's source is chosen when both deliver it | [source selection](source-selection.md#rules) |
| Cloud access switched off, sign-in required or no answer | [when the cloud stops](source-selection.md#when-the-cloud-stops) |
| A system set up with local access only | [local access only](source-selection.md#a-system-set-up-with-local-access-only) |
| Adding the cloud access later | [adding the cloud later](source-selection.md#adding-the-cloud-later) |
| Removing the cloud access | [removing the cloud access](source-selection.md#removing-the-cloud-access) |
| A longer time without the cloud | [without the cloud for more than 30 days](source-selection.md#without-the-cloud-for-more-than-30-days) |
| Readings only the local access provides | [entities](entities.md#readings-only-the-local-access-provides) |
| Faults from the local error list | [faults](faults.md) |
| Setting up the local access | [setup](setup.md), [local setup dialogs](local-setup-dialogs.md) |
| Which gateways and firmware the local access supports | [compatibility](compatibility.md) |

## Without the cloud, in a little more detail

- **Shown:** current local readings, counters that continue from the last
  shown count (never backwards), and the faults the gateway's local error list
  reports, with their bell notification (same notification setting as with
  the cloud).
- **Not available:** all controls (modes, setpoints, hot water, extra hot
  water), time programs, holidays, notifications that only the cloud sends,
  and readings only the cloud provides. Some state texts may be missing when
  the local answer does not list its allowed values.
- **Never shown as entities:** identifiers, addresses, names and network data
  of the gateway.
- **Requests:** showing the local readings sends nothing to the cloud. A
  cloud access that is switched on but cannot sign in or get an answer keeps
  trying as before.
