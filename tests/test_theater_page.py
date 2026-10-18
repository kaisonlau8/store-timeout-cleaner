"""B4 战区详情页与全站战区链接。"""

import html as html_lib
import json
import logging
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from score_query import build_report, build_theater_report
import timeout_algo
import timeout_query
from timeout_store import connect

ROOT = Path(__file__).resolve().parent.parent
THEATER = "华东战区"
START = "2026-07-01"
END = "2026-07-31"
RULE_30 = "2055182790919555459"
RULE_10 = "2035988283787413634"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _data_file(path: Path) -> None:
    empty = {"group_count": 0, "store_count": 0, "store_stats": {}}
    data = {
        "updated_at": "2026-10-15 00:00:00",
        "summary": {"all_total": 3, "all_valid": 3, "all_invalid": 0, "source": "test"},
        "stats": {
            "national": {
                "all": {"group_count": 4, "store_count": 3, "store_stats": {"甲店": 2, "乙店": 1, "丙店": 1}},
                "filter": {"group_count": 2, "store_count": 2, "store_stats": {"甲店": 1, "丙店": 1}},
            },
            "after_may1": {"all": empty, "filter": empty},
            "monthly": {"months": [], "years": ["2026"], "data": {}},
        },
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _seed(root: Path) -> str:
    db = str(root / "timeout.db")
    conn = timeout_algo.open_db(str(root))
    try:
        conn.executemany(
            "INSERT INTO chat_store(room_id, store, theater, group_name, in_robot) VALUES (?, ?, ?, ?, ?)",
            [
                ("room001", "甲店", THEATER, "甲店群1", 1),
                ("room002", "甲店", THEATER, "甲店群2", 1),
                ("room003", "乙店", THEATER, "乙店群1", 0),
                ("room004", "", THEATER, "未归店群", 0),
                ("room005", "丙店", "华北战区", "丙店群1", 1),
            ],
        )
        conn.executemany(
            "INSERT INTO timeout_event("
            "msg_id, rule_id, rule_name, room_id, store, theater, created_at, room_name, content, exclude_reason"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("m1", RULE_30, "30 分钟未回复预警", "room001", "甲店", THEATER, "2026-07-10 10:00:00", "甲店群1", "在吗想问下车", ""),
                ("m2", RULE_30, "30 分钟未回复预警", "room002", "甲店", THEATER, "2026-07-11 10:00:00", "甲店群2", "明天能提车吗", ""),
                ("m3", RULE_10, "10 分钟未回复预警", "room001", "甲店", THEATER, "2026-07-15 10:00:00", "甲店群1", "有人吗", ""),
                ("m4", RULE_30, "30 分钟未回复预警", "room002", "甲店", THEATER, "2026-07-16 10:00:00", "甲店群2", "好的", ""),
                ("m5", RULE_30, "30 分钟未回复预警", "room005", "丙店", "华北战区", "2026-07-17 10:00:00", "丙店群1", "想咨询保养", ""),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    _data_file(root / "data.json")
    return db


@pytest.fixture
def theater_env(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    data = tmp_path / "data.json"
    monkeypatch.setenv("CLEANER_DB_PATH", db)
    monkeypatch.setenv("CLEANER_DATA_FILE", str(data))
    monkeypatch.setenv("MHERO_AUTH", "off")
    monkeypatch.setenv("MHERO_AUTH_CLEANER", "off")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    import app as app_module

    return app_module.app.test_client(), tmp_path, db


def _links(html: str, marker: str) -> list[str]:
    values = [
        unquote(html_lib.unescape(value))
        for value in re.findall(r'href="([^"]*)"', html)
    ]
    return [value for value in values if marker in value]


def test_theater_page_summary_stores_and_unassigned_groups(theater_env):
    client, root, db = theater_env
    response = client.get(
        f"/theater/{THEATER}", query_string={"start": START, "end": END}
    )
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert f"<h1>{THEATER}</h1>" in html
    back = html_lib.unescape(re.search(r'data-ds-back="([^"]*)"', html).group(1))
    parts = urlsplit(back)
    assert parts.path == "/"
    back_qs = parse_qs(parts.query)
    assert back_qs["theater"] == [THEATER]
    assert back_qs["start"] == [START]
    assert back_qs["end"] == [END]
    assert 'data-ds-back-label="拉群覆盖"' in html

    assert re.search(r'<h4>门店数</h4>\s*<div class="big-number">2</div>', html)
    assert re.search(r'<h4>群总数</h4>\s*<div class="big-number">4</div>', html)
    assert re.search(r'<h4>已拉机器人群数</h4>\s*<div class="big-number">2</div>', html)
    assert "覆盖率 50.0%" in html
    assert re.search(r'<h4>仅展示（不计 KPI）</h4>\s*<div class="big-number">1</div>', html)
    assert "规则：10 分钟未回复预警" in html

    expected_kpi = sum(
        row["valid"]
        for row in build_report(
            str(root), date(2026, 7, 1), date(2026, 7, 31), db_path=db
        )["stores"]
        if row["theater"] == THEATER
    )
    assert expected_kpi == 2
    assert re.search(
        r'<h4>计入 KPI 超时</h4>\s*<div class="big-number">' + str(expected_kpi) + r"</div>",
        html,
    )
    assert "评分口径，与提醒页不同" in html
    assert 'class="theater-kpi-info"' in html
    assert "与超时提醒页的规则触发数不同" in html

    # 主表、真正的 <th> 表头与按 KPI 倒序
    assert 'class="store-table theater-table" data-ds-primary' in html
    primary = html.split("data-ds-primary", 1)[1].split("</table>", 1)[0]
    headers = [
        re.sub(r"<[^>]+>", "", cell).strip()
        for cell in re.findall(r"<th[^>]*>(.*?)</th>", primary, re.S)
    ]
    assert headers == ["门店", "群数", "已拉机器人", "覆盖率", "计入 KPI 超时", "评分页"]
    assert html.index("甲店") < html.index("乙店")

    store_links = _links(html, "/store/")
    assert len(store_links) == 2
    for link in store_links:
        parts = urlsplit(link)
        qs = parse_qs(parts.query)
        assert qs["start"] == [START]
        assert qs["end"] == [END]
        assert qs["theater"] == [THEATER]
    score_links = _links(html, "/score/store?")
    assert len(score_links) == 2
    for link in score_links:
        parts = urlsplit(link)
        assert parts.path == "/score/store"
        qs = parse_qs(parts.query)
        assert qs["start"] == [START]
        assert qs["end"] == [END]
        assert qs["theater"] == [THEATER]
        assert qs["name"]

    # 未归店群：折叠明细 + 群名链接
    unassigned_at = html.index('data-ds-title="未归店群"')
    unassigned = html[unassigned_at : unassigned_at + 2000]
    assert "未归店群" in unassigned
    group_links = _links(unassigned, "/group/room004")
    assert group_links
    assert "start=" + START in group_links[0] and "end=" + END in group_links[0]
    assert "#i-right" in html


def test_theater_unknown_and_bad_range(theater_env):
    client, _root, _db = theater_env
    unknown = client.get("/theater/火星战区", query_string={"start": START, "end": END})
    assert unknown.status_code == 404
    body = unknown.get_data(as_text=True)
    assert "没有这个战区" in body or "未知战区" in body
    assert "返回拉群覆盖" in body

    bad = client.get(
        f"/theater/{THEATER}", query_string={"start": "2026-07-31", "end": "2026-07-01"}
    )
    assert bad.status_code == 400
    error_html = bad.get_data(as_text=True)
    assert "日期范围错误" in error_html
    assert 'aria-label="主导航"' in error_html
    assert "用默认日期重新打开" in error_html
    assert "返回拉群覆盖" in error_html


def test_theater_links_across_pages(theater_env):
    client, _root, _db = theater_env

    index_html = client.get(
        "/", query_string={"theater": THEATER, "start": START, "end": END}
    ).get_data(as_text=True)
    index_links = _links(index_html, "/theater/")
    assert index_links
    assert any(
        parse_qs(urlsplit(link).query).get("start") == [START]
        and parse_qs(urlsplit(link).query).get("end") == [END]
        for link in index_links
    )

    score_html = client.get(
        "/score", query_string={"start": START, "end": END, "theater": THEATER}
    ).get_data(as_text=True)
    score_theater_links = _links(score_html, f"/theater/{THEATER}")
    assert score_theater_links
    score_qs = parse_qs(urlsplit(score_theater_links[0]).query)
    assert score_qs["start"] == [START]
    assert score_qs["end"] == [END]

    store_html = client.get(
        "/store/甲店", query_string={"start": START, "end": END}
    ).get_data(as_text=True)
    store_theater_links = _links(store_html, f"/theater/{THEATER}")
    assert store_theater_links
    store_qs = parse_qs(urlsplit(store_theater_links[0]).query)
    assert store_qs["start"] == [START]
    assert store_qs["end"] == [END]

    group_html = client.get(
        "/group/room001", query_string={"start": START, "end": END}
    ).get_data(as_text=True)
    group_theater_links = _links(group_html, f"/theater/{THEATER}")
    assert group_theater_links
    group_qs = parse_qs(urlsplit(group_theater_links[0]).query)
    assert group_qs["start"] == [START]
    assert group_qs["end"] == [END]

    groups_js = _read("static/js/groups-list.js")
    assert "function theaterHref(name)" in groups_js
    assert "'/theater/' + encodeURIComponent(name)" in groups_js
    assert 'data-label="战区"' in groups_js

    # 首页按月统计（display.js）也生成战区链接。
    display_js = _read("static/js/display.js")
    assert "function theaterPageHref(name)" in display_js
    assert "'/theater/' + encodeURIComponent(name)" in display_js


def test_theater_display_only_matches_timeout_rule_card(theater_env):
    client, _root, db = theater_env
    # 已剔除的 10 分钟规则命中不应计入战区页，/timeout 卡片同样不计。
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO timeout_event("
            "msg_id, rule_id, rule_name, room_id, store, theater, created_at, room_name, content, exclude_reason"
            ") VALUES ('m10-excluded', ?, '10 分钟未回复预警', 'room001', '甲店', ?, "
            "'2026-07-20 10:00:00', '甲店群1', '好的', 'LLM复核剔除: 寒暄')",
            (RULE_10, THEATER),
        )
        conn.commit()
    finally:
        conn.close()

    page = client.get(
        f"/theater/{THEATER}", query_string={"start": START, "end": END}
    )
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    match = re.search(
        r"<h4>仅展示（不计 KPI）</h4>\s*<div class=\"big-number\">(\d+)</div>", html
    )
    assert match
    theater_count = int(match.group(1))

    payload = client.get(
        "/api/timeout/chart",
        query_string={"theater": THEATER, "start": START, "end": END},
    ).get_json()
    rule_counts = {row["rule_id"]: row["triggers"] for row in payload["totals"]["rules"]}
    timeout_count = sum(
        count for rule_id, count in rule_counts.items() if rule_id == RULE_10
    )
    assert theater_count == timeout_count == 1


def test_theater_hides_empty_unassigned_collapse(theater_env):
    client, _root, _db = theater_env
    html = client.get(
        "/theater/华北战区", query_string={"start": START, "end": END}
    ).get_data(as_text=True)
    assert 'data-ds-title="未归店群"' not in html


def test_theater_display_only_three_states(theater_env, monkeypatch, caplog):
    client, root, db = theater_env
    start, end = date(2026, 7, 1), date(2026, 7, 31)

    # 查询成功但没有匹配触发：0 是真实业务零值，不是失败。
    zero = build_theater_report(str(root), "华北战区", start, end, db_path=db)
    assert zero["display_only_state"] == "ok"
    assert zero["display_only"] == 0
    assert zero["display_only_rules"] == ["10 分钟未回复预警"]

    # 没有任何不计 KPI 的规则：单独的 none 状态。
    with monkeypatch.context() as patch:
        patch.setattr(timeout_algo, "non_kpi_rules", lambda *args, **kwargs: (set(), set()))
        none = build_theater_report(str(root), THEATER, start, end, db_path=db)
    assert none["display_only_state"] == "none"
    assert none["display_only"] == 0
    assert none["display_only_rules"] == []

    # 查询失败：不能伪造 0，页面显示 — / 暂不可用，并记录异常。
    caplog.set_level(logging.ERROR)

    def boom(*args, **kwargs):
        raise RuntimeError("模拟仅展示查询失败")

    with monkeypatch.context() as patch:
        patch.setattr(timeout_query, "query_rule_totals", boom)
        response = client.get(
            f"/theater/{THEATER}", query_string={"start": START, "end": END}
        )
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '<div class="big-number">—</div>' in html
    assert "暂不可用" in html
    assert "10 分钟未回复预警" in html
    assert "读取战区仅展示规则统计失败" in caplog.text
