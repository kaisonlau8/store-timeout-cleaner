import unittest

from score_math import MERGE_MAP, StoreInput, score_stores


def store(name, robots, valid, groups=None, raw=None, **extra):
    return StoreInput(
        name=name,
        group_count=groups if groups is not None else robots,
        robot_group_count=robots,
        valid=valid,
        raw=valid if raw is None else raw,
        **extra,
    )


class ScoreMathTests(unittest.TestCase):
    def test_shrinkage_rate_bands_and_tie_takes_better_band(self):
        rows = score_stores(
            [
                store("甲", 80, 0),
                store("乙", 80, 0),
                store("丙", 80, 8),
                store("丁", 80, 16),
                store("戊", 80, 24),
            ]
        )
        by = {r["name"]: r for r in rows}
        # μ = 48/400 = 0.12；率 = 有效 + 2.4
        self.assertAlmostEqual(by["甲"]["rate"], 2.4)
        self.assertAlmostEqual(by["乙"]["rate"], 2.4)
        self.assertAlmostEqual(by["丙"]["rate"], 10.4)
        self.assertEqual(by["甲"]["rank"], 1)
        self.assertEqual(by["乙"]["rank"], 1)
        self.assertEqual(by["丙"]["rank"], 3)
        self.assertEqual(by["甲"]["tier"], "前20%")
        self.assertEqual(by["乙"]["score"], 100)
        self.assertEqual(by["丙"]["tier"], "后50%")
        self.assertEqual(by["丙"]["score"], 60)
        self.assertEqual(by["戊"]["score"], 60)
        self.assertEqual(by["丁"]["score"], 60)

    def test_zero_robot_store_scores_zero_and_stays_out_of_mu(self):
        rows = score_stores(
            [
                store("甲", 80, 0),
                store("未接入", 0, 100, groups=6, raw=120),
            ]
        )
        by = {r["name"]: r for r in rows}
        self.assertAlmostEqual(by["甲"]["rate"], 0.0)  # μ=0，未接入店不进入分母
        self.assertEqual(by["未接入"]["kind"], "zero")
        self.assertEqual(by["未接入"]["score"], 0)
        self.assertEqual(by["未接入"]["tier"], "未接入机器人")
        self.assertIsNone(by["未接入"]["rank"])
        self.assertIn("未接入", by["未接入"]["remark"])

    def test_store_missing_from_scrm_gets_average_score(self):
        rows = score_stores(
            [
                store("甲", 80, 0),
                store("乙", 80, 0),
                store("丙", 80, 8),
                store("丁", 80, 16),
                store("戊", 80, 24),
                StoreInput(name="档案外", in_scrm=False, group_count=0, robot_group_count=0, valid=3, raw=4),
            ]
        )
        by = {r["name"]: r for r in rows}
        ranked_mean = (100 + 100 + 60 + 60 + 60) / 5
        self.assertEqual(by["档案外"]["kind"], "avg")
        self.assertAlmostEqual(by["档案外"]["score"], ranked_mean)
        self.assertEqual(by["档案外"]["tier"], "SCRM无店名/平均分")
        self.assertIsNone(by["档案外"]["rank"])
        self.assertAlmostEqual(by["甲"]["rate"], 2.4)
        self.assertEqual(by["档案外"]["valid"], 3)

    def test_delivery_without_robots_shares_retail_center(self):
        delivery = "猛士杭州西溪交付服务中心"
        center = MERGE_MAP[delivery]
        rows = score_stores(
            [
                store(center, 50, 5, groups=10, raw=9, theater="华东战区"),
                store(delivery, 0, 1, groups=2, raw=2, theater="华东战区"),
                store("普通店", 50, 5, groups=50, raw=5),
            ]
        )
        by = {r["name"]: r for r in rows}
        self.assertEqual(by[delivery]["kind"], "merge")
        self.assertEqual(by[delivery]["robot_group_count"], 50)
        self.assertEqual(by[delivery]["valid"], 6)
        self.assertEqual(by[delivery]["raw"], 11)
        self.assertEqual(by[center]["robot_group_count"], 50)
        self.assertEqual(by[center]["valid"], 5)
        self.assertNotEqual(by[delivery]["score"], 0)
        self.assertEqual(by[delivery]["score"], 60)
        self.assertEqual(by[center]["score"], 80)
        self.assertIn(center, by[delivery]["remark"])

    def test_delivery_with_own_robots_is_not_merged(self):
        delivery = "猛士上海闵行交付服务中心"
        center = MERGE_MAP[delivery]
        rows = score_stores(
            [
                store(center, 40, 4),
                store(delivery, 10, 1, groups=12),
            ]
        )
        by = {r["name"]: r for r in rows}
        self.assertEqual(by[delivery]["kind"], "")
        self.assertEqual(by[delivery]["robot_group_count"], 10)
        self.assertEqual(by[delivery]["valid"], 1)

    def test_delivery_without_center_falls_back_to_zero(self):
        delivery = "猛士徐州泉山交付服务中心"
        rows = score_stores([store(delivery, 0, 2, groups=3, raw=2)])
        self.assertEqual(rows[0]["kind"], "zero")
        self.assertEqual(rows[0]["score"], 0)

    def test_no_ranked_stores_average_is_zero(self):
        rows = score_stores(
            [
                store("空店", 0, 1, groups=2),
                StoreInput(name="档案外", in_scrm=False),
            ]
        )
        by = {r["name"]: r for r in rows}
        self.assertEqual(by["空店"]["score"], 0)
        self.assertEqual(by["档案外"]["score"], 0)


if __name__ == "__main__":
    unittest.main()
