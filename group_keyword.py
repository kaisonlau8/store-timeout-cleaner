"""群名关键字统计：基于 chat_store 全量客户群做大小写不敏感的子串统计。"""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import quote

from timeout_store import get_meta


class KeywordError(ValueError):
    """非法的关键字参数。"""


def open_ro(db_path: str) -> sqlite3.Connection:
    """只读打开 sqlite（uri mode=ro），供统计接口使用，避免误写。"""
    conn = sqlite3.connect("file:" + quote(db_path) + "?mode=ro", uri=True)
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
) -> dict[str, Any]:
    """按群名子串统计。theater 过滤作用于所有统计；空关键字抛 KeywordError。"""
    kw = str(keyword or "").strip()
    if not kw:
        raise KeywordError("关键字不能为空")
    theater = str(theater or "").strip()
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
