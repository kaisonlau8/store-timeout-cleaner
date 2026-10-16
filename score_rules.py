"""超时事件的确定性剔除规则。

顺序与口径跟考评脚本一致：重复触发 → 员工/营销话术 → 寒暄/表情 → 仅 @ →
同会话 30 分钟连发。已有原因的不再覆盖。LLM 只作为钩子，规则留下的事件才打标。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

REASON_DUP = "重复触发(同一消息)"
REASON_STAFF = "疑似员工/营销话术"
REASON_CLOSE = "寒暄/结束语/纯表情"
REASON_AT = "仅@人无内容"
REASON_BURST = "同会话30分钟内连发合并"
REASON_LLM = "LLM复核剔除"

_STAFF_RE = re.compile(
    r"^(尊敬的客户您好|您好！我们正在|您好！这是您爱车|优惠活动|你好！我是猛士客服|"
    r"送.*(体检|保养)|\*?这份报价|.*(三者|交强险).*(万|险))"
)
_CLOSE_RE = re.compile(
    r"^(好的?|好了|ok|OK|嗯+|哦+|哦，?好的|谢谢.*|感谢.*|收到|可以了?|没事|没问题|"
    r"知道了?|行|明白|辛苦了?|好嘞|好滴|节日快乐|中秋快乐|国庆快乐|[\W_]*)"
    r"[。！!~～.，,]*$"
)
_AT_RE = re.compile(r"^@\S+$")
_BURST_SECONDS = 1800


def classify_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [dict(ev) for ev in events]
    for row in rows:
        row["reason"] = row.get("reason") or ""

    order = sorted(
        range(len(rows)),
        key=lambda i: (_room(rows[i]), _as_dt(rows[i].get("t")), i),
    )
    _mark_duplicates(rows, order)
    _mark_content(rows, order)
    _mark_bursts(rows, order)
    _mark_llm(rows)
    return rows


def _mark_duplicates(rows: list[dict[str, Any]], order: list[int]) -> None:
    seen: set[tuple[Any, ...]] = set()
    for i in order:
        row = rows[i]
        if row["reason"]:
            continue
        key = _dup_key(row)
        if key in seen:
            row["reason"] = REASON_DUP
        else:
            seen.add(key)


def _mark_content(rows: list[dict[str, Any]], order: list[int]) -> None:
    for i in order:
        row = rows[i]
        if row["reason"]:
            continue
        msg = row.get("msg")
        if msg is None:
            continue
        text = str(msg).strip()
        if _STAFF_RE.search(text):
            row["reason"] = REASON_STAFF
        elif "【" not in text and _CLOSE_RE.search(text):
            row["reason"] = REASON_CLOSE
        elif _AT_RE.match(text):
            row["reason"] = REASON_AT


def _mark_bursts(rows: list[dict[str, Any]], order: list[int]) -> None:
    last: dict[str, datetime] = {}
    for i in order:
        row = rows[i]
        if row["reason"]:
            continue
        room = _room(row)
        moment = _as_dt(row.get("t"))
        prev = last.get(room)
        if prev is not None and (moment - prev).total_seconds() <= _BURST_SECONDS:
            row["reason"] = REASON_BURST
        else:
            last[room] = moment


def _mark_llm(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if row["reason"]:
            continue
        if row.get("llm_exclude"):
            row["reason"] = str(row.get("llm_reason") or REASON_LLM)


def _room(row: dict[str, Any]) -> str:
    return str(row.get("room") or "")


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if not value:
        return datetime.min
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return datetime.min


def _dup_key(row: dict[str, Any]) -> tuple[Any, ...]:
    room = _room(row)
    moment = _as_dt(row.get("t"))
    msg = row.get("msg")
    if msg is None:
        return ("id", str(row.get("msg_id") or ""), room, moment)
    return ("msg", room, moment, str(msg).strip())
