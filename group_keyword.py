"""群名关键字统计：基于 chat_store 全量客户群做大小写不敏感的子串统计。"""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import quote

from list_filters import store_query_match
from timeout_store import is_readonly_open_error


class KeywordError(ValueError):
    """非法的关键字参数。"""


def open_ro(db_path: str) -> sqlite3.Connection:
    """只读打开 sqlite；只读副本用 immutable 避免创建 -wal/-shm。"""
    uri = "file:" + quote(db_path) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.OperationalError as exc:
        if not is_readonly_open_error(exc):
            conn.close()
            raise
        conn.close()
        conn = sqlite3.connect(uri + "&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
    return conn


def list_theaters(conn: sqlite3.Connection) -> list[str]:
    return [
        row["theater"]
        for row in conn.execute(
            "SELECT DISTINCT theater FROM chat_store "
            "WHERE theater IS NOT NULL AND theater != '' ORDER BY theater"
        )
    ]


def query_keyword_groups(
    conn: sqlite3.Connection,
    keyword: str,
    theater: str | None = None,
    only_robot: bool = False,
    store_q: str | None = None,
) -> dict[str, Any]:
    """按群名子串统计。theater 与门店搜索同时作用于所有统计；空关键字抛 KeywordError。"""
    kw = str(keyword or "").strip()
    if not kw:
        raise KeywordError("关键字不能为空")
    theater = str(theater or "").strip()
    store_q = str(store_q or "").strip()
    only_robot = bool(only_robot)

    where = ["instr(lower(group_name), lower(?)) > 0"]
    params: list[Any] = [kw]
    if theater:
        where.append("theater = ?")
        params.append(theater)
    if only_robot:
        where.append("in_robot = 1")

    rows = [
        {
            "room_id": row["room_id"] or "",
            "store": row["store"] or "",
            "theater": row["theater"] or "",
            "group_name": row["group_name"] or "",
            "in_robot": int(row["in_robot"] or 0),
        }
        for row in conn.execute(
            f"SELECT room_id, store, theater, group_name, in_robot FROM chat_store "
            f"WHERE {' AND '.join(where)}",
            params,
        )
    ]
    rows.sort(key=lambda r: r["group_name"])
    if store_q:
        rows = [row for row in rows if store_query_match(row["store"], "", store_q)]

    by_theater_map: dict[str, dict[str, Any]] = {}
    by_store_map: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["theater"]:
            agg = by_theater_map.setdefault(
                row["theater"], {"theater": row["theater"], "group_count": 0, "stores": set()}
            )
            agg["group_count"] += 1
            if row["store"]:
                agg["stores"].add(row["store"])
        if row["store"]:
            agg = by_store_map.setdefault(
                row["store"],
                {"store": row["store"], "theater": row["theater"], "group_count": 0, "robot_count": 0},
            )
            agg["group_count"] += 1
            agg["robot_count"] += row["in_robot"]
            if not agg["theater"] and row["theater"]:
                agg["theater"] = row["theater"]

    by_theater = [
        {"theater": agg["theater"], "group_count": agg["group_count"], "store_count": len(agg["stores"])}
        for agg in by_theater_map.values()
    ]
    by_theater.sort(key=lambda x: (-x["group_count"], x["theater"]))
    by_store = sorted(by_store_map.values(), key=lambda x: (-x["group_count"], x["store"]))

    unmatched = [
        {"room_id": row["room_id"], "group_name": row["group_name"], "in_robot": row["in_robot"]}
        for row in rows
        if not row["store"] or not row["theater"]
    ]

    return {
        "keyword": kw,
        "theater": theater,
        "q": store_q,
        "only_robot": only_robot,
        "total": len(rows),
        "robot_total": sum(row["in_robot"] for row in rows),
        "matched_store_total": len({row["store"] for row in rows if row["store"]}),
        "unmatched_total": len(unmatched),
        "by_theater": by_theater,
        "by_store": by_store,
        "unmatched": unmatched,
        "groups": [
            {
                "group_name": row["group_name"],
                "store": row["store"],
                "theater": row["theater"],
                "in_robot": row["in_robot"],
            }
            for row in rows
        ],
        "theaters": list_theaters(conn),
        "synced_at": None,  # 由 app 从 data.json updated_at 填充（与 chat_store 同一次同步写入）
    }


class GroupListError(ValueError):
    """非法的群目录分页/筛选参数。"""


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def query_group_list(
    conn: sqlite3.Connection,
    q: str | None = None,
    theater: str | None = None,
    store: str | None = None,
    robot: str | None = "all",
    page: int | str | None = 1,
    size: int | str | None = 50,
) -> dict[str, Any]:
    """群目录：按群名/群 ID、战区、门店、机器人状态筛选并分页。"""
    try:
        page_no = int(page if page not in (None, "") else 1)
    except (TypeError, ValueError):
        raise GroupListError("page 必须是正整数") from None
    try:
        page_size = int(size if size not in (None, "") else 50)
    except (TypeError, ValueError):
        raise GroupListError("size 必须是正整数") from None
    if page_no < 1:
        raise GroupListError("page 必须是正整数")
    if page_size < 1:
        raise GroupListError("size 必须是正整数")
    page_size = min(page_size, 100)

    keyword = str(q or "").strip()
    theater = str(theater or "").strip()
    store = str(store or "").strip()
    robot = str(robot or "all").strip().lower()
    if robot not in ("all", "1", "0"):
        raise GroupListError("robot 只能是 all、1 或 0")

    where: list[str] = []
    params: list[Any] = []
    if keyword:
        where.append(
            "(instr(lower(ifnull(group_name, '')), lower(?)) > 0 "
            "OR instr(lower(ifnull(room_id, '')), lower(?)) > 0)"
        )
        params.extend([keyword, keyword])
    if theater:
        where.append("theater = ?")
        params.append(theater)
    if store:
        where.append("instr(lower(ifnull(store, '')), lower(?)) > 0")
        params.append(store)
    if robot == "1":
        where.append("in_robot = 1")
    elif robot == "0":
        where.append("in_robot = 0")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    cols = _table_columns(conn, "chat_store")
    owner_name_col = "owner_name" if "owner_name" in cols else "NULL AS owner_name"
    member_count_col = "member_count" if "member_count" in cols else "NULL AS member_count"
    create_time_col = "create_time" if "create_time" in cols else "NULL AS create_time"
    total = int(
        conn.execute(f"SELECT COUNT(*) AS n FROM chat_store{where_sql}", params).fetchone()["n"] or 0
    )
    offset = (page_no - 1) * page_size
    rows = [
        {
            "room_id": row["room_id"] or "",
            "group_name": row["group_name"] or "",
            "store": row["store"] or "",
            "theater": row["theater"] or "",
            "in_robot": int(row["in_robot"] or 0),
            "owner_name": row["owner_name"],
            "member_count": row["member_count"],
            "create_time": row["create_time"] or "",
        }
        for row in conn.execute(
            f"SELECT room_id, group_name, store, theater, in_robot, "
            f"{owner_name_col}, {member_count_col}, {create_time_col} "
            f"FROM chat_store{where_sql} "
            "ORDER BY create_time DESC, room_id "
            "LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        )
    ]
    return {
        "total": total,
        "page": page_no,
        "size": page_size,
        "rows": rows,
        "theaters": list_theaters(conn),
    }
