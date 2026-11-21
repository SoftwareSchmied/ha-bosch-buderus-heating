# Local access for iCom gateways (beta)

Beta: expected to work, but not yet tested on a real gateway. I am looking for
testers (see [Reporting](#reporting)).

iCom gateways (KM200, KM100, KM50, IP inside, MB LAN 2 and MB LANi, the
IVT/Bosch IP module) can be read from your home network as well as through
the cloud. The local access reads over the gateway's own encrypted web
interface on port 80 and supplies readings like any other local access; it
does not replace the cloud. Writing stays with the cloud, with a fresh read
and a read-back as before. German version: [icom-local-access.de.md](icom-local-access.de.md).

## What it needs

- The gateway is also connected to the Bosch or Buderus app and set up in
  this integration through the cloud connection. The local access always
  belongs to such a system; a system that only exists locally is not offered
  for iCom gateways.
- The **gateway password** from the sticker on the device (the dashes are
  ignored) and the **private password** (the personal password of the
  gateway): the one you set for it in the app when pairing the gateway, or in
  the old device app of the gateway (for example MyDevice). If you never set
  one, the local access cannot be set up. The most common cause of "could not
  be decrypted" is a private password that differs from the one the gateway
  has: if you are not sure which one that is, set one again in the app and use
  the same one here. A password entered on the display of the heating appliance
  proves nothing about it.
- The address of the gateway in your home network (an IP address or host
  name). A fixed address (a reservation in the router) saves you from setting
  it up again.

## Set up

1. Open the integration, **Configure**, then **Connections**.
2. Choose **Set up local access (iCom gateway)**.
3. Choose the system, unless only one fits.
4. Enter the address, the gateway password and the private password.

The setup reads the gateway's own ID once. It must be the gateway ID the cloud
knows for that system; otherwise nothing is saved and the dialog says so
(another gateway at that address, or a wrong address). A wrong password is
named separately.

If the gateway reports no usable ID (none at all, empty or a placeholder such
as -1), the setup compares what the gateway answers with what the cloud has
just read for the same paths: firmware and hardware version, type, brand,
outdoor temperature (within 2 K, cloud reading at most 15 minutes old), system
pressure and the number of starts. At least two readings must agree, at least
one of them a live value (temperature, pressure or starts), and none may
clearly contradict. This needs a running cloud connection with current values;
if it is not there yet, wait a few minutes and try again. A gateway that does
report a usable but different ID is never accepted this way. The way used is
shown in the diagnostics (`identity_method`); later checks then read one stored
version value instead of the ID. If that value changes later (a replaced gateway, or a firmware
update when the gateway reports no hardware version), the access stops as for a
different gateway; run **Change iCom access data** once more. To change the address or a password later, open the access
under **Connections** and choose **Change iCom access data**.

Local values stay **off** after the setup. The access reads and checks the
gateway, but the existing entities keep the cloud values until you switch on
**Use local values for existing entities** under the access. Without a usable
cloud, local values are used anyway (see [source selection](source-selection.md)).

## What it reads

Only the readings of the [pair catalog](observation-pairs.md): the same paths the
cloud offers for outdoor temperature, circuit and hot water status, setpoints,
modulation, flame, pressure, supply and return temperature, energy and operating
counters. The circuits (heating circuits, hot water circuits, heat sources) come
from the directories of the gateway. A path the gateway does not offer is
asked again after a week. The local access creates no entity of its own, and
it reads no faults, schedules, holiday settings or recordings in this version.

A value read locally replaces the cloud value only when its type and unit
agree with the cloud's, and only for readings the pair catalog lists.

## Gentle access

These gateways are slow, and several reports say they stop answering after days
of frequent requests until their power is interrupted. The local access
therefore:

- sends one request at a time, with a gap of at least one second after each;
- reads live values every two minutes at the earliest, settings and energy
  values every five, counters and the firmware every 15, descriptive values
  hourly and identifiers daily (the access's **local polling frequency** can only
  make this slower; values below two minutes count as two minutes);
- stops asking a path that answered 404 or 403 for a week;
- pauses all reading after two failed requests in a row (no answer within
  10 seconds, no connection, a server error): first for a minute, then two,
  five, ten, twenty and finally thirty minutes for as long as the failures
  continue. An overload answer (429, 503) pauses for at least its Retry-After
  time. A reading that works again ends the series;
- keeps the values of a confirmed access during the first pause, but withdraws
  them after three missed periods or from the second pause on;
- stops completely, without further requests, when two answers in a row cannot
  be decrypted (wrong or changed password; a single one, also right after a
  restart, is only looked at again after a minute) or when the gateway reports
  a usable ID other than the one it was set up with. The access then shows
  "sign-in required" or "not reachable" until you enter the data again;
- treats an ID that says nothing (none, empty or a placeholder such as -1) as
  no answer for the check: the access pauses and looks again, it does not stop;
- makes no request while no cloud connection supplies the system any more (for
  example after the cloud connection was removed), and goes on by itself when
  one does again;
- checks the gateway ID again every hour and after every pause.

If the gateway stops answering altogether, switch the access off under
**Connections**, interrupt the gateway's power for a moment and switch the
access on again.

## Privacy and security

- Both passwords are stored in the configuration entry of Home Assistant, like
  the tokens of the other connections. They are part of your Home Assistant
  backups. They are never sent anywhere, not even to the gateway: the key for the
  encryption is derived from them in Home Assistant.
- The traffic stays inside your home network. It is plain HTTP on port 80 whose
  content is encrypted with the vendor's own method (AES with a key derived from
  the two passwords); it is not a TLS connection, and the method is the
  vendor's, not mine.
- The diagnostics of the integration name counts, ages and fixed codes. They
  never contain a password, a key, an address or a gateway ID or serial number.
- The access is read-only. It never changes anything on the gateway.

## What the dialog and the status say

| Text | Meaning |
|---|---|
| The gateway did not answer | No connection within the time limit; check the address and port 80. |
| The answer could not be decrypted | One of the two passwords is wrong, or one was changed in the device app. Most often the private password differs from the one the gateway has; check it in the app where you set it. |
| The gateway is busy | It answered with an overload message; try again later. |
| It could not be matched with the system | The gateway reports no usable ID, and its values do not agree closely enough with the cloud values (or the cloud has no current values yet). Try again in a few minutes. |
| Not the gateway of the selected system | The ID read locally is not the cloud's gateway ID. |

## Reporting

The protocol is implemented from public descriptions and checked against a
simulated gateway only. If you have an iCom gateway, please try the beta
([how to get test versions](setup.md#beta-versions)) and tell me in an
[issue](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/issues)
or in [Share your installation](https://github.com/SoftwareSchmied/ha-bosch-buderus-heating/discussions/81)
what happened, with the diagnostics of the integration attached. Of particular
interest: whether the setup's ID check passes (and by which way, see
`identity_method` in the diagnostics), which values are shown, and how
long the gateway runs without a hang.

## Not included

Writing through the local access, systems without a cloud connection, faults
and schedules from the local access, the gateway's cloud (XMPP) connection and
gateways without a local interface.
