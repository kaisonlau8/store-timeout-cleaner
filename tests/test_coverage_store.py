"""拉群覆盖：从门店下钻到尚未拉入机器人的群名。"""

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeout_store import connect, replace_context

ROWS = [
    {"group_id": "r1", "group_name": "X700-VIP 车主群A", "store": "猛士汽车武汉门店", "theater": "武汉特区"},
    {"group_id": "r2", "group_name": "x700-vip 车主群B", "store": "猛士汽车武汉门店", "theater": "武汉特区"},
    {"group_id": "r3", "group_name": "X700 交付群", "store": "猛士汽车上海门店", "theater": "华东战区"},
    {"group_id": "r4", "group_name": "x700 服务群", "store": "猛士汽车上海门店", "theater": "华东战区"},
    {"group_id": "r5", "group_name": "X700 群主未归店", "store": "", "theater": ""},
    {"group_id": "r6", "group_name": "VIP 保养群", "store": "猛士汽车北京门店", "theater": "华北战区"},
    {"group_id": "r7", "group_name": "X700 无战区", "store": "猛士汽车广州门店", "theater": ""},
    {"group_id": "r8", "group_name": "混战区售后群", "store": "猛士汽车杭州门店", "theater": "华东战区"},
    {"group_id": "r9", "group_name": "混战区零售群", "store": "猛士汽车杭州门店", "theater": "华东战区"},
    {"group_id": "r10", "group_name": "混战区少量群", "store": "猛士汽车杭州门店", "theater": "华北战区"},
]
ROBOT_IDS = ["r1", "r3", "r6", "r8"]


def _seed(db_path: str) -> None:
    conn = connect(db_path)
    replace_context(conn, robot_chat_ids=ROBOT_IDS, employee_ids=["u1"], group_rows=ROWS)
    conn.commit()
    conn.close()


def _minimal_stats() -> dict:
    empty = {"group_count": 0, "store_count": 0, "store_stats": {}}
    return {
        "updated_at": "2026-10-15 00:00:00",
        "summary": {
            "all_total": 2,
            "all_valid": 2,
            "all_invalid": 0,
            "filter_total": 1,
            "filter_valid": 1,
            "filter_invalid": 0,
            "source": "test",
        },
        "stats": {
            "national": {
                "all": {
                    "group_count": 2,
                    "store_count": 1,
                    "store_stats": {"猛士汽车武汉门店": 2},
                },
                "filter": {
                    "group_count": 1,
                    "store_count": 1,
                    "store_stats": {"猛士汽车武汉门店": 1},
                },
            },
            "after_may1": {"all": empty, "filter": empty},
            "monthly": {"months": [], "years": ["2026"], "data": {}},
        },
    }


class CoverageQueryTests(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        replace_context(self.conn, robot_chat_ids=ROBOT_IDS, employee_ids=["u1"], group_rows=ROWS)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_majority_theater(self):
        from coverage_query import store_coverage_detail

        detail = store_coverage_detail(self.conn, "猛士汽车杭州门店")
        self.assertEqual(detail["theater"], "华东战区")
        self.assertEqual(detail["group_count"], 3)
        self.assertEqual(detail["robot_count"], 1)
        self.assertEqual(detail["pending_count"], 2)

    def test_detail_splits_groups_without_robot(self):
        from coverage_query import store_coverage_detail

        detail = store_coverage_detail(self.conn, "猛士汽车武汉门店")
        self.assertEqual(detail["store"], "猛士汽车武汉门店")
        self.assertEqual(detail["theater"], "武汉特区")
        self.assertEqual(detail["group_count"], 2)
        self.assertEqual(detail["robot_count"], 1)
        self.assertEqual([row["group_name"] for row in detail["pending"]], ["x700-vip 车主群B"])
        self.assertEqual([row["group_name"] for row in detail["robot"]], ["X700-VIP 车主群A"])
        self.assertTrue(all(not row["in_robot"] for row in detail["pending"]))

    def test_padded_store_name_still_lists_pending_group(self):
        from coverage_query import store_coverage_detail

        self.conn.execute(
            "UPDATE chat_store SET store = ? WHERE room_id = ?",
            ("  猛士汽车武汉门店  ", "r2"),
        )
        self.conn.commit()
        detail = store_coverage_detail(self.conn, "猛士汽车武汉门店")
        self.assertEqual(detail["pending_count"], 1)
        self.assertEqual([row["group_name"] for row in detail["pending"]], ["x700-vip 车主群B"])

    def test_unknown_or_blank_store(self):
        from coverage_query import store_coverage_detail

        self.assertIsNone(store_coverage_detail(self.conn, "不存在的门店"))
        self.assertIsNone(store_coverage_detail(self.conn, "   "))
        self.assertIsNone(store_coverage_detail(self.conn, ""))


class CoveragePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmp.name, "timeout.db")
        data_file = os.path.join(cls.tmp.name, "data.json")
        _seed(db_path)
        with open(data_file, "w", encoding="utf-8") as fh:
            json.dump(_minimal_stats(), fh)
        os.environ["CLEANER_DB_PATH"] = db_path
        os.environ["CLEANER_DATA_FILE"] = data_file
        os.environ.pop("MHERO_AUTH", None)
        os.environ.pop("MHERO_AUTH_CLEANER", None)
        import app as app_module

        cls.app_module = importlib.reload(app_module)
        cls.client = cls.app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        os.environ.pop("CLEANER_DB_PATH", None)
        os.environ.pop("CLEANER_DATA_FILE", None)

    def test_index_store_list_keeps_filters_in_links(self):
        resp = self.client.get("/?theater=%E6%AD%A6%E6%B1%89%E7%89%B9%E5%8C%BA&q=%E6%AD%A6%E6%B1%89")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('name="theater"', html)
        self.assertIn('name="q"', html)
        self.assertIn("_nav.html", Path(__file__).resolve().parents[1].joinpath("templates/index.html").read_text(encoding="utf-8"))
        href = "/coverage/store?" + urlencode(
            {"name": "猛士汽车武汉门店", "theater": "武汉特区", "q": "武汉"}
        )
        self.assertIn(href.replace("&", "&amp;"), html)
        table = html.split('data-coverage="national-all"', 1)[1].split("</table>", 1)[0]
        self.assertIn("猛士汽车武汉门店", table)
        self.assertNotIn("猛士汽车上海门店", table)

    def test_detail_lists_unroboted_group_names(self):
        query = urlencode({"name": "猛士汽车武汉门店", "theater": "武汉特区", "q": "武汉"})
        resp = self.client.get("/coverage/store?" + query)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("猛士汽车武汉门店", html)
        self.assertIn("武汉特区", html)
        pending = html.split('id="pending-groups"', 1)[1].split('id="robot-groups"', 1)[0]
        robot = html.split('id="robot-groups"', 1)[1]
        self.assertIn("未拉机器人的群", pending)
        self.assertIn("x700-vip 车主群B", pending)
        self.assertNotIn("X700-VIP 车主群A", pending)
        self.assertIn("已拉机器人的群", robot)
        self.assertIn("X700-VIP 车主群A", robot)
        back = "/?" + urlencode({"theater": "武汉特区", "q": "武汉"})
        self.assertIn(back.replace("&", "&amp;"), html)

    def test_unknown_store_page(self):
        resp = self.client.get("/coverage/store?name=" + "不存在的门店")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("没有这家门店", resp.get_data(as_text=True))

    def test_monthly_script_preserves_query(self):
        script = Path(__file__).resolve().parents[1].joinpath("static/js/display.js").read_text(encoding="utf-8")
        self.assertIn("/coverage/store?", script)
        self.assertIn("get('theater')", script)
        self.assertIn("get('q')", script)
