"""超时明细下钻：事件列表、群成员、超时前后聊天上下文。

成员与消息只读调用 SCRM OpenAPI（不发送任何消息），进程内缓存 10 分钟。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scrm_sync import make_scrm_client

SHANGHAI = ZoneInfo("Asia/Shanghai")
_CACHE: dict[tuple, tuple[float, Any]] = {}
_LOCK = threading.Lock()
_TTL = 600
_client = None

ROLE_LABEL = {"staff": "员工", "customer": "客户", "robot": "机器人"}
SCENE = {1: "成员直接邀请", 2: "邀请链接", 3: "扫群二维码"}


def _get_client(root: str):
    global _client
    if _client is None:
        _client = make_scrm_client(root)
    return _client


def _cached(key: tuple, fn):
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _TTL:
            return hit[1]
    value = fn()
    with _LOCK:
        if len(_CACHE) > 500:
            _CACHE.clear()
        _CACHE[key] = (now, value)
    return value


def _fmt_ts(value: Any) -> str:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    if ts > 1e12:
        ts /= 1000
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _robot_ids() -> tuple[set[str], str]:
    ids = {p.strip() for p in os.environ.get("SCRM_ROBOT_USERIDS", "").split(",") if p.strip()}
    return ids, os.environ.get("SCRM_ROBOT_USERNAME", "刘明轩").strip()


def _role(rec: dict) -> str:
    ids, robot_name = _robot_ids()
    uid = str(rec.get("userid") or "")
    name = str(rec.get("name") or "")
    if uid in ids or (robot_name and name == robot_name):
        return "robot"
    return "staff" if int(rec.get("type") or 0) == 1 else "customer"


def group_members(root: str, room_id: str) -> list[dict[str, Any]]:
    def load():
        client = _get_client(root)
        out = []
        page = 1
        while page <= 20:
            payload = client._request(
                "/openapi/chat/group/member/list",
                params={"chat_id": room_id, "current_index": page, "page_size": 100},
            )
            data = payload.get("data") or {}
            recs = data.get("records") or []
            out.extend(recs)
            if len(recs) < 100 or len(out) >= int(data.get("total") or 0):
                break
            page += 1
        return out

    members = []
    for rec in _cached(("members", room_id), load):
        role = _role(rec)
        members.append({
            "userid": str(rec.get("userid") or ""),
            "name": rec.get("name") or rec.get("user_chat_nick_name") or "（未知）",
            "nick": rec.get("user_chat_nick_name") or "",
            "role": role,
            "role_label": ROLE_LABEL[role],
            "join_time": _fmt_ts(rec.get("join_time")),
            "join_scene": SCENE.get(int(rec.get("join_scene") or 0), ""),
        })
    order = {"robot": 0, "staff": 1, "customer": 2}
    members.sort(key=lambda m: (order[m["role"]], m["join_time"] or "9"))
    return members


def chat_context(root: str, room_id: str, at: str, rule_minutes: int = 30, msg_id: str = "") -> dict[str, Any]:
    """返回超时时间点附近的群消息。高亮该时间点之前最后一条客户消息（未被回复的那条）。"""
    t = datetime.strptime(at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
    if msg_id:  # at = 客户发言时间：看前 20 分钟到截止后 10 分钟
        start = int((t - timedelta(minutes=20)).timestamp())
        end = int((t + timedelta(minutes=rule_minutes + 10)).timestamp())
    else:
        start = int((t - timedelta(minutes=max(rule_minutes, 10) + 30)).timestamp())
        end = int((t + timedelta(minutes=30)).timestamp())

    def load():
        payload = _get_client(root)._request(
            "/openapi/chat/message/list",
            params={"type": 1, "target_id": room_id, "msg_time_start": start,
                    "msg_time_end": end, "page_size": 100},
        )
        return (payload.get("data") or {}).get("records") or []

    recs = [r for r in _cached(("ctx", room_id, start, end), load) if not r.get("room_id") or r.get("room_id") == room_id]
    recs.sort(key=lambda r: (r.get("msgtime") or 0, r.get("seq") or 0))
    try:
        names = {m["userid"]: m for m in group_members(root, room_id)}
    except Exception:
        names = {}
    t_ms = t.timestamp() * 1000
    hl = None
    for idx, r in enumerate(recs):
        sender = str(r.get("from") or "")
        member = names.get(sender)
        is_customer = (member["role"] == "customer") if member else sender.startswith(("wm", "wo"))
        if is_customer and (r.get("msgtime") or 0) <= t_ms:
            hl = idx
    if msg_id:
        exact = next((i for i, r in enumerate(recs) if str(r.get("msgid") or "") == msg_id), None)
        if exact is not None:
            hl = exact
    msgs = []
    for idx, r in enumerate(recs):
        sender = str(r.get("from") or "")
        member = names.get(sender) or {}
        mtype = r.get("msgtype") or ""
        text = (r.get("content") or "").strip() if mtype == "text" else ""
        if not text:
            text = {"image": "[图片]", "voice": "[语音]", "video": "[视频]", "file": "[文件]",
                    "emotion": "[表情]", "link": "[链接]", "weapp": "[小程序]", "revoke": "[撤回]"}.get(mtype, "[" + (mtype or "消息") + "]")
        msgs.append({
            "time": _fmt_ts(r.get("msgtime")),
            "sender": member.get("name") or ("客户" if sender.startswith(("wm", "wo")) else sender[:6] or "未知"),
            "role": member.get("role") or ("customer" if sender.startswith(("wm", "wo")) else "staff"),
            "text": text[:500],
            "trigger": idx == hl,
        })
    return {"messages": msgs, "window": [_fmt_ts(start), _fmt_ts(end)], "exact": bool(msg_id) and hl is not None and str(recs[hl].get("msgid") or "") == msg_id}


def query_events(conn: sqlite3.Connection, *, theater: str = "", q: str = "", store: str = "",
                 rule_id: str = "", start: str = "", end: str = "", room_id: str = "",
                 limit: int = 200, offset: int = 0, status: str = "valid") -> dict[str, Any]:
    clauses, params = ["1=1"], []
    if status == "valid":
        clauses.append("ifnull(e.exclude_reason, '') = ''")
    elif status == "excluded":
        clauses.append("ifnull(e.exclude_reason, '') NOT IN ('', '重复触发(同一消息)')")
    if theater:
        clauses.append("e.theater = ?"); params.append(theater)
    if store:
        clauses.append("TRIM(e.store) = ?"); params.append(store.strip())
    elif q:
        clauses.append("instr(lower(e.store), lower(?)) > 0"); params.append(q)
    if rule_id:
        clauses.append("e.rule_id = ?"); params.append(rule_id)
    if room_id:
        clauses.append("e.room_id = ?"); params.append(room_id)
    if start:
        clauses.append("e.created_at >= ?"); params.append(start[:10] + " 00:00:00")
    if end:
        end_d = datetime.strptime(end[:10], "%Y-%m-%d") + timedelta(days=1)
        clauses.append("e.created_at < ?"); params.append(end_d.strftime("%Y-%m-%d 00:00:00"))
    where = " AND ".join(clauses)
    total = conn.execute(f"SELECT COUNT(*) FROM timeout_event e WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        "SELECT e.msg_id, e.rule_id, e.rule_name, e.room_id, e.store, e.theater, e.created_at, "
        "ifnull(e.content, '') AS content, ifnull(e.exclude_reason, '') AS exclude_reason, "
        "COALESCE(NULLIF(e.room_name,''), NULLIF(c.group_name,''), e.room_id) AS group_name, "
        "COALESCE(r.chat_time_out, 0) AS minutes "
        "FROM timeout_event e LEFT JOIN chat_store c ON c.room_id = e.room_id "
        "LEFT JOIN timeout_rule r ON r.rule_id = e.rule_id "
        f"WHERE {where} ORDER BY e.created_at DESC, e.msg_id LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return {"total": total, "events": [dict(r) for r in rows]}
