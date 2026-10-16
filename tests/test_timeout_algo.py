import os
import tempfile
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

import timeout_algo
from timeout_store import connect

SH = ZoneInfo("Asia/Shanghai")


def ts(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=SH).timestamp())


class AlgoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.environ.pop("DEEPSEEK_API_KEY", None)
        os.environ["TIMEOUT_SINCE"] = "2026-01-01"
        conn = connect(os.path.join(self.root, "timeout.db"))
        conn.execute("INSERT INTO chat_store(room_id, store, theater, group_name, in_robot) VALUES ('r1','甲店','华东','甲店群',1)")
        conn.execute("INSERT INTO robot_chat(room_id) VALUES ('r1')")
        conn.execute("INSERT INTO employee(userid) VALUES ('emp')")
        # 旧 Excel 事件：迁移后应进存档表，不再出现在 timeout_event
        conn.execute("INSERT INTO timeout_event(msg_id, rule_id, created_at, source) VALUES ('x1','r','2026-07-01 10:00:00','excel')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _msgs(self, conn, rows):
        conn.executemany("INSERT INTO chat_msg VALUES (?,?,?,?,?,?)", rows)
        conn.commit()

    def test_legacy_archived_and_rules_seeded(self):
        conn = timeout_algo.open_db(self.root)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM timeout_event").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM timeout_event_legacy").fetchone()[0], 1)
        groups = timeout_algo.list_rules(conn)
        self.assertEqual({g["current"]["minutes"] for g in groups}, {10, 30})
        ten = next(g for g in groups if g["current"]["minutes"] == 10)["current"]
        self.assertEqual(ten["effective_from"], "2026-03-23")
        conn.close()

    def test_rule_versions_apply_by_day(self):
        conn = timeout_algo.open_db(self.root)
        thirty = next(g for g in timeout_algo.list_rules(conn) if g["current"]["minutes"] == 30)
        key, since = timeout_algo.save_rule(conn, {"name": "30 分钟", "minutes": 45, "days": [1, 2, 3, 4, 5],
                                                   "start": "09:00", "end": "20:00", "effective_from": "2026-08-01"},
                                            rule_key=thirty["rule_key"], actor="t")
        self.assertEqual(since, date(2026, 8, 1))
        before = {r.minutes for r in timeout_algo.rules_for_day(conn, date(2026, 7, 31))}
        after = {r.minutes for r in timeout_algo.rules_for_day(conn, date(2026, 8, 3))}
        self.assertEqual(before, {10, 30})
        self.assertEqual(after, {10, 45})
        with self.assertRaises(ValueError):
            timeout_algo.save_rule(conn, {"name": "x", "minutes": 5, "days": [1], "start": "20:00", "end": "09:00",
                                          "effective_from": "2026-08-01"}, rule_key=None, actor="t")
        conn.close()

    def test_compute_day_and_filters(self):
        conn = timeout_algo.open_db(self.root)
        # 2026-08-03 周一
        self._msgs(conn, [
            ("m1", "r1", "wm-cust", ts("2026-08-03 10:00:00"), "text", "刹车有异响"),
            ("m2", "r1", "emp", ts("2026-08-03 10:20:00"), "text", "马上看"),
            ("m3", "r1", "wm-cust", ts("2026-08-03 15:00:00"), "text", "好的"),
            ("m4", "r1", "wm-cust", ts("2026-08-03 21:00:00"), "text", "晚上消息"),
        ])
        n = timeout_algo.compute_day(conn, date(2026, 8, 3), ["r1"], ["emp"], {"r1": {"store": "甲店", "theater": "华东", "group_name": "甲店群"}})
        conn.commit()
        conn.close()
        # m1：10 分钟超时（20 分钟才回），30 分钟未超；m3：10 与 30 都超
        self.assertEqual(n, 3)
        stats = timeout_algo.apply_filters(self.root, date(2026, 8, 3), date(2026, 8, 3), llm=False)
        self.assertEqual(stats["valid"], 1)  # m1 计入；m3「好的」被寒暄规则剔除，另一条重复
        conn = connect(os.path.join(self.root, "timeout.db"))
        rows = {(r["msg_id"], r["rule_id"]): r["exclude_reason"] for r in conn.execute("SELECT * FROM timeout_event")}
        conn.close()
        self.assertEqual(rows[("m1", "2035988283787413634")], "")
        self.assertTrue(all(v for k, v in rows.items() if k[0] == "m3"))


if __name__ == "__main__":
    unittest.main()


class KpiFlagTests(AlgoTests):
    def test_ten_minute_rule_is_display_only_and_flag_is_editable(self):
        from score_query import _classified_events
        conn = timeout_algo.open_db(self.root)
        flags = timeout_algo.kpi_rule_flags(conn)
        self.assertFalse(flags[timeout_algo.NON_KPI_SEED])
        self.assertEqual(sum(flags.values()), 1)
        conn.execute("INSERT INTO timeout_event(msg_id, rule_id, rule_name, room_id, store, created_at, source, content, exclude_reason) "
                     "VALUES ('m1', ?, '10 分钟未回复预警', 'r1', '甲店', '2026-07-01 10:00:00', 'api', '请问到店时间', ''),"
                     "('m1', '2055182790919555459', '30 分钟未回复预警', 'r1', '甲店', '2026-07-01 10:00:00', 'api', '请问到店时间', '')",
                     (timeout_algo.NON_KPI_SEED,))
        conn.commit()
        kpi, _, _ = _classified_events(self.root, date(2026, 7, 1), date(2026, 7, 1))
        self.assertEqual([e["rule_id"] for e in kpi], ["2055182790919555459"])
        self.assertEqual(kpi[0].get("reason") or "", "")  # 30 分钟那条不再被当成「重复触发」
        allev, _, _ = _classified_events(self.root, date(2026, 7, 1), date(2026, 7, 1), kpi_only=False)
        self.assertEqual(len(allev), 2)
        ten = next(g for g in timeout_algo.list_rules(conn) if g["current"]["minutes"] == 10)["current"]
        timeout_algo.save_rule(conn, {"name": ten["name"], "minutes": 10, "days": ten["days"], "start": "09:00", "end": "20:00",
                                      "effective_from": "2026-03-23", "enabled": True, "counts_for_kpi": True},
                               rule_key=timeout_algo.NON_KPI_SEED, actor="t")
        self.assertTrue(timeout_algo.kpi_rule_flags(conn)[timeout_algo.NON_KPI_SEED])
        # 不传 counts_for_kpi 时沿用原设置
        timeout_algo.save_rule(conn, {"name": ten["name"], "minutes": 10, "days": ten["days"], "start": "09:00", "end": "20:00",
                                      "effective_from": "2026-03-23"}, rule_key=timeout_algo.NON_KPI_SEED, actor="t")
        self.assertTrue(timeout_algo.kpi_rule_flags(conn)[timeout_algo.NON_KPI_SEED])
        conn.close()
