"""B1 打通链接：评分明细、门店详情、超时明细与群名统计之间的站内跳转。"""

import html as html_lib
import json
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from flask import Flask

from score_query import load_rule_events, store_detail
from score_web import register_score
from stc_live import query_events
from tests.test_coverage_store import _minimal_stats
from tests.test_score_query import _msg_id, _seed as seed_score
from timeout_store import connect, replace_context

ROOT = Path(__file__).resolve().parent.parent
STORE = "猛士汽车武汉门店"
SCORE_START = "2026-07-01"
SCORE_END = "2026-09-30"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _section(html: str, marker: str) -> str:
    return html.split(f'id="{marker}"', 1)[1].split("</details>", 1)[0]


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def score_client(tmp_path):
    root = str(tmp_path)
    seed_score(root)
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    app.jinja_env.globals["ds_css"] = lambda: ""
    app.jinja_env.globals["ds_sprite"] = lambda: ""
    app.jinja_env.globals["ds_cards"] = lambda: ""
    register_score(app, root)
    return app.test_client()


@pytest.fixture
def link_client(tmp_path, monkeypatch):
    db = tmp_path / "timeout.db"
    conn = connect(str(db))
    replace_context(
        conn,
        robot_chat_ids=["r1"],
        employee_ids=[],
        group_rows=[
            {"group_id": "r1", "group_name": "X700 交付群", "store": STORE, "theater": "华东战区"},
            {"group_id": "r2", "group_name": "X700 未归店群", "store": "", "theater": ""},
        ],
    )
    conn.executemany(
        "INSERT INTO timeout_event(msg_id, rule_id, rule_name, room_id, store, created_at, room_name, exclude_reason) "
        "VALUES (?, ?, '首次回复超时', ?, ?, ?, ?, ?)",
        [
            ("msg-july", "rule", "r1", STORE, "2026-07-15 10:00:00", "X700 交付群", ""),
            ("msg-june", "rule", "r1", STORE, "2026-06-15 10:00:00", "X700 交付群", ""),
            (
                "msg-july-excluded",
                "rule-excluded",
                "r1",
                STORE,
                "2026-07-16 10:00:00",
                "X700 交付群",
                "寒暄/结束语/纯表情",
            ),
            ("msg-30", "rule-30", "r1", STORE, "2026-10-08 16:19:49", "X700 交付群", ""),
            ("msg-10", "rule-10", "r1", STORE, "2026-10-08 16:19:49", "X700 交付群", ""),
        ],
    )
    conn.commit()
    conn.close()

    data = _minimal_stats()
    months = ["2026-06", "2026-07", "2026-09"]
    data["stats"]["monthly"] = {
        "months": months,
        "years": ["2025", "2026"],
        "data": {
            month: {
                "all": {"group_count": 1, "store_count": 1, "store_stats": {STORE: 1}},
                "filter": {"group_count": 1, "store_count": 1, "store_stats": {STORE: 1}},
            }
            for month in months
        },
    }
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("CLEANER_DB_PATH", str(db))
    monkeypatch.setenv("CLEANER_DATA_FILE", str(data_file))
    monkeypatch.delenv("MHERO_AUTH", raising=False)
    monkeypatch.delenv("MHERO_AUTH_CLEANER", raising=False)
    import app as app_module

    return app_module.app.test_client()


# 1. store_detail / load_rule_events 保留 room_id、msg_id
def test_store_detail_events_keep_room_and_msg_ids():
    with tempfile.TemporaryDirectory() as root:
        seed_score(root)
        detail = store_detail(root, "甲店", date(2026, 7, 1), date(2026, 9, 30))
        assert len(detail["counted"]) == 1
        assert len(detail["excluded"]) == 1
        expected_msg = _msg_id("2026-08-01 09:00:00", "甲店客户群1", "刹车有异响")
        for event in detail["counted"] + detail["excluded"]:
            assert event["room_id"] == "name:甲店客户群1"
            assert event["msg_id"]
            assert event["rule_id"] == "规则10分钟"
            assert set(event) >= {"created_at", "room_id", "room_name", "msg_id", "rule_id", "rule_name", "reason", "message", "shared"}
        assert detail["counted"][0]["msg_id"] == expected_msg
        assert detail["counted"][0]["rule_id"] == "规则10分钟"
        # 计数口径不变：原始 2 条，有效 1 条，明细仍是 1 + 1。
        assert detail["store"]["raw"] == 2
        assert detail["store"]["valid"] == 1

        events, _, _ = load_rule_events(root, date(2026, 7, 1), date(2026, 9, 30))
        assert events
        assert all(event.get("room_id") and event.get("msg_id") for event in events)


def test_query_events_filters_by_msg_and_rule(tmp_path):
    conn = connect(str(tmp_path / "timeout.db"))
    try:
        conn.executemany(
            "INSERT INTO timeout_event(msg_id, rule_id, rule_name, room_id, store, created_at, room_name) "
            "VALUES (?, ?, ?, 'room1', ?, ?, 'X700 交付群')",
            [
                ("m30", "r30", "30分钟规则", STORE, "2026-10-08 16:19:49"),
                ("m10", "r10", "10分钟规则", STORE, "2026-10-08 16:19:49"),
            ],
        )
        conn.commit()

        same_day = query_events(
            conn, room_id="room1", start="2026-10-08", end="2026-10-08", status="all"
        )
        assert same_day["total"] == 2

        exact = query_events(
            conn,
            room_id="room1",
            msg_id="m30",
            rule_id="r30",
            start="2026-10-08",
            end="2026-10-08",
            status="all",
        )
        assert exact["total"] == 1
        assert [(event["msg_id"], event["rule_id"]) for event in exact["events"]] == [("m30", "r30")]

        rule_only = query_events(
            conn, room_id="room1", rule_id="r30", start="2026-10-08", end="2026-10-08", status="all"
        )
        assert [event["msg_id"] for event in rule_only["events"]] == ["m30"]

        long_id = "z" * 120
        conn.execute(
            "INSERT INTO timeout_event(msg_id, rule_id, rule_name, room_id, store, created_at, room_name) "
            "VALUES (?, 'r-long', '长ID规则', 'room1', ?, '2026-10-08 17:00:00', 'X700 交付群')",
            (long_id, STORE),
        )
        conn.commit()
        capped = query_events(conn, msg_id="z" * 130, rule_id="r-long", status="all")
        assert capped["total"] == 1
        assert capped["events"][0]["msg_id"] == long_id
    finally:
        conn.close()


def test_timeout_events_api_filters_by_msg_and_rule(link_client):
    payload = link_client.get(
        "/api/timeout/events",
        query_string={
            "room_id": "r1",
            "msg_id": "msg-30",
            "rule_id": "rule-30",
            "start": "2026-10-08",
            "end": "2026-10-08",
            "status": "all",
        },
    ).get_json()
    assert payload["total"] == 1
    assert [(event["msg_id"], event["rule_id"]) for event in payload["events"]] == [("msg-30", "rule-30")]

    same_day = link_client.get(
        "/api/timeout/events",
        query_string={
            "room_id": "r1",
            "start": "2026-10-08",
            "end": "2026-10-08",
            "status": "all",
        },
    ).get_json()
    assert same_day["total"] == 2


# 2. /score/store 页面：会话按钮、操作列、返回链接、页头链接
def test_score_detail_members_context_and_header_links(score_client):
    url = (
        "/score/store?name=%E7%94%B2%E5%BA%97"
        f"&start={SCORE_START}&end={SCORE_END}"
        "&theater=%E5%8D%8E%E4%B8%9C%E6%88%98%E5%8C%BA&q=%E7%94%B2"
    )
    response = score_client.get(url)
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    # 所在会话：有 room_id 时是群详情链接。
    group_links = [html_lib.unescape(value) for value in re.findall(r'href="([^"]*/group/[^"]*)"', html)]
    assert len(group_links) == 2
    for link in group_links:
        parts = urlsplit(link)
        assert parts.path == "/group/" + quote("name:甲店客户群1", safe="")
        params = parse_qs(parts.query)
        assert params["start"] == [SCORE_START]
        assert params["end"] == [SCORE_END]
        assert params["event"]
    assert "所在会话" in html

    # 两张表都有操作列表头，操作按钮带上下文 JSON。
    assert html.count("<th>操作</th>") == 2
    assert html.count("上下文 / 成员") == 2
    assert 'class="sd-btn"' in html
    events = [json.loads(html_lib.unescape(raw)) for raw in re.findall(r'data-sd-events="([^"]*)"', html)]
    assert len(events) == 2
    assert {item["status"] for item in events} == {"all"}
    assert {item["room_id"] for item in events} == {"name:甲店客户群1"}
    assert {(item["start"], item["end"]) for item in events} == {("2026-08-01", "2026-08-01")}
    expected_events = {
        (_msg_id("2026-08-01 09:00:00", "甲店客户群1", "刹车有异响"), "规则10分钟"),
        (_msg_id("2026-08-01 09:05:00", "甲店客户群1", "好的"), "规则10分钟"),
    }
    assert {(item["msg_id"], item["rule_id"]) for item in events} == expected_events
    for item in events:
        assert set(item) == {"room_id", "msg_id", "rule_id", "start", "end", "status"}
        assert "store" not in item
    assert set(re.findall(r'data-sd-title="([^"]*)"', html)) == {"甲店客户群1 · 2026-08-01"}

    # 引入抽屉脚本；重复的 table-head 已删除。
    assert "js/stc-detail.js" in html
    assert '<div class="table-head">' not in html

    # 返回链接保留筛选参数。
    back = html_lib.unescape(re.search(r'data-ds-back="([^"]*)"', html).group(1))
    parts = urlsplit(back)
    assert parts.path == "/score"
    params = parse_qs(parts.query)
    assert params["start"] == [SCORE_START]
    assert params["end"] == [SCORE_END]
    assert params["theater"] == ["华东战区"]
    assert params["q"] == ["甲"]

    # 页头 sub 不再以多余的分隔符开头，并有「门店详情」站内链接。
    sub = html.split('<div class="ant-page-header-sub">', 1)[1].split("</div>", 1)[0]
    sub_text = re.sub(r"<[^>]+>", "", sub).strip()
    assert not sub_text.startswith("·")
    assert "20-50% · 80.00 分" in sub_text
    assert "原始 2 / 有效 1" in sub_text
    store_links = [html_lib.unescape(value) for value in re.findall(r'href="([^"]*/store/[^"]*)"', html)]
    assert store_links == [f"/store/%E7%94%B2%E5%BA%97?start={SCORE_START}&end={SCORE_END}"]
    assert "门店详情<svg class=\"ai\" aria-hidden=\"true\"><use href=\"#i-right\"/></svg></a>" in html

    # 没有筛选参数时，返回链接不带 theater/q。
    plain = score_client.get(f"/score/store?name=%E7%94%B2%E5%BA%97&start={SCORE_START}&end={SCORE_END}")
    plain_back = html_lib.unescape(re.search(r'data-ds-back="([^"]*)"', plain.get_data(as_text=True)).group(1))
    plain_params = parse_qs(urlsplit(plain_back).query)
    assert plain_params["start"] == [SCORE_START]
    assert plain_params["end"] == [SCORE_END]
    assert "theater" not in plain_params and "q" not in plain_params


def test_score_detail_empty_rows_span_new_operation_column(score_client):
    html = score_client.get(
        f"/score/store?name=%E7%94%B2%E5%BA%97&start=2026-09-01&end=2026-09-30"
    ).get_data(as_text=True)
    assert 'colspan="6"' in html
    assert 'colspan="7"' in html


# 3. 门店详情页：超时评分反链；/store?start=&end= 识别为超时日期区间
def test_coverage_store_links_to_score_and_accepts_start_end_alias(link_client):
    response = link_client.get(f"/store/{quote(STORE, safe='')}?start=2026-07-01&end=2026-07-31")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    expected = f"/score/store?name={quote(STORE, safe='')}&start=2026-07-01&end=2026-07-31"
    links = [html_lib.unescape(value) for value in re.findall(r'href="([^"]*score/store[^"]*)"', html)]
    assert links == [expected]
    assert "超时评分<svg class=\"ai\" aria-hidden=\"true\"><use href=\"#i-right\"/></svg></a>" in html

    sub = html.split('<div class="ant-page-header-sub">', 1)[1].split("</div>", 1)[0]
    sub_text = re.sub(r"<[^>]+>", "", sub).strip()
    assert not sub_text.startswith("·")
    assert sub_text.startswith("华东战区")
    assert "群总数" in sub_text and "已拉机器人" in sub_text and "未拉机器人" in sub_text

    # start/end 被 date_ranges 当作超时日期别名，页面筛选与明细都按该区间生效。
    assert "超时 2026-07-01 ~ 2026-07-31" in html
    events = _section(html, "timeout-events")
    assert "msg-july" in events
    assert "msg-june" not in events
    event_payloads = [
        json.loads(html_lib.unescape(raw))
        for raw in re.findall(r'data-sd-events="([^"]*)"', events)
    ]
    assert len(event_payloads) == 1
    assert event_payloads[0]["msg_id"] == "msg-july"
    assert event_payloads[0]["rule_id"] == "rule"
    assert event_payloads[0]["store"] == STORE
    assert event_payloads[0]["status"] == "valid"

    excluded = _section(html, "timeout-excluded")
    excluded_payloads = [
        json.loads(html_lib.unescape(raw))
        for raw in re.findall(r'data-sd-events="([^"]*)"', excluded)
    ]
    assert len(excluded_payloads) == 1
    assert excluded_payloads[0]["msg_id"] == "msg-july-excluded"
    assert excluded_payloads[0]["rule_id"] == "rule-excluded"
    assert excluded_payloads[0]["store"] == STORE
    assert excluded_payloads[0]["status"] == "excluded"

    reversed_range = link_client.get(f"/store/{quote(STORE, safe='')}?start=2026-07-31&end=2026-07-01")
    assert reversed_range.status_code == 400


# 4. timeout.js 门店明细链接带上当前区间与战区
def test_timeout_store_links_keep_range_and_theater():
    source = _read("static/js/timeout.js")
    assert "function storeHref(name)" in source
    assert "lastQuery.start" in source and "lastQuery.end" in source and "lastQuery.theater" in source
    assert "'/store/' + encodeURIComponent(name)" in source

    match = re.search(r"    function storeHref\(name\) \{.*?\n    \}", source, re.S)
    assert match
    script = (
        "let lastQuery = {start: '2026-07-01', end: '2026-07-31', theater: '华东战区'};\n"
        + match.group(0)
        + "\nconsole.log(storeHref('甲店'));\n"
    )
    assert _run_node(script) == (
        "/store/%E7%94%B2%E5%BA%97?start=2026-07-01&end=2026-07-31"
        "&theater=%E5%8D%8E%E4%B8%9C%E6%88%98%E5%8C%BA"
    )


# 5. 群名统计：门店链接、未归店群成员按钮、重复渲染不叠加空状态行
def test_groups_page_assets_and_unmatched_room_ids(link_client):
    assert "js/stc-detail.js" in _read("templates/groups.html")
    response = link_client.get("/api/groups/keyword?keyword=X700")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["unmatched"][0]["room_id"] == "r2"
    assert payload["unmatched"][0]["group_name"] == "X700 未归店群"


def test_groups_js_renders_links_and_clears_tables():
    source = _read("static/js/groups.js")
    assert '<a href="/store/' in source and "encodeURIComponent(row.store)" in source
    assert 'class="sd-link"' in source and 'href="/group/' in source
    assert "encodeURIComponent(row.room_id)" in source
    assert 'data-sd-members="' not in source
    assert source.count("tbody.innerHTML = '';") >= 3

    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const src = fs.readFileSync(SOURCE, 'utf8');
function el(extra) {
  return Object.assign({
    innerHTML: '', textContent: '', value: '', checked: false, hidden: true, disabled: false, href: '',
    listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; },
    appendChild() {}, querySelector() { return null; }, querySelectorAll() { return []; },
  }, extra || {});
}
const els = {
  'keyword-input': el({value: 'X700'}), 'theater-select': el({value: ''}), 'store-q': el({value: ''}),
  'robot-only': el({checked: false}), 'query-btn': el(), 'export-btn': el(),
  'groups-error': el(), 'groups-result': el(), 'groups-empty': el(), 'empty-desc': el(),
  'groups-cards': el(), 'synced-at': el(),
};
const tbodies = {
  '#theater-table tbody': el(), '#store-table tbody': el(), '#unmatched-table tbody': el(),
};
Object.values(tbodies).forEach((tbody) => {
  tbody.appendChild = function (child) { this.innerHTML += '<tr>' + (child.innerHTML || '') + '</tr>'; };
});
const document = {
  getElementById: (id) => els[id] || null,
  querySelector: (sel) => tbodies[sel] || null,
  querySelectorAll: () => [],
  createElement: () => el(),
  addEventListener: (name, fn) => { if (name === 'DOMContentLoaded') document.ready = fn; },
};
const location = {search: '?keyword=X700', pathname: '/groups'};
const history = {replaceState() {}};
let responses = [
  {
    total: 2, robot_total: 0, matched_store_total: 1, unmatched_total: 1,
    keyword: 'X700', theater: '', q: '', only_robot: 0, theaters: [],
    by_theater: [{theater: '华东战区', group_count: 1, store_count: 1}],
    by_store: [{store: '甲店', theater: '华东战区', group_count: 1, robot_count: 0}],
    unmatched: [{room_id: 'r2', group_name: 'X700 未归店群', in_robot: 0}],
  },
  {
    total: 2, robot_total: 0, matched_store_total: 0, unmatched_total: 0,
    keyword: 'X700', theater: '', q: '', only_robot: 0, theaters: [],
    by_theater: [], by_store: [], unmatched: [],
  },
];
const context = {
  document, location, history, URLSearchParams, console,
  fetch: async () => ({ok: true, status: 200, json: async () => responses.shift()}),
};
vm.runInNewContext(src, context);
async function settle() { for (let i = 0; i < 20; i += 1) await Promise.resolve(); }
(async () => {
  document.ready();
  await settle();
  assert(tbodies['#store-table tbody'].innerHTML.includes(
    '<a href="/store/%E7%94%B2%E5%BA%97">甲店</a>'
  ));
  const unmatched = tbodies['#unmatched-table tbody'].innerHTML;
  assert(unmatched.includes('class="sd-link"'));
  assert(unmatched.includes('href="/group/r2"'));
  assert(unmatched.includes('X700 未归店群'));
  assert(!unmatched.includes('data-sd-members='));

  els['query-btn'].listeners.click();
  await settle();
  assert.equal((tbodies['#unmatched-table tbody'].innerHTML.match(/<tr>/g) || []).length, 1);
  console.log('ok');
})().catch((err) => { console.error(err); process.exit(1); });
""".replace("SOURCE", json.dumps(str(ROOT / "static/js/groups.js")))
    assert _run_node(script) == "ok"


# 6. stc-detail.js：状态码中文提示，后端 error 优先
def test_stc_detail_error_messages_are_friendly():
    source = _read("static/js/stc-detail.js")
    assert "'请求失败 ' + r.status" not in source
    for text in ("参数无效，无法查询", "没有权限查看", "没有找到对应记录", "服务暂时不可用，请稍后重试"):
        assert text in source

    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
let src = fs.readFileSync(SOURCE, 'utf8');
src = src.replace(
  'window.StcDetail = { events: events, members: members, close: close };',
  'window.StcDetail = { events: events, members: members, close: close }; window.__getJSON = getJSON;'
);
const document = {addEventListener() {}, getElementById() { return null; }};
const window = {};
const context = {window, document, requestAnimationFrame: (fn) => fn(), setTimeout() {}, fetch: null};
vm.runInNewContext(src, context);
async function expect(status, body, message) {
  context.fetch = async () => ({ok: status >= 200 && status < 300, status, json: async () => body});
  await assert.rejects(window.__getJSON('/x'), (err) => err.message === message);
}
(async () => {
  await expect(400, {}, '参数无效，无法查询');
  await expect(403, {}, '没有权限查看');
  await expect(404, {}, '没有找到对应记录');
  await expect(500, {}, '服务暂时不可用，请稍后重试');
  await expect(503, {}, '服务暂时不可用，请稍后重试');
  await expect(400, {error: '后端给的错误'}, '后端给的错误');
  context.fetch = async () => { throw new Error('offline'); };
  await assert.rejects(window.__getJSON('/x'), (err) => err.message === '服务暂时不可用，请稍后重试');
  console.log('ok');
})().catch((err) => { console.error(err); process.exit(1); });
""".replace("SOURCE", json.dumps(str(ROOT / "static/js/stc-detail.js")))
    assert _run_node(script) == "ok"


def test_scrm_api_errors_do_not_leak_exception_class(link_client, monkeypatch):
    import app as app_module

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(app_module.stc_live, "group_members", boom)
    response = link_client.get("/api/group/room123/members")
    assert response.status_code == 502
    members_error = response.get_json()["error"]
    assert members_error == "SCRM 群成员接口暂不可用，请稍后重试"
    assert "RuntimeError" not in members_error

    monkeypatch.setattr(app_module.stc_live, "chat_context", boom)
    response = link_client.get(
        "/api/timeout/context",
        query_string={"room_id": "room123", "at": "2026-10-08 16:19:49"},
    )
    assert response.status_code == 502
    context_error = response.get_json()["error"]
    assert context_error == "SCRM 会话存档接口暂不可用，请稍后重试"
    assert "RuntimeError" not in context_error


def test_score_detail_source_column_and_sd_btn_are_nowrap():
    css = _read("static/css/stc-detail.css")
    assert re.search(r"\.sd-btn\{[^}]*white-space:nowrap", css)
    assert re.search(r"th\.col-nowrap,td\.col-nowrap\{white-space:nowrap\}", css)
    template = _read("templates/score_detail.html")
    assert template.count('class="col-nowrap"') == 4


def test_stc_detail_drawer_status_labels():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
let src = fs.readFileSync(SOURCE, 'utf8');
src = src.replace(
  'window.StcDetail = { events: events, members: members, close: close };',
  'window.StcDetail = { events: events, members: members, close: close }; window.__statusLabel = statusLabel;'
);
const document = {addEventListener() {}, getElementById() { return null; }};
const window = {};
const context = {window, document, requestAnimationFrame: (fn) => fn(), setTimeout() {}, fetch: null};
vm.runInNewContext(src, context);
assert.equal(window.__statusLabel('excluded'), '已剔除 ');
assert.equal(window.__statusLabel('all'), '共 ');
assert.equal(window.__statusLabel('valid'), '计入 ');
assert.equal(window.__statusLabel(undefined), '计入 ');
console.log('ok');
""".replace("SOURCE", json.dumps(str(ROOT / "static/js/stc-detail.js")))
    assert _run_node(script) == "ok"


def test_stc_detail_duplicate_reason_is_neutral():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
let src = fs.readFileSync(SOURCE, 'utf8');
src = src.replace(
  'window.StcDetail = { events: events, members: members, close: close };',
  'window.StcDetail = { events: events, members: members, close: close }; window.__eventItem = eventItem;'
);
const document = {addEventListener() {}, getElementById() { return null; }};
const window = {};
const context = {window, document, requestAnimationFrame: (fn) => fn(), setTimeout() {}, fetch: null};
vm.runInNewContext(src, context);
const base = {room_id: 'r1', created_at: '2026-10-08 16:19:49', rule_name: '规则', group_name: '群', minutes: 30};
const duplicate = window.__eventItem(Object.assign({}, base, {exclude_reason: '重复触发(同一消息)'}));
assert(duplicate.includes('<span class="sd-tag">同一消息另有规则命中</span>'));
assert(!duplicate.includes('不计入：'));
assert(!duplicate.includes('重复触发'));
const other = window.__eventItem(Object.assign({}, base, {exclude_reason: '寒暄/结束语/纯表情'}));
assert(other.includes('<span class="sd-tag">不计入：寒暄/结束语/纯表情</span>'));
console.log('ok');
""".replace("SOURCE", json.dumps(str(ROOT / "static/js/stc-detail.js")))
    assert _run_node(script) == "ok"


def test_static_asset_cache_versions_are_consistent():
    expected = {
        "stc-detail.css": "4",
        "stc-detail.js": "6",
        "groups.js": "7",
        "groups-list.css": "2",
        "groups-list.js": "4",
        "group.js": "3",
        "store-page.js": "1",
        "timeout.js": "12",
    }
    templates = list((ROOT / "templates").glob("*.html"))
    for asset, version in expected.items():
        pattern = re.compile(r"filename='[^']*" + re.escape(asset) + r"'\)\s*\}\}\?v=(\d+)")
        found = []
        for path in templates:
            found.extend(pattern.findall(path.read_text(encoding="utf-8")))
        assert found, asset
        assert set(found) == {version}, (asset, found)


# 7. 首页按月统计：默认月份取数据里最新的月份
def test_index_month_defaults_to_latest_month(link_client):
    response = link_client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    match = re.search(
        r'id="month-select-wrap".*?<div class="select-trigger" data-value="([^"]+)">([^<]+)</div>',
        html,
        re.S,
    )
    assert match
    assert match.group(1) == "09"
    assert match.group(2) == "09月"
    month_options = re.findall(r'<div class="select-option" data-value="(\d{2})">', html)
    assert month_options == [f"{number:02d}" for number in range(1, 13)]
