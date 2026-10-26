#!/usr/bin/env python3
"""本地周报入口；不触发 SCRM 同步。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weekly_report import main

if __name__ == "__main__":
    raise SystemExit(main())
