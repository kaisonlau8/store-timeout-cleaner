import unittest
from datetime import datetime, timedelta

from score_rules import (
    REASON_AT,
    REASON_BURST,
    REASON_CLOSE,
    REASON_DUP,
    REASON_LLM,
    REASON_STAFF,
    classify_events,
)


def ev(room, when, msg, msg_id=None, **extra):
    base = {
        "room": room,
        "t": when if isinstance(when, datetime) else datetime.fromisoformat(when),
        "msg": msg,
        "msg_id": msg_id or f"{room}|{when}|{msg}",
    }
    base.update(extra)
    return base


T0 = datetime(2026, 7, 1, 10, 0, 0)


class ClassifyEventTests(unittest.TestCase):
    def test_duplicate_message_keeps_first(self):
        rows = classify_events(
            [
                ev("群A", T0, "刹车有异响", "m1"),
                ev("群A", T0, "刹车有异响", "m1"),
            ]
        )
        self.assertEqual(rows[0]["reason"], "")
        self.assertEqual(rows[1]["reason"], REASON_DUP)

    def test_staff_script(self):
        rows = classify_events([ev("群A", T0, "您好！我们正在为您安排顾问")])
        self.assertEqual(rows[0]["reason"], REASON_STAFF)

    def test_close_and_emoji(self):
        rows = classify_events(
            [
                ev("群A", T0, "好的"),
                ev("群A", T0 + timedelta(hours=2), "😊"),
                ev("群A", T0 + timedelta(hours=3), "谢谢老板"),
            ]
        )
        self.assertEqual([r["reason"] for r in rows], [REASON_CLOSE, REASON_CLOSE, REASON_CLOSE])

    def test_bracket_message_is_not_small_talk(self):
        rows = classify_events([ev("群A", T0, "【到店】好的，我下午过去")])
        self.assertEqual(rows[0]["reason"], "")

    def test_at_only(self):
        rows = classify_events(
            [
                ev("群A", T0, "@顾问"),
                ev("群A", T0 + timedelta(hours=1), "@顾问 车还没好"),
            ]
        )
        self.assertEqual(rows[0]["reason"], REASON_AT)
        self.assertEqual(rows[1]["reason"], "")

    def test_burst_within_30_minutes_of_last_counted(self):
        rows = classify_events(
            [
                ev("群A", T0, "刹车有异响"),
                ev("群A", T0 + timedelta(minutes=20), "好的"),
                ev("群A", T0 + timedelta(minutes=25), "还是没人回"),
                ev("群B", T0 + timedelta(minutes=10), "另一家的问题"),
                ev("群A", T0 + timedelta(minutes=31), "超过三十分钟"),
            ]
        )
        self.assertEqual(rows[0]["reason"], "")
        self.assertEqual(rows[1]["reason"], REASON_CLOSE)
        self.assertEqual(rows[2]["reason"], REASON_BURST)
        self.assertEqual(rows[3]["reason"], "")
        self.assertEqual(rows[4]["reason"], "")

    def test_burst_boundary_is_inclusive_1800_seconds(self):
        rows = classify_events(
            [
                ev("群A", T0, "第一句"),
                ev("群A", T0 + timedelta(seconds=1800), "刚好三十分钟"),
                ev("群A", T0 + timedelta(seconds=1800 + 1801), "再过三十多分钟"),
            ]
        )
        self.assertEqual(rows[1]["reason"], REASON_BURST)
        self.assertEqual(rows[2]["reason"], "")

    def test_missing_message_skips_content_rules_but_still_dedupes(self):
        rows = classify_events(
            [
                ev("群A", T0, None, "same"),
                ev("群A", T0, None, "same"),
                ev("群A", T0 + timedelta(minutes=5), None, "other"),
            ]
        )
        self.assertEqual(rows[0]["reason"], "")
        self.assertEqual(rows[1]["reason"], REASON_DUP)
        self.assertEqual(rows[2]["reason"], REASON_BURST)

    def test_llm_flag_only_applies_when_rules_keep_the_event(self):
        rows = classify_events(
            [
                ev("群A", T0, "想问下保养周期", llm_exclude=True),
                ev("群A", T0 + timedelta(hours=2), "好的", llm_exclude=True),
            ]
        )
        self.assertEqual(rows[0]["reason"], REASON_LLM)
        self.assertEqual(rows[1]["reason"], REASON_CLOSE)

    def test_input_order_preserved(self):
        later = ev("群A", T0 + timedelta(minutes=5), "后写但更早录入", "b")
        earlier = ev("群A", T0, "先发生", "a")
        rows = classify_events([later, earlier])
        self.assertEqual([r["msg_id"] for r in rows], ["b", "a"])
        self.assertEqual(rows[0]["reason"], REASON_BURST)
        self.assertEqual(rows[1]["reason"], "")


if __name__ == "__main__":
    unittest.main()
