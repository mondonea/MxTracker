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
- **publish_homeassistant_sensors**: Publishes Home Assistant sensors for dashboard cards. Default: `true`.
- **homeassistant_publish_interval_seconds**: Republishes sensors on a timer in addition to immediate updates after edits. Default: `300`.

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

The app publishes Home Assistant sensors directly through the internal Home Assistant API when `publish_homeassistant_sensors` is enabled:

- `sensor.mxtracker_overdue`
- `sensor.mxtracker_due_today`
- `sensor.mxtracker_upcoming_30_days`
- `sensor.mxtracker_due_14_days`
- `sensor.mxtracker_ready`
- `sensor.mxtracker_all_items`
- `sensor.mxtracker_on_track_percent`
- `sensor.mxtracker_completed_30_days`

The table sensors expose an `items` attribute with compact rows:

- `name`
- `category`
- `status`
- `status_key`
- `is_overdue`
- `due_date`
- `due_phrase`
- `days_until`
- `last_done`
- `repeat`
- `detail_url`

Notes are intentionally not published into Home Assistant state attributes. They remain available inside the app UI and CSV exports.

Example interactive due-soon dashboard card:

```yaml
type: markdown
title: Maintenance Due Soon
content: |
  {%- for item in state_attr('sensor.mxtracker_due_14_days', 'items') or [] %}
  {%- if item.is_overdue %}
  <ha-alert alert-type="error">[{{ item.name }}]({{ item.detail_url }}) - {{ item.due_phrase }} - {{ item.last_done }}</ha-alert>
  {%- else %}
  [**{{ item.name }}**]({{ item.detail_url }})<br>
  {{ item.category }} - {{ item.due_phrase }} - Last done {{ item.last_done }}
  {%- endif %}
  {%- else %}
  Nothing is due in the next 14 days.
  {%- endfor %}
```

Example full audit table:

```yaml
type: markdown
title: Home Maintenance Audit
content: |
  | Task | Category | Due | Last done |
  |---|---|---|---|
  {%- for item in state_attr('sensor.mxtracker_all_items', 'items') or [] %}
  | [{{ item.name }}]({{ item.detail_url }}) | {{ item.category }} | {{ item.due_date }} | {{ item.last_done }} |
  {%- endfor %}
```

The app also provides read-only endpoints for local integrations or troubleshooting:

- `/api/summary`
- `/api/tasks`

Ingress remains the preferred UI access path; do not expose a public port for the app.

Fallback REST sensor example:

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
