"""分层 SCRM 同步：限流客户端、groups/members/full 三层任务、锁与运行指标。

设计约束：
- SCRM /openapi 在并发 >2 时会明显退化，默认串行（并发 1），硬上限 2。
- 网络请求绝不持有 timeout.db 的 .sync.lock；写库只在短临界区内完成。
- 每层各有自己的 flock，上一轮未结束时直接跳过。
- 指标落 JSONL 和 timeout.db 的 sync_meta，不记录 token/secret。
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import random
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from scrm_client import ScrmClient, ScrmError
from scrm_sync import (
    _as_str_id,
    _group_to_row,
    load_scrm_env,
    sync_from_scrm,
)
import timeout_store

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
ROOT = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONCURRENCY = 1
HARD_MAX_CONCURRENCY = 2
DEFAULT_MIN_INTERVAL = 0.2
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_BASE = 2.0
DEFAULT_BACKOFF_CAP = 60.0
DEFAULT_SLOW_THRESHOLD = 2.0
DEFAULT_SLOW_CAP = 30.0
SYNC_LOCK_TIMEOUT = 120.0

TIERS = ("groups", "members", "full")
TIER_LOCKS = {"groups": "groups", "members": "members", "full": "groups"}

# 每层只会在对应分钟启动；simulate_day() 严格按这张表生成。
TIER_SCHEDULE: dict[str, tuple[str, ...]] = {
    "groups": tuple(f"{hour:02d}:20" for hour in range(24)),
    "full": ("02:00",),
    "members": ("02:30", "06:30", "10:30", "14:30", "18:30", "22:30"),
}
BLACKOUT_TIMES = ("00:00", "08:30", "09:00", "12:00")
BLACKOUT_MINUTES = 5


class ScrmTierError(RuntimeError):
    """分层同步的业务失败（例如缓存刷新错误率过高）。"""


# ---------------------------------------------------------------------------
# 环境与时间
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _concurrency_from_env() -> int:
    value = _env_int("SCRM_SYNC_CONCURRENCY", DEFAULT_CONCURRENCY)
    return max(1, min(HARD_MAX_CONCURRENCY, value))


def _interval_from_env() -> float:
    value = _env_float("SCRM_SYNC_MIN_INTERVAL", DEFAULT_MIN_INTERVAL)
    return max(0.0, value)


def _tier_limit() -> int | None:
    value = _env_int("SCRM_TIER_LIMIT", 0)
    return value if value > 0 else None


def _timeout_db_path(root: str) -> str:
    return os.environ.get("CLEANER_DB_PATH") or os.path.join(root, "timeout.db")


def _cache_db_path() -> str:
    value = os.environ.get("SCRM_CACHE_DB", "").strip()
    if not value:
        value = "~/m-hero-run/data/scrm_cache.sqlite3"
    return os.path.expanduser(value)


def _metrics_path() -> str:
    value = os.environ.get("SCRM_TIER_METRICS", "").strip()
    if not value:
        value = "~/m-hero-run/cron-logs/scrm-tier-metrics.jsonl"
    return os.path.expanduser(value)


def _now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


_SECRET_RE = re.compile(r"(?i)(access_token|app_secret|secret|password)=([^&\s]+)")


def _redact(text: Any) -> str:
    """日志/异常里的凭证脱敏；不打印完整请求 URL。"""
    return _SECRET_RE.sub(r"\1=***", str(text or ""))


def _hm_to_minutes(hm: str | int) -> int:
    if isinstance(hm, int):
        if 0 <= hm < 1440:
            return hm
        raise ValueError(f"非法时间: {hm}")
    if hasattr(hm, "hour") and hasattr(hm, "minute"):
        return int(hm.hour) * 60 + int(hm.minute)
    text = str(hm).strip()
    try:
        parts = text.split(":")
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"非法时间: {hm}") from exc
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"非法时间: {hm}")
    return hour * 60 + minute


def _minutes_to_hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def blackout_ok(hm: str | int) -> bool:
    """返回 False 表示该时刻落在 00:00/08:30/09:00/12:00 前后 5 分钟内。"""
    current = _hm_to_minutes(hm)
    for center in BLACKOUT_TIMES:
        diff = abs(current - _hm_to_minutes(center))
        diff = min(diff, 1440 - diff)
        if diff <= BLACKOUT_MINUTES:
            return False
    return True


def simulate_day() -> list[tuple[str, str]]:
    """模拟一天 1440 分钟，返回 (HH:MM, tier)，黑名单窗口内不启动。"""
    by_time = {
        minute: tier
        for tier, times in TIER_SCHEDULE.items()
        for minute in (_hm_to_minutes(item) for item in times)
    }
    runs: list[tuple[str, str]] = []
    for minute in range(1440):
        if not blackout_ok(minute):
            continue
        tier = by_time.get(minute)
        if tier:
            runs.append((_minutes_to_hm(minute), tier))
    return runs


def plan_for_tier(tier: str, root: str | None = None) -> dict[str, Any]:
    """dry-run 计划：只读环境，不请求 SCRM、不创建锁/数据库。"""
    if tier not in TIERS:
        raise ValueError(f"未知 tier: {tier}")
    root = os.path.abspath(root or ROOT)
    return {
        "tier": tier,
        "dry_run": True,
        "schedule": list(TIER_SCHEDULE[tier]),
        "lock": os.path.join(root, f".sync-tier-{TIER_LOCKS[tier]}.lock"),
        "timeout_db": _timeout_db_path(root),
        "cache_db": _cache_db_path() if tier == "members" else None,
        "metrics": _metrics_path(),
        "concurrency": _concurrency_from_env(),
        "min_interval": _interval_from_env(),
        "tier_limit": _tier_limit(),
    }


# ---------------------------------------------------------------------------
# 限流客户端
# ---------------------------------------------------------------------------


class ThrottledScrmClient(ScrmClient):
    """在 ScrmClient 上加入全局并发、起始间隔、重试和运行指标。

    默认参数来自 SCRM_SYNC_CONCURRENCY / SCRM_SYNC_MIN_INTERVAL。所有计时/睡眠
    都通过可注入的 clock/sleep，便于测试时使用虚拟时间。
    """

    def __init__(
        self,
        base_url: str,
        app_id: str,
        app_secret: str,
        timeout: int = 60,
        *,
        concurrency: int | None = None,
        min_interval: float | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_cap: float = DEFAULT_BACKOFF_CAP,
        slow_threshold: float = DEFAULT_SLOW_THRESHOLD,
        slow_cap: float = DEFAULT_SLOW_CAP,
        jitter_ratio: float = 0.5,
        rng: Any | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        super().__init__(base_url, app_id, app_secret, timeout=timeout)
        if concurrency is None:
            concurrency = _concurrency_from_env()
        if min_interval is None:
            min_interval = _interval_from_env()

        self.concurrency = max(1, min(HARD_MAX_CONCURRENCY, int(concurrency)))
        self.min_interval = max(0.0, float(min_interval))
        self.max_attempts = max(1, min(DEFAULT_MAX_ATTEMPTS, int(max_attempts)))
        self.backoff_base = max(0.0, float(backoff_base))
        self.backoff_cap = max(0.0, float(backoff_cap))
        self.slow_threshold = max(0.0, float(slow_threshold))
        self.slow_cap = max(0.0, float(slow_cap))
        self.jitter_ratio = max(0.0, min(1.0, float(jitter_ratio)))

        self._rng = rng or random.Random()
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep

        self._semaphore = threading.BoundedSemaphore(self.concurrency)
        self._pace_lock = threading.Lock()
        self._token_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._last_start: float | None = None
        self._cooldown_until = 0.0

        self._started_at = self._clock()
        self._requests = 0
        self._ok = 0
        self._retries = 0
        self._errors: dict[str, int] = {}
        self._latencies: list[float] = []

    # -- token：并发下也只取一次 ------------------------------------------
    def access_token(self) -> str:
        with self._token_lock:
            return super().access_token()

    # -- 指标 --------------------------------------------------------------
    def _record_request(self) -> None:
        with self._metrics_lock:
            self._requests += 1

    def _record_ok(self) -> None:
        with self._metrics_lock:
            self._ok += 1

    def _record_error(self, kind: str) -> None:
        with self._metrics_lock:
            self._errors[kind] = self._errors.get(kind, 0) + 1

    def _record_retry(self) -> None:
        with self._metrics_lock:
            self._retries += 1

    def _record_latency(self, seconds: float) -> None:
        with self._metrics_lock:
            self._latencies.append(max(0.0, float(seconds)))

    def reset_metrics(self) -> None:
        with self._metrics_lock:
            self._started_at = self._clock()
            self._requests = 0
            self._ok = 0
            self._retries = 0
            self._errors = {}
            self._latencies = []

    def metrics(self) -> dict[str, Any]:
        with self._metrics_lock:
            latencies = sorted(self._latencies)
            requests = self._requests
            ok = self._ok
            retries = self._retries
            errors = dict(self._errors)
            started = self._started_at
        p50 = _percentile(latencies, 0.50)
        p95 = _percentile(latencies, 0.95)
        return {
            "requests": requests,
            "ok": ok,
            "errors": errors,
            "error_kinds": dict(errors),
            "retries": retries,
            "p50_ms": round(p50 * 1000.0, 1),
            "p95_ms": round(p95 * 1000.0, 1),
            "latency_ms": {
                "p50": round(p50 * 1000.0, 1),
                "p95": round(p95 * 1000.0, 1),
            },
            "seconds": round(max(0.0, self._clock() - started), 3),
        }

    @property
    def stats(self) -> dict[str, Any]:
        return self.metrics()

    # -- 节流 / 自适应降速 --------------------------------------------------
    def _pace(self) -> None:
        with self._pace_lock:
            now = self._clock()
            base = self.min_interval
            jitter = self._rng.uniform(0.0, base * self.jitter_ratio)
            earliest = now
            if self._last_start is not None:
                earliest = max(earliest, self._last_start + base + jitter)
            if self._cooldown_until > earliest:
                earliest = self._cooldown_until
            delay = max(0.0, earliest - now)
            self._last_start = earliest
        if delay > 0:
            self._sleep(delay)

    def _note_latency(self, latency: float) -> None:
        if latency <= self.slow_threshold:
            return
        until = self._clock() + min(self.slow_cap, latency)
        with self._pace_lock:
            self._cooldown_until = max(self._cooldown_until, until)

    def _backoff_delay(self, attempt: int) -> float:
        ceiling = min(self.backoff_cap, self.backoff_base * (2 ** max(0, attempt - 1)))
        if ceiling <= 0:
            return 0.0
        return self._rng.uniform(0.0, ceiling)

    # -- 单次 HTTP 请求（可被子类/测试替换） -------------------------------
    def _send_http(
        self,
        url: str,
        data: bytes | None,
        headers: dict[str, str],
        method: str,
    ) -> Any:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8"))

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        body: Any = None,
        with_token: bool = True,
    ) -> dict[str, Any]:
        query = dict(params or {})
        if with_token:
            query["access_token"] = self.access_token()
        url = self.base_url + path
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)

        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        self._semaphore.acquire()
        try:
            last_exc: BaseException | None = None
            last_kind = "unknown"
            for attempt in range(1, self.max_attempts + 1):
                self._pace()
                started = self._clock()
                self._record_request()
                try:
                    payload = self._send_http(url, data, headers, method)
                except Exception as exc:  # noqa: BLE001 - 按类型分类后重试
                    latency = max(0.0, self._clock() - started)
                    self._record_latency(latency)
                    self._note_latency(latency)
                    kind = _error_kind(exc)
                    self._record_error(kind)
                    last_exc, last_kind = exc, kind
                    if not _retryable(exc) or attempt >= self.max_attempts:
                        raise ScrmError(
                            f"请求失败 {method} {path} ({kind}): {_redact(exc)}"
                        ) from exc
                    self._record_retry()
                    delay = self._backoff_delay(attempt)
                    log.warning(
                        "SCRM 请求重试 %s %s 第 %s/%s 次，%s 后重试 (%s)",
                        method,
                        path,
                        attempt,
                        self.max_attempts,
                        f"{delay:.2f}s",
                        kind,
                    )
                    self._sleep(delay)
                    continue

                latency = max(0.0, self._clock() - started)
                self._record_latency(latency)
                self._note_latency(latency)

                if not isinstance(payload, dict):
                    self._record_error("bad_response")
                    raise ScrmError(f"响应不是 JSON 对象: {path}")
                code = payload.get("code")
                if code not in (0, "0", None):
                    self._record_error("api_code")
                    raise ScrmError(
                        f"{path} 返回 code={code} msg={_redact(payload.get('msg'))}"
                    )
                self._record_ok()
                return payload

            raise ScrmError(
                f"请求失败 {method} {path}: {last_kind}"
            ) from last_exc
        finally:
            self._semaphore.release()


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    index = max(0, min(len(values) - 1, int(-(-len(values) * ratio // 1)) - 1))
    return values[index]


def _error_kind(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
            return "timeout"
        return "url_error"
    if isinstance(exc, json.JSONDecodeError):
        return "json_decode"
    if isinstance(exc, ScrmError):
        return "scrm"
    return "other"


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= int(exc.code) < 600
    if isinstance(exc, (urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError)):
        return True
    return False


def make_throttled_client(root: str, **kwargs: Any) -> ThrottledScrmClient:
    """与 make_scrm_client 相同的环境加载/校验，但返回限流客户端。"""
    load_scrm_env(root)
    base_url = os.environ.get("SCRM_BASE_URL", "").strip()
    app_id = os.environ.get("SCRM_APP_ID", "").strip()
    app_secret = os.environ.get("SCRM_APP_SECRET", "").strip()
    if not base_url or not app_id or not app_secret:
        raise ScrmError("缺少 SCRM_BASE_URL / SCRM_APP_ID / SCRM_APP_SECRET，请配置 .env")
    return ThrottledScrmClient(base_url, app_id, app_secret, **kwargs)


# 兼容可能的调用方命名。
make_client = make_throttled_client


# ---------------------------------------------------------------------------
# 锁与数据库
# ---------------------------------------------------------------------------


@contextmanager
def sync_lock(root: str, timeout: float | None = None):
    """等待 ROOT/.sync.lock（普通 fcntl.flock，最多等 timeout 秒）。

    与 app._SYNC_LOCK 不是同一把进程内锁；这里只使用文件锁。
    """
    if timeout is None:
        timeout = SYNC_LOCK_TIMEOUT
    path = os.path.join(root, ".sync.lock")
    fh = open(path, "a+")
    deadline = time.monotonic() + max(0.0, float(timeout))
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ScrmTierError("等待 .sync.lock 超时")
                time.sleep(0.05)
        yield fh
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _lock_path(root: str, tier: str) -> str:
    return os.path.join(root, f".sync-tier-{TIER_LOCKS[tier]}.lock")


def _acquire_tier_lock(root: str, tier: str):
    fh = open(_lock_path(root, tier), "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def _release_tier_lock(fh) -> None:
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def _timeout_transaction(root: str, fn: Callable[[sqlite3.Connection], Any]) -> Any:
    """在 .sync.lock 内执行一次 timeout.db 写事务。"""
    with sync_lock(root):
        conn = timeout_store.connect(_timeout_db_path(root))
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            return result
        finally:
            conn.close()


def _store_last_run(root: str, tier: str, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        def write(conn: sqlite3.Connection) -> None:
            timeout_store.set_meta(conn, f"tier_{tier}_last", text)

        _timeout_transaction(root, write)
    except Exception as exc:  # 指标失败不应掩盖本轮同步结果
        log.warning("写入 tier_%s_last 失败: %s", tier, _redact(exc))


# ---------------------------------------------------------------------------
# 缓存库（members 层）
# ---------------------------------------------------------------------------

GROUP_MEMBER_COLUMNS = (
    "chat_id",
    "userid",
    "type",
    "join_time",
    "join_scene",
    "nick",
    "name",
    "invitor",
)
FOLLOW_COLUMNS = (
    "external_userid",
    "userid",
    "name",
    "type",
    "gender",
    "unionid",
    "corp_name",
    "remark",
    "remark_mobiles",
    "add_way",
    "add_time",
    "is_del",
    "tags",
)

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS scrm_group_member (
  chat_id TEXT NOT NULL,
  userid TEXT NOT NULL,
  type INTEGER,
  join_time INTEGER,
  join_scene INTEGER,
  nick TEXT,
  name TEXT,
  invitor TEXT,
  PRIMARY KEY (chat_id, userid)
);
CREATE TABLE IF NOT EXISTS scrm_group_member_staging (
  chat_id TEXT NOT NULL,
  userid TEXT NOT NULL,
  type INTEGER,
  join_time INTEGER,
  join_scene INTEGER,
  nick TEXT,
  name TEXT,
  invitor TEXT,
  PRIMARY KEY (chat_id, userid)
);
CREATE TABLE IF NOT EXISTS scrm_follow (
  external_userid TEXT NOT NULL,
  userid TEXT NOT NULL,
  name TEXT,
  type INTEGER,
  gender INTEGER,
  unionid TEXT,
  corp_name TEXT,
  remark TEXT,
  remark_mobiles TEXT,
  add_way INTEGER,
  add_time INTEGER,
  is_del INTEGER,
  tags TEXT,
  PRIMARY KEY (external_userid, userid)
);
CREATE TABLE IF NOT EXISTS scrm_follow_staging (
  external_userid TEXT NOT NULL,
  userid TEXT NOT NULL,
  name TEXT,
  type INTEGER,
  gender INTEGER,
  unionid TEXT,
  corp_name TEXT,
  remark TEXT,
  remark_mobiles TEXT,
  add_way INTEGER,
  add_time INTEGER,
  is_del INTEGER,
  tags TEXT,
  PRIMARY KEY (external_userid, userid)
);
"""


def _cache_connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(CACHE_SCHEMA)
    conn.commit()
    if path != ":memory:":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return conn


def _dedupe(rows: Iterable[tuple[Any, ...]], key_size: int) -> list[tuple[Any, ...]]:
    out: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    for row in rows:
        out[tuple(row[:key_size])] = row
    return list(out.values())


def _cache_replace(
    path: str,
    member_rows: Iterable[dict[str, Any]],
    follow_rows: Iterable[dict[str, Any]],
) -> None:
    """先写 staging，再在一个事务里整表替换两张主表。"""
    members = _dedupe(
        (tuple(row.get(col) for col in GROUP_MEMBER_COLUMNS) for row in member_rows),
        key_size=2,
    )
    follows = _dedupe(
        (tuple(row.get(col) for col in FOLLOW_COLUMNS) for row in follow_rows),
        key_size=2,
    )
    placeholders = ", ".join("?" for _ in GROUP_MEMBER_COLUMNS)
    follow_placeholders = ", ".join("?" for _ in FOLLOW_COLUMNS)
    member_cols = ", ".join(GROUP_MEMBER_COLUMNS)
    follow_cols = ", ".join(FOLLOW_COLUMNS)

    conn = _cache_connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM scrm_group_member_staging")
            conn.execute("DELETE FROM scrm_follow_staging")
            conn.executemany(
                f"INSERT INTO scrm_group_member_staging({member_cols}) VALUES ({placeholders})",
                members,
            )
            conn.executemany(
                f"INSERT INTO scrm_follow_staging({follow_cols}) VALUES ({follow_placeholders})",
                follows,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM scrm_group_member")
            conn.execute("DELETE FROM scrm_follow")
            conn.execute(
                f"INSERT INTO scrm_group_member({member_cols}) "
                f"SELECT {member_cols} FROM scrm_group_member_staging"
            )
            conn.execute(
                f"INSERT INTO scrm_follow({follow_cols}) "
                f"SELECT {follow_cols} FROM scrm_follow_staging"
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if path != ":memory:":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    finally:
        conn.close()


def _cache_delete_groups(path: str, chat_ids: Iterable[str]) -> int:
    ids = sorted({str(cid) for cid in chat_ids if cid})
    if not ids:
        return 0
    conn = _cache_connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            total = 0
            for chat_id in ids:
                cur = conn.execute(
                    "DELETE FROM scrm_group_member WHERE chat_id = ?", (chat_id,)
                )
                total += max(0, cur.rowcount)
            conn.commit()
            return total
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 字段转换
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        return 1 if int(value) else 0
    except (TypeError, ValueError):
        return 1 if str(value).strip().lower() in ("1", "true", "yes") else 0


def _member_row(chat_id: str, rec: dict[str, Any]) -> dict[str, Any] | None:
    userid = _text(rec.get("userid") or rec.get("user_id"))
    if not userid:
        return None
    return {
        "chat_id": chat_id,
        "userid": userid,
        "type": _int_or_none(rec.get("type")),
        "join_time": _int_or_none(rec.get("join_time")),
        "join_scene": _int_or_none(rec.get("join_scene")),
        "nick": _text(
            rec.get("user_chat_nick_name")
            or rec.get("nick")
            or rec.get("nickname")
        ),
        "name": _text(rec.get("name") or rec.get("username")),
        "invitor": _text(
            rec.get("invitor")
            or rec.get("inviter")
            or rec.get("invitor_userid")
            or rec.get("invite_userid")
        ),
    }


def _follow_row(employee_userid: str, rec: dict[str, Any]) -> dict[str, Any] | None:
    nested = rec.get("external_contact")
    if isinstance(nested, dict):
        rec = {**rec, **nested}
    external = _text(
        rec.get("external_userid")
        or rec.get("external_user_id")
        or rec.get("external_id")
        or rec.get("external_contact_id")
    )
    if not external:
        return None
    userid = _text(rec.get("userid") or rec.get("user_id") or employee_userid) or employee_userid
    return {
        "external_userid": external,
        "userid": userid,
        "name": _text(rec.get("name") or rec.get("external_name")),
        "type": _int_or_none(rec.get("type")),
        "gender": _int_or_none(rec.get("gender")),
        "unionid": _text(rec.get("unionid")),
        "corp_name": _text(rec.get("corp_name")),
        "remark": _text(rec.get("remark")),
        "remark_mobiles": _text(rec.get("remark_mobiles") or rec.get("mobiles")),
        "add_way": _int_or_none(rec.get("add_way")),
        "add_time": _int_or_none(rec.get("add_time")),
        "is_del": _bool_int(rec.get("is_del")),
        "tags": _text(rec.get("tags")),
    }


# ---------------------------------------------------------------------------
# groups 层
# ---------------------------------------------------------------------------


def _group_ids(rows: Iterable[dict[str, Any]]) -> set[str]:
    return {
        str(row.get("group_id") or "").strip()
        for row in rows
        if str(row.get("group_id") or "").strip()
    }


def _run_groups_tier(root: str, client: ScrmClient) -> dict[str, Any]:
    result = sync_from_scrm(root, client=client)
    rows = [row for row in (result.get("all_rows") or []) if row.get("group_id")]
    robot_ids = [str(cid) for cid in (result.get("robot_chat_ids") or []) if cid]
    employee_names = result.get("employee_names") or {}

    def write(conn: sqlite3.Connection) -> dict[str, Any]:
        existing = {
            row["room_id"]: row["group_name"]
            for row in conn.execute("SELECT room_id, group_name FROM chat_store")
        }
        new_groups = 0
        renamed = 0
        for row in rows:
            room_id = str(row.get("group_id") or "").strip()
            if not room_id:
                continue
            group_name = row.get("group_name") or ""
            if room_id not in existing:
                conn.execute(
                    """
                    INSERT INTO chat_store(
                      room_id, store, theater, group_name, create_time, in_robot,
                      owner_id, owner_name, owner_dept, member_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        room_id,
                        row.get("store") or "",
                        row.get("theater") or "",
                        group_name,
                        row.get("create_time") or None,
                        1 if room_id in set(robot_ids) else 0,
                        row.get("owner_id") or None,
                        row.get("owner") or None,
                        row.get("dept") or None,
                        _int_or_none(row.get("member_count")),
                    ),
                )
                new_groups += 1
                existing[room_id] = group_name
            elif (existing.get(room_id) or "") != group_name:
                conn.execute(
                    """
                    UPDATE chat_store SET
                      group_name = ?, owner_id = ?, owner_name = ?, owner_dept = ?,
                      store = ?, theater = ?, member_count = ?
                    WHERE room_id = ?
                    """,
                    (
                        group_name,
                        row.get("owner_id") or None,
                        row.get("owner") or None,
                        row.get("dept") or None,
                        row.get("store") or "",
                        row.get("theater") or "",
                        _int_or_none(row.get("member_count")),
                        room_id,
                    ),
                )
                renamed += 1
                existing[room_id] = group_name

        before = conn.total_changes
        for room_id in sorted(set(robot_ids)):
            conn.execute(
                "INSERT OR IGNORE INTO robot_chat(room_id) VALUES (?)", (room_id,)
            )
        robot_added = conn.total_changes - before

        before = conn.total_changes
        for userid, name in sorted(employee_names.items()):
            userid = str(userid or "").strip()
            if not userid:
                continue
            conn.execute(
                """
                INSERT INTO employee(userid, name) VALUES (?, ?)
                ON CONFLICT(userid) DO UPDATE SET name = excluded.name
                WHERE excluded.name IS NOT NULL AND excluded.name <> ''
                """,
                (userid, (name or "").strip() or None),
            )
        employee_changes = conn.total_changes - before
        return {
            "groups": len(rows),
            "new_groups": new_groups,
            "renamed_groups": renamed,
            "robot_rooms_added": robot_added,
            "employee_changes": employee_changes,
        }

    counts = _timeout_transaction(root, write)
    log.info(
        "groups 层完成 new=%s renamed=%s robot_added=%s",
        counts["new_groups"],
        counts["renamed_groups"],
        counts["robot_rooms_added"],
    )
    return counts


# ---------------------------------------------------------------------------
# members 层
# ---------------------------------------------------------------------------


def _fetch_member_directory(
    client: ScrmClient,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    # members 层的主数据是群列表；员工/部门只用于刷新归属，取不到时沿用旧归属。
    try:
        users = client.list_users()
    except Exception as exc:  # noqa: BLE001 - 目录失败不阻塞成员/客户缓存
        log.warning("members 层员工目录不可用，沿用旧归属: %s", _redact(exc))
        users = []
    try:
        departments = client.list_departments()
    except Exception as exc:  # noqa: BLE001
        log.warning("members 层部门目录不可用，沿用旧归属: %s", _redact(exc))
        departments = []
    groups = client.list_groups()
    user_by_id = {_as_str_id(u.get("userId") or u.get("userid")): u for u in users}
    dept_by_id: dict[int, dict[str, Any]] = {}
    for dept in departments:
        try:
            dept_by_id[int(dept["id"])] = dept
        except (KeyError, TypeError, ValueError):
            continue
    rows = [_group_to_row(group, user_by_id, dept_by_id) for group in groups]
    return rows, users, departments


def _run_members_tier(root: str, client: ScrmClient) -> dict[str, Any]:
    rows, users, departments = _fetch_member_directory(client)
    rows = [row for row in rows if row.get("group_id")]
    has_users = bool(users)
    has_departments = bool(departments)

    def update_groups(conn: sqlite3.Connection) -> tuple[int, list[str], list[str]]:
        existing = {
            row["room_id"]
            for row in conn.execute("SELECT room_id FROM chat_store")
        }
        updated = 0
        for row in rows:
            room_id = str(row.get("group_id") or "").strip()
            if not room_id or room_id not in existing:
                continue
            if has_users and has_departments:
                conn.execute(
                    """
                    UPDATE chat_store SET
                      owner_id = ?, owner_name = ?, owner_dept = ?,
                      store = ?, theater = ?, member_count = ?
                    WHERE room_id = ?
                    """,
                    (
                        row.get("owner_id") or None,
                        row.get("owner") or None,
                        row.get("dept") or None,
                        row.get("store") or "",
                        row.get("theater") or "",
                        _int_or_none(row.get("member_count")),
                        room_id,
                    ),
                )
            elif has_users:
                conn.execute(
                    """
                    UPDATE chat_store SET
                      owner_id = ?, owner_name = ?, member_count = ?
                    WHERE room_id = ?
                    """,
                    (
                        row.get("owner_id") or None,
                        row.get("owner") or None,
                        _int_or_none(row.get("member_count")),
                        room_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE chat_store SET owner_id = ?, member_count = ? "
                    "WHERE room_id = ?",
                    (
                        row.get("owner_id") or None,
                        _int_or_none(row.get("member_count")),
                        room_id,
                    ),
                )
            updated += 1
        employees = [
            row["userid"]
            for row in conn.execute("SELECT userid FROM employee ORDER BY userid")
        ]
        return updated, sorted(existing), employees

    updated, _existing_rooms, employee_ids = _timeout_transaction(root, update_groups)

    group_ids = sorted(_group_ids(rows))
    user_ids = sorted(
        {_as_str_id(u.get("userId") or u.get("userid")) for u in users}
        | {str(uid) for uid in employee_ids}
    )
    user_ids = [uid for uid in user_ids if uid]
    limit = _tier_limit()
    if limit:
        group_ids = group_ids[:limit]
        user_ids = user_ids[:limit]

    member_rows: list[dict[str, Any]] = []
    follow_rows: list[dict[str, Any]] = []
    errors = 0
    units = 0

    for chat_id in group_ids:
        units += 1
        try:
            records = client.paginate_get(
                "/openapi/chat/group/member/list",
                extra_params={"chat_id": chat_id},
            )
            for rec in records or []:
                item = _member_row(chat_id, rec)
                if item:
                    member_rows.append(item)
        except Exception as exc:  # noqa: BLE001 - 单个群失败不拖垮整层
            errors += 1
            log.warning(
                "members 层拉取群成员失败 chat_id=%s: %s", chat_id, _redact(exc)
            )

    for userid in user_ids:
        units += 1
        try:
            records = client.paginate_post(
                "/openapi/user/customer/list",
                body_base={"user_id": userid},
            )
            for rec in records or []:
                item = _follow_row(userid, rec)
                if item:
                    follow_rows.append(item)
        except Exception as exc:  # noqa: BLE001 - 单个员工失败不拖垮整层
            errors += 1
            log.warning(
                "members 层拉取客户失败 userid=%s: %s", userid, _redact(exc)
            )

    error_rate = (errors / units) if units else 0.0
    if error_rate > 0.05:
        raise ScrmTierError(
            f"members 层错误率 {error_rate:.2%} 超过 5%，保留旧缓存"
            f"（errors={errors}, units={units}）"
        )

    _cache_replace(_cache_db_path(), member_rows, follow_rows)
    counts = {
        "groups": len(group_ids),
        "employees": len(user_ids),
        "groups_updated": updated,
        "members": len(member_rows),
        "follows": len(follow_rows),
        "errors": errors,
        "units": units,
        "error_rate": round(error_rate, 4),
    }
    log.info(
        "members 层完成 groups=%s employees=%s members=%s follows=%s errors=%s",
        counts["groups"],
        counts["employees"],
        counts["members"],
        counts["follows"],
        counts["errors"],
    )
    return counts


# ---------------------------------------------------------------------------
# full 层
# ---------------------------------------------------------------------------


def _run_full_tier(root: str, client: ScrmClient) -> dict[str, Any]:
    result = sync_from_scrm(root, client=client)
    rows = [row for row in (result.get("all_rows") or []) if row.get("group_id")]
    result_ids = _group_ids(rows)

    def write(conn: sqlite3.Connection) -> dict[str, Any]:
        before = {
            row["room_id"]
            for row in conn.execute("SELECT room_id FROM chat_store")
        }
        dissolved = sorted(before - result_ids)
        added = sorted(result_ids - before)
        timeout_store.replace_context(
            conn,
            robot_chat_ids=result.get("robot_chat_ids") or [],
            employee_ids=result.get("employee_ids") or [],
            group_rows=rows,
            employee_names=result.get("employee_names") or {},
        )
        return {
            "groups": len(rows),
            "dissolved": len(dissolved),
            "new_groups": len(added),
            "dissolved_ids": dissolved,
        }

    counts = _timeout_transaction(root, write)
    dissolved = counts.pop("dissolved_ids", [])
    deleted = 0
    if dissolved:
        try:
            deleted = _cache_delete_groups(_cache_db_path(), dissolved)
        except Exception as exc:  # noqa: BLE001 - 缓存清理不掩盖主同步
            log.warning("full 层清理旧群缓存失败: %s", _redact(exc))
    counts["cache_members_deleted"] = deleted
    log.info(
        "full 层完成 groups=%s new=%s dissolved=%s cache_deleted=%s",
        counts["groups"],
        counts["new_groups"],
        counts["dissolved"],
        counts["cache_members_deleted"],
    )
    return counts


# ---------------------------------------------------------------------------
# 运行入口与指标
# ---------------------------------------------------------------------------


def _empty_client_metrics() -> dict[str, Any]:
    return {
        "requests": 0,
        "ok": 0,
        "errors": {},
        "error_kinds": {},
        "retries": 0,
        "p50_ms": 0.0,
        "p95_ms": 0.0,
        "latency_ms": {"p50": 0.0, "p95": 0.0},
        "seconds": 0.0,
    }


def _client_metrics(client: Any) -> dict[str, Any]:
    if client is None:
        return _empty_client_metrics()
    value = getattr(client, "metrics", None)
    if isinstance(value, dict):
        return dict(value)
    if not callable(value):
        return _empty_client_metrics()
    try:
        return value()
    except Exception:
        return _empty_client_metrics()


def _metrics_line(
    tier: str,
    start: str,
    end: str,
    status: str,
    counts: dict[str, Any],
    client: Any,
    seconds: float,
    error: BaseException | None = None,
) -> dict[str, Any]:
    request_metrics = _client_metrics(client)
    run_seconds = round(max(0.0, seconds), 3)
    line: dict[str, Any] = {
        "tier": tier,
        "start": start,
        "end": end,
        "status": status,
        "counts": counts,
    }
    line.update(request_metrics)
    line["seconds"] = run_seconds
    line["request_seconds"] = request_metrics.get("seconds", 0.0)
    line["request_metrics"] = request_metrics
    if error is not None:
        line["error"] = type(error).__name__
    return line


def _append_metrics(line: dict[str, Any]) -> None:
    path = _metrics_path()
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:  # 指标写失败不影响同步
        log.warning("写入 tier 指标失败: %s", _redact(exc))


def run_tier(
    tier: str,
    root: str | None = None,
    client: ScrmClient | None = None,
) -> dict[str, Any]:
    """运行一个 tier；tier 锁被占用时返回 status=skip（退出码 0）。

    真正的同步失败会先写指标再抛出，由 CLI 转成非 0 退出码。
    """
    if tier not in TIERS:
        raise ValueError(f"未知 tier: {tier}")
    root = os.path.abspath(root or ROOT)
    start = _now_iso()
    started_at = time.monotonic()
    tier_lock = _acquire_tier_lock(root, tier)
    if tier_lock is None:
        log.info("skip: previous run still going")
        line = _metrics_line(
            tier,
            start,
            _now_iso(),
            "skip",
            {},
            None,
            time.monotonic() - started_at,
        )
        _append_metrics(line)
        return {
            "tier": tier,
            "status": "skip",
            "counts": {},
            "metrics": line["request_metrics"],
            "start": line["start"],
            "end": line["end"],
        }

    try:
        if client is None:
            client = make_throttled_client(root)
        if tier == "groups":
            counts = _run_groups_tier(root, client)
        elif tier == "members":
            counts = _run_members_tier(root, client)
        else:
            counts = _run_full_tier(root, client)

        end = _now_iso()
        line = _metrics_line(
            tier,
            start,
            end,
            "success",
            counts,
            client,
            time.monotonic() - started_at,
        )
        _append_metrics(line)
        _store_last_run(root, tier, line)
        return {
            "tier": tier,
            "status": "success",
            "counts": counts,
            "metrics": line["request_metrics"],
            "start": line["start"],
            "end": line["end"],
        }
    except BaseException as exc:
        end = _now_iso()
        line = _metrics_line(
            tier,
            start,
            end,
            "error",
            {},
            client,
            time.monotonic() - started_at,
            error=exc,
        )
        _append_metrics(line)
        log.error("%s 层同步失败: %s", tier, _redact(exc))
        raise
    finally:
        _release_tier_lock(tier_lock)


def run_groups(root: str | None = None, client: ScrmClient | None = None) -> dict[str, Any]:
    return run_tier("groups", root=root, client=client)


def run_members(root: str | None = None, client: ScrmClient | None = None) -> dict[str, Any]:
    return run_tier("members", root=root, client=client)


def run_full(root: str | None = None, client: ScrmClient | None = None) -> dict[str, Any]:
    return run_tier("full", root=root, client=client)


# 兼容更显式的命名。
run_groups_tier = run_groups
run_members_tier = run_members
run_full_tier = run_full
