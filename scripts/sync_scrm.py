#!/usr/bin/env python3
"""从企微 SCRM 全量同步客户群统计。由 launchd 每天 00:00 / 12:00 调用。"""

from __future__ import annotations

import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from app import run_sync  # noqa: E402


def main() -> int:
    summary = run_sync()
    logging.info(
        "同步完成 all=%s valid=%s filter=%s robots=%s",
        summary.get("all_total"),
        summary.get("all_valid"),
        summary.get("filter_total"),
        ",".join(r.get("username", "") for r in summary.get("robots") or []),
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        logging.exception("SCRM 同步失败")
        raise SystemExit(1)
