"""门店超时收缩率、分档和特殊店。

率 = (有效超时 + M×μ) / (机器人群数 + M) × 100，M 默认 20。
μ = 参与排名门店的有效超时合计 / 机器人群合计。
按率从低到高排名，并列用 min（同率都取较好的那一档）：
名次 ≤ 20%×店数 → 100，≤ 50%×店数 → 80，其余 60。
"""

from __future__ import annotations

from dataclasses import dataclass

M_DEFAULT = 20

# 自身没有机器人群的交付店，并入同城零售中心的群数和超时。
MERGE_MAP = {
    "猛士徐州泉山交付服务中心": "猛士汽车徐州泉山零售中心",
    "猛士六安金安交付服务中心": "猛士汽车六安金安零售中心",
    "猛士杭州西溪交付服务中心": "猛士汽车杭州西溪零售中心",
    "猛士上海闵行交付服务中心": "猛士汽车上海闵行零售中心",
}

TIER_TOP = "前20%"
TIER_MID = "20-50%"
TIER_REST = "后50%"
TIER_ZERO = "未接入机器人"
TIER_AVG = "SCRM无店名/平均分"


@dataclass
class StoreInput:
    name: str
    code: str = ""
    theater: str = ""
    group_count: int = 0
    robot_group_count: int = 0
    raw: int = 0
    valid: int = 0
    in_scrm: bool = True


def score_stores(rows: list[StoreInput], *, m: int = M_DEFAULT) -> list[dict]:
    by_name = {row.name: row for row in rows}
    prepared: list[dict] = []
    for row in rows:
        prepared.append(_prepare(row, by_name))

    ranked = [item for item in prepared if item["kind"] in ("", "merge")]
    group_sum = sum(int(item["robot_group_count"]) for item in ranked)
    valid_sum = sum(int(item["valid"]) for item in ranked)
    mu = (valid_sum / group_sum) if group_sum else 0.0

    for item in prepared:
        robots = int(item["robot_group_count"])
        denom = robots + m
        item["rate"] = ((int(item["valid"]) + m * mu) / denom * 100) if denom else None
        item["rank"] = None
        item["tier"] = ""
        item["score"] = None

    ranks = _rank_min([float(item["rate"]) for item in ranked])
    n = len(ranked)
    for item, rank in zip(ranked, ranks):
        item["rank"] = rank
        if rank <= 0.2 * n:
            item["tier"] = TIER_TOP
            item["score"] = 100
        elif rank <= 0.5 * n:
            item["tier"] = TIER_MID
            item["score"] = 80
        else:
            item["tier"] = TIER_REST
            item["score"] = 60

    avg_score = (sum(item["score"] for item in ranked) / n) if n else 0.0
    for item in prepared:
        if item["kind"] == "zero":
            item["score"] = 0
            item["tier"] = TIER_ZERO
            item["rank"] = None
        elif item["kind"] == "avg":
            item["score"] = avg_score
            item["tier"] = TIER_AVG
            item["rank"] = None
        item["mu"] = mu
        item["m"] = m
    return prepared


def _prepare(row: StoreInput, by_name: dict[str, StoreInput]) -> dict:
    target_name = MERGE_MAP.get(row.name)
    target = by_name.get(target_name) if target_name else None
    robots = int(row.robot_group_count or 0)
    valid = int(row.valid or 0)
    raw = int(row.raw or 0)
    groups = int(row.group_count or 0)
    kind = ""
    remark = ""
    if target is not None and robots <= 0:
        robots += int(target.robot_group_count or 0)
        valid += int(target.valid or 0)
        raw += int(target.raw or 0)
        kind = "merge"
        remark = f"本店无机器人群，合并同城零售中心「{target.name}」的群数与超时"
    elif groups > 0 and robots <= 0:
        kind = "zero"
        remark = "有客户群但未接入机器人"
    elif not row.in_scrm:
        kind = "avg"
        remark = "SCRM 未找到该店，得分取已排名门店的平均分"
    elif robots <= 0:
        kind = "zero"
        remark = "有客户群但未接入机器人"

    return {
        "name": row.name,
        "code": row.code or "",
        "theater": row.theater or "",
        "group_count": groups,
        "robot_group_count": robots,
        "raw": raw,
        "valid": valid,
        "kind": kind,
        "remark": remark,
    }


def _rank_min(values: list[float]) -> list[int]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = i + 1
        for k in range(i, j + 1):
            ranks[order[k]] = rank
        i = j + 1
    return ranks
