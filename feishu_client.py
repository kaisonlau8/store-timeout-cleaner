"""飞书群文本发送。仅显式发送入口调用，不落盘凭证、不重试消息。"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from scrm_sync import load_dotenv

load_dotenv(str(Path(__file__).resolve().parent / ".env"))
load_dotenv(str(Path(__file__).resolve().parent.parent / "NSS_Questionnaire_Reminder" / ".env"))

BASE_URL = "https://open.feishu.cn/open-apis"


def _post(path, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        BASE_URL + path, data=json.dumps(payload, ensure_ascii=False).encode(),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
    except (OSError, ValueError):
        raise RuntimeError("飞书请求失败（未记录请求或响应中的凭证）") from None
    if result.get("code") != 0:
        raise RuntimeError("飞书接口返回失败")
    return result


def send_chat_text(chat_id, text):
    if not chat_id or not chat_id.strip():
        raise ValueError("缺少飞书 chat_id")
    app_id = os.environ.get("FEISHU_APP_ID") or os.environ.get("APP_ID")
    secret = os.environ.get("FEISHU_APP_SECRET") or os.environ.get("APP_SECRET")
    if not app_id or not secret:
        raise RuntimeError("缺少飞书应用环境变量")
    token = _post("/auth/v3/tenant_access_token/internal", {
        "app_id": app_id, "app_secret": secret,
    })["tenant_access_token"]
    return _post("/im/v1/messages?receive_id_type=chat_id", {
        "receive_id": chat_id.strip(), "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }, token).get("data") or {}
