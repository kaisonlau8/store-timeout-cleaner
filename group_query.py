"""群详情页的只读数据：群信息、超时三分组、本地聊天上下文。

KPI 口径与评分页完全一致：计入 KPI 的规则命中由 ``score_query.prepare_events``
统一过滤、应用评分剔除规则和 LLM 缓存；本模块只按 room_id 再筛一次。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from score_query import prepare_events
from timeout_algo import non_kpi_rules

SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_EVENTS = 500
DUP_REASON = "重复触发(同一消息)"

_NON_TEXT = {
    "image": "[图片]",
    "voice": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "emotion": "[表情]",
    "link": "[链接]",
    "weapp": "[小程序]",
    "revoke": "[撤回]",
}
_ROLE_LABEL = {"customer": "客户", "staff": "员工", "robot": "机器人"}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def group_info(conn: sqlite3.Connection, room_id: str) -> dict[str, Any] | None:
    """chat_store 中的群；缺失时用最近一条 timeout_event 的群名/门店/战区兜底。"""
    cols = _columns(conn, "chat_store")
    if cols:
        create_col = "create_time" if "create_time" in cols else "NULL AS create_time"
        group_col = "group_name" if "group_name" in cols else "'' AS group_name"
        store_col = "store" if "store" in cols else "'' AS store"
        theater_col = "theater" if "theater" in cols else "'' AS theater"
        robot_col = "in_robot" if "in_robot" in cols else "0 AS in_robot"
        try:
            row = conn.execute(
                f"SELECT room_id, {group_col}, {store_col}, {theater_col}, {create_col}, {robot_col} "
                "FROM chat_store WHERE room_id = ?",
                (room_id,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            return {
                "room_id": row["room_id"] or "",
                "group_name": row["group_name"] or "",
                "store": row["store"] or "",
                "theater": row["theater"] or "",
                "create_time": row["create_time"] or "",
                "in_robot": int(row["in_robot"] or 0),
            }

    event_cols = _columns(conn, "timeout_event")
    if not event_cols or "room_id" not in event_cols:
        return None
    room_name = "room_name" if "room_name" in event_cols else "NULL AS room_name"
    store = "store" if "store" in event_cols else "NULL AS store"
    theater = "theater" if "theater" in event_cols else "NULL AS theater"
    try:
        row = conn.execute(
            f"SELECT room_id, {room_name}, {store}, {theater} FROM timeout_event "
            "WHERE room_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (room_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is None:
        return None
    return {
        "room_id": row["room_id"] or room_id,
        "group_name": row["room_name"] or "",
        "store": row["store"] or "",
        "theater": row["theater"] or "",
        "create_time": "",
        "in_robot": 0,
    }


def _minutes_maps(conn: sqlite3.Connection) -> tuple[dict[str, int], dict[str, int]]:
    """timeout_rule.chat_time_out 优先，calc_rule.minutes 兜底。"""
    by_rule: dict[str, int] = {}
    try:
        for row in conn.execute("SELECT rule_id, chat_time_out FROM timeout_rule"):
            value = int(row["chat_time_out"] or 0)
            if value > 0:
                by_rule[str(row["rule_id"])] = value
    except (sqlite3.Error, TypeError, ValueError):
        pass
    by_key: dict[str, int] = {}
    try:
        # 同一 rule_key 的多个版本按生效日升序，最后一次写入即最新版本。
        for row in conn.execute("SELECT rule_key, minutes FROM calc_rule ORDER BY effective_from, id"):
            value = int(row["minutes"] or 0)
            if value > 0:
                by_key[str(row["rule_key"])] = value
    except (sqlite3.Error, TypeError, ValueError):
        pass
    return by_rule, by_key


def _minutes(rule_id: str, maps: tuple[dict[str, int], dict[str, int]]) -> int:
    by_rule, by_key = maps
    rule_id = str(rule_id or "")
    return int(by_rule.get(rule_id) or by_key.get(rule_id) or 30)


def _item(
    *,
    msg_id: Any,
    rule_id: Any,
    rule_name: Any,
    created_at: Any,
    content: Any,
    reason: Any,
    minutes: int,
) -> dict[str, Any]:
    text = "" if content is None else str(content)
    return {
        "msg_id": str(msg_id or ""),
        "rule_id": str(rule_id or ""),
        "rule_name": str(rule_name or ""),
        "created_at": str(created_at or ""),
        "content": text,
        "message": text,
        "reason": str(reason or ""),
        "minutes": int(minutes or 30),
    }


def _sort_cap(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items.sort(key=lambda item: (item.get("created_at") or "", item.get("msg_id") or ""), reverse=True)
    return items[:MAX_EVENTS]


def group_events(root: str, conn: sqlite3.Connection, room_id: str, start: Any, end: Any) -> dict[str, list[dict[str, Any]]]:
    """返回 {kpi_counted, kpi_excluded, display_only}，每组按时间倒序、最多 500 条。"""
    first, last = _as_date(start), _as_date(end)
    maps = _minutes_maps(conn)

    # --- 计入 KPI 的规则：与 /score/store 同一 prepare_events 管线 ---
    events, _unmatched, _missing, _meta = prepare_events(root, first, last)
    kpi_counted: list[dict[str, Any]] = []
    kpi_excluded: list[dict[str, Any]] = []
    for ev in events:
        if (ev.get("room_id") or "") != room_id:
            continue
        item = _item(
            msg_id=ev.get("msg_id"),
            rule_id=ev.get("rule_id"),
            rule_name=ev.get("rule_name"),
            created_at=ev.get("created_at"),
            content=ev.get("msg"),
            reason=ev.get("reason"),
            minutes=_minutes(ev.get("rule_id"), maps),
        )
        (kpi_excluded if item["reason"] else kpi_counted).append(item)

    # --- 非 KPI 规则：只从 timeout_event 取「仅展示」；其它剔除原因并入已剔除 ---
    display_only: list[dict[str, Any]] = []
    try:
        non_kpi_ids = non_kpi_rules(root)[0]
    except Exception:
        non_kpi_ids = set()
    if non_kpi_ids:
        event_cols = _columns(conn, "timeout_event")
        exclude_col = "exclude_reason" if "exclude_reason" in event_cols else "'' AS exclude_reason"
        content_col = "content" if "content" in event_cols else "'' AS content"
        room_name_col = "room_name" if "room_name" in event_cols else "'' AS room_name"
        placeholders = ",".join("?" * len(non_kpi_ids))
        start_sql = first.isoformat() + " 00:00:00"
        end_sql = (last + timedelta(days=1)).isoformat() + " 00:00:00"
        try:
            rows = conn.execute(
                f"SELECT msg_id, rule_id, rule_name, created_at, {content_col}, {exclude_col}, {room_name_col} "
                "FROM timeout_event WHERE room_id = ? AND created_at >= ? AND created_at < ? "
                f"AND rule_id IN ({placeholders})",
                (room_id, start_sql, end_sql, *sorted(non_kpi_ids)),
            )
        except sqlite3.Error:
            rows = []
        for row in rows:
            reason = (row["exclude_reason"] or "").strip()
            item = _item(
                msg_id=row["msg_id"],
                rule_id=row["rule_id"],
                rule_name=row["rule_name"],
                created_at=row["created_at"],
                content=row["content"],
                reason=reason,
                minutes=_minutes(row["rule_id"], maps),
            )
            if not reason or reason == DUP_REASON:
                # 重复触发表示消息已计入，只是同一消息又命中另一条规则，不是剔除。
                item["reason"] = ""
                display_only.append(item)
            else:
                kpi_excluded.append(item)

    return {
        "kpi_counted": _sort_cap(kpi_counted),
        "kpi_excluded": _sort_cap(kpi_excluded),
        "display_only": _sort_cap(display_only),
    }


def _message_text(msgtype: Any, content: Any) -> str:
    kind = str(msgtype or "").strip()
    if kind == "text":
        return ("" if content is None else str(content))[:500]
    return _NON_TEXT.get(kind, "[消息]")


def _message_role(from_id: str, employee_ids: Iterable[str], robot_ids: Iterable[str]) -> str:
    if from_id in robot_ids:
        return "robot"
    if from_id.startswith(("wm", "wo")):
        return "customer"
    if from_id in employee_ids:
        return "staff"
    return "staff"


def local_context(
    conn: sqlite3.Connection,
    room_id: str,
    msg_id: str,
    at: str,
    minutes: int,
    employee_ids: Iterable[str],
    robot_ids: Iterable[str],
) -> dict[str, Any]:
    """本地 chat_msg 中触发时间前 20 分钟到截止后 10 分钟的消息。"""
    result: dict[str, Any] = {"messages": [], "source": "local"}
    try:
        moment = datetime.strptime(str(at)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
    except (TypeError, ValueError):
        return result
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = 30
    start = int((moment - timedelta(minutes=20)).timestamp())
    end = int((moment + timedelta(minutes=minutes + 10)).timestamp())
    employees = {str(item) for item in (employee_ids or [])}
    robots = {str(item) for item in (robot_ids or [])}
    try:
        rows = conn.execute(
            "SELECT msg_id, from_id, msgtime, msgtype, content FROM chat_msg "
            "WHERE room_id = ? AND msgtime >= ? AND msgtime <= ? "
            "ORDER BY msgtime, msg_id LIMIT 200",
            (room_id, start, end),
        ).fetchall()
    except sqlite3.Error:
        return result

    messages = []
    for row in rows:
        sender = str(row["from_id"] or "")
        role = _message_role(sender, employees, robots)
        try:
            when = datetime.fromtimestamp(int(row["msgtime"]), SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            when = ""
        messages.append(
            {
                "time": when,
                "role": role,
                "role_label": _ROLE_LABEL[role],
                "text": _message_text(row["msgtype"], row["content"]),
                "trigger": str(row["msg_id"] or "") == str(msg_id or "") and bool(msg_id),
            }
        )
    result["messages"] = messages
    return result
