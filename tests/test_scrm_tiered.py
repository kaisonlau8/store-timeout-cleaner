"""分层 SCRM 同步测试：全部使用 fake client / 本地临时库，不访问真实接口。"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import scrm_sync
import scrm_tiered
import timeout_store
from scrm_tiered import (
    ThrottledScrmClient,
    blackout_ok,
    run_full,
    run_groups,
    run_members,
    simulate_day,
)

ROOT = Path(__file__).resolve().parent.parent
STORE = "猛士汽车武汉门店"
DEPT_PATH = "猛士汽车/门店/武汉特区/" + STORE

USERS = [
    {"userId": "u1", "username": "张三", "main_department_id": 4},
    {"userId": "u2", "username": "李四", "main_department_id": 4},
    {"userId": "u3", "username": "刘明轩", "main_department_id": 4},
]
DEPARTMENTS = [
    {"id": 1, "name": "猛士汽车", "parent_id": 0},
    {"id": 2, "name": "门店", "parent_id": 1},
    {"id": 3, "name": "武汉特区", "parent_id": 2},
    {"id": 4, "name": STORE, "parent_id": 3},
]


def _group(chat_id: str, name: str, owner: str, count: int) -> dict:
    return {
        "chat_id": chat_id,
        "name": name,
        "owner": owner,
        "count": count,
        "create_time": 1777593600,
    }


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "timeout.db"
        self.cache_path = self.root / "cache" / "scrm_cache.sqlite3"
        self.metrics_path = self.root / "logs" / "metrics.jsonl"
        self.env = mock.patch.dict(
            os.environ,
            {
                "CLEANER_DB_PATH": str(self.db_path),
                "SCRM_CACHE_DB": str(self.cache_path),
                "SCRM_TIER_METRICS": str(self.metrics_path),
                "SCRM_ROBOT_USERIDS": "u3",
                "SCRM_ROBOT_USERNAME": "刘明轩",
                "SCRM_TIER_LIMIT": "",
                "SCRM_SYNC_CONCURRENCY": "1",
                "SCRM_SYNC_MIN_INTERVAL": "0",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def seed_timeout(
        self,
        rows: list[dict],
        *,
        robot_ids: list[str] | None = None,
        employee_ids: list[str] | None = None,
        employee_names: dict[str, str] | None = None,
    ) -> None:
        conn = timeout_store.connect(str(self.db_path))
        try:
            timeout_store.replace_context(
                conn,
                robot_chat_ids=robot_ids or [],
                employee_ids=employee_ids or [],
                group_rows=rows,
                employee_names=employee_names or {},
            )
            conn.commit()
        finally:
            conn.close()

    def seed_cache(self, members: list[tuple], follows: list[tuple] | None = None) -> None:
        conn = scrm_tiered._cache_connect(str(self.cache_path))
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO scrm_group_member"
                "(chat_id,userid,type,join_time,join_scene,nick,name,invitor) "
                "VALUES (?,?,?,?,?,?,?,?)",
                members,
            )
            if follows:
                conn.executemany(
                    "INSERT OR REPLACE INTO scrm_follow"
                    "(external_userid,userid,name,type,gender,unionid,corp_name,"
                    "remark,remark_mobiles,add_way,add_time,is_del,tags) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    follows,
                )
            conn.commit()
        finally:
            conn.close()

    def read_metrics(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.metrics_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class FakeGroupClient:
    def __init__(self, groups: list[dict], robot_groups: list[str]):
        self.groups = groups
        self.robot_groups = robot_groups

    def list_users(self):
        return USERS

    def list_departments(self):
        return DEPARTMENTS

    def list_groups(self, **filters):
        if filters.get("userid"):
            return [{"chat_id": cid} for cid in self.robot_groups]
        return self.groups


class FakeMemberClient(FakeGroupClient):
    def __init__(self, groups, robot_groups, members, follows, fail_group=""):
        super().__init__(groups, robot_groups)
        self.members = members
        self.follows = follows
        self.fail_group = fail_group

    def paginate_get(self, path, *, extra_params=None, page_size=100, records_key="records"):
        assert path == "/openapi/chat/group/member/list"
        chat_id = extra_params["chat_id"]
        if chat_id == self.fail_group:
            raise scrm_sync.ScrmError("fake member failure")
        return self.members.get(chat_id, [])

    def paginate_post(self, path, *, body_base=None, page_size=100, records_key="records"):
        assert path == "/openapi/user/customer/list"
        userid = body_base["user_id"]
        return self.follows.get(userid, [])


class ThrottleTests(unittest.TestCase):
    class FakeClock:
        def __init__(self):
            self.now_value = 0.0
            self.sleeps: list[float] = []

        def now(self):
            return self.now_value

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now_value += seconds

    class ZeroRng:
        def uniform(self, low, high):
            return 0.0

    class TransportClient(ThrottledScrmClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.starts: list[float] = []

        def _send_http(self, url, data, headers, method):
            self.starts.append(self._clock())
            return {"code": 0, "data": {}}

    def make_client(self, clock, **kwargs):
        return self.TransportClient(
            "http://fake",
            "app",
            "secret",
            clock=clock.now,
            sleep=clock.sleep,
            rng=self.ZeroRng(),
            **kwargs,
        )

    def test_min_spacing_between_request_starts(self):
        clock = self.FakeClock()
        client = self.make_client(clock, concurrency=2, min_interval=0.2)
        for _ in range(3):
            client._request("/openapi/test", with_token=False)
        self.assertEqual(len(client.starts), 3)
        gaps = [b - a for a, b in zip(client.starts, client.starts[1:])]
        self.assertTrue(all(gap >= 0.2 - 1e-9 for gap in gaps), gaps)

    def test_concurrency_cap_two(self):
        class ActiveClient(ThrottledScrmClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.lock = threading.Lock()
                self.active = 0
                self.max_active = 0

            def _send_http(self, url, data, headers, method):
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.08)
                with self.lock:
                    self.active -= 1
                return {"code": 0, "data": {}}

        client = ActiveClient(
            "http://fake",
            "app",
            "secret",
            concurrency=2,
            min_interval=0,
            rng=self.ZeroRng(),
        )
        barrier = threading.Barrier(4)

        def call():
            barrier.wait()
            client._request("/openapi/test", with_token=False)

        threads = [threading.Thread(target=call) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(client.max_active, 2)

    def test_backoff_on_429_and_500_then_success(self):
        clock = self.FakeClock()

        class RetryClient(ThrottledScrmClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.calls = 0

            def _send_http(self, url, data, headers, method):
                self.calls += 1
                if self.calls == 1:
                    raise urllib.error.HTTPError(url, 429, "rate limit", None, None)
                if self.calls == 2:
                    raise urllib.error.HTTPError(url, 500, "boom", None, None)
                return {"code": 0, "data": {"ok": True}}

        class HalfRng:
            def uniform(self, low, high):
                return (low + high) / 2

        client = RetryClient(
            "http://fake",
            "app",
            "secret",
            concurrency=1,
            min_interval=0,
            clock=clock.now,
            sleep=clock.sleep,
            rng=HalfRng(),
        )
        payload = client._request("/openapi/test", with_token=False)
        self.assertEqual(payload["data"], {"ok": True})
        self.assertEqual(client.calls, 3)
        self.assertEqual(clock.sleeps, [1.0, 2.0])
        metrics = client.metrics()
        self.assertEqual(metrics["requests"], 3)
        self.assertEqual(metrics["ok"], 1)
        self.assertEqual(metrics["retries"], 2)
        self.assertEqual(metrics["errors"]["http_429"], 1)
        self.assertEqual(metrics["errors"]["http_500"], 1)

    def test_token_is_cached_for_the_run(self):
        clock = self.FakeClock()

        class TokenClient(ThrottledScrmClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.token_calls = 0

            def _send_http(self, url, data, headers, method):
                if "/openapi/access_token" in url:
                    self.token_calls += 1
                    return {
                        "code": 0,
                        "data": {"access_token": "tok", "expires_in": 7200},
                    }
                return {"code": 0, "data": {"url": url}}

        client = TokenClient(
            "http://fake",
            "app",
            "secret",
            min_interval=0,
            clock=clock.now,
            sleep=clock.sleep,
            rng=self.ZeroRng(),
        )
        client._request("/openapi/a")
        client._request("/openapi/b")
        self.assertEqual(client.token_calls, 1)
        self.assertEqual(client.metrics()["requests"], 3)

    def test_adaptive_slowdown_waits_before_next_request(self):
        clock = self.FakeClock()
        client = self.make_client(clock, concurrency=1, min_interval=0)
        calls = {"n": 0}

        def send(url, data, headers, method):
            calls["n"] += 1
            client.starts.append(clock.now())
            if calls["n"] == 1:
                clock.now_value += 5.0
            return {"code": 0, "data": {}}

        client._send_http = send
        client._request("/openapi/a", with_token=False)
        client._request("/openapi/b", with_token=False)
        self.assertEqual(client.starts, [0.0, 10.0])
        self.assertIn(5.0, clock.sleeps)

    def test_gives_up_after_five_attempts(self):
        clock = self.FakeClock()

        class Always500(ThrottledScrmClient):
            def _send_http(self, url, data, headers, method):
                raise urllib.error.HTTPError(url, 503, "down", None, None)

        client = Always500(
            "http://fake",
            "app",
            "secret",
            min_interval=0,
            clock=clock.now,
            sleep=clock.sleep,
            rng=self.ZeroRng(),
        )
        with self.assertRaises(scrm_sync.ScrmError):
            client._request("/openapi/test", with_token=False)
        self.assertEqual(client.metrics()["requests"], 5)
        self.assertEqual(client.metrics()["retries"], 4)


class GroupsTierTests(_Base):
    def test_diff_inserts_new_updates_renamed_and_never_deletes(self):
        self.seed_timeout(
            [
                {
                    "group_id": "g1",
                    "group_name": "旧甲群",
                    "store": "旧店",
                    "theater": "旧战区",
                    "owner": "旧人",
                    "owner_id": "old",
                    "dept": "old",
                    "member_count": 1,
                },
                {
                    "group_id": "g_keep",
                    "group_name": "保留群",
                    "store": STORE,
                    "theater": "武汉特区",
                    "owner": "张三",
                    "owner_id": "u1",
                    "dept": DEPT_PATH,
                    "member_count": 3,
                },
            ],
            robot_ids=["g1"],
            employee_ids=["u_old"],
            employee_names={"u_old": "老员工"},
        )
        groups = [
            _group("g1", "新甲群", "u1", 15),
            _group("g2", "乙群", "u2", 20),
        ]
        client = FakeGroupClient(groups, robot_groups=["g2"])

        result = run_groups(str(self.root), client=client)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["counts"]["new_groups"], 1)
        self.assertEqual(result["counts"]["renamed_groups"], 1)

        conn = timeout_store.connect(str(self.db_path))
        try:
            rows = {
                row["room_id"]: dict(row)
                for row in conn.execute("SELECT * FROM chat_store")
            }
            self.assertEqual(set(rows), {"g1", "g2", "g_keep"})
            self.assertEqual(rows["g1"]["group_name"], "新甲群")
            self.assertEqual(rows["g1"]["owner_name"], "张三")
            self.assertEqual(rows["g1"]["store"], STORE)
            self.assertEqual(rows["g1"]["theater"], "武汉特区")
            self.assertEqual(rows["g1"]["member_count"], 15)
            self.assertEqual(rows["g2"]["in_robot"], 1)
            self.assertEqual(rows["g_keep"]["group_name"], "保留群")
            robots = {row["room_id"] for row in conn.execute("SELECT room_id FROM robot_chat")}
            self.assertEqual(robots, {"g1", "g2"})
            employees = {
                row["userid"]: row["name"]
                for row in conn.execute("SELECT userid, name FROM employee")
            }
            self.assertEqual(employees["u_old"], "老员工")
            self.assertEqual(employees["u1"], "张三")
            last = json.loads(timeout_store.get_meta(conn, "tier_groups_last"))
            self.assertEqual(last["status"], "success")
        finally:
            conn.close()

        metrics = self.read_metrics()
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0]["tier"], "groups")
        self.assertEqual(metrics[0]["counts"]["new_groups"], 1)
        self.assertNotIn("secret", json.dumps(metrics, ensure_ascii=False))


class FullTierTests(_Base):
    def test_removes_dissolved_groups_and_cache_members(self):
        keep = {
            "group_id": "g_keep",
            "group_name": "保留群",
            "store": STORE,
            "theater": "武汉特区",
            "owner": "张三",
            "owner_id": "u1",
            "dept": DEPT_PATH,
            "member_count": 3,
        }
        gone = {
            "group_id": "g_gone",
            "group_name": "已解散",
            "store": STORE,
            "theater": "武汉特区",
            "owner": "李四",
            "owner_id": "u2",
            "dept": DEPT_PATH,
            "member_count": 2,
        }
        self.seed_timeout(
            [keep, gone],
            robot_ids=["g_keep", "g_gone"],
            employee_ids=["u1", "u2"],
            employee_names={"u1": "张三", "u2": "李四"},
        )
        self.seed_cache(
            [
                ("g_keep", "m1", 1, 1, 1, "n", "name", ""),
                ("g_gone", "m2", 1, 2, 1, "n", "name", ""),
            ]
        )
        data_file = self.root / "data.json"
        data_file.write_text('{"sentinel": true}', encoding="utf-8")

        client = FakeGroupClient(
            [_group("g_keep", "保留群-新", "u1", 9)],
            robot_groups=["g_keep"],
        )
        result = run_full(str(self.root), client=client)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["counts"]["dissolved"], 1)
        self.assertEqual(result["counts"]["cache_members_deleted"], 1)

        conn = timeout_store.connect(str(self.db_path))
        try:
            rooms = {row["room_id"] for row in conn.execute("SELECT room_id FROM chat_store")}
            robots = {row["room_id"] for row in conn.execute("SELECT room_id FROM robot_chat")}
            self.assertEqual(rooms, {"g_keep"})
            self.assertEqual(robots, {"g_keep"})
        finally:
            conn.close()
        # full 层不计算超时，也不改写 data.json；00:00/12:00 原任务仍负责统计。
        self.assertEqual(data_file.read_text(encoding="utf-8"), '{"sentinel": true}')

        cache = sqlite3.connect(str(self.cache_path))
        try:
            cached = {
                row[0]
                for row in cache.execute("SELECT chat_id FROM scrm_group_member")
            }
        finally:
            cache.close()
        self.assertEqual(cached, {"g_keep"})


class MembersTierTests(_Base):
    def _seed(self):
        self.seed_timeout(
            [
                {
                    "group_id": "g1",
                    "group_name": "甲群",
                    "store": "",
                    "theater": "",
                    "owner": "旧人",
                    "owner_id": "old",
                    "dept": "",
                    "member_count": 1,
                },
                {
                    "group_id": "g2",
                    "group_name": "乙群",
                    "store": STORE,
                    "theater": "武汉特区",
                    "owner": "李四",
                    "owner_id": "u2",
                    "dept": DEPT_PATH,
                    "member_count": 2,
                },
            ],
            employee_ids=["u1", "u2"],
            employee_names={"u1": "张三", "u2": "李四"},
        )

    def _client(self, fail_group=""):
        groups = [
            _group("g1", "甲群", "u1", 11),
            _group("g2", "乙群", "u2", 22),
            _group("g_new", "新群但本层不插入", "u2", 5),
        ]
        members = {
            "g1": [{"userid": "m1", "type": 2, "join_time": 10, "join_scene": 1,
                    "user_chat_nick_name": "小明", "name": "客户甲", "invitor": "u1"}],
            "g2": [{"userid": "m2", "type": 1, "join_time": 11, "join_scene": 2,
                    "user_chat_nick_name": "小李", "name": "员工乙"}],
            "g_new": [{"userid": "m3", "type": 2, "join_time": 12}],
        }
        follows = {
            "u1": [{"external_userid": "ext1", "name": "客户1", "type": 1,
                    "gender": 1, "unionid": "union1", "corp_name": "公司",
                    "remark": "备注", "remark_mobiles": ["13800000000"],
                    "add_way": 1, "add_time": 100, "is_del": 0, "tags": ["A"]}],
            "u2": [{"external_userid": "ext2", "name": "客户2"}],
        }
        return FakeMemberClient(groups, [], members, follows, fail_group=fail_group)

    def test_owner_count_update_and_staging_swap(self):
        self._seed()
        self.seed_cache([("old_chat", "old_member", 1, 1, 1, "", "", "")])
        result = run_members(str(self.root), client=self._client())

        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(result["counts"]["members"], 2)
        self.assertGreaterEqual(result["counts"]["follows"], 2)

        conn = timeout_store.connect(str(self.db_path))
        try:
            rows = {
                row["room_id"]: dict(row)
                for row in conn.execute("SELECT * FROM chat_store")
            }
            self.assertEqual(set(rows), {"g1", "g2"})  # 不插入 g_new
            self.assertEqual(rows["g1"]["owner_name"], "张三")
            self.assertEqual(rows["g1"]["member_count"], 11)
            self.assertEqual(rows["g1"]["store"], STORE)
            self.assertEqual(rows["g2"]["member_count"], 22)
        finally:
            conn.close()

        cache = sqlite3.connect(str(self.cache_path))
        try:
            cached = {
                (row[0], row[1])
                for row in cache.execute("SELECT chat_id, userid FROM scrm_group_member")
            }
            follow_names = {
                row[0]: row[1]
                for row in cache.execute(
                    "SELECT external_userid, name FROM scrm_follow ORDER BY external_userid"
                )
            }
        finally:
            cache.close()
        self.assertEqual(
            cached,
            {("g1", "m1"), ("g2", "m2"), ("g_new", "m3")},
        )
        self.assertEqual(follow_names, {"ext1": "客户1", "ext2": "客户2"})
        self.assertEqual(stat.S_IMODE(self.cache_path.stat().st_mode), 0o600)

    def test_aborts_and_keeps_old_cache_when_error_rate_over_five_percent(self):
        self._seed()
        original = [("g1", "old_member", 1, 1, 1, "", "", "")]
        self.seed_cache(original)

        with self.assertRaises(scrm_tiered.ScrmTierError):
            run_members(str(self.root), client=self._client(fail_group="g1"))

        cache = sqlite3.connect(str(self.cache_path))
        try:
            rows = list(cache.execute("SELECT chat_id, userid FROM scrm_group_member"))
        finally:
            cache.close()
        self.assertEqual(rows, [("g1", "old_member")])
        self.assertEqual(self.read_metrics()[-1]["status"], "error")

    def test_tier_limit_caps_groups_and_users(self):
        self._seed()
        os.environ["SCRM_TIER_LIMIT"] = "1"
        fake = self._client()
        calls = []
        orig_get = fake.paginate_get
        orig_post = fake.paginate_post

        def paginate_get(*args, **kwargs):
            calls.append(("get", kwargs["extra_params"]["chat_id"]))
            return orig_get(*args, **kwargs)

        def paginate_post(*args, **kwargs):
            calls.append(("post", kwargs["body_base"]["user_id"]))
            return orig_post(*args, **kwargs)

        fake.paginate_get = paginate_get
        fake.paginate_post = paginate_post
        result = run_members(str(self.root), client=fake)
        self.assertEqual(result["counts"]["groups"], 1)
        self.assertEqual(result["counts"]["employees"], 1)
        self.assertEqual(len(calls), 2)


class LockTests(_Base):
    def test_busy_tier_lock_skips_without_calling_client(self):
        lock_path = self.root / ".sync-tier-groups.lock"
        fh = open(lock_path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            class BoomClient:
                def list_users(self):
                    raise AssertionError("client must not be called")

            result = run_groups(str(self.root), client=BoomClient())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()

        self.assertEqual(result["status"], "skip")
        metrics = self.read_metrics()
        self.assertEqual(metrics[-1]["status"], "skip")
        self.assertEqual(metrics[-1]["requests"], 0)


class ScheduleTests(unittest.TestCase):
    def test_simulate_day_counts_and_no_blackout_starts(self):
        runs = simulate_day()
        self.assertEqual(len(runs), 31)
        groups = [item for item in runs if item[1] == "groups"]
        full = [item for item in runs if item[1] == "full"]
        members = [item for item in runs if item[1] == "members"]
        self.assertEqual(len(groups), 24)
        self.assertEqual(len(full), 1)
        self.assertEqual(len(members), 6)
        self.assertEqual(full, [("02:00", "full")])
        self.assertTrue(all(hm.endswith(":20") for hm, _ in groups))
        self.assertTrue(all(blackout_ok(hm) for hm, _ in runs))

    def test_blackout_windows_are_inclusive_and_wrap_midnight(self):
        for hm in ("00:00", "00:05", "08:30", "08:25", "08:35", "09:00", "12:00"):
            self.assertFalse(blackout_ok(hm), hm)
        for hm in ("00:06", "08:24", "08:36", "12:06", "23:54"):
            self.assertTrue(blackout_ok(hm), hm)
        self.assertFalse(blackout_ok("23:56"))
        self.assertFalse(blackout_ok(1439))


class UnchangedBehaviourTests(unittest.TestCase):
    def test_sync_from_scrm_default_still_uses_factory(self):
        class FakeClient:
            def list_users(self):
                return USERS

            def list_departments(self):
                return DEPARTMENTS

            def list_groups(self, **filters):
                if filters.get("userid"):
                    return [{"chat_id": "g1"}]
                return [_group("g1", "甲群", "u1", 12)]

        env = mock.patch.dict(
            os.environ, {"SCRM_ROBOT_USERIDS": "u3"}, clear=False
        )
        with env, mock.patch.object(scrm_sync, "make_scrm_client", lambda root: FakeClient()):
            result = scrm_sync.sync_from_scrm("/tmp/not-used")
        self.assertEqual(result["all_rows"][0]["group_name"], "甲群")
        self.assertEqual(result["employee_names"]["u1"], "张三")

    def test_run_sync_and_cron_script_are_not_rewired(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        start = app_source.index("def run_sync():")
        end = app_source.index("def run_timeout_sync(", start)
        block = app_source[start:end]
        self.assertIn("lock_fh = _acquire_sync_lock()", block)
        self.assertIn("summary = _run_group_and_timeout()", block)
        self.assertIn('summary["timeout"] = timeout_algo.run(ROOT, label="cron")', block)
        self.assertNotIn("Throttled", block)
        self.assertNotIn("scrm_tiered", block)

        script = (ROOT / "scripts" / "sync_scrm.py").read_text(encoding="utf-8")
        self.assertIn("from app import run_sync", script)
        self.assertIn("summary = run_sync()", script)
        self.assertNotIn("scrm_tiered", script)


if __name__ == "__main__":
    unittest.main()
