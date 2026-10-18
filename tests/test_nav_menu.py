"""共享主导航：DS 结构（.menu/.mi/.on），移动端由 cards.js 自动汉堡 + 统一抽屉承接。"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = (
    "templates/index.html",
    "templates/timeout.html",
    "templates/groups.html",
    "templates/score.html",
    "templates/score_detail.html",
    "templates/coverage_store.html",
    "templates/group.html",
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
            "templates/coverage_store.html": "coverage",
            "templates/group.html": "coverage",
        }
        for name in PAGES:
            html = read(name)
            self.assertIn("_nav.html", html)
            self.assertIn("nav_active = '%s'" % expected[name], html)
            self.assertNotIn('class="page-nav"', html)

    def test_partial_uses_ds_menu_hooks(self):
        html = read("templates/_nav.html")
        for href, label in (
            ('href="/"', "拉群覆盖"),
            ('href="/timeout"', "超时提醒"),
            ('href="/groups"', "群管理"),
            ('href="/score"', "超时评分"),
        ):
            self.assertIn(href, html)
            self.assertIn(label, html)
        # DS 结构：.menu + .mi，当前页 .on（桌面下划线 / 抽屉 #e6f4ff+#1677ff 都由 DS 提供）
        self.assertIn('class="menu"', html)
        self.assertIn('aria-label="主导航"', html)
        self.assertIn("{% if nav_active == 'coverage' %} on{% endif %}", html)
        # 页面不再自带第二只汉堡 / 遮罩 / 开合脚本（cards.js 统一处理，中文文案也在 DS）
        self.assertNotIn("nav-burger", html)
        self.assertNotIn("nav-mask", html)
        self.assertNotIn("<script>", html)

    def test_css_drops_legacy_nav_and_uses_single_breakpoint(self):
        css = read("static/css/style.css")
        # 旧的自定义汉堡/抽屉规则整体下线，交给 antd-lite.css 4.5/4.6
        for legacy in ("button.nav-burger", ".nav-mask", ".page-nav", ".nav-drawer-title",
                       "is-nav-open", "nav-scroll-lock", "cubic-bezier(0.7, 0.3, 0.1, 1)"):
            self.assertNotIn(legacy, css)
        # 唯一导航断点 860px：.cl-meta 隐藏与 DS 汉堡同断点
        self.assertIn("@media (max-width: 860px) { .cl-meta { display: none; } }", css)
        self.assertNotIn("max-width: 768px) { .cl-meta", css)

    def test_ds_assets_provide_drawer_contract(self):
        """抽屉契约来自设计系统副本：断点 / 曲线 / 宽度 / 遮罩。"""
        css = read("ds/antd-lite.css")
        js = read("ds/cards.js")
        self.assertIn("@media (max-width: 860px)", css)
        self.assertIn("cubic-bezier(.23, 1, .32, 1)", css)
        self.assertIn("min(300px, 82vw)", css)
        self.assertIn("rgba(0, 0, 0, .45)", css)
        self.assertIn("打开菜单", js)
        self.assertIn("关闭菜单", js)


if __name__ == "__main__":
    unittest.main()
