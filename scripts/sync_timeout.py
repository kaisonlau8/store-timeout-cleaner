#!/usr/bin/env python3
"""按会话存档计算超时（回填/重算）。定时任务走 sync_scrm.py（拉群后自动计算）。

  python scripts/sync_timeout.py                    # 缺失天 + 最近 2 天
  python scripts/sync_timeout.py --start 2026-05-01 --refetch   # 全量重拉重算
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from app import run_timeout_sync  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--refetch", action="store_true", help="区间内全部重新拉取会话存档")
    ap.add_argument("--no-fetch", action="store_true", help="只用本地缓存重算")
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    result = run_timeout_sync(
        start=date.fromisoformat(a.start) if a.start else None,
        end=date.fromisoformat(a.end) if a.end else None,
        fetch=not a.no_fetch, refetch_all=a.refetch, llm=not a.no_llm, label="cli",
    )
    logging.info("超时计算完成 %s", json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        logging.exception("超时计算失败")
        raise SystemExit(1)
