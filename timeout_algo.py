"""超时算法：会话存档缓存 + 本地计算规则（带生效区间）+ 规则过滤 + DeepSeek 复核。

管家 Excel 导入已停用。超时 = 规则窗口内客户在机器人群发言，截止时刻前同群没有企业成员回复。
规则只存在本地（calc_rule），不写回 SCRM，所以改规则不会影响管家真实的超时提醒。
同一规则的多个版本用 rule_key 关联，每个版本有 effective_from / effective_to，
回算历史某天时只用当天有效的版本。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from timeout_detect import TimeoutRule, detect_timeout_events, parse_msgtime
from timeout_store import (
    connect,
    get_meta,
    load_chat_map,
    load_employees,
    load_robot_rooms,
    set_meta,
)

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
LOOKAHEAD_MINUTES = 0  # 规则窗口不跨午夜（截止也须在窗口内），当天消息足够
REFETCH_DAYS = 2
NON_TEXT = {"image": "[图片]", "voice": "[语音]", "video": "[视频]", "file": "[文件]",
            "emotion": "[表情]", "link": "[链接]", "weapp": "[小程序]", "location": "[位置]",
            "card": "[名片]", "mixed": "[图文]", "chatrecord": "[聊天记录]"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_msg (
  msg_id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL,
  from_id TEXT,
  msgtime INTEGER NOT NULL,
  msgtype TEXT,
  content TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_msg_room_time ON chat_msg(room_id, msgtime);
CREATE INDEX IF NOT EXISTS idx_chat_msg_time ON chat_msg(msgtime);
CREATE TABLE IF NOT EXISTS chat_msg_day (
  day TEXT PRIMARY KEY,
  messages INTEGER,
  fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS calc_rule (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_key TEXT NOT NULL,
  name TEXT NOT NULL,
  minutes INTEGER NOT NULL,
  days TEXT NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  source TEXT,
  note TEXT,
  updated_at TEXT,
  updated_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_calc_rule_key ON calc_rule(rule_key, effective_from);
"""

# 首次启用时按 SCRM 现有两条规则播种；生效日取 SCRM 规则的创建时间。
SEED_RULES = [
    {"rule_key": "2055182790919555459", "name": "30 分钟未回复预警", "minutes": 30,
     "days": [1, 2, 3, 4, 5], "start": "09:00", "end": "20:00", "created": 1778828735},
    {"rule_key": "2035988283787413634", "name": "10 分钟未回复预警", "minutes": 10,
     "days": [1, 2, 3, 4, 5, 6, 7], "start": "09:00", "end": "20:00", "created": 1774252408},
]


def _now() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def open_db(root: str):
    conn = connect(os.path.join(root, "timeout.db"))
    conn.executescript(SCHEMA)
    _seed(conn)
    return conn


def _seed(conn) -> None:
    if conn.execute("SELECT COUNT(*) FROM calc_rule").fetchone()[0]:
        return
    for r in SEED_RULES:
        eff = datetime.fromtimestamp(r["created"], SHANGHAI).date().isoformat()
        conn.execute(
            "INSERT INTO calc_rule(rule_key,name,minutes,days,start_time,end_time,enabled,effective_from,"
            "source,note,updated_at,updated_by) VALUES (?,?,?,?,?,?,1,?,?,?,?,?)",
            (r["rule_key"], r["name"], r["minutes"], json.dumps(r["days"]), r["start"], r["end"], eff,
             "scrm-seed", "按 SCRM 规则创建日播种", _now(), "system"),
        )
    _mirror_rule_table(conn)
    conn.commit()


# ---------------------------------------------------------------- rules

def _row(r) -> dict[str, Any]:
    d = dict(r)
    d["days"] = json.loads(d["days"] or "[]")
    d["enabled"] = bool(d["enabled"])
    return d


def list_rules(conn) -> list[dict[str, Any]]:
    """按 rule_key 分组：current 为最新版本，history 为全部版本（新→旧）。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM calc_rule ORDER BY rule_key, effective_from DESC, id DESC"):
        groups.setdefault(r["rule_key"], []).append(_row(r))
    out = [{"rule_key": k, "current": v[0], "history": v} for k, v in groups.items()]
    out.sort(key=lambda g: (not g["current"]["enabled"], g["current"]["minutes"], g["rule_key"]))
    return out


def _validate(data: dict[str, Any]) -> dict[str, Any]:
    name = str(data.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("请填写规则名称")
    try:
        minutes = int(data.get("minutes"))
    except (TypeError, ValueError):
        raise ValueError("超时分钟需为整数")
    if not 1 <= minutes <= 240:
        raise ValueError("超时分钟需在 1~240 之间")
    days = sorted({int(d) for d in (data.get("days") or []) if str(d).isdigit() and 1 <= int(d) <= 7})
    if not days:
        raise ValueError("至少选择一天")
    clocks = []
    for key in ("start", "end"):
        text = str(data.get(key) or "").strip()[:5]
        try:
            clocks.append(datetime.strptime(text, "%H:%M").strftime("%H:%M"))
        except ValueError:
            raise ValueError("时间格式应为 HH:MM")
    if clocks[0] >= clocks[1]:
        raise ValueError("开始时间需早于结束时间（不支持跨午夜）")
    try:
        eff = date.fromisoformat(str(data.get("effective_from") or "")[:10])
    except ValueError:
        raise ValueError("请填写生效日期")
    return {"name": name, "minutes": minutes, "days": days, "start": clocks[0], "end": clocks[1],
            "enabled": bool(data.get("enabled", True)), "effective_from": eff,
            "note": str(data.get("note") or "").strip()[:200]}


def save_rule(conn, data: dict[str, Any], *, rule_key: str | None, actor: str) -> tuple[str, date]:
    """新建或修改规则，返回 (rule_key, 需要重算的起始日)。

    修改时：生效日早于等于当前版本起始日 → 原地覆盖该版本；否则把当前版本截到前一天，
    从生效日起新增一个版本。这样历史日期仍按当时的规则计算。
    """
    v = _validate(data)
    eff = v["effective_from"]
    now = _now()
    if rule_key is None:
        n = conn.execute("SELECT COUNT(DISTINCT rule_key) FROM calc_rule").fetchone()[0]
        rule_key = f"local-{n + 1}-{int(datetime.now().timestamp())}"
        recompute_from = eff
    else:
        versions = list(conn.execute(
            "SELECT * FROM calc_rule WHERE rule_key=? ORDER BY effective_from DESC, id DESC", (rule_key,)))
        if not versions:
            raise KeyError(rule_key)
        cur = versions[0]
        cur_from = date.fromisoformat(cur["effective_from"])
        if eff <= cur_from:
            # 覆盖当前版本，并删掉生效日之后才开始、已被覆盖的更早版本不受影响
            conn.execute("DELETE FROM calc_rule WHERE rule_key=? AND effective_from>=? AND id!=?",
                         (rule_key, eff.isoformat(), cur["id"]))
            conn.execute("DELETE FROM calc_rule WHERE id=?", (cur["id"],))
            prev = conn.execute(
                "SELECT id FROM calc_rule WHERE rule_key=? AND effective_from<? ORDER BY effective_from DESC LIMIT 1",
                (rule_key, eff.isoformat())).fetchone()
            if prev:
                conn.execute("UPDATE calc_rule SET effective_to=? WHERE id=?",
                             ((eff - timedelta(days=1)).isoformat(), prev["id"]))
            recompute_from = min(eff, cur_from)
        else:
            conn.execute("UPDATE calc_rule SET effective_to=? WHERE id=?",
                         ((eff - timedelta(days=1)).isoformat(), cur["id"]))
            recompute_from = eff
    conn.execute(
        "INSERT INTO calc_rule(rule_key,name,minutes,days,start_time,end_time,enabled,effective_from,"
        "source,note,updated_at,updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (rule_key, v["name"], v["minutes"], json.dumps(v["days"]), v["start"], v["end"], int(v["enabled"]),
         eff.isoformat(), "admin", v["note"], now, actor[:60]),
    )
    _mirror_rule_table(conn)
    conn.commit()
    return rule_key, recompute_from


def _mirror_rule_table(conn) -> None:
    """timeout_rule 供图表/筛选取规则名：同步为每条规则的最新版本。"""
    conn.execute("DELETE FROM timeout_rule")
    for g in list_rules(conn):
        c = g["current"]
        conn.execute(
            "INSERT INTO timeout_rule(rule_id, rule_name, chat_time_out, extra, updated_at) VALUES (?,?,?,?,?)",
            (g["rule_key"], c["name"], c["minutes"],
             json.dumps({"days": c["days"], "start": c["start_time"], "end": c["end_time"],
                         "enabled": c["enabled"]}, ensure_ascii=False), c["updated_at"]),
        )


def rules_for_day(conn, day: date) -> list[TimeoutRule]:
    d = day.isoformat()
    out = []
    for r in conn.execute(
        "SELECT * FROM calc_rule WHERE enabled=1 AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)",
        (d, d),
    ):
        out.append(TimeoutRule(
            rule_id=r["rule_key"], rule_name=r["name"], minutes=int(r["minutes"]),
            days=frozenset(json.loads(r["days"])),
            start=datetime.strptime(r["start_time"], "%H:%M").time(),
            end=datetime.strptime(r["end_time"], "%H:%M").time(), rule_type=2,
        ))
    return out


# ---------------------------------------------------------------- messages

def _day_bounds(day: date) -> tuple[int, int]:
    start = datetime(day.year, day.month, day.day, tzinfo=SHANGHAI)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def fetch_day(client, conn, day: date) -> int:
    s, e = _day_bounds(day)
    rows = []
    for rec in client.iter_messages(msg_time_start=s, msg_time_end=e):
        room = str(rec.get("room_id") or "").strip()
        msgid = str(rec.get("msgid") or "").strip()
        dt = parse_msgtime(rec.get("msgtime"))
        if not room or not msgid or dt is None:
            continue
        mtype = str(rec.get("msgtype") or "")
        text = str(rec.get("content") or "").strip() if mtype == "text" else NON_TEXT.get(mtype, f"[{mtype or '消息'}]")
        rows.append((msgid, room, str(rec.get("from") or "").strip(), int(dt.timestamp()), mtype, text[:2000]))
    conn.execute("DELETE FROM chat_msg WHERE msgtime>=? AND msgtime<?", (s, e))
    conn.executemany("INSERT OR REPLACE INTO chat_msg VALUES (?,?,?,?,?,?)", rows)
    conn.execute(
        "INSERT INTO chat_msg_day(day,messages,fetched_at) VALUES (?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET messages=excluded.messages, fetched_at=excluded.fetched_at",
        (day.isoformat(), len(rows), _now()),
    )
    conn.commit()
    return len(rows)


def _messages(conn, day: date) -> list[dict[str, Any]]:
    s, _ = _day_bounds(day)
    e = s + 86400 + LOOKAHEAD_MINUTES * 60
    return [
        {"msgid": r["msg_id"], "room_id": r["room_id"], "from": r["from_id"], "msgtime": r["msgtime"],
         "msgtype": r["msgtype"], "content": r["content"]}
        for r in conn.execute("SELECT * FROM chat_msg WHERE msgtime>=? AND msgtime<? ORDER BY msgtime", (s, e))
    ]


# ---------------------------------------------------------------- compute

def compute_day(conn, day: date, robot_rooms, employees, chat_map) -> int:
    rules = rules_for_day(conn, day)
    msgs = _messages(conn, day)
    detected = detect_timeout_events(msgs, rules, employees, robot_rooms) if rules else []
    by_id = {m["msgid"]: m for m in msgs}
    key = day.isoformat()
    rows = []
    for ev in detected:
        if not ev["created_at"].startswith(key):
            continue
        info = chat_map.get(ev["room_id"]) or {}
        m = by_id.get(ev["msg_id"]) or {}
        rows.append((ev["msg_id"], ev["rule_id"], ev["rule_name"], ev["room_id"], info.get("store") or "",
                     info.get("theater") or "", ev["created_at"], ev["customer_id"], "api",
                     info.get("group_name") or "", m.get("content") or "", m.get("msgtype") or ""))
    conn.execute("DELETE FROM timeout_event WHERE created_at>=? AND created_at<?",
                 (key + " 00:00:00", (day + timedelta(days=1)).isoformat() + " 00:00:00"))
    conn.executemany(
        "INSERT OR REPLACE INTO timeout_event(msg_id,rule_id,rule_name,room_id,store,theater,created_at,"
        "customer_id,source,room_name,content,msgtype,exclude_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'')",
        rows,
    )
    return len(rows)


def apply_filters(root: str, start: date, end: date, *, llm: bool = True) -> dict[str, int]:
    """规则过滤 + DeepSeek 复核（同评分页的流水线），结果写回 timeout_event.exclude_reason。"""
    from score_llm import execute_review, llm_configured
    from score_query import prepare_events

    if llm and llm_configured(root):
        try:
            execute_review(root, start, end)
        except Exception:
            log.warning("DeepSeek 复核失败，未复核的消息仍计入", exc_info=True)
    events, _u, _m, _meta = prepare_events(root, start, end)
    # 同一消息命中多条规则：第二条记「重复触发」。若首条被剔除，重复的那条跟着用首条原因，
    # 这样「重复触发」只表示「消息已计入、只是规则多算一次」。
    primary = {ev["msg_id"]: ev.get("reason") or "" for ev in events
               if (ev.get("reason") or "") != "重复触发(同一消息)"}
    for ev in events:
        if ev.get("reason") != "重复触发(同一消息)":
            continue
        if ev["msg_id"] not in primary:
            ev["reason"] = "重复消息(同群同秒同内容)"  # 另一条 msg_id 的重复发送
        elif primary[ev["msg_id"]]:
            ev["reason"] = primary[ev["msg_id"]]
    conn = open_db(root)
    try:
        conn.executemany(
            "UPDATE timeout_event SET exclude_reason=? WHERE msg_id=? AND rule_id=?",
            [(ev.get("reason") or "", ev["msg_id"], ev["rule_id"]) for ev in events],
        )
        conn.commit()
    finally:
        conn.close()
    stats = {"total": len(events), "valid": 0, "rule_excluded": 0, "llm_excluded": 0, "duplicate": 0}
    for ev in events:
        r = ev.get("reason") or ""
        if r == "重复触发(同一消息)":
            stats["duplicate"] = stats.get("duplicate", 0) + 1
            stats["total"] -= 1
        elif not r:
            stats["valid"] += 1
        elif r.startswith("LLM"):
            stats["llm_excluded"] += 1
        else:
            stats["rule_excluded"] += 1
    return stats


# ---------------------------------------------------------------- jobs

_job_lock = threading.Lock()
_job: dict[str, Any] = {"status": "idle"}


def job_status() -> dict[str, Any]:
    with _job_lock:
        return dict(_job)


def _set_job(**kw) -> None:
    with _job_lock:
        _job.update(kw)


def first_day(conn) -> date:
    floor = date.fromisoformat((os.environ.get("TIMEOUT_SINCE") or "2026-05-01").strip()[:10])
    row = conn.execute("SELECT MIN(effective_from) FROM calc_rule").fetchone()
    eff = date.fromisoformat(row[0]) if row and row[0] else floor
    return max(floor, eff)


def run(root: str, *, start: date | None = None, end: date | None = None, fetch: bool = True,
        refetch_all: bool = False, llm: bool = True, label: str = "sync",
        progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """拉取缺失天的会话存档（最近 REFETCH_DAYS 天总是重拉），按天重算超时，再过滤复核。"""
    from scrm_sync import make_scrm_client

    _set_job(status="running", label=label, phase="准备", done=0, total=0, started_at=_now(), error="")
    conn = open_db(root)
    try:
        today = datetime.now(SHANGHAI).date()
        start = start or first_day(conn)
        end = min(end or today, today)
        robot_rooms = load_robot_rooms(conn)
        employees = load_employees(conn)
        if not robot_rooms or not employees:
            raise RuntimeError("缺少群/员工上下文，请先完成拉群同步")
        chat_map = load_chat_map(conn)
        have = {r[0] for r in conn.execute("SELECT day FROM chat_msg_day")}
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        client = make_scrm_client(root) if fetch else None
        fetched = 0
        total = len(days)
        counts = 0
        for i, day in enumerate(days):
            _set_job(phase=f"计算 {day.isoformat()}", done=i, total=total)
            if client and (refetch_all or day.isoformat() not in have or (today - day).days < REFETCH_DAYS):
                fetch_day(client, conn, day)
                fetched += 1
            counts += compute_day(conn, day, robot_rooms, employees, chat_map)
            conn.commit()
        set_meta(conn, "updated_at", _now())
        set_meta(conn, "last_complete_date", end.isoformat())
        conn.commit()
    except Exception as exc:
        _set_job(status="error", error=str(exc)[:200])
        raise
    finally:
        conn.close()
    _set_job(phase="规则过滤与 DeepSeek 复核", done=total, total=total)
    stats = apply_filters(root, start, end, llm=llm)
    result = {"start": start.isoformat(), "end": end.isoformat(), "days": total, "fetched_days": fetched,
              "events": counts, **stats}
    conn = open_db(root)
    try:
        set_meta(conn, "last_run", json.dumps({**result, "label": label, "finished_at": _now()}, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    _set_job(status="done", phase="完成", finished_at=_now(), result=result)
    return result


def start_background(root: str, **kw) -> bool:
    with _job_lock:
        if _job.get("status") == "running":
            return False
        _job.update(status="running", phase="排队", done=0, total=0)

    def work():
        try:
            run(root, **kw)
        except Exception as exc:
            log.exception("超时重算失败")
            _set_job(status="error", error=str(exc)[:200])

    threading.Thread(target=work, name="timeout-recompute", daemon=True).start()
    return True
