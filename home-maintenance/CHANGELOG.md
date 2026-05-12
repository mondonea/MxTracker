# Changelog

## 2.0.5

- Changed normal Mark done actions to open a closure prompt for type and notes before recording maintenance history.
- Added a dedicated Close Maintenance Item screen for dashboard and audit workflows.
- Restricted Admin reopen actions to maintenance items with completion history and House todos that are actually closed.
- Added server-side guards so crafted reopen requests cannot reopen never-closed maintenance items or active House todos.

## 2.0.4

- Added closure type and closure notes to maintenance completion history.
- Added a richer item-detail closure form for Done, Skipped, Not needed, and Partial outcomes.
- Kept quick Done actions fast while preserving detailed audit fields in history, reports, admin repair, and CSV export.
- Expanded Admin mode with editable completion fields and typed-confirm reopen actions for both maintenance items and House todos.

## 2.0.3

- Added short-lived Admin mode for repair-only actions.
- Added an admin-only workflow to remove wrongly recorded maintenance completions with typed confirmation.
- Restored task last-done and next-due values from remaining completion history after a repair.

## 2.0.2

- Added human-readable IDs for maintenance items, House todos, and each maintenance completion iteration.
- Exposed IDs in detail pages, dashboard/focus/audit/calendar views, Home Assistant sensor payloads, Markdown tables, APIs, and CSV exports.
- Added ID-aware search for maintenance items and House todos.

## 2.0.1

- Grouped navigation into Maintenance, Projects, and System sections so recurring maintenance and House todos have clearer workflows.
- Renamed the root view to Dashboard and kept Add maintenance separate from Add house todo.
- Moved detailed asset fields into a collapsed Advanced asset details panel on maintenance forms.
- Improved the maintenance audit table spacing and mobile label behavior.
- Added typed DELETE confirmation and a trash affordance before deleting maintenance items or House todos.

## 2.0.0

- Expanded recurring maintenance items with asset, location, model, serial, filter/part size, purchase date, warranty date, priority, season, tags, supplies, and estimated-time fields.
- Added reusable per-item maintenance checklists with add, toggle, remove, and automatic reset after completion.
- Added annual reports with completion trends, overdue counts, due-next-14 counts, never-completed counts, supplies counts, and recent annual history.
- Added token-protected Home Assistant action endpoints for marking maintenance done, snoozing, and resolving detail links.
- Added a Home Assistant setup page with generated secret, `rest_command`, and actionable notification examples.
- Added lifecycle event recording and per-item lifecycle timelines.
- Added read-only annual report, backup health, and Home Assistant setup-example API endpoints.
- Added richer CSV exports with asset, completion, and lifecycle fields.
- Added unit coverage for 2.0 migrations, checklist lifecycle behavior, reports, backup health, lifecycle events, CSV output, and action API validation.

## 0.1.9

- Added search and filters to the All items maintenance audit view.
- Added House todos filters for active/all status, category, risk band, area, and search.
- Added dashboard shortcuts for the due-next-14-days focus view and calendar.
- Added stricter browser security headers for camera, microphone, geolocation, and same-origin framing.
- Added unit coverage for maintenance filters, House todo filters, and security headers.

## 0.1.8

- Added a House todos module for one-off repair projects alongside recurring maintenance.
- Added priority scoring based on likelihood, consequence, urgency, effort, and cost.
- Added a risk map, mobile-friendly todo list, todo detail pages, and nested checklist planning.
- Added start-gate checklist items that roll up into ready-to-start status and progress.
- Added a Home Assistant sensor and read-only API endpoint for active house todos.
- Added optional demo data seeding for fresh test databases.
- Added unit coverage for todo scoring, readiness, dashboard rendering, detail rendering, and sensor payloads.

## 0.1.7

- Added a monthly calendar view for maintenance due dates.
- Added cached Home Assistant area sync and per-item area assignment.
- Added Home Assistant area metadata to item APIs, CSV export, and sensor payloads.
- Moved delete access off the dashboard and kept it on item detail pages.
- Highlighted the active top navigation view.
- Removed overdue red panel styling when there are no overdue items.
- Closed SQLite connections after each transaction to reduce long-running resource use.
- Added unit coverage for calendar rendering, delete visibility, active navigation, area sync, area validation, sensor payloads, and database migrations.

## 0.1.6

- Expanded item detail pages with category, status, due timing, completion count, created date, and updated date.
- Extended tests to validate richer detail pages and item actions.

## 0.1.5

- Added unit tests for Home Assistant dashboard link generation and item detail routing.
- Fixed dashboard detail links to use the Supervisor ingress proxy URL that forwards query strings into the app.

## 0.1.4

- Fixed dashboard detail links to use Home Assistant's frontend ingress route.
- Kept query-string item routing so each table row opens the matching maintenance detail page.

## 0.1.3

- Fixed Home Assistant dashboard links by discovering the app's Supervisor ingress URL.
- Added a prebuilt due-next-14 Markdown table attribute for Home Assistant dashboard cards.
- Added query-string item routing so dashboard links can open item details even when routed through the ingress root.

## 0.1.2

- Added a 14-day Home Assistant dashboard sensor for overdue, current, and near-term maintenance.
- Added clickable detail links in Home Assistant sensor attributes.
- Added a focused due-soon page and per-item detail pages with completion history.

## 0.1.1

- Added Home Assistant sensor publishing for dashboard cards and maintenance audit tables.
- Added app options to enable or disable sensor publishing and tune the publish interval.
- Added compact table attributes for overdue, due today, upcoming, ready, and all maintenance items.

## 0.1.0

- Initial Home Assistant add-on.
- Added local web UI for recurring maintenance tasks.
- Added local SQLite persistence.
- Added read-only summary and task API endpoints.
- Added category support.
- Added dashboard metrics and urgency-grouped task tables.
- Added 7-day task snooze action.
- Added full maintenance item audit view.
- Added CSV exports for maintenance items and completion history.
- Added Home Assistant add-on configuration options for upcoming window and request logging.
- Added System, Light, and Dark theme selector.
- Fixed mobile layout with card-style task rows.
