#!/usr/bin/env python3
"""扫描 input_file/ 中管家导出的回复超时记录 Excel 并登记。"""

from __future__ import annotations

import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from timeout_excel import scan_input_dir  # noqa: E402


def main() -> int:
    summary = scan_input_dir(ROOT)
    logging.info(
        "扫描完成 imported=%s skipped=%s errors=%s",
        len(summary.get("imported") or []),
        len(summary.get("skipped") or []),
        len(summary.get("errors") or []),
    )
    for item in summary.get("imported") or []:
        logging.info(
            "  %s %s~%s events=%s unmatched=%s",
            item.get("filename"),
            item.get("range_start"),
            item.get("range_end"),
            item.get("events"),
            item.get("unmatched"),
        )
    for err in summary.get("errors") or []:
        logging.warning("  %s %s", err.get("filename"), err.get("error"))
    return 1 if summary.get("errors") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        logging.exception("超时 Excel 导入失败")
        raise SystemExit(1)
