import unittest
from datetime import datetime, time
from zoneinfo import ZoneInfo

from scrm_sync import extract_store, extract_theater
from timeout_detect import TimeoutRule, detect_timeout_events, in_rule_window, rule_from_detail

SHANGHAI = ZoneInfo("Asia/Shanghai")


def ts(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=SHANGHAI).timestamp() * 1000)


def msg(msgid: str, room: str, sender: str, when: str, msgtype: str = "text") -> dict:
    return {
        "msgid": msgid,
        "room_id": room,
        "from": sender,
        "msgtime": ts(when),
        "msgtype": msgtype,
    }


class ExtractTests(unittest.TestCase):
    def test_store_and_theater(self):
        path = "猛士科技/门店/武汉特区/武汉一部/猛士武汉汉口店/销售"
        self.assertEqual(extract_store(path), "猛士武汉汉口店")
        self.assertEqual(extract_theater(path), "武汉特区")

    def test_store_directly_under_men_dian(self):
        path = "猛士科技/门店/猛士武汉汉口店"
        self.assertEqual(extract_store(path), "猛士武汉汉口店")
        self.assertIsNone(extract_theater(path))


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.ten = TimeoutRule(
            rule_id="10",
            rule_name="10分钟",
            minutes=10,
            days=frozenset(range(1, 8)),
            start=time(9, 0),
            end=time(20, 0),
            rule_type=2,
        )
        self.thirty = TimeoutRule(
            rule_id="30",
            rule_name="30分钟",
            minutes=30,
            days=frozenset({1, 2, 3, 4, 5}),
            start=time(9, 0),
            end=time(20, 0),
            rule_type=2,
        )
        self.rooms = ["room-a"]
        self.staff = ["1001"]

    def test_no_reply_hits_both_rules(self):
        messages = [msg("m1", "room-a", "ext-1", "2026-08-12T10:00:00")]
        events = detect_timeout_events(messages, [self.ten, self.thirty], self.staff, self.rooms)
        self.assertEqual({e["rule_id"] for e in events}, {"10", "30"})
        self.assertEqual({e["msg_id"] for e in events}, {"m1"})

    def test_staff_reply_cancels(self):
        messages = [
            msg("m1", "room-a", "ext-1", "2026-08-12T10:00:00"),
            msg("m2", "room-a", "1001", "2026-08-12T10:05:00"),
        ]
        events = detect_timeout_events(messages, [self.ten, self.thirty], self.staff, self.rooms)
        self.assertEqual(events, [])

    def test_late_reply_still_counts_ten_not_thirty(self):
        messages = [
            msg("m1", "room-a", "ext-1", "2026-08-12T10:00:00"),
            msg("m2", "room-a", "1001", "2026-08-12T10:15:00"),
        ]
        events = detect_timeout_events(messages, [self.ten, self.thirty], self.staff, self.rooms)
        self.assertEqual([e["rule_id"] for e in events], ["10"])

    def test_outside_hours(self):
        messages = [msg("m1", "room-a", "ext-1", "2026-08-12T08:00:00")]
        events = detect_timeout_events(messages, [self.ten], self.staff, self.rooms)
        self.assertEqual(events, [])

    def test_deadline_after_window(self):
        messages = [msg("m1", "room-a", "ext-1", "2026-08-12T19:55:00")]
        events = detect_timeout_events(messages, [self.ten], self.staff, self.rooms)
        self.assertEqual(events, [])

    def test_weekend_skips_weekday_rule(self):
        # 2026-08-08 is Saturday
        messages = [msg("m1", "room-a", "ext-1", "2026-08-08T10:00:00")]
        events = detect_timeout_events(messages, [self.ten, self.thirty], self.staff, self.rooms)
        self.assertEqual([e["rule_id"] for e in events], ["10"])

    def test_ignores_non_robot_room(self):
        messages = [msg("m1", "room-b", "ext-1", "2026-08-12T10:00:00")]
        events = detect_timeout_events(messages, [self.ten], self.staff, self.rooms)
        self.assertEqual(events, [])

    def test_rule_from_detail(self):
        rule = rule_from_detail(
            {
                "rule_id": "203",
                "rule_name": "10 分钟未回复预警",
                "chat_time_out": 10,
                "receive_days": [1, 2, 3, 4, 5, 6, 7],
                "receive_start_time": "09:00:00",
                "receive_end_time": "20:00:00",
                "rule_type": 2,
            }
        )
        self.assertIsNotNone(rule)
        self.assertTrue(in_rule_window(datetime(2026, 8, 12, 9, 0, tzinfo=SHANGHAI), rule))
        self.assertFalse(in_rule_window(datetime(2026, 8, 12, 20, 1, tzinfo=SHANGHAI), rule))


if __name__ == "__main__":
    unittest.main()
