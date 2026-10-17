"""DeepSeek 复核：只处理规则留下的消息，结果按正文哈希缓存。

客户消息正文会发给 DeepSeek。密钥只从环境变量 DEEPSEEK_API_KEY 读取，不写日志。
页面计分只读缓存。导入新超时数据，或打开仍有未缓存正文的评分区间时，后台自动补齐；
已缓存的正文不会再次发送，除非管理接口显式 force。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from score_rules import format_llm_reason
from timeout_store import connect

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")

PROMPT_VERSION = "v1"
BATCH_SIZE = 20
MAX_WORKERS = 100
FAIL_COOLDOWN_SECONDS = 600
MODEL = "deepseek-chat"
API_URL = "https://api.deepseek.com/chat/completions"
PROMPT = (
    "你是汽车售后服务群质检。对每条客户群消息判断是否属于需要门店回复的真实客户诉求/问题。"
    "不应计入的类型：员工/营销话术、寒暄结束语、纯表情、仅@人、闲聊、系统自动消息、广告、非提问的告知/感谢、重复。"
    "返回JSON {\"items\":[{\"i\":序号,\"count\":true/false,\"reason\":\"不计入时给不超过10字原因，计入时填有效\"}]}，每条都要返回。图片/语音/视频无法判断时 count=true。拿不准时 count=true。"
)

_lock = threading.Lock()
_running: set[str] = set()
_follow: dict[str, bool] = {}
_threads: list[threading.Thread] = []


def text_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def llm_configured(root: str) -> bool:
    return bool(api_key(root))


def api_key(root: str) -> str:
    _load_env(os.path.join(root, ".env"))
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


def review_texts(
    texts: list[str],
    *,
    key: str,
    post: Callable[..., Any],
    sleep: Callable[[float], None] = time.sleep,
    executor_factory: Callable[..., Any] | None = None,
    attempts: int = 8,
    batch_size: int = BATCH_SIZE,
) -> dict[str, dict[str, Any]]:
    verdicts, _failed = _review(
        texts,
        key=key,
        post=post,
        sleep=sleep,
        executor_factory=executor_factory,
        attempts=attempts,
        batch_size=batch_size,
    )
    return verdicts


def execute_review(
    root: str,
    start: date,
    end: date,
    *,
    key: str | None = None,
    post: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    force: bool = False,
    attempts: int = 8,
) -> dict[str, Any]:
    secret = key if key is not None else api_key(root)
    if not secret:
        _write_job(root, start, end, status="off", total=0, done=0, failed=0, note="未配置密钥")
        return {"reviewed": 0, "cached": 0, "failed": 0, "status": "off"}

    from score_query import load_rule_events

    events, _unmatched, _missing = load_rule_events(root, start, end)
    # KPI-only classification can retain a different message after burst merging.
    # Review the union so neither consumer waits forever for an unreachable text.
    from score_query import prepare_events
    kpi_events, _, _, _ = prepare_events(root, start, end, use_llm=False)
    pending = _dedupe(_pending_texts(events) + _pending_texts(kpi_events))
    cached = cache_get(root, pending)
    todo = pending if force else [text for text in pending if text not in cached]
    _write_job(root, start, end, status="running", total=len(todo), done=0, failed=0, note="")
    if not todo:
        _write_job(root, start, end, status="done", total=0, done=0, failed=0, note="缓存已覆盖")
        return {"reviewed": 0, "cached": len(cached), "failed": 0, "status": "done"}

    done = {"n": 0}
    failed = {"n": 0}

    def on_batch(batch: list[str], verdicts: dict[str, dict[str, Any]]) -> None:
        with _lock:
            if verdicts:
                cache_put(root, verdicts)
            done["n"] += len(batch)
            if not verdicts:
                failed["n"] += 1
            _write_job(
                root,
                start,
                end,
                status="running",
                total=len(todo),
                done=done["n"],
                failed=failed["n"],
                note="",
            )

    verdicts, batch_failed = _review(
        todo,
        key=secret,
        post=post or _default_post,
        sleep=sleep,
        attempts=attempts,
        on_batch=on_batch,
    )
    # on_batch already counts empty results; batch_failed is the same number.
    failed_n = failed["n"] or batch_failed
    note = "有批次失败，未返回的消息仍计入" if failed_n else "完成"
    _write_job(root, start, end, status="done", total=len(todo), done=len(todo), failed=failed_n, note=note)
    return {"reviewed": len(verdicts), "cached": len(cached), "failed": failed_n, "status": "done"}


def start_review_job(
    root: str,
    start: date,
    end: date,
    *,
    force: bool = False,
    follow_up: bool = False,
    post: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    key = _range_key(start, end)
    with _lock:
        if key in _running:
            if follow_up or force:
                _follow[key] = bool(_follow.get(key)) or bool(force)
            return review_status(root, start, end)
        _running.add(key)
        _follow.pop(key, None)
    _write_job(root, start, end, status="running", total=0, done=0, failed=0, note="已启动")

    def work() -> None:
        current_force = force
        try:
            while True:
                execute_review(root, start, end, force=current_force, post=post)
                with _lock:
                    if key not in _follow:
                        break
                    current_force = bool(_follow.pop(key))
        except Exception:
            log.warning("LLM 复核任务失败")
            _write_job(root, start, end, status="error", total=0, done=0, failed=0, note="复核失败")
        finally:
            with _lock:
                _running.discard(key)
                _follow.pop(key, None)

    thread = threading.Thread(target=work, name="score-llm-review", daemon=True)
    with _lock:
        _threads.append(thread)
    thread.start()
    return review_status(root, start, end)


def wait_review_jobs() -> None:
    with _lock:
        pending = [thread for thread in _threads if thread.is_alive()]
        _threads[:] = pending
    for thread in pending:
        thread.join()


def maybe_autostart_review(
    root: str,
    start: date,
    end: date,
    *,
    pending: int | None = None,
    cached: int | None = None,
    source: str = "page",
) -> dict[str, Any]:
    """后台增量复核。没配密钥时直接返回，不抛错。"""
    if not api_key(root):
        return {"status": "off", "total": 0, "done": 0, "failed": 0, "note": "未配置密钥", "updated_at": ""}
    if source == "page" and pending is not None and cached is not None and cached >= pending:
        return review_status(root, start, end)
    status = review_status(root, start, end)
    if status.get("status") == "running":
        if source == "import":
            return start_review_job(root, start, end, follow_up=True)
        return status
    # A GET may start a new range once, but must never reschedule a terminal job.
    # Imports / explicit review requests own retries and newly arrived data.
    if source == "page" and status.get("status") in {"done", "error", "off"}:
        return status
    return start_review_job(root, start, end, force=False)


def review_status(root: str, start: date, end: date) -> dict[str, Any]:
    conn = _open(root)
    if conn is None:
        return {"status": "idle", "total": 0, "done": 0, "failed": 0, "note": "", "updated_at": ""}
    try:
        _ensure(conn)
        row = conn.execute(
            "SELECT status, total, done, failed, note, updated_at FROM score_llm_job WHERE range_key = ?",
            (_range_key(start, end),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"status": "idle", "total": 0, "done": 0, "failed": 0, "note": "", "updated_at": ""}
    return {
        "status": row["status"],
        "total": int(row["total"] or 0),
        "done": int(row["done"] or 0),
        "failed": int(row["failed"] or 0),
        "note": row["note"] or "",
        "updated_at": row["updated_at"] or "",
    }


def cache_get(root: str, texts: list[str]) -> dict[str, dict[str, Any]]:
    wanted = _dedupe(texts)
    if not wanted:
        return {}
    conn = _open(root)
    if conn is None:
        return {}
    try:
        _ensure(conn)
        found: dict[str, dict[str, Any]] = {}
        hashes = {text_hash(text): text for text in wanted}
        keys = list(hashes)
        for offset in range(0, len(keys), 400):
            chunk = keys[offset : offset + 400]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""
                SELECT text_hash, keep, reason FROM score_llm_cache
                WHERE prompt_version = ? AND text_hash IN ({marks})
                """,
                [PROMPT_VERSION, *chunk],
            )
            for row in rows:
                text = hashes.get(row["text_hash"])
                if text is None:
                    continue
                found[text] = {"count": bool(row["keep"]), "reason": row["reason"] or ""}
        return found
    finally:
        conn.close()


def cache_put(root: str, verdicts: dict[str, dict[str, Any]]) -> None:
    if not verdicts:
        return
    conn = _open(root)
    if conn is None:
        return
    try:
        _ensure(conn)
        now = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        conn.executemany(
            """
            INSERT INTO score_llm_cache(text_hash, prompt_version, keep, reason, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(text_hash, prompt_version) DO UPDATE SET
              keep = excluded.keep,
              reason = excluded.reason,
              updated_at = excluded.updated_at
            """,
            [
                (text_hash(text), PROMPT_VERSION, 1 if item.get("count") else 0, str(item.get("reason") or "")[:20], now)
                for text, item in verdicts.items()
                if isinstance(text, str) and text.strip()
            ],
        )
        conn.commit()
    finally:
        conn.close()


def apply_verdicts(events: list[dict[str, Any]], verdicts: dict[str, dict[str, Any]]) -> None:
    for event in events:
        if event.get("reason"):
            continue
        msg = event.get("msg")
        if not isinstance(msg, str) or not msg.strip():
            continue
        verdict = verdicts.get(msg.strip())
        if not verdict or verdict.get("count") is not False:
            continue
        event["llm_exclude"] = True
        event["llm_reason"] = verdict.get("reason") or ""
        event["reason"] = format_llm_reason(str(event["llm_reason"]))


def _review(
    texts: list[str],
    *,
    key: str,
    post: Callable[..., Any],
    sleep: Callable[[float], None],
    executor_factory: Callable[..., Any] | None = None,
    attempts: int = 8,
    batch_size: int = BATCH_SIZE,
    on_batch: Callable[[list[str], dict[str, dict[str, Any]]], None] | None = None,
) -> tuple[dict[str, dict[str, Any]], int]:
    unique = _dedupe(texts)
    batches = [unique[index : index + batch_size] for index in range(0, len(unique), batch_size)]
    if not batches:
        return {}, 0
    factory = executor_factory or ThreadPoolExecutor

    def run(batch: list[str]) -> tuple[list[str], dict[str, dict[str, Any]]]:
        verdicts = _review_batch(batch, key=key, post=post, sleep=sleep, attempts=attempts)
        if on_batch is not None:
            on_batch(batch, verdicts)
        return batch, verdicts

    merged: dict[str, dict[str, Any]] = {}
    failed = 0
    with factory(MAX_WORKERS) as pool:
        for _batch, verdicts in pool.map(run, batches):
            if not verdicts:
                failed += 1
            merged.update(verdicts)
    return merged, failed


def _review_batch(
    texts: list[str],
    *,
    key: str,
    post: Callable[..., Any],
    sleep: Callable[[float], None],
    attempts: int,
) -> dict[str, dict[str, Any]]:
    body = "\n".join(f"{index}. {text[:300].replace(chr(10), ' ')}" for index, text in enumerate(texts))
    payload = {
        "model": MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": body},
        ],
    }
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    for attempt in range(attempts):
        try:
            response = post(API_URL, headers=headers, json=payload, timeout=180)
            status = int(getattr(response, "status_code", 0) or 0)
            if status == 429 or status >= 500:
                sleep((2**attempt) + random.random())
                continue
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            items = json.loads(content).get("items") or []
            out: dict[str, dict[str, Any]] = {}
            for item in items:
                index = int(item.get("i"))
                if not 0 <= index < len(texts):
                    continue
                out[texts[index]] = {
                    "count": _as_keep(item.get("count")),
                    "reason": str(item.get("reason") or "").strip()[:20],
                }
            return out
        except Exception:
            sleep(1 + attempt)
    log.warning("DeepSeek 批次失败，本批不剔除")
    return {}


def _as_keep(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no"}
    if value is None:
        return True
    return bool(value)


def _failed_recently(status: dict[str, Any]) -> bool:
    failed = status.get("status") == "error" or (
        status.get("status") == "done" and int(status.get("failed") or 0) > 0
    )
    if not failed:
        return False
    raw = str(status.get("updated_at") or "")
    try:
        stamp = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
    except ValueError:
        return False
    return (datetime.now(SHANGHAI) - stamp).total_seconds() < FAIL_COOLDOWN_SECONDS


def _pending_texts(events: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for event in events:
        if event.get("reason"):
            continue
        msg = event.get("msg")
        if isinstance(msg, str) and msg.strip():
            texts.append(msg.strip())
    return texts


def _dedupe(texts: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for raw in texts:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    unique.sort()
    return unique


class _HttpResp:
    def __init__(self, status: int, payload: dict[str, Any]):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


def _default_post(url: str, headers: dict[str, str] | None = None, json: dict[str, Any] | None = None, timeout: int = 180):
    data = json_lib_dumps(json or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return _HttpResp(getattr(response, "status", 200), json_lib_loads(raw))
    except urllib.error.HTTPError as exc:
        return _HttpResp(exc.code, {})


def json_lib_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def json_lib_loads(raw: str) -> dict[str, Any]:
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _range_key(start: date, end: date) -> str:
    return f"{start.isoformat()}|{end.isoformat()}"


def _write_job(
    root: str,
    start: date,
    end: date,
    *,
    status: str,
    total: int,
    done: int,
    failed: int,
    note: str,
) -> None:
    conn = _open(root)
    if conn is None:
        return
    try:
        _ensure(conn)
        conn.execute(
            """
            INSERT INTO score_llm_job(range_key, status, total, done, failed, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(range_key) DO UPDATE SET
              status = excluded.status,
              total = excluded.total,
              done = excluded.done,
              failed = excluded.failed,
              note = excluded.note,
              updated_at = excluded.updated_at
            """,
            (
                _range_key(start, end),
                status,
                int(total),
                int(done),
                int(failed),
                note,
                datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _ensure(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS score_llm_cache (
          text_hash TEXT NOT NULL,
          prompt_version TEXT NOT NULL,
          keep INTEGER NOT NULL,
          reason TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL,
          PRIMARY KEY (text_hash, prompt_version)
        );
        CREATE TABLE IF NOT EXISTS score_llm_job (
          range_key TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          total INTEGER NOT NULL DEFAULT 0,
          done INTEGER NOT NULL DEFAULT 0,
          failed INTEGER NOT NULL DEFAULT 0,
          note TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL
        );
        """
    )


def _open(root: str):
    path = os.path.join(root, "timeout.db")
    if not os.path.isfile(path):
        return None
    return connect(path)


def _load_env(path: str) -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")
