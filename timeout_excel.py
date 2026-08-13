"""导入管家「回复超时记录」Excel，按文件名时间范围覆盖入库并登记。"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import warnings
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from timeout_store import (
    clear_non_excel_events,
    connect,
    get_import,
    list_imports,
    replace_range_events,
    set_meta,
    upsert_import,
    upsert_rules,
)

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
INPUT_DIRNAME = "input_file"

_RANGE_PATTERNS = [
    re.compile(r"导出时间范围(\d{8})-(\d{8})"),
    re.compile(r"导出时间范围(\d{4}[-.]\d{2}[-.]\d{2})-(\d{4}[-.]\d{2}[-.]\d{2})"),
    re.compile(r"(\d{8})-(\d{8})"),
    re.compile(r"(\d{4}-\d{2}-\d{2})-(\d{4}-\d{2}-\d{2})"),
]
_MINUTES_RE = re.compile(r"(\d+)\s*分钟")


class ExcelImportError(ValueError):
    pass


def parse_filename_range(filename: str) -> tuple[date, date]:
    name = Path(filename).name
    for pattern in _RANGE_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        start = _parse_ymd(match.group(1))
        end = _parse_ymd(match.group(2))
        if end < start:
            raise ExcelImportError(f"文件名时间范围起止颠倒：{name}")
        return start, end
    raise ExcelImportError(
        "文件名需包含导出时间范围，例如 导出时间范围20260701-20260731回复超时记录.xlsx"
    )


def _parse_ymd(text: str) -> date:
    raw = text.strip().replace(".", "")
    if re.fullmatch(r"\d{8}", raw):
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    raw = text.strip().replace(".", "-")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ExcelImportError(f"无法解析日期：{text}") from exc


def _file_sig(path: str) -> str:
    st = os.stat(path)
    return f"{st.st_mtime_ns}:{st.st_size}"


def _norm_col(name: Any) -> str:
    return str(name or "").strip().replace(" ", "")


def _cell(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return str(row[key]).strip()
    return ""


def _parse_trigger_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=SHANGHAI)
        return dt
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt


def _msg_id(created_at: str, room_name: str, content: str) -> str:
    raw = f"{created_at}|{room_name}|{content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _load_group_index(conn) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    rows = list(
        conn.execute(
            "SELECT room_id, store, theater, group_name, in_robot FROM chat_store"
        )
    )
    rows.sort(key=lambda r: (0 if r["in_robot"] else 1, 0 if r["store"] else 1))
    for row in rows:
        name = (row["group_name"] or "").strip()
        if not name or name in index:
            continue
        index[name] = {
            "room_id": row["room_id"] or "",
            "store": row["store"] or "",
            "theater": row["theater"] or "",
        }
    return index


def _rule_maps(conn) -> tuple[dict[str, str], dict[str, int]]:
    id_by_name: dict[str, str] = {}
    minutes_by_id: dict[str, int] = {}
    for row in conn.execute("SELECT rule_id, rule_name, chat_time_out FROM timeout_rule"):
        name = (row["rule_name"] or "").strip()
        rid = str(row["rule_id"] or "").strip()
        if name:
            id_by_name[name] = rid or name
        if rid:
            minutes_by_id[rid] = int(row["chat_time_out"] or 0)
    return id_by_name, minutes_by_id


def _read_excel_rows(path: str) -> list[dict[str, Any]]:
    import pandas as pd

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Workbook contains no default style")
        frame = pd.read_excel(path)
    frame.columns = [_norm_col(c) for c in frame.columns]
    required = {"触发时间", "所在会话", "触发超时规则"}
    missing = required - set(frame.columns)
    if missing:
        raise ExcelImportError("Excel 缺少列：" + "、".join(sorted(missing)))
    return frame.to_dict(orient="records")


def import_excel_file(root: str, path: str, *, force: bool = False) -> dict[str, Any]:
    filename = os.path.basename(path)
    start, end = parse_filename_range(filename)
    if not os.path.isfile(path):
        raise ExcelImportError(f"文件不存在：{filename}")
    sig = _file_sig(path)

    db_path = os.path.join(root, "timeout.db")
    conn = connect(db_path)
    try:
        existing = get_import(conn, filename)
        if existing and existing.get("file_sig") == sig and not force:
            log.info("跳过未变化的导出 %s", filename)
            return {**existing, "skipped": True}

        raw_rows = _read_excel_rows(path)
        group_index = _load_group_index(conn)
        id_by_name, _minutes = _rule_maps(conn)
        now_text = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")

        events: list[dict[str, Any]] = []
        unmatched = 0
        seen: set[tuple[str, str]] = set()
        rule_seen: dict[str, dict[str, Any]] = {}

        for row in raw_rows:
            chat_type = _cell(row, "会话类型")
            if chat_type and chat_type != "外部群聊":
                continue
            dt = _parse_trigger_time(row.get("触发时间"))
            if dt is None:
                continue
            day = dt.date()
            if day < start or day > end:
                continue
            room_name = _cell(row, "所在会话") or "未知群聊"
            rule_name = _cell(row, "触发超时规则")
            if not rule_name:
                continue
            created_at = dt.strftime("%Y-%m-%d %H:%M:%S")
            content = _cell(row, "超时消息")
            msg_id = _msg_id(created_at, room_name, content)
            rule_id = id_by_name.get(rule_name) or rule_name
            if (msg_id, rule_id) in seen:
                continue
            seen.add((msg_id, rule_id))

            info = group_index.get(room_name) or {}
            store = info.get("store") or ""
            theater = info.get("theater") or ""
            room_id = info.get("room_id") or f"name:{room_name}"
            if not store:
                unmatched += 1

            minutes_match = _MINUTES_RE.search(rule_name)
            minutes = int(minutes_match.group(1)) if minutes_match else 0
            rule_seen[rule_id] = {
                "rule_id": rule_id,
                "rule_name": rule_name,
                "chat_time_out": minutes,
            }
            events.append(
                {
                    "msg_id": msg_id,
                    "rule_id": rule_id,
                    "rule_name": rule_name,
                    "room_id": room_id,
                    "room_name": room_name,
                    "store": store,
                    "theater": theater,
                    "created_at": created_at,
                    "customer_id": "",
                    "source": "excel",
                }
            )

        clear_non_excel_events(conn)
        if rule_seen:
            upsert_rules(conn, rule_seen.values(), now_text)
        replace_range_events(conn, start, end, events)
        rec = {
            "filename": filename,
            "range_start": start.isoformat(),
            "range_end": end.isoformat(),
            "file_sig": sig,
            "rows_in_file": len(raw_rows),
            "events": len(events),
            "unmatched": unmatched,
            "imported_at": now_text,
        }
        upsert_import(conn, rec)
        set_meta(conn, "updated_at", now_text)
        conn.commit()
        log.info(
            "导入 %s 区间 %s~%s 行=%s 计入=%s 未归店=%s",
            filename,
            start,
            end,
            rec["rows_in_file"],
            rec["events"],
            rec["unmatched"],
        )
        return rec
    finally:
        conn.close()


def scan_input_dir(root: str, *, force: bool = False) -> dict[str, Any]:
    folder = os.path.join(root, INPUT_DIRNAME)
    os.makedirs(folder, exist_ok=True)
    imported: list[dict[str, Any]] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    names = sorted(
        n for n in os.listdir(folder) if n.lower().endswith((".xlsx", ".xls")) and not n.startswith("~$")
    )
    for name in names:
        path = os.path.join(folder, name)
        try:
            rec = import_excel_file(root, path, force=force)
            if rec.get("skipped"):
                skipped.append(name)
            else:
                imported.append(rec)
        except ExcelImportError as exc:
            errors.append({"filename": name, "error": str(exc)})
            log.warning("跳过 %s：%s", name, exc)
    db_path = os.path.join(root, "timeout.db")
    conn = connect(db_path)
    try:
        registry = list_imports(conn)
    finally:
        conn.close()
    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "imports": registry,
    }
