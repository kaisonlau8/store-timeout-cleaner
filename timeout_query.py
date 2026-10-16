"""超时提醒统计查询。"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from timeout_store import connect, get_meta, list_imports

SHANGHAI = ZoneInfo("Asia/Shanghai")


def db_path(root: str) -> str:
    return os.path.join(root, "timeout.db")


def _open(root: str) -> sqlite3.Connection | None:
    path = db_path(root)
    if not os.path.isfile(path):
        return None
    return connect(path)


def _parse_date(value: str | None, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return fallback


def _align_start(day: date, grain: str) -> date:
    if grain == "month":
        return date(day.year, day.month, 1)
    if grain == "quarter":
        month = ((day.month - 1) // 3) * 3 + 1
        return date(day.year, month, 1)
    return day


def _next_bucket(day: date, grain: str) -> date:
    if grain == "day":
        return day + timedelta(days=1)
    if grain == "month":
        if day.month == 12:
            return date(day.year + 1, 1, 1)
        return date(day.year, day.month + 1, 1)
    month = day.month + 3
    if month > 12:
        return date(day.year + 1, month - 12, 1)
    return date(day.year, month, 1)


def _bucket_label(day: date, grain: str) -> str:
    if grain == "day":
        return day.isoformat()
    if grain == "month":
        return day.strftime("%Y-%m")
    return f"{day.year}-Q{(day.month - 1) // 3 + 1}"


def _iter_labels(start: date, end: date, grain: str) -> list[str]:
    start = _align_start(start, grain)
    labels: list[str] = []
    cur = start
    while cur <= end:
        labels.append(_bucket_label(cur, grain))
        cur = _next_bucket(cur, grain)
    return labels


def _bucket_sql(grain: str) -> str:
    if grain == "day":
        return "substr(created_at, 1, 10)"
    if grain == "month":
        return "substr(created_at, 1, 7)"
    return (
        "printf('%s-Q%s', substr(created_at, 1, 4), "
        "((CAST(substr(created_at, 6, 2) AS INTEGER) - 1) / 3) + 1)"
    )


def _scope_clause(
    scope: str,
    ident: str,
    theater: str = "",
    q: str = "",
) -> tuple[str, list[Any]]:
    """战区与门店搜索同时生效。未传 theater/q 时仍兼容 scope=theater|store。"""
    clauses: list[str] = []
    params: list[Any] = []
    theater = (theater or "").strip()
    q = (q or "").strip()
    ident = (ident or "").strip()
    if theater:
        clauses.append("theater = ?")
        params.append(theater)
    elif scope == "theater" and ident:
        clauses.append("theater = ?")
        params.append(ident)
    if q:
        clauses.append("instr(lower(store), lower(?)) > 0")
        params.append(q)
    elif scope == "store" and ident:
        clauses.append("store = ?")
        params.append(ident)
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


def query_meta(root: str) -> dict[str, Any]:
    today = datetime.now(SHANGHAI).date()
    empty = {
        "theaters": [],
        "stores": [],
        "rules": [],
        "imports": [],
        "min_date": None,
        "max_date": None,
        "updated_at": None,
        "event_count": 0,
    }
    conn = _open(root)
    if conn is None:
        return empty
    try:
        theaters = [
            row["theater"]
            for row in conn.execute(
                "SELECT DISTINCT theater FROM timeout_event "
                "WHERE theater IS NOT NULL AND theater != '' ORDER BY theater"
            )
        ]
        stores = [
            {"name": row["store"], "theater": row["theater"] or ""}
            for row in conn.execute(
                "SELECT store, MAX(theater) AS theater FROM timeout_event "
                "WHERE store IS NOT NULL AND store != '' GROUP BY store ORDER BY store"
            )
        ]
        rules = [
            {"rule_id": row["rule_id"], "rule_name": row["rule_name"], "chat_time_out": row["chat_time_out"]}
            for row in conn.execute(
                "SELECT rule_id, rule_name, chat_time_out FROM timeout_rule ORDER BY chat_time_out, rule_name"
            )
        ]
        if not rules:
            rules = [
                {"rule_id": row["rule_id"], "rule_name": row["rule_name"], "chat_time_out": None}
                for row in conn.execute(
                    "SELECT DISTINCT rule_id, rule_name FROM timeout_event ORDER BY rule_name"
                )
            ]
        bounds = conn.execute(
            "SELECT MIN(substr(created_at,1,10)) AS min_d, MAX(substr(created_at,1,10)) AS max_d, COUNT(*) AS n "
            "FROM timeout_event"
        ).fetchone()
        return {
            "theaters": theaters,
            "stores": stores,
            "rules": rules,
            "imports": [],
            "source": "api",
            "last_run": get_meta(conn, "last_run"),
            "min_date": bounds["min_d"] if bounds else None,
            "max_date": bounds["max_d"] if bounds else today.isoformat(),
            "updated_at": get_meta(conn, "updated_at"),
            "event_count": int(bounds["n"] or 0) if bounds else 0,
        }
    finally:
        conn.close()


def query_chart(
    root: str,
    *,
    scope: str = "national",
    ident: str = "",
    grain: str = "month",
    start: str | None = None,
    end: str | None = None,
    theater: str = "",
    q: str = "",
) -> dict[str, Any]:
    if grain not in {"day", "month", "quarter"}:
        grain = "month"
    if scope not in {"national", "theater", "store"}:
        scope = "national"

    today = datetime.now(SHANGHAI).date()
    start_d = _parse_date(start, date(2026, 5, 1))
    end_d = _parse_date(end, today)
    if end_d < start_d:
        start_d, end_d = end_d, start_d
    start_sql = start_d.isoformat() + " 00:00:00"
    end_sql = (end_d + timedelta(days=1)).isoformat() + " 00:00:00"
    labels = _iter_labels(start_d, end_d, grain)
    empty_totals = {
        "triggers": 0,
        "messages": 0,
        "rooms": 0,
        "stores": 0,
        "rules": [],
    }
    empty = {
        "labels": labels,
        "series": [],
        "totals": empty_totals,
        "stores": [],
        "grain": grain,
        "scope": scope,
        "id": ident,
        "theater": (theater or "").strip(),
        "q": (q or "").strip(),
        "start": start_d.isoformat(),
        "end": end_d.isoformat(),
    }
    conn = _open(root)
    if conn is None:
        return empty
    try:
        extra, extra_params = _scope_clause(scope, ident, theater=theater, q=q)
        base_where = f"created_at >= ? AND created_at < ?{extra}"
        # 计入 = 未被剔除；同一消息命中多条规则时「重复触发」也按各规则计次，但消息数去重
        where = base_where + " AND ifnull(exclude_reason, '') IN ('', '重复触发(同一消息)')"
        params: list[Any] = [start_sql, end_sql, *extra_params]
        bucket = _bucket_sql(grain)

        rule_rows = list(
            conn.execute(
                f"""
                SELECT {bucket} AS bucket, rule_id, MAX(rule_name) AS rule_name,
                       COUNT(*) AS triggers,
                       COUNT(DISTINCT msg_id) AS messages,
                       COUNT(DISTINCT room_id) AS rooms
                FROM timeout_event
                WHERE {where}
                GROUP BY bucket, rule_id
                """,
                params,
            )
        )
        msg_rows = {
            row["bucket"]: int(row["messages"])
            for row in conn.execute(
                f"""
                SELECT {bucket} AS bucket, COUNT(DISTINCT msg_id) AS messages
                FROM timeout_event
                WHERE {where}
                GROUP BY bucket
                """,
                params,
            )
        }
        rules_meta = []
        seen: set[str] = set()
        for row in conn.execute(
            "SELECT rule_id, rule_name, chat_time_out FROM timeout_rule ORDER BY chat_time_out, rule_name"
        ):
            rules_meta.append(row)
            seen.add(row["rule_id"])
        for row in rule_rows:
            if row["rule_id"] not in seen:
                rules_meta.append(row)
                seen.add(row["rule_id"])

        series = [
            {
                "key": "messages",
                "name": "计入超时的消息",
                "data": [msg_rows.get(label, 0) for label in labels],
            }
        ]
        by_rule: dict[str, dict[str, int]] = {}
        rule_names: dict[str, str] = {}
        for row in rule_rows:
            rule_names[row["rule_id"]] = row["rule_name"] or row["rule_id"]
            by_rule.setdefault(row["rule_id"], {})[row["bucket"]] = int(row["triggers"])
        for meta in rules_meta:
            rid = meta["rule_id"]
            name = meta["rule_name"] if "rule_name" in meta.keys() else rule_names.get(rid, rid)
            series.append(
                {
                    "key": f"rule:{rid}",
                    "name": name or rid,
                    "rule_id": rid,
                    "data": [by_rule.get(rid, {}).get(label, 0) for label in labels],
                }
            )

        totals_row = conn.execute(
            f"""
            SELECT COUNT(*) AS triggers,
                   COUNT(DISTINCT msg_id) AS messages,
                   COUNT(DISTINCT room_id) AS rooms,
                   COUNT(DISTINCT CASE WHEN store IS NOT NULL AND store != '' THEN store END) AS stores
            FROM timeout_event
            WHERE {where}
            """,
            params,
        ).fetchone()
        excluded_row = conn.execute(
            f"""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN exclude_reason LIKE 'LLM%' THEN 1 ELSE 0 END) AS llm
            FROM timeout_event
            WHERE {base_where} AND ifnull(exclude_reason, '') NOT IN ('', '重复触发(同一消息)')
            """,
            params,
        ).fetchone()
        rule_totals = [
            {
                "rule_id": row["rule_id"],
                "rule_name": row["rule_name"] or row["rule_id"],
                "triggers": int(row["triggers"]),
                "messages": int(row["messages"]),
                "rooms": int(row["rooms"]),
            }
            for row in conn.execute(
                f"""
                SELECT rule_id, MAX(rule_name) AS rule_name,
                       COUNT(*) AS triggers,
                       COUNT(DISTINCT msg_id) AS messages,
                       COUNT(DISTINCT room_id) AS rooms
                FROM timeout_event
                WHERE {where}
                GROUP BY rule_id
                ORDER BY triggers DESC
                """,
                params,
            )
        ]
        store_rows = [
            {
                "store": row["store"],
                "theater": row["theater"] or "",
                "messages": int(row["messages"]),
                "rooms": int(row["rooms"]),
                "triggers": int(row["triggers"]),
                "by_rule": {},
            }
            for row in conn.execute(
                f"""
                SELECT store, MAX(theater) AS theater,
                       COUNT(*) AS triggers,
                       COUNT(DISTINCT msg_id) AS messages,
                       COUNT(DISTINCT room_id) AS rooms
                FROM timeout_event
                WHERE {where} AND store IS NOT NULL AND store != ''
                GROUP BY store
                ORDER BY messages DESC, store
                """,
                params,
            )
        ]
        store_index = {row["store"]: row for row in store_rows}
        for row in conn.execute(
            f"""
            SELECT store, rule_id, MAX(rule_name) AS rule_name, COUNT(*) AS triggers
            FROM timeout_event
            WHERE {where} AND store IS NOT NULL AND store != ''
            GROUP BY store, rule_id
            """,
            params,
        ):
            target = store_index.get(row["store"])
            if target is not None:
                target["by_rule"][row["rule_id"]] = {
                    "rule_name": row["rule_name"] or row["rule_id"],
                    "triggers": int(row["triggers"]),
                }

        return {
            "labels": labels,
            "series": series,
            "totals": {
                "triggers": int(totals_row["triggers"] or 0) if totals_row else 0,
                "messages": int(totals_row["messages"] or 0) if totals_row else 0,
                "rooms": int(totals_row["rooms"] or 0) if totals_row else 0,
                "stores": int(totals_row["stores"] or 0) if totals_row else 0,
                "excluded": int(excluded_row["n"] or 0) if excluded_row else 0,
                "llm_excluded": int(excluded_row["llm"] or 0) if excluded_row else 0,
                "rules": rule_totals,
            },
            "stores": store_rows,
            "grain": grain,
            "scope": scope,
            "id": ident,
            "theater": (theater or "").strip(),
            "q": (q or "").strip(),
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
        }
    finally:
        conn.close()
