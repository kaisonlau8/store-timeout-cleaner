"""按超时规则在会话存档上还原「会计入超时提醒」的客户消息。"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
SKIP_MSGTYPES = {"", "revoke", "system"}


@dataclass(frozen=True)
class TimeoutRule:
    rule_id: str
    rule_name: str
    minutes: int
    days: frozenset[int]
    start: time
    end: time
    rule_type: int


def parse_msgtime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts > 10_000_000_000:
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts, SHANGHAI)
    except (OverflowError, OSError, ValueError):
        return None


def parse_clock(value: Any, fallback: time) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, str) and ":" in value:
        parts = value.split(":")
        try:
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            second = int(float(parts[2])) if len(parts) > 2 else 0
            return time(hour, minute, second)
        except (TypeError, ValueError):
            return fallback
    if isinstance(value, (int, float)) and value:
        ts = int(value)
        if ts > 10_000_000_000:
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts, SHANGHAI).time()
        except (OverflowError, OSError, ValueError):
            return fallback
    return fallback


def parse_days(value: Any) -> frozenset[int]:
    if not value:
        return frozenset(range(1, 8))
    out: set[int] = set()
    for item in value:
        try:
            num = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= num <= 7:
            out.add(num)
    return frozenset(out) if out else frozenset(range(1, 8))


def rule_from_detail(detail: dict[str, Any]) -> TimeoutRule | None:
    rule_id = str(detail.get("rule_id") or "").strip()
    if not rule_id:
        return None
    try:
        minutes = int(detail.get("chat_time_out") or 0)
    except (TypeError, ValueError):
        return None
    if minutes < 1:
        return None
    try:
        rule_type = int(detail.get("rule_type") or 0)
    except (TypeError, ValueError):
        rule_type = 0
    return TimeoutRule(
        rule_id=rule_id,
        rule_name=str(detail.get("rule_name") or rule_id),
        minutes=minutes,
        days=parse_days(detail.get("receive_days")),
        start=parse_clock(detail.get("receive_start_time"), time(9, 0)),
        end=parse_clock(detail.get("receive_end_time"), time(20, 0)),
        rule_type=rule_type,
    )


def in_rule_window(dt: datetime, rule: TimeoutRule) -> bool:
    weekday = dt.weekday() + 1
    if weekday not in rule.days:
        return False
    clock = dt.time()
    start, end = rule.start, rule.end
    if start <= end:
        return start <= clock <= end
    return clock >= start or clock <= end


EXTERNAL_PREFIXES = ("wm", "wo")


def is_staff(from_id: str, employee_set: set[str]) -> bool:
    """企业成员：在通讯录里，或不是外部联系人 ID（wm/wo 开头）。离职/未同步到通讯录的员工也算。"""
    return from_id in employee_set or not from_id.startswith(EXTERNAL_PREFIXES)


def detect_timeout_events(
    messages: Iterable[dict[str, Any]],
    rules: Iterable[TimeoutRule],
    employee_ids: Iterable[str],
    robot_rooms: Iterable[str],
) -> list[dict[str, Any]]:
    """客户消息在规则窗口内发出，且截止时刻前同群无企业成员回复，则计入。"""
    employee_set = {str(x) for x in employee_ids if x}
    room_set = {str(x) for x in robot_rooms if x}
    active_rules = [r for r in rules if r.rule_type == 2]
    if not employee_set or not room_set or not active_rules:
        return []

    by_room: dict[str, list[tuple[datetime, str, str]]] = defaultdict(list)
    for rec in messages:
        room_id = str(rec.get("room_id") or "").strip()
        if not room_id or room_id not in room_set:
            continue
        msgtype = str(rec.get("msgtype") or "")
        if msgtype in SKIP_MSGTYPES:
            continue
        dt = parse_msgtime(rec.get("msgtime"))
        if dt is None:
            continue
        from_id = str(rec.get("from") or "").strip()
        if not from_id:
            continue
        msgid = str(rec.get("msgid") or rec.get("msg_id") or "").strip()
        if not msgid:
            continue
        by_room[room_id].append((dt, from_id, msgid))

    events: list[dict[str, Any]] = []
    for room_id, items in by_room.items():
        items.sort(key=lambda row: row[0])
        emp_times = [dt for dt, from_id, _ in items if is_staff(from_id, employee_set)]
        for dt, from_id, msgid in items:
            if is_staff(from_id, employee_set):
                continue
            for rule in active_rules:
                if not in_rule_window(dt, rule):
                    continue
                deadline = dt + timedelta(minutes=rule.minutes)
                if not in_rule_window(deadline, rule):
                    continue
                idx = bisect_right(emp_times, dt)
                if idx < len(emp_times) and emp_times[idx] < deadline:
                    continue
                events.append(
                    {
                        "msg_id": msgid,
                        "rule_id": rule.rule_id,
                        "rule_name": rule.rule_name,
                        "room_id": room_id,
                        "created_at": dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "customer_id": from_id,
                    }
                )
    return events
