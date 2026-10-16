"""独立门店路由、旧库兼容及两类日期筛选。"""

import importlib
import json
import sqlite3
from unittest.mock import patch

import pytest

from coverage_query import coverage_store_href, store_coverage_detail
from tests.test_coverage_store import _minimal_stats
from timeout_store import connect, replace_context

STORE = "猛士汽车武汉门店"
LONG_NAME = "VIP 车主交付服务群" * 12


@pytest.fixture
def store_client(tmp_path, monkeypatch):
    db = tmp_path / "timeout.db"
    conn = connect(str(db))
    replace_context(conn, robot_chat_ids=["r1"], employee_ids=[], group_rows=[
        {"group_id": "r1", "group_name": LONG_NAME, "store": STORE, "create_time": "2026-05-01T00:00:00+08:00"},
        {"group_id": "r2", "group_name": "交付服务群", "store": STORE, "create_time": "2026-05-31T23:59:59+08:00"},
        {"group_id": "r3", "group_name": "未知日期群", "store": STORE},
        {"group_id": "r4", "group_name": "六月服务群", "store": STORE, "create_time": "2026-06-01T00:00:00+08:00"},
    ])
    conn.executemany(
        "INSERT INTO timeout_event(msg_id, rule_id, rule_name, room_id, store, created_at, room_name) VALUES (?, 'rule', ?, ?, ?, ?, ?)",
        [
            ("msg-start", "首次回复超时", "r1", STORE, "2026-07-01 00:00:00", ""),
            ("msg-end", "跟进超时", "gone", STORE, "2026-07-31 23:59:59", "历史归档群"),
            ("msg-next", "首次回复超时", "r2", STORE, "2026-08-01 00:00:00", ""),
            ("msg-other", "首次回复超时", "r1", "其他门店", "2026-07-01 12:00:00", ""),
        ],
    )
    conn.commit()
    conn.close()
    data = tmp_path / "data.json"
    data.write_text(json.dumps(_minimal_stats()), encoding="utf-8")
    monkeypatch.setenv("CLEANER_DB_PATH", str(db))
    monkeypatch.setenv("CLEANER_DATA_FILE", str(data))
    monkeypatch.delenv("MHERO_AUTH", raising=False)
    monkeypatch.delenv("MHERO_AUTH_CLEANER", raising=False)
    import app
    module = importlib.reload(app)
    yield module.app.test_client(), module, db


def body(client, **query):
    response = client.get(coverage_store_href(STORE), query_string=query)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def section(html, section_id):
    return html.split(f'id="{section_id}"', 1)[1].split('<div class="stat-block', 1)[0]


def test_new_route_full_lists_and_events(store_client):
    client, _, _ = store_client
    html = body(client)
    assert f"<h1>{STORE}</h1>" in html
    assert "群总数 4" in html and "已拉机器人 1" in html and "未拉机器人 3" in html
    assert LONG_NAME in section(html, "robot-groups")
    assert "交付服务群" in section(html, "pending-groups")
    events = section(html, "timeout-events")
    assert "msg-start" in events and "msg-end" in events and "msg-next" in events
    assert "msg-other" not in events
    assert "历史归档群" in events and LONG_NAME in events
    assert events.index("msg-next") < events.index("msg-end") < events.index("msg-start")
    assert html.count('<th data-card-title>群名</th>') == 3
    assert "ant-table-cards" in html


def test_legacy_redirect_preserves_home_search(store_client):
    client, _, _ = store_client
    response = client.get("/coverage/store", query_string={"name": STORE, "q": "武汉", "theater": "武汉特区"})
    assert response.status_code in (301, 302)
    assert response.location == coverage_store_href(STORE, "武汉特区", "武汉")
    html = client.get(response.location).get_data(as_text=True)
    assert LONG_NAME in html
    assert 'name="q" value=""' in html


def test_group_dates_inclusive_and_unknown_visible(store_client):
    client, _, _ = store_client
    html = body(client, group_from="2026-05-01", group_to="2026-05-31")
    assert LONG_NAME in section(html, "robot-groups")
    pending = section(html, "pending-groups")
    assert "交付服务群" in pending and "未知日期群" in pending
    assert "六月服务群" not in pending
    assert "创建日期未知的群在日期筛选时仍显示" in html
    assert "msg-next" in section(html, "timeout-events")
    html = body(client, group_from="2026-06-01")
    assert LONG_NAME not in section(html, "robot-groups")


def test_timeout_dates_and_rule_search(store_client):
    client, _, _ = store_client
    html = body(client, date_from="2026-07-01", date_to="2026-07-31")
    events = section(html, "timeout-events")
    assert "msg-start" in events and "msg-end" in events and "msg-next" not in events
    assert "六月服务群" in section(html, "pending-groups")
    html = body(client, q="跟进")
    assert "msg-end" in section(html, "timeout-events")
    assert "msg-start" not in section(html, "timeout-events")
    assert "暂无已拉机器人的群" in html


def test_search_group_casefold_and_no_results(store_client):
    client, _, _ = store_client
    html = body(client, q="vip")
    assert LONG_NAME in section(html, "robot-groups")
    assert "msg-start" in section(html, "timeout-events")
    assert "msg-end" not in section(html, "timeout-events")
    html = body(client, q="无匹配关键词")
    assert "暂无超时记录" in html and "暂无未拉机器人的群" in html


def test_directory_code_and_encoded_slash(store_client):
    client, module, _ = store_client
    with patch.object(module, "load_directory", return_value=[{"name": STORE, "code": "WH001"}]):
        html = client.get("/store/WH001").get_data(as_text=True)
        assert f"<h1>{STORE}</h1>" in html
    with patch.object(module, "load_directory", return_value=[{"name": "测试/门店", "code": ""}]):
        response = client.get(coverage_store_href("测试/门店"))
        assert response.status_code == 200
        assert "<h1>测试/门店</h1>" in response.get_data(as_text=True)


def test_invalid_date(store_client):
    client, _, _ = store_client
    assert client.get(coverage_store_href(STORE), query_string={"date_from": "bad"}).status_code == 400


def test_historical_events_without_current_groups(store_client):
    client, _, db = store_client
    conn = connect(str(db))
    conn.execute("DELETE FROM chat_store")
    conn.commit()
    conn.close()
    html = body(client, q="跟进")
    assert "msg-end" in section(html, "timeout-events")
    assert "群总数 0" in html
    html = body(client, q="不匹配")
    assert "暂无超时记录" in html
    assert "没有这家门店" not in html


def test_old_schema_read_and_migration(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chat_store(room_id TEXT PRIMARY KEY, store TEXT, theater TEXT, group_name TEXT, in_robot INTEGER)")
    conn.execute("INSERT INTO chat_store VALUES ('old', ?, '', '旧群', 0)", (STORE,))
    conn.commit()
    detail = store_coverage_detail(conn, STORE, group_from="2026-01-01", group_to="2026-01-31")
    assert detail["pending"][0]["create_time"] == ""
    conn.close()
    conn = connect(path)
    assert conn.execute("SELECT create_time FROM chat_store").fetchone()[0] is None
    replace_context(conn, robot_chat_ids=[], employee_ids=[], group_rows=[
        {"group_id": "new", "store": STORE, "create_time": "2026-10-16T12:30:00+08:00"}
    ])
    assert conn.execute("SELECT create_time FROM chat_store").fetchone()[0] == "2026-10-16T12:30:00+08:00"
    conn.close()
