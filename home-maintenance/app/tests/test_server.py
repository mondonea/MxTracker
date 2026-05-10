import importlib.util
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("mxtracker_server", SERVER_PATH)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class MaintenanceServerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        server.DB_PATH = str(Path(self.tempdir.name) / "home-maintenance.db")
        server.HA_PUBLISHER = None
        server.init_db()

    def tearDown(self):
        self.tempdir.cleanup()

    def add_task(self, name, next_due_on, category="General", notes=""):
        server.save_task(
            {
                "name": name,
                "category": category,
                "notes": notes,
                "ha_area_id": "",
                "ha_area_name": "",
                "interval_count": 3,
                "interval_unit": "months",
                "next_due_on": next_due_on.isoformat(),
            }
        )

    def test_due_14_sensor_uses_supervisor_ingress_links_and_filters_later_items(self):
        server.supervisor_get_json = lambda path: {
            "ingress_url": "/api/hassio_ingress/sessionabc",
            "slug": "0b3ee7ba_home_maintenance",
        }
        self.assertTrue(server.refresh_homeassistant_app_info())

        today = date.today()
        self.add_task("Replace AC filter", today - timedelta(days=1), "HVAC", "private note")
        self.add_task("Clean dryer vent", today + timedelta(days=14), "Safety")
        self.add_task("Clean gutters", today + timedelta(days=15), "Exterior")

        payload = server.homeassistant_state_payloads()["sensor.mxtracker_due_14_days"]
        items = payload["attributes"]["items"]
        table = payload["attributes"]["markdown_table"]

        self.assertEqual(payload["state"], "2")
        self.assertEqual([item["name"] for item in items], ["Replace AC filter", "Clean dryer vent"])
        self.assertEqual(items[0]["detail_url"], "/api/hassio_ingress/sessionabc/?mx_item=1")
        self.assertIn("[Replace AC filter](/api/hassio_ingress/sessionabc/?mx_item=1)", table)
        self.assertIn('style="color: var(--error-color); font-weight: 700;"', table)
        self.assertNotIn("/hassio/ingress/", table)
        self.assertNotIn("Clean gutters", table)
        self.assertNotIn("private note", str(payload))

    def test_dashboard_table_escapes_task_names_and_csv_export_hardens_formula_cells(self):
        server.set_setting("supervisor_ingress_url", "/api/hassio_ingress/sessionabc")
        today = date.today()
        self.add_task("<script>alert(1)</script>|Filter", today, "HVAC", "=private note")

        payload = server.homeassistant_state_payloads()["sensor.mxtracker_due_14_days"]
        table = payload["attributes"]["markdown_table"]

        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;&#124;Filter", table)
        self.assertNotIn("<script>alert(1)</script>", table)
        self.assertEqual(server.csv_cell(" =2+2"), "' =2+2")
        self.assertEqual(server.csv_cell("\t=2+2"), "'\t=2+2")

    def test_security_helpers_reject_unsafe_ingress_paths_and_csrf_values(self):
        self.assertEqual(server.normalize_base_path("/api/hassio_ingress/sessionabc"), "/api/hassio_ingress/sessionabc")
        self.assertEqual(server.normalize_base_path("/api/hassio_ingress/sessionabc?x=1"), "")
        self.assertEqual(server.normalize_base_path("/api/hassio_ingress/sessionabc; Secure"), "")
        self.assertTrue(server.valid_csrf_token_value("a" * 32))
        self.assertFalse(server.valid_csrf_token_value("a" * 31))
        self.assertFalse(server.valid_csrf_token_value("a" * 32 + "<"))

    def test_query_string_item_route_renders_specific_detail_page_with_history(self):
        today = date.today()
        self.add_task("Replace AC filter", today, "HVAC", "Use 20x25x1 filter.")
        task = server.get_tasks()[0]
        self.assertTrue(server.complete_task(task["id"]))

        detail_task = server.get_enriched_task(task["id"])
        html = server.render_item_detail(detail_task, "csrf-token")

        self.assertIn("<title>Replace AC filter</title>", html)
        self.assertIn("<h2>Replace AC filter</h2>", html)
        self.assertIn("Use 20x25x1 filter.", html)
        self.assertIn("<span>Category</span>HVAC", html)
        self.assertIn("<span>Home Assistant area</span>Unassigned", html)
        self.assertIn("<span>Times completed</span>1", html)
        self.assertIn("<span>Created</span>", html)
        self.assertIn("<span>Updated</span>", html)
        self.assertIn("Completion History", html)
        self.assertIn(today.isoformat(), html)
        self.assertIn('action="/complete/1"', html)
        self.assertIn('action="/snooze/1"', html)
        self.assertIn('href="/edit/1"', html)
        self.assertIn('href="/delete/1?return_to=%2Fitem%2F1"', html)

    def test_root_query_item_selection_uses_item_detail_renderer(self):
        today = date.today()
        self.add_task("Clean dishwasher", today, "Appliances", "Run cleaning cycle.")
        task = server.get_tasks()[0]

        query = {"mx_item": [str(task["id"])]}
        html = server.render_root_page(query, "csrf-token")

        self.assertIn("<title>Clean dishwasher</title>", html)
        self.assertIn("<h2>Clean dishwasher</h2>", html)
        self.assertIn("Run cleaning cycle.", html)
        self.assertIn("Completion History", html)

    def test_dashboard_hides_delete_and_only_colors_nonempty_overdue_section(self):
        today = date.today()
        self.add_task("Clean dishwasher", today + timedelta(days=2), "Appliances")

        html = server.render_dashboard("csrf-token")

        self.assertIn("Nothing overdue", html)
        self.assertNotIn('<section class="panel table-panel overdue-section">', html)
        self.assertNotIn('href="/delete/', html)
        self.assertIn('class="button secondary nav-link active" href="/" aria-current="page"', html)

        self.add_task("Replace AC filter", today - timedelta(days=1), "HVAC")
        html = server.render_dashboard("csrf-token")

        self.assertIn('<section class="panel table-panel overdue-section">', html)
        self.assertNotIn('href="/delete/', html)

    def test_calendar_view_renders_month_tasks_and_active_nav(self):
        today = date.today()
        self.add_task("Test smoke detectors", today, "Safety")

        html = server.render_calendar_view("csrf-token", {"month": [today.strftime("%Y-%m")]})

        self.assertIn("<title>Maintenance Calendar</title>", html)
        self.assertIn(today.strftime("%B %Y"), html)
        self.assertIn("Test smoke detectors", html)
        self.assertIn('href="/item/1"', html)
        self.assertIn('href="/calendar" aria-current="page"', html)

    def test_all_items_view_has_active_nav_and_area_column(self):
        today = date.today()
        server.replace_homeassistant_areas([{"id": "kitchen", "name": "Kitchen"}])
        errors, cleaned = server.validate_task_form(
            {
                "name": "Clean dishwasher",
                "category": "Appliances",
                "notes": "",
                "ha_area_id": "kitchen",
                "interval_count": "1",
                "interval_unit": "months",
                "next_due_on": today.isoformat(),
            }
        )
        self.assertEqual(errors, [])
        server.save_task(cleaned)

        html = server.render_items_audit("csrf-token")

        self.assertIn('class="button secondary nav-link active" href="/items" aria-current="page"', html)
        self.assertIn("<th>Area</th>", html)
        self.assertIn("<td data-label=\"Area\">Kitchen</td>", html)

    def test_homeassistant_area_sync_validation_and_sensor_payloads(self):
        self.assertIsNone(server.clean_ha_area_record({"id": None, "name": "Kitchen"}))
        self.assertIsNone(server.clean_ha_area_record({"id": "bad area", "name": "Kitchen"}))

        original_renderer = server.render_homeassistant_template
        try:
            server.render_homeassistant_template = lambda template: '[{"id":"kitchen","name":"Kitchen"},{"id":"garage","name":"Garage"}]'
            self.assertTrue(server.refresh_homeassistant_areas())
        finally:
            server.render_homeassistant_template = original_renderer

        today = date.today()
        form = {
            "name": "Clean dishwasher",
            "category": "Appliances",
            "notes": "Run cleaning cycle.",
            "ha_area_id": "kitchen",
            "interval_count": "1",
            "interval_unit": "months",
            "next_due_on": today.isoformat(),
        }
        errors, cleaned = server.validate_task_form(form)
        self.assertEqual(errors, [])
        self.assertEqual(cleaned["ha_area_name"], "Kitchen")
        server.save_task(cleaned)

        task = server.get_tasks()[0]
        self.assertEqual(task["ha_area_id"], "kitchen")
        self.assertEqual(task["ha_area_name"], "Kitchen")

        item = server.homeassistant_state_payloads()["sensor.mxtracker_all_items"]["attributes"]["items"][0]
        self.assertEqual(item["ha_area_id"], "kitchen")
        self.assertEqual(item["ha_area_name"], "Kitchen")
        self.assertIn("Kitchen", server.tasks_csv())

        invalid = dict(form)
        invalid["ha_area_id"] = "not synced"
        errors, _ = server.validate_task_form(invalid)
        self.assertIn("Choose a valid Home Assistant area.", errors)

    def test_init_db_migrates_homeassistant_area_columns_and_cache_table(self):
        legacy_path = Path(self.tempdir.name) / "legacy-home-maintenance.db"
        server.DB_PATH = str(legacy_path)
        with server.connect_db() as conn:
            conn.execute(
                """
                CREATE TABLE tasks (
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

        server.init_db()

        with server.connect_db() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            area_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ha_areas'"
            ).fetchone()
        self.assertIn("ha_area_id", columns)
        self.assertIn("ha_area_name", columns)
        self.assertIsNotNone(area_table)

    def test_house_todo_scoring_readiness_and_detail_checklist(self):
        server.replace_homeassistant_areas([{"id": "bathroom", "name": "Bathroom"}])
        errors, cleaned = server.validate_todo_form(
            {
                "title": "Fix running toilet",
                "category": "Plumbing",
                "description": "Tank keeps running after flush.",
                "ha_area_id": "bathroom",
                "likelihood": "5",
                "consequence": "3",
                "urgency": "4",
                "effort": "2",
                "cost": "1",
                "status": "planning",
                "target_on": date.today().isoformat(),
            }
        )
        self.assertEqual(errors, [])
        project_id = server.save_todo(cleaned)
        self.assertTrue(server.add_todo_checklist_item(project_id, "Buy flapper", required_for_start=True))
        self.assertTrue(server.add_todo_checklist_item(project_id, "Turn off water"))

        todo = server.get_enriched_todo(project_id)
        self.assertEqual(todo["derived_status"], "planning")
        self.assertFalse(todo["ready_to_start"])
        self.assertEqual(todo["hazard_score"], 15)
        self.assertGreater(todo["priority_score"], 20)

        gate = [item for item in server.get_todo_checklist(project_id) if item["required_for_start"]][0]
        self.assertEqual(server.toggle_todo_checklist_item(gate["id"]), project_id)
        todo = server.get_enriched_todo(project_id)

        self.assertEqual(todo["derived_status"], "ready")
        self.assertTrue(todo["ready_to_start"])
        self.assertEqual(todo["progress_percent"], 50)

        html = server.render_todo_detail(server.get_todo(project_id), "csrf-token")
        self.assertIn("<title>Fix running toilet</title>", html)
        self.assertIn("<span>Status</span>Ready", html)
        self.assertIn("<span>Ready gate</span>1/1", html)
        self.assertIn("Buy flapper", html)
        self.assertIn("Start gate", html)
        self.assertIn('action="/todo/1/checklist"', html)

    def test_house_todo_dashboard_orders_by_status_and_score(self):
        server.save_todo(
            {
                "title": "Patch paint chip",
                "category": "Interior",
                "description": "",
                "ha_area_id": "",
                "ha_area_name": "",
                "likelihood": 5,
                "consequence": 1,
                "urgency": 1,
                "effort": 1,
                "cost": 1,
                "status": "backlog",
                "target_on": "",
            }
        )
        risky_id = server.save_todo(
            {
                "title": "Replace sparking outlet",
                "category": "Electrical",
                "description": "",
                "ha_area_id": "",
                "ha_area_name": "",
                "likelihood": 4,
                "consequence": 5,
                "urgency": 5,
                "effort": 3,
                "cost": 2,
                "status": "planning",
                "target_on": "",
            }
        )
        server.add_todo_checklist_item(risky_id, "Find breaker", required_for_start=True)
        gate = server.get_todo_checklist(risky_id)[0]
        server.toggle_todo_checklist_item(gate["id"])

        todos = server.get_todos()
        self.assertEqual(todos[0]["title"], "Replace sparking outlet")
        self.assertEqual(todos[0]["derived_status"], "ready")
        self.assertEqual(todos[1]["title"], "Patch paint chip")

        html = server.render_todos_view("csrf-token")
        self.assertIn("<title>House Todos</title>", html)
        self.assertIn("Risk Map", html)
        self.assertIn("Replace sparking outlet", html)
        self.assertIn("Patch paint chip", html)
        self.assertIn('href="/todo/new"', html)

    def test_house_todo_homeassistant_sensor_payload_and_api_public_shape(self):
        project_id = server.save_todo(
            {
                "title": "Replace kitchen faucet",
                "category": "Plumbing",
                "description": "Old faucet leaks at the handle.",
                "ha_area_id": "",
                "ha_area_name": "",
                "likelihood": 4,
                "consequence": 4,
                "urgency": 3,
                "effort": 3,
                "cost": 3,
                "status": "in_work",
                "target_on": "",
            }
        )
        server.add_todo_checklist_item(project_id, "Measure sink holes")

        payload = server.homeassistant_state_payloads()["sensor.mxtracker_house_todos"]
        item = payload["attributes"]["items"][0]

        self.assertEqual(payload["state"], "1")
        self.assertEqual(payload["attributes"]["in_work_count"], 1)
        self.assertEqual(item["title"], "Replace kitchen faucet")
        self.assertEqual(item["detail_url"], "/todo/1")
        self.assertIn("[Replace kitchen faucet](/todo/1)", payload["attributes"]["markdown_table"])

        public = server.public_todo(server.get_enriched_todo(project_id))
        self.assertEqual(public["status"], "in_work")
        self.assertIn("priority_score", public)

    def test_demo_seed_data_is_realistic_and_idempotent(self):
        self.assertTrue(server.seed_demo_data())
        self.assertFalse(server.seed_demo_data())

        tasks = server.get_tasks()
        todos = server.get_todos()
        titles = [todo["title"] for todo in todos]

        self.assertEqual(len(tasks), 2)
        self.assertEqual(len(todos), 5)
        self.assertIn("Replace sparking outlet", titles)
        self.assertIn("Investigate garage ceiling stain", titles)
        self.assertEqual(server.get_setting("demo_seeded_at")[:4], str(date.today().year))

        faucet = next(todo for todo in todos if todo["title"] == "Replace kitchen faucet")
        self.assertEqual(faucet["derived_status"], "in_work")
        self.assertGreater(faucet["progress_percent"], 0)

        dashboard = server.render_todos_view("csrf-token")
        self.assertIn("Fix running toilet", dashboard)
        self.assertIn("Risk Map", dashboard)
        self.assertIn("Ready to start", dashboard)

        payload = server.homeassistant_state_payloads()["sensor.mxtracker_house_todos"]
        self.assertEqual(payload["state"], "5")
        self.assertLessEqual(len(payload["attributes"]["items"]), 10)


if __name__ == "__main__":
    unittest.main()
