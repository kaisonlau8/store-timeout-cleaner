"""从会话存档按天还原超时提醒事件。"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scrm_client import ScrmClient, ScrmError
from scrm_sync import make_scrm_client
from timeout_detect import TimeoutRule, detect_timeout_events, rule_from_detail
from timeout_store import (
    connect,
    event_count,
    get_meta,
    load_chat_map,
    load_employees,
    load_robot_rooms,
    replace_context,
    replace_day_events,
    set_meta,
    upsert_rules,
)

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
LOOKAHEAD_MINUTES = 35
DEFAULT_SINCE = "2026-05-01"


def _db_path(root: str) -> str:
    return os.path.join(root, "timeout.db")


def _since_date(conn) -> date:
    floor = date.fromisoformat(os.environ.get("TIMEOUT_SINCE", DEFAULT_SINCE).strip() or DEFAULT_SINCE)
    last = get_meta(conn, "last_complete_date")
    if not last:
        return floor
    try:
        last_d = date.fromisoformat(last)
    except ValueError:
        return floor
    return max(floor, last_d)


def fetch_active_rules(client: ScrmClient) -> list[TimeoutRule]:
    listed = client.list_timeout_rules()
    rules: list[TimeoutRule] = []
    for item in listed:
        rule_id = str(item.get("rule_id") or "").strip()
        if not rule_id:
            continue
        detail = client.get_timeout_rule_detail(rule_id)
        if not detail.get("rule_id"):
            detail = {**item, **detail, "rule_id": rule_id}
        parsed = rule_from_detail(detail)
        if parsed is None:
            log.warning("跳过无法解析的超时规则 %s", rule_id)
            continue
        if parsed.rule_type != 2:
            log.info("跳过非群聊客户发言规则 %s type=%s", parsed.rule_name, parsed.rule_type)
            continue
        rules.append(parsed)
        log.info(
            "超时规则 %s %s %s分钟 星期%s %s-%s",
            parsed.rule_id,
            parsed.rule_name,
            parsed.minutes,
            ",".join(str(d) for d in sorted(parsed.days)),
            parsed.start.strftime("%H:%M"),
            parsed.end.strftime("%H:%M"),
        )
    if not rules:
        raise ScrmError("未拿到可用的群聊超时回复规则")
    return rules


def _sync_one_day(
    client: ScrmClient,
    conn,
    day: date,
    rules: list[TimeoutRule],
    robot_rooms: list[str],
    employees: list[str],
    chat_map: dict[str, dict[str, str]],
) -> int:
    start = datetime(day.year, day.month, day.day, tzinfo=SHANGHAI)
    end = start + timedelta(days=1, minutes=LOOKAHEAD_MINUTES)
    messages = list(
        client.iter_messages(
            msg_time_start=int(start.timestamp()),
            msg_time_end=int(end.timestamp()),
        )
    )
    detected = detect_timeout_events(messages, rules, employees, robot_rooms)
    day_key = day.isoformat()
    kept: list[dict[str, Any]] = []
    for ev in detected:
        if not str(ev.get("created_at") or "").startswith(day_key):
            continue
        info = chat_map.get(ev["room_id"]) or {}
        ev["store"] = info.get("store") or ""
        ev["theater"] = info.get("theater") or ""
        kept.append(ev)
    replace_day_events(conn, day, kept)
    log.info("超时还原 %s 存档=%s 计入=%s", day_key, len(messages), len(kept))
    return len(kept)


def sync_timeout_events(
    root: str,
    *,
    robot_chat_ids: list[str] | None = None,
    employee_ids: list[str] | None = None,
    group_rows: list[dict[str, Any]] | None = None,
    employee_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    conn = connect(_db_path(root))
    try:
        if group_rows is not None:
            replace_context(
                conn,
                robot_chat_ids=robot_chat_ids or [],
                employee_ids=employee_ids or [],
                group_rows=group_rows,
                employee_names=employee_names or {},
            )
            conn.commit()

        robot_rooms = load_robot_rooms(conn)
        employees = load_employees(conn)
        if not robot_rooms or not employees:
            raise ScrmError("缺少群/员工上下文，请先完成拉群同步")

        client = make_scrm_client(root)
        rules = fetch_active_rules(client)
        now_text = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        upsert_rules(
            conn,
            [
                {
                    "rule_id": r.rule_id,
                    "rule_name": r.rule_name,
                    "chat_time_out": r.minutes,
                    "extra": json.dumps(
                        {
                            "days": sorted(r.days),
                            "start": r.start.strftime("%H:%M:%S"),
                            "end": r.end.strftime("%H:%M:%S"),
                            "rule_type": r.rule_type,
                        },
                        ensure_ascii=False,
                    ),
                }
                for r in rules
            ],
            now_text,
        )

        chat_map = load_chat_map(conn)
        today = datetime.now(SHANGHAI).date()
        start_day = _since_date(conn)
        days = 0
        inserted = 0
        day = start_day
        while day <= today:
            inserted += _sync_one_day(client, conn, day, rules, robot_rooms, employees, chat_map)
            set_meta(conn, "last_complete_date", day.isoformat())
            conn.commit()
            days += 1
            day += timedelta(days=1)

        set_meta(conn, "updated_at", now_text)
        conn.commit()
        return {
            "days": days,
            "day_events": inserted,
            "total_events": event_count(conn),
            "rules": [{"rule_id": r.rule_id, "rule_name": r.rule_name, "minutes": r.minutes} for r in rules],
            "updated_at": now_text,
        }
    finally:
        conn.close()
