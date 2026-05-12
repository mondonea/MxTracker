#!/usr/bin/env python3
import calendar
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
from contextlib import contextmanager
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
TODO_CATEGORIES = ["General", "Plumbing", "Electrical", "Appliances", "Exterior", "Interior", "Safety", "Water", "Comfort", "Other"]
PRIORITIES = {"low", "normal", "high", "critical"}
PRIORITY_LABELS = {"low": "Low", "normal": "Normal", "high": "High", "critical": "Critical"}
SEASONS = {"", "spring", "summer", "fall", "winter", "year_round"}
SEASON_LABELS = {
    "": "Any season",
    "spring": "Spring",
    "summer": "Summer",
    "fall": "Fall",
    "winter": "Winter",
    "year_round": "Year round",
}
TODO_STATUSES = {"backlog", "planning", "ready", "in_work", "blocked", "done"}
TODO_STATUS_LABELS = {
    "backlog": "Backlog",
    "planning": "Planning",
    "ready": "Ready",
    "in_work": "In work",
    "blocked": "Blocked",
    "done": "Done",
}
CLOSURE_TYPES = {"done", "skipped", "not_needed", "partial"}
CLOSURE_LABELS = {
    "done": "Done",
    "skipped": "Skipped",
    "not_needed": "Not needed",
    "partial": "Partial",
}
TODO_STATUS_ORDER = {"in_work": 0, "ready": 1, "blocked": 2, "planning": 3, "backlog": 4, "done": 5}
STATUS_ORDER = {"overdue": 0, "due_today": 1, "upcoming": 2}
CSRF_COOKIE = "hm_csrf"
THEME_COOKIE = "hm_theme"
ADMIN_COOKIE = "hm_admin"
THEMES = {"system", "light", "dark"}
APP_VERSION = "2.0.4"
MAX_FORM_BYTES = 16 * 1024
ADMIN_SESSION_SECONDS = 15 * 60
CSRF_TOKEN_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
HA_AREA_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
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
SEED_DEMO_DATA = bool_env("HOME_MAINTENANCE_SEED_DEMO", False)
HOMEASSISTANT_SYNC_INTERVAL_SECONDS = bounded_int_env("HOME_MAINTENANCE_HA_SYNC_INTERVAL_SECONDS", 300, 30, 86400)
HOMEASSISTANT_API_BASE = os.environ.get("HOME_MAINTENANCE_HA_API_BASE", "http://supervisor/core/api").rstrip("/")
SUPERVISOR_API_BASE = os.environ.get("HOME_MAINTENANCE_SUPERVISOR_API_BASE", "http://supervisor").rstrip("/")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "").strip()
HOMEASSISTANT_REQUEST_TIMEOUT = 3
HOMEASSISTANT_ENTITY_PREFIX = "mxtracker"
HOMEASSISTANT_DASHBOARD_WINDOW_DAYS = 14
API_ACTION_TOKEN = os.environ.get("HOME_MAINTENANCE_API_ACTION_TOKEN", "").strip()
HA_PUBLISHER = None
ADMIN_SESSIONS = {}
ADMIN_SESSIONS_LOCK = threading.Lock()
HOMEASSISTANT_AREAS_TEMPLATE = """
{% set ns = namespace(items=[]) %}
{% for area_id in areas() %}
{% set resolved_name = area_name(area_id) or area_id %}
{% set ns.items = ns.items + [{'id': area_id, 'name': resolved_name}] %}
{% endfor %}
{{ ns.items | to_json }}
""".strip()



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


def query_first(query, name, default=""):
    values = query.get(name, [default]) if query else [default]
    return values[0].strip() if values else default


def selected_attr(current, value):
    return " selected" if current == value else ""


@contextmanager
def connect_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'General',
                notes TEXT NOT NULL DEFAULT '',
                ha_area_id TEXT NOT NULL DEFAULT '',
                ha_area_name TEXT NOT NULL DEFAULT '',
                asset_name TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                model_number TEXT NOT NULL DEFAULT '',
                serial_number TEXT NOT NULL DEFAULT '',
                filter_size TEXT NOT NULL DEFAULT '',
                purchase_date TEXT NOT NULL DEFAULT '',
                warranty_expires_on TEXT NOT NULL DEFAULT '',
                priority TEXT NOT NULL DEFAULT 'normal',
                season TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                requires_supplies INTEGER NOT NULL DEFAULT 0,
                estimated_minutes INTEGER NOT NULL DEFAULT 0,
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
        if "ha_area_id" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN ha_area_id TEXT NOT NULL DEFAULT ''")
        if "ha_area_name" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN ha_area_name TEXT NOT NULL DEFAULT ''")
        task_column_defaults = {
            "asset_name": "TEXT NOT NULL DEFAULT ''",
            "location": "TEXT NOT NULL DEFAULT ''",
            "model_number": "TEXT NOT NULL DEFAULT ''",
            "serial_number": "TEXT NOT NULL DEFAULT ''",
            "filter_size": "TEXT NOT NULL DEFAULT ''",
            "purchase_date": "TEXT NOT NULL DEFAULT ''",
            "warranty_expires_on": "TEXT NOT NULL DEFAULT ''",
            "priority": "TEXT NOT NULL DEFAULT 'normal'",
            "season": "TEXT NOT NULL DEFAULT ''",
            "tags": "TEXT NOT NULL DEFAULT ''",
            "requires_supplies": "INTEGER NOT NULL DEFAULT 0",
            "estimated_minutes": "INTEGER NOT NULL DEFAULT 0",
        }
        for column_name, definition in task_column_defaults.items():
            if column_name not in columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column_name} {definition}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS completion_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                task_name TEXT NOT NULL,
                completed_on TEXT NOT NULL,
                next_due_on TEXT NOT NULL,
                previous_next_due_on TEXT NOT NULL DEFAULT '',
                closure_type TEXT NOT NULL DEFAULT 'done',
                closure_notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
            """
        )
        history_columns = {row["name"] for row in conn.execute("PRAGMA table_info(completion_history)").fetchall()}
        if "previous_next_due_on" not in history_columns:
            conn.execute("ALTER TABLE completion_history ADD COLUMN previous_next_due_on TEXT NOT NULL DEFAULT ''")
        if "closure_type" not in history_columns:
            conn.execute("ALTER TABLE completion_history ADD COLUMN closure_type TEXT NOT NULL DEFAULT 'done'")
        if "closure_notes" not in history_columns:
            conn.execute("ALTER TABLE completion_history ADD COLUMN closure_notes TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_next_due_on ON tasks(next_due_on, name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_category ON tasks(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_ha_area ON tasks(ha_area_id, next_due_on)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority, next_due_on)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_completed_on ON completion_history(completed_on DESC, id DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ha_areas (
                area_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ha_areas_name ON ha_areas(name, area_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_checklist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                is_done INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_checklist_task ON task_checklist_items(task_id, position, id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                task_name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_data TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at DESC, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_events_created ON task_events(created_at DESC, id DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS todo_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'General',
                description TEXT NOT NULL DEFAULT '',
                ha_area_id TEXT NOT NULL DEFAULT '',
                ha_area_name TEXT NOT NULL DEFAULT '',
                likelihood INTEGER NOT NULL DEFAULT 2,
                consequence INTEGER NOT NULL DEFAULT 2,
                urgency INTEGER NOT NULL DEFAULT 2,
                effort INTEGER NOT NULL DEFAULT 2,
                cost INTEGER NOT NULL DEFAULT 2,
                status TEXT NOT NULL DEFAULT 'backlog',
                target_on TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS todo_checklist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                parent_id INTEGER,
                label TEXT NOT NULL,
                required_for_start INTEGER NOT NULL DEFAULT 0,
                is_done INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES todo_projects(id) ON DELETE CASCADE,
                FOREIGN KEY(parent_id) REFERENCES todo_checklist_items(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_todo_projects_status ON todo_projects(status, updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_todo_projects_area ON todo_projects(ha_area_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_todo_checklist_project ON todo_checklist_items(project_id, parent_id, position, id)")


def display_sequence(prefix, value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    return f"{prefix}{number:02d}"


def maintenance_public_id(task_id):
    return display_sequence("Mx", task_id)


def maintenance_iteration_id(task_id, iteration_number):
    return f"{maintenance_public_id(task_id)}-{int(iteration_number)}"


def todo_public_id(project_id):
    return display_sequence("ToDo-", project_id)


def id_badge(value):
    return f'<span class="id-badge">{escape(value)}</span>'


def get_tasks():
    with connect_db() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY next_due_on ASC, name ASC").fetchall()
    tasks = []
    for row in rows:
        task = dict(row)
        tasks.append(enrich_task(task))
    return sorted(tasks, key=lambda item: (STATUS_ORDER[item["status"]], item["next_due_on"], item["name"].lower()))


def get_task(task_id):
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def enrich_task(task):
    task["public_id"] = maintenance_public_id(task["id"])
    task["category"] = task.get("category") or "General"
    task["ha_area_id"] = task.get("ha_area_id") or ""
    task["ha_area_name"] = task.get("ha_area_name") or ""
    for field in [
        "asset_name",
        "location",
        "model_number",
        "serial_number",
        "filter_size",
        "purchase_date",
        "warranty_expires_on",
        "priority",
        "season",
        "tags",
    ]:
        task[field] = task.get(field) or ("normal" if field == "priority" else "")
    if task["priority"] not in PRIORITIES:
        task["priority"] = "normal"
    if task["season"] not in SEASONS:
        task["season"] = ""
    task["priority_label"] = PRIORITY_LABELS[task["priority"]]
    task["season_label"] = SEASON_LABELS[task["season"]]
    task["requires_supplies"] = bool(task.get("requires_supplies"))
    task["estimated_minutes"] = int(task.get("estimated_minutes") or 0)
    task["status"] = classify_due(task["next_due_on"])
    task["status_label"] = status_label(task["status"])
    task["days_until"] = days_until(task["next_due_on"])
    task["due_phrase"] = due_phrase(task["days_until"])
    task["recurrence_phrase"] = recurrence_phrase(task["interval_count"], task["interval_unit"])
    return task


def get_enriched_task(task_id):
    task = get_task(task_id)
    return enrich_task(task) if task else None


def completion_history_rows(task_id=None, limit=None):
    where_sql = "WHERE h.task_id = ?" if task_id is not None else ""
    query_params = (task_id,) if task_id is not None else ()
    sql = f"""
        SELECT id, task_id, task_name, completed_on, next_due_on, previous_next_due_on, closure_type, closure_notes, created_at, iteration_number
        FROM (
            SELECT
                h.id,
                h.task_id,
                h.task_name,
                h.completed_on,
                h.next_due_on,
                h.previous_next_due_on,
                h.closure_type,
                h.closure_notes,
                h.created_at,
                ROW_NUMBER() OVER (
                    PARTITION BY h.task_id
                    ORDER BY h.completed_on ASC, h.id ASC
                ) AS iteration_number
            FROM completion_history h
            {where_sql}
        )
        ORDER BY completed_on DESC, id DESC
    """
    if limit is not None:
        sql += " LIMIT ?"
        query_params = (*query_params, limit)
    with connect_db() as conn:
        rows = conn.execute(sql, query_params).fetchall()
    return [enrich_history_row(row) for row in rows]


def get_history(limit=20):
    return completion_history_rows(limit=limit)


def get_task_history(task_id, limit=50):
    return completion_history_rows(task_id=task_id, limit=limit)


def get_all_history():
    return completion_history_rows()


def get_completion_history_item(history_id):
    if not history_id:
        return None
    for item in completion_history_rows():
        if item["id"] == history_id:
            return item
    return None


def enrich_history_row(row):
    item = dict(row)
    item["task_public_id"] = maintenance_public_id(item["task_id"])
    item["iteration_number"] = int(item.get("iteration_number") or 0)
    item["public_id"] = maintenance_iteration_id(item["task_id"], item["iteration_number"])
    item["closure_type"] = item.get("closure_type") if item.get("closure_type") in CLOSURE_TYPES else "done"
    item["closure_label"] = CLOSURE_LABELS[item["closure_type"]]
    item["closure_notes"] = item.get("closure_notes") or ""
    return item


def get_task_checklist(task_id):
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM task_checklist_items
            WHERE task_id = ?
            ORDER BY position, id
            """,
            (task_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_task_checklist_item(item_id):
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM task_checklist_items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def add_task_checklist_item(task_id, label):
    label = label.strip()
    task = get_task(task_id)
    if not label or len(label) > 180 or not task:
        return False
    now = utc_now_iso()
    with connect_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next_position FROM task_checklist_items WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO task_checklist_items (task_id, label, is_done, position, created_at, updated_at)
            VALUES (?, ?, 0, ?, ?, ?)
            """,
            (task_id, label, row["next_position"], now, now),
        )
    request_homeassistant_sync()
    record_task_event(task_id, task["name"], "checklist_added", {"label": label})
    return True


def toggle_task_checklist_item(item_id):
    item = get_task_checklist_item(item_id)
    if not item:
        return None
    now = utc_now_iso()
    with connect_db() as conn:
        conn.execute(
            "UPDATE task_checklist_items SET is_done = ?, updated_at = ? WHERE id = ?",
            (0 if item["is_done"] else 1, now, item_id),
        )
    request_homeassistant_sync()
    task = get_task(item["task_id"])
    if task:
        record_task_event(item["task_id"], task["name"], "checklist_toggled", {"label": item["label"], "is_done": not bool(item["is_done"])})
    return item["task_id"]


def delete_task_checklist_item(item_id):
    item = get_task_checklist_item(item_id)
    if not item:
        return None
    with connect_db() as conn:
        conn.execute("DELETE FROM task_checklist_items WHERE id = ?", (item_id,))
    request_homeassistant_sync()
    task = get_task(item["task_id"])
    if task:
        record_task_event(item["task_id"], task["name"], "checklist_deleted", {"label": item["label"]})
    return item["task_id"]


def record_task_event(task_id, task_name, event_type, event_data=None):
    if not task_id or not task_name or not event_type:
        return False
    payload = json.dumps(event_data or {}, separators=(",", ":"), sort_keys=True)
    if len(payload) > 4096:
        payload = json.dumps({"truncated": True}, separators=(",", ":"))
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO task_events (task_id, task_name, event_type, event_data, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, task_name, event_type, payload, utc_now_iso()),
        )
    return True


def get_task_events(task_id, limit=80):
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM task_events
            WHERE task_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (task_id, limit),
        ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        try:
            event["event_data_json"] = json.loads(event["event_data"] or "{}")
        except json.JSONDecodeError:
            event["event_data_json"] = {}
        events.append(event)
    return events


def get_all_task_events(limit=None):
    sql = """
        SELECT *
        FROM task_events
        ORDER BY created_at DESC, id DESC
    """
    params = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    with connect_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def event_type_label(event_type):
    return {
        "created": "Created",
        "updated": "Updated",
        "completed": "Completed",
        "completion_removed": "Completion removed",
        "snoozed": "Snoozed",
        "deleted": "Deleted",
        "checklist_added": "Checklist step added",
        "checklist_toggled": "Checklist step toggled",
        "checklist_deleted": "Checklist step removed",
    }.get(event_type, event_type.replace("_", " ").title())


def summarize_event_data(raw_data):
    try:
        data = json.loads(raw_data or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    details = []
    if "changed" in data and isinstance(data["changed"], list) and data["changed"]:
        details.append("changed=" + ",".join(str(item) for item in data["changed"][:12]))
    for key in ["completion_id", "closure_type", "closure_notes", "completed_on", "next_due_on", "previous_next_due_on", "from", "to", "days", "label", "is_done", "name"]:
        if key in data and data[key] not in ("", None, []):
            details.append(f"{key}={data[key]}")
    return "; ".join(details)


def clamp_score(value, default=2):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(5, number))


def todo_hazard_score(todo):
    return int(todo["likelihood"]) * int(todo["consequence"])


def todo_priority_score(todo):
    hazard = todo_hazard_score(todo)
    drag = 1 + ((int(todo["effort"]) - 1) * 0.22) + ((int(todo["cost"]) - 1) * 0.18)
    return round(((hazard * 1.35) + (int(todo["urgency"]) * 3) + (int(todo["consequence"]) * 1.8)) / drag, 1)


def todo_risk_band(score):
    if score >= 20:
        return "critical"
    if score >= 12:
        return "high"
    if score >= 6:
        return "medium"
    return "low"


def todo_completion_stats(items):
    total = len(items)
    done = sum(1 for item in items if item["is_done"])
    required = [item for item in items if item["required_for_start"]]
    work = [item for item in items if not item["required_for_start"]]
    required_done = sum(1 for item in required if item["is_done"])
    return {
        "total": total,
        "done": done,
        "work_total": len(work),
        "work_done": sum(1 for item in work if item["is_done"]),
        "progress_percent": 100 if not total else round((done / total) * 100),
        "required_total": len(required),
        "required_done": required_done,
        "ready_to_start": bool(required) and required_done == len(required),
    }


def derive_todo_status(todo, stats):
    saved = todo.get("status") or "backlog"
    if saved == "blocked":
        return "blocked"
    if saved == "done" or (stats["work_total"] > 0 and stats["work_done"] == stats["work_total"]):
        return "done"
    if saved == "in_work" or (stats["done"] > 0 and not stats["ready_to_start"]):
        return "in_work"
    if stats["ready_to_start"]:
        return "ready"
    return saved if saved in TODO_STATUSES else "backlog"


def todo_action_lane(todo):
    if todo["derived_status"] == "blocked":
        return "Unblock"
    if todo["derived_status"] == "done":
        return "Done"
    if todo_hazard_score(todo) >= 15 and int(todo["effort"]) <= 3:
        return "Do soon"
    if int(todo["effort"]) <= 2 and int(todo["cost"]) <= 2:
        return "Quick win"
    if int(todo["cost"]) >= 4 or int(todo["effort"]) >= 4:
        return "Plan"
    return "Steady"


def get_todo_checklist(project_id):
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM todo_checklist_items
            WHERE project_id = ?
            ORDER BY parent_id IS NOT NULL, parent_id, position, id
            """,
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def enrich_todo(todo, checklist=None):
    todo["public_id"] = todo_public_id(todo["id"])
    todo["category"] = todo.get("category") or "General"
    todo["ha_area_id"] = todo.get("ha_area_id") or ""
    todo["ha_area_name"] = todo.get("ha_area_name") or ""
    todo["status"] = todo.get("status") if todo.get("status") in TODO_STATUSES else "backlog"
    items = checklist if checklist is not None else get_todo_checklist(todo["id"])
    todo.update(todo_completion_stats(items))
    todo["derived_status"] = derive_todo_status(todo, todo)
    todo["status_label"] = TODO_STATUS_LABELS[todo["derived_status"]]
    todo["hazard_score"] = todo_hazard_score(todo)
    todo["risk_band"] = todo_risk_band(todo["hazard_score"])
    todo["priority_score"] = todo_priority_score(todo)
    todo["action_lane"] = todo_action_lane(todo)
    return todo


def get_todos():
    with connect_db() as conn:
        rows = conn.execute("SELECT * FROM todo_projects").fetchall()
    todos = [enrich_todo(dict(row)) for row in rows]
    return sorted(todos, key=lambda item: (TODO_STATUS_ORDER[item["derived_status"]], -item["priority_score"], item["title"].lower()))


def get_todo(project_id):
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM todo_projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def get_enriched_todo(project_id):
    todo = get_todo(project_id)
    return enrich_todo(todo) if todo else None


def get_checklist_item(item_id):
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM todo_checklist_items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def validate_todo_form(fields):
    errors = []
    title = fields.get("title", "").strip()
    category = fields.get("category", "General").strip()
    description = fields.get("description", "").strip()
    ha_area_id = fields.get("ha_area_id", "").strip()
    ha_area_name = ""
    status = fields.get("status", "backlog").strip()
    target_on = fields.get("target_on", "").strip()
    if not title:
        errors.append("Title is required.")
    if len(title) > 120:
        errors.append("Title must be 120 characters or fewer.")
    if category not in TODO_CATEGORIES:
        errors.append("Choose a valid category.")
    if len(description) > 1600:
        errors.append("Details must be 1600 characters or fewer.")
    if status not in TODO_STATUSES:
        errors.append("Choose a valid status.")
    if target_on:
        try:
            parse_date(target_on)
        except ValueError:
            errors.append("Target date must be valid.")
    if ha_area_id:
        area_lookup = homeassistant_area_lookup()
        if not valid_ha_area_id(ha_area_id):
            errors.append("Choose a valid Home Assistant area.")
        elif area_lookup:
            ha_area_name = area_lookup.get(ha_area_id, "")
            if not ha_area_name:
                errors.append("Choose a Home Assistant area from the synced list.")
        else:
            ha_area_name = fields.get("ha_area_name", "").strip() or ha_area_id
            if not valid_ha_area_name(ha_area_name):
                errors.append("Choose a valid Home Assistant area.")
    return errors, {
        "title": title,
        "category": category,
        "description": description,
        "ha_area_id": ha_area_id,
        "ha_area_name": ha_area_name,
        "likelihood": clamp_score(fields.get("likelihood"), 2),
        "consequence": clamp_score(fields.get("consequence"), 2),
        "urgency": clamp_score(fields.get("urgency"), 2),
        "effort": clamp_score(fields.get("effort"), 2),
        "cost": clamp_score(fields.get("cost"), 2),
        "status": status,
        "target_on": target_on,
    }


def save_todo(fields, project_id=None):
    now = utc_now_iso()
    with connect_db() as conn:
        if project_id is None:
            cursor = conn.execute(
                """
                INSERT INTO todo_projects
                    (title, category, description, ha_area_id, ha_area_name, likelihood, consequence, urgency, effort, cost, status, target_on, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (fields["title"], fields["category"], fields["description"], fields.get("ha_area_id", ""), fields.get("ha_area_name", ""), fields["likelihood"], fields["consequence"], fields["urgency"], fields["effort"], fields["cost"], fields["status"], fields["target_on"], now, now),
            )
            project_id = cursor.lastrowid
        else:
            conn.execute(
                """
                UPDATE todo_projects
                SET title = ?, category = ?, description = ?, ha_area_id = ?, ha_area_name = ?,
                    likelihood = ?, consequence = ?, urgency = ?, effort = ?, cost = ?,
                    status = ?, target_on = ?, updated_at = ?
                WHERE id = ?
                """,
                (fields["title"], fields["category"], fields["description"], fields.get("ha_area_id", ""), fields.get("ha_area_name", ""), fields["likelihood"], fields["consequence"], fields["urgency"], fields["effort"], fields["cost"], fields["status"], fields["target_on"], now, project_id),
            )
    request_homeassistant_sync()
    return project_id


def add_todo_checklist_item(project_id, label, parent_id=None, required_for_start=False):
    label = label.strip()
    if not label or len(label) > 180:
        return False
    if parent_id:
        parent = get_checklist_item(parent_id)
        if not parent or parent["project_id"] != project_id:
            return False
    now = utc_now_iso()
    with connect_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next_position FROM todo_checklist_items WHERE project_id = ? AND parent_id IS ?",
            (project_id, parent_id),
        ).fetchone()
        cursor = conn.execute(
            """
            INSERT INTO todo_checklist_items
                (project_id, parent_id, label, required_for_start, is_done, position, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (project_id, parent_id, label, 1 if required_for_start else 0, row["next_position"], now, now),
        )
    request_homeassistant_sync()
    return cursor.lastrowid


def toggle_todo_checklist_item(item_id):
    item = get_checklist_item(item_id)
    if not item:
        return None
    is_done = 0 if item["is_done"] else 1
    now = utc_now_iso()
    with connect_db() as conn:
        conn.execute("UPDATE todo_checklist_items SET is_done = ?, updated_at = ? WHERE id = ?", (is_done, now, item_id))
        if is_done:
            conn.execute("UPDATE todo_checklist_items SET is_done = 1, updated_at = ? WHERE parent_id = ?", (now, item_id))
    request_homeassistant_sync()
    return item["project_id"]


def delete_todo_checklist_item(item_id):
    item = get_checklist_item(item_id)
    if not item:
        return None
    with connect_db() as conn:
        conn.execute("DELETE FROM todo_checklist_items WHERE id = ?", (item_id,))
    request_homeassistant_sync()
    return item["project_id"]


def delete_todo(project_id):
    with connect_db() as conn:
        conn.execute("DELETE FROM todo_projects WHERE id = ?", (project_id,))
    request_homeassistant_sync()


def seed_demo_data():
    with connect_db() as conn:
        task_count = conn.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()["count"]
        todo_count = conn.execute("SELECT COUNT(*) AS count FROM todo_projects").fetchone()["count"]
    if task_count or todo_count:
        return False

    today = date.today()
    demo_tasks = [
        {
            "name": "Replace HVAC filter",
            "category": "HVAC",
            "notes": "Demo recurring item: check size before ordering replacements.",
            "ha_area_id": "",
            "ha_area_name": "",
            "interval_count": 3,
            "interval_unit": "months",
            "next_due_on": today.isoformat(),
        },
        {
            "name": "Flush water heater",
            "category": "Plumbing",
            "notes": "Demo recurring item: attach hose, drain sediment, inspect for leaks.",
            "ha_area_id": "",
            "ha_area_name": "",
            "interval_count": 1,
            "interval_unit": "years",
            "next_due_on": (today + timedelta(days=18)).isoformat(),
        },
    ]
    for task in demo_tasks:
        save_task(task)

    demo_todos = [
        (
            {
                "title": "Fix running toilet",
                "category": "Plumbing",
                "description": "Tank keeps running after flush. Likely flapper or fill valve. Water waste is high but repair effort is small.",
                "ha_area_id": "",
                "ha_area_name": "",
                "likelihood": 5,
                "consequence": 3,
                "urgency": 4,
                "effort": 2,
                "cost": 1,
                "status": "planning",
                "target_on": (today + timedelta(days=2)).isoformat(),
            },
            [
                ("Identify toilet model", True, True, []),
                ("Buy flapper or fill valve kit", True, False, ["Check shutoff valve first"]),
                ("Replace part and test three flushes", False, False, []),
            ],
        ),
        (
            {
                "title": "Replace kitchen faucet",
                "category": "Plumbing",
                "description": "Handle leaks and finish is worn. Needs parts chosen before the work can start.",
                "ha_area_id": "",
                "ha_area_name": "",
                "likelihood": 4,
                "consequence": 4,
                "urgency": 3,
                "effort": 3,
                "cost": 3,
                "status": "in_work",
                "target_on": (today + timedelta(days=10)).isoformat(),
            },
            [
                ("Measure sink hole layout", True, True, []),
                ("Pick replacement faucet", True, False, ["Confirm supply line length", "Check if basin wrench is needed"]),
                ("Install and leak test", False, False, []),
            ],
        ),
        (
            {
                "title": "Replace sparking outlet",
                "category": "Electrical",
                "description": "Intermittent spark when plugging in a vacuum. Treat as a safety issue and verify the breaker before work.",
                "ha_area_id": "",
                "ha_area_name": "",
                "likelihood": 4,
                "consequence": 5,
                "urgency": 5,
                "effort": 3,
                "cost": 2,
                "status": "planning",
                "target_on": today.isoformat(),
            },
            [
                ("Find and label breaker", True, False, []),
                ("Buy tamper-resistant outlet", True, False, []),
                ("Replace outlet or call electrician", False, False, ["Use tester before touching wires"]),
            ],
        ),
        (
            {
                "title": "Patch hallway drywall dent",
                "category": "Interior",
                "description": "Cosmetic dent from moving furniture. Low consequence, quick if supplies are already out.",
                "ha_area_id": "",
                "ha_area_name": "",
                "likelihood": 5,
                "consequence": 1,
                "urgency": 1,
                "effort": 1,
                "cost": 1,
                "status": "backlog",
                "target_on": "",
            },
            [
                ("Find matching paint", True, False, []),
                ("Spackle, sand, and touch up", False, False, []),
            ],
        ),
        (
            {
                "title": "Investigate garage ceiling stain",
                "category": "Water",
                "description": "Small stain near the air handler. Could be old, but consequence is high if active.",
                "ha_area_id": "",
                "ha_area_name": "",
                "likelihood": 3,
                "consequence": 5,
                "urgency": 4,
                "effort": 2,
                "cost": 2,
                "status": "blocked",
                "target_on": (today + timedelta(days=4)).isoformat(),
            },
            [
                ("Check attic after rain", True, False, []),
                ("Photograph stain and mark edge", False, False, []),
                ("Decide whether to call roofer or HVAC tech", False, False, []),
            ],
        ),
    ]
    for fields, checklist in demo_todos:
        project_id = save_todo(fields)
        for label, required, done, children in checklist:
            item_id = add_todo_checklist_item(project_id, label, required_for_start=required)
            if done:
                toggle_todo_checklist_item(item_id)
            for child_label in children:
                add_todo_checklist_item(project_id, child_label, parent_id=item_id)
    set_setting("demo_seeded_at", utc_now_iso())
    return True


def get_completion_count(days=30):
    since = (date.today() - timedelta(days=days)).isoformat()
    with connect_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM completion_history WHERE completed_on >= ? AND closure_type = 'done'",
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


def update_setting(key, value):
    value = "" if value is None else str(value)
    if get_setting(key) == value:
        return False
    set_setting(key, value)
    return True


def valid_ha_area_id(value):
    return 0 < len(value) <= 80 and all(char in HA_AREA_ID_CHARS for char in value)


def valid_ha_area_name(value):
    return 0 < len(value) <= 120 and not any(ord(char) < 32 for char in value)


def clean_ha_area_record(item):
    if not isinstance(item, dict):
        return None
    raw_area_id = item.get("id", "")
    raw_name = item.get("name", "")
    area_id = "" if raw_area_id is None else str(raw_area_id).strip()
    name = "" if raw_name is None else str(raw_name).strip()
    if not valid_ha_area_id(area_id):
        return None
    if not name:
        name = area_id
    if not valid_ha_area_name(name):
        return None
    return {"area_id": area_id, "name": name}


def get_homeassistant_areas():
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT area_id, name
            FROM ha_areas
            ORDER BY lower(name), area_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def replace_homeassistant_areas(areas):
    cleaned = []
    seen = set()
    for item in areas:
        record = clean_ha_area_record(item)
        if record and record["area_id"] not in seen:
            cleaned.append(record)
            seen.add(record["area_id"])
    now = utc_now_iso()
    with connect_db() as conn:
        existing = [
            dict(row)
            for row in conn.execute(
                """
                SELECT area_id, name
                FROM ha_areas
                ORDER BY area_id
                """
            ).fetchall()
        ]
        wanted = sorted(cleaned, key=lambda item: item["area_id"])
        if existing == wanted:
            return False
        conn.execute("DELETE FROM ha_areas")
        conn.executemany(
            """
            INSERT INTO ha_areas (area_id, name, updated_at)
            VALUES (?, ?, ?)
            """,
            [(item["area_id"], item["name"], now) for item in cleaned],
        )
    return True


def homeassistant_area_lookup():
    return {area["area_id"]: area["name"] for area in get_homeassistant_areas()}


def remember_ingress_base_path(base_path):
    base_path = normalize_base_path(base_path)
    if not base_path:
        return
    if update_setting("ingress_base_path", base_path):
        request_homeassistant_sync()


def get_ingress_base_path():
    return normalize_base_path(get_setting("ingress_base_path", ""))


def get_homeassistant_ingress_base_path():
    for key in ("supervisor_ingress_url", "ingress_base_path"):
        value = normalize_base_path(get_setting(key, ""))
        if value:
            return value
    return ""


def supervisor_get_json(path):
    if not SUPERVISOR_TOKEN:
        return {}
    request = urllib.request.Request(
        f"{SUPERVISOR_API_BASE}{path}",
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=HOMEASSISTANT_REQUEST_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("data", payload)


def render_homeassistant_template(template):
    if not SUPERVISOR_TOKEN:
        return ""
    body = json.dumps({"template": template}, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{HOMEASSISTANT_API_BASE}/template",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(request, timeout=HOMEASSISTANT_REQUEST_TIMEOUT) as response:
        return response.read().decode("utf-8")


def refresh_homeassistant_app_info():
    try:
        info = supervisor_get_json("/addons/self/info")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return False
    changed = False
    ingress_url = normalize_base_path(info.get("ingress_url", ""))
    slug = str(info.get("slug", "")).strip()
    if ingress_url:
        changed = update_setting("supervisor_ingress_url", ingress_url) or changed
    if slug:
        changed = update_setting("addon_slug", slug) or changed
    return changed


def refresh_homeassistant_areas():
    try:
        rendered = render_homeassistant_template(HOMEASSISTANT_AREAS_TEMPLATE)
        if len(rendered) > 64 * 1024:
            return False
        areas = json.loads(rendered)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, TypeError):
        return False
    if not isinstance(areas, list) or len(areas) > 256:
        return False
    return replace_homeassistant_areas(areas)


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


def task_matches_filters(task, filters):
    q = filters["q"].lower()
    if q:
        searchable = " ".join(
            [
                task["name"],
                task["public_id"],
                task["notes"],
                task["category"],
                task.get("ha_area_name", ""),
                task.get("ha_area_id", ""),
                task.get("asset_name", ""),
                task.get("location", ""),
                task.get("model_number", ""),
                task.get("serial_number", ""),
                task.get("filter_size", ""),
                task.get("tags", ""),
                task.get("priority_label", ""),
                task.get("season_label", ""),
                task["recurrence_phrase"],
                task["due_phrase"],
            ]
        ).lower()
        if q not in searchable:
            return False
    if filters["category"] and task["category"] != filters["category"]:
        return False
    status = filters["status"]
    if status == "due_14":
        if task["status"] not in {"overdue", "due_today"} and task["days_until"] > HOMEASSISTANT_DASHBOARD_WINDOW_DAYS:
            return False
    elif status == "never_done":
        if task["last_completed_on"]:
            return False
    elif status == "requires_supplies":
        if not task["requires_supplies"]:
            return False
    elif status and task["status"] != status:
        return False
    area = filters["area"]
    if area == "unassigned":
        if task.get("ha_area_id"):
            return False
    elif area and task.get("ha_area_id") != area:
        return False
    return True


def task_filters_from_query(query):
    category = query_first(query, "category")
    if category not in CATEGORIES:
        category = ""
    status = query_first(query, "status")
    if status not in {"", "overdue", "due_today", "upcoming", "due_14", "never_done", "requires_supplies"}:
        status = ""
    area = query_first(query, "area")
    known_areas = {area_item["area_id"] for area_item in get_homeassistant_areas()}
    if area not in known_areas and area != "unassigned":
        area = ""
    return {
        "q": query_first(query, "q")[:120],
        "category": category,
        "status": status,
        "area": area,
    }


def filter_tasks(tasks, query):
    filters = task_filters_from_query(query or {})
    return [task for task in tasks if task_matches_filters(task, filters)], filters


def todo_matches_filters(todo, filters):
    q = filters["q"].lower()
    if q:
        searchable = " ".join(
            [
                todo["title"],
                todo["public_id"],
                todo["description"],
                todo["category"],
                todo.get("ha_area_name", ""),
                todo.get("ha_area_id", ""),
                todo["status_label"],
                todo["risk_band"],
                todo["action_lane"],
            ]
        ).lower()
        if q not in searchable:
            return False
    if filters["category"] and todo["category"] != filters["category"]:
        return False
    status = filters["status"]
    if status == "active":
        if todo["derived_status"] == "done":
            return False
    elif status and status != "all" and todo["derived_status"] != status:
        return False
    if filters["risk"] and todo["risk_band"] != filters["risk"]:
        return False
    area = filters["area"]
    if area == "unassigned":
        if todo.get("ha_area_id"):
            return False
    elif area and todo.get("ha_area_id") != area:
        return False
    return True


def todo_filters_from_query(query):
    category = query_first(query, "category")
    if category not in TODO_CATEGORIES:
        category = ""
    status = query_first(query, "status", "active") or "active"
    if status not in {"active", "all", "backlog", "planning", "ready", "in_work", "blocked", "done"}:
        status = "active"
    risk = query_first(query, "risk")
    if risk not in {"", "critical", "high", "medium", "low"}:
        risk = ""
    area = query_first(query, "area")
    known_areas = {area_item["area_id"] for area_item in get_homeassistant_areas()}
    if area not in known_areas and area != "unassigned":
        area = ""
    return {
        "q": query_first(query, "q")[:120],
        "category": category,
        "status": status,
        "risk": risk,
        "area": area,
    }


def filter_todos(todos, query):
    filters = todo_filters_from_query(query or {})
    return [todo for todo in todos if todo_matches_filters(todo, filters)], filters


def homeassistant_detail_path(task_id):
    return f"/?mx_item={task_id}"


def homeassistant_focus_path():
    return "/?mx_view=focus"


def homeassistant_task_row(task, base_path=""):
    detail_path = homeassistant_detail_path(task["id"])
    return {
        "id": task["id"],
        "public_id": task["public_id"],
        "name": task["name"],
        "category": task["category"],
        "ha_area_id": task.get("ha_area_id", ""),
        "ha_area_name": task.get("ha_area_name", ""),
        "asset_name": task.get("asset_name", ""),
        "location": task.get("location", ""),
        "priority": task.get("priority", "normal"),
        "priority_label": task.get("priority_label", "Normal"),
        "season": task.get("season", ""),
        "season_label": task.get("season_label", "Any season"),
        "requires_supplies": bool(task.get("requires_supplies")),
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


def homeassistant_todo_row(todo, base_path=""):
    detail_path = f"/todo/{todo['id']}"
    return {
        "id": todo["id"],
        "public_id": todo["public_id"],
        "title": todo["title"],
        "category": todo["category"],
        "ha_area_id": todo.get("ha_area_id", ""),
        "ha_area_name": todo.get("ha_area_name", ""),
        "status": todo["status_label"],
        "status_key": todo["derived_status"],
        "risk_band": todo["risk_band"],
        "hazard_score": todo["hazard_score"],
        "priority_score": todo["priority_score"],
        "likelihood": todo["likelihood"],
        "consequence": todo["consequence"],
        "urgency": todo["urgency"],
        "effort": todo["effort"],
        "cost": todo["cost"],
        "progress_percent": todo["progress_percent"],
        "ready_to_start": todo["ready_to_start"],
        "action_lane": todo["action_lane"],
        "target_on": todo["target_on"],
        "detail_path": detail_path,
        "detail_url": homeassistant_link(detail_path, base_path),
    }


def markdown_table_link(label, url):
    safe_label = escape(label).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    safe_label = safe_label.replace("\n", " ").replace("\r", " ").replace("|", "&#124;")
    if not url:
        return safe_label
    return f"[{safe_label}]({url})"


def homeassistant_due_table(items):
    if not items:
        return "Nothing is due in the next 14 days."
    lines = [
        "| ID | Task | Status | Due date | Category | Area | Last done | Repeat |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in items:
        due = item["due_phrase"]
        if item["is_overdue"]:
            due = f'<span style="color: var(--error-color); font-weight: 700;">{escape(due)}</span>'
        lines.append(
            " | ".join(
                [
                    f"| {escape(item['public_id'])}",
                    markdown_table_link(item["name"], item["detail_url"]),
                    due,
                    escape(item["due_date"]),
                    escape(item["category"]),
                    escape(item.get("ha_area_name") or "Unassigned"),
                    escape(item["last_done"]),
                    f"{escape(item['repeat'])} |",
                ]
            )
        )
    return "\n".join(lines)


def homeassistant_todo_table(items):
    if not items:
        return "No house todos yet."
    lines = [
        "| ID | Todo | Status | Score | Progress | Area | Lane |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in items:
        lines.append(
            " | ".join(
                [
                    f"| {escape(item['public_id'])}",
                    markdown_table_link(item["title"], item["detail_url"]),
                    escape(item["status"]),
                    escape(item["priority_score"]),
                    f"{escape(item['progress_percent'])}%",
                    escape(item.get("ha_area_name") or "Unassigned"),
                    f"{escape(item['action_lane'])} |",
                ]
            )
        )
    return "\n".join(lines)


def homeassistant_state_payloads(tasks=None):
    tasks = tasks if tasks is not None else get_tasks()
    summary = summarize(tasks)
    ingress_base_path = get_homeassistant_ingress_base_path()
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
    dashboard_table = homeassistant_due_table(dashboard_items)
    todos = get_todos()
    todo_active = [todo for todo in todos if todo["derived_status"] != "done"]
    todo_ready = [todo for todo in todo_active if todo["derived_status"] == "ready"]
    todo_in_work = [todo for todo in todo_active if todo["derived_status"] == "in_work"]
    todo_top = sorted(todo_active, key=lambda item: (-item["priority_score"], item["title"].lower()))[:10]
    todo_items = [homeassistant_todo_row(todo, ingress_base_path) for todo in todo_top]
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
                "dashboard_url": homeassistant_link(homeassistant_focus_path(), ingress_base_path),
                "calendar_url": homeassistant_link("/calendar", ingress_base_path),
                "reports_url": homeassistant_link("/reports", ingress_base_path),
                "setup_url": homeassistant_link("/ha-setup", ingress_base_path),
                "actions": {
                    "mark_done": "/api/actions/mark_done",
                    "snooze": "/api/actions/snooze",
                    "open_detail": "/api/actions/open_detail",
                    "requires_token": True,
                },
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
                "markdown_table": dashboard_table,
                "item_count": len(dashboard_items),
                "window_days": HOMEASSISTANT_DASHBOARD_WINDOW_DAYS,
                "dashboard_url": homeassistant_link(homeassistant_focus_path(), ingress_base_path),
                "calendar_url": homeassistant_link("/calendar", ingress_base_path),
                "reports_url": homeassistant_link("/reports", ingress_base_path),
                "setup_url": homeassistant_link("/ha-setup", ingress_base_path),
                "actions": {
                    "mark_done": "/api/actions/mark_done",
                    "snooze": "/api/actions/snooze",
                    "open_detail": "/api/actions/open_detail",
                    "requires_token": True,
                },
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
                "calendar_url": homeassistant_link("/calendar", ingress_base_path),
                "reports_url": homeassistant_link("/reports", ingress_base_path),
                "setup_url": homeassistant_link("/ha-setup", ingress_base_path),
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
        f"sensor.{HOMEASSISTANT_ENTITY_PREFIX}_house_todos": {
            "state": str(len(todo_active)),
            "attributes": {
                "friendly_name": "MxTracker House Todos",
                "icon": "mdi:home-alert-outline",
                "unit_of_measurement": "items",
                "items": todo_items,
                "markdown_table": homeassistant_todo_table(todo_items),
                "item_count": len(todo_active),
                "ready_count": len(todo_ready),
                "in_work_count": len(todo_in_work),
                "dashboard_url": homeassistant_link("/todos", ingress_base_path),
                "setup_url": homeassistant_link("/ha-setup", ingress_base_path),
                "ingress_base_path": ingress_base_path,
                "updated_at": updated_at,
            },
        },
    }


def public_task(task):
    if not task:
        return None
    return {
        "id": task["id"],
        "public_id": task["public_id"],
        "name": task["name"],
        "category": task["category"],
        "ha_area_id": task.get("ha_area_id", ""),
        "ha_area_name": task.get("ha_area_name", ""),
        "notes": task["notes"],
        "asset_name": task.get("asset_name", ""),
        "location": task.get("location", ""),
        "model_number": task.get("model_number", ""),
        "serial_number": task.get("serial_number", ""),
        "filter_size": task.get("filter_size", ""),
        "purchase_date": task.get("purchase_date", ""),
        "warranty_expires_on": task.get("warranty_expires_on", ""),
        "priority": task.get("priority", "normal"),
        "priority_label": task.get("priority_label", "Normal"),
        "season": task.get("season", ""),
        "season_label": task.get("season_label", "Any season"),
        "tags": task.get("tags", ""),
        "requires_supplies": bool(task.get("requires_supplies")),
        "estimated_minutes": task.get("estimated_minutes", 0),
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


def public_todo(todo):
    if not todo:
        return None
    return {
        "id": todo["id"],
        "public_id": todo["public_id"],
        "title": todo["title"],
        "category": todo["category"],
        "ha_area_id": todo.get("ha_area_id", ""),
        "ha_area_name": todo.get("ha_area_name", ""),
        "description": todo["description"],
        "likelihood": todo["likelihood"],
        "consequence": todo["consequence"],
        "urgency": todo["urgency"],
        "effort": todo["effort"],
        "cost": todo["cost"],
        "status": todo["derived_status"],
        "status_label": todo["status_label"],
        "target_on": todo["target_on"],
        "hazard_score": todo["hazard_score"],
        "priority_score": todo["priority_score"],
        "risk_band": todo["risk_band"],
        "action_lane": todo["action_lane"],
        "progress_percent": todo["progress_percent"],
        "ready_to_start": todo["ready_to_start"],
    }


def csv_cell(value):
    text = "" if value is None else str(value)
    if text.startswith(("\t", "\r")) or text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def safe_return_path(value):
    if value in {"/", "/items", "/focus", "/calendar", "/reports", "/ha-setup", "/admin", "/todos", "/todo/new"}:
        return value
    if value.startswith("/item/") and value.removeprefix("/item/").isdigit():
        return value
    if value.startswith("/todo/") and value.removeprefix("/todo/").isdigit():
        return value
    return "/"


def safe_theme(value):
    return value if value in THEMES else "system"


def valid_csrf_token_value(value):
    return 32 <= len(value) <= 128 and all(char in CSRF_TOKEN_CHARS for char in value)


def normalize_base_path(value):
    if not value or not value.startswith("/") or "://" in value:
        return ""
    if any(char in value for char in ("\r", "\n", "\t", ";", ",", "\\", "?", "#")):
        return ""
    if any(char.isspace() for char in value):
        return ""
    return value.rstrip("/")


def app_url(path, base_path=""):
    path = path if path.startswith("/") else f"/{path}"
    return f"{normalize_base_path(base_path)}{path}"


def homeassistant_link(path, base_path=""):
    base_path = normalize_base_path(base_path) or get_homeassistant_ingress_base_path()
    return app_url(path, base_path) if base_path else app_url(path)


def safe_referer_path(value, base_path=""):
    if not value:
        return "/"
    parsed = urlparse(value)
    path = parsed.path or "/"
    base_path = normalize_base_path(base_path)
    if base_path and path.startswith(base_path):
        path = path.removeprefix(base_path) or "/"
    if path not in {"/", "/items", "/new", "/focus", "/calendar", "/reports", "/ha-setup", "/admin", "/todos", "/todo/new"} and not path.startswith(("/edit/", "/item/", "/todo/", "/todo/edit/")):
        return "/"
    return path


def tasks_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Maintenance ID",
            "Name",
            "Category",
            "Status",
            "Due phrase",
            "Next due",
            "Last completed",
            "Recurrence",
            "Home Assistant area",
            "Home Assistant area ID",
            "Asset",
            "Location",
            "Model number",
            "Serial number",
            "Filter size",
            "Purchase date",
            "Warranty expires",
            "Priority",
            "Season",
            "Tags",
            "Requires supplies",
            "Estimated minutes",
            "Notes",
        ]
    )
    for task in get_tasks():
        writer.writerow(
            [
                csv_cell(task["public_id"]),
                csv_cell(task["name"]),
                csv_cell(task["category"]),
                csv_cell(task["status_label"]),
                csv_cell(task["due_phrase"]),
                csv_cell(task["next_due_on"]),
                csv_cell(task["last_completed_on"] or "Never"),
                csv_cell(task["recurrence_phrase"]),
                csv_cell(task.get("ha_area_name", "")),
                csv_cell(task.get("ha_area_id", "")),
                csv_cell(task.get("asset_name", "")),
                csv_cell(task.get("location", "")),
                csv_cell(task.get("model_number", "")),
                csv_cell(task.get("serial_number", "")),
                csv_cell(task.get("filter_size", "")),
                csv_cell(task.get("purchase_date", "")),
                csv_cell(task.get("warranty_expires_on", "")),
                csv_cell(task.get("priority_label", "")),
                csv_cell(task.get("season_label", "")),
                csv_cell(task.get("tags", "")),
                csv_cell("Yes" if task.get("requires_supplies") else "No"),
                csv_cell(task.get("estimated_minutes", 0)),
                csv_cell(task["notes"]),
            ]
        )
    return output.getvalue()


def history_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Iteration ID", "Maintenance ID", "Task", "Closure type", "Closure notes", "Closed on", "Next due after closure", "Recorded at"])
    for item in get_all_history():
        writer.writerow(
            [
                csv_cell(item["public_id"]),
                csv_cell(item["task_public_id"]),
                csv_cell(item["task_name"]),
                csv_cell(item["closure_label"]),
                csv_cell(item["closure_notes"]),
                csv_cell(item["completed_on"]),
                csv_cell(item["next_due_on"]),
                csv_cell(item["created_at"]),
            ]
        )
    return output.getvalue()


def events_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Maintenance ID", "Task ID", "Task", "Event", "Details", "Recorded at"])
    for item in get_all_task_events():
        writer.writerow(
            [
                csv_cell(maintenance_public_id(item["task_id"])),
                csv_cell(item["task_id"]),
                csv_cell(item["task_name"]),
                csv_cell(event_type_label(item["event_type"])),
                csv_cell(summarize_event_data(item["event_data"])),
                csv_cell(item["created_at"]),
            ]
        )
    return output.getvalue()


def backup_health():
    expected_tables = {
        "tasks",
        "completion_history",
        "task_checklist_items",
        "task_events",
        "todo_projects",
        "todo_checklist_items",
        "ha_areas",
        "app_settings",
    }
    expected_task_columns = {
        "id",
        "name",
        "category",
        "notes",
        "ha_area_id",
        "ha_area_name",
        "asset_name",
        "location",
        "model_number",
        "serial_number",
        "filter_size",
        "purchase_date",
        "warranty_expires_on",
        "priority",
        "season",
        "tags",
        "requires_supplies",
        "estimated_minutes",
        "interval_count",
        "interval_unit",
        "next_due_on",
        "last_completed_on",
        "created_at",
        "updated_at",
    }
    errors = []
    counts = {}
    with connect_db() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        missing_tables = sorted(expected_tables - tables)
        if missing_tables:
            errors.append(f"Missing tables: {', '.join(missing_tables)}")
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()} if "tasks" in tables else set()
        missing_task_columns = sorted(expected_task_columns - task_columns)
        if missing_task_columns:
            errors.append(f"Missing task columns: {', '.join(missing_task_columns)}")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            errors.append(f"SQLite integrity check failed: {integrity}")
        for table in sorted(expected_tables & tables):
            counts[table] = int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
    return {
        "status": "ok" if not errors else "error",
        "version": APP_VERSION,
        "backup_scope": "/data/home-maintenance.db",
        "checked_at": utc_now_iso(),
        "errors": errors,
        "counts": counts,
    }


def addon_internal_url():
    slug = get_setting("addon_slug", "").strip()
    if slug:
        return f"http://{slug.replace('_', '-')}:8099"
    return "http://REPLACE_ADDON_HOSTNAME:8099"


def homeassistant_examples():
    base_url = addon_internal_url()
    rest_command_yaml = f"""
mxtracker_mark_done:
  url: "{base_url}/api/actions/mark_done"
  method: post
  content_type: "application/json"
  headers:
    X-MxTracker-Token: !secret mxtracker_api_action_token
  payload: '{{"task_id": {{{{ task_id | int }}}}}}'

mxtracker_snooze:
  url: "{base_url}/api/actions/snooze"
  method: post
  content_type: "application/json"
  headers:
    X-MxTracker-Token: !secret mxtracker_api_action_token
  payload: '{{"task_id": {{{{ task_id | int }}}}, "days": {{{{ days | default(7) | int }}}}}}'
""".strip()
    notification_yaml = """
alias: MxTracker actionable maintenance reminder
mode: restart
trigger:
  - platform: state
    entity_id: sensor.mxtracker_due_14_days
condition:
  - condition: numeric_state
    entity_id: sensor.mxtracker_due_14_days
    above: 0
action:
  - variables:
      item: "{{ state_attr('sensor.mxtracker_due_14_days', 'items')[0] }}"
      action_done: "{{ 'MX_DONE_' ~ context.id }}"
      action_snooze: "{{ 'MX_SNOOZE_' ~ context.id }}"
  - action: notify.mobile_app_YOUR_DEVICE
    data:
      title: "Maintenance due: {{ item.name }}"
      message: "{{ item.due_phrase }} - {{ item.category }} - Last done {{ item.last_done }}"
      data:
        url: "{{ item.detail_url }}"
        actions:
          - action: "{{ action_done }}"
            title: Done
          - action: "{{ action_snooze }}"
            title: Snooze 7d
          - action: URI
            title: Open details
            uri: "{{ item.detail_url }}"
  - wait_for_trigger:
      - platform: event
        event_type: mobile_app_notification_action
        event_data:
          action: "{{ action_done }}"
      - platform: event
        event_type: mobile_app_notification_action
        event_data:
          action: "{{ action_snooze }}"
    timeout: "00:30:00"
    continue_on_timeout: false
  - choose:
      - conditions: "{{ wait.trigger.event.data.action == action_done }}"
        sequence:
          - action: rest_command.mxtracker_mark_done
            data:
              task_id: "{{ item.id }}"
      - conditions: "{{ wait.trigger.event.data.action == action_snooze }}"
        sequence:
          - action: rest_command.mxtracker_snooze
            data:
              task_id: "{{ item.id }}"
              days: 7
""".strip()
    secrets_yaml = """
mxtracker_api_action_token: "PASTE_LONG_RANDOM_TOKEN_HERE"
""".strip()
    return {
        "addon_internal_url": base_url,
        "rest_command_yaml": rest_command_yaml,
        "notification_automation_yaml": notification_yaml,
        "secrets_yaml": secrets_yaml,
    }


def annual_report(year=None):
    current_year = date.today().year
    try:
        report_year = int(year or current_year)
    except (TypeError, ValueError):
        report_year = current_year
    if report_year < 2000 or report_year > 2100:
        report_year = current_year
    tasks = get_tasks()
    start = f"{report_year}-01-01"
    end = f"{report_year}-12-31"
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT task_id, task_name, completed_on, next_due_on, closure_type, closure_notes, created_at
            FROM completion_history
            WHERE completed_on BETWEEN ? AND ? AND closure_type = 'done'
            ORDER BY completed_on DESC, id DESC
            """,
            (start, end),
        ).fetchall()
    completions = [dict(row) for row in rows]
    by_month = {month: 0 for month in range(1, 13)}
    by_task = {}
    for item in completions:
        completed = parse_date(item["completed_on"])
        by_month[completed.month] += 1
        by_task[item["task_name"]] = by_task.get(item["task_name"], 0) + 1
    return {
        "year": report_year,
        "total_items": len(tasks),
        "overdue": sum(1 for task in tasks if task["status"] == "overdue"),
        "due_next_14": sum(
            1
            for task in tasks
            if task["status"] in {"overdue", "due_today"} or task["days_until"] <= HOMEASSISTANT_DASHBOARD_WINDOW_DAYS
        ),
        "never_completed": sum(1 for task in tasks if not task["last_completed_on"]),
        "requires_supplies": sum(1 for task in tasks if task.get("requires_supplies")),
        "completed": len(completions),
        "by_month": by_month,
        "top_completed": sorted(by_task.items(), key=lambda item: (-item[1], item[0].lower()))[:8],
        "recent": completions[:12],
    }


def handle_api_action(action, payload, base_path=""):
    if not isinstance(payload, dict):
        return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "JSON body must be an object."}
    try:
        task_id = int(payload.get("task_id", 0))
    except (TypeError, ValueError):
        task_id = 0
    if task_id < 1:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "task_id is required."}
    task = get_enriched_task(task_id)
    if not task:
        return HTTPStatus.NOT_FOUND, {"ok": False, "error": "Maintenance item not found."}
    if action == "mark_done":
        complete_task(task_id, "done", clean_closure_notes(payload.get("closure_notes", "")))
        updated = get_enriched_task(task_id)
        return HTTPStatus.OK, {"ok": True, "action": action, "task": public_task(updated)}
    if action == "snooze":
        try:
            days = int(payload.get("days", 7))
        except (TypeError, ValueError):
            days = 7
        if days < 1 or days > 365:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "days must be between 1 and 365."}
        snooze_task(task_id, days)
        updated = get_enriched_task(task_id)
        return HTTPStatus.OK, {"ok": True, "action": action, "task": public_task(updated)}
    if action == "open_detail":
        detail_path = homeassistant_detail_path(task_id)
        return HTTPStatus.OK, {
            "ok": True,
            "action": action,
            "task_id": task_id,
            "detail_url": homeassistant_link(detail_path, base_path),
        }
    return HTTPStatus.NOT_FOUND, {"ok": False, "error": "Unknown action."}


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
            refresh_homeassistant_app_info()
            refresh_homeassistant_areas()
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
    ha_area_id = fields.get("ha_area_id", "").strip()
    ha_area_name = ""
    interval_unit = fields.get("interval_unit", "").strip()
    next_due_on = fields.get("next_due_on", "").strip()
    asset_name = fields.get("asset_name", "").strip()
    location = fields.get("location", "").strip()
    model_number = fields.get("model_number", "").strip()
    serial_number = fields.get("serial_number", "").strip()
    filter_size = fields.get("filter_size", "").strip()
    purchase_date = fields.get("purchase_date", "").strip()
    warranty_expires_on = fields.get("warranty_expires_on", "").strip()
    priority = fields.get("priority", "normal").strip()
    season = fields.get("season", "").strip()
    tags = fields.get("tags", "").strip()
    requires_supplies = fields.get("requires_supplies") == "1"

    try:
        interval_count = int(fields.get("interval_count", ""))
    except ValueError:
        interval_count = 0
    try:
        estimated_minutes = int(fields.get("estimated_minutes", "0") or "0")
    except ValueError:
        estimated_minutes = -1

    if not name:
        errors.append("Name is required.")
    if len(name) > 120:
        errors.append("Name must be 120 characters or fewer.")
    if category not in CATEGORIES:
        errors.append("Choose a valid category.")
    if len(notes) > 1000:
        errors.append("Notes must be 1000 characters or fewer.")
    length_limits = {
        "Asset name": (asset_name, 120),
        "Location": (location, 120),
        "Model number": (model_number, 120),
        "Serial number": (serial_number, 120),
        "Filter size": (filter_size, 80),
        "Tags": (tags, 200),
    }
    for label, (value, limit) in length_limits.items():
        if len(value) > limit:
            errors.append(f"{label} must be {limit} characters or fewer.")
    if priority not in PRIORITIES:
        errors.append("Choose a valid priority.")
    if season not in SEASONS:
        errors.append("Choose a valid season.")
    for field_label, raw_date in [("Purchase date", purchase_date), ("Warranty date", warranty_expires_on)]:
        if raw_date:
            try:
                parse_date(raw_date)
            except ValueError:
                errors.append(f"{field_label} must be valid.")
    if estimated_minutes < 0 or estimated_minutes > 10080:
        errors.append("Estimated minutes must be between 0 and 10080.")
    if ha_area_id:
        area_lookup = homeassistant_area_lookup()
        if not valid_ha_area_id(ha_area_id):
            errors.append("Choose a valid Home Assistant area.")
        elif area_lookup:
            ha_area_name = area_lookup.get(ha_area_id, "")
            if not ha_area_name:
                errors.append("Choose a Home Assistant area from the synced list.")
        else:
            ha_area_name = fields.get("ha_area_name", "").strip() or ha_area_id
            if not valid_ha_area_name(ha_area_name):
                errors.append("Choose a valid Home Assistant area.")
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
        "ha_area_id": ha_area_id,
        "ha_area_name": ha_area_name,
        "asset_name": asset_name,
        "location": location,
        "model_number": model_number,
        "serial_number": serial_number,
        "filter_size": filter_size,
        "purchase_date": purchase_date,
        "warranty_expires_on": warranty_expires_on,
        "priority": priority,
        "season": season,
        "tags": tags,
        "requires_supplies": 1 if requires_supplies else 0,
        "estimated_minutes": estimated_minutes,
        "interval_count": interval_count,
        "interval_unit": interval_unit,
        "next_due_on": next_due_on,
    }


def save_task(fields, task_id=None):
    now = utc_now_iso()
    previous = get_task(task_id) if task_id is not None else None
    with connect_db() as conn:
        if task_id is None:
            cursor = conn.execute(
                """
                INSERT INTO tasks
                    (name, category, notes, ha_area_id, ha_area_name, asset_name, location, model_number,
                     serial_number, filter_size, purchase_date, warranty_expires_on, priority, season, tags,
                     requires_supplies, estimated_minutes, interval_count, interval_unit, next_due_on, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fields["name"],
                    fields["category"],
                    fields["notes"],
                    fields.get("ha_area_id", ""),
                    fields.get("ha_area_name", ""),
                    fields.get("asset_name", ""),
                    fields.get("location", ""),
                    fields.get("model_number", ""),
                    fields.get("serial_number", ""),
                    fields.get("filter_size", ""),
                    fields.get("purchase_date", ""),
                    fields.get("warranty_expires_on", ""),
                    fields.get("priority", "normal"),
                    fields.get("season", ""),
                    fields.get("tags", ""),
                    fields.get("requires_supplies", 0),
                    fields.get("estimated_minutes", 0),
                    fields["interval_count"],
                    fields["interval_unit"],
                    fields["next_due_on"],
                    now,
                    now,
                ),
            )
            task_id = cursor.lastrowid
        else:
            conn.execute(
                """
                UPDATE tasks
                SET name = ?, category = ?, notes = ?, ha_area_id = ?, ha_area_name = ?,
                    asset_name = ?, location = ?, model_number = ?, serial_number = ?, filter_size = ?,
                    purchase_date = ?, warranty_expires_on = ?, priority = ?, season = ?, tags = ?,
                    requires_supplies = ?, estimated_minutes = ?,
                    interval_count = ?, interval_unit = ?, next_due_on = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    fields["name"],
                    fields["category"],
                    fields["notes"],
                    fields.get("ha_area_id", ""),
                    fields.get("ha_area_name", ""),
                    fields.get("asset_name", ""),
                    fields.get("location", ""),
                    fields.get("model_number", ""),
                    fields.get("serial_number", ""),
                    fields.get("filter_size", ""),
                    fields.get("purchase_date", ""),
                    fields.get("warranty_expires_on", ""),
                    fields.get("priority", "normal"),
                    fields.get("season", ""),
                    fields.get("tags", ""),
                    fields.get("requires_supplies", 0),
                    fields.get("estimated_minutes", 0),
                    fields["interval_count"],
                    fields["interval_unit"],
                    fields["next_due_on"],
                    now,
                    task_id,
                ),
            )
    request_homeassistant_sync()
    event_name = fields["name"]
    if previous is None:
        record_task_event(task_id, event_name, "created", {"next_due_on": fields["next_due_on"]})
    else:
        changed = [
            key
            for key in [
                "name",
                "category",
                "notes",
                "ha_area_id",
                "ha_area_name",
                "asset_name",
                "location",
                "model_number",
                "serial_number",
                "filter_size",
                "purchase_date",
                "warranty_expires_on",
                "priority",
                "season",
                "tags",
                "requires_supplies",
                "estimated_minutes",
                "interval_count",
                "interval_unit",
                "next_due_on",
            ]
            if str(previous.get(key, "")) != str(fields.get(key, ""))
        ]
        record_task_event(task_id, event_name, "updated", {"changed": changed})
    return task_id


def latest_done_date(history_rows):
    for item in history_rows:
        if item["closure_type"] == "done":
            return item["completed_on"]
    return None


def complete_task(task_id, closure_type="done", closure_notes=""):
    task = get_task(task_id)
    if not task:
        return False
    closure_type = clean_closure_type(closure_type)
    closure_notes = clean_closure_notes(closure_notes)
    completed_on = today_iso()
    next_due = calculate_next_due(
        parse_date(completed_on),
        task["interval_count"],
        task["interval_unit"],
    ).isoformat()
    last_completed_on = completed_on if closure_type == "done" else task["last_completed_on"]
    now = utc_now_iso()
    with connect_db() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET last_completed_on = ?, next_due_on = ?, updated_at = ?
            WHERE id = ?
            """,
            (last_completed_on, next_due, now, task_id),
        )
        conn.execute(
            """
            INSERT INTO completion_history
                (task_id, task_name, completed_on, next_due_on, previous_next_due_on, closure_type, closure_notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, task["name"], completed_on, next_due, task["next_due_on"], closure_type, closure_notes, now),
        )
        if closure_type == "done":
            conn.execute(
                "UPDATE task_checklist_items SET is_done = 0, updated_at = ? WHERE task_id = ?",
                (now, task_id),
            )
    request_homeassistant_sync()
    record_task_event(
        task_id,
        task["name"],
        "completed",
        {
            "closure_type": closure_type,
            "closure_notes": closure_notes,
            "completed_on": completed_on,
            "next_due_on": next_due,
            "previous_next_due_on": task["next_due_on"],
        },
    )
    return True


def snooze_task(task_id, days=7):
    task = get_task(task_id)
    if not task:
        return False
    next_due = (date.today() + timedelta(days=days)).isoformat()
    previous_due = task["next_due_on"]
    with connect_db() as conn:
        conn.execute(
            "UPDATE tasks SET next_due_on = ?, updated_at = ? WHERE id = ?",
            (next_due, utc_now_iso(), task_id),
        )
    request_homeassistant_sync()
    record_task_event(task_id, task["name"], "snoozed", {"from": previous_due, "to": next_due, "days": days})
    return True


def delete_task(task_id):
    task = get_task(task_id)
    with connect_db() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    request_homeassistant_sync()
    if task:
        record_task_event(task_id, task["name"], "deleted", {"name": task["name"]})


def delete_completion_history_item(history_id):
    item = get_completion_history_item(history_id)
    if not item:
        return None
    task = get_task(item["task_id"])
    if not task:
        return None
    with connect_db() as conn:
        conn.execute("DELETE FROM completion_history WHERE id = ?", (history_id,))
    recalculate_task_from_history(item["task_id"], item.get("previous_next_due_on") or today_iso())
    request_homeassistant_sync()
    record_task_event(
        item["task_id"],
        task["name"],
        "completion_removed",
        {
            "completion_id": item["public_id"],
            "completed_on": item["completed_on"],
            "next_due_on": get_task(item["task_id"])["next_due_on"],
        },
    )
    return item


def recalculate_task_from_history(task_id, fallback_next_due=None):
    task = get_task(task_id)
    if not task:
        return False
    history = completion_history_rows(task_id=task_id)
    if history:
        last_completed = latest_done_date(history)
        next_due = history[0]["next_due_on"]
    else:
        last_completed = None
        next_due = fallback_next_due or task["next_due_on"] or today_iso()
    with connect_db() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET last_completed_on = ?, next_due_on = ?, updated_at = ?
            WHERE id = ?
            """,
            (last_completed, next_due, utc_now_iso(), task_id),
        )
    return True


def update_completion_history_item(history_id, fields):
    item = get_completion_history_item(history_id)
    if not item:
        return ["Completion record not found."], None
    completed_on = fields.get("completed_on", "").strip()
    next_due_on = fields.get("next_due_on", "").strip()
    errors = []
    for label, value in (("Closed date", completed_on), ("Next due date", next_due_on)):
        try:
            parse_date(value)
        except ValueError:
            errors.append(f"{label} must be valid.")
    cleaned = {
        "completed_on": completed_on,
        "next_due_on": next_due_on,
        "closure_type": clean_closure_type(fields.get("closure_type", "done")),
        "closure_notes": clean_closure_notes(fields.get("closure_notes", "")),
    }
    if errors:
        return errors, None
    with connect_db() as conn:
        conn.execute(
            """
            UPDATE completion_history
            SET completed_on = ?, next_due_on = ?, closure_type = ?, closure_notes = ?
            WHERE id = ?
            """,
            (cleaned["completed_on"], cleaned["next_due_on"], cleaned["closure_type"], cleaned["closure_notes"], history_id),
        )
    recalculate_task_from_history(item["task_id"], item.get("previous_next_due_on") or today_iso())
    request_homeassistant_sync()
    record_task_event(
        item["task_id"],
        item["task_name"],
        "completion_edited",
        {
            "completion_id": item["public_id"],
            "completed_on": cleaned["completed_on"],
            "next_due_on": cleaned["next_due_on"],
            "closure_type": cleaned["closure_type"],
            "closure_notes": cleaned["closure_notes"],
        },
    )
    return [], get_completion_history_item(history_id)


def reopen_task(task_id):
    task = get_task(task_id)
    if not task:
        return None
    now = utc_now_iso()
    due_on = today_iso()
    with connect_db() as conn:
        conn.execute("UPDATE tasks SET next_due_on = ?, updated_at = ? WHERE id = ?", (due_on, now, task_id))
        conn.execute("UPDATE task_checklist_items SET is_done = 0, updated_at = ? WHERE task_id = ?", (now, task_id))
    request_homeassistant_sync()
    record_task_event(task_id, task["name"], "reopened", {"previous_next_due_on": task["next_due_on"], "next_due_on": due_on})
    return get_task(task_id)


def reopen_todo(project_id):
    todo = get_todo(project_id)
    if not todo:
        return None
    now = utc_now_iso()
    with connect_db() as conn:
        conn.execute("UPDATE todo_projects SET status = 'backlog', updated_at = ? WHERE id = ?", (now, project_id))
        conn.execute("UPDATE todo_checklist_items SET is_done = 0, updated_at = ? WHERE project_id = ?", (now, project_id))
    request_homeassistant_sync()
    return get_enriched_todo(project_id)


def trash_icon():
    return """
      <svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M9 3h6l1 2h4v2H4V5h4l1-2Zm1 7h2v8h-2v-8Zm4 0h2v8h-2v-8ZM7 8h10l-1 13H8L7 8Z"></path>
      </svg>
    """


def delete_confirmed(fields):
    return fields.get("confirm_text", "").strip() == "DELETE"


def admin_unlock_confirmed(fields):
    return fields.get("confirm_text", "").strip() == "ADMIN"


def history_delete_confirmed(fields, history_item):
    return history_item and fields.get("confirm_text", "").strip() == f"REMOVE {history_item['public_id']}"


def task_reopen_confirmed(fields, task):
    return task and fields.get("confirm_text", "").strip() == f"REOPEN {maintenance_public_id(task['id'])}"


def todo_reopen_confirmed(fields, todo):
    return todo and fields.get("confirm_text", "").strip() == f"REOPEN {todo_public_id(todo['id'])}"


def clean_closure_type(value):
    value = (value or "done").strip()
    return value if value in CLOSURE_TYPES else "done"


def clean_closure_notes(value):
    return (value or "").strip()[:1000]


def create_admin_session():
    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + ADMIN_SESSION_SECONDS
    with ADMIN_SESSIONS_LOCK:
        ADMIN_SESSIONS[token] = expires_at
    return token, expires_at


def admin_session_active(token):
    if not token or not valid_csrf_token_value(token):
        return False
    now = int(time.time())
    with ADMIN_SESSIONS_LOCK:
        expired = [key for key, expires_at in ADMIN_SESSIONS.items() if expires_at <= now]
        for key in expired:
            ADMIN_SESSIONS.pop(key, None)
        return ADMIN_SESSIONS.get(token, 0) > now


def clear_admin_session(token):
    if token:
        with ADMIN_SESSIONS_LOCK:
            ADMIN_SESSIONS.pop(token, None)


def render_layout(title, body, csrf_token, notice="", theme="system", base_path="", active_view=""):
    theme = theme if theme in THEMES else "system"
    base_path = normalize_base_path(base_path)
    theme_links = []
    for mode in ["system", "light", "dark"]:
        active = " active" if theme == mode else ""
        theme_links.append(f'<a class="theme-link{active}" href="{app_url(f"/theme/{mode}", base_path)}">{mode.title()}</a>')
    nav_groups = [
        (
            "Maintenance",
            [
                ("dashboard", "Dashboard", "/"),
                ("soon", "Soon", "/focus"),
                ("audit", "Audit", "/items"),
                ("calendar", "Calendar", "/calendar"),
                ("reports", "Reports", "/reports"),
                ("new", "Add maintenance", "/new"),
            ],
        ),
        ("Projects", [("todos", "House todos", "/todos")]),
        ("System", [("ha_setup", "HA setup", "/ha-setup"), ("admin", "Admin", "/admin")]),
    ]
    nav_groups_html = []
    for group_label, items in nav_groups:
        nav_links = []
        for key, label, path in items:
            active = key == active_view
            classes = "button nav-link" if key == "new" else "button secondary nav-link"
            if active:
                classes += " active"
            aria_current = ' aria-current="page"' if active else ""
            nav_links.append(f'<a class="{classes}" href="{app_url(path, base_path)}"{aria_current}>{label}</a>')
        nav_groups_html.append(
            f"""
            <div class="nav-group">
              <span class="nav-label">{escape(group_label)}</span>
              <div class="nav-links">{''.join(nav_links)}</div>
            </div>
            """
        )
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
    pre {{
      overflow-x: auto;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-strong);
      color: var(--text);
      font-size: .9rem;
      line-height: 1.45;
    }}
    .icon {{
      width: 18px;
      height: 18px;
      display: inline-block;
      fill: currentColor;
      flex: 0 0 auto;
    }}
    .wrap {{ width: min(1120px, calc(100% - 32px)); margin: 0 auto; }}
    header {{
      border-bottom: 1px solid var(--line);
      background: var(--surface);
    }}
    .topbar {{
      min-height: 76px;
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 0;
    }}
    h1 {{ margin: 0; font-size: clamp(1.5rem, 2vw, 2.15rem); letter-spacing: 0; }}
    h2 {{ margin: 0 0 14px; font-size: 1.1rem; letter-spacing: 0; }}
    main {{ padding: 24px 0 42px; }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    .primary-nav {{
      display: flex;
      align-items: flex-start;
      justify-content: flex-end;
      gap: 14px;
      flex-wrap: wrap;
    }}
    .nav-group {{
      display: grid;
      gap: 6px;
      min-width: 0;
    }}
    .nav-label {{
      color: var(--muted);
      font-size: .72rem;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: .06em;
    }}
    .nav-links {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }}
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
    .button.nav-link.active {{
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
      box-shadow: var(--shadow);
    }}
    .button.danger, button.danger {{
      border-color: var(--danger);
      background: var(--danger);
      color: #fff;
    }}
    .button.confirm-danger, button.confirm-danger {{
      min-height: 46px;
      font-weight: 900;
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
    .id-badge {{
      display: inline-flex;
      align-items: center;
      width: fit-content;
      min-height: 24px;
      padding: 2px 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--surface-strong);
      color: var(--muted);
      font-size: .76rem;
      font-weight: 900;
      letter-spacing: .03em;
      white-space: nowrap;
    }}
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
    label input[type="checkbox"] {{
      width: auto;
      min-height: auto;
      margin-right: 8px;
    }}
    details.advanced-panel {{
      margin-top: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-strong);
      padding: 0;
      overflow: hidden;
    }}
    details.advanced-panel summary {{
      cursor: pointer;
      padding: 12px 14px;
      font-weight: 900;
    }}
    .advanced-content {{
      padding: 0 14px 14px;
      border-top: 1px solid var(--line);
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
    .calendar-toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 16px;
    }}
    .calendar-toolbar h2 {{
      margin: 0;
    }}
    .calendar-grid {{
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 1px;
      border: 1px solid var(--line);
      background: var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    .calendar-weekday, .calendar-day {{
      background: var(--surface);
      min-width: 0;
    }}
    .calendar-weekday {{
      padding: 8px;
      color: var(--muted);
      font-size: .78rem;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: .04em;
    }}
    .calendar-day {{
      min-height: 132px;
      padding: 8px;
    }}
    .calendar-day.outside {{
      background: var(--surface-strong);
      color: var(--muted);
    }}
    .calendar-day.today {{
      box-shadow: inset 0 0 0 2px var(--accent);
    }}
    .calendar-date {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
      font-weight: 900;
    }}
    .calendar-items {{
      display: grid;
      gap: 6px;
    }}
    .calendar-task {{
      display: block;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 7px;
      background: var(--surface-strong);
      color: var(--text);
      text-decoration: none;
      overflow-wrap: anywhere;
    }}
    .calendar-task strong {{
      display: block;
      font-size: .88rem;
      line-height: 1.2;
    }}
    .calendar-task.overdue {{
      border-color: var(--danger);
      background: var(--danger-bg);
    }}
    .calendar-task.overdue strong {{
      color: var(--danger);
    }}
    .calendar-task .meta {{
      font-size: .76rem;
    }}
    .detail-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: 16px 0;
    }}
    .detail-section {{
      margin-top: 18px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
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
    .table-scroll {{
      width: 100%;
      overflow-x: auto;
    }}
    .audit-table {{
      min-width: 1040px;
      table-layout: auto;
    }}
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
    .filter-panel {{
      margin-bottom: 18px;
    }}
    .filter-grid {{
      display: grid;
      grid-template-columns: minmax(190px, 1.4fr) repeat(4, minmax(120px, 1fr));
      gap: 12px;
      align-items: end;
    }}
    .filter-grid label {{
      margin-top: 0;
    }}
    .filter-actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .result-count {{
      margin-top: 12px;
      color: var(--muted);
      font-weight: 800;
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
    .task-table .col-name {{ width: 32%; }}
    .task-table .col-id {{ width: 72px; }}
    .task-table .col-category {{ width: 13%; }}
    .task-table .col-due {{ width: 19%; }}
    .task-table .col-repeat {{ width: 16%; }}
    .task-table .col-actions {{ width: 20%; }}
    .audit-table .col-id {{ width: 88px; }}
    .audit-table .col-name {{ width: 230px; }}
    .audit-table .col-category {{ width: 120px; }}
    .audit-table .col-area {{ width: 130px; }}
    .audit-table .col-due {{ width: 150px; }}
    .audit-table .col-date {{ width: 120px; }}
    .audit-table .col-repeat {{ width: 130px; }}
    .audit-table .col-actions {{ width: 170px; }}
    .quick-actions {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; }}
    .quick-actions form {{ min-width: 0; }}
    .quick-actions .button, .quick-actions button {{ min-height: 34px; padding: 6px 9px; font-size: .9rem; }}
    .quick-actions .button, .quick-actions button {{ width: 100%; }}
    .todo-toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 18px;
    }}
    .todo-board {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 340px;
      gap: 18px;
      align-items: start;
    }}
    .todo-card-grid {{
      display: grid;
      gap: 12px;
    }}
    .todo-card {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      padding: 14px;
      border: 1px solid var(--line);
      border-left: 5px solid var(--accent);
      border-radius: 8px;
      background: var(--surface);
      color: var(--text);
      text-decoration: none;
    }}
    .todo-card.critical {{ border-left-color: var(--danger); }}
    .todo-card.high {{ border-left-color: var(--warn); }}
    .todo-card.medium {{ border-left-color: var(--accent); }}
    .todo-card.low {{ border-left-color: var(--ok); }}
    .todo-title {{
      display: block;
      margin-bottom: 4px;
      font-size: 1.02rem;
      overflow-wrap: anywhere;
    }}
    .score-pill {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 52px;
      min-height: 52px;
      border-radius: 8px;
      background: var(--surface-strong);
      border: 1px solid var(--line);
      font-weight: 900;
      color: var(--accent-strong);
    }}
    .progress-bar {{
      height: 8px;
      border-radius: 999px;
      background: var(--surface-strong);
      border: 1px solid var(--line);
      overflow: hidden;
      margin-top: 10px;
    }}
    .progress-fill {{
      height: 100%;
      background: var(--accent);
    }}
    .risk-matrix {{
      display: grid;
      grid-template-columns: 48px repeat(5, minmax(0, 1fr));
      gap: 3px;
      align-items: stretch;
    }}
    .matrix-label {{
      min-height: 42px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--muted);
      font-size: .78rem;
      font-weight: 900;
    }}
    .matrix-cell {{
      min-height: 72px;
      padding: 6px;
      border-radius: 7px;
      border: 1px solid var(--line);
      background: var(--surface-strong);
    }}
    .matrix-cell.critical {{ background: var(--danger-bg); border-color: var(--danger); }}
    .matrix-cell.high {{ border-color: var(--warn); }}
    .matrix-cell.medium {{ border-color: var(--accent); }}
    .matrix-cell.low {{ border-color: var(--ok); }}
    .matrix-dot {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 26px;
      height: 26px;
      margin: 2px;
      border-radius: 999px;
      border: 2px solid var(--surface);
      background: var(--text);
      color: var(--surface);
      font-size: .72rem;
      font-weight: 900;
      text-decoration: none;
    }}
    .matrix-dot.cost-1, .matrix-dot.cost-2 {{ width: 22px; height: 22px; }}
    .matrix-dot.cost-4, .matrix-dot.cost-5 {{ width: 32px; height: 32px; }}
    .checklist {{
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }}
    .check-item {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto;
      gap: 10px;
      align-items: start;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
    }}
    .check-item.child {{
      margin-left: 24px;
      background: var(--surface-strong);
    }}
    .check-item.done strong {{
      text-decoration: line-through;
      color: var(--muted);
    }}
    .check-item input[type="checkbox"] {{
      width: 22px;
      min-height: 22px;
      margin-top: 1px;
    }}
    .mini-form {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      gap: 8px;
      align-items: end;
      margin-top: 12px;
    }}
    .scale-grid {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
    }}
    .report-bars {{
      display: grid;
      gap: 8px;
    }}
    .report-bar {{
      display: grid;
      grid-template-columns: 42px minmax(0, 1fr) 42px;
      gap: 10px;
      align-items: center;
    }}
    .bar-track {{
      height: 12px;
      border: 1px solid var(--line);
      border-radius: 999px;
      overflow: hidden;
      background: var(--surface-strong);
    }}
    .bar-fill {{
      height: 100%;
      background: var(--accent);
    }}
    .scale-grid label {{
      margin-top: 0;
    }}
    footer {{ color: var(--muted); font-size: .88rem; margin-top: 22px; }}
    @media (max-width: 820px) {{
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .layout {{ grid-template-columns: 1fr; }}
      .topbar {{ align-items: flex-start; flex-direction: column; padding: 16px 0; }}
      .primary-nav {{ justify-content: flex-start; width: 100%; }}
      .nav-group {{ width: 100%; }}
      .wrap {{ width: min(100% - 22px, 1120px); }}
      .form-row {{ grid-template-columns: 1fr; }}
      .task-head {{ flex-direction: column; }}
      nav .button {{ width: auto; }}
      .theme-switch {{ width: 100%; }}
      .theme-link {{ flex: 1; text-align: center; }}
      .table-head {{ flex-direction: column; }}
      .detail-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .todo-board {{ grid-template-columns: 1fr; }}
      .todo-toolbar {{ align-items: stretch; flex-direction: column; }}
      .todo-toolbar .actions, .todo-toolbar .button {{ width: 100%; }}
      .filter-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .scale-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .task-table, .task-table tbody, .task-table tr, .task-table td {{ display: block; width: 100%; max-width: 100%; }}
      .task-table thead {{ display: none; }}
      .task-table .col-name,
      .task-table .col-id,
      .task-table .col-category,
      .task-table .col-due,
      .task-table .col-repeat,
      .task-table .col-actions {{ width: 100%; }}
      .task-table .col-area,
      .task-table .col-date {{ width: 100%; }}
      .audit-table {{ min-width: 0; }}
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
      .audit-table td.col-category {{
        display: block;
        width: 100%;
        padding: 0;
        border: 0;
        border-radius: 0;
        background: transparent;
        font-weight: inherit;
      }}
      .audit-table td.col-category::before {{
        content: attr(data-label);
        display: block;
      }}
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
      .nav-links .button {{ flex: 1 1 calc(50% - 8px); }}
      .audit-actions .button {{ width: 100%; }}
      .quick-actions {{ grid-template-columns: 1fr; }}
      .focus-item {{ grid-template-columns: 1fr; }}
      .todo-card {{ grid-template-columns: 1fr; }}
      .mini-form {{ grid-template-columns: 1fr; }}
      .filter-grid {{ grid-template-columns: 1fr; }}
      .filter-actions .button, .filter-actions button {{ width: 100%; }}
      .check-item.child {{ margin-left: 12px; }}
      .risk-matrix {{ grid-template-columns: 34px repeat(5, minmax(46px, 1fr)); overflow-x: auto; }}
      .matrix-cell {{ min-height: 58px; }}
      .detail-grid {{ grid-template-columns: 1fr; }}
      .calendar-toolbar {{ align-items: stretch; flex-direction: column; }}
      .calendar-toolbar .actions {{ width: 100%; }}
      .calendar-toolbar .button {{ flex: 1; }}
      .calendar-grid {{
        display: block;
        border: 0;
        background: transparent;
        border-radius: 0;
      }}
      .calendar-weekday {{ display: none; }}
      .calendar-day {{
        min-height: auto;
        border: 1px solid var(--line);
        border-radius: 8px;
        margin-bottom: 10px;
      }}
      .calendar-day.outside {{
        display: none;
      }}
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
      <nav class="primary-nav" aria-label="Primary navigation">
        {''.join(nav_groups_html)}
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
            area = f'<div class="meta">Area {escape(task["ha_area_name"])}</div>' if task.get("ha_area_name") else ""
            complete_url = app_url(f"/complete/{task_id}", base_path)
            snooze_url = app_url(f"/snooze/{task_id}", base_path)
            edit_url = app_url(f"/edit/{task_id}", base_path)
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
                    {id_badge(task["public_id"])}
                    <strong><a href="{app_url(f"/item/{task_id}", base_path)}">{escape(task["name"])}</a></strong>
                    {notes}
                    <div class="meta">Last done {last_done}</div>
                  </td>
                  <td data-label="Category" class="col-category">{escape(task["category"])}</td>
                  <td data-label="Due" class="col-due">
                    <span class="badge {task["status"]}">{escape(task["due_phrase"])}</span>
                    <div class="meta">{escape(task["next_due_on"])}</div>
                    {area}
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
              {id_badge(item["public_id"])}
              <strong>{escape(item["task_name"])}</strong>
              <div>Completed {escape(item["completed_on"])} - Next due {escape(item["next_due_on"])}</div>
            </div>
            """
        )
    if not history_items:
        history_items.append('<div class="empty">No completed tasks yet.</div>')

    body = f"""
      <div class="audit-actions">
        <a class="button" href="{app_url("/focus", base_path)}">Due next 14 days</a>
        <a class="button secondary" href="{app_url("/calendar", base_path)}">Open calendar</a>
      </div>
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
          {render_task_table("Overdue", "Needs attention first" if overdue_tasks else "Nothing overdue", overdue_tasks, "overdue-section" if overdue_tasks else "")}
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
    return render_layout("Dashboard", body, csrf_token, notice, theme, base_path, active_view="dashboard")


def render_root_page(query, csrf_token, notice="", theme="system", base_path=""):
    mx_item = query.get("mx_item", [""])[0]
    if mx_item.isdigit():
        task = get_enriched_task(int(mx_item))
        if task:
            return render_item_detail(task, csrf_token, notice=notice, theme=theme, base_path=base_path)
    if query.get("mx_view", [""])[0] == "focus":
        return render_focus_view(csrf_token, notice=notice, theme=theme, base_path=base_path)
    return render_dashboard(csrf_token, notice=notice, theme=theme, base_path=base_path)


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
            area_meta = f'<span class="meta">{escape(task["ha_area_name"])}</span>' if task.get("ha_area_name") else ""
            items.append(
                f"""
                <a class="focus-item{status_class}" href="{app_url(f"/item/{task["id"]}", base_path)}">
                  <div>
                    {id_badge(task["public_id"])}
                    <strong>{escape(task["name"])}</strong>
                    <div class="focus-meta">
                      <span class="badge {task["status"]}">{escape(task["due_phrase"])}</span>
                      <span class="meta">{escape(task["category"])}</span>
                      {area_meta}
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
    return render_layout("Maintenance Due Soon", body, csrf_token, notice, theme, base_path, active_view="soon")


def render_todos_view(csrf_token, query=None, notice="", theme="system", base_path=""):
    todos = get_todos()
    visible_todos, filters = filter_todos(todos, query or {})
    active_todos = [todo for todo in todos if todo["derived_status"] != "done"]
    summary = {
        "active": len(active_todos),
        "ready": sum(1 for todo in active_todos if todo["derived_status"] == "ready"),
        "in_work": sum(1 for todo in active_todos if todo["derived_status"] == "in_work"),
        "blocked": sum(1 for todo in active_todos if todo["derived_status"] == "blocked"),
    }
    cards = []
    for todo in visible_todos:
        area = f'<span class="meta">{escape(todo["ha_area_name"])}</span>' if todo.get("ha_area_name") else ""
        target = f'<span class="meta">Target {escape(todo["target_on"])}</span>' if todo.get("target_on") else ""
        cards.append(
            f"""
            <a class="todo-card {todo["risk_band"]}" href="{app_url(f"/todo/{todo["id"]}", base_path)}">
              <div>
                {id_badge(todo["public_id"])}
                <strong class="todo-title">{escape(todo["title"])}</strong>
                <div class="focus-meta">
                  <span class="badge {todo["derived_status"]}">{escape(todo["status_label"])}</span>
                  <span class="meta">{escape(todo["category"])}</span>
                  <span class="meta">{escape(todo["action_lane"])}</span>
                  {area}
                  {target}
                </div>
                <div class="progress-bar" aria-label="Progress {todo["progress_percent"]}%">
                  <div class="progress-fill" style="width: {todo["progress_percent"]}%"></div>
                </div>
                <div class="meta">{todo["progress_percent"]}% done - Risk {todo["hazard_score"]}/25 - Effort {todo["effort"]}/5 - Cost {todo["cost"]}/5</div>
              </div>
              <span class="score-pill">{escape(todo["priority_score"])}</span>
            </a>
            """
        )
    if not cards:
        cards.append('<div class="empty">No house todos match these filters.</div>')

    category_options = ['<option value="">All categories</option>']
    for category in TODO_CATEGORIES:
        category_options.append(f'<option value="{escape(category)}"{selected_attr(filters["category"], category)}>{escape(category)}</option>')
    status_options = [
        ("active", "Active"),
        ("all", "All statuses"),
        ("ready", "Ready"),
        ("in_work", "In work"),
        ("blocked", "Blocked"),
        ("planning", "Planning"),
        ("backlog", "Backlog"),
        ("done", "Done"),
    ]
    status_options_html = "".join(
        f'<option value="{status}"{selected_attr(filters["status"], status)}>{escape(label)}</option>'
        for status, label in status_options
    )
    risk_options = ['<option value="">All risk</option>']
    for risk, label in [("critical", "Critical"), ("high", "High"), ("medium", "Medium"), ("low", "Low")]:
        risk_options.append(f'<option value="{risk}"{selected_attr(filters["risk"], risk)}>{label}</option>')
    area_options = [f'<option value=""{selected_attr(filters["area"], "")}>All areas</option>']
    area_options.append(f'<option value="unassigned"{selected_attr(filters["area"], "unassigned")}>Unassigned</option>')
    for area in get_homeassistant_areas():
        area_options.append(f'<option value="{escape(area["area_id"])}"{selected_attr(filters["area"], area["area_id"])}>{escape(area["name"])}</option>')

    matrix_cells = ['<div class="matrix-label"></div>']
    for consequence in range(1, 6):
        matrix_cells.append(f'<div class="matrix-label">C{consequence}</div>')
    todos_by_cell = {}
    for todo in visible_todos:
        todos_by_cell.setdefault((int(todo["likelihood"]), int(todo["consequence"])), []).append(todo)
    for likelihood in range(5, 0, -1):
        matrix_cells.append(f'<div class="matrix-label">L{likelihood}</div>')
        for consequence in range(1, 6):
            score = likelihood * consequence
            band = todo_risk_band(score)
            dots = []
            for todo in sorted(todos_by_cell.get((likelihood, consequence), []), key=lambda item: -item["priority_score"]):
                dots.append(
                    f'<a class="matrix-dot cost-{todo["cost"]}" href="{app_url(f"/todo/{todo["id"]}", base_path)}" title="{escape(todo["public_id"])} {escape(todo["title"])}">{escape(todo["id"])}</a>'
                )
            matrix_cells.append(f'<div class="matrix-cell {band}">{"".join(dots)}</div>')

    body = f"""
      <div class="todo-toolbar">
        <div>
          <h2>House Todos</h2>
          <div class="meta">One-off repairs ranked by risk, consequence, readiness, effort, and cost.</div>
        </div>
        <div class="actions">
          <a class="button" href="{app_url("/todo/new", base_path)}">Add house todo</a>
        </div>
      </div>
      <section class="grid" aria-label="House todo summary">
        <div class="stat"><strong>{summary["active"]}</strong><span>Active</span></div>
        <div class="stat"><strong>{summary["ready"]}</strong><span>Ready to start</span></div>
        <div class="stat"><strong>{summary["in_work"]}</strong><span>In work</span></div>
        <div class="stat"><strong>{summary["blocked"]}</strong><span>Blocked</span></div>
      </section>
      <section class="panel filter-panel">
        <form action="{app_url("/todos", base_path)}" method="get">
          <div class="filter-grid">
            <div>
              <label for="todo-q">Search</label>
              <input id="todo-q" name="q" value="{escape(filters["q"])}" placeholder="ID, title, detail, area, lane">
            </div>
            <div>
              <label for="todo-category">Category</label>
              <select id="todo-category" name="category">{''.join(category_options)}</select>
            </div>
            <div>
              <label for="todo-status">Status</label>
              <select id="todo-status" name="status">{status_options_html}</select>
            </div>
            <div>
              <label for="todo-risk">Risk</label>
              <select id="todo-risk" name="risk">{''.join(risk_options)}</select>
            </div>
            <div>
              <label for="todo-area">Area</label>
              <select id="todo-area" name="area">{''.join(area_options)}</select>
            </div>
          </div>
          <div class="filter-actions form-actions">
            <button type="submit">Apply filters</button>
            <a class="button secondary" href="{app_url("/todos", base_path)}">Clear</a>
          </div>
          <div class="result-count">{len(visible_todos)} of {len(todos)} house todos shown</div>
        </form>
      </section>
      <section class="todo-board">
        <div class="todo-card-grid">{''.join(cards)}</div>
        <aside class="panel">
          <h2>Risk Map</h2>
          <div class="meta">Likelihood rises upward; consequence rises right. Larger dots usually mean higher cost.</div>
          <div class="risk-matrix" aria-label="Likelihood by consequence risk map">
            {''.join(matrix_cells)}
          </div>
        </aside>
      </section>
    """
    return render_layout("House Todos", body, csrf_token, notice, theme, base_path, active_view="todos")


def parse_calendar_month(value):
    fallback = date.today().replace(day=1)
    if not value:
        return fallback
    try:
        parsed = datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except ValueError:
        return fallback
    if parsed.year < 2000 or parsed.year > 2100:
        return fallback
    return parsed


def render_calendar_view(csrf_token, query=None, notice="", theme="system", base_path=""):
    query = query or {}
    month_start = parse_calendar_month(query.get("month", [""])[0])
    month_end = add_months(month_start, 1) - timedelta(days=1)
    prev_month = add_months(month_start, -1).strftime("%Y-%m")
    next_month = add_months(month_start, 1).strftime("%Y-%m")
    today = date.today()
    tasks_by_date = {}
    for task in get_tasks():
        due_date = parse_date(task["next_due_on"])
        if month_start <= due_date <= month_end:
            tasks_by_date.setdefault(due_date, []).append(task)

    weekday_cells = "".join(f'<div class="calendar-weekday">{name}</div>' for name in ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])
    day_cells = []
    for week in calendar.Calendar(firstweekday=6).monthdatescalendar(month_start.year, month_start.month):
        for day in week:
            classes = ["calendar-day"]
            if day.month != month_start.month:
                classes.append("outside")
            if day == today:
                classes.append("today")
            day_tasks = tasks_by_date.get(day, [])
            task_links = []
            for task in day_tasks:
                area = f'<div class="meta">{escape(task["ha_area_name"])}</div>' if task.get("ha_area_name") else ""
                task_links.append(
                    f"""
                    <a class="calendar-task {task["status"]}" href="{app_url(f"/item/{task["id"]}", base_path)}">
                      {id_badge(task["public_id"])}
                      <strong>{escape(task["name"])}</strong>
                      <div class="meta">{escape(task["category"])} - {escape(task["due_phrase"])}</div>
                      {area}
                    </a>
                    """
                )
            count_status = "overdue" if any(task["status"] == "overdue" for task in day_tasks) else "upcoming"
            count = f'<span class="badge {count_status}">{len(day_tasks)}</span>' if day_tasks else ""
            day_cells.append(
                f"""
                <div class="{' '.join(classes)}">
                  <div class="calendar-date"><span>{day.day}</span>{count}</div>
                  <div class="calendar-items">{''.join(task_links)}</div>
                </div>
                """
            )

    body = f"""
      <section class="panel">
        <div class="calendar-toolbar">
          <h2>{escape(month_start.strftime("%B %Y"))}</h2>
          <div class="actions">
            <a class="button secondary" href="{app_url(f"/calendar?month={prev_month}", base_path)}">Previous</a>
            <a class="button secondary" href="{app_url("/calendar", base_path)}">Today</a>
            <a class="button secondary" href="{app_url(f"/calendar?month={next_month}", base_path)}">Next</a>
          </div>
        </div>
        <div class="calendar-grid" aria-label="Maintenance calendar">
          {weekday_cells}
          {''.join(day_cells)}
        </div>
      </section>
    """
    return render_layout("Maintenance Calendar", body, csrf_token, notice, theme, base_path, active_view="calendar")


def render_reports_view(csrf_token, query=None, notice="", theme="system", base_path=""):
    report = annual_report(query_first(query or {}, "year"))
    health = backup_health()
    max_month = max(report["by_month"].values()) or 1
    month_rows = []
    for month in range(1, 13):
        count = report["by_month"][month]
        month_rows.append(
            f"""
            <div class="report-bar">
              <strong>{calendar.month_abbr[month]}</strong>
              <div class="bar-track"><div class="bar-fill" style="width: {round((count / max_month) * 100)}%"></div></div>
              <span class="meta">{count}</span>
            </div>
            """
        )
    top_rows = []
    for task_name, count in report["top_completed"]:
        top_rows.append(f'<div class="history-item"><strong>{escape(task_name)}</strong><div>{count} completion{"s" if count != 1 else ""}</div></div>')
    if not top_rows:
        top_rows.append('<div class="empty">No completions recorded for this year.</div>')
    recent_rows = []
    for item in report["recent"]:
        recent_rows.append(
            f"""
            <div class="history-item">
              <strong>{escape(item["task_name"])}</strong>
              <div>Completed {escape(item["completed_on"])} - Next due {escape(item["next_due_on"])}</div>
            </div>
            """
        )
    if not recent_rows:
        recent_rows.append('<div class="empty">No annual history yet.</div>')
    previous_year = report["year"] - 1
    next_year = report["year"] + 1
    body = f"""
      <section class="grid" aria-label="Annual maintenance report">
        <div class="stat"><strong>{report["completed"]}</strong><span>Completed in {report["year"]}</span></div>
        <div class="stat"><strong>{report["overdue"]}</strong><span>Overdue now</span></div>
        <div class="stat"><strong>{report["due_next_14"]}</strong><span>Due next 14 days</span></div>
        <div class="stat"><strong>{report["never_completed"]}</strong><span>Never completed</span></div>
        <div class="stat"><strong>{report["requires_supplies"]}</strong><span>Need supplies</span></div>
        <div class="stat"><strong>{report["total_items"]}</strong><span>Total maintenance items</span></div>
      </section>
      <section class="layout">
        <div class="panel">
          <div class="calendar-toolbar">
            <h2>{report["year"]} Completion Trend</h2>
            <div class="actions">
              <a class="button secondary" href="{app_url(f"/reports?year={previous_year}", base_path)}">Previous</a>
              <a class="button secondary" href="{app_url("/reports", base_path)}">This year</a>
              <a class="button secondary" href="{app_url(f"/reports?year={next_year}", base_path)}">Next</a>
            </div>
          </div>
          <div class="report-bars">{''.join(month_rows)}</div>
        </div>
        <aside class="panel">
          <h2>Most Completed</h2>
          <div class="history">{''.join(top_rows)}</div>
        </aside>
      </section>
      <section class="panel">
        <h2>Recent Annual History</h2>
        <div class="history">{''.join(recent_rows)}</div>
      </section>
      <section class="panel">
        <div class="table-head">
          <h2>Backup Health</h2>
          <div class="meta">Schema and database integrity check</div>
        </div>
        <div class="detail-grid">
          <div class="detail-field"><span>Status</span>{escape(health["status"].upper())}</div>
          <div class="detail-field"><span>Version</span>{escape(health["version"])}</div>
          <div class="detail-field"><span>Backup scope</span>{escape(health["backup_scope"])}</div>
          <div class="detail-field"><span>Checked</span>{escape(health["checked_at"])}</div>
        </div>
        {f'<div class="errors">{"; ".join(escape(error) for error in health["errors"])}</div>' if health["errors"] else '<div class="notice">Backup health check passed.</div>'}
      </section>
    """
    return render_layout("Maintenance Reports", body, csrf_token, notice, theme, base_path, active_view="reports")


def render_ha_setup_view(csrf_token, notice="", theme="system", base_path=""):
    examples = homeassistant_examples()
    body = f"""
      <section class="panel">
        <div class="table-head">
          <div>
            <h2>Home Assistant Setup</h2>
            <div class="meta">Rest commands and actionable notifications for native Home Assistant automations.</div>
          </div>
        </div>
        <div class="detail-grid">
          <div class="detail-field"><span>Action API</span>Token protected</div>
          <div class="detail-field"><span>Internal URL</span>{escape(examples["addon_internal_url"])}</div>
          <div class="detail-field"><span>Sensor</span>sensor.mxtracker_due_14_days</div>
          <div class="detail-field"><span>Actions</span>Done, snooze, open detail</div>
        </div>
      </section>
      <section class="panel">
        <h2>secrets.yaml</h2>
        <pre>{escape(examples["secrets_yaml"])}</pre>
      </section>
      <section class="panel">
        <h2>configuration.yaml rest_command</h2>
        <pre>{escape(examples["rest_command_yaml"])}</pre>
      </section>
      <section class="panel">
        <h2>Actionable Notification Automation</h2>
        <pre>{escape(examples["notification_automation_yaml"])}</pre>
      </section>
    """
    return render_layout("Home Assistant Setup", body, csrf_token, notice, theme, base_path, active_view="ha_setup")


def render_admin_view(csrf_token, admin_enabled=False, notice="", theme="system", base_path=""):
    if not admin_enabled:
        body = f"""
          <section class="panel">
            <h2>Admin Mode</h2>
            <p class="meta">Admin mode is for repair actions only. Unlock it only when you need to correct bad audit data.</p>
            <form action="{app_url("/admin/unlock", base_path)}" method="post">
              <input type="hidden" name="csrf_token" value="{csrf_token}">
              <label for="confirm_text">Type ADMIN to unlock for 15 minutes</label>
              <input id="confirm_text" name="confirm_text" autocomplete="off" required pattern="ADMIN" placeholder="ADMIN">
              <div class="actions form-actions">
                <button type="submit">Unlock admin mode</button>
              </div>
            </form>
          </section>
        """
        return render_layout("Admin Mode", body, csrf_token, notice, theme, base_path, active_view="admin")

    rows = []
    for item in get_all_history()[:100]:
        confirm_phrase = f"REMOVE {item['public_id']}"
        closure_options = "".join(
            f'<option value="{key}"{" selected" if key == item["closure_type"] else ""}>{escape(label)}</option>'
            for key, label in CLOSURE_LABELS.items()
        )
        rows.append(
            f"""
            <tr>
              <td data-label="ID" class="col-id">{id_badge(item["public_id"])}</td>
              <td data-label="Task" class="col-name">
                <strong>{escape(item["task_name"])}</strong>
                <div class="meta">{escape(item["task_public_id"])}</div>
              </td>
              <td data-label="Type" class="col-category">{escape(item["closure_label"])}</td>
              <td data-label="Closed" class="col-date">{escape(item["completed_on"])}</td>
              <td data-label="Next due" class="col-date">{escape(item["next_due_on"])}</td>
              <td data-label="Notes" class="col-name">{escape(item["closure_notes"])}</td>
              <td data-label="Actions" class="col-actions">
                <form action="{app_url(f"/admin/history/edit/{item["id"]}", base_path)}" method="post">
                  <input type="hidden" name="csrf_token" value="{csrf_token}">
                  <label for="completed-{item["id"]}">Closed date</label>
                  <input id="completed-{item["id"]}" type="date" name="completed_on" value="{escape(item["completed_on"])}" required>
                  <label for="next-due-{item["id"]}">Next due</label>
                  <input id="next-due-{item["id"]}" type="date" name="next_due_on" value="{escape(item["next_due_on"])}" required>
                  <label for="closure-type-{item["id"]}">Closure type</label>
                  <select id="closure-type-{item["id"]}" name="closure_type">{closure_options}</select>
                  <label for="closure-notes-{item["id"]}">Closure notes</label>
                  <textarea id="closure-notes-{item["id"]}" name="closure_notes" maxlength="1000">{escape(item["closure_notes"])}</textarea>
                  <button class="secondary" type="submit">Save closure fields</button>
                </form>
                <form action="{app_url(f"/admin/history/delete/{item["id"]}", base_path)}" method="post">
                  <input type="hidden" name="csrf_token" value="{csrf_token}">
                  <label for="confirm-{item["id"]}">Type {escape(confirm_phrase)}</label>
                  <input id="confirm-{item["id"]}" name="confirm_text" autocomplete="off" required placeholder="{escape(confirm_phrase)}">
                  <button class="danger confirm-danger" type="submit">{trash_icon()} Remove completion</button>
                </form>
              </td>
            </tr>
            """
        )
    table_body = "".join(rows) if rows else '<tr><td colspan="7" class="empty">No completion history to repair.</td></tr>'
    maintenance_rows = []
    for task in get_tasks():
        confirm_phrase = f"REOPEN {task['public_id']}"
        maintenance_rows.append(
            f"""
            <tr>
              <td data-label="ID" class="col-id">{id_badge(task["public_id"])}</td>
              <td data-label="Task" class="col-name">
                <strong>{escape(task["name"])}</strong>
                <div class="meta">{escape(task["category"])}</div>
              </td>
              <td data-label="Last done" class="col-date">{escape(task["last_completed_on"] or "Never")}</td>
              <td data-label="Next due" class="col-date">{escape(task["next_due_on"])}</td>
              <td data-label="Actions" class="col-actions">
                <form action="{app_url(f"/admin/task/reopen/{task["id"]}", base_path)}" method="post">
                  <input type="hidden" name="csrf_token" value="{csrf_token}">
                  <label for="reopen-task-{task["id"]}">Type {escape(confirm_phrase)}</label>
                  <input id="reopen-task-{task["id"]}" name="confirm_text" autocomplete="off" required placeholder="{escape(confirm_phrase)}">
                  <button class="secondary" type="submit">Reopen maintenance item</button>
                </form>
              </td>
            </tr>
            """
        )
    maintenance_body = "".join(maintenance_rows) if maintenance_rows else '<tr><td colspan="5" class="empty">No maintenance items to reopen.</td></tr>'
    todo_rows = []
    for todo in get_todos():
        confirm_phrase = f"REOPEN {todo['public_id']}"
        todo_rows.append(
            f"""
            <tr>
              <td data-label="ID" class="col-id">{id_badge(todo["public_id"])}</td>
              <td data-label="Task" class="col-name">
                <strong>{escape(todo["title"])}</strong>
                <div class="meta">{escape(todo["category"])} - {escape(todo["status_label"])}</div>
              </td>
              <td data-label="Progress" class="col-date">{todo["progress_percent"]}%</td>
              <td data-label="Target" class="col-date">{escape(todo["target_on"] or "No target")}</td>
              <td data-label="Actions" class="col-actions">
                <form action="{app_url(f"/admin/todo/reopen/{todo["id"]}", base_path)}" method="post">
                  <input type="hidden" name="csrf_token" value="{csrf_token}">
                  <label for="reopen-todo-{todo["id"]}">Type {escape(confirm_phrase)}</label>
                  <input id="reopen-todo-{todo["id"]}" name="confirm_text" autocomplete="off" required placeholder="{escape(confirm_phrase)}">
                  <button class="secondary" type="submit">Reopen house todo</button>
                </form>
              </td>
            </tr>
            """
        )
    todo_body = "".join(todo_rows) if todo_rows else '<tr><td colspan="5" class="empty">No house todos to reopen.</td></tr>'
    body = f"""
      <section class="panel">
        <div class="table-head">
          <div>
            <h2>Admin Mode</h2>
            <div class="meta">Repair audit data, edit closure records, and reopen items after accidental completion.</div>
          </div>
          <form class="inline" action="{app_url("/admin/lock", base_path)}" method="post">
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <button class="secondary" type="submit">Lock admin mode</button>
          </form>
        </div>
        <p class="meta">Edits and removals recalculate maintenance last-done and next-due values from the remaining history. Reopen keeps audit history intact and makes the item active again.</p>
      </section>
      <section class="panel table-panel">
        <div class="table-head">
          <h2>Completion History Repair</h2>
          <div class="meta">Newest records first</div>
        </div>
        <div class="table-scroll">
          <table class="task-table audit-table">
            <thead>
              <tr>
                <th class="col-id">ID</th>
                <th class="col-name">Task</th>
                <th class="col-category">Type</th>
                <th class="col-date">Closed</th>
                <th class="col-date">Next due</th>
                <th class="col-name">Notes</th>
                <th class="col-actions">Actions</th>
              </tr>
            </thead>
            <tbody>{table_body}</tbody>
          </table>
        </div>
      </section>
      <section class="panel table-panel">
        <div class="table-head">
          <h2>Reopen Maintenance</h2>
          <div class="meta">Keeps history, moves next due to today, and clears checklist steps.</div>
        </div>
        <div class="table-scroll">
          <table class="task-table audit-table">
            <thead>
              <tr>
                <th class="col-id">ID</th>
                <th class="col-name">Item</th>
                <th class="col-date">Last done</th>
                <th class="col-date">Next due</th>
                <th class="col-actions">Actions</th>
              </tr>
            </thead>
            <tbody>{maintenance_body}</tbody>
          </table>
        </div>
      </section>
      <section class="panel table-panel">
        <div class="table-head">
          <h2>Reopen House Todos</h2>
          <div class="meta">Moves the todo back to backlog and clears checklist progress.</div>
        </div>
        <div class="table-scroll">
          <table class="task-table audit-table">
            <thead>
              <tr>
                <th class="col-id">ID</th>
                <th class="col-name">Todo</th>
                <th class="col-date">Progress</th>
                <th class="col-date">Target</th>
                <th class="col-actions">Actions</th>
              </tr>
            </thead>
            <tbody>{todo_body}</tbody>
          </table>
        </div>
      </section>
    """
    return render_layout("Admin Mode", body, csrf_token, notice, theme, base_path, active_view="admin")


def render_items_audit(csrf_token, query=None, notice="", theme="system", base_path=""):
    tasks = get_tasks()
    visible_tasks, filters = filter_tasks(tasks, query or {})
    if visible_tasks:
        rows = []
        for task in visible_tasks:
            task_id = task["id"]
            notes = f'<div class="meta">{escape(task["notes"])}</div>' if task["notes"] else ""
            asset = f'<div class="meta">{escape(task["asset_name"])}</div>' if task.get("asset_name") else ""
            priority = f'<div class="meta">{escape(task["priority_label"])}</div>' if task.get("priority") != "normal" else ""
            area_name = task.get("ha_area_name") or "Unassigned"
            complete_url = app_url(f"/complete/{task_id}", base_path)
            edit_url = app_url(f"/edit/{task_id}", base_path)
            rows.append(
                f"""
                <tr>
                  <td data-label="ID" class="col-id">{id_badge(task["public_id"])}</td>
                  <td data-label="Task" class="col-name">
                    <strong><a href="{app_url(f"/item/{task_id}", base_path)}">{escape(task["name"])}</a></strong>
                    {asset}
                    {notes}
                  </td>
                  <td data-label="Category" class="col-category">{escape(task["category"])}</td>
                  <td data-label="Area" class="col-area">{escape(area_name)}</td>
                  <td data-label="Status" class="col-due">
                    <span class="badge {task["status"]}">{escape(task["due_phrase"])}</span>
                    <div class="meta">{escape(task["status_label"])}</div>
                    {priority}
                  </td>
                  <td data-label="Last done" class="col-date">{escape(task["last_completed_on"] or "Never")}</td>
                  <td data-label="Next due" class="col-date">{escape(task["next_due_on"])}</td>
                  <td data-label="Repeat" class="col-repeat">{escape(task["recurrence_phrase"])}</td>
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
        table_body = '<tr><td colspan="9" class="empty">No maintenance items match these filters.</td></tr>'

    category_options = ['<option value="">All categories</option>']
    for category in CATEGORIES:
        category_options.append(f'<option value="{escape(category)}"{selected_attr(filters["category"], category)}>{escape(category)}</option>')
    status_options = [
        ("", "All statuses"),
        ("due_14", "Due next 14 days"),
        ("overdue", "Overdue"),
        ("due_today", "Due today"),
        ("upcoming", "Upcoming"),
        ("never_done", "Never completed"),
        ("requires_supplies", "Requires supplies"),
    ]
    status_options_html = "".join(
        f'<option value="{status}"{selected_attr(filters["status"], status)}>{escape(label)}</option>'
        for status, label in status_options
    )
    area_options = [f'<option value=""{selected_attr(filters["area"], "")}>All areas</option>']
    area_options.append(f'<option value="unassigned"{selected_attr(filters["area"], "unassigned")}>Unassigned</option>')
    for area in get_homeassistant_areas():
        area_options.append(f'<option value="{escape(area["area_id"])}"{selected_attr(filters["area"], area["area_id"])}>{escape(area["name"])}</option>')

    body = f"""
      <div class="audit-actions">
        <a class="button" href="{app_url("/export/tasks.csv", base_path)}">Export items CSV</a>
        <a class="button secondary" href="{app_url("/export/history.csv", base_path)}">Export history CSV</a>
        <a class="button secondary" href="{app_url("/export/events.csv", base_path)}">Export lifecycle CSV</a>
      </div>
      <section class="panel filter-panel">
        <form action="{app_url("/items", base_path)}" method="get">
          <div class="filter-grid">
            <div>
              <label for="task-q">Search</label>
              <input id="task-q" name="q" value="{escape(filters["q"])}" placeholder="ID, task, notes, category, area">
            </div>
            <div>
              <label for="task-category">Category</label>
              <select id="task-category" name="category">{''.join(category_options)}</select>
            </div>
            <div>
              <label for="task-status">Status</label>
              <select id="task-status" name="status">{status_options_html}</select>
            </div>
            <div>
              <label for="task-area">Area</label>
              <select id="task-area" name="area">{''.join(area_options)}</select>
            </div>
          </div>
          <div class="filter-actions form-actions">
            <button type="submit">Apply filters</button>
            <a class="button secondary" href="{app_url("/items", base_path)}">Clear</a>
          </div>
          <div class="result-count">{len(visible_tasks)} of {len(tasks)} maintenance items shown</div>
        </form>
      </section>
      <section class="panel table-panel">
        <div class="table-head">
          <h2>All Items</h2>
          <div class="meta">Audit every maintenance item at a glance</div>
        </div>
        <div class="table-scroll">
        <table class="task-table audit-table">
          <thead>
            <tr>
              <th class="col-id">ID</th>
              <th class="col-name">Task</th>
              <th class="col-category">Category</th>
              <th class="col-area">Area</th>
              <th class="col-due">Status</th>
              <th class="col-date">Last done</th>
              <th class="col-date">Next due</th>
              <th class="col-repeat">Repeat</th>
              <th class="col-actions">Actions</th>
            </tr>
          </thead>
          <tbody>{table_body}</tbody>
        </table>
        </div>
      </section>
    """
    return render_layout("Maintenance Audit", body, csrf_token, notice, theme, base_path, active_view="audit")


def render_item_detail(task, csrf_token, notice="", theme="system", base_path=""):
    history = get_task_history(task["id"])
    checklist = get_task_checklist(task["id"])
    events = get_task_events(task["id"])
    history_count = len(history)
    status_class = " overdue" if task["status"] == "overdue" else ""
    area_name = task.get("ha_area_name") or "Unassigned"
    complete_url = app_url(f"/complete/{task['id']}", base_path)
    snooze_url = app_url(f"/snooze/{task['id']}", base_path)
    edit_url = app_url(f"/edit/{task['id']}", base_path)
    delete_url = f'{app_url(f"/delete/{task["id"]}", base_path)}?return_to={quote(f"/item/{task["id"]}", safe="")}'
    return_to = f"/item/{task['id']}"
    checklist_items = []
    for item in checklist:
        checked = " checked" if item["is_done"] else ""
        done_class = " done" if item["is_done"] else ""
        checklist_items.append(
            f"""
            <div class="check-item{done_class}">
              <form class="inline" action="{app_url(f"/item/checklist/{item["id"]}/toggle", base_path)}" method="post">
                <input type="hidden" name="csrf_token" value="{csrf_token}">
                <input type="checkbox" aria-label="Toggle {escape(item["label"])}" onchange="this.form.submit()"{checked}>
              </form>
              <div><strong>{escape(item["label"])}</strong></div>
              <form class="inline" action="{app_url(f"/item/checklist/{item["id"]}/delete", base_path)}" method="post">
                <input type="hidden" name="csrf_token" value="{csrf_token}">
                <button class="secondary" type="submit">Remove</button>
              </form>
            </div>
            """
        )
    checklist_html = "".join(checklist_items) if checklist_items else '<div class="empty">No checklist steps yet.</div>'
    closure_options = "".join(f'<option value="{key}">{escape(label)}</option>' for key, label in CLOSURE_LABELS.items())

    if history:
        history_rows = []
        for item in history:
            history_rows.append(
                f"""
                <tr>
                  <td data-label="ID">{id_badge(item["public_id"])}</td>
                  <td data-label="Type">{escape(item["closure_label"])}</td>
                  <td data-label="Closed">{escape(item["completed_on"])}</td>
                  <td data-label="Next due">{escape(item["next_due_on"])}</td>
                  <td data-label="Notes">{escape(item["closure_notes"] or "")}</td>
                  <td data-label="Recorded">{escape(item["created_at"])}</td>
                </tr>
                """
            )
        history_body = "".join(history_rows)
    else:
        history_body = '<tr><td colspan="6" class="empty">No completions recorded yet.</td></tr>'
    event_items = []
    for event in events:
        data = event.get("event_data_json") or {}
        details = []
        if "changed" in data and data["changed"]:
            details.append("Changed " + ", ".join(str(item) for item in data["changed"][:8]))
        for key in ["closure_type", "closure_notes", "completed_on", "next_due_on", "from", "to", "days", "label"]:
            if key in data and data[key] not in ("", None, []):
                details.append(f"{key.replace('_', ' ')}: {data[key]}")
        event_items.append(
            f"""
            <div class="history-item">
              <strong>{escape(event_type_label(event["event_type"]))}</strong>
              <div>{escape(event["created_at"])}</div>
              {f'<div>{escape("; ".join(details))}</div>' if details else ''}
            </div>
            """
        )
    if not event_items:
        event_items.append('<div class="empty">No lifecycle events yet.</div>')

    notes = f'<p class="notes">{escape(task["notes"])}</p>' if task["notes"] else '<p class="empty">No notes saved for this item.</p>'
    body = f"""
      <section class="panel detail-hero{status_class}">
        <div class="table-head">
          <div>
            <h2>{escape(task["name"])}</h2>
            <div class="meta">{escape(task["public_id"])} - {escape(task["category"])} - {escape(area_name)}</div>
          </div>
          <span class="badge {task["status"]}">{escape(task["due_phrase"])}</span>
        </div>
        <div class="detail-grid">
          <div class="detail-field"><span>Maintenance ID</span>{escape(task["public_id"])}</div>
          <div class="detail-field"><span>Category</span>{escape(task["category"])}</div>
          <div class="detail-field"><span>Home Assistant area</span>{escape(area_name)}</div>
          <div class="detail-field"><span>Asset</span>{escape(task.get("asset_name") or "Not set")}</div>
          <div class="detail-field"><span>Location</span>{escape(task.get("location") or "Not set")}</div>
          <div class="detail-field"><span>Status</span>{escape(task["status_label"])}</div>
          <div class="detail-field"><span>Next due</span>{escape(task["next_due_on"])}</div>
          <div class="detail-field"><span>Due timing</span>{escape(task["due_phrase"])}</div>
          <div class="detail-field"><span>Last done</span>{escape(task["last_completed_on"] or "Never")}</div>
          <div class="detail-field"><span>Repeat</span>{escape(task["recurrence_phrase"])}</div>
          <div class="detail-field"><span>Priority</span>{escape(task["priority_label"])}</div>
          <div class="detail-field"><span>Season</span>{escape(task["season_label"])}</div>
          <div class="detail-field"><span>Requires supplies</span>{"Yes" if task.get("requires_supplies") else "No"}</div>
          <div class="detail-field"><span>Estimated time</span>{escape(task.get("estimated_minutes") or 0)} min</div>
          <div class="detail-field"><span>Model</span>{escape(task.get("model_number") or "Not set")}</div>
          <div class="detail-field"><span>Serial</span>{escape(task.get("serial_number") or "Not set")}</div>
          <div class="detail-field"><span>Filter/part</span>{escape(task.get("filter_size") or "Not set")}</div>
          <div class="detail-field"><span>Warranty</span>{escape(task.get("warranty_expires_on") or "Not set")}</div>
          <div class="detail-field"><span>Times completed</span>{history_count}</div>
          <div class="detail-field"><span>Created</span>{escape(task["created_at"])}</div>
          <div class="detail-field"><span>Updated</span>{escape(task["updated_at"])}</div>
        </div>
        <h2>Details</h2>
        {notes}
        <div class="detail-section">
          <div class="table-head">
            <div>
              <h2>Checklist</h2>
              <div class="meta">Reusable steps for this recurring maintenance item.</div>
            </div>
          </div>
          <div class="checklist">{checklist_html}</div>
          <form class="mini-form" action="{app_url(f"/item/{task["id"]}/checklist", base_path)}" method="post">
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <input name="label" maxlength="180" placeholder="Add maintenance step">
            <button type="submit">Add step</button>
          </form>
        </div>
        <div class="actions form-actions">
          <form class="inline" action="{complete_url}" method="post">
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <input type="hidden" name="return_to" value="{escape(return_to)}">
            <input type="hidden" name="closure_type" value="done">
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
        <div class="detail-section">
          <div class="table-head">
            <div>
              <h2>Record Closure</h2>
              <div class="meta">Use this when you need notes or a closure type other than Done.</div>
            </div>
          </div>
          <form action="{complete_url}" method="post">
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <input type="hidden" name="return_to" value="{escape(return_to)}">
            <label for="closure_type">Closure type</label>
            <select id="closure_type" name="closure_type">{closure_options}</select>
            <label for="closure_notes">Closure notes</label>
            <textarea id="closure_notes" name="closure_notes" maxlength="1000" placeholder="What happened, what was skipped, parts used, or why it was not needed"></textarea>
            <div class="actions form-actions">
              <button type="submit">Record closure</button>
            </div>
          </form>
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
              <th>ID</th>
              <th>Type</th>
              <th>Closed</th>
              <th>Next due after closure</th>
              <th>Notes</th>
              <th>Recorded</th>
            </tr>
          </thead>
          <tbody>{history_body}</tbody>
        </table>
      </section>
      <section class="panel">
        <div class="table-head">
          <h2>Lifecycle Events</h2>
          <div class="meta">Create, edit, completion, snooze, and checklist audit trail</div>
        </div>
        <div class="history">{''.join(event_items)}</div>
      </section>
    """
    return render_layout(task["name"], body, csrf_token, notice, theme, base_path, active_view="audit")


def render_todo_detail(todo, csrf_token, notice="", theme="system", base_path=""):
    checklist = get_todo_checklist(todo["id"])
    todo = enrich_todo(todo, checklist)
    parents = [item for item in checklist if not item["parent_id"]]
    children = {}
    for item in checklist:
        if item["parent_id"]:
            children.setdefault(item["parent_id"], []).append(item)

    def render_check_item(item, child=False):
        checked = " checked" if item["is_done"] else ""
        done_class = " done" if item["is_done"] else ""
        required = '<span class="badge ready">Start gate</span>' if item["required_for_start"] else ""
        delete_url = app_url(f"/todo/checklist/{item['id']}/delete", base_path)
        toggle_url = app_url(f"/todo/checklist/{item['id']}/toggle", base_path)
        subform = ""
        if not child:
            subform = f"""
              <form class="mini-form" action="{app_url(f"/todo/{todo["id"]}/checklist", base_path)}" method="post">
                <input type="hidden" name="csrf_token" value="{csrf_token}">
                <input type="hidden" name="parent_id" value="{item["id"]}">
                <input name="label" maxlength="180" placeholder="Add sub-step">
                <button class="secondary" type="submit">Add</button>
              </form>
            """
        child_markup = "".join(render_check_item(child_item, True) for child_item in children.get(item["id"], []))
        return f"""
          <div class="check-item{' child' if child else ''}{done_class}">
            <form class="inline" action="{toggle_url}" method="post">
              <input type="hidden" name="csrf_token" value="{csrf_token}">
              <input type="checkbox" aria-label="Toggle {escape(item["label"])}" onchange="this.form.submit()"{checked}>
            </form>
            <div>
              <strong>{escape(item["label"])}</strong>
              <div class="focus-meta">{required}</div>
              {subform}
            </div>
            <form class="inline" action="{delete_url}" method="post">
              <input type="hidden" name="csrf_token" value="{csrf_token}">
              <button class="secondary" type="submit">Remove</button>
            </form>
          </div>
          {child_markup}
        """

    checklist_html = "".join(render_check_item(item) for item in parents)
    if not checklist_html:
        checklist_html = '<div class="empty">No checklist items yet. Add planning gates or work steps below.</div>'

    area_name = todo.get("ha_area_name") or "Unassigned"
    target = todo["target_on"] or "No target"
    edit_url = app_url(f"/todo/edit/{todo['id']}", base_path)
    delete_url = f'{app_url(f"/todo/delete/{todo["id"]}", base_path)}?return_to={quote(f"/todo/{todo["id"]}", safe="")}'
    notes = f'<p class="notes">{escape(todo["description"])}</p>' if todo["description"] else '<p class="empty">No details saved for this todo.</p>'
    body = f"""
      <section class="panel detail-hero">
        <div class="table-head">
          <div>
            <h2>{escape(todo["title"])}</h2>
            <div class="meta">{escape(todo["public_id"])} - {escape(todo["category"])} - {escape(area_name)}</div>
          </div>
          <span class="score-pill">{escape(todo["priority_score"])}</span>
        </div>
        <div class="detail-grid">
          <div class="detail-field"><span>Todo ID</span>{escape(todo["public_id"])}</div>
          <div class="detail-field"><span>Status</span>{escape(todo["status_label"])}</div>
          <div class="detail-field"><span>Progress</span>{todo["progress_percent"]}%</div>
          <div class="detail-field"><span>Ready gate</span>{todo["required_done"]}/{todo["required_total"]}</div>
          <div class="detail-field"><span>Action lane</span>{escape(todo["action_lane"])}</div>
          <div class="detail-field"><span>Risk score</span>{todo["hazard_score"]}/25</div>
          <div class="detail-field"><span>Likelihood</span>{todo["likelihood"]}/5</div>
          <div class="detail-field"><span>Consequence</span>{todo["consequence"]}/5</div>
          <div class="detail-field"><span>Urgency</span>{todo["urgency"]}/5</div>
          <div class="detail-field"><span>Effort</span>{todo["effort"]}/5</div>
          <div class="detail-field"><span>Cost</span>{todo["cost"]}/5</div>
          <div class="detail-field"><span>Target</span>{escape(target)}</div>
          <div class="detail-field"><span>Updated</span>{escape(todo["updated_at"])}</div>
        </div>
        <div class="progress-bar" aria-label="Progress {todo["progress_percent"]}%">
          <div class="progress-fill" style="width: {todo["progress_percent"]}%"></div>
        </div>
        <h2>Details</h2>
        {notes}
        <div class="actions form-actions">
          <a class="button secondary" href="{edit_url}">Edit</a>
          <a class="button danger" href="{delete_url}">Delete</a>
          <a class="button secondary" href="{app_url("/todos", base_path)}">Back to house todos</a>
        </div>
      </section>
      <section class="panel">
        <div class="table-head">
          <div>
            <h2>Plan</h2>
            <div class="meta">Items marked start gate decide when the project becomes ready.</div>
          </div>
        </div>
        <div class="checklist">{checklist_html}</div>
        <form class="mini-form" action="{app_url(f"/todo/{todo["id"]}/checklist", base_path)}" method="post">
          <input type="hidden" name="csrf_token" value="{csrf_token}">
          <input name="label" maxlength="180" placeholder="Add planning or work step">
          <label><input type="checkbox" name="required_for_start" value="1"> Start gate</label>
          <button type="submit">Add step</button>
        </form>
      </section>
    """
    return render_layout(todo["title"], body, csrf_token, notice, theme, base_path, active_view="todos")


def render_todo_form(csrf_token, todo=None, errors=None, theme="system", base_path=""):
    errors = errors or []
    is_edit = todo is not None
    values = todo or {
        "title": "",
        "category": "General",
        "description": "",
        "ha_area_id": "",
        "ha_area_name": "",
        "likelihood": 2,
        "consequence": 2,
        "urgency": 2,
        "effort": 2,
        "cost": 2,
        "status": "backlog",
        "target_on": "",
    }
    values["ha_area_id"] = values.get("ha_area_id") or ""
    values["ha_area_name"] = values.get("ha_area_name") or ""
    action = app_url(f'/todo/edit/{todo["id"]}', base_path) if is_edit else app_url("/todo/new", base_path)
    title = "Edit House Todo" if is_edit else "Add House Todo"

    category_options = []
    for category in TODO_CATEGORIES:
        selected = " selected" if values["category"] == category else ""
        category_options.append(f'<option value="{escape(category)}"{selected}>{escape(category)}</option>')
    status_options = []
    for status in ["backlog", "planning", "ready", "in_work", "blocked", "done"]:
        selected = " selected" if values["status"] == status else ""
        status_options.append(f'<option value="{status}"{selected}>{TODO_STATUS_LABELS[status]}</option>')

    selected_area_id = values["ha_area_id"]
    area_options = ['<option value="">No area</option>']
    known_area_ids = set()
    for area in get_homeassistant_areas():
        area_id = area["area_id"]
        known_area_ids.add(area_id)
        selected = " selected" if selected_area_id == area_id else ""
        area_options.append(f'<option value="{escape(area_id)}"{selected}>{escape(area["name"])}</option>')
    if selected_area_id and selected_area_id not in known_area_ids:
        selected_name = values["ha_area_name"] or selected_area_id
        area_options.append(f'<option value="{escape(selected_area_id)}" selected>{escape(selected_name)}</option>')

    def scale_select(name, label, low, high):
        options = []
        current = int(values.get(name, 2) or 2)
        for score in range(1, 6):
            selected = " selected" if current == score else ""
            options.append(f'<option value="{score}"{selected}>{score}</option>')
        return f"""
          <div>
            <label for="{name}">{label}</label>
            <select id="{name}" name="{name}">{''.join(options)}</select>
            <div class="meta">{escape(low)} to {escape(high)}</div>
          </div>
        """

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
          <label for="title">Title</label>
          <input id="title" name="title" maxlength="120" required value="{escape(values["title"])}">
          <label for="category">Category</label>
          <select id="category" name="category">{''.join(category_options)}</select>
          <label for="ha_area_id">Home Assistant area</label>
          <select id="ha_area_id" name="ha_area_id">{''.join(area_options)}</select>
          <input type="hidden" name="ha_area_name" value="{escape(values["ha_area_name"])}">
          <label for="description">Details</label>
          <textarea id="description" name="description" maxlength="1600">{escape(values["description"])}</textarea>
          <div class="scale-grid">
            {scale_select("likelihood", "Likelihood", "Rare", "Already happening")}
            {scale_select("consequence", "Consequence", "Annoying", "Damage or safety issue")}
            {scale_select("urgency", "Urgency", "Someday", "This week")}
            {scale_select("effort", "Effort", "Few minutes", "Multi-day")}
            {scale_select("cost", "Cost", "Free", "Expensive")}
          </div>
          <label for="status">Status</label>
          <select id="status" name="status">{''.join(status_options)}</select>
          <label for="target_on">Target date</label>
          <input id="target_on" name="target_on" type="date" value="{escape(values.get("target_on", ""))}">
          <div class="actions form-actions">
            <button type="submit">Save house todo</button>
            <a class="button secondary" href="{app_url("/todos", base_path)}">Cancel</a>
          </div>
        </form>
      </section>
    """
    return render_layout(title, body, csrf_token, theme=theme, base_path=base_path, active_view="todos")


def render_todo_delete_confirm(todo, csrf_token, return_to="/todos", notice="", theme="system", base_path=""):
    delete_url = app_url(f"/todo/delete/{todo['id']}", base_path)
    body = f"""
      <section class="panel">
        <h2>{trash_icon()} Delete House Todo</h2>
        <p class="meta">This removes the one-off project and its checklist. Type DELETE to confirm.</p>
        <div class="task confirm-card">
          <h3>{escape(todo["title"])}</h3>
          <div class="meta">{escape(todo.get("category") or "General")}</div>
        </div>
        <form action="{delete_url}" method="post">
          <input type="hidden" name="csrf_token" value="{csrf_token}">
          <input type="hidden" name="return_to" value="{escape(return_to)}">
          <label for="confirm_text">Confirmation text</label>
          <input id="confirm_text" name="confirm_text" autocomplete="off" required pattern="DELETE" placeholder="Type DELETE">
          <div class="actions form-actions">
            <button class="danger confirm-danger" type="submit">{trash_icon()} Delete house todo</button>
            <a class="button secondary" href="{app_url(return_to, base_path)}">Cancel</a>
          </div>
        </form>
      </section>
    """
    return render_layout("Delete House Todo", body, csrf_token, notice, theme=theme, base_path=base_path, active_view="todos")


def render_task_form(csrf_token, task=None, errors=None, theme="system", base_path=""):
    errors = errors or []
    is_edit = task is not None
    values = task or {
        "name": "",
        "category": "General",
        "notes": "",
        "ha_area_id": "",
        "ha_area_name": "",
        "asset_name": "",
        "location": "",
        "model_number": "",
        "serial_number": "",
        "filter_size": "",
        "purchase_date": "",
        "warranty_expires_on": "",
        "priority": "normal",
        "season": "",
        "tags": "",
        "requires_supplies": 0,
        "estimated_minutes": 0,
        "interval_count": 1,
        "interval_unit": "months",
        "next_due_on": today_iso(),
    }
    values["ha_area_id"] = values.get("ha_area_id") or ""
    values["ha_area_name"] = values.get("ha_area_name") or ""
    action = app_url(f'/edit/{task["id"]}', base_path) if is_edit else app_url("/new", base_path)
    title = "Edit Maintenance Item" if is_edit else "Add Maintenance Item"
    unit_options = []
    for unit in ["days", "weeks", "months", "years"]:
        selected = " selected" if values["interval_unit"] == unit else ""
        unit_options.append(f'<option value="{unit}"{selected}>{unit.title()}</option>')
    category_options = []
    for category in CATEGORIES:
        selected = " selected" if values["category"] == category else ""
        category_options.append(f'<option value="{escape(category)}"{selected}>{escape(category)}</option>')
    selected_area_id = values["ha_area_id"]
    area_options = ['<option value="">No area</option>']
    known_area_ids = set()
    for area in get_homeassistant_areas():
        area_id = area["area_id"]
        known_area_ids.add(area_id)
        selected = " selected" if selected_area_id == area_id else ""
        area_options.append(f'<option value="{escape(area_id)}"{selected}>{escape(area["name"])}</option>')
    if selected_area_id and selected_area_id not in known_area_ids:
        selected_name = values["ha_area_name"] or selected_area_id
        area_options.append(f'<option value="{escape(selected_area_id)}" selected>{escape(selected_name)}</option>')
    priority_options = []
    for priority in ["low", "normal", "high", "critical"]:
        priority_options.append(f'<option value="{priority}"{selected_attr(values.get("priority", "normal"), priority)}>{PRIORITY_LABELS[priority]}</option>')
    season_options = []
    for season in ["", "spring", "summer", "fall", "winter", "year_round"]:
        season_options.append(f'<option value="{season}"{selected_attr(values.get("season", ""), season)}>{SEASON_LABELS[season]}</option>')
    checked_supplies = " checked" if values.get("requires_supplies") else ""
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
          <label for="ha_area_id">Home Assistant area</label>
          <select id="ha_area_id" name="ha_area_id">{''.join(area_options)}</select>
          <input type="hidden" name="ha_area_name" value="{escape(values["ha_area_name"])}">
          <div class="form-row">
            <div>
              <label for="asset_name">Asset</label>
              <input id="asset_name" name="asset_name" maxlength="120" value="{escape(values.get("asset_name", ""))}" placeholder="HVAC unit, dishwasher, gutters">
            </div>
            <div>
              <label for="priority">Priority</label>
              <select id="priority" name="priority">{''.join(priority_options)}</select>
            </div>
          </div>
          <div class="form-row">
            <div>
              <label for="season">Season</label>
              <select id="season" name="season">{''.join(season_options)}</select>
            </div>
            <div>
              <label><input type="checkbox" name="requires_supplies" value="1"{checked_supplies}> Requires supplies</label>
            </div>
          </div>
          <label for="tags">Tags</label>
          <input id="tags" name="tags" maxlength="200" value="{escape(values.get("tags", ""))}" placeholder="supplies, ladder, outside">
          <details class="advanced-panel">
            <summary>Advanced asset details</summary>
            <div class="advanced-content">
              <div class="form-row">
                <div>
                  <label for="location">Location</label>
                  <input id="location" name="location" maxlength="120" value="{escape(values.get("location", ""))}" placeholder="Basement, kitchen, garage">
                </div>
                <div>
                  <label for="model_number">Model number</label>
                  <input id="model_number" name="model_number" maxlength="120" value="{escape(values.get("model_number", ""))}">
                </div>
              </div>
              <div class="form-row">
                <div>
                  <label for="serial_number">Serial number</label>
                  <input id="serial_number" name="serial_number" maxlength="120" value="{escape(values.get("serial_number", ""))}">
                </div>
                <div>
                  <label for="filter_size">Filter or part size</label>
                  <input id="filter_size" name="filter_size" maxlength="80" value="{escape(values.get("filter_size", ""))}">
                </div>
              </div>
              <div class="form-row">
                <div>
                  <label for="estimated_minutes">Estimated minutes</label>
                  <input id="estimated_minutes" name="estimated_minutes" type="number" min="0" max="10080" value="{escape(values.get("estimated_minutes", 0))}">
                </div>
                <div>
                  <label for="purchase_date">Purchase date</label>
                  <input id="purchase_date" name="purchase_date" type="date" value="{escape(values.get("purchase_date", ""))}">
                </div>
              </div>
              <label for="warranty_expires_on">Warranty expires</label>
              <input id="warranty_expires_on" name="warranty_expires_on" type="date" value="{escape(values.get("warranty_expires_on", ""))}">
            </div>
          </details>
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
            <button type="submit">Save maintenance item</button>
            <a class="button secondary" href="{app_url("/", base_path)}">Cancel</a>
          </div>
        </form>
      </section>
    """
    return render_layout(title, body, csrf_token, theme=theme, base_path=base_path, active_view="new" if not is_edit else "audit")


def render_delete_confirm(task, csrf_token, return_to="/", notice="", theme="system", base_path=""):
    delete_url = app_url(f"/delete/{task['id']}", base_path)
    recurrence = recurrence_phrase(task["interval_count"], task["interval_unit"])
    category = task.get("category") or "General"
    body = f"""
      <section class="panel">
        <h2>{trash_icon()} Delete Maintenance Item</h2>
        <p class="meta">This removes the maintenance item, future schedule, checklist, and history. Type DELETE to confirm.</p>
        <div class="task confirm-card">
          <h3>{escape(task["name"])}</h3>
          <div class="meta">{escape(category)} - {escape(recurrence)}</div>
        </div>
        <form action="{delete_url}" method="post">
          <input type="hidden" name="csrf_token" value="{csrf_token}">
          <input type="hidden" name="return_to" value="{escape(return_to)}">
          <label for="confirm_text">Confirmation text</label>
          <input id="confirm_text" name="confirm_text" autocomplete="off" required pattern="DELETE" placeholder="Type DELETE">
          <div class="actions form-actions">
            <button class="danger confirm-danger" type="submit">{trash_icon()} Delete maintenance item</button>
            <a class="button secondary" href="{app_url(return_to, base_path)}">Cancel</a>
          </div>
        </form>
      </section>
    """
    return render_layout("Delete Maintenance Item", body, csrf_token, notice, theme=theme, base_path=base_path, active_view="audit")


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
            query = parse_qs(parsed.query)
            notice = query.get("notice", [""])[0]
            self.respond_html(render_root_page(query, csrf, notice=notice, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path == "/items":
            query = parse_qs(parsed.query)
            notice = query.get("notice", [""])[0]
            self.respond_html(render_items_audit(csrf, query=query, notice=notice, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path == "/todos":
            query = parse_qs(parsed.query)
            notice = query.get("notice", [""])[0]
            self.respond_html(render_todos_view(csrf, query=query, notice=notice, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path == "/todo/new":
            self.respond_html(render_todo_form(csrf, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path.startswith("/todo/edit/"):
            project_id = self.extract_id(parsed.path, "/todo/edit/")
            todo = get_todo(project_id) if project_id else None
            if not todo:
                self.redirect("/todos", "House todo not found.")
                return
            self.respond_html(render_todo_form(csrf, todo=todo, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path.startswith("/todo/delete/"):
            project_id = self.extract_id(parsed.path, "/todo/delete/")
            todo = get_todo(project_id) if project_id else None
            if not todo:
                self.redirect("/todos", "House todo not found.")
                return
            query = parse_qs(parsed.query)
            return_to = safe_return_path(query.get("return_to", ["/todos"])[0])
            notice = query.get("notice", [""])[0]
            self.respond_html(render_todo_delete_confirm(todo, csrf, return_to, notice=notice, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path.startswith("/todo/"):
            project_id = self.extract_id(parsed.path, "/todo/")
            todo = get_todo(project_id) if project_id else None
            if not todo:
                self.redirect("/todos", "House todo not found.")
                return
            notice = parse_qs(parsed.query).get("notice", [""])[0]
            self.respond_html(render_todo_detail(todo, csrf, notice=notice, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path == "/calendar":
            query = parse_qs(parsed.query)
            notice = query.get("notice", [""])[0]
            self.respond_html(render_calendar_view(csrf, query=query, notice=notice, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path == "/reports":
            query = parse_qs(parsed.query)
            notice = query.get("notice", [""])[0]
            self.respond_html(render_reports_view(csrf, query=query, notice=notice, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path == "/ha-setup":
            notice = parse_qs(parsed.query).get("notice", [""])[0]
            self.respond_html(render_ha_setup_view(csrf, notice=notice, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path == "/admin":
            notice = parse_qs(parsed.query).get("notice", [""])[0]
            self.respond_html(render_admin_view(csrf, self.admin_mode_enabled(), notice=notice, theme=theme, base_path=base_path), csrf)
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
            query = parse_qs(parsed.query)
            return_to = safe_return_path(query.get("return_to", ["/"])[0])
            notice = query.get("notice", [""])[0]
            self.respond_html(render_delete_confirm(task, csrf, return_to, notice=notice, theme=theme, base_path=base_path), csrf)
            return
        if parsed.path == "/api/summary":
            self.respond_json(summarize(get_tasks()))
            return
        if parsed.path == "/api/tasks":
            self.respond_json({"tasks": [public_task(task) for task in get_tasks()]})
            return
        if parsed.path == "/api/todos":
            self.respond_json({"todos": [public_todo(todo) for todo in get_todos()]})
            return
        if parsed.path == "/api/areas":
            self.respond_json({"areas": get_homeassistant_areas()})
            return
        if parsed.path == "/api/report":
            self.respond_json({"report": annual_report(parse_qs(parsed.query).get("year", [""])[0])})
            return
        if parsed.path == "/api/backup/health":
            self.respond_json({"backup": backup_health()})
            return
        if parsed.path == "/api/homeassistant/examples":
            self.respond_json({"examples": homeassistant_examples()})
            return
        if parsed.path == "/export/tasks.csv":
            self.respond_csv(tasks_csv(), "mxtracker-maintenance-items.csv")
            return
        if parsed.path == "/export/history.csv":
            self.respond_csv(history_csv(), "mxtracker-maintenance-history.csv")
            return
        if parsed.path == "/export/events.csv":
            self.respond_csv(events_csv(), "mxtracker-lifecycle-events.csv")
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
        if parsed.path.startswith("/api/actions/"):
            if content_type != "application/json":
                self.respond_json({"ok": False, "error": "Unsupported media type."}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return
            if not self.valid_api_action_token():
                self.respond_json({"ok": False, "error": "API action token is not configured or invalid."}, HTTPStatus.FORBIDDEN)
                return
            try:
                payload = self.read_json()
            except ValueError as error:
                self.respond_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            action = parsed.path.removeprefix("/api/actions/").strip("/")
            status, response = handle_api_action(action, payload, self.base_path())
            self.respond_json(response, status)
            return
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
        if parsed.path == "/todo/new":
            errors, cleaned = validate_todo_form(fields)
            if errors:
                csrf = self.get_or_create_csrf_token()
                self.respond_html(render_todo_form(csrf, todo=cleaned, errors=errors, theme=self.current_theme(), base_path=self.base_path()), csrf, HTTPStatus.BAD_REQUEST)
                return
            project_id = save_todo(cleaned)
            self.redirect(f"/todo/{project_id}", "House todo added.")
            return
        if parsed.path.startswith("/todo/edit/"):
            project_id = self.extract_id(parsed.path, "/todo/edit/")
            if not project_id or not get_todo(project_id):
                self.redirect("/todos", "House todo not found.")
                return
            errors, cleaned = validate_todo_form(fields)
            cleaned["id"] = project_id
            if errors:
                csrf = self.get_or_create_csrf_token()
                self.respond_html(render_todo_form(csrf, todo=cleaned, errors=errors, theme=self.current_theme(), base_path=self.base_path()), csrf, HTTPStatus.BAD_REQUEST)
                return
            save_todo(cleaned, project_id)
            self.redirect(f"/todo/{project_id}", "House todo updated.")
            return
        if parsed.path.startswith("/todo/") and parsed.path.endswith("/checklist"):
            project_id = self.extract_id(parsed.path.removesuffix("/checklist"), "/todo/")
            if not project_id or not get_todo(project_id):
                self.redirect("/todos", "House todo not found.")
                return
            parent_raw = fields.get("parent_id", "").strip()
            parent_id = int(parent_raw) if parent_raw.isdigit() else None
            if add_todo_checklist_item(project_id, fields.get("label", ""), parent_id, fields.get("required_for_start") == "1"):
                self.redirect(f"/todo/{project_id}", "Checklist item added.")
            else:
                self.redirect(f"/todo/{project_id}", "Checklist item could not be added.")
            return
        if parsed.path.startswith("/item/") and parsed.path.endswith("/checklist"):
            task_id = self.extract_id(parsed.path.removesuffix("/checklist"), "/item/")
            if task_id and add_task_checklist_item(task_id, fields.get("label", "")):
                self.redirect(f"/item/{task_id}", "Checklist step added.")
            else:
                self.redirect(f"/item/{task_id or ''}", "Checklist step could not be added.")
            return
        if parsed.path.startswith("/item/checklist/") and parsed.path.endswith("/toggle"):
            item_id = self.extract_id(parsed.path.removesuffix("/toggle"), "/item/checklist/")
            task_id = toggle_task_checklist_item(item_id) if item_id else None
            if task_id:
                self.redirect(f"/item/{task_id}", "Checklist updated.")
            else:
                self.redirect("/", "Checklist step not found.")
            return
        if parsed.path.startswith("/item/checklist/") and parsed.path.endswith("/delete"):
            item_id = self.extract_id(parsed.path.removesuffix("/delete"), "/item/checklist/")
            task_id = delete_task_checklist_item(item_id) if item_id else None
            if task_id:
                self.redirect(f"/item/{task_id}", "Checklist step removed.")
            else:
                self.redirect("/", "Checklist step not found.")
            return
        if parsed.path.startswith("/todo/checklist/") and parsed.path.endswith("/toggle"):
            item_id = self.extract_id(parsed.path.removesuffix("/toggle"), "/todo/checklist/")
            project_id = toggle_todo_checklist_item(item_id) if item_id else None
            if project_id:
                self.redirect(f"/todo/{project_id}", "Checklist updated.")
            else:
                self.redirect("/todos", "Checklist item not found.")
            return
        if parsed.path.startswith("/todo/checklist/") and parsed.path.endswith("/delete"):
            item_id = self.extract_id(parsed.path.removesuffix("/delete"), "/todo/checklist/")
            project_id = delete_todo_checklist_item(item_id) if item_id else None
            if project_id:
                self.redirect(f"/todo/{project_id}", "Checklist item removed.")
            else:
                self.redirect("/todos", "Checklist item not found.")
            return
        if parsed.path.startswith("/todo/delete/"):
            project_id = self.extract_id(parsed.path, "/todo/delete/")
            return_to = safe_return_path(fields.get("return_to", "/todos"))
            if not delete_confirmed(fields):
                confirm_path = f"/todo/delete/{project_id}?return_to={quote(return_to, safe='')}" if project_id else "/todos"
                self.redirect(confirm_path, "Type DELETE to confirm deletion.")
                return
            if project_id:
                delete_todo(project_id)
            self.redirect(return_to if return_to != f"/todo/{project_id}" else "/todos", "House todo deleted.")
            return
        if parsed.path == "/admin/unlock":
            if admin_unlock_confirmed(fields):
                token, _ = create_admin_session()
                self.redirect_with_admin_cookie("/admin", "Admin mode unlocked for 15 minutes.", token=token)
            else:
                self.redirect("/admin", "Type ADMIN to unlock admin mode.")
            return
        if parsed.path == "/admin/lock":
            self.clear_current_admin_session()
            self.redirect_with_admin_cookie("/admin", "Admin mode locked.", clear=True)
            return
        if parsed.path.startswith("/admin/history/delete/"):
            history_id = self.extract_id(parsed.path, "/admin/history/delete/")
            history_item = get_completion_history_item(history_id) if history_id else None
            if not self.admin_mode_enabled():
                self.redirect("/admin", "Unlock admin mode before repairing completion history.")
                return
            if not history_item:
                self.redirect("/admin", "Completion record not found.")
                return
            if not history_delete_confirmed(fields, history_item):
                self.redirect("/admin", f"Type REMOVE {history_item['public_id']} to remove that completion.")
                return
            removed = delete_completion_history_item(history_id)
            if removed:
                self.redirect("/admin", f"Removed completion {removed['public_id']}.")
            else:
                self.redirect("/admin", "Completion record could not be removed.")
            return
        if parsed.path.startswith("/admin/history/edit/"):
            history_id = self.extract_id(parsed.path, "/admin/history/edit/")
            if not self.admin_mode_enabled():
                self.redirect("/admin", "Unlock admin mode before editing completion history.")
                return
            errors, updated = update_completion_history_item(history_id, fields) if history_id else (["Completion record not found."], None)
            if errors:
                self.redirect("/admin", " ".join(errors))
            else:
                self.redirect("/admin", f"Updated completion {updated['public_id']}.")
            return
        if parsed.path.startswith("/admin/task/reopen/"):
            task_id = self.extract_id(parsed.path, "/admin/task/reopen/")
            task = get_enriched_task(task_id) if task_id else None
            if not self.admin_mode_enabled():
                self.redirect("/admin", "Unlock admin mode before reopening maintenance items.")
                return
            if not task:
                self.redirect("/admin", "Maintenance item not found.")
                return
            if not task_reopen_confirmed(fields, task):
                self.redirect("/admin", f"Type REOPEN {task['public_id']} to reopen that maintenance item.")
                return
            reopened = reopen_task(task_id)
            self.redirect("/admin", f"Reopened maintenance item {task['public_id']}.") if reopened else self.redirect("/admin", "Maintenance item could not be reopened.")
            return
        if parsed.path.startswith("/admin/todo/reopen/"):
            project_id = self.extract_id(parsed.path, "/admin/todo/reopen/")
            todo = get_enriched_todo(project_id) if project_id else None
            if not self.admin_mode_enabled():
                self.redirect("/admin", "Unlock admin mode before reopening house todos.")
                return
            if not todo:
                self.redirect("/admin", "House todo not found.")
                return
            if not todo_reopen_confirmed(fields, todo):
                self.redirect("/admin", f"Type REOPEN {todo['public_id']} to reopen that house todo.")
                return
            reopened = reopen_todo(project_id)
            self.redirect("/admin", f"Reopened house todo {todo['public_id']}.") if reopened else self.redirect("/admin", "House todo could not be reopened.")
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
            closure_type = clean_closure_type(fields.get("closure_type", "done"))
            if task_id and complete_task(task_id, closure_type, fields.get("closure_notes", "")):
                self.redirect(return_to, f"Maintenance item closed as {CLOSURE_LABELS[closure_type].lower()}.")
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
            if not delete_confirmed(fields):
                confirm_path = f"/delete/{task_id}?return_to={quote(return_to, safe='')}" if task_id else "/"
                self.redirect(confirm_path, "Type DELETE to confirm deletion.")
                return
            if task_id:
                delete_task(task_id)
            self.redirect(return_to if return_to != f"/item/{task_id}" else "/items", "Maintenance item deleted.")
            return
        self.respond_not_found()

    def read_form(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid content length.") from error
        if length < 0:
            raise ValueError("Invalid content length.")
        if length > MAX_FORM_BYTES:
            raise ValueError("Form payload is too large.")
        try:
            raw = self.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Form payload must be UTF-8.") from error
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid content length.") from error
        if length < 0:
            raise ValueError("Invalid content length.")
        if length > MAX_FORM_BYTES:
            raise ValueError("JSON payload is too large.")
        try:
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw or "{}")
        except UnicodeDecodeError as error:
            raise ValueError("JSON payload must be UTF-8.") from error
        except json.JSONDecodeError as error:
            raise ValueError("JSON payload must be valid.") from error
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object.")
        return payload

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
        if morsel and valid_csrf_token_value(morsel.value):
            return morsel.value
        return secrets.token_urlsafe(32)

    def valid_csrf(self, submitted):
        cookie = SimpleCookie(self.headers.get("Cookie"))
        morsel = cookie.get(CSRF_COOKIE)
        if not morsel or not submitted or not valid_csrf_token_value(morsel.value):
            return False
        return secrets.compare_digest(morsel.value, submitted)

    def valid_api_action_token(self):
        if not API_ACTION_TOKEN or not valid_csrf_token_value(API_ACTION_TOKEN):
            return False
        token = self.headers.get("X-MxTracker-Token", "").strip()
        if not token:
            authorization = self.headers.get("Authorization", "")
            if authorization.startswith("Bearer "):
                token = authorization.removeprefix("Bearer ").strip()
        return valid_csrf_token_value(token) and secrets.compare_digest(API_ACTION_TOKEN, token)

    def current_theme(self):
        cookie = SimpleCookie(self.headers.get("Cookie"))
        morsel = cookie.get(THEME_COOKIE)
        return safe_theme(morsel.value if morsel else "system")

    def current_admin_token(self):
        cookie = SimpleCookie(self.headers.get("Cookie"))
        morsel = cookie.get(ADMIN_COOKIE)
        return morsel.value if morsel else ""

    def admin_mode_enabled(self):
        return admin_session_active(self.current_admin_token())

    def clear_current_admin_session(self):
        clear_admin_session(self.current_admin_token())

    def set_security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'self'")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")

    def set_csrf_cookie(self, csrf_token):
        self.send_header("Set-Cookie", f"{CSRF_COOKIE}={csrf_token}; HttpOnly; SameSite=Lax; Path={self.base_path() or '/'}")

    def set_theme_cookie(self, theme):
        self.send_header("Set-Cookie", f"{THEME_COOKIE}={safe_theme(theme)}; HttpOnly; SameSite=Lax; Path={self.base_path() or '/'}")

    def set_admin_cookie(self, token):
        self.send_header("Set-Cookie", f"{ADMIN_COOKIE}={token}; HttpOnly; SameSite=Lax; Path={self.base_path() or '/'}; Max-Age={ADMIN_SESSION_SECONDS}")

    def clear_admin_cookie(self):
        self.send_header("Set-Cookie", f"{ADMIN_COOKIE}=; HttpOnly; SameSite=Lax; Path={self.base_path() or '/'}; Max-Age=0")

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

    def redirect_with_admin_cookie(self, path, notice="", token="", clear=False):
        location = app_url(path, self.base_path())
        if notice:
            separator = "&" if "?" in location else "?"
            location = f"{location}{separator}notice={quote(notice)}"
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.set_security_headers()
        if clear:
            self.clear_admin_cookie()
        elif token:
            self.set_admin_cookie(token)
        self.end_headers()

    def respond_not_found(self):
        self.respond_text("Not found.", HTTPStatus.NOT_FOUND)


def main():
    global HA_PUBLISHER
    init_db()
    if SEED_DEMO_DATA:
        seeded = seed_demo_data()
        print("Demo data seeded." if seeded else "Demo data skipped: database already has items.")
    HA_PUBLISHER = HomeAssistantPublisher()
    HA_PUBLISHER.start()
    server = HTTPServer((HOST, PORT), MaintenanceHandler)
    print(f"Home Maintenance Tracker listening on {HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
