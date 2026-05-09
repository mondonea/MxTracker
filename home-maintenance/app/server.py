#!/usr/bin/env python3
import csv
import html
import io
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.parse import quote


DB_PATH = os.environ.get("HOME_MAINTENANCE_DB_PATH", "/data/home-maintenance.db")
HOST = os.environ.get("HOME_MAINTENANCE_HOST", "0.0.0.0")
PORT = int(os.environ.get("HOME_MAINTENANCE_PORT", "8099"))
ALLOWED_UNITS = {"days", "weeks", "months", "years"}
CATEGORIES = ["General", "HVAC", "Plumbing", "Electrical", "Appliances", "Exterior", "Yard", "Safety", "Other"]
STATUS_ORDER = {"overdue": 0, "due_today": 1, "upcoming": 2}
CSRF_COOKIE = "hm_csrf"
THEME_COOKIE = "hm_theme"
THEMES = {"system", "light", "dark"}
MAX_FORM_BYTES = 16 * 1024
ALLOWED_CLIENTS = {
    item.strip()
    for item in os.environ.get("HOME_MAINTENANCE_ALLOWED_CLIENTS", "172.30.32.2").split(",")
    if item.strip()
}


def bool_env(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def bounded_int_env(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


LOG_REQUESTS = bool_env("HOME_MAINTENANCE_LOG_REQUESTS", False)
UPCOMING_WINDOW_DAYS = bounded_int_env("HOME_MAINTENANCE_UPCOMING_WINDOW_DAYS", 30, 1, 365)
PUBLISH_HOMEASSISTANT = bool_env("HOME_MAINTENANCE_PUBLISH_HOMEASSISTANT", True)
HOMEASSISTANT_SYNC_INTERVAL_SECONDS = bounded_int_env("HOME_MAINTENANCE_HA_SYNC_INTERVAL_SECONDS", 300, 30, 86400)
HOMEASSISTANT_API_BASE = os.environ.get("HOME_MAINTENANCE_HA_API_BASE", "http://supervisor/core/api").rstrip("/")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "").strip()
HOMEASSISTANT_REQUEST_TIMEOUT = 3
HOMEASSISTANT_ENTITY_PREFIX = "mxtracker"
HOMEASSISTANT_DASHBOARD_WINDOW_DAYS = 14
HA_PUBLISHER = None



def today_iso():
    return date.today().isoformat()


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_date(value):
    return date.fromisoformat(value)


def add_months(start, months):
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    days_in_month = [
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ][month - 1]
    return date(year, month, min(start.day, days_in_month))


def calculate_next_due(from_date, interval_count, interval_unit):
    if interval_unit == "days":
        return from_date + timedelta(days=interval_count)
    if interval_unit == "weeks":
        return from_date + timedelta(weeks=interval_count)
    if interval_unit == "months":
        return add_months(from_date, interval_count)
    if interval_unit == "years":
        return add_months(from_date, interval_count * 12)
    raise ValueError("Unsupported interval unit")


def classify_due(next_due_on):
    due_date = parse_date(next_due_on)
    current = date.today()
    if due_date < current:
        return "overdue"
    if due_date == current:
        return "due_today"
    return "upcoming"


def days_until(next_due_on):
    return (parse_date(next_due_on) - date.today()).days


def due_phrase(days):
    if days < 0:
        return f"{abs(days)} day{'s' if abs(days) != 1 else ''} overdue"
    if days == 0:
        return "Due today"
    return f"Due in {days} day{'s' if days != 1 else ''}"


def recurrence_phrase(interval_count, interval_unit):
    unit = interval_unit[:-1] if interval_count == 1 and interval_unit.endswith("s") else interval_unit
    return f"Every {interval_count} {unit}"


def status_label(status):
    return {
        "overdue": "Overdue",
        "due_today": "Due today",
        "upcoming": "Upcoming",
    }[status]


def escape(value):
    return html.escape("" if value is None else str(value), quote=True)


def connect_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db():
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'General',
                notes TEXT NOT NULL DEFAULT '',
                interval_count INTEGER NOT NULL,
                interval_unit TEXT NOT NULL,
                next_due_on TEXT NOT NULL,
                last_completed_on TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "category" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN category TEXT NOT NULL DEFAULT 'General'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS completion_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                task_name TEXT NOT NULL,
                completed_on TEXT NOT NULL,
                next_due_on TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_next_due_on ON tasks(next_due_on, name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_category ON tasks(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_completed_on ON completion_history(completed_on DESC, id DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )


def get_tasks():
    with connect_db() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY next_due_on ASC, name ASC").fetchall()
    tasks = []
    for row in rows:
        task = dict(row)
        task["category"] = task.get("category") or "General"
        task["status"] = classify_due(task["next_due_on"])
        task["status_label"] = status_label(task["status"])
        task["days_until"] = days_until(task["next_due_on"])
        task["due_phrase"] = due_phrase(task["days_until"])
        task["recurrence_phrase"] = recurrence_phrase(task["interval_count"], task["interval_unit"])
        tasks.append(task)
    return sorted(tasks, key=lambda item: (STATUS_ORDER[item["status"]], item["next_due_on"], item["name"].lower()))


def get_task(task_id):
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def enrich_task(task):
    task["category"] = task.get("category") or "General"
    task["status"] = classify_due(task["next_due_on"])
    task["status_label"] = status_label(task["status"])
    task["days_until"] = days_until(task["next_due_on"])
    task["due_phrase"] = due_phrase(task["days_until"])
    task["recurrence_phrase"] = recurrence_phrase(task["interval_count"], task["interval_unit"])
    return task


def get_enriched_task(task_id):
    task = get_task(task_id)
    return enrich_task(task) if task else None


def get_history(limit=20):
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT task_name, completed_on, next_due_on
            FROM completion_history
            ORDER BY completed_on DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_task_history(task_id, limit=50):
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT task_name, completed_on, next_due_on, created_at
            FROM completion_history
            WHERE task_id = ?
            ORDER BY completed_on DESC, id DESC
            LIMIT ?
            """,
            (task_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_all_history():
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT task_name, completed_on, next_due_on, created_at
            FROM completion_history
            ORDER BY completed_on DESC, id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_completion_count(days=30):
    since = (date.today() - timedelta(days=days)).isoformat()
    with connect_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM completion_history WHERE completed_on >= ?",
            (since,),
        ).fetchone()
    return int(row["count"])


def get_setting(key, default=""):
    with connect_db() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def remember_ingress_base_path(base_path):
    base_path = normalize_base_path(base_path)
    if not base_path:
        return
    if get_setting("ingress_base_path") != base_path:
        set_setting("ingress_base_path", base_path)
        request_homeassistant_sync()


def get_ingress_base_path():
    return normalize_base_path(get_setting("ingress_base_path", ""))


def summarize(tasks):
    counts = {"overdue": 0, "due_today": 0, "upcoming": 0}
    upcoming_window = 0
    for task in tasks:
        counts[task["status"]] += 1
        if task["status"] == "upcoming" and task["days_until"] <= UPCOMING_WINDOW_DAYS:
            upcoming_window += 1
    next_task = tasks[0] if tasks else None
    ready_count = counts["due_today"] + counts["overdue"]
    on_track = 100 if not tasks else round(((len(tasks) - counts["overdue"]) / len(tasks)) * 100)
    return {
        "total": len(tasks),
        "overdue": counts["overdue"],
        "due_today": counts["due_today"],
        "upcoming": counts["upcoming"],
        "upcoming_window": upcoming_window,
        "ready_count": ready_count,
        "completed_30_days": get_completion_count(30),
        "on_track_percent": on_track,
        "next_task": public_task(next_task) if next_task else None,
    }


def homeassistant_task_row(task, base_path=""):
    detail_path = f"/item/{task['id']}"
    return {
        "id": task["id"],
        "name": task["name"],
        "category": task["category"],
        "status": task["status_label"],
        "status_key": task["status"],
        "is_overdue": task["status"] == "overdue",
        "due_date": task["next_due_on"],
        "due_phrase": task["due_phrase"],
        "days_until": task["days_until"],
        "last_done": task["last_completed_on"] or "Never",
        "repeat": task["recurrence_phrase"],
        "detail_path": detail_path,
        "detail_url": homeassistant_link(detail_path, base_path),
    }


def homeassistant_state_payloads(tasks=None):
    tasks = tasks if tasks is not None else get_tasks()
    summary = summarize(tasks)
    ingress_base_path = get_ingress_base_path()
    overdue = [task for task in tasks if task["status"] == "overdue"]
    due_today = [task for task in tasks if task["status"] == "due_today"]
    upcoming_window = [
        task
        for task in tasks
        if task["status"] == "upcoming" and task["days_until"] <= UPCOMING_WINDOW_DAYS
    ]
    dashboard_tasks = [
        task
        for task in tasks
        if task["status"] in {"overdue", "due_today"} or task["days_until"] <= HOMEASSISTANT_DASHBOARD_WINDOW_DAYS
    ]
    ready = overdue + due_today
    all_items = [homeassistant_task_row(task, ingress_base_path) for task in tasks]
    dashboard_items = [homeassistant_task_row(task, ingress_base_path) for task in dashboard_tasks]
    updated_at = utc_now_iso()

    def count_payload(name, state, icon, items):
        return {
            "state": str(state),
            "attributes": {
                "friendly_name": name,
                "icon": icon,
                "unit_of_measurement": "items",
                "items": [homeassistant_task_row(task, ingress_base_path) for task in items],
                "item_count": len(items),
                "dashboard_url": homeassistant_link("/focus", ingress_base_path),
                "ingress_base_path": ingress_base_path,
                "updated_at": updated_at,
            },
        }

    return {
        f"sensor.{HOMEASSISTANT_ENTITY_PREFIX}_overdue": count_payload(
            "MxTracker Overdue", summary["overdue"], "mdi:alert-circle-outline", overdue
        ),
        f"sensor.{HOMEASSISTANT_ENTITY_PREFIX}_due_today": count_payload(
            "MxTracker Due Today", summary["due_today"], "mdi:calendar-today", due_today
        ),
        f"sensor.{HOMEASSISTANT_ENTITY_PREFIX}_upcoming_30_days": count_payload(
            f"MxTracker Upcoming {UPCOMING_WINDOW_DAYS} Days",
            summary["upcoming_window"],
            "mdi:calendar-clock",
            upcoming_window,
        ),
        f"sensor.{HOMEASSISTANT_ENTITY_PREFIX}_ready": count_payload(
            "MxTracker Ready", summary["ready_count"], "mdi:format-list-checks", ready
        ),
        f"sensor.{HOMEASSISTANT_ENTITY_PREFIX}_due_14_days": {
            "state": str(len(dashboard_items)),
            "attributes": {
                "friendly_name": "MxTracker Due Next 14 Days",
                "icon": "mdi:calendar-alert",
                "unit_of_measurement": "items",
                "items": dashboard_items,
                "item_count": len(dashboard_items),
                "window_days": HOMEASSISTANT_DASHBOARD_WINDOW_DAYS,
                "dashboard_url": homeassistant_link("/focus", ingress_base_path),
                "ingress_base_path": ingress_base_path,
                "updated_at": updated_at,
            },
        },
        f"sensor.{HOMEASSISTANT_ENTITY_PREFIX}_all_items": {
            "state": str(summary["total"]),
            "attributes": {
                "friendly_name": "MxTracker All Items",
                "icon": "mdi:home-clock",
                "unit_of_measurement": "items",
                "items": all_items,
                "item_count": len(all_items),
                "dashboard_url": homeassistant_link("/items", ingress_base_path),
                "ingress_base_path": ingress_base_path,
                "updated_at": updated_at,
            },
        },
        f"sensor.{HOMEASSISTANT_ENTITY_PREFIX}_on_track_percent": {
            "state": str(summary["on_track_percent"]),
            "attributes": {
                "friendly_name": "MxTracker On Track",
                "icon": "mdi:percent-circle-outline",
                "unit_of_measurement": "%",
                "updated_at": updated_at,
            },
        },
        f"sensor.{HOMEASSISTANT_ENTITY_PREFIX}_completed_30_days": {
            "state": str(summary["completed_30_days"]),
            "attributes": {
                "friendly_name": "MxTracker Completed 30 Days",
                "icon": "mdi:check-circle-outline",
                "unit_of_measurement": "items",
                "updated_at": updated_at,
            },
        },
    }


def public_task(task):
    if not task:
        return None
    return {
        "id": task["id"],
        "name": task["name"],
        "category": task["category"],
        "notes": task["notes"],
        "interval_count": task["interval_count"],
        "interval_unit": task["interval_unit"],
        "next_due_on": task["next_due_on"],
        "last_completed_on": task["last_completed_on"],
        "status": task["status"],
        "status_label": task["status_label"],
        "days_until": task["days_until"],
        "due_phrase": task["due_phrase"],
        "recurrence_phrase": task["recurrence_phrase"],
    }


def csv_cell(value):
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def safe_return_path(value):
    if value in {"/", "/items", "/focus"}:
        return value
    if value.startswith("/item/") and value.removeprefix("/item/").isdigit():
        return value
    return "/"


def safe_theme(value):
    return value if value in THEMES else "system"


def normalize_base_path(value):
    if not value or not value.startswith("/") or "://" in value:
        return ""
    return value.rstrip("/")


def app_url(path, base_path=""):
    path = path if path.startswith("/") else f"/{path}"
    return f"{normalize_base_path(base_path)}{path}"


def homeassistant_link(path, base_path=""):
    base_path = normalize_base_path(base_path) or get_ingress_base_path()
    return app_url(path, base_path) if base_path else app_url(path)


def safe_referer_path(value, base_path=""):
    if not value:
        return "/"
    parsed = urlparse(value)
    path = parsed.path or "/"
    base_path = normalize_base_path(base_path)
    if base_path and path.startswith(base_path):
        path = path.removeprefix(base_path) or "/"
    if path not in {"/", "/items", "/new", "/focus"} and not path.startswith(("/edit/", "/item/")):
        return "/"
    return path


def tasks_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Name",
            "Category",
            "Status",
            "Due phrase",
            "Next due",
            "Last completed",
            "Recurrence",
            "Notes",
        ]
    )
    for task in get_tasks():
        writer.writerow(
            [
                csv_cell(task["name"]),
                csv_cell(task["category"]),
                csv_cell(task["status_label"]),
                csv_cell(task["due_phrase"]),
                csv_cell(task["next_due_on"]),
                csv_cell(task["last_completed_on"] or "Never"),
                csv_cell(task["recurrence_phrase"]),
                csv_cell(task["notes"]),
            ]
        )
    return output.getvalue()


def history_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Task", "Completed on", "Next due after completion", "Recorded at"])
    for item in get_all_history():
        writer.writerow(
            [
                csv_cell(item["task_name"]),
                csv_cell(item["completed_on"]),
                csv_cell(item["next_due_on"]),
                csv_cell(item["created_at"]),
            ]
        )
    return output.getvalue()


def post_homeassistant_state(entity_id, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{HOMEASSISTANT_API_BASE}/states/{entity_id}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(request, timeout=HOMEASSISTANT_REQUEST_TIMEOUT) as response:
        response.read()


class HomeAssistantPublisher:
    def __init__(self):
        self.event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="ha-state-publisher", daemon=True)
        self.last_error = None
        self.started = False

    def start(self):
        if not PUBLISH_HOMEASSISTANT:
            print("Home Assistant sensor publishing is disabled.")
            return
        if not SUPERVISOR_TOKEN:
            print("Home Assistant sensor publishing skipped: SUPERVISOR_TOKEN is unavailable.")
            return
        self.started = True
        self.thread.start()
        self.request_sync()

    def request_sync(self):
        if self.started:
            self.event.set()

    def run(self):
        next_publish_at = 0
        while True:
            timeout = max(0, next_publish_at - time.monotonic()) if next_publish_at else 0
            self.event.wait(timeout)
            self.event.clear()
            self.publish()
            next_publish_at = time.monotonic() + HOMEASSISTANT_SYNC_INTERVAL_SECONDS

    def publish(self):
        try:
            for entity_id, payload in homeassistant_state_payloads().items():
                post_homeassistant_state(entity_id, payload)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as error:
            self.report_error(str(error))
            return
        except Exception as error:
            self.report_error(f"{type(error).__name__}: {error}")
            return
        if self.last_error:
            print("Home Assistant sensor publishing recovered.")
            self.last_error = None

    def report_error(self, message):
        if message != self.last_error:
            print(f"Home Assistant sensor publishing failed: {message}")
            self.last_error = message


def request_homeassistant_sync():
    if HA_PUBLISHER is not None:
        HA_PUBLISHER.request_sync()


def validate_task_form(fields):
    errors = []
    name = fields.get("name", "").strip()
    category = fields.get("category", "General").strip()
    notes = fields.get("notes", "").strip()
    interval_unit = fields.get("interval_unit", "").strip()
    next_due_on = fields.get("next_due_on", "").strip()

    try:
        interval_count = int(fields.get("interval_count", ""))
    except ValueError:
        interval_count = 0

    if not name:
        errors.append("Name is required.")
    if len(name) > 120:
        errors.append("Name must be 120 characters or fewer.")
    if category not in CATEGORIES:
        errors.append("Choose a valid category.")
    if len(notes) > 1000:
        errors.append("Notes must be 1000 characters or fewer.")
    if interval_count < 1 or interval_count > 120:
        errors.append("Interval must be between 1 and 120.")
    if interval_unit not in ALLOWED_UNITS:
        errors.append("Choose a valid interval unit.")
    try:
        parse_date(next_due_on)
    except ValueError:
        errors.append("Next due must be a valid date.")

    return errors, {
        "name": name,
        "category": category,
        "notes": notes,
        "interval_count": interval_count,
        "interval_unit": interval_unit,
        "next_due_on": next_due_on,
    }


def save_task(fields, task_id=None):
    now = utc_now_iso()
    with connect_db() as conn:
        if task_id is None:
            conn.execute(
                """
                INSERT INTO tasks
                    (name, category, notes, interval_count, interval_unit, next_due_on, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fields["name"],
                    fields["category"],
                    fields["notes"],
                    fields["interval_count"],
                    fields["interval_unit"],
                    fields["next_due_on"],
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE tasks
                SET name = ?, category = ?, notes = ?, interval_count = ?, interval_unit = ?, next_due_on = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    fields["name"],
                    fields["category"],
                    fields["notes"],
                    fields["interval_count"],
                    fields["interval_unit"],
                    fields["next_due_on"],
                    now,
                    task_id,
                ),
            )
    request_homeassistant_sync()


def complete_task(task_id):
    task = get_task(task_id)
    if not task:
        return False
    completed_on = today_iso()
    next_due = calculate_next_due(
        parse_date(completed_on),
        task["interval_count"],
        task["interval_unit"],
    ).isoformat()
    now = utc_now_iso()
    with connect_db() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET last_completed_on = ?, next_due_on = ?, updated_at = ?
            WHERE id = ?
            """,
            (completed_on, next_due, now, task_id),
        )
        conn.execute(
            """
            INSERT INTO completion_history
                (task_id, task_name, completed_on, next_due_on, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, task["name"], completed_on, next_due, now),
        )
    request_homeassistant_sync()
    return True


def snooze_task(task_id, days=7):
    task = get_task(task_id)
    if not task:
        return False
    next_due = (date.today() + timedelta(days=days)).isoformat()
    with connect_db() as conn:
        conn.execute(
            "UPDATE tasks SET next_due_on = ?, updated_at = ? WHERE id = ?",
            (next_due, utc_now_iso(), task_id),
        )
    request_homeassistant_sync()
    return True


def delete_task(task_id):
    with connect_db() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    request_homeassistant_sync()


def render_layout(title, body, csrf_token, notice="", theme="system", base_path=""):
    theme = theme if theme in THEMES else "system"
    base_path = normalize_base_path(base_path)
    theme_links = []
    for mode in ["system", "light", "dark"]:
        active = " active" if theme == mode else ""
        theme_links.append(f'<a class="theme-link{active}" href="{app_url(f"/theme/{mode}", base_path)}">{mode.title()}</a>')
    return f"""<!doctype html>
<html lang="en" data-theme="{escape(theme)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f7f9;
      --surface: #ffffff;
      --surface-strong: #eef5f7;
      --text: #15232d;
      --muted: #657681;
      --line: #d8e2e7;
      --accent: #0891b2;
      --accent-strong: #0e7490;
      --danger: #c2414b;
      --danger-bg: #fff1f2;
      --warn: #b7791f;
      --ok: #15803d;
      --later-bg: #f1f5f9;
      --shadow: 0 14px 34px rgba(15, 35, 45, 0.08);
    }}
    html[data-theme="dark"] {{
      color-scheme: dark;
      --bg: #0d151c;
      --surface: #14232d;
      --surface-strong: #1b3340;
      --text: #eef6fb;
      --muted: #a9bac5;
      --line: #29414f;
      --accent: #67d4e4;
      --accent-strong: #9be8f1;
      --danger: #ff9aa2;
      --danger-bg: #2a1f23;
      --warn: #ffd166;
      --ok: #8fe0a5;
      --later-bg: #172631;
      --shadow: none;
    }}
    @media (prefers-color-scheme: dark) {{
      html[data-theme="system"] {{
        color-scheme: dark;
        --bg: #0d151c;
        --surface: #14232d;
        --surface-strong: #1b3340;
        --text: #eef6fb;
        --muted: #a9bac5;
        --line: #29414f;
        --accent: #67d4e4;
        --accent-strong: #9be8f1;
        --danger: #ff9aa2;
        --danger-bg: #2a1f23;
        --warn: #ffd166;
        --ok: #8fe0a5;
        --later-bg: #172631;
        --shadow: none;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    a {{ color: var(--accent-strong); }}
    .wrap {{ width: min(1120px, calc(100% - 32px)); margin: 0 auto; }}
    header {{
      border-bottom: 1px solid var(--line);
      background: var(--surface);
    }}
    .topbar {{
      min-height: 76px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }}
    h1 {{ margin: 0; font-size: clamp(1.5rem, 2vw, 2.15rem); letter-spacing: 0; }}
    h2 {{ margin: 0 0 14px; font-size: 1.1rem; letter-spacing: 0; }}
    main {{ padding: 24px 0 42px; }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    .form-actions {{ margin-top: 16px; }}
    .confirm-card {{ box-shadow: none; }}
    .theme-switch {{
      display: flex;
      gap: 4px;
      padding: 4px;
      border: 1px solid var(--line);
      background: var(--surface-strong);
      border-radius: 8px;
    }}
    .theme-link {{
      min-height: 34px;
      padding: 6px 10px;
      border-radius: 6px;
      color: var(--muted);
      text-decoration: none;
      font-weight: 800;
    }}
    .theme-link.active {{
      background: var(--surface);
      color: var(--accent-strong);
      box-shadow: var(--shadow);
    }}
    .button, button {{
      border: 1px solid var(--accent);
      background: var(--accent);
      color: #fff;
      min-height: 40px;
      padding: 8px 13px;
      border-radius: 8px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
    }}
    .button.secondary, button.secondary {{
      background: transparent;
      color: var(--accent-strong);
    }}
    .button.danger, button.danger {{
      border-color: var(--danger);
      background: var(--danger);
      color: #fff;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 20px;
    }}
    .stat, .panel, .task {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }}
    .stat {{ padding: 16px; }}
    .stat strong {{ display: block; font-size: 1.8rem; line-height: 1; }}
    .stat span {{ color: var(--muted); font-size: .92rem; }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      gap: 18px;
      align-items: start;
    }}
    .panel {{ padding: 18px; }}
    .task {{
      padding: 16px;
      margin-bottom: 12px;
    }}
    .task-head {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: start;
    }}
    .task h3 {{
      margin: 0 0 4px;
      font-size: 1.08rem;
      overflow-wrap: anywhere;
    }}
    .meta, .empty, .history {{ color: var(--muted); }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 3px 9px;
      border-radius: 999px;
      font-size: .84rem;
      font-weight: 800;
      white-space: nowrap;
      border: 1px solid var(--line);
    }}
    .badge.overdue {{ color: var(--danger); }}
    .badge.due_today {{ color: var(--warn); }}
    .badge.upcoming {{ color: var(--ok); }}
    .notes {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      margin: 10px 0 0;
    }}
    .task-actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 12px;
    }}
    form.inline {{ display: inline; }}
    label {{ display: block; font-weight: 800; margin: 13px 0 6px; }}
    input, select, textarea {{
      width: 100%;
      border: 1px solid var(--line);
      background: var(--surface);
      color: var(--text);
      border-radius: 7px;
      min-height: 42px;
      padding: 9px 10px;
      font: inherit;
    }}
    textarea {{ min-height: 120px; resize: vertical; }}
    .form-row {{
      display: grid;
      grid-template-columns: 120px minmax(0, 1fr);
      gap: 10px;
    }}
    .notice, .errors {{
      padding: 12px 14px;
      border-radius: 8px;
      margin-bottom: 16px;
      border: 1px solid var(--line);
      background: var(--surface);
    }}
    .errors {{ color: var(--danger); }}
    .history-item {{ padding: 10px 0; border-top: 1px solid var(--line); }}
    .history-item:first-child {{ border-top: 0; }}
    .focus-list {{
      display: grid;
      gap: 12px;
    }}
    .focus-item {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
      color: var(--text);
      text-decoration: none;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
    }}
    .focus-item.overdue {{
      border-color: var(--danger);
      background: var(--danger-bg);
    }}
    .focus-item.overdue strong,
    .detail-hero.overdue h2 {{
      color: var(--danger);
    }}
    .focus-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 6px;
    }}
    .detail-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: 16px 0;
    }}
    .detail-field {{
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-strong);
    }}
    .detail-field span {{
      display: block;
      color: var(--muted);
      font-size: .78rem;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: .04em;
      margin-bottom: 4px;
    }}
    .table-panel {{ margin-bottom: 18px; overflow: hidden; }}
    .table-panel.overdue-section {{
      border-color: var(--danger);
      background: var(--danger-bg);
    }}
    .table-panel.overdue-section h2,
    .table-panel.overdue-section .task-table strong {{
      color: var(--danger);
    }}
    .table-panel.later-section {{
      border-color: var(--line);
      background: var(--later-bg);
    }}
    .table-panel.later-section h2,
    .table-panel.later-section .task-table strong {{
      color: var(--muted);
    }}
    .table-panel.overdue-section .badge.overdue {{
      color: var(--danger);
      border-color: var(--danger);
      background: var(--surface);
    }}
    .audit-actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin: 0 0 18px;
    }}
    .table-head {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      padding: 16px 18px 6px;
    }}
    .task-table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }}
    .task-table th, .task-table td {{
      padding: 10px 12px;
      border-top: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
    }}
    .task-table th {{
      color: var(--muted);
      font-size: .82rem;
      font-weight: 800;
    }}
    .task-table .col-name {{ width: 31%; }}
    .task-table .col-category {{ width: 14%; }}
    .task-table .col-due {{ width: 18%; }}
    .task-table .col-repeat {{ width: 15%; }}
    .task-table .col-actions {{ width: 22%; }}
    .quick-actions {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; }}
    .quick-actions form {{ min-width: 0; }}
    .quick-actions .button, .quick-actions button {{ min-height: 34px; padding: 6px 9px; font-size: .9rem; }}
    .quick-actions .button, .quick-actions button {{ width: 100%; }}
    footer {{ color: var(--muted); font-size: .88rem; margin-top: 22px; }}
    @media (max-width: 820px) {{
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .layout {{ grid-template-columns: 1fr; }}
      .topbar {{ align-items: flex-start; flex-direction: column; padding: 16px 0; }}
      .wrap {{ width: min(100% - 22px, 1120px); }}
      .form-row {{ grid-template-columns: 1fr; }}
      .task-head {{ flex-direction: column; }}
      nav .button {{ width: auto; }}
      .theme-switch {{ width: 100%; }}
      .theme-link {{ flex: 1; text-align: center; }}
      .table-head {{ flex-direction: column; }}
      .detail-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .task-table, .task-table tbody, .task-table tr, .task-table td {{ display: block; width: 100%; max-width: 100%; }}
      .task-table thead {{ display: none; }}
      .task-table .col-name,
      .task-table .col-category,
      .task-table .col-due,
      .task-table .col-repeat,
      .task-table .col-actions {{ width: 100%; }}
      .task-table tr {{
        border-top: 1px solid var(--line);
        padding: 14px 18px 16px;
      }}
      .task-table td {{
        border-top: 0;
        padding: 0;
        margin-top: 12px;
      }}
      .task-table td::before {{
        content: attr(data-label);
        display: block;
        color: var(--muted);
        font-size: .76rem;
        font-weight: 900;
        text-transform: uppercase;
        letter-spacing: .04em;
        margin-bottom: 4px;
      }}
      .task-table td.col-name::before {{ content: ""; display: none; }}
      .task-table td.col-name {{
        margin-top: 0;
      }}
      .task-table td.col-name strong {{
        display: block;
        font-size: 1.05rem;
        line-height: 1.25;
      }}
      .task-table td.col-category {{
        display: inline-flex;
        width: auto;
        max-width: 100%;
        padding: 4px 10px;
        border: 1px solid var(--line);
        border-radius: 999px;
        background: var(--surface-strong);
        font-weight: 800;
      }}
      .task-table td.col-category::before {{ content: ""; display: none; }}
      .task-table td.col-due .badge {{
        width: fit-content;
        max-width: 100%;
      }}
      .quick-actions {{ grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top: 4px; }}
    }}
    @media (max-width: 520px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .actions {{ width: 100%; }}
      nav .button {{ flex: 1; }}
      .audit-actions .button {{ width: 100%; }}
      .quick-actions {{ grid-template-columns: 1fr; }}
      .focus-item {{ grid-template-columns: 1fr; }}
      .detail-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <div>
        <h1>Home Maintenance</h1>
        <div class="meta">Private recurring task tracker</div>
      </div>
      <nav class="actions">
        <a class="button secondary" href="{app_url("/", base_path)}">Dashboard</a>
        <a class="button secondary" href="{app_url("/items", base_path)}">All items</a>
        <a class="button" href="{app_url("/new", base_path)}">Add item</a>
        <span class="theme-switch" aria-label="Theme mode">
          {''.join(theme_links)}
        </span>
      </nav>
    </div>
  </header>
  <main class="wrap">
    {f'<div class="notice">{escape(notice)}</div>' if notice else ''}
    {body}
    <footer>Data is stored locally in this add-on's Home Assistant storage.</footer>
  </main>
</body>
</html>"""


def render_dashboard(csrf_token, notice="", theme="system", base_path=""):
    tasks = get_tasks()
    summary = summarize(tasks)
    history = get_history()
    overdue_tasks = [task for task in tasks if task["status"] == "overdue"]
    current_tasks = [task for task in tasks if task["status"] == "due_today"]
    upcoming_tasks = [
        task
        for task in tasks
        if task["status"] == "upcoming" and task["days_until"] <= UPCOMING_WINDOW_DAYS
    ]
    future_tasks = [
        task
        for task in tasks
        if task["status"] == "upcoming" and task["days_until"] > UPCOMING_WINDOW_DAYS
    ]

    def render_task_rows(section_tasks, allow_snooze=True, return_to="/"):
        if not section_tasks:
            return '<tr><td colspan="5" class="empty">Nothing here right now.</td></tr>'
        rows = []
        for task in section_tasks:
            task_id = task["id"]
            last_done = escape(task["last_completed_on"] or "Never")
            notes = f'<div class="meta">{escape(task["notes"])}</div>' if task["notes"] else ""
            complete_url = app_url(f"/complete/{task_id}", base_path)
            snooze_url = app_url(f"/snooze/{task_id}", base_path)
            edit_url = app_url(f"/edit/{task_id}", base_path)
            delete_url = f'{app_url(f"/delete/{task_id}", base_path)}?return_to={quote(return_to, safe="")}'
            snooze = ""
            if allow_snooze:
                snooze = f"""
                  <form class="inline" action="{snooze_url}" method="post">
                    <input type="hidden" name="csrf_token" value="{csrf_token}">
                    <input type="hidden" name="return_to" value="{escape(return_to)}">
                    <button class="secondary" type="submit">Snooze 7d</button>
                  </form>
                """
            rows.append(
                f"""
                <tr>
                  <td data-label="Task" class="col-name">
                    <strong><a href="{app_url(f"/item/{task_id}", base_path)}">{escape(task["name"])}</a></strong>
                    {notes}
                    <div class="meta">Last done {last_done}</div>
                  </td>
                  <td data-label="Category" class="col-category">{escape(task["category"])}</td>
                  <td data-label="Due" class="col-due">
                    <span class="badge {task["status"]}">{escape(task["due_phrase"])}</span>
                    <div class="meta">{escape(task["next_due_on"])}</div>
                  </td>
                  <td data-label="Repeat" class="col-repeat">{escape(task["recurrence_phrase"])}</td>
                  <td data-label="Actions" class="col-actions">
                    <div class="quick-actions">
                      <form class="inline" action="{complete_url}" method="post">
                        <input type="hidden" name="csrf_token" value="{csrf_token}">
                        <input type="hidden" name="return_to" value="{escape(return_to)}">
                        <button type="submit">Done</button>
                      </form>
                      {snooze}
                      <a class="button secondary" href="{edit_url}">Edit</a>
                      <a class="button danger" href="{delete_url}">Delete</a>
                    </div>
                  </td>
                </tr>
                """
            )
        return "".join(rows)

    def render_task_table(title, subtitle, section_tasks, section_class="", allow_snooze=True, return_to="/"):
        return f"""
          <section class="panel table-panel {escape(section_class)}">
            <div class="table-head">
              <h2>{escape(title)}</h2>
              <div class="meta">{escape(subtitle)}</div>
            </div>
            <table class="task-table">
              <thead>
                <tr>
                  <th class="col-name">Task</th>
                  <th class="col-category">Category</th>
                  <th class="col-due">Due</th>
                  <th class="col-repeat">Repeat</th>
                  <th class="col-actions">Actions</th>
                </tr>
              </thead>
              <tbody>{render_task_rows(section_tasks, allow_snooze, return_to)}</tbody>
            </table>
          </section>
        """

    history_items = []
    for item in history:
        history_items.append(
            f"""
            <div class="history-item">
              <strong>{escape(item["task_name"])}</strong>
              <div>Completed {escape(item["completed_on"])} · Next due {escape(item["next_due_on"])}</div>
            </div>
            """
        )
    if not history_items:
        history_items.append('<div class="empty">No completed tasks yet.</div>')

    body = f"""
      <section class="grid" aria-label="Maintenance summary">
        <div class="stat"><strong>{summary["overdue"]}</strong><span>Overdue</span></div>
        <div class="stat"><strong>{summary["due_today"]}</strong><span>Due today</span></div>
        <div class="stat"><strong>{summary["upcoming_window"]}</strong><span>Upcoming 30 days</span></div>
        <div class="stat"><strong>{summary["total"]}</strong><span>Total items</span></div>
        <div class="stat"><strong>{summary["on_track_percent"]}%</strong><span>On track</span></div>
        <div class="stat"><strong>{summary["completed_30_days"]}</strong><span>Completed 30 days</span></div>
      </section>
      <section class="layout">
        <div>
          {render_task_table("Overdue", "Needs attention first", overdue_tasks, "overdue-section")}
          {render_task_table("Current", "Due today", current_tasks)}
          {render_task_table("Upcoming", f"Next {UPCOMING_WINDOW_DAYS} days", upcoming_tasks)}
          {render_task_table("Later", "Planned beyond 30 days", future_tasks, "later-section", allow_snooze=False)}
        </div>
        <aside class="panel">
          <h2>Recent History</h2>
          <div class="history">{''.join(history_items)}</div>
        </aside>
      </section>
    """
    return render_layout("Home Maintenance", body, csrf_token, notice, theme, base_path)


def render_focus_view(csrf_token, notice="", theme="system", base_path=""):
    tasks = [
        task
        for task in get_tasks()
        if task["status"] in {"overdue", "due_today"} or task["days_until"] <= HOMEASSISTANT_DASHBOARD_WINDOW_DAYS
    ]
    if tasks:
        items = []
        for task in tasks:
            status_class = " overdue" if task["status"] == "overdue" else ""
            items.append(
                f"""
                <a class="focus-item{status_class}" href="{app_url(f"/item/{task["id"]}", base_path)}">
                  <div>
                    <strong>{escape(task["name"])}</strong>
                    <div class="focus-meta">
                      <span class="badge {task["status"]}">{escape(task["due_phrase"])}</span>
                      <span class="meta">{escape(task["category"])}</span>
                      <span class="meta">Last done {escape(task["last_completed_on"] or "Never")}</span>
                    </div>
                  </div>
                  <div class="meta">{escape(task["next_due_on"])}</div>
                </a>
                """
            )
        content = "".join(items)
    else:
        content = '<div class="empty">Nothing is due in the next 14 days.</div>'

    body = f"""
      <section class="panel">
        <div class="table-head">
          <div>
            <h2>Due Next 14 Days</h2>
            <div class="meta">Overdue items stay visible until completed or snoozed.</div>
          </div>
          <a class="button secondary" href="{app_url("/", base_path)}">Open dashboard</a>
        </div>
        <div class="focus-list">{content}</div>
      </section>
    """
    return render_layout("Maintenance Due Soon", body, csrf_token, notice, theme, base_path)


def render_items_audit(csrf_token, notice="", theme="system", base_path=""):
    tasks = get_tasks()
    if tasks:
        rows = []
        for task in tasks:
            task_id = task["id"]
            notes = f'<div class="meta">{escape(task["notes"])}</div>' if task["notes"] else ""
            complete_url = app_url(f"/complete/{task_id}", base_path)
            edit_url = app_url(f"/edit/{task_id}", base_path)
            rows.append(
                f"""
                <tr>
                  <td data-label="Task" class="col-name">
                    <strong><a href="{app_url(f"/item/{task_id}", base_path)}">{escape(task["name"])}</a></strong>
                    {notes}
                  </td>
                  <td data-label="Category" class="col-category">{escape(task["category"])}</td>
                  <td data-label="Status" class="col-due">
                    <span class="badge {task["status"]}">{escape(task["due_phrase"])}</span>
                    <div class="meta">{escape(task["status_label"])}</div>
                  </td>
                  <td data-label="Last done">{escape(task["last_completed_on"] or "Never")}</td>
                  <td data-label="Next due">{escape(task["next_due_on"])}</td>
                  <td data-label="Repeat">{escape(task["recurrence_phrase"])}</td>
                  <td data-label="Actions" class="col-actions">
                    <div class="quick-actions">
                      <form class="inline" action="{complete_url}" method="post">
                        <input type="hidden" name="csrf_token" value="{csrf_token}">
                        <input type="hidden" name="return_to" value="/items">
                        <button type="submit">Done</button>
                      </form>
                      <a class="button secondary" href="{edit_url}">Edit</a>
                    </div>
                  </td>
                </tr>
                """
            )
        table_body = "".join(rows)
    else:
        table_body = '<tr><td colspan="7" class="empty">No maintenance items yet.</td></tr>'

    body = f"""
      <div class="audit-actions">
        <a class="button" href="{app_url("/export/tasks.csv", base_path)}">Export items CSV</a>
        <a class="button secondary" href="{app_url("/export/history.csv", base_path)}">Export history CSV</a>
      </div>
      <section class="panel table-panel">
        <div class="table-head">
          <h2>All Items</h2>
          <div class="meta">Audit every maintenance item at a glance</div>
        </div>
        <table class="task-table">
          <thead>
            <tr>
              <th class="col-name">Task</th>
              <th class="col-category">Category</th>
              <th class="col-due">Status</th>
              <th>Last done</th>
              <th>Next due</th>
              <th>Repeat</th>
              <th class="col-actions">Actions</th>
            </tr>
          </thead>
          <tbody>{table_body}</tbody>
        </table>
      </section>
    """
    return render_layout("All Maintenance Items", body, csrf_token, notice, theme, base_path)


def render_item_detail(task, csrf_token, notice="", theme="system", base_path=""):
    history = get_task_history(task["id"])
    status_class = " overdue" if task["status"] == "overdue" else ""
    complete_url = app_url(f"/complete/{task['id']}", base_path)
    snooze_url = app_url(f"/snooze/{task['id']}", base_path)
    edit_url = app_url(f"/edit/{task['id']}", base_path)
    delete_url = f'{app_url(f"/delete/{task["id"]}", base_path)}?return_to={quote(f"/item/{task["id"]}", safe="")}'
    return_to = f"/item/{task['id']}"

    if history:
        history_rows = []
        for item in history:
            history_rows.append(
                f"""
                <tr>
                  <td data-label="Completed">{escape(item["completed_on"])}</td>
                  <td data-label="Next due">{escape(item["next_due_on"])}</td>
                  <td data-label="Recorded">{escape(item["created_at"])}</td>
                </tr>
                """
            )
        history_body = "".join(history_rows)
    else:
        history_body = '<tr><td colspan="3" class="empty">No completions recorded yet.</td></tr>'

    notes = f'<p class="notes">{escape(task["notes"])}</p>' if task["notes"] else '<p class="empty">No notes saved for this item.</p>'
    body = f"""
      <section class="panel detail-hero{status_class}">
        <div class="table-head">
          <div>
            <h2>{escape(task["name"])}</h2>
            <div class="meta">{escape(task["category"])}</div>
          </div>
          <span class="badge {task["status"]}">{escape(task["due_phrase"])}</span>
        </div>
        <div class="detail-grid">
          <div class="detail-field"><span>Next due</span>{escape(task["next_due_on"])}</div>
          <div class="detail-field"><span>Last done</span>{escape(task["last_completed_on"] or "Never")}</div>
          <div class="detail-field"><span>Repeat</span>{escape(task["recurrence_phrase"])}</div>
          <div class="detail-field"><span>Status</span>{escape(task["status_label"])}</div>
        </div>
        <h2>Details</h2>
        {notes}
        <div class="actions form-actions">
          <form class="inline" action="{complete_url}" method="post">
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <input type="hidden" name="return_to" value="{escape(return_to)}">
            <button type="submit">Mark done</button>
          </form>
          <form class="inline" action="{snooze_url}" method="post">
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <input type="hidden" name="return_to" value="{escape(return_to)}">
            <button class="secondary" type="submit">Snooze 7d</button>
          </form>
          <a class="button secondary" href="{edit_url}">Edit</a>
          <a class="button danger" href="{delete_url}">Delete</a>
        </div>
      </section>
      <section class="panel table-panel">
        <div class="table-head">
          <h2>Completion History</h2>
          <div class="meta">Most recent first</div>
        </div>
        <table class="task-table">
          <thead>
            <tr>
              <th>Completed</th>
              <th>Next due after completion</th>
              <th>Recorded</th>
            </tr>
          </thead>
          <tbody>{history_body}</tbody>
        </table>
      </section>
    """
    return render_layout(task["name"], body, csrf_token, notice, theme, base_path)


def render_task_form(csrf_token, task=None, errors=None, theme="system", base_path=""):
    errors = errors or []
    is_edit = task is not None
    values = task or {
        "name": "",
        "category": "General",
        "notes": "",
        "interval_count": 1,
        "interval_unit": "months",
        "next_due_on": today_iso(),
    }
    action = app_url(f'/edit/{task["id"]}', base_path) if is_edit else app_url("/new", base_path)
    title = "Edit Item" if is_edit else "Add Item"
    unit_options = []
    for unit in ["days", "weeks", "months", "years"]:
        selected = " selected" if values["interval_unit"] == unit else ""
        unit_options.append(f'<option value="{unit}"{selected}>{unit.title()}</option>')
    category_options = []
    for category in CATEGORIES:
        selected = " selected" if values["category"] == category else ""
        category_options.append(f'<option value="{escape(category)}"{selected}>{escape(category)}</option>')
    error_html = ""
    if errors:
        error_html = '<div class="errors"><strong>Check these fields:</strong><ul>'
        error_html += "".join(f"<li>{escape(error)}</li>" for error in errors)
        error_html += "</ul></div>"
    body = f"""
      <section class="panel">
        <h2>{title}</h2>
        {error_html}
        <form action="{action}" method="post">
          <input type="hidden" name="csrf_token" value="{csrf_token}">
          <label for="name">Name</label>
          <input id="name" name="name" maxlength="120" required value="{escape(values["name"])}">
          <label for="category">Category</label>
          <select id="category" name="category">{''.join(category_options)}</select>
          <label for="notes">Notes</label>
          <textarea id="notes" name="notes" maxlength="1000">{escape(values["notes"])}</textarea>
          <div class="form-row">
            <div>
              <label for="interval_count">Every</label>
              <input id="interval_count" name="interval_count" type="number" min="1" max="120" required value="{escape(values["interval_count"])}">
            </div>
            <div>
              <label for="interval_unit">Unit</label>
              <select id="interval_unit" name="interval_unit">{''.join(unit_options)}</select>
            </div>
          </div>
          <label for="next_due_on">Next due</label>
          <input id="next_due_on" name="next_due_on" type="date" required value="{escape(values["next_due_on"])}">
          <div class="actions form-actions">
            <button type="submit">Save item</button>
            <a class="button secondary" href="{app_url("/", base_path)}">Cancel</a>
          </div>
        </form>
      </section>
    """
    return render_layout(title, body, csrf_token, theme=theme, base_path=base_path)


def render_delete_confirm(task, csrf_token, return_to="/", theme="system", base_path=""):
    delete_url = app_url(f"/delete/{task['id']}", base_path)
    recurrence = recurrence_phrase(task["interval_count"], task["interval_unit"])
    category = task.get("category") or "General"
    body = f"""
      <section class="panel">
        <h2>Delete Item</h2>
        <p class="meta">This removes the maintenance item and its future schedule.</p>
        <div class="task confirm-card">
          <h3>{escape(task["name"])}</h3>
          <div class="meta">{escape(category)} · {escape(recurrence)}</div>
        </div>
        <div class="actions form-actions">
          <form class="inline" action="{delete_url}" method="post">
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <input type="hidden" name="return_to" value="{escape(return_to)}">
            <button class="danger" type="submit">Delete item</button>
          </form>
          <a class="button secondary" href="{app_url(return_to, base_path)}">Cancel</a>
        </div>
      </section>
    """
    return render_layout("Delete Maintenance Item", body, csrf_token, theme=theme, base_path=base_path)


class MaintenanceHandler(BaseHTTPRequestHandler):
    server_version = "HomeMaintenance/0.1"
    sys_version = ""

    def log_message(self, fmt, *args):
        if LOG_REQUESTS:
            print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def do_GET(self):
        if not self.client_allowed():
            self.respond_text("Forbidden.", HTTPStatus.FORBIDDEN)
            return
        parsed = urlparse(self.path)
        csrf = self.get_or_create_csrf_token()
        theme = self.current_theme()
        base_path = self.base_path()
        remember_ingress_base_path(base_path)
        if parsed.path.startswith("/theme/"):
            requested = parsed.path.removeprefix("/theme/").strip("/")
            return_to = safe_referer_path(self.headers.get("Referer", "/"), base_path)
            self.redirect_with_theme(return_to, safe_theme(requested))
            return
        if parsed.path == "/":
            notice = parse_qs(parsed.query).get("notice", [""])[0]
            self.respond_html(render_dashboard(csrf, notice=notice, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path == "/items":
            notice = parse_qs(parsed.query).get("notice", [""])[0]
            self.respond_html(render_items_audit(csrf, notice=notice, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path == "/focus":
            notice = parse_qs(parsed.query).get("notice", [""])[0]
            self.respond_html(render_focus_view(csrf, notice=notice, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path.startswith("/item/"):
            task_id = self.extract_id(parsed.path, "/item/")
            task = get_enriched_task(task_id) if task_id else None
            if not task:
                self.redirect("/", "Item not found.")
                return
            notice = parse_qs(parsed.query).get("notice", [""])[0]
            self.respond_html(render_item_detail(task, csrf, notice=notice, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path == "/new":
            self.respond_html(render_task_form(csrf, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path.startswith("/edit/"):
            task_id = self.extract_id(parsed.path, "/edit/")
            task = get_task(task_id) if task_id else None
            if not task:
                self.redirect("/", "Item not found.")
                return
            self.respond_html(render_task_form(csrf, task=task, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path.startswith("/delete/"):
            task_id = self.extract_id(parsed.path, "/delete/")
            task = get_task(task_id) if task_id else None
            if not task:
                self.redirect("/", "Item not found.")
                return
            return_to = safe_return_path(parse_qs(parsed.query).get("return_to", ["/"])[0])
            self.respond_html(render_delete_confirm(task, csrf, return_to, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path == "/api/summary":
            self.respond_json(summarize(get_tasks()))
            return
        if parsed.path == "/api/tasks":
            self.respond_json({"tasks": [public_task(task) for task in get_tasks()]})
            return
        if parsed.path == "/export/tasks.csv":
            self.respond_csv(tasks_csv(), "mxtracker-maintenance-items.csv")
            return
        if parsed.path == "/export/history.csv":
            self.respond_csv(history_csv(), "mxtracker-maintenance-history.csv")
            return
        if parsed.path == "/health":
            self.respond_json({"status": "ok"})
            return
        self.respond_not_found()

    def do_POST(self):
        if not self.client_allowed():
            self.respond_text("Forbidden.", HTTPStatus.FORBIDDEN)
            return
        parsed = urlparse(self.path)
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            self.respond_text("Unsupported media type.", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            fields = self.read_form()
        except ValueError as error:
            self.respond_text(str(error), HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        if not self.valid_csrf(fields.get("csrf_token", "")):
            self.respond_text("Invalid request token.", HTTPStatus.FORBIDDEN)
            return
        if parsed.path == "/new":
            errors, cleaned = validate_task_form(fields)
            if errors:
                csrf = self.get_or_create_csrf_token()
                self.respond_html(render_task_form(csrf, task=cleaned, errors=errors, theme=self.current_theme(), base_path=self.base_path()), csrf, HTTPStatus.BAD_REQUEST)
                return
            save_task(cleaned)
            self.redirect("/", "Maintenance item added.")
            return
        if parsed.path.startswith("/edit/"):
            task_id = self.extract_id(parsed.path, "/edit/")
            if not task_id or not get_task(task_id):
                self.redirect("/", "Item not found.")
                return
            errors, cleaned = validate_task_form(fields)
            cleaned["id"] = task_id
            if errors:
                csrf = self.get_or_create_csrf_token()
                self.respond_html(render_task_form(csrf, task=cleaned, errors=errors, theme=self.current_theme(), base_path=self.base_path()), csrf, HTTPStatus.BAD_REQUEST)
                return
            save_task(cleaned, task_id)
            self.redirect("/", "Maintenance item updated.")
            return
        if parsed.path.startswith("/complete/"):
            task_id = self.extract_id(parsed.path, "/complete/")
            return_to = safe_return_path(fields.get("return_to", "/"))
            if task_id and complete_task(task_id):
                self.redirect(return_to, "Maintenance item completed.")
            else:
                self.redirect(return_to, "Item not found.")
            return
        if parsed.path.startswith("/snooze/"):
            task_id = self.extract_id(parsed.path, "/snooze/")
            return_to = safe_return_path(fields.get("return_to", "/"))
            if task_id and snooze_task(task_id):
                self.redirect(return_to, "Maintenance item snoozed for 7 days.")
            else:
                self.redirect(return_to, "Item not found.")
            return
        if parsed.path.startswith("/delete/"):
            task_id = self.extract_id(parsed.path, "/delete/")
            return_to = safe_return_path(fields.get("return_to", "/"))
            if task_id:
                delete_task(task_id)
            self.redirect(return_to, "Maintenance item deleted.")
            return
        self.respond_not_found()

    def read_form(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid content length.") from error
        if length > MAX_FORM_BYTES:
            raise ValueError("Form payload is too large.")
        raw = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def extract_id(self, path, prefix):
        try:
            value = path.removeprefix(prefix).strip("/")
            return int(value)
        except ValueError:
            return None

    def client_allowed(self):
        return self.client_address[0] in ALLOWED_CLIENTS

    def base_path(self):
        return normalize_base_path(self.headers.get("X-Ingress-Path", ""))

    def get_or_create_csrf_token(self):
        cookie = SimpleCookie(self.headers.get("Cookie"))
        morsel = cookie.get(CSRF_COOKIE)
        if morsel and len(morsel.value) >= 32:
            return morsel.value
        return secrets.token_urlsafe(32)

    def valid_csrf(self, submitted):
        cookie = SimpleCookie(self.headers.get("Cookie"))
        morsel = cookie.get(CSRF_COOKIE)
        return bool(morsel and submitted and secrets.compare_digest(morsel.value, submitted))

    def current_theme(self):
        cookie = SimpleCookie(self.headers.get("Cookie"))
        morsel = cookie.get(THEME_COOKIE)
        return safe_theme(morsel.value if morsel else "system")

    def set_security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'self'")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")

    def set_csrf_cookie(self, csrf_token):
        self.send_header("Set-Cookie", f"{CSRF_COOKIE}={csrf_token}; HttpOnly; SameSite=Lax; Path={self.base_path() or '/'}")

    def set_theme_cookie(self, theme):
        self.send_header("Set-Cookie", f"{THEME_COOKIE}={safe_theme(theme)}; HttpOnly; SameSite=Lax; Path={self.base_path() or '/'}")

    def respond_html(self, content, csrf_token, status=HTTPStatus.OK):
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.set_security_headers()
        self.set_csrf_cookie(csrf_token)
        self.end_headers()
        self.wfile.write(body)

    def respond_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.set_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def respond_csv(self, content, filename):
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.set_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def respond_text(self, text, status=HTTPStatus.OK):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.set_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, path, notice=""):
        location = app_url(path, self.base_path())
        if notice:
            separator = "&" if "?" in location else "?"
            location = f"{location}{separator}notice={quote(notice)}"
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.set_security_headers()
        self.end_headers()

    def redirect_with_theme(self, path, theme):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", app_url(path, self.base_path()))
        self.set_security_headers()
        self.set_theme_cookie(theme)
        self.end_headers()

    def respond_not_found(self):
        self.respond_text("Not found.", HTTPStatus.NOT_FOUND)


def main():
    global HA_PUBLISHER
    init_db()
    HA_PUBLISHER = HomeAssistantPublisher()
    HA_PUBLISHER.start()
    server = HTTPServer((HOST, PORT), MaintenanceHandler)
    print(f"Home Maintenance Tracker listening on {HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
