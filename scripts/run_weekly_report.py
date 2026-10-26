#!/usr/bin/env python3
"""本地周报入口；不触发 SCRM 同步。启动前加载本目录 .env（及可选 NSS 同应用凭证）。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scrm_sync import load_dotenv  # noqa: E402

load_dotenv(str(ROOT / ".env"))
# 兼容：若本目录未配飞书凭证，回读 NSS 的 APP_ID/APP_SECRET（不覆盖已有键）
nss_env = ROOT.parent / "NSS_Questionnaire_Reminder" / ".env"
load_dotenv(str(nss_env))

from weekly_report import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
