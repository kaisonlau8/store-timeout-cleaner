import os
import tempfile
import unittest

from flask import Flask

from date_ranges import parse_range
from score_web import _view_report, register_score
from tests.test_score_query import _seed


class ScorePageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        _seed(self.root)
        self.app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
        )
        self.app.jinja_env.globals["ds_css"] = lambda: ""
        self.app.jinja_env.globals["ds_sprite"] = lambda: ""
        self.app.jinja_env.globals["ds_cards"] = lambda: ""
        register_score(self.app, self.root)
        self.client = self.app.test_client()
        self._old_key = os.environ.pop("DEEPSEEK_API_KEY", None)

    def tearDown(self):
        if self._old_key is not None:
            os.environ["DEEPSEEK_API_KEY"] = self._old_key
        elif "DEEPSEEK_API_KEY" in os.environ:
            del os.environ["DEEPSEEK_API_KEY"]
        self.tmp.cleanup()

    def test_default_range_is_current_month(self):
        page = self.client.get("/score")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        start, end = parse_range()
        self.assertIn(start.isoformat(), html)
        self.assertIn(end.isoformat(), html)
        detail = self.client.get('/score/store?name=甲店')
        self.assertEqual(detail.status_code, 200)
        self.assertIn('value="'+str(start)+'"', detail.text)
        self.assertIn('value="'+str(end)+'"', detail.text)

    def test_list_export_and_detail(self):
        page = self.client.get("/score?start=2026-07-01&end=2026-09-30")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("甲店", html)
        self.assertIn("未接入机器人", html)
        self.assertIn("导出 CSV", html)
        self.assertIn("导出 Excel", html)

        csv_resp = self.client.get("/api/score/export.csv?start=2026-07-01&end=2026-09-30")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("甲店", csv_resp.get_data(as_text=True))

        xlsx = self.client.get("/api/score/export.xlsx?start=2026-07-01&end=2026-09-30")
        self.assertEqual(xlsx.status_code, 200)
        self.assertIn("spreadsheet", xlsx.mimetype)

        detail = self.client.get("/score/store?name=甲店&start=2026-07-01&end=2026-09-30")
        self.assertEqual(detail.status_code, 200)
        body = detail.get_data(as_text=True)
        self.assertIn("刹车有异响", body)
        self.assertIn("寒暄", body)
        self.assertIn("剔除", body)

    def test_region_and_store_filters_keep_range(self):
        page = self.client.get("/score?start=2026-07-01&end=2026-09-30&theater=华北战区&q=未接入&foo=keep")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("未接入店", html)
        self.assertNotIn("甲店", html)
        self.assertIn("华北战区", html)
        self.assertIn("华东战区", html)
        self.assertIn('value="2026-07-01"', html)
        self.assertIn('value="2026-09-30"', html)
        self.assertIn('name="foo"', html)
        self.assertIn('value="keep"', html)
        self.assertIn("门店名称或编码", html)
        self.assertIn("当前筛选", html)

        csv_resp = self.client.get("/api/score/export.csv?start=2026-07-01&end=2026-09-30&q=甲")
        text = csv_resp.get_data(as_text=True)
        self.assertIn("甲店", text)
        self.assertNotIn("未接入店", text)

    def test_code_matches_with_theater(self):
        report = {
            "stores": [
                {"name": "甲店", "code": "S001", "theater": "华东战区", "score": 100},
                {"name": "乙店", "code": "S002", "theater": "华东战区", "score": 80},
                {"name": "丙店", "code": "S001", "theater": "华北战区", "score": 60},
            ]
        }
        view = _view_report(report, "华东战区", "s001")
        self.assertEqual([row["name"] for row in view["stores"]], ["甲店"])
        self.assertEqual(view["theaters"], ["华东战区", "华北战区"])


if __name__ == "__main__":
    unittest.main()
