# Documentation

Open the add-on from the Home Assistant sidebar after installation.

## Home Assistant Experience

MxTracker is designed to be managed from Home Assistant:

- The web UI opens through Home Assistant Ingress.
- The sidebar entry is named **Maintenance**.
- The add-on exposes no public port by default.
- Add-on settings are managed from the Home Assistant add-on configuration page.
- Add-on backups include the local `/data` database.

## Add-on Options

These options are available from the Home Assistant add-on configuration page:

- **upcoming_window_days**: Controls how many days appear in the dashboard's Upcoming section. Default: `30`.
- **request_logging**: Enables request logs for debugging. Default: `false`.

## Installation Source

The add-on metadata points to:

```text
https://github.com/mondonea/MxTracker
```

Home Assistant must be able to fetch that repository URL for the normal custom repository install flow to work. Avoid embedding GitHub tokens in URLs.

## Task Fields

- **Name**: The maintenance task, such as `Replace AC filter`.
- **Category**: A high-level home area such as HVAC, Appliances, Exterior, Yard, or Safety.
- **Notes**: Optional details.
- **Every**: The recurrence interval.
- **Next due**: The first or next due date.

When a task is marked complete, the next due date is calculated from the completion date.

## Dashboard Views

- **Overdue** shows missed tasks first.
- **Current** shows tasks due today.
- **Upcoming** shows tasks due in the next 30 days.
- **Later** shows tasks already scheduled farther out.

The dashboard metrics include total tasks, overdue tasks, due-today tasks, upcoming tasks in the next 30 days, recent completions, and an on-track percentage.

Use **Snooze 7d** when a task is known but needs to move out of the current work queue.

## Audit View

Use **All items** to see the full maintenance list in one place. This view is intended for periodic household audits and shows:

- Task name.
- Category.
- Current status.
- Last completed date.
- Next due date.
- Recurrence interval.

The audit and dashboard views collapse into mobile-friendly cards on narrow screens so the add-on remains usable from the Home Assistant mobile app or a phone browser.

## Theme

Use the header control to switch between:

- **System**: Follows the device/browser color mode.
- **Light**: Uses a bright modern Home Assistant-style palette.
- **Dark**: Uses a darker high-contrast palette.

## CSV Exports

The audit view includes CSV exports for the full item list and completion history.

Spreadsheet safety note: exported text cells that begin with formula-like characters are prefixed so spreadsheet apps do not evaluate them as formulas.

## Home Assistant Dashboard Sensors

The app provides read-only endpoints for Home Assistant sensors:

- `/api/summary`
- `/api/tasks`

The recommended first integration is a local REST sensor that reads `/api/summary`. Ingress remains the preferred UI access path; do not expose a public port for the add-on.

Example Home Assistant REST sensor:

```yaml
sensor:
  - platform: rest
    name: MxTracker Overdue
    resource: http://local-home-maintenance:8099/api/summary
    value_template: "{{ value_json.overdue }}"
    json_attributes:
      - due_today
      - upcoming_window
      - total
      - ready_count
      - completed_30_days
      - on_track_percent
      - next_task
```

The exact internal add-on hostname can vary by install source. If this is not reachable, keep using the sidebar UI and add dashboard sensors later through a dedicated Home Assistant integration.

## Data Storage

The SQLite database is stored at:

```text
/data/home-maintenance.db
```

Home Assistant add-on backups include `/data` by default.
