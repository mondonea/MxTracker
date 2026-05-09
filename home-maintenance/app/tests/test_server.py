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
        self.assertIn("<span>Times completed</span>1", html)
        self.assertIn("<span>Created</span>", html)
        self.assertIn("<span>Updated</span>", html)
        self.assertIn("Completion History", html)
        self.assertIn(today.isoformat(), html)
        self.assertIn('action="/complete/1"', html)
        self.assertIn('action="/snooze/1"', html)
        self.assertIn('href="/edit/1"', html)

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


if __name__ == "__main__":
    unittest.main()
