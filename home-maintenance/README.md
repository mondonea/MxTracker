# Home Maintenance Tracker

Track recurring home maintenance tasks and one-off house repair todos from inside Home Assistant.

## Features

- Add, edit, and delete maintenance tasks.
- Add, edit, and delete one-off house todos such as leaks, broken fixtures, and repair projects.
- Mark tasks complete.
- Track overdue, due today, upcoming, and later items.
- View dashboard metrics for overdue work, current work, upcoming work, on-track percentage, and recent completions.
- Open a focused 14-day dashboard view for overdue, current, and near-term work.
- View maintenance due dates in a monthly calendar.
- Click into each maintenance item to review details, actions, and completion history.
- Audit a full list of maintenance items with last completed and next due dates.
- Export maintenance items and completion history to CSV.
- Group tasks into practical home categories such as HVAC, Appliances, Exterior, Yard, and Safety.
- Link maintenance items to Home Assistant areas synced from your existing Home Assistant setup.
- Rank house todos with a likelihood x consequence risk map, urgency, effort, cost, readiness, and progress.
- Use house todo detail pages with nested checklist steps and start-gate checkboxes that roll up into ready/in-work progress.
- Snooze a task for 7 days when it needs to move out of today's work.
- Switch between System, Light, and Dark themes from inside the app.
- Use mobile-friendly card views inside Home Assistant on a phone.
- Store task history locally.
- Publish Home Assistant sensors for summary cards and maintenance table attributes.
- Provide read-only JSON API endpoints for local integrations and troubleshooting.

## Privacy

This add-on stores data locally in `/data/home-maintenance.db`. It does not send maintenance data to GitHub, Home Assistant Cloud, or any external service.

The app only accepts requests from Home Assistant's Ingress gateway by default.

Request logging is disabled by default to reduce disk noise and avoid storing household activity details in logs.

The add-on exposes no public port by default. Use the Home Assistant sidebar entry named **Maintenance** to open the app.

## Resource Use

The add-on is intentionally lean:

- Python standard library only.
- One lightweight background publisher for Home Assistant sensor updates.
- No frontend build step.
- Single-process HTTP server for Home Assistant Ingress use.
- SQLite with indexes for due-date and history lookups.
- Form payloads are capped to limit accidental or abusive memory use.

## Home Assistant Management

The add-on is configured for Home Assistant-first use:

- Ingress web UI.
- Sidebar panel.
- Local `/data` storage included in add-on backups.
- Add-on configuration options for the Upcoming window, sensor publishing, publish interval, and debug request logging.
- Optional demo data seeding for a fresh test database.
- Home Assistant sensors for dashboard cards and maintenance audit tables.
- Cached Home Assistant area names for assigning maintenance items to rooms or areas.

## Dashboard

The dashboard is grouped around how maintenance is usually handled:

- **Overdue**: tasks that need attention first.
- **Current**: tasks due today.
- **Upcoming**: tasks due in the next 30 days.
- **Later**: scheduled tasks beyond the next 30 days.

Each row shows the category, due timing, recurrence, last completion date, linked Home Assistant area when set, and quick actions. Delete is available from the item detail page, not the main dashboard.

## House Todos

The **House todos** view is for one-off repair projects, not recurring maintenance. Each todo tracks likelihood, consequence, urgency, effort, cost, status, Home Assistant area, optional target date, and detailed notes.

The priority score is inspired by a classic likelihood x consequence risk matrix, then adjusted for household reality: urgency and consequence push an item upward, while high effort and cost add drag. The risk map plots likelihood vertically and consequence horizontally; larger dots indicate higher expected cost.

Each todo has its own detail page with a nested checklist. Checklist items marked **Start gate** decide whether the project is ready to start. Checklist completion also updates progress and can move a todo into ready, in-work, or done states.

## Demo Data

For local testing or a fresh Home Assistant test install, enable `seed_demo_data` once. The app only seeds demo records when the database has no maintenance items and no house todos, so it will not add examples into an existing tracker.

## Calendar

The **Calendar** view shows maintenance items by next due date with month navigation. Each calendar item links to its detail page.

## Audit And Export

The **All items** view shows every maintenance item with category, Home Assistant area, status, last completed date, next due date, recurrence, and quick actions.

CSV exports are available for:

- Maintenance items.
- Completion history.

## Theme

Use the theme control in the header to choose **System**, **Light**, or **Dark** mode. The preference is stored locally in the browser cookie for the Home Assistant session.

## API

Read-only endpoints:

- `/api/summary`
- `/api/tasks`
- `/api/todos`
- `/api/areas`

These are intended for local Home Assistant use.

## Home Assistant Sensors

When sensor publishing is enabled, the app creates these Home Assistant sensors through the internal Home Assistant API:

- `sensor.mxtracker_overdue`
- `sensor.mxtracker_due_today`
- `sensor.mxtracker_upcoming_30_days`
- `sensor.mxtracker_due_14_days`
- `sensor.mxtracker_ready`
- `sensor.mxtracker_all_items`
- `sensor.mxtracker_on_track_percent`
- `sensor.mxtracker_completed_30_days`
- `sensor.mxtracker_house_todos`

The due-next-14 sensor includes a ready-to-render `markdown_table` attribute for dashboard cards. The table rows link to MxTracker item detail pages and include category, Home Assistant area, due date, last done, and recurrence. Notes are not published into Home Assistant state attributes; use the app UI or CSV export for full detail.

The house-todos sensor publishes the top active projects by priority score, including status, risk band, progress, readiness, and links back to each todo detail page.

When running inside Home Assistant, the app periodically reads area IDs and names through Home Assistant's local template API and caches them in SQLite for fast form rendering.

## Local Development

For local testing outside Home Assistant, set:

```text
HOME_MAINTENANCE_ALLOWED_CLIENTS=127.0.0.1,::1
```

To enable request logs while debugging, set:

```text
HOME_MAINTENANCE_LOG_REQUESTS=true
```

Run the unit tests with:

```text
python3 -m unittest discover -s home-maintenance/app/tests -p 'test_*.py'
```
