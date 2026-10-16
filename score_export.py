"""门店评分表导出。只含汇总，不含客户消息正文。"""

from __future__ import annotations

import csv
import io
from typing import Any

COLUMNS = (
    ("name", "门店名称"),
    ("code", "门店编码"),
    ("theater", "战区"),
    ("robot_group_count", "机器人群数"),
    ("raw", "原始超时"),
    ("valid", "有效超时"),
    ("llm_excluded", "LLM剔除"),
    ("rate", "超时率"),
    ("rank", "排名"),
    ("tier", "分档"),
    ("score", "得分"),
    ("remark", "备注"),
)


def stores_to_csv(stores: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([title for _, title in COLUMNS])
    for row in stores:
        writer.writerow([_cell(row, key) for key, _ in COLUMNS])
    return buffer.getvalue().encode("utf-8-sig")


def stores_to_xlsx(stores: list[dict[str, Any]]) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "门店评分"
    sheet.append([title for _, title in COLUMNS])
    for row in stores:
        sheet.append([_cell(row, key) for key, _ in COLUMNS])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _cell(row: dict[str, Any], key: str) -> Any:
    if key == "rate":
        if row.get("kind") in ("zero", "avg") or row.get("rate") is None:
            return ""
        return round(float(row["rate"]), 2)
    if key == "rank":
        return "" if row.get("rank") is None else int(row["rank"])
    if key == "score":
        score = row.get("score")
        if score is None:
            return ""
        value = float(score)
        if abs(value - round(value)) < 1e-9:
            return int(round(value))
        return round(value, 2)
    if key == "code":
        return row.get("code") or ""
    if key == "llm_excluded":
        return int(row.get("llm_excluded") or 0)
    return row.get(key, "")
