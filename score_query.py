"""从 timeout.db 和已导入的 Excel 重算门店超时评分。

消息正文只在计算时从 input_file 对照，不写入仓库。Excel 缺失时仍可按群和时间做
去重与 30 分钟合并，话术类规则会跳过没有正文的事件。
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from score_llm import apply_verdicts, cache_get, llm_configured, review_status
from score_math import MERGE_MAP, StoreInput, score_stores
from score_rules import classify_events
from timeout_excel import _cell, _msg_id, _parse_trigger_time
from timeout_store import connect

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def last_quarter(today: date) -> tuple[date, date]:
    quarter = (today.month - 1) // 3
    if quarter == 0:
        year = today.year - 1
        start_month = 10
    else:
        year = today.year
        start_month = (quarter - 1) * 3 + 1
    start = date(year, start_month, 1)
    end_month = start_month + 2
    if end_month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, end_month + 1, 1) - timedelta(days=1)
    return start, end


def parse_range(
    start: str | None,
    end: str | None,
    today: date | None = None,
) -> tuple[date, date]:
    today = today or datetime.now(SHANGHAI).date()
    default_start, default_end = last_quarter(today)

    def one(value: str | None, fallback: date) -> date:
        if not value:
            return fallback
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return fallback

    start_d = one(start, default_start)
    end_d = one(end, default_end)
    if end_d < start_d:
        start_d, end_d = end_d, start_d
    return start_d, end_d


def build_report(root: str, start: date, end: date, *, use_llm: bool | None = None) -> dict[str, Any]:
    events, unmatched, missing_message, llm_meta = prepare_events(root, start, end, use_llm=use_llm)
    stores = _score_from_events(root, events)
    mu = stores[0]["mu"] if stores else 0.0
    ranked = sum(1 for row in stores if row["kind"] in ("", "merge"))
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "m": 20,
        "mu": mu,
        "mu_per_100": mu * 100,
        "ranked": ranked,
        "unmatched": unmatched,
        "missing_message": missing_message,
        "stores": _sort_rows(stores),
        **llm_meta,
    }


def load_rule_events(root: str, start: date, end: date) -> tuple[list[dict[str, Any]], int, int]:
    events, unmatched, missing, _meta = prepare_events(root, start, end, use_llm=False)
    return events, unmatched, missing


def prepare_events(
    root: str,
    start: date,
    end: date,
    *,
    use_llm: bool | None = None,
) -> tuple[list[dict[str, Any]], int, int, dict[str, Any]]:
    events, unmatched, missing_message = _classified_events(root, start, end)
    pending = _pending_messages(events)
    verdicts = cache_get(root, pending)
    available = llm_configured(root)
    if use_llm is None:
        use_llm = available
    if use_llm:
        apply_verdicts(events, verdicts)
    meta = {
        "llm_on": bool(use_llm),
        "llm_available": available,
        "llm_cached": sum(1 for text in pending if text in verdicts),
        "llm_pending": len(pending),
        "llm_job": review_status(root, start, end),
    }
    return events, unmatched, missing_message, meta


def store_detail(
    root: str,
    store: str,
    start: date,
    end: date,
    *,
    use_llm: bool | None = None,
) -> dict[str, Any]:
    report = build_report(root, start, end, use_llm=use_llm)
    row = next((item for item in report["stores"] if item["name"] == store), None)
    events, _, _, _ = prepare_events(root, start, end, use_llm=report["llm_on"])
    names = {store}
    shared_from: set[str] = set()
    target = MERGE_MAP.get(store)
    if row and row.get("kind") == "merge" and target:
        names.add(target)
        shared_from.add(target)
    counted: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for ev in events:
        if (ev.get("store") or "") not in names:
            continue
        item = {
            "created_at": ev.get("created_at") or "",
            "room_name": ev.get("room") or "",
            "rule_name": ev.get("rule_name") or "",
            "reason": ev.get("reason") or "",
            "message": ev.get("msg") or "",
            "shared": (ev.get("store") or "") in shared_from,
        }
        if item["reason"]:
            excluded.append(item)
        else:
            counted.append(item)
    return {
        "store": row,
        "name": store,
        "counted": counted,
        "excluded": excluded,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "missing_message": report["missing_message"],
        "llm_on": report["llm_on"],
        "llm_available": report["llm_available"],
        "llm_cached": report["llm_cached"],
        "llm_pending": report["llm_pending"],
    }


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(row: dict[str, Any]):
        if row.get("rank") is None:
            return (1, 0, 0.0, row.get("name") or "")
        return (0, int(row["rank"]), float(row.get("rate") or 0), row.get("name") or "")

    return sorted(rows, key=key)


def _score_from_events(root: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, int]] = {}
    for ev in events:
        name = (ev.get("store") or "").strip()
        if not name:
            continue
        bucket = counts.setdefault(name, {"raw": 0, "valid": 0, "llm_excluded": 0})
        bucket["raw"] += 1
        reason = ev.get("reason") or ""
        if reason.startswith("LLM复核剔除"):
            bucket["llm_excluded"] += 1
        elif not reason:
            bucket["valid"] += 1

    directory = _store_directory(root)
    inputs: list[StoreInput] = []
    seen: set[str] = set()
    for name, info in directory.items():
        bucket = counts.get(name) or {"raw": 0, "valid": 0, "llm_excluded": 0}
        inputs.append(
            StoreInput(
                name=name,
                theater=info["theater"],
                group_count=info["group_count"],
                robot_group_count=info["robot_group_count"],
                raw=bucket["raw"],
                valid=bucket["valid"],
                in_scrm=True,
            )
        )
        seen.add(name)
    for name, bucket in counts.items():
        if name in seen:
            continue
        inputs.append(
            StoreInput(
                name=name,
                raw=bucket["raw"],
                valid=bucket["valid"],
                in_scrm=False,
            )
        )
        seen.add(name)
    for src, dst in MERGE_MAP.items():
        if src not in seen and dst in seen:
            inputs.append(StoreInput(name=src, in_scrm=False))
    scored = score_stores(inputs)
    for row in scored:
        excluded = int(counts.get(row["name"], {}).get("llm_excluded", 0))
        if row["kind"] == "merge":
            target = MERGE_MAP.get(row["name"])
            excluded += int(counts.get(target, {}).get("llm_excluded", 0))
        row["llm_excluded"] = excluded
    return scored


def _pending_messages(events: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    texts: list[str] = []
    for event in events:
        if event.get("reason"):
            continue
        msg = event.get("msg")
        if not isinstance(msg, str):
            continue
        text = msg.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        texts.append(text)
    return texts


def _store_directory(root: str) -> dict[str, dict[str, Any]]:
    conn = _open(root)
    if conn is None:
        return {}
    try:
        stats: dict[str, dict[str, Any]] = {}
        for row in conn.execute("SELECT store, theater, in_robot FROM chat_store"):
            name = (row["store"] or "").strip()
            if not name:
                continue
            item = stats.setdefault(
                name,
                {"group_count": 0, "robot_group_count": 0, "theaters": {}},
            )
            item["group_count"] += 1
            if row["in_robot"]:
                item["robot_group_count"] += 1
            theater = (row["theater"] or "").strip()
            if theater:
                item["theaters"][theater] = item["theaters"].get(theater, 0) + 1
        for item in stats.values():
            theaters = item.pop("theaters")
            item["theater"] = max(theaters, key=theaters.get) if theaters else ""
        return stats
    finally:
        conn.close()


def _classified_events(
    root: str, start: date, end: date
) -> tuple[list[dict[str, Any]], int, int]:
    conn = _open(root)
    if conn is None:
        return [], 0, 0
    try:
        end_excl = (end + timedelta(days=1)).isoformat() + " 00:00:00"
        start_sql = start.isoformat() + " 00:00:00"
        db_rows = conn.execute(
            """
            SELECT msg_id, rule_id, rule_name, room_id, room_name, store, theater, created_at, content
            FROM timeout_event
            WHERE created_at >= ? AND created_at < ?
            ORDER BY created_at, room_name, msg_id, rule_id
            """,
            (start_sql, end_excl),
        ).fetchall()
    finally:
        conn.close()

    messages: dict[str, str] = {}  # 正文来自会话存档（timeout_event.content）；Excel 导入已停用
    pending: list[dict[str, Any]] = []
    missing = 0
    unmatched = 0
    for row in db_rows:
        msg = row["content"] if row["content"] else messages.get(row["msg_id"])
        if msg is None:
            missing += 1
        if not (row["store"] or "").strip():
            unmatched += 1
        pending.append(
            {
                "msg_id": row["msg_id"],
                "rule_id": row["rule_id"],
                "rule_name": row["rule_name"] or "",
                "room": (row["room_name"] or row["room_id"] or ""),
                "store": (row["store"] or "").strip(),
                "theater": row["theater"] or "",
                "t": row["created_at"],
                "created_at": row["created_at"],
                "msg": msg,
            }
        )
    return classify_events(pending), unmatched, missing


def _message_index(root: str) -> dict[str, str]:
    folder = os.path.join(root, "input_file")
    if not os.path.isdir(folder):
        return {}
    index: dict[str, str] = {}
    try:
        import pandas as pd
    except ImportError:
        log.warning("未安装 pandas，无法从 Excel 对照超时消息")
        return {}
    for name in sorted(os.listdir(folder)):
        if name.startswith("~$") or not name.lower().endswith((".xlsx", ".xls")):
            continue
        path = os.path.join(folder, name)
        try:
            frame = pd.read_excel(path)
        except Exception:
            log.warning("读取超时 Excel 失败：%s", name, exc_info=True)
            continue
        frame.columns = [str(col or "").strip().replace(" ", "") for col in frame.columns]
        if "超时消息" not in frame.columns:
            continue
        for rec in frame.to_dict(orient="records"):
            chat_type = _cell(rec, "会话类型")
            if chat_type and chat_type != "外部群聊":
                continue
            moment = _parse_trigger_time(rec.get("触发时间"))
            if moment is None:
                continue
            room = _cell(rec, "所在会话") or "未知群聊"
            content = _cell(rec, "超时消息")
            created_at = moment.strftime("%Y-%m-%d %H:%M:%S")
            index[_msg_id(created_at, room, content)] = content.strip()
    return index


def _open(root: str):
    path = os.path.join(root, "timeout.db")
    if not os.path.isfile(path):
        return None
    return connect(path)
