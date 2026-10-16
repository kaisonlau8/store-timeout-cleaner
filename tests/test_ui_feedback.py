"""控制台页面接上 Ant Design 交互反馈，且没有改动飞书或环境文件。"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class FeedbackSurfaceTests(unittest.TestCase):
    def test_pages_include_feedback_assets(self):
        for name in ("templates/index.html", "templates/timeout.html"):
            html = read(name)
            self.assertIn("css/antd-feedback.css", html)
            self.assertIn("js/antd-feedback.js", html)

    def test_export_links_opt_in(self):
        index = read("templates/index.html")
        self.assertEqual(index.count("data-antd-export"), 5)
        timeout = read("templates/timeout.html")
        self.assertIn('id="export-link" data-antd-export', timeout)
        self.assertIn('id="import-progress"', timeout)
        self.assertIn('id="timeout-results"', timeout)
        self.assertIn('data-loading-text="查询中"', timeout)
        self.assertIn('data-loading-text="上传中"', timeout)

    def test_feedback_css_uses_antd_patterns(self):
        css = read("static/css/antd-feedback.css")
        for token in (
            ".ant-btn-loading",
            ".ant-spin-dot",
            ".ant-progress",
            ".ant-message",
            ".ant-notification",
            "#1677ff",
        ):
            self.assertIn(token, css)
        self.assertNotIn("antd.min.css", css)

    def test_feedback_script_api(self):
        js = read("static/js/antd-feedback.js")
        for name in ("message", "notification", "setButtonLoading", "setSpin", "upload"):
            self.assertIn(name, js)
        timeout = read("static/js/timeout.js")
        self.assertIn("AntdFeedback.setSpin", timeout)
        self.assertIn("AntdFeedback.upload", timeout)
        self.assertIn("AntdFeedback.notification", timeout)
        self.assertNotIn("feishu", timeout.lower())

    def test_no_login_page_invented(self):
        names = [p.name for p in (ROOT / "templates").glob("*.html")]
        self.assertFalse([n for n in names if "login" in n])


if __name__ == "__main__":
    unittest.main()
