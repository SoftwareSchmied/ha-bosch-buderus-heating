# The settings dialog in detail

The **Configure** dialog of an entry keeps its pages short. This page
collects the details that the pages themselves leave out, and explains how
the dialog behaves.

## Moving through the dialog

- Every menu below the main menu ends with **← Back**. Every selection form
  under the main menu (holiday periods, schedules, room controller schedules,
  operating the appliance, the token list of a gateway) offers **← Back** as
  its first choice, so the whole dialog no longer has to be closed with X.
- Forms after which another form follows show **Next**; the form that saves
  shows the button that ends the step.
- A finished action (a saved setting, a written holiday or schedule, a
  planned search, a connection test) shows its outcome on a page of its own.
  **OK** returns to the menu the action came from, so several things can be
  done in one session.
- A room controller switch point is shown before and after the change, as
  for the heating and hot water schedules. Nothing is sent before **OK** on
  that page; the change is sent once and read back.

## Language of the texts

Fixed texts, including the outcome pages, the connection test results, the
**Back** entries and the fixed choices such as a new system without the
cloud, follow the language of your user profile.

Some parts are built from current data and therefore follow the language of
the Home Assistant server: the list of accesses with their state, the status
line of one access, the weekly overview and the switch point names of a
schedule, and generic system names such as "Heating system 1". Home
Assistant gives a dialog no access to the profile language, and its
translations cannot hold a value that changes with the state.

## Names in selections

Systems are listed by the name of their device in Home Assistant (your own
name if you gave one). A system without a device in this entry, for example
one not set up yet, keeps its identifier, and two systems with the same name
get their identifier added. A room controller program is listed by its name;
its number only appears when it has no name or another program has the same
one. Devices of the second cloud service (for example a water softener) are
still listed by their identifier.

## Signing in again

**Configure → Connections → cloud access → Sign in again** opens the same
SingleKey ID sign-in page as a new cloud access, in the same dialog. After
the sign-in, the account is checked as for a renewed sign-in under
**Attention required**: another account is refused and the previous sign-in
is kept. A renewed sign-in that waits under **Attention required** for this
access is closed once the sign-in in the dialog has worked. A device search
that finds the sign-in expired continues with the same sign-in page.

If you paste the address of a SingleKey ID page (`singlekey-id.com/...`)
instead of the app address, the dialog says so and the sign-in link stays
valid. See [setup](setup.md) for where to find the app address.

## Adding a local access

**Configure → Connections → Set up local access** adds a local access: to a
system that has none yet, or as a new system without the cloud. The main
menu offers it only while an entry has no access to list. A gateway that Home Assistant finds in the network while
entries exist can be added to one of them or set up as a new entry with local
access only. Its address is preselected in the local setup of the new entry,
also when other gateways are announced; any other address can still be chosen.

The local setup asks to confirm a certificate once per port. The second page,
for the data port after the sign-in, has its own title. Not ticking the box
shows a note instead of the same page again; nothing is sent. The last page
saves with **OK**; to keep nothing, close the dialog.

## Local values for existing entities

While the cloud is running, this option decides whether existing sensors use
the local value of a reading that both accesses provide under the same
documented resource. Without the cloud (switched off, sign-in required,
paused, limited or unreachable), local values are always used. The option is
on by default for new setups with cloud and local access.

When enabled, the local value is used as long as it is current. If the local
value is outdated or missing, the current cloud value is used; if both are
outdated, the sensor becomes unavailable. Energy, start and operating-time
counters use the local value too, but only change their source when every
shown count is at least the last shown count, so a source change never makes
them count backwards. Adjustable settings keep using the cloud. See
[source selection](source-selection.md).

## Local polling

The counts on the page show the state when the dialog was opened. The interval
is the target for live values. Settings, energy values and diagnostics are
read every 5 minutes and counters every 15 minutes, or at this interval if it
is longer; static details are read daily. A value becomes unavailable after
three missed intervals. Large resource sets can take longer. Cloud polling
stays independent. Readings that the cloud also provides are read locally at
least as often as in the cloud. See [local polling](local-polling.md).

## Cloud polling profile

**Standard** reads current heating operating values every minute, settings
and energy values every 5 minutes and slower information every 15 minutes.
**Cloud-friendly** doubles these intervals. Active faults are checked every
minute in both profiles. The profile applies to the heating and wallbox
polling of the selected cloud access; additional cloud devices report changes
as messages. Sign-in, device checks and the confirmation after a change can
add requests outside this schedule. If a polling cycle takes longer than its
interval, the next one starts later.

## Wallbox

With a wallbox the settings menu has the step **Wallbox**. It allows the
integration to authenticate before a start (off by default) and takes the
optional authentication name of the vendor app. Without this permission,
the start button stays unavailable on a wallbox that requires
authentication. See [wallboxes](pointt-wallboxes.md).

## Reconfigure (older entries)

**Reconfigure** reloads the integration and performs a complete resource
discovery. Use it after a firmware update or when circuits, hot water
systems, heat sources or gateways have changed. **Standard** reads current
operating values every minute; **Cloud-friendly** reads them every two
minutes and all other changing values less often. Changing the app brand
requires a new SingleKey ID sign-in. See [reconfiguration](reconfiguration.md).
