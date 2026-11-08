# Holiday periods

I also support [older ICOM and WDDW2 formats](legacy-holidays.md). ICOM uses
whole-day slots without clocks or custom names. WDDW2 provides one on/off mode
without calendar dates. The clock and name instructions below describe the
modern holiday format.

Open the integration's **Configure → Manage holiday periods** dialog to create,
edit, or delete a holiday. New dates stay empty, clocks default to 08:00, and
saved periods retain their actual times. Opening these dialogs uses cached
integration data and makes no additional PointT requests. Notification
settings remain accessible from the integration's configuration dialog.

Creating and editing use the same form with the supported settings together.
The form saves directly without an additional action menu.
For an existing holiday, the red **Delete holiday period completely** button
at the bottom of the form
opens a confirmation with its saved name and dates. Opening the confirmation
does not save the current draft. Leaving the confirmation unchecked returns
to editing with unsaved inputs intact. Deletion requires explicit confirmation.

Selecting an existing holiday opens the edit form directly. It shows the
saved name and dates together with the supported heating, hot-water,
ventilation, temperature, and circuit settings. A name field appears only when
the installation supports storing names.

The **Holiday periods** calendar remains an additional way to create periods
and change their names and dates or delete them. Its standard editor does not
include installation-specific heating settings.

Holiday support appears only when the connected gateway provides the required
PointT resources. The available settings may differ between installations;
the integration displays only options advertised by the current system.

Holiday times use the gateway's time zone, with Home Assistant's configured
zone as the fallback. A time zone supplied for an individual period takes
precedence for that period. The calendar and holiday sensors use the same
rules, including daylight-saving changes. Named zones are prepared
asynchronously when resource data changes, without additional cloud requests.

Urlaubszeiten verwenden die Zeitzone der Anlage. Fehlt eine gültige Angabe,
gilt die in Home Assistant eingestellte Zeitzone. Eine Zeitzone für einen
einzelnen Zeitraum hat für diesen Vorrang. Kalender und Urlaubssensoren
berücksichtigen dabei auch die Sommerzeit. Das Laden der Zeitzonen erfolgt
asynchron und benötigt keine zusätzlichen Cloud-Abfragen.

## Create a holiday period

To choose installation-specific modes before creating a period, open the
integration's **Configure → Manage holiday periods** dialog and select
**New holiday**. Enter its name, dates, advertised modes and circuits on the
same form, then choose the form’s submit button. If several gateways
can create holidays, select the installation first. The **New holiday** option
follows your profile language, independently of the server language. A date-only gateway
uses date selectors. The end date is exclusive, matching the HA calendar.

Both dates are initially empty. Select them explicitly; this avoids invalid
automatic date values in affected Home Assistant frontends. On installations
supporting clock times, separate start and end clocks default to **08:00** in
the installation's time zone. Both can be changed, including to **00:00**.
Editing an existing holiday shows its saved hours and minutes. Date and clock
inputs sit directly beside each other, including on narrow screens. A small
frontend module renders these native browser inputs and the delete button
inside the existing integration dialog. Both forms omit seconds, which are sent as 00 for entered times.
Times use 15-minute steps: minutes must be 00, 15, 30, or 45.
For example, 22:38 is rejected with a message at the affected field; choose an
appropriate quarter-hour time yourself. No automatic rounding takes place.
If an input needs correction, the dialog retains the name and dates you entered.
An end before or equal to the start shows a specific error at the end field.
Existing timestamps with nonstandard seconds remain unchanged when only
heating settings are edited.
The calendar also reports the quarter-hour requirement before sending a write.

Local times that do not exist or occur twice during a clock change are rejected
before sending a request. Choose a time outside the affected interval. PointT
holiday dates do not carry a UTC offset, so even an offset-aware calendar event
cannot distinguish the two occurrences of a repeated time. Periods can still
span a clock change when their start and end times are unambiguous.

The standard calendar dialog still supports creation when the usual defaults
are valid for this gateway. It never substitutes an unadvertised mode. Use
**Configure → Manage holiday periods → New holiday** if the defaults do not
fit your installation.


1. Open **Calendar** in Home Assistant.
2. Make sure **Holiday periods** is selected in the calendar list.
3. Select **Add event** or click the desired day or time in the calendar.
4. In **Calendar**, select **Holiday periods** if Home Assistant did not fill
   it automatically.
5. Enter a title and choose the start and end.
6. Leave **Repeat** set to **No repeat**. Leave **Location** and
   **Description** empty because PointT does not support these fields.
7. Select **Save**.

The integration sends the new period once and reads it back from PointT. Home
Assistant reports success only after the heating system has confirmed it.

A new period initially uses conservative defaults:
all advertised circuits, constant-temperature heating at 17 °C when
supported, hot water off, ventilation off when supported, and thermal
disinfection on when supported. Review these settings as described below.

## Configure heating, hot water, and ventilation

1. Open **Settings → Devices & services → Integrations**.
2. Find **Bosch/Buderus Home Comfort**. If several entries exist, use the one for
   the gateway that owns the holiday.
3. Select **Configure** on the integration entry.
4. Select **Manage holiday periods**.
5. Select the holiday period to open its settings.
6. Adjust the settings offered by the system:

   | Setting | Meaning |
   |---|---|
   | Apply for | Heating, hot-water, or ventilation circuits assigned to this holiday |
   | Central Heating | As Saturday, Constant temperature, OFF, or Setback |
   | Constant temperature | Room target used only with Constant temperature; the range advertised by this gateway |
   | Hot Water | As Saturday, OFF, Eco+, Eco, Comfort, or OFF with Thermal Disinfection |
   | Ventilation | As Saturday, OFF, fan level 1–4, or Demand |
   | Thermal disinfection | Whether thermal disinfection remains enabled during the holiday |

7. Select the form’s submit button.

The integration writes the configuration once and reads every field back.
The success message means that PointT returned the requested settings, not
merely that it accepted the request.

## Change the date, time, or name

1. Open the integration's **Configure → Manage holiday periods** dialog.
2. Select the holiday to open its details directly.
3. Change the prefilled name, start, or end. Heating settings can be adjusted
   on the same form.
4. Choose the form’s submit button. If an input needs correction, the entered values remain
   in the form. Times use quarter-hour steps, with no automatic rounding.

The end is exclusive. For example, an all-day holiday from August 1 through
August 8 has August 9 as its end date. For installations supporting clock
times, October 29 at 00:00 is sent as October 29 at 00:00, without conversion
to the previous day at 24:00 and without rounding to 23:45. Existing responses
using 24:00 are still read as the equivalent next-day 00:00.

Alternatively, open the event in **Calendar → Holiday periods**, select
**Edit**, and change its title, start, or end. Calendar views that support
drag-and-drop can also move or resize it. Keep **Repeat** set to **No repeat**.

Changing only the calendar details preserves the assigned circuits, heating,
hot-water and ventilation modes, thermal-disinfection setting, and constant
temperature. Home Assistant may attach a technical event identifier to an
ordinary edit; the integration handles that identifier without treating the
event as recurring.

Before creation or editing, the integration reads a complete current holiday
list. Edits merge only the fields changed in Home Assistant with that live
period, preserving changes made in the manufacturer app. Conflicting changes
to the same field stop the write; reopen the event and review its current
settings before submitting it again. Reading and writing are separate requests,
so an external edit during that brief interval cannot be excluded atomically.

If PointT returns different settings after a write, the integration displays
that actual holiday immediately. The dialog shows the requested and returned
periods and asks you to edit the existing entry. An HTTP acknowledgement alone
does not prove that every requested value was stored.

Creation is blocked when the live list already contains a holiday with the
same start and circuit selection, including an entry with a changed end time.
If creation remains unconfirmed, the dialog closes instead of offering another
submission of the same form. During that loaded integration session, a further
creation attempt for the same gateway only checks the unresolved request.
This also applies when switching between the options dialog and calendar.
Other gateways are unaffected. The unresolved-request guard is kept in memory,
so after a reload or restart only the current-list duplicate check remains.
Check the manufacturer app before retrying a request whose result is unknown.

## Delete a holiday period

1. Open **Configure → Manage holiday periods** and select the holiday.
2. Press **Delete holiday period completely** at the bottom of the edit form.
   This opens confirmation without first saving form edits, including incomplete
   or invalid date changes.
3. Check the saved name and dates in the confirmation. With multiple gateways,
   the installation is shown too.
4. Select **Delete this holiday completely** and submit. Leaving the
   confirmation unchecked returns directly to editing, preserving unsaved
   inputs.

Deletion removes the period from the heating system and the calendar. The
integration reads the current holiday list before deleting and rejects the
request if the selected period has changed since the confirmation was shown.
This adds one list read when deleting through the options dialog; opening the
menus or forms does not make additional cloud requests. An external change
between that read and the deletion cannot be excluded atomically.

Alternatively, open the event in **Calendar → Holiday periods**, select
**Delete event**, and confirm.

Deletion is also confirmed by reading the holiday list back from PointT.

## Limitations and troubleshooting

- Recurring events are not supported. A real recurrence rule or the
  **This and future events** range is rejected without writing anything.
- Location and description are not stored by PointT.
- If **Holiday periods** is read-only or **Manage holiday periods** is unavailable, the
  gateway has not supplied a current, complete write configuration. Reload
  the integration and try again; unsupported systems remain safely read-only.
- Changes made in the app while the configuration dialog is open are preserved
  for fields you did not edit. Conflicting changes to the same field are
  rejected; reopen the dialog to review the current values.
- A failed or unconfirmed write is never retried automatically. Check the
  cloud connection and submit the change again only after reviewing the
  current period.
