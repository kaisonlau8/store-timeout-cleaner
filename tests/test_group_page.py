"""B2 群详情页：数据口径、路由、本地上下文与站内链接。"""

import importlib
import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo

import pytest

import timeout_algo
from group_query import group_events, group_info, local_context
from score_query import store_detail
from tests.test_coverage_store import _minimal_stats
from timeout_store import connect, replace_context

ROOT = Path(__file__).resolve().parent.parent
STORE = "猛士汽车武汉门店"
ROOM = "room001"
ROOM_EMPTY = "room002"
ROOM_NO_STORE = "room003"
KPI_RULE = "2055182790919555459"
KPI_NAME = "30 分钟未回复预警"
NON_KPI_RULE = "2035988283787413634"
NON_KPI_NAME = "10 分钟未回复预警"
START = date(2026, 8, 1)
END = date(2026, 8, 1)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _ts(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI).timestamp())


def _seed(root):
    root = str(root)
    conn = connect(os.path.join(root, "timeout.db"))
    try:
        replace_context(
            conn,
            robot_chat_ids=[ROOM],
            employee_ids=["emp1"],
            group_rows=[
                {
                    "group_id": ROOM,
                    "group_name": "甲店客户群1",
                    "store": STORE,
                    "theater": "华东战区",
                    "create_time": "2026-05-01T00:00:00+08:00",
                },
                {
                    "group_id": ROOM_EMPTY,
                    "group_name": "无消息群",
                    "store": STORE,
                    "theater": "华东战区",
                    "create_time": "",
                },
                {
                    "group_id": ROOM_NO_STORE,
                    "group_name": "未归店群",
                    "store": "",
                    "theater": "",
                    "create_time": "",
                },
            ],
        )
        conn.executemany(
            "INSERT INTO timeout_event(msg_id, rule_id, rule_name, room_id, store, theater, created_at, "
            "room_name, content, exclude_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("kpi-ok", KPI_RULE, KPI_NAME, ROOM, STORE, "华东战区",
                 "2026-08-01 09:00:00", "甲店客户群1", "刹车有异响", ""),
                ("kpi-ex", KPI_RULE, KPI_NAME, ROOM, STORE, "华东战区",
                 "2026-08-01 10:00:00", "甲店客户群1", "好的", "寒暄/结束语/纯表情"),
                ("nonkpi-ok", NON_KPI_RULE, NON_KPI_NAME, ROOM, STORE, "华东战区",
                 "2026-08-01 11:00:00", "甲店客户群1", "想咨询保养", ""),
                ("nonkpi-dup", NON_KPI_RULE, NON_KPI_NAME, ROOM, STORE, "华东战区",
                 "2026-08-01 11:30:00", "甲店客户群1", "同一消息又命中", "重复触发(同一消息)"),
                ("nonkpi-ex", NON_KPI_RULE, NON_KPI_NAME, ROOM, STORE, "华东战区",
                 "2026-08-01 12:30:00", "甲店客户群1", "收到", "寒暄/结束语/纯表情"),
                ("other-room", KPI_RULE, KPI_NAME, "room-other", "别的门店", "华南战区",
                 "2026-08-01 09:30:00", "别的群", "别的消息", ""),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    # 播种 calc_rule / chat_msg，并同步 timeout_rule。
    timeout_algo.open_db(root).close()

    conn = connect(os.path.join(root, "timeout.db"))
    try:
        rows = [
            ("m-too-early", ROOM, "wm-customer", _ts("2026-08-01 08:39:59"), "text", "窗口外"),
            ("m-start", ROOM, "wm-customer", _ts("2026-08-01 08:40:00"), "text", "窗口起点"),
            ("m-trigger", ROOM, "wm-customer", _ts("2026-08-01 09:01:00"), "text", "刹车有异响"),
            ("m-robot", ROOM, "robot1", _ts("2026-08-01 09:02:00"), "text", "机器人回复"),
            ("m-staff", ROOM, "emp1", _ts("2026-08-01 09:03:00"), "text", "员工回复"),
            ("m-other", ROOM, "u-other", _ts("2026-08-01 09:04:00"), "text", "其他成员"),
            ("m-image", ROOM, "wm-customer", _ts("2026-08-01 09:05:00"), "image", ""),
            ("m-unknown", ROOM, "wm-customer", _ts("2026-08-01 09:06:00"), "weird", ""),
            ("m-end", ROOM, "wm-customer", _ts("2026-08-01 09:40:00"), "text", "窗口终点"),
            ("m-too-late", ROOM, "wm-customer", _ts("2026-08-01 09:40:01"), "text", "窗口外"),
        ]
        conn.executemany(
            "INSERT INTO chat_msg(msg_id, room_id, from_id, msgtime, msgtype, content) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _write_data(root):
    path = Path(root) / "data.json"
    path.write_text(json.dumps(_minimal_stats(), ensure_ascii=False), encoding="utf-8")
    return path


def _run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def group_client(tmp_path, monkeypatch):
    _seed(tmp_path)
    data = _write_data(tmp_path)
    monkeypatch.setenv("CLEANER_DB_PATH", str(tmp_path / "timeout.db"))
    monkeypatch.setenv("CLEANER_DATA_FILE", str(data))
    monkeypatch.setenv("SCRM_ROBOT_USERIDS", "robot1")
    monkeypatch.delenv("MHERO_AUTH", raising=False)
    monkeypatch.delenv("MHERO_AUTH_CLEANER", raising=False)
    import app as app_module

    module = importlib.reload(app_module)
    yield module.app.test_client(), module, tmp_path


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _group_links(html):
    return [value.replace("&amp;", "&") for value in re.findall(r'href="([^"]*/group/[^"]*)"', html)]


def test_group_info_uses_store_row_falls_back_and_returns_none(tmp_path):
    conn = connect(str(tmp_path / "timeout.db"))
    try:
        conn.execute(
            "INSERT INTO chat_store(room_id, store, theater, group_name, create_time, in_robot) VALUES (?, ?, ?, ?, ?, ?)",
            (ROOM, STORE, "华东战区", "甲店客户群1", "2026-05-01T00:00:00+08:00", 1),
        )
        info = group_info(conn, ROOM)
        assert info == {
            "room_id": ROOM,
            "group_name": "甲店客户群1",
            "store": STORE,
            "theater": "华东战区",
            "create_time": "2026-05-01T00:00:00+08:00",
            "in_robot": 1,
            "owner_id": None,
            "owner_name": None,
            "owner_dept": None,
            "member_count": None,
        }

        conn.execute("DELETE FROM chat_store")
        conn.execute(
            "INSERT INTO timeout_event(msg_id, rule_id, rule_name, room_id, store, theater, created_at, room_name) "
            "VALUES ('history-msg', 'rule', '规则', ?, ?, '华南战区', '2026-08-01 09:00:00', '历史归档群')",
            (ROOM, STORE),
        )
        conn.commit()
        fallback = group_info(conn, ROOM)
        assert fallback["group_name"] == "历史归档群"
        assert fallback["store"] == STORE
        assert fallback["theater"] == "华南战区"
        assert fallback["create_time"] == ""
        assert fallback["in_robot"] == 0
        assert group_info(conn, "missing-room") is None
    finally:
        conn.close()


def test_group_events_three_groups_and_alignment_with_store_detail(tmp_path):
    _seed(tmp_path)
    conn = connect(str(tmp_path / "timeout.db"))
    try:
        groups = group_events(str(tmp_path), conn, ROOM, START, END)
    finally:
        conn.close()

    assert {item["msg_id"] for item in groups["kpi_counted"]} == {"kpi-ok"}
    assert {item["msg_id"] for item in groups["kpi_excluded"]} == {"kpi-ex", "nonkpi-ex"}
    assert {item["msg_id"] for item in groups["display_only"]} == {"nonkpi-ok", "nonkpi-dup"}
    assert all(item["reason"] == "" for item in groups["kpi_counted"] + groups["display_only"])
    assert all(item["reason"] for item in groups["kpi_excluded"])
    assert {item["minutes"] for item in groups["kpi_counted"]} == {30}
    assert {item["minutes"] for item in groups["display_only"]} == {10}
    for key in groups:
        assert len(groups[key]) <= 500

    detail = store_detail(str(tmp_path), STORE, START, END)
    aligned = [item for item in detail["counted"] if item["room_id"] == ROOM]
    assert len(groups["kpi_counted"]) == len(aligned) == 1
    assert groups["kpi_counted"][0]["msg_id"] == aligned[0]["msg_id"]


def test_local_context_roles_trigger_text_and_window(tmp_path):
    _seed(tmp_path)
    conn = connect(str(tmp_path / "timeout.db"))
    try:
        payload = local_context(
            conn, ROOM, "m-trigger", "2026-08-01 09:00:00", 30, ["emp1"], ["robot1"]
        )
    finally:
        conn.close()
    assert payload["source"] == "local"
    messages = payload["messages"]
    assert [msg["time"] for msg in messages][0] == "2026-08-01 08:40:00"
    assert [msg["time"] for msg in messages][-1] == "2026-08-01 09:40:00"
    assert all("08:39:59" not in msg["time"] and "09:40:01" not in msg["time"] for msg in messages)
    by_text = {msg["text"]: msg for msg in messages}
    assert by_text["窗口起点"]["role"] == "customer"
    assert by_text["机器人回复"]["role"] == "robot"
    assert by_text["机器人回复"]["role_label"] == "机器人"
    assert by_text["员工回复"]["role"] == "staff"
    assert by_text["其他成员"]["role"] == "staff"
    assert by_text["[图片]"]["role"] == "customer"
    assert "[消息]" in by_text
    trigger = [msg for msg in messages if msg["trigger"]]
    assert len(trigger) == 1
    assert trigger[0]["text"] == "刹车有异响"
    assert trigger[0]["role_label"] == "客户"


def test_local_context_empty_has_local_source(tmp_path):
    _seed(tmp_path)
    conn = connect(str(tmp_path / "timeout.db"))
    try:
        payload = local_context(
            conn, ROOM, "none", "2026-08-02 09:00:00", 30, ["emp1"], ["robot1"]
        )
    finally:
        conn.close()
    assert payload == {"messages": [], "source": "local"}


def test_group_page_renders_counts_links_and_empty_state(group_client):
    client, _, _ = group_client
    response = client.get(f"/group/{ROOM}?start=2026-08-01&end=2026-08-01")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "<h1>甲店客户群1</h1>" in html
    assert f'data-ds-back="/store/{quote(STORE, safe="")}?start=2026-08-01&amp;end=2026-08-01"' in html
    assert 'data-ds-back-label="猛士汽车武汉门店"' in html
    for title in ("计入 KPI 的超时", "仅展示（不计 KPI）", "已剔除", "群成员"):
        assert f'data-ds-title="{title}"' in html
    assert "计入 KPI 超时" in html and "仅展示（不计 KPI）" in html and "已剔除" in html
    assert "最近一次超时" in html and "2026-08-01 12:30:00" in html
    assert "区间内" in html and "本群当前区间" not in html
    assert "创建时间 2026-05-01 00:00" in html
    assert "2026-05-01T00:00:00+08:00" not in html
    assert 'data-ds-title="计入 KPI 的超时" data-ds-count="1"' in html
    assert 'data-ds-title="仅展示（不计 KPI）" data-ds-count="2"' in html
    assert 'data-ds-title="已剔除" data-ds-count="2"' in html
    assert 'data-ds-title="群成员" data-ds-count="—"' in html
    assert html.count("data-ctx") == 5
    assert 'data-msg="kpi-ok"' in html and 'data-at="2026-08-01 09:00:00"' in html
    assert "仅展示，不计 KPI" in html
    assert f'data-room-id="{ROOM}"' in html
    assert "static/js/group.js" in html
    assert 'class="ant-tag ant-tag-success"' in html
    assert f'<a class="store-link" href="/store/{quote(STORE, safe="")}?start=2026-08-01&amp;end=2026-08-01">' in html

    no_store = client.get(f"/group/{ROOM_NO_STORE}?start=2026-08-01&end=2026-08-01")
    assert no_store.status_code == 200
    no_store_html = no_store.get_data(as_text=True)
    assert 'data-ds-back="/"' in no_store_html
    assert 'data-ds-back-label="拉群覆盖"' in no_store_html
    assert "未归战区" in no_store_html

    empty = client.get(f"/group/{ROOM_EMPTY}?start=2026-08-01&end=2026-08-01")
    assert empty.status_code == 200
    empty_html = empty.get_data(as_text=True)
    assert "当前区间没有计入 KPI 的超时" in empty_html
    assert "最近一次超时" in empty_html and ">—<" in empty_html


def test_group_page_formats_create_time_and_keeps_unparseable_value(group_client):
    client, _, root = group_client
    conn = connect(str(Path(root) / "timeout.db"))
    try:
        conn.execute(
            "UPDATE chat_store SET create_time = ? WHERE room_id = ?",
            ("2024-11-13T08:21:14+08:00", ROOM_EMPTY),
        )
        conn.commit()
    finally:
        conn.close()
    html = client.get(f"/group/{ROOM_EMPTY}?start=2026-08-01&end=2026-08-01").get_data(as_text=True)
    assert "创建时间 2024-11-13 08:21" in html
    assert "2024-11-13T08:21:14+08:00" not in html

    conn = connect(str(Path(root) / "timeout.db"))
    try:
        conn.execute(
            "UPDATE chat_store SET create_time = ? WHERE room_id = ?",
            ("不是时间", ROOM_EMPTY),
        )
        conn.commit()
    finally:
        conn.close()
    html = client.get(f"/group/{ROOM_EMPTY}?start=2026-08-01&end=2026-08-01").get_data(as_text=True)
    assert "创建时间 不是时间" in html


def test_group_page_404_and_400(group_client):
    client, _, _ = group_client
    malformed = client.get("/group/abc")
    assert malformed.status_code == 404
    assert "没有这个群" in malformed.get_data(as_text=True)
    missing = client.get("/group/missing-room?start=2026-08-01&end=2026-08-01")
    assert missing.status_code == 404
    missing_html = missing.get_data(as_text=True)
    assert "没有这个群" in missing_html
    assert "返回拉群覆盖" in missing_html
    assert 'data-ds-back="/"' in missing_html
    reversed_range = client.get(f"/group/{ROOM}?start=2026-08-02&end=2026-08-01")
    assert reversed_range.status_code == 400


def test_group_context_api_local_hit_and_scrm_fallback(group_client, monkeypatch):
    client, module, _ = group_client
    local = client.get(
        f"/api/group/{ROOM}/context",
        query_string={"msg_id": "m-trigger", "at": "2026-08-01 09:00:00", "minutes": 30},
    )
    assert local.status_code == 200
    payload = local.get_json()
    assert payload["source"] == "local"
    assert payload["messages"]
    assert [msg["text"] for msg in payload["messages"] if msg["trigger"]] == ["刹车有异响"]

    def boom(*args, **kwargs):
        raise RuntimeError("scrm down")

    monkeypatch.setattr(module.stc_live, "chat_context", boom)
    failed = client.get(
        f"/api/group/{ROOM}/context",
        query_string={"at": "2026-08-02 09:00:00"},
    )
    assert failed.status_code == 502
    assert failed.get_json()["error"] == "暂时取不到聊天记录，请稍后重试"

    monkeypatch.setattr(
        module.stc_live,
        "chat_context",
        lambda *args, **kwargs: {"messages": [], "window": ["2026-08-02 08:30:00", "2026-08-02 09:40:00"]},
    )
    fallback = client.get(
        f"/api/group/{ROOM}/context",
        query_string={"at": "2026-08-02 09:00:00"},
    )
    assert fallback.status_code == 200
    assert fallback.get_json()["source"] == "scrm"

    assert client.get(f"/api/group/{ROOM}/context", query_string={"at": "bad"}).status_code == 400
    assert client.get("/api/group/abc/context", query_string={"at": "2026-08-02 09:00:00"}).status_code == 400


def test_store_and_score_pages_link_group_names_with_ranges_and_events(group_client):
    client, _, root = group_client
    store_html = client.get(
        f"/store/{quote(STORE, safe='')}?date_from=2026-08-01&date_to=2026-08-01"
    ).get_data(as_text=True)
    store_links = _group_links(store_html)
    assert any(link.startswith(f"/group/{ROOM}?") for link in store_links)
    event_links = [link for link in store_links if "event=" in link]
    assert any("event=kpi-ok" in link for link in event_links)
    for link in event_links:
        params = parse_qs(urlsplit(link).query)
        assert params["start"] == ["2026-08-01"]
        assert params["end"] == ["2026-08-01"]
    plain_links = [link for link in store_links if "event=" not in link]
    assert plain_links
    for link in plain_links:
        params = parse_qs(urlsplit(link).query)
        assert params["start"] == ["2026-08-01"]
        assert params["end"] == ["2026-08-01"]

    from flask import Flask
    from score_web import register_score

    score_app = Flask(__name__, template_folder=str(ROOT / "templates"))
    score_app.jinja_env.globals["ds_css"] = lambda: ""
    score_app.jinja_env.globals["ds_sprite"] = lambda: ""
    score_app.jinja_env.globals["ds_cards"] = lambda: ""
    register_score(score_app, str(root))
    score_html = score_app.test_client().get(
        f"/score/store?name={quote(STORE, safe='')}&start=2026-08-01&end=2026-08-01"
    ).get_data(as_text=True)
    score_links = _group_links(score_html)
    assert len(score_links) == 2
    assert {parse_qs(urlsplit(link).query)["event"][0] for link in score_links} == {"kpi-ok", "kpi-ex"}
    for link in score_links:
        params = parse_qs(urlsplit(link).query)
        assert params["start"] == ["2026-08-01"]
        assert params["end"] == ["2026-08-01"]
        assert 'href="' + link.replace("&", "&amp;") + '"' in score_html
    assert '打开群详情' not in score_html


def test_groups_js_unmatched_links_to_group_page():
    source = _read("static/js/groups.js")
    assert "href=\"/group/' + encodeURIComponent(row.room_id)" in source
    assert "data-sd-members" not in source


def test_stc_detail_group_links_and_members_detail_link():
    source = _read("static/js/stc-detail.js")
    assert "function groupHref" in source
    assert "return '/group/' + encodeURIComponent(roomId) + '?event='" in source
    assert "&start=" in source and "&end=" in source
    assert "打开群详情" in source
    assert 'data-act="members"' not in source


def test_timeout_event_room_index_exists(tmp_path):
    conn = connect(str(tmp_path / "timeout.db"))
    try:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(timeout_event)")}
    finally:
        conn.close()
    assert "idx_event_room" in indexes


def test_store_group_pager_assets_and_script():
    html = _read("templates/coverage_store.html")
    assert html.count('data-page-size="50"') == 2
    assert html.count("data-group-pager ") == 2
    assert "js/store-page.js" in html
    source = _read("static/js/store-page.js")
    assert "data-group-pager" in source
    assert "显示更多（剩 " in source
    assert "shown + size" in source


def test_group_js_context_row_has_cell_and_members_count_updates():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const src = fs.readFileSync('static/js/group.js', 'utf8');
function el(tag) {
  return {
    tagName: String(tag || '').toUpperCase(), children: [], cells: [], attrs: {}, dataset: {},
    className: '', innerHTML: '', textContent: '', hidden: false, nextSibling: null,
    appendChild(child) { this.children.push(child); if (child.tagName === 'TD') this.cells.push(child); return child; },
    setAttribute(key, value) { this.attrs[key] = String(value); },
    getAttribute(key) { return this.attrs[key] == null ? null : this.attrs[key]; },
    querySelector(selector) { return this.queries && this.queries[selector] ? this.queries[selector] : null; },
    closest() { return null; },
    classList: { add() {}, contains() { return false; } },
  };
}
const holder = el('div'); holder.attrs['data-group-room'] = 'room001';
const row = el('tr'); row.cells = [el('td'), el('td'), el('td'), el('td')];
const parent = { inserted: null, insertBefore(node) { this.inserted = node; node.parentNode = this; } };
row.parentNode = parent;
const button = el('button');
button.attrs = {'data-msg': 'm1', 'data-at': '2026-10-09 13:12:16', 'data-min': '30'};
button.closest = () => row;
const memberPanel = el('div'); memberPanel.hidden = false;
const memberBody = el('div');
const memberCount = el('span'); memberCount.textContent = '(−)';
const memberBox = el('div');
memberBox.queries = {
  '.ds-collapse-panel': memberPanel,
  '[data-members-body]': memberBody,
  '.ds-collapse-count': memberCount,
};
const listeners = {};
const document = {
  readyState: 'loading',
  querySelector(selector) { return selector === '[data-group-room]' ? holder : null; },
  querySelectorAll() { return []; },
  getElementById(id) { return id === 'group-members' ? memberBox : null; },
  createElement(tag) { return el(tag); },
  addEventListener(name, fn) { listeners[name] = fn; },
};
let fetchImpl = async () => ({
  ok: true, status: 200,
  json: async () => ({source: 'local', messages: [
    {role: 'customer', role_label: '客户', time: '2026-10-09 13:12:16', text: '电池有吗 弄两颗', trigger: true},
  ]}),
});
const window = {};
const context = {
  document, window, location: {search: ''}, URLSearchParams, console,
  fetch: (...args) => fetchImpl(...args), setTimeout: (fn) => fn(),
};
vm.runInNewContext(src, context);
listeners.DOMContentLoaded();
async function settle() { for (let i = 0; i < 20; i += 1) await Promise.resolve(); }
(async () => {
  window.StcGroup.context(button);
  await settle();
  assert(parent.inserted, 'context row not inserted');
  assert.equal(parent.inserted.tagName, 'TR');
  assert.equal(parent.inserted.children.length, 1, 'context row must contain its td');
  const cell = parent.inserted.children[0];
  assert.equal(cell.tagName, 'TD');
  assert.equal(cell.colSpan, 4);
  assert(cell.innerHTML.includes('来源：本地存档'));
  assert(cell.innerHTML.includes('trigger'));
  assert(cell.innerHTML.includes('电池有吗 弄两颗'));

  fetchImpl = async () => ({
    ok: true, status: 200,
    json: async () => ({members: [
      {name: '张三', role: 'staff', role_label: '员工', join_time: '2026-05-01 09:00:00'},
      {name: '李四', role: 'customer', role_label: '客户', join_time: '2026-05-02 10:00:00'},
    ]}),
  });
  window.StcGroup.members();
  await settle();
  assert.equal(memberBox.dataset.dsCount, '2');
  assert.equal(memberCount.textContent, '(2)');
  assert(memberBody.innerHTML.includes('姓名'));

  fetchImpl = async () => ({ok: false, status: 502, json: async () => ({error: 'SCRM 群成员接口暂不可用，请稍后重试'})});
  memberBox.setAttribute('data-members-loaded', '0');
  window.StcGroup.members();
  await settle();
  assert.equal(memberBox.dataset.dsCount, '—');
  assert.equal(memberCount.textContent, '(—)');
  assert(memberBody.innerHTML.includes('SCRM 群成员接口暂不可用，请稍后重试'));
  console.log('ok');
})().catch((error) => { console.error(error); process.exit(1); });
"""
    assert _run_node(script) == "ok"
