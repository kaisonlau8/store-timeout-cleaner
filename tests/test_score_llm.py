import json
import os
import tempfile
import threading
import time
import unittest
from datetime import date
from unittest import mock

from score_llm import (
    BATCH_SIZE,
    MAX_WORKERS,
    execute_review,
    review_status,
    review_texts,
    start_review_job,
    text_hash,
)
from score_query import build_report, store_detail
from score_export import stores_to_csv
from tests.test_score_query import _seed
from timeout_store import connect


class _Resp:
    def __init__(self, status, payload=None, raw=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = raw or ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


def _ok(body_items):
    return _Resp(
        200,
        {"choices": [{"message": {"content": json.dumps({"items": body_items}, ensure_ascii=False)}}]},
    )


def _items_for(content):
    items = []
    for line in content.splitlines():
        idx, _text = line.split(". ", 1)
        items.append({"i": int(idx), "count": False, "reason": "闲聊"})
    return items


class ReviewClientTests(unittest.TestCase):
    def test_batches_unique_texts_and_uses_high_concurrency(self):
        texts = [f"消息{i}" for i in range(45)] + ["消息0"]
        sizes = []
        workers = {}

        def post(url, headers=None, json=None, timeout=None):
            self.assertNotIn("super-secret", url)
            content = json["messages"][1]["content"]
            sizes.append(len(content.splitlines()))
            self.assertEqual(json["model"], "deepseek-chat")
            self.assertEqual(headers["Authorization"], "Bearer super-secret")
            return _ok(_items_for(content))

        class Factory:
            def __init__(self, n):
                workers["n"] = n

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def map(self, fn, batches):
                return map(fn, list(batches))

        out = review_texts(
            texts,
            key="super-secret",
            post=post,
            sleep=lambda _s: None,
            executor_factory=Factory,
        )
        self.assertEqual(sorted(sizes), [5, BATCH_SIZE, BATCH_SIZE])
        self.assertEqual(workers["n"], MAX_WORKERS)
        self.assertEqual(len(out), 45)
        self.assertFalse(out["消息0"]["count"])
        self.assertEqual(out["消息0"]["reason"], "闲聊")

    def test_retries_429_then_succeeds(self):
        sleeps = []
        calls = {"n": 0}

        def post(url, headers=None, json=None, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                return _Resp(429)
            content = json["messages"][1]["content"]
            return _ok([{"i": 0, "count": True, "reason": "有效"}])

        out = review_texts(
            ["想问下保养周期"],
            key="k",
            post=post,
            sleep=lambda s: sleeps.append(s),
        )
        self.assertEqual(calls["n"], 3)
        self.assertGreaterEqual(len(sleeps), 2)
        self.assertTrue(out["想问下保养周期"]["count"])

    def test_failed_batch_is_not_a_verdict(self):
        def post(url, headers=None, json=None, timeout=None):
            return _Resp(500)

        out = review_texts(["还在吗"], key="k", post=post, sleep=lambda _s: None, attempts=2)
        self.assertEqual(out, {})


class CacheAndScoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        _seed(self.root)
        self._old = os.environ.pop("DEEPSEEK_API_KEY", None)

    def tearDown(self):
        if self._old is not None:
            os.environ["DEEPSEEK_API_KEY"] = self._old
        elif "DEEPSEEK_API_KEY" in os.environ:
            del os.environ["DEEPSEEK_API_KEY"]
        self.tmp.cleanup()

    def test_cache_skips_second_call_and_rules_are_not_resent(self):
        seen = []

        def post(url, headers=None, json=None, timeout=None):
            content = json["messages"][1]["content"]
            seen.append(content)
            items = []
            for line in content.splitlines():
                idx, text = line.split(". ", 1)
                exclude = text == "刹车有异响"
                items.append({"i": int(idx), "count": not exclude, "reason": "非提问告知" if exclude else "有效"})
            return _ok(items)

        first = execute_review(
            self.root,
            date(2026, 7, 1),
            date(2026, 9, 30),
            key="k",
            post=post,
            sleep=lambda _s: None,
        )
        self.assertEqual(first["reviewed"], 3)
        self.assertEqual(len(seen), 1)
        self.assertNotIn("好的", seen[0])
        self.assertIn("刹车有异响", seen[0])

        def boom(*args, **kwargs):
            raise AssertionError("缓存命中不应再请求")

        second = execute_review(
            self.root,
            date(2026, 7, 1),
            date(2026, 9, 30),
            key="k",
            post=boom,
            sleep=lambda _s: None,
        )
        self.assertEqual(second["reviewed"], 0)
        self.assertGreaterEqual(second["cached"], 1)
        conn = connect(os.path.join(self.root, "timeout.db"))
        try:
            row = conn.execute(
                "SELECT keep, reason FROM score_llm_cache WHERE text_hash = ? AND prompt_version = ?",
                (text_hash("刹车有异响"), "v1"),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["keep"], 0)
        self.assertEqual(row["reason"], "非提问告知")

    def test_scoring_with_and_without_llm_flags(self):
        def post(url, headers=None, json=None, timeout=None):
            content = json["messages"][1]["content"]
            items = []
            for line in content.splitlines():
                idx, text = line.split(". ", 1)
                exclude = text == "刹车有异响"
                items.append({"i": int(idx), "count": not exclude, "reason": "非提问告知" if exclude else "有效"})
            return _ok(items)

        execute_review(
            self.root,
            date(2026, 7, 1),
            date(2026, 9, 30),
            key="k",
            post=post,
            sleep=lambda _s: None,
        )
        off = build_report(self.root, date(2026, 7, 1), date(2026, 9, 30), use_llm=False)
        on = build_report(self.root, date(2026, 7, 1), date(2026, 9, 30), use_llm=True)
        os.environ["DEEPSEEK_API_KEY"] = "k"
        auto = build_report(self.root, date(2026, 7, 1), date(2026, 9, 30))
        off_store = {row["name"]: row for row in off["stores"]}["甲店"]
        on_store = {row["name"]: row for row in on["stores"]}["甲店"]
        self.assertEqual(off_store["valid"], 1)
        self.assertEqual(off_store["llm_excluded"], 0)
        self.assertEqual(on_store["valid"], 0)
        self.assertEqual(on_store["llm_excluded"], 1)
        self.assertTrue(on["llm_on"])
        auto_store = {row["name"]: row for row in auto["stores"]}["甲店"]
        self.assertTrue(auto["llm_on"])
        self.assertEqual(auto_store["valid"], 0)
        detail = store_detail(self.root, "甲店", date(2026, 7, 1), date(2026, 9, 30), use_llm=True)
        reasons = [ev["reason"] for ev in detail["excluded"]]
        messages = [ev["message"] for ev in detail["excluded"]]
        self.assertIn("LLM复核剔除: 非提问告知", reasons)
        self.assertIn("刹车有异响", messages)
        csv_text = stores_to_csv(on["stores"]).decode("utf-8-sig")
        self.assertIn("LLM剔除", csv_text)
        self.assertNotIn("刹车有异响", csv_text)

    def test_api_failure_leaves_event_counted(self):
        def post(url, headers=None, json=None, timeout=None):
            return _Resp(500)

        with self.assertLogs("score_llm", level="WARNING") as logs:
            result = execute_review(
                self.root,
                date(2026, 7, 1),
                date(2026, 9, 30),
                key="super-secret-key",
                post=post,
                sleep=lambda _s: None,
                attempts=1,
            )
        self.assertEqual(result["failed"], 1)
        self.assertNotIn("super-secret-key", "\n".join(logs.output))
        report = build_report(self.root, date(2026, 7, 1), date(2026, 9, 30), use_llm=True)
        store = {row["name"]: row for row in report["stores"]}["甲店"]
        self.assertEqual(store["valid"], 1)
        self.assertEqual(store["llm_excluded"], 0)

    def test_page_says_llm_off_without_key_and_job_does_not_block(self):
        from flask import Flask

        from score_web import register_score

        app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
        )
        app.jinja_env.globals["ds_css"] = lambda: ""
        app.jinja_env.globals["ds_sprite"] = lambda: ""
        app.jinja_env.globals["ds_cards"] = lambda: ""
        register_score(app, self.root)
        client = app.test_client()
        with mock.patch("score_llm.start_review_job") as job:
            page = client.get("/score?start=2026-07-01&end=2026-09-30")
        self.assertEqual(page.status_code, 200)
        job.assert_not_called()
        html = page.get_data(as_text=True)
        self.assertIn("LLM复核关闭", html)
        self.assertNotIn("运行 LLM", html)
        self.assertNotIn("计入剔除", html)
        self.assertNotIn("llm-run", html)
        self.assertNotIn("llm-toggle", html)
        self.assertNotIn("DEEPSEEK_API_KEY", html)

        os.environ["DEEPSEEK_API_KEY"] = "super-secret-key"
        with mock.patch("score_web.start_review_job", return_value={"status": "running", "total": 2, "done": 0, "failed": 0}) as job:
            resp = client.post("/api/score/llm-review?start=2026-07-01&end=2026-09-30")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.get_json()["status"], "running")
        job.assert_called_once()
        self.assertNotIn("super-secret-key", resp.get_data(as_text=True))

    def test_review_job_returns_before_the_request_finishes(self):
        gate = threading.Event()

        def post(url, headers=None, json=None, timeout=None):
            self.assertTrue(gate.wait(2))
            content = json["messages"][1]["content"]
            return _ok(_items_for(content))

        os.environ["DEEPSEEK_API_KEY"] = "k"
        started = time.monotonic()
        status = start_review_job(
            self.root,
            date(2026, 7, 1),
            date(2026, 9, 30),
            post=post,
        )
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(status["status"], "running")
        gate.set()
        for _ in range(50):
            current = review_status(self.root, date(2026, 7, 1), date(2026, 9, 30))
            if current["status"] == "done":
                break
            time.sleep(0.05)
        self.assertEqual(current["status"], "done")

    def _score_client(self):
        from flask import Flask

        from score_web import register_score

        app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
        )
        app.jinja_env.globals["ds_css"] = lambda: ""
        app.jinja_env.globals["ds_sprite"] = lambda: ""
        app.jinja_env.globals["ds_cards"] = lambda: ""
        register_score(app, self.root)
        return app.test_client()

    def test_force_refresh_sends_cached_texts_again(self):
        calls = {"n": 0}

        def post(url, headers=None, json=None, timeout=None):
            calls["n"] += 1
            content = json["messages"][1]["content"]
            return _ok(_items_for(content))

        kwargs = {
            "key": "k",
            "post": post,
            "sleep": lambda _s: None,
        }
        execute_review(self.root, date(2026, 7, 1), date(2026, 9, 30), **kwargs)
        self.assertEqual(calls["n"], 1)
        execute_review(self.root, date(2026, 7, 1), date(2026, 9, 30), force=True, **kwargs)
        self.assertEqual(calls["n"], 2)

    def test_opening_score_starts_incremental_review_for_uncached_texts(self):
        os.environ["DEEPSEEK_API_KEY"] = "k"
        client = self._score_client()
        with mock.patch(
            "score_llm.start_review_job",
            return_value={"status": "running", "total": 3, "done": 0, "failed": 0, "note": ""},
        ) as job:
            page = client.get("/score?start=2026-07-01&end=2026-09-30")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("LLM复核已自动计入", html)
        self.assertNotIn("运行 LLM", html)
        self.assertNotIn("重新复核", html)
        self.assertNotIn("计入剔除", html)
        self.assertNotIn("llm-run", html)
        self.assertNotIn("llm-toggle", html)
        self.assertNotIn("llm=", html)
        self.assertIn('data-auto="1"', html)
        job.assert_called_once()
        self.assertFalse(job.call_args.kwargs.get("force", False))
        script_path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "score.js")
        with open(script_path, encoding="utf-8") as handle:
            script = handle.read()
        self.assertNotIn("llm-run", script)
        self.assertIn("llm-status", script)

    def test_cached_scores_apply_without_an_include_toggle(self):
        from score_llm import cache_put

        cache_put(
            self.root,
            {
                "刹车有异响": {"count": False, "reason": "非提问告知"},
                "想预约保养": {"count": True, "reason": "有效"},
                "对不上门店": {"count": True, "reason": "有效"},
            },
        )
        os.environ["DEEPSEEK_API_KEY"] = "k"
        client = self._score_client()
        with mock.patch("score_llm.start_review_job") as job:
            page = client.get("/score?start=2026-07-01&end=2026-09-30&llm=0")
            detail = client.get("/score/store?name=甲店&start=2026-07-01&end=2026-09-30&llm=0")
            exported = client.get("/api/score/export.csv?start=2026-07-01&end=2026-09-30&llm=0")
        html = page.get_data(as_text=True)
        self.assertIn("LLM复核已自动计入", html)
        self.assertIn('data-auto="0"', html)
        self.assertNotIn("计入剔除", html)
        self.assertNotIn("llm=", html)
        job.assert_not_called()
        body = detail.get_data(as_text=True)
        self.assertIn("LLM复核剔除: 非提问告知", body)
        self.assertNotIn("llm=", body)
        line = next(row for row in exported.get_data(as_text=True).splitlines() if row.startswith("甲店"))
        self.assertIn("甲店,,华东战区,2,2,0,1", line)

    def test_partial_cache_counts_now_and_schedules_the_gap(self):
        from score_llm import cache_put

        cache_put(self.root, {"刹车有异响": {"count": False, "reason": "非提问告知"}})
        os.environ["DEEPSEEK_API_KEY"] = "k"
        client = self._score_client()
        with mock.patch(
            "score_llm.start_review_job",
            return_value={"status": "running", "total": 2, "done": 0, "failed": 0, "note": ""},
        ) as job:
            detail = client.get("/score/store?name=甲店&start=2026-07-01&end=2026-09-30&llm=0")
        self.assertIn("LLM复核剔除: 非提问告知", detail.get_data(as_text=True))
        job.assert_called_once()
        self.assertFalse(job.call_args.kwargs.get("force", False))

    def test_score_page_returns_before_review_finishes(self):
        from score_llm import wait_review_jobs

        gate = threading.Event()

        def post(url, headers=None, json=None, timeout=None):
            self.assertTrue(gate.wait(3))
            content = json["messages"][1]["content"]
            return _ok(_items_for(content))

        os.environ["DEEPSEEK_API_KEY"] = "k"
        client = self._score_client()
        with mock.patch("score_llm._default_post", post):
            started = time.monotonic()
            page = client.get("/score?start=2026-07-01&end=2026-09-30")
            elapsed = time.monotonic() - started
            self.assertEqual(page.status_code, 200)
            self.assertLess(elapsed, 1.0)
            self.assertIn("LLM复核已自动计入", page.get_data(as_text=True))
            gate.set()
            wait_review_jobs()

    def test_excel_import_schedules_incremental_review_only(self):
        from timeout_excel import import_excel_file

        path = os.path.join(self.root, "input_file", "导出时间范围20260701-20260930回复超时记录.xlsx")
        os.environ["DEEPSEEK_API_KEY"] = "k"
        with mock.patch("score_llm.start_review_job", return_value={"status": "running"}) as job:
            rec = import_excel_file(self.root, path, force=True)
            self.assertGreater(rec["events"], 0)
            job.assert_called_once()
            self.assertFalse(job.call_args.kwargs.get("force", False))
            self.assertEqual(job.call_args.args[1], date(2026, 7, 1))
            self.assertEqual(job.call_args.args[2], date(2026, 9, 30))
            job.reset_mock()
            again = import_excel_file(self.root, path, force=False)
        self.assertTrue(again.get("skipped"))
        job.assert_not_called()

    def test_excel_import_without_key_still_succeeds(self):
        from timeout_excel import import_excel_file

        path = os.path.join(self.root, "input_file", "导出时间范围20260701-20260930回复超时记录.xlsx")
        with mock.patch("score_llm.start_review_job") as job:
            rec = import_excel_file(self.root, path, force=True)
        self.assertGreater(rec["events"], 0)
        job.assert_not_called()

    def test_failed_review_does_not_immediately_autostart_again(self):
        from score_llm import maybe_autostart_review

        def post(url, headers=None, json=None, timeout=None):
            return _Resp(500)

        execute_review(
            self.root,
            date(2026, 7, 1),
            date(2026, 9, 30),
            key="k",
            post=post,
            sleep=lambda _s: None,
            attempts=1,
        )
        os.environ["DEEPSEEK_API_KEY"] = "k"
        with mock.patch("score_llm.start_review_job") as job:
            maybe_autostart_review(
                self.root,
                date(2026, 7, 1),
                date(2026, 9, 30),
                pending=3,
                cached=0,
                source="page",
            )
        job.assert_not_called()
        with mock.patch("score_llm.start_review_job", return_value={"status": "running"}) as job:
            maybe_autostart_review(
                self.root,
                date(2026, 7, 1),
                date(2026, 9, 30),
                source="import",
            )
        job.assert_called_once()
        self.assertFalse(job.call_args.kwargs.get("force", False))

    def test_import_during_running_review_queues_another_incremental_pass(self):
        from score_llm import maybe_autostart_review, wait_review_jobs

        entered = threading.Event()
        release = threading.Event()
        calls = []

        def fake_execute(root, start, end, **kwargs):
            calls.append(bool(kwargs.get("force")))
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(3))
            return {"reviewed": 0, "cached": 0, "failed": 0, "status": "done"}

        os.environ["DEEPSEEK_API_KEY"] = "k"
        with mock.patch("score_llm.execute_review", fake_execute):
            start_review_job(self.root, date(2026, 7, 1), date(2026, 9, 30), force=False)
            self.assertTrue(entered.wait(2))
            maybe_autostart_review(self.root, date(2026, 7, 1), date(2026, 9, 30), source="import")
            release.set()
            wait_review_jobs()
        self.assertEqual(calls, [False, False])

    def test_force_endpoint_remains_off_the_score_page(self):
        os.environ["DEEPSEEK_API_KEY"] = "k"
        client = self._score_client()
        with mock.patch(
            "score_web.start_review_job",
            return_value={"status": "running", "total": 1, "done": 0, "failed": 0, "note": ""},
        ) as job:
            resp = client.post("/api/score/llm-review?start=2026-07-01&end=2026-09-30&force=1")
        self.assertEqual(resp.status_code, 202)
        self.assertTrue(job.call_args.kwargs.get("force"))

    def test_sync_script_waits_for_background_review(self):
        path = os.path.join(os.path.dirname(__file__), "..", "scripts", "sync_timeout.py")
        with open(path, encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn("wait_review_jobs", script)


if __name__ == "__main__":
    unittest.main()
