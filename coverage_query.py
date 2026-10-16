"""拉群覆盖页门店明细。全量群数 / 已拉机器人群数来自 chat_store。"""

from __future__ import annotations

import os
import sqlite3
from typing import Any
from urllib.parse import quote, urlencode

from group_keyword import list_theaters, open_ro
from list_filters import row_visible


def load_directory(db_path: str) -> list[dict[str, Any]]:
    """按门店汇总 chat_store：group_count 为群数，robot_group_count 为 in_robot 合计。"""
    if not db_path or not os.path.isfile(db_path):
        return []
    try:
        conn = open_ro(db_path)
    except sqlite3.Error:
        return []
    try:
        stats: dict[str, dict[str, Any]] = {}
        for row in conn.execute("SELECT store, theater, in_robot FROM chat_store"):
            name = (row["store"] or "").strip()
            if not name:
                continue
            item = stats.setdefault(
                name,
                {
                    "name": name,
                    "code": "",
                    "group_count": 0,
                    "robot_group_count": 0,
                    "theaters": {},
                },
            )
            item["group_count"] += 1
            if row["in_robot"]:
                item["robot_group_count"] += 1
            theater = (row["theater"] or "").strip()
            if theater:
                item["theaters"][theater] = item["theaters"].get(theater, 0) + 1
        rows: list[dict[str, Any]] = []
        for item in stats.values():
            theaters = item.pop("theaters")
            item["theater"] = max(theaters, key=theaters.get) if theaters else ""
            rows.append(item)
        rows.sort(key=lambda r: (-r["group_count"], -r["robot_group_count"], r["name"]))
        return rows
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def directory_theaters(db_path: str) -> list[str]:
    """战区选项：chat_store.theater 去重。"""
    if not db_path or not os.path.isfile(db_path):
        return []
    try:
        conn = open_ro(db_path)
    except sqlite3.Error:
        return []
    try:
        return list_theaters(conn)
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _store_stats(node: dict | None, dataset: str) -> dict:
    return ((node or {}).get(dataset) or {}).get("store_stats") or {}


def collect_store_names(data: dict | None) -> set[str]:
    names: set[str] = set()
    stats = (data or {}).get("stats") or {}
    for scope in ("national", "after_may1"):
        block = stats.get(scope) or {}
        names.update(_store_stats(block, "all"))
        names.update(_store_stats(block, "filter"))
    monthly = (stats.get("monthly") or {}).get("data") or {}
    for month in monthly.values():
        names.update(_store_stats(month, "all"))
        names.update(_store_stats(month, "filter"))
    return names


def _period_rows(all_stats: dict, robot_stats: dict, by_name: dict[str, dict]) -> list[dict[str, Any]]:
    rows = []
    for name in set(all_stats) | set(robot_stats):
        info = by_name.get(name) or {}
        rows.append(
            {
                "name": name,
                "code": info.get("code") or "",
                "theater": info.get("theater") or "",
                "group_count": int(all_stats.get(name) or 0),
                "robot_group_count": int(robot_stats.get(name) or 0),
            }
        )
    rows.sort(key=lambda r: (-r["group_count"], -r["robot_group_count"], r["name"]))
    return rows


def national_rows(directory: list[dict], all_stats: dict, robot_stats: dict) -> list[dict[str, Any]]:
    """全国明细优先用 chat_store 汇总。库里没有的店回退到 data.json 计数。"""
    if not directory:
        return _period_rows(all_stats, robot_stats, {})
    by_name = {row["name"]: dict(row) for row in directory}
    for name in set(all_stats) | set(robot_stats):
        if name in by_name:
            continue
        by_name[name] = {
            "name": name,
            "code": "",
            "theater": "",
            "group_count": int(all_stats.get(name) or 0),
            "robot_group_count": int(robot_stats.get(name) or 0),
        }
    rows = list(by_name.values())
    rows.sort(key=lambda r: (-r["group_count"], -r["robot_group_count"], r["name"]))
    return rows


def _pack(rows: list[dict]) -> dict[str, Any]:
    all_rows = [row for row in rows if int(row.get("group_count") or 0) > 0]
    robot_rows = [row for row in rows if int(row.get("robot_group_count") or 0) > 0]
    return {
        "all_rows": all_rows,
        "robot_rows": robot_rows,
        "all_groups": sum(int(row["group_count"]) for row in all_rows),
        "all_stores": len(all_rows),
        "robot_groups": sum(int(row["robot_group_count"]) for row in robot_rows),
        "robot_stores": len(robot_rows),
    }


def _filter(rows: list[dict], theater: str, query: str) -> list[dict]:
    return [row for row in rows if row_visible(row, theater, query)]


def build_coverage_view(data: dict | None, theater: str, query: str, db_path: str) -> dict[str, Any]:
    theater = (theater or "").strip()
    query = (query or "").strip()
    directory = load_directory(db_path)
    by_name = {row["name"]: row for row in directory}
    store_meta = {
        row["name"]: {"theater": row.get("theater") or "", "code": row.get("code") or ""}
        for row in directory
    }
    for name in collect_store_names(data):
        store_meta.setdefault(name, {"theater": "", "code": ""})

    theaters = directory_theaters(db_path)
    if theater and theater not in theaters:
        theaters = sorted(set(theaters) | {theater})

    stats = (data or {}).get("stats") or {}
    national_block = stats.get("national") or {}
    after_block = stats.get("after_may1") or {}
    national = _pack(
        _filter(
            national_rows(
                directory,
                _store_stats(national_block, "all"),
                _store_stats(national_block, "filter"),
            ),
            theater,
            query,
        )
    )
    after = _pack(
        _filter(
            _period_rows(
                _store_stats(after_block, "all"),
                _store_stats(after_block, "filter"),
                by_name,
            ),
            theater,
            query,
        )
    )
    qs_pairs = [(key, value) for key, value in (("theater", theater), ("q", query)) if value]
    return {
        "theater": theater,
        "q": query,
        "theaters": theaters,
        "store_meta": store_meta,
        "list_filter": {"theater": theater, "q": query},
        "qs": urlencode(qs_pairs),
        "national": national,
        "after_may1": after,
    }


def allowed_store_names(data: dict | None, theater: str, query: str, db_path: str) -> set[str] | None:
    """无筛选时返回 None（不过滤）。有筛选时返回允许导出的店名。"""
    theater = (theater or "").strip()
    query = (query or "").strip()
    if not theater and not query:
        return None
    directory = load_directory(db_path)
    by_name = {row["name"]: row for row in directory}
    names = set(by_name) | collect_store_names(data)
    allowed: set[str] = set()
    for name in names:
        info = by_name.get(name) or {}
        row = {
            "name": name,
            "code": info.get("code") or "",
            "theater": info.get("theater") or "",
        }
        if row_visible(row, theater, query):
            allowed.add(name)
    return allowed


def coverage_back_href(theater: str = "", q: str = "") -> str:
    """返回拉群覆盖列表，并带上当前战区与门店搜索。"""
    params: dict[str, str] = {}
    theater = (theater or "").strip()
    q = (q or "").strip()
    if theater:
        params["theater"] = theater
    if q:
        params["q"] = q
    if not params:
        return "/"
    return "/?" + urlencode(params)


def coverage_store_href(name: str, theater: str = "", q: str = "") -> str:
    """以门店名生成稳定地址；首页搜索另存为 back_q，避免影响群名搜索。"""
    params: dict[str, str] = {}
    theater = (theater or "").strip()
    q = (q or "").strip()
    if theater:
        params["theater"] = theater
    if q:
        params["back_q"] = q
    return "/store/" + quote(name, safe="") + ("?" + urlencode(params) if params else "")


def _primary_theater(counts: dict[str, int]) -> str:
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _group_row(row: sqlite3.Row) -> dict[str, Any]:
    in_robot = 1 if int(row["in_robot"] or 0) else 0
    return {
        "room_id": row["room_id"],
        "group_name": row["group_name"] or "",
        "in_robot": in_robot,
        "create_time": row["create_time"] or "",
    }


def store_coverage_detail(
    conn: sqlite3.Connection, store: str, *, q: str = "", group_from: str = "",
    group_to: str = "", date_from: str = "", date_to: str = "",
) -> dict[str, Any] | None:
    """一家门店的群名单。pending 为 in_robot 为 0 的群，robot 为已拉入的群。"""
    name = (store or "").strip()
    if not name:
        return None
    cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_store)")}
    create_col = "create_time" if "create_time" in cols else "NULL AS create_time"
    rows = conn.execute(
        f"SELECT room_id, store, theater, group_name, in_robot, {create_col} FROM chat_store "
        "WHERE TRIM(store) = ?",
        (name,),
    ).fetchall()
    theaters: dict[str, int] = {}
    pending = []
    robot = []
    query = q.casefold()
    for row in rows:
        item = _group_row(row)
        day = item["create_time"][:10]
        if (not query or query in (item["group_name"] + " " + item["room_id"]).casefold()) and (
            not day or ((not group_from or day >= group_from) and (not group_to or day <= group_to))
        ):
            (robot if item["in_robot"] else pending).append(item)
        label = (row["theater"] or "").strip()
        if label:
            theaters[label] = theaters.get(label, 0) + 1
    pending.sort(key=lambda item: (item["group_name"].casefold(), item["room_id"]))
    robot.sort(key=lambda item: (item["group_name"].casefold(), item["room_id"]))
    event_cols = {row[1] for row in conn.execute("PRAGMA table_info(timeout_event)")}
    room_name = "e.room_name" if "room_name" in event_cols else "NULL"
    events = []
    has_events = False
    if event_cols:
        for row in conn.execute(
            f"SELECT e.*, COALESCE(NULLIF({room_name}, ''), NULLIF(c.group_name, ''), e.room_id) AS group_name "
            "FROM timeout_event e LEFT JOIN chat_store c ON c.room_id = e.room_id "
            "WHERE TRIM(e.store) = ? ORDER BY e.created_at DESC, e.msg_id, e.rule_id", (name,)
        ):
            has_events = True
            event = dict(row)
            day = event["created_at"][:10]
            text = " ".join(str(event.get(key) or "") for key in ("group_name", "rule_name", "msg_id", "room_id"))
            if (not query or query in text.casefold()) and (not date_from or day >= date_from) and (not date_to or day <= date_to):
                events.append(event)
    if not rows and not has_events:
        return None
    return {
        "store": name,
        "theater": _primary_theater(theaters),
        "group_count": len(rows),
        "robot_count": sum(bool(row["in_robot"]) for row in rows),
        "pending_count": sum(not row["in_robot"] for row in rows),
        "events": events,
        "pending": pending,
        "robot": robot,
    }
