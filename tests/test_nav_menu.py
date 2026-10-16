"""共享主导航：桌面横排不变，窄屏变成带进出场动画的汉堡抽屉。"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = (
    "templates/index.html",
    "templates/timeout.html",
    "templates/groups.html",
    "templates/score.html",
    "templates/score_detail.html",
)


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class NavMenuTests(unittest.TestCase):
    def test_pages_include_shared_nav(self):
        expected = {
            "templates/index.html": "coverage",
            "templates/timeout.html": "timeout",
            "templates/groups.html": "groups",
            "templates/score.html": "score",
            "templates/score_detail.html": "score",
        }
        for name in PAGES:
            html = read(name)
            self.assertIn("_nav.html", html)
            self.assertIn("nav_active = '%s'" % expected[name], html)
            self.assertNotIn('class="page-nav"', html)

    def test_partial_keeps_chinese_labels_and_links(self):
        html = read("templates/_nav.html")
        for href, label in (
            ('href="/"', "拉群覆盖"),
            ('href="/timeout"', "超时提醒"),
            ('href="/groups"', "群名统计"),
            ('href="/score"', "超时评分"),
        ):
            self.assertIn(href, html)
            self.assertIn(label, html)
        self.assertIn("打开菜单", html)
        self.assertIn("关闭菜单", html)
        self.assertIn("菜单", html)
        self.assertIn('aria-label="主导航"', html)

    def test_css_breakpoint_and_enter_exit_motion(self):
        css = read("static/css/style.css")
        mobile = css.split("@media (max-width: 860px)", 1)[1]
        self.assertIn("translateX(100%)", mobile)
        self.assertIn("translateX(0)", mobile)
        self.assertIn("cubic-bezier(0.7, 0.3, 0.1, 1)", mobile)
        self.assertIn(".nav-mask", mobile)
        self.assertIn("visibility 0s linear 0.3s", mobile)
        self.assertIn("visibility 0s linear 0s", mobile)
        self.assertIn("prefers-reduced-motion: reduce", css)
        burger = css.split(".nav-burger,", 1)[0]
        self.assertNotIn("max-width: 860px", burger[-400:])


if __name__ == "__main__":
    unittest.main()
