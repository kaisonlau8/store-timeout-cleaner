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
DEFAULT_MAX_SECONDS = {"groups": 900.0, "full": 900.0, "members": 10800.0}
DEFAULT_FULL_MIN_RATIO = 0.95

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
# 运行中的请求/写库只避让原任务的 00:00 / 12:00；启动时还避让 08:30 / 09:00。
RUNTIME_BLACKOUT_TIMES = ("00:00", "12:00")


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


def _tier_max_seconds(tier: str) -> float:
    default = DEFAULT_MAX_SECONDS.get(tier, 900.0)
    raw = os.environ.get("SCRM_TIER_MAX_SECONDS", "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


def _full_min_ratio() -> float:
    value = _env_float("SCRM_FULL_MIN_RATIO", DEFAULT_FULL_MIN_RATIO)
    return max(0.0, min(1.0, value))


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


def _shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def _now_iso() -> str:
    return _shanghai_now().isoformat(timespec="seconds")


_SECRET_KEY_PATTERN = (
    r"[A-Za-z0-9_.-]*(?:token|secret)[A-Za-z0-9_.-]*"
    r"|app_?id|app_?secret|password"
)
_JSON_SECRET_RE = re.compile(
    rf'(?i)(["\'](?:{_SECRET_KEY_PATTERN})["\']\s*:\s*)'
    r'(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|null|true|false|-?\d+(?:\.\d+)?)'
)
_KV_SECRET_RE = re.compile(
    rf"(?i)((?:^|(?<=[^A-Za-z0-9_.-]))(?:{_SECRET_KEY_PATTERN})\s*=\s*)"
    r'(?:"[^"]*"|\'[^\']*\'|[^&\s,;]+)'
)


def _redact(text: Any) -> str:
    """日志/异常里的凭证脱敏；覆盖 key=value 与 JSON 两种形式。"""
    value = str(text or "")
    value = _JSON_SECRET_RE.sub(lambda match: match.group(1) + '"***"', value)
    value = _KV_SECRET_RE.sub(lambda match: match.group(1) + "***", value)
    return value


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


def _runtime_blackout_remaining(now: datetime | None = None) -> float:
    """运行中黑名单剩余秒数；仅 00:00 / 12:00 前后 5 分钟。"""
    current = now or _shanghai_now()
    seconds = (
        current.hour * 3600
        + current.minute * 60
        + current.second
        + current.microsecond / 1_000_000.0
    )
    window = BLACKOUT_MINUTES * 60
    remaining = 0.0
    for center in RUNTIME_BLACKOUT_TIMES:
        center_seconds = _hm_to_minutes(center) * 60
        diff = abs(seconds - center_seconds)
        diff = min(diff, 86400.0 - diff)
        if diff <= window:
            remaining = max(remaining, window - diff + 0.001)
    return remaining


def _check_deadline(deadline: float | None, label: str = "") -> None:
    if deadline is None:
        return
    if time.monotonic() >= deadline:
        prefix = f"{label} " if label else ""
        raise ScrmTierError(
            f"{prefix}超过最大运行时间（SCRM_TIER_MAX_SECONDS），已中止且未发布"
        )


def _wait_runtime_blackout(
    deadline: float | None = None,
    *,
    label: str = "",
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> None:
    """在 00:00/12:00 前后 5 分钟主动等待，绝不占用 .sync.lock。"""
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    prefix = f"{label} " if label else ""
    while True:
        delay = _runtime_blackout_remaining()
        if delay <= 0:
            return
        if deadline is not None and clock() >= deadline:
            raise ScrmTierError(
                f"{prefix}超过最大运行时间（SCRM_TIER_MAX_SECONDS），已中止且未发布"
            )
        if deadline is not None and clock() + delay >= deadline:
            raise ScrmTierError(
                f"{prefix}等待运行时黑名单将超过最大运行时间，已中止且未发布"
            )
        log.info(
            "SCRM 运行时黑名单（00:00/12:00 ±%s 分钟），等待 %.1fs",
            BLACKOUT_MINUTES,
            delay,
        )
        sleep(delay)


def _in_start_blackout(now: datetime | None = None) -> bool:
    return not blackout_ok(now or _shanghai_now())


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
        "max_seconds": _tier_max_seconds(tier),
        "full_min_ratio": _full_min_ratio(),
    }


# ---------------------------------------------------------------------------
# 限流客户端
# ---------------------------------------------------------------------------


def _net_lock_paths(root: str | None, concurrency: int) -> list[str]:
    """跨进程节流槽位文件；并发 1 用单文件，并发 2 用两个槽位。"""
    if not root:
        return []
    if int(concurrency) > 1:
        return [
            os.path.join(root, f".scrm-net.{index}.lock")
            for index in range(min(HARD_MAX_CONCURRENCY, int(concurrency)))
        ]
    return [os.path.join(root, ".scrm-net.lock")]


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
        wall_clock: Callable[[], float] | None = None,
        root: str | None = None,
        deadline: float | None = None,
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
        self._wall_clock = wall_clock or time.time
        self._root = os.path.abspath(root) if root else None
        self._deadline = deadline
        self._net_paths = _net_lock_paths(self._root, self.concurrency) if self._root else []

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

    def set_deadline(self, deadline: float | None) -> None:
        self._deadline = deadline

    def _check_deadline(self) -> None:
        if self._deadline is not None and self._clock() >= self._deadline:
            raise ScrmTierError(
                "SCRM 请求 超过最大运行时间（SCRM_TIER_MAX_SECONDS），已中止且未发布"
            )

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
    def _pace(self, *, include_interval: bool = True) -> None:
        with self._pace_lock:
            now = self._clock()
            base = self.min_interval if include_interval else 0.0
            jitter = self._rng.uniform(0.0, base * self.jitter_ratio) if base else 0.0
            earliest = now
            if self._last_start is not None:
                earliest = max(earliest, self._last_start + base + jitter)
            if self._cooldown_until > earliest:
                earliest = self._cooldown_until
            delay = max(0.0, earliest - now)
            self._last_start = earliest
        if delay > 0:
            if self._deadline is not None and now + delay >= self._deadline:
                raise ScrmTierError(
                    "SCRM 请求 等待节流将超过最大运行时间，已中止且未发布"
                )
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

    def _retry_delay(self, attempt: int, exc: BaseException) -> float:
        """429/503 优先使用 Retry-After（秒），最多 120 秒。"""
        if isinstance(exc, urllib.error.HTTPError) and int(exc.code) in (429, 503):
            headers = getattr(exc, "headers", None)
            raw = headers.get("Retry-After") if headers is not None else None
            if raw in (None, "") and headers is not None:
                raw = headers.get("retry-after")
            if raw not in (None, ""):
                try:
                    seconds = float(str(raw).strip())
                except (TypeError, ValueError):
                    seconds = None
                if seconds is not None:
                    return min(120.0, max(0.0, seconds))
        return self._backoff_delay(attempt)

    # -- 跨进程网络槽位（flock + 最近一次请求起始时间） ---------------------
    @contextmanager
    def _net_slot(self):
        if not self._net_paths:
            yield None
            return
        os.makedirs(self._root or ".", exist_ok=True)
        while True:
            self._check_deadline()
            for path in self._net_paths:
                fh = open(path, "a+", encoding="utf-8")
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    fh.close()
                    continue
                try:
                    yield fh
                finally:
                    try:
                        fcntl.flock(fh, fcntl.LOCK_UN)
                    finally:
                        fh.close()
                return
            if self._deadline is not None:
                self._check_deadline()
            self._sleep(0.05)

    def _wait_net_turn(self, fh) -> None:
        if fh is None:
            return
        fh.seek(0)
        raw = fh.read().strip()
        try:
            last_start = float(raw) if raw else 0.0
        except (TypeError, ValueError):
            last_start = 0.0
        now = self._wall_clock()
        jitter = self._rng.uniform(0.0, self.min_interval * self.jitter_ratio)
        target = last_start + self.min_interval + jitter
        delay = max(0.0, target - now)
        if delay > 0:
            if (
                self._deadline is not None
                and self._clock() + delay >= self._deadline
            ):
                raise ScrmTierError(
                    "SCRM 请求 等待跨进程节流将超过最大运行时间，已中止且未发布"
                )
            self._sleep(delay)

    def _mark_net_start(self, fh) -> None:
        if fh is None:
            return
        fh.seek(0)
        fh.truncate()
        fh.write(f"{self._wall_clock():.6f}")
        fh.flush()

    @contextmanager
    def _attempt_slot(self):
        """等待黑名单/节流后占用网络槽位，yield 本次请求的 started 时间。"""
        self._check_deadline()
        _wait_runtime_blackout(
            self._deadline,
            label="SCRM 请求",
            sleep=self._sleep,
            clock=self._clock,
        )
        self._pace(include_interval=not self._net_paths)
        self._check_deadline()
        with self._net_slot() as fh:
            if fh is not None:
                self._wait_net_turn(fh)
                # 等待跨进程槽位期间可能进入黑名单。
                _wait_runtime_blackout(
                    self._deadline,
                    label="SCRM 请求",
                    sleep=self._sleep,
                    clock=self._clock,
                )
            self._check_deadline()
            started = self._clock()
            self._record_request()
            self._mark_net_start(fh)
            yield started

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
                started: float | None = None
                try:
                    with self._attempt_slot() as started:
                        payload = self._send_http(url, data, headers, method)
                except ScrmTierError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 按类型分类后重试
                    latency = (
                        max(0.0, self._clock() - started)
                        if started is not None
                        else 0.0
                    )
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
                    delay = self._retry_delay(attempt, exc)
                    log.warning(
                        "SCRM 请求重试 %s %s 第 %s/%s 次，%s 后重试 (%s)",
                        method,
                        path,
                        attempt,
                        self.max_attempts,
                        f"{delay:.2f}s",
                        kind,
                    )
                    if (
                        self._deadline is not None
                        and self._clock() + delay >= self._deadline
                    ):
                        raise ScrmTierError(
                            "SCRM 请求 重试等待将超过最大运行时间，已中止且未发布"
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
    kwargs.setdefault("root", root)
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


def _timeout_transaction(
    root: str,
    fn: Callable[[sqlite3.Connection], Any],
    *,
    deadline: float | None = None,
    label: str = "",
) -> Any:
    """在 .sync.lock 内执行一次 timeout.db 写事务。

    00:00 / 12:00 前后 5 分钟先等待，且等待期间绝不持有 .sync.lock，
    保证原任务的非阻塞取锁永远看到空闲。
    """
    _check_deadline(deadline, label)
    _wait_runtime_blackout(deadline, label=label)
    _check_deadline(deadline, label)
    with sync_lock(root):
        _check_deadline(deadline, label)
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


def _read_chat_store_ids(root: str) -> set[str]:
    """短读 chat_store（不取 .sync.lock），用于发布时的竞态过滤。"""
    conn = timeout_store.connect(_timeout_db_path(root))
    try:
        return {
            str(row["room_id"])
            for row in conn.execute("SELECT room_id FROM chat_store")
            if row["room_id"]
        }
    finally:
        conn.close()


def _read_employee_ids(root: str) -> list[str]:
    conn = timeout_store.connect(_timeout_db_path(root))
    try:
        return [
            str(row["userid"])
            for row in conn.execute("SELECT userid FROM employee ORDER BY userid")
            if row["userid"]
        ]
    finally:
        conn.close()


def _read_chat_snapshot(root: str) -> dict[str, Any]:
    """短读 chat_store / robot_chat 的当前规模。"""
    conn = timeout_store.connect(_timeout_db_path(root))
    try:
        chat_rows = int(conn.execute("SELECT COUNT(*) FROM chat_store").fetchone()[0])
        robot_rows = [
            str(row["room_id"])
            for row in conn.execute("SELECT room_id FROM robot_chat")
            if row["room_id"]
        ]
        return {"chat_rows": chat_rows, "robot_rows": robot_rows}
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
    *,
    member_units: Iterable[str] | None = None,
    follow_units: Iterable[str] | None = None,
) -> None:
    """先写 staging，再在一个事务里替换主表。

    member_units / follow_units 为 None 时整表替换；给出时只替换这些 unit，
    其余 unit 的行保持不变（SCRM_TIER_LIMIT 的增量模式）。
    """
    members = _dedupe(
        (tuple(row.get(col) for col in GROUP_MEMBER_COLUMNS) for row in member_rows),
        key_size=2,
    )
    follows = _dedupe(
        (tuple(row.get(col) for col in FOLLOW_COLUMNS) for row in follow_rows),
        key_size=2,
    )
    member_unit_ids = (
        None if member_units is None else sorted({str(unit) for unit in member_units if unit})
    )
    follow_unit_ids = (
        None if follow_units is None else sorted({str(unit) for unit in follow_units if unit})
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
            if member_unit_ids is None:
                conn.execute("DELETE FROM scrm_group_member")
            else:
                conn.executemany(
                    "DELETE FROM scrm_group_member WHERE chat_id = ?",
                    [(unit,) for unit in member_unit_ids],
                )
            if follow_unit_ids is None:
                conn.execute("DELETE FROM scrm_follow")
            else:
                conn.executemany(
                    "DELETE FROM scrm_follow WHERE userid = ?",
                    [(unit,) for unit in follow_unit_ids],
                )
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


def _cache_live_counts(
    path: str,
) -> tuple[dict[str, int], dict[str, int], int, int]:
    """读取当前缓存里每个 unit 的旧行数，供失败加权/失败沿用判断。"""
    if path != ":memory:" and not os.path.exists(path):
        return {}, {}, 0, 0
    conn = _cache_connect(path)
    try:
        member_counts = {
            str(row["chat_id"]): int(row["n"])
            for row in conn.execute(
                "SELECT chat_id, COUNT(*) AS n FROM scrm_group_member GROUP BY chat_id"
            )
        }
        follow_counts = {
            str(row["userid"]): int(row["n"])
            for row in conn.execute(
                "SELECT userid, COUNT(*) AS n FROM scrm_follow GROUP BY userid"
            )
        }
        member_total = int(
            conn.execute("SELECT COUNT(*) FROM scrm_group_member").fetchone()[0]
        )
        follow_total = int(
            conn.execute("SELECT COUNT(*) FROM scrm_follow").fetchone()[0]
        )
        return member_counts, follow_counts, member_total, follow_total
    finally:
        conn.close()


def _cache_live_rows(path: str, table: str, key_col: str, value: str) -> list[dict[str, Any]]:
    """读取某个失败 unit 在旧缓存里的行，原样回填 staging。"""
    if table not in ("scrm_group_member", "scrm_follow"):
        raise ValueError(f"非法缓存表: {table}")
    if key_col not in ("chat_id", "userid"):
        raise ValueError(f"非法缓存键: {key_col}")
    if path != ":memory:" and not os.path.exists(path):
        return []
    conn = _cache_connect(path)
    try:
        return [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM {table} WHERE {key_col} = ?", (value,)
            )
        ]
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


def _run_groups_tier(
    root: str,
    client: ScrmClient,
    deadline: float | None = None,
) -> dict[str, Any]:
    result = sync_from_scrm(root, client=client)
    _check_deadline(deadline, "groups 层")
    rows = [row for row in (result.get("all_rows") or []) if row.get("group_id")]
    robot_ids = [str(cid) for cid in (result.get("robot_chat_ids") or []) if cid]
    robot_set = set(robot_ids)
    employee_names = result.get("employee_names") or {}

    def write(conn: sqlite3.Connection) -> dict[str, Any]:
        existing = {
            row["room_id"]: dict(row)
            for row in conn.execute(
                "SELECT room_id, store, theater, group_name, create_time, in_robot, "
                "owner_id, owner_name, owner_dept, member_count FROM chat_store"
            )
        }
        new_groups = 0
        renamed = 0
        updated = 0
        for row in rows:
            room_id = str(row.get("group_id") or "").strip()
            if not room_id:
                continue
            group_name = row.get("group_name") or ""
            desired = {
                "group_name": group_name,
                "owner_id": row.get("owner_id") or None,
                "owner_name": row.get("owner") or None,
                "owner_dept": row.get("dept") or None,
                "store": row.get("store") or "",
                "theater": row.get("theater") or "",
                "member_count": _int_or_none(row.get("member_count")),
            }
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
                        desired["store"],
                        desired["theater"],
                        group_name,
                        row.get("create_time") or None,
                        1 if room_id in robot_set else 0,
                        desired["owner_id"],
                        desired["owner_name"],
                        desired["owner_dept"],
                        desired["member_count"],
                    ),
                )
                new_groups += 1
                existing[room_id] = {**desired, "in_robot": 1 if room_id in robot_set else 0}
                continue

            old = existing[room_id]
            changed = False
            for key, value in desired.items():
                old_value = old.get(key)
                if key in ("owner_id", "owner_name", "owner_dept"):
                    old_value = old_value or None
                elif key in ("store", "theater", "group_name"):
                    old_value = old_value or ""
                if old_value != value:
                    changed = True
                    break
            if not changed:
                continue

            conn.execute(
                """
                UPDATE chat_store SET
                  group_name = ?, owner_id = ?, owner_name = ?, owner_dept = ?,
                  store = ?, theater = ?, member_count = ?
                WHERE room_id = ?
                """,
                (
                    group_name,
                    desired["owner_id"],
                    desired["owner_name"],
                    desired["owner_dept"],
                    desired["store"],
                    desired["theater"],
                    desired["member_count"],
                    room_id,
                ),
            )
            updated += 1
            if (old.get("group_name") or "") != group_name:
                renamed += 1
            existing[room_id] = {**old, **desired}

        in_robot_updated = 0
        robot_added = 0
        if robot_set:
            before = conn.total_changes
            conn.executemany(
                "UPDATE chat_store SET in_robot = 1 WHERE room_id = ? AND in_robot <> 1",
                [(room_id,) for room_id in sorted(robot_set)],
            )
            placeholders = ", ".join("?" for _ in robot_set)
            conn.execute(
                f"UPDATE chat_store SET in_robot = 0 "
                f"WHERE in_robot <> 0 AND room_id NOT IN ({placeholders})",
                tuple(sorted(robot_set)),
            )
            in_robot_updated = conn.total_changes - before

            before = conn.total_changes
            for room_id in sorted(robot_set):
                conn.execute(
                    "INSERT OR IGNORE INTO robot_chat(room_id) VALUES (?)", (room_id,)
                )
            robot_added = conn.total_changes - before
        else:
            log.warning("groups 层机器人群列表为空，跳过机器人相关更新")

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
            "updated_groups": updated,
            "groups_updated": updated,
            "in_robot_updated": in_robot_updated,
            "robot_rooms_added": robot_added,
            "employee_changes": employee_changes,
        }

    counts = _timeout_transaction(
        root, write, deadline=deadline, label="groups 层"
    )
    log.info(
        "groups 层完成 new=%s updated=%s renamed=%s in_robot_updated=%s robot_added=%s",
        counts["new_groups"],
        counts["updated_groups"],
        counts["renamed_groups"],
        counts["in_robot_updated"],
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
    except ScrmTierError:
        raise
    except Exception as exc:  # noqa: BLE001 - 目录失败不阻塞成员/客户缓存
        log.warning("members 层员工目录不可用，沿用旧归属: %s", _redact(exc))
        users = []
    try:
        departments = client.list_departments()
    except ScrmTierError:
        raise
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


def _run_members_tier(
    root: str,
    client: ScrmClient,
    deadline: float | None = None,
) -> dict[str, Any]:
    _check_deadline(deadline, "members 层")
    rows, users, departments = _fetch_member_directory(client)
    _check_deadline(deadline, "members 层")
    rows = [row for row in rows if row.get("group_id")]
    has_users = bool(users)
    has_departments = bool(departments)

    employee_ids = _read_employee_ids(root)
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

    cache_path = _cache_db_path()
    (
        previous_member_counts,
        previous_follow_counts,
        previous_member_total,
        previous_follow_total,
    ) = _cache_live_counts(cache_path)

    member_rows: list[dict[str, Any]] = []
    follow_rows: list[dict[str, Any]] = []
    errors = 0
    units = 0
    failed_member_rows = 0
    failed_follow_rows = 0

    for chat_id in group_ids:
        _check_deadline(deadline, "members 层")
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
        except ScrmTierError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单个群失败不拖垮整层
            errors += 1
            # 失败 unit 沿用旧缓存，绝不让 staging 丢掉上一轮的数据。
            previous = _cache_live_rows(
                cache_path, "scrm_group_member", "chat_id", chat_id
            )
            member_rows.extend(previous)
            failed_member_rows += len(previous)
            log.warning(
                "members 层拉取群成员失败 chat_id=%s（沿用旧数据 %s 行）: %s",
                chat_id,
                len(previous),
                _redact(exc),
            )

    for userid in user_ids:
        _check_deadline(deadline, "members 层")
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
        except ScrmTierError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单个员工失败不拖垮整层
            errors += 1
            previous = _cache_live_rows(cache_path, "scrm_follow", "userid", userid)
            follow_rows.extend(previous)
            failed_follow_rows += len(previous)
            log.warning(
                "members 层拉取客户失败 userid=%s（沿用旧数据 %s 行）: %s",
                userid,
                len(previous),
                _redact(exc),
            )

    error_rate = (errors / units) if units else 0.0
    previous_total = previous_member_total + previous_follow_total
    failed_previous_rows = failed_member_rows + failed_follow_rows
    row_error_rate = (
        (failed_previous_rows / previous_total) if previous_total else 0.0
    )
    member_row_rate = (
        (failed_member_rows / previous_member_total)
        if previous_member_total
        else 0.0
    )
    follow_row_rate = (
        (failed_follow_rows / previous_follow_total)
        if previous_follow_total
        else 0.0
    )
    if (
        error_rate > 0.05
        or row_error_rate > 0.05
        or member_row_rate > 0.05
        or follow_row_rate > 0.05
    ):
        raise ScrmTierError(
            f"members 层错误率超过 5%，保留旧缓存"
            f"（units={error_rate:.2%}, old_rows={row_error_rate:.2%}, "
            f"member_rows={member_row_rate:.2%}, follows={follow_row_rate:.2%}, "
            f"errors={errors}, units={units}, "
            f"failed_old_rows={failed_previous_rows}, old_rows_total={previous_total}）"
        )

    _check_deadline(deadline, "members 层")
    # 与 full 层的竞态：发布时 chat_store 已不存在的群不再写成员缓存。
    chat_ids = _read_chat_store_ids(root)
    member_rows = [
        item for item in member_rows if str(item.get("chat_id") or "") in chat_ids
    ]
    _check_deadline(deadline, "members 层")

    def update_groups(conn: sqlite3.Connection) -> int:
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
        return updated

    updated = _timeout_transaction(
        root, update_groups, deadline=deadline, label="members 层"
    )
    # 进入发布阶段后不再因 deadline 中断，避免只更新 chat_store 而没更新缓存。
    _cache_replace(
        cache_path,
        member_rows,
        follow_rows,
        member_units=set(group_ids) if limit else None,
        follow_units=set(user_ids) if limit else None,
    )
    counts = {
        "groups": len(group_ids),
        "employees": len(user_ids),
        "groups_updated": updated,
        "members": len(member_rows),
        "follows": len(follow_rows),
        "errors": errors,
        "units": units,
        "error_rate": round(error_rate, 4),
        "row_error_rate": round(row_error_rate, 4),
        "member_row_error_rate": round(member_row_rate, 4),
        "follow_row_error_rate": round(follow_row_rate, 4),
        "failed_old_rows": failed_previous_rows,
        "old_rows_total": previous_total,
    }
    log.info(
        "members 层完成 groups=%s employees=%s members=%s follows=%s "
        "errors=%s units=%s row_error_rate=%.2f%%",
        counts["groups"],
        counts["employees"],
        counts["members"],
        counts["follows"],
        counts["errors"],
        counts["units"],
        row_error_rate * 100.0,
    )
    return counts


# ---------------------------------------------------------------------------
# full 层
# ---------------------------------------------------------------------------


def _full_completeness_error(
    rows: list[dict[str, Any]],
    robot_ids: list[str],
    snapshot: dict[str, Any],
) -> str | None:
    if not rows:
        return "全量群列表为空"
    chat_rows = int(snapshot.get("chat_rows") or 0)
    ratio = _full_min_ratio()
    if chat_rows and len(rows) < ratio * chat_rows:
        return (
            f"全量群数量 {len(rows)} 低于当前 chat_store {chat_rows} 行的 "
            f"{ratio:.0%}（SCRM_FULL_MIN_RATIO）"
        )
    old_robot_rows = snapshot.get("robot_rows") or []
    if not robot_ids and old_robot_rows:
        return (
            f"机器人群列表为空，但 robot_chat 仍有 {len(old_robot_rows)} 行"
        )
    return None


def _run_full_tier(
    root: str,
    client: ScrmClient,
    deadline: float | None = None,
) -> dict[str, Any]:
    result = sync_from_scrm(root, client=client)
    _check_deadline(deadline, "full 层")
    rows = [row for row in (result.get("all_rows") or []) if row.get("group_id")]
    result_ids = _group_ids(rows)
    robot_ids = [
        str(cid) for cid in (result.get("robot_chat_ids") or []) if cid
    ]

    snapshot = _read_chat_snapshot(root)
    guard_error = _full_completeness_error(rows, robot_ids, snapshot)
    if guard_error:
        raise ScrmTierError(f"full 层完整性保护中止（未写入）：{guard_error}")

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

    _check_deadline(deadline, "full 层")
    counts = _timeout_transaction(root, write, deadline=deadline, label="full 层")
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
    force: bool = False,
) -> dict[str, Any]:
    """运行一个 tier；tier 锁被占用或处于启动黑名单时返回 status=skip。

    ``force=True`` 仅跳过启动黑名单检查，运行中的 00:00/12:00 避让仍然生效。
    真正的同步失败会先写指标再抛出，由 CLI 转成非 0 退出码。
    """
    if tier not in TIERS:
        raise ValueError(f"未知 tier: {tier}")
    root = os.path.abspath(root or ROOT)
    start = _now_iso()
    started_at = time.monotonic()

    if not force and _in_start_blackout():
        log.info(
            "skip: %s 处于启动黑名单（00:00/08:30/09:00/12:00 ±%s 分钟）",
            tier,
            BLACKOUT_MINUTES,
        )
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
            "reason": "blackout",
            "counts": {},
            "metrics": line["request_metrics"],
            "start": line["start"],
            "end": line["end"],
        }

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
            "reason": "busy",
            "counts": {},
            "metrics": line["request_metrics"],
            "start": line["start"],
            "end": line["end"],
        }

    deadline = started_at + _tier_max_seconds(tier)
    try:
        if client is None:
            client = make_throttled_client(root)
        set_deadline = getattr(client, "set_deadline", None)
        if callable(set_deadline):
            set_deadline(deadline)
        elif hasattr(client, "_deadline"):
            setattr(client, "_deadline", deadline)

        if tier == "groups":
            counts = _run_groups_tier(root, client, deadline=deadline)
        elif tier == "members":
            counts = _run_members_tier(root, client, deadline=deadline)
        else:
            counts = _run_full_tier(root, client, deadline=deadline)

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


def run_groups(
    root: str | None = None,
    client: ScrmClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    return run_tier("groups", root=root, client=client, force=force)


def run_members(
    root: str | None = None,
    client: ScrmClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    return run_tier("members", root=root, client=client, force=force)


def run_full(
    root: str | None = None,
    client: ScrmClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    return run_tier("full", root=root, client=client, force=force)


# 兼容更显式的命名。
run_groups_tier = run_groups
run_members_tier = run_members
run_full_tier = run_full
