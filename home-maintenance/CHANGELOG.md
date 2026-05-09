# Changelog

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
