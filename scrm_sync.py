"""从企微 SCRM 拉取客户群，按群主部门归店，并筛出已拉超时提醒机器人的群。"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from scrm_client import ScrmClient, ScrmError

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def load_dotenv(path: str) -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")


def load_scrm_env(root: str) -> None:
    load_dotenv(os.path.join(root, ".env"))
    sibling = os.path.join(os.path.dirname(root), "scrm-api", ".env")
    load_dotenv(sibling)


def make_scrm_client(root: str) -> ScrmClient:
    load_scrm_env(root)
    base_url = os.environ.get("SCRM_BASE_URL", "").strip()
    app_id = os.environ.get("SCRM_APP_ID", "").strip()
    app_secret = os.environ.get("SCRM_APP_SECRET", "").strip()
    if not base_url or not app_id or not app_secret:
        raise ScrmError("缺少 SCRM_BASE_URL / SCRM_APP_ID / SCRM_APP_SECRET，请配置 .env")
    return ScrmClient(base_url, app_id, app_secret)


def extract_theater(dept_str: str | None) -> str | None:
    """部门路径里「门店」下一段视为战区。"""
    if not isinstance(dept_str, str) or not dept_str.strip():
        return None
    parts = [p.strip() for p in dept_str.split("/") if p.strip()]
    for i, part in enumerate(parts):
        if part == "门店" and i + 1 < len(parts):
            nxt = parts[i + 1]
            if nxt.startswith("猛士"):
                return None
            return nxt
    return None


def extract_store(dept_str: str | None) -> str | None:
    """从部门路径中取门店名（以「猛士」开头的最末一段）。"""
    if not isinstance(dept_str, str) or not dept_str.strip():
        return None
    parts = [p.strip() for p in dept_str.split("/") if p.strip()]
    for part in reversed(parts):
        if part.startswith("猛士"):
            return part
    return None


def _as_str_id(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return _as_str_id(value[0] if value else "")
    return str(value).strip()


def _dept_ids(value: Any) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        out = []
        for item in value:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        return out
    if isinstance(value, str):
        text = value.replace("[", "").replace("]", "")
        return _dept_ids([p.strip() for p in text.split(",") if p.strip()])
    try:
        return [int(value)]
    except (TypeError, ValueError):
        return []


def _build_dept_path(dept_id: int | None, by_id: dict[int, dict[str, Any]]) -> str:
    if not dept_id:
        return ""
    parts: list[str] = []
    seen: set[int] = set()
    current = dept_id
    while current and current in by_id and current not in seen:
        seen.add(current)
        node = by_id[current]
        name = (node.get("name") or "").strip()
        if name:
            parts.append(name)
        parent = node.get("parent_id")
        try:
            current = int(parent) if parent not in (None, "", 0, "0") else 0
        except (TypeError, ValueError):
            break
    return "/".join(reversed(parts))


def _parse_create_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    if ts > 10_000_000_000:
        ts = ts / 1000
    try:
        return datetime.fromtimestamp(ts, SHANGHAI)
    except (OverflowError, OSError, ValueError):
        return None


def _group_to_row(
    rec: dict[str, Any],
    users: dict[str, dict[str, Any]],
    dept_by_id: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    owner_id = _as_str_id(rec.get("owner"))
    user = users.get(owner_id) or {}
    main_ids = _dept_ids(user.get("main_department_id"))
    all_ids = _dept_ids(user.get("department_id"))
    dept_id = (main_ids or all_ids or [None])[0]
    dept_path = _build_dept_path(dept_id, dept_by_id)
    create_time = _parse_create_time(rec.get("create_time"))
    try:
        member_count = int(rec.get("count") or 0)
    except (TypeError, ValueError):
        member_count = 0
    return {
        "group_id": rec.get("chat_id") or "",
        "group_name": rec.get("name") or "",
        "owner": user.get("username") or owner_id,
        "owner_id": owner_id,
        "dept": dept_path,
        "store": extract_store(dept_path),
        "theater": extract_theater(dept_path),
        "create_time": create_time.isoformat() if create_time else None,
        "member_count": member_count,
        "customer_count": 0,
    }


def _resolve_robots(
    users: list[dict[str, Any]],
    configured_ids: list[str],
    username: str,
    position_keyword: str,
) -> list[dict[str, str]]:
    by_id = {_as_str_id(u.get("userId") or u.get("userid")): u for u in users}
    robots: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(uid: str, user: dict[str, Any] | None) -> None:
        if not uid or uid in seen:
            return
        seen.add(uid)
        robots.append(
            {
                "userid": uid,
                "username": (user or {}).get("username") or uid,
                "position": (user or {}).get("position") or "",
            }
        )

    for uid in configured_ids:
        add(uid, by_id.get(uid))
    if robots:
        return robots

    name = (username or "").strip()
    if name:
        for uid, user in by_id.items():
            if (user.get("username") or "").strip() == name:
                add(uid, user)
        if robots:
            return robots

    keyword = (position_keyword or "").strip()
    if keyword:
        for uid, user in by_id.items():
            if keyword in (user.get("position") or ""):
                add(uid, user)
    return robots


def sync_from_scrm(root: str, client: ScrmClient | None = None) -> dict[str, Any]:
    if client is None:
        client = make_scrm_client(root)

    configured_ids = [
        p.strip()
        for p in os.environ.get("SCRM_ROBOT_USERIDS", "").split(",")
        if p.strip()
    ]
    robot_username = os.environ.get("SCRM_ROBOT_USERNAME", "刘明轩").strip()
    position_keyword = os.environ.get("SCRM_ROBOT_POSITION", "").strip()

    log.info("SCRM 拉取员工与部门")
    users = client.list_users()
    departments = client.list_departments()
    user_by_id = {_as_str_id(u.get("userId") or u.get("userid")): u for u in users}
    dept_by_id: dict[int, dict[str, Any]] = {}
    for dept in departments:
        try:
            dept_by_id[int(dept["id"])] = dept
        except (KeyError, TypeError, ValueError):
            continue

    robots = _resolve_robots(users, configured_ids, robot_username, position_keyword)
    if not robots:
        raise ScrmError(
            "未找到超时提醒机器人账号。请在 .env 设置 SCRM_ROBOT_USERIDS 或 SCRM_ROBOT_USERNAME。"
        )

    log.info("SCRM 拉取全量客户群")
    all_groups = client.list_groups()
    log.info("全量客户群 %s", len(all_groups))

    robot_chat_ids: set[str] = set()
    robot_meta = []
    for robot in robots:
        uid = robot["userid"]
        log.info("拉取机器人成员群 %s (%s)", robot["username"], uid)
        groups = client.list_groups(userid=uid, user_type=1)
        ids = {str(g.get("chat_id") or "") for g in groups if g.get("chat_id")}
        robot_chat_ids.update(ids)
        robot_meta.append({**robot, "group_count": len(ids)})
        log.info("  %s 群 %s", robot["username"], len(ids))

    all_rows = [_group_to_row(g, user_by_id, dept_by_id) for g in all_groups]
    by_id = {str(r["group_id"]): r for r in all_rows if r.get("group_id")}
    filter_rows = [by_id[cid] for cid in robot_chat_ids if cid in by_id]
    extra = [g for g in robot_chat_ids if g not in by_id]
    if extra:
        log.warning("机器人群有 %s 个不在全量列表，已忽略", len(extra))

    def summarize(rows: list[dict[str, Any]]) -> tuple[int, int, int]:
        valid = sum(1 for r in rows if r.get("store"))
        return len(rows), valid, len(rows) - valid

    all_total, all_valid, all_invalid = summarize(all_rows)
    filter_total, filter_valid, filter_invalid = summarize(filter_rows)

    return {
        "all_rows": all_rows,
        "filter_rows": filter_rows,
        "summary": {
            "source": "scrm",
            "all_total": all_total,
            "all_valid": all_valid,
            "all_invalid": all_invalid,
            "filter_total": filter_total,
            "filter_valid": filter_valid,
            "filter_invalid": filter_invalid,
            "robots": robot_meta,
        },
        "robot_chat_ids": sorted(robot_chat_ids),
        "employee_ids": sorted(user_by_id.keys()),
        # 只供 replace_context 写 employee.name；data.json 只存 stats/summary，不落盘本字段。
        "employee_names": {
            userid: (user.get("username") or "").strip()
            for userid, user in user_by_id.items()
            if userid
        },
    }
