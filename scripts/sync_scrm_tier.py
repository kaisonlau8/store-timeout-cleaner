#!/usr/bin/env python3
"""分层 SCRM 同步入口。

  python scripts/sync_scrm_tier.py groups
  python scripts/sync_scrm_tier.py members
  python scripts/sync_scrm_tier.py full
  python scripts/sync_scrm_tier.py groups --dry-run

tier 锁被上一轮占用时记录 skip 并以 0 退出；真实失败以非 0 退出。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    from scrm_tiered import TIERS, _redact, plan_for_tier, run_tier

    parser = argparse.ArgumentParser(description="分层 SCRM 同步")
    parser.add_argument("tier", choices=TIERS)
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不请求 SCRM")
    args = parser.parse_args()

    if args.dry_run:
        print(json.dumps(plan_for_tier(args.tier, ROOT), ensure_ascii=False, indent=2))
        return 0

    try:
        result = run_tier(args.tier, root=ROOT)
    except Exception as exc:
        logging.error("%s 同步失败: %s", args.tier, _redact(exc))
        return 1

    if result.get("status") == "skip":
        logging.info("%s 跳过：上一轮仍在运行", args.tier)
        return 0

    counts = result.get("counts") or {}
    logging.info(
        "%s 同步完成 %s",
        args.tier,
        json.dumps(counts, ensure_ascii=False, sort_keys=True),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
