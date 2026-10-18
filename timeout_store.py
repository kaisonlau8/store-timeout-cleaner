"""超时提醒事件的本地 sqlite。"""

from __future__ import annotations

import sqlite3
from datetime import date
from urllib.parse import quote
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS timeout_event (
  msg_id TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  rule_name TEXT,
  room_id TEXT,
  store TEXT,
  theater TEXT,
  created_at TEXT NOT NULL,
  customer_id TEXT,
  PRIMARY KEY (msg_id, rule_id)
);
CREATE INDEX IF NOT EXISTS idx_event_time ON timeout_event(created_at);
CREATE INDEX IF NOT EXISTS idx_event_store ON timeout_event(store);
CREATE INDEX IF NOT EXISTS idx_event_theater ON timeout_event(theater);
CREATE INDEX IF NOT EXISTS idx_event_rule ON timeout_event(rule_id);
CREATE INDEX IF NOT EXISTS idx_event_room ON timeout_event(room_id);

CREATE TABLE IF NOT EXISTS timeout_rule (
  rule_id TEXT PRIMARY KEY,
  rule_name TEXT,
  chat_time_out INTEGER,
  extra TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS chat_store (
  room_id TEXT PRIMARY KEY,
  store TEXT,
  theater TEXT,
  group_name TEXT,
  create_time TEXT,
  in_robot INTEGER NOT NULL DEFAULT 0,
  owner_id TEXT,
  owner_name TEXT,
  owner_dept TEXT,
  member_count INTEGER
);

CREATE TABLE IF NOT EXISTS robot_chat (
  room_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS employee (
  userid TEXT PRIMARY KEY,
  name TEXT
);

CREATE TABLE IF NOT EXISTS sync_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS timeout_import (
  filename TEXT PRIMARY KEY,
  range_start TEXT NOT NULL,
  range_end TEXT NOT NULL,
  file_sig TEXT,
  rows_in_file INTEGER,
  events INTEGER,
  unmatched INTEGER,
  imported_at TEXT NOT NULL
);
"""


def is_readonly_open_error(exc: BaseException) -> bool:
    """只读文件系统副本才回退 immutable；locked/损坏等错误继续抛出。"""
    return "unable to open database file" in str(exc).lower()


def connect_ro(db_path: str) -> sqlite3.Connection:
    """只读打开（用于真实的只读数据副本；immutable 避免创建 -wal/-shm）。"""
    conn = sqlite3.connect("file:" + quote(db_path) + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(timeout_event)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE timeout_event ADD COLUMN source TEXT")
    if "room_name" not in cols:
        conn.execute("ALTER TABLE timeout_event ADD COLUMN room_name TEXT")
    for col in ("content", "msgtype", "exclude_reason"):
        if col not in cols:
            conn.execute(f"ALTER TABLE timeout_event ADD COLUMN {col} TEXT")
    _archive_excel(conn)
    group_cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_store)")}
    for col, col_type in (
        ("create_time", "TEXT"),
        ("owner_id", "TEXT"),
        ("owner_name", "TEXT"),
        ("owner_dept", "TEXT"),
        ("member_count", "INTEGER"),
    ):
        if col not in group_cols:
            conn.execute(f"ALTER TABLE chat_store ADD COLUMN {col} {col_type}")
    employee_cols = {row[1] for row in conn.execute("PRAGMA table_info(employee)")}
    if "name" not in employee_cols:
        conn.execute("ALTER TABLE employee ADD COLUMN name TEXT")
    conn.commit()


def _archive_excel(conn: sqlite3.Connection) -> None:
    """管家 Excel 导入的旧事件挪到 timeout_event_legacy 存档（不删除），页面只用接口算出的事件。"""
    n = conn.execute("SELECT COUNT(*) FROM timeout_event WHERE source = 'excel'").fetchone()[0]
    if not n:
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS timeout_event_legacy AS SELECT * FROM timeout_event WHERE 0"
    )
    legacy_cols = {row[1] for row in conn.execute("PRAGMA table_info(timeout_event_legacy)")}
    cols = [row[1] for row in conn.execute("PRAGMA table_info(timeout_event)")]
    for col in cols:
        if col not in legacy_cols:
            conn.execute(f"ALTER TABLE timeout_event_legacy ADD COLUMN {col} TEXT")
    names = ", ".join(cols)
    conn.execute(f"INSERT INTO timeout_event_legacy({names}) SELECT {names} FROM timeout_event WHERE source = 'excel'")
    conn.execute("DELETE FROM timeout_event WHERE source = 'excel'")
    conn.execute(
        "INSERT INTO sync_meta(key, value) VALUES ('legacy_archived', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + excluded.value",
        (str(n),),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def replace_context(
    conn: sqlite3.Connection,
    *,
    robot_chat_ids: Iterable[str],
    employee_ids: Iterable[str],
    group_rows: Iterable[dict[str, Any]],
    employee_names: dict[str, str] | None = None,
) -> None:
    robot_set = {str(x) for x in robot_chat_ids if x}
    conn.execute("DELETE FROM robot_chat")
    conn.executemany("INSERT INTO robot_chat(room_id) VALUES (?)", [(x,) for x in sorted(robot_set)])

    names = {str(key): "" if value is None else str(value) for key, value in (employee_names or {}).items() if key}
    employee_ids_set = {str(i) for i in employee_ids if i} | set(names)
    conn.execute("DELETE FROM employee")
    conn.executemany(
        "INSERT INTO employee(userid, name) VALUES (?, ?)",
        [
            (userid, names.get(userid, "").strip() or None)
            for userid in sorted(employee_ids_set)
        ],
    )

    conn.execute("DELETE FROM chat_store")
    rows = []
    for rec in group_rows:
        room_id = str(rec.get("group_id") or "").strip()
        if not room_id:
            continue
        member_count = rec.get("member_count")
        if member_count is not None:
            try:
                member_count = int(member_count)
            except (TypeError, ValueError):
                member_count = None
        rows.append(
            (
                room_id,
                rec.get("store") or "",
                rec.get("theater") or "",
                rec.get("group_name") or "",
                rec.get("create_time") or None,
                1 if room_id in robot_set else 0,
                rec.get("owner_id") or None,
                rec.get("owner") or None,
                rec.get("dept") or None,
                member_count,
            )
        )
    conn.executemany(
        "INSERT OR REPLACE INTO chat_store("
        "room_id, store, theater, group_name, create_time, in_robot, owner_id, owner_name, owner_dept, member_count"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def load_robot_rooms(conn: sqlite3.Connection) -> list[str]:
    return [row["room_id"] for row in conn.execute("SELECT room_id FROM robot_chat")]


def load_employees(conn: sqlite3.Connection) -> list[str]:
    return [row["userid"] for row in conn.execute("SELECT userid FROM employee")]


def load_chat_map(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in conn.execute("SELECT room_id, store, theater, group_name FROM chat_store"):
        out[row["room_id"]] = {
            "store": row["store"] or "",
            "theater": row["theater"] or "",
            "group_name": row["group_name"] or "",
        }
    return out


def upsert_rules(conn: sqlite3.Connection, rules: Iterable[dict[str, Any]], updated_at: str) -> None:
    for rule in rules:
        conn.execute(
            """
            INSERT INTO timeout_rule(rule_id, rule_name, chat_time_out, extra, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(rule_id) DO UPDATE SET
              rule_name = excluded.rule_name,
              chat_time_out = excluded.chat_time_out,
              extra = excluded.extra,
              updated_at = excluded.updated_at
            """,
            (
                rule["rule_id"],
                rule.get("rule_name") or "",
                int(rule.get("chat_time_out") or 0),
                rule.get("extra") or "",
                updated_at,
            ),
        )


def replace_range_events(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    events: list[dict[str, Any]],
) -> None:
    start_sql = start.isoformat() + " 00:00:00"
    end_sql = date.fromordinal(end.toordinal() + 1).isoformat() + " 00:00:00"
    conn.execute(
        "DELETE FROM timeout_event WHERE created_at >= ? AND created_at < ?",
        (start_sql, end_sql),
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO timeout_event(
          msg_id, rule_id, rule_name, room_id, store, theater, created_at, customer_id, source, room_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                ev["msg_id"],
                ev["rule_id"],
                ev.get("rule_name") or "",
                ev.get("room_id") or "",
                ev.get("store") or "",
                ev.get("theater") or "",
                ev["created_at"],
                ev.get("customer_id") or "",
                ev.get("source") or "excel",
                ev.get("room_name") or "",
            )
            for ev in events
        ],
    )


def clear_non_excel_events(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM timeout_event WHERE ifnull(source, '') != 'excel'")


def upsert_import(conn: sqlite3.Connection, rec: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO timeout_import(
          filename, range_start, range_end, file_sig, rows_in_file, events, unmatched, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(filename) DO UPDATE SET
          range_start = excluded.range_start,
          range_end = excluded.range_end,
          file_sig = excluded.file_sig,
          rows_in_file = excluded.rows_in_file,
          events = excluded.events,
          unmatched = excluded.unmatched,
          imported_at = excluded.imported_at
        """,
        (
            rec["filename"],
            rec["range_start"],
            rec["range_end"],
            rec.get("file_sig") or "",
            int(rec.get("rows_in_file") or 0),
            int(rec.get("events") or 0),
            int(rec.get("unmatched") or 0),
            rec["imported_at"],
        ),
    )


def get_import(conn: sqlite3.Connection, filename: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM timeout_import WHERE filename = ?",
        (filename,),
    ).fetchone()
    return dict(row) if row else None


def list_imports(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM timeout_import ORDER BY range_start, filename"
        )
    ]


def replace_day_events(conn: sqlite3.Connection, day: date, events: list[dict[str, Any]]) -> None:
    conn.execute(
        "DELETE FROM timeout_event WHERE substr(created_at, 1, 10) = ?",
        (day.isoformat(),),
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO timeout_event(
          msg_id, rule_id, rule_name, room_id, store, theater, created_at, customer_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                ev["msg_id"],
                ev["rule_id"],
                ev.get("rule_name") or "",
                ev.get("room_id") or "",
                ev.get("store") or "",
                ev.get("theater") or "",
                ev["created_at"],
                ev.get("customer_id") or "",
            )
            for ev in events
        ],
    )


def event_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM timeout_event").fetchone()
    return int(row["n"] if row else 0)
