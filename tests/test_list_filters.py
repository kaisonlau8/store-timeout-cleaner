import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ensure_auth_stub():
    try:
        import mhero_auth  # noqa: F401
        return
    except ImportError:
        pass
    pkg = types.ModuleType("mhero_auth")
    adapter = types.ModuleType("mhero_auth.flask_adapter")

    def init_app(app, *args, **kwargs):
        return None

    adapter.init_app = init_app
    pkg.flask_adapter = adapter
    sys.modules["mhero_auth"] = pkg
    sys.modules["mhero_auth.flask_adapter"] = adapter


_ensure_auth_stub()

from list_filters import row_visible, store_query_match
from timeout_query import query_chart
from timeout_store import connect


class StoreQueryTests(unittest.TestCase):
    def test_name_or_code_casefold(self):
        self.assertTrue(store_query_match("猛士汽车武汉门店", "", "武汉"))
        self.assertTrue(store_query_match("甲店", "S001", "s001"))
        self.assertFalse(store_query_match("甲店", "S001", "S002"))
        self.assertTrue(store_query_match("甲店", "", ""))

    def test_theater_and_query_together(self):
        rows = [
            {"name": "甲店", "code": "S001", "theater": "华东战区"},
            {"name": "乙店", "code": "S002", "theater": "华东战区"},
            {"name": "丙店", "code": "S009", "theater": "华北战区"},
        ]
        shown = [row["name"] for row in rows if row_visible(row, "华东战区", "S001")]
        self.assertEqual(shown, ["甲店"])


class TimeoutFilterTests(unittest.TestCase):
    def test_theater_and_store_search_together(self):
        with tempfile.TemporaryDirectory() as root:
            conn = connect(os.path.join(root, "timeout.db"))
            rows = [
                ("m1", "甲店", "华东战区", "2026-08-01 09:00:00"),
                ("m2", "甲店分部", "华东战区", "2026-08-02 09:00:00"),
                ("m3", "乙店", "华东战区", "2026-08-03 09:00:00"),
                ("m4", "丙店", "华北战区", "2026-08-04 09:00:00"),
            ]
            for msg_id, store, theater, created_at in rows:
                conn.execute(
                    """
                    INSERT INTO timeout_event(
                      msg_id, rule_id, rule_name, room_id, store, theater, created_at
                    ) VALUES (?, 'r', '规则', 'room', ?, ?, ?)
                    """,
                    (msg_id, store, theater, created_at),
                )
            conn.commit()
            conn.close()

            both = query_chart(root, theater="华东战区", q="甲", start="2026-08-01", end="2026-08-31")
            self.assertEqual(sorted(row["store"] for row in both["stores"]), ["甲店", "甲店分部"])
            self.assertEqual(both["totals"]["stores"], 2)

            exact = query_chart(root, scope="store", ident="乙店", start="2026-08-01", end="2026-08-31")
            self.assertEqual([row["store"] for row in exact["stores"]], ["乙店"])

            legacy = query_chart(root, scope="theater", ident="华北战区", start="2026-08-01", end="2026-08-31")
            self.assertEqual([row["store"] for row in legacy["stores"]], ["丙店"])


class CoveragePageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        db_path = os.path.join(root, "timeout.db")
        data_file = os.path.join(root, "data.json")
        conn = connect(db_path)
        conn.executemany(
            "INSERT INTO chat_store(room_id, store, theater, group_name, in_robot) VALUES (?, ?, ?, ?, ?)",
            [
                ("r1", "甲店", "华东战区", "甲店群1", 1),
                ("r2", "甲店", "华东战区", "甲店群2", 0),
                ("r3", "未接入店", "华北战区", "未接入群", 0),
            ],
        )
        conn.commit()
        conn.close()
        payload = {
            "updated_at": "2026-10-15 00:00:00",
            "summary": {
                "all_total": 3,
                "all_valid": 3,
                "all_invalid": 0,
                "filter_total": 1,
                "filter_valid": 1,
                "filter_invalid": 0,
                "source": "test",
            },
            "stats": {
                "national": {
                    "all": {"group_count": 149, "store_count": 2, "store_stats": {"甲店": 99, "未接入店": 50}},
                    "filter": {"group_count": 99, "store_count": 1, "store_stats": {"甲店": 99}},
                },
                "after_may1": {
                    "all": {"group_count": 7, "store_count": 2, "store_stats": {"甲店": 3, "未接入店": 4}},
                    "filter": {"group_count": 1, "store_count": 1, "store_stats": {"甲店": 1}},
                },
                "monthly": {
                    "months": ["2026-08"],
                    "years": ["2026"],
                    "data": {
                        "2026-08": {
                            "all": {"group_count": 1, "store_count": 1, "store_stats": {"甲店": 1}},
                            "filter": {"group_count": 1, "store_count": 1, "store_stats": {"甲店": 1}},
                        }
                    },
                },
            },
        }
        with open(data_file, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        self._env = {
            "CLEANER_DB_PATH": os.environ.get("CLEANER_DB_PATH"),
            "CLEANER_DATA_FILE": os.environ.get("CLEANER_DATA_FILE"),
        }
        os.environ["CLEANER_DB_PATH"] = db_path
        os.environ["CLEANER_DATA_FILE"] = data_file
        os.environ.pop("MHERO_AUTH", None)
        os.environ.pop("MHERO_AUTH_CLEANER", None)
        import app as app_module

        self.client = app_module.app.test_client()

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _table(self, html, marker):
        return html.split(f'data-coverage="{marker}"', 1)[1].split("</table>", 1)[0]

    def test_store_detail_shows_chat_store_counts(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("全量群数", html)
        self.assertIn("已拉机器人群数", html)
        self.assertIn("门店名称或编码", html)
        self.assertIn("华东战区", html)
        self.assertIn("华北战区", html)
        national = self._table(html, "national-all")
        self.assertIn("甲店", national)
        self.assertIn("未接入店", national)
        self.assertIn(">2<", national)
        self.assertIn(">1<", national)
        robot = self._table(html, "national-robot")
        self.assertIn("甲店", robot)
        self.assertNotIn("未接入店", robot)
        after = self._table(html, "after-all")
        self.assertIn('华东<span class="ds-nowrap">战区</span>', after)
        self.assertIn(">3<", after)
        self.assertIn(">4<", after)

    def test_filters_combine_and_keep_extra_params(self):
        resp = self.client.get("/?theater=华北战区&q=未接入&foo=keep")
        html = resp.get_data(as_text=True)
        national = self._table(html, "national-all")
        self.assertIn("未接入店", national)
        self.assertNotIn("甲店", national)
        self.assertIn('name="foo"', html)
        self.assertIn('value="keep"', html)
        self.assertIn("selected", html)

        empty = self.client.get("/?theater=华东战区&q=未接入")
        empty_html = empty.get_data(as_text=True)
        self.assertIn("当前筛选没有门店", self._table(empty_html, "national-all"))

        exported = self.client.get("/api/export-all?theater=华北战区")
        self.assertEqual(exported.status_code, 200)
        text = exported.get_data(as_text=True)
        self.assertIn("未接入店", text)
        self.assertNotIn("甲店", text)


if __name__ == "__main__":
    unittest.main()
