# Holiday periods

Holiday periods have two parts in Home Assistant:

- The **Holiday periods** calendar manages the name, start, and end.
- **Configure holiday** in the integration options manages how the heating
  system behaves during that period.

This separation is necessary because Home Assistant's standard calendar
editor has no fields for heating circuits, operating modes, temperatures, hot
water, or ventilation.

Holiday support appears only when the connected gateway provides the required
PointT resources. The available settings may differ between installations;
the integration displays only options advertised by the current system.

## Create a holiday period

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

A new period initially uses conservative defaults based on the official apps:
all advertised circuits, constant-temperature heating at 17 °C when
supported, hot water off, ventilation off when supported, and thermal
disinfection on when supported. Review these settings as described below.

## Configure heating, hot water, and ventilation

1. Open **Settings → Devices & services → Integrations**.
2. Find **Bosch/Buderus Heating**. If several entries exist, use the one for
   the gateway that owns the holiday.
3. Select **Configure** on the integration entry.
4. Select **Configure holiday**.
5. Select the holiday period and continue.
6. Adjust the settings offered by the system:

   | Setting | Meaning |
   |---|---|
   | Apply for | Heating, hot-water, or ventilation circuits assigned to this holiday |
   | Central Heating | As Saturday, Constant temperature, OFF, or Setback |
   | Constant temperature | Room target used only with Constant temperature; 5–30 °C, or a stricter gateway range |
   | Hot Water | As Saturday, OFF, Eco+, Eco, Comfort, or OFF with Thermal Disinfection |
   | Ventilation | As Saturday, OFF, fan level 1–4, or Demand |
   | Thermal disinfection | Whether thermal disinfection remains enabled during the holiday |

7. Select **Submit**.

The integration writes the configuration once and reads every field back.
The success message means that PointT returned the requested settings, not
merely that it accepted the request.

## Change the date, time, or name

1. Open **Calendar → Holiday periods**.
2. Open the event, select **Edit**, and change its title, start, or end. You
   can also move or resize the event directly in calendar views that support
   drag-and-drop.
3. Keep **Repeat** set to **No repeat**, then save.

Changing only the calendar details preserves the assigned circuits, heating,
hot-water and ventilation modes, thermal-disinfection setting, and constant
temperature. Home Assistant may attach a technical event identifier to an
ordinary edit; the integration handles that identifier without treating the
event as recurring.

## Delete a holiday period

1. Open the event in **Calendar → Holiday periods**.
2. Select **Delete event** and confirm.

Deletion is also confirmed by reading the holiday list back from PointT.

## Limitations and troubleshooting

- Recurring events are not supported. A real recurrence rule or the
  **This and future events** range is rejected without writing anything.
- Location and description are not stored by PointT.
- If **Holiday periods** is read-only or **Configure holiday** is absent, the
  gateway has not supplied a current, complete write configuration. Reload
  the integration and try again; unsupported systems remain safely read-only.
- If a period changed while the configuration dialog was open, close the
  dialog, reopen it, and apply the change to the freshly read values.
- A failed or unconfirmed write is never retried automatically. Check the
  cloud connection and submit the change again only after reviewing the
  current period.
