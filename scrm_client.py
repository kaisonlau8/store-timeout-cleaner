"""企微 SCRM 生产 OpenAPI 客户端（基线 /openapi）。"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ScrmError(RuntimeError):
    pass


class ScrmClient:
    def __init__(self, base_url: str, app_id: str, app_secret: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout = timeout
        self._token: str | None = None
        self._token_expire_at = 0.0

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        body: Any = None,
        with_token: bool = True,
    ) -> dict[str, Any]:
        query = dict(params or {})
        if with_token:
            query["access_token"] = self.access_token()
        url = self.base_url + path
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)

        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_err: Exception | None = None
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_err = exc
                time.sleep(0.6 * (attempt + 1))
        else:
            raise ScrmError(f"请求失败 {method} {path}: {last_err}") from last_err

        if not isinstance(payload, dict):
            raise ScrmError(f"响应不是 JSON 对象: {path}")
        code = payload.get("code")
        if code not in (0, "0", None):
            raise ScrmError(f"{path} 返回 code={code} msg={payload.get('msg')}")
        return payload

    def access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expire_at:
            return self._token
        payload = self._request(
            "/openapi/access_token",
            params={"appid": self.app_id, "app_secret": self.app_secret},
            with_token=False,
        )
        data = payload.get("data") or {}
        token = data.get("access_token")
        if not token:
            raise ScrmError("未拿到 access_token")
        expires_in = int(data.get("expires_in") or 7200)
        self._token = str(token)
        self._token_expire_at = now + max(60, expires_in - 120)
        return self._token

    def paginate_get(
        self,
        path: str,
        *,
        extra_params: dict[str, Any] | None = None,
        page_size: int = 100,
        records_key: str = "records",
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            params = dict(extra_params or {})
            params["current_index"] = page
            params["page_size"] = page_size
            payload = self._request(path, params=params)
            data = payload.get("data") or {}
            chunk = data.get(records_key) or []
            if not isinstance(chunk, list):
                break
            records.extend(chunk)
            total = int(data.get("total") or 0)
            if not chunk or len(records) >= total or len(chunk) < page_size:
                break
            page += 1
        return records

    def paginate_post(
        self,
        path: str,
        *,
        body_base: dict[str, Any] | None = None,
        page_size: int = 100,
        records_key: str = "records",
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            body = dict(body_base or {})
            body["current_index"] = page
            body["page_size"] = page_size
            payload = self._request(path, method="POST", body=body)
            data = payload.get("data") or {}
            chunk = data.get(records_key) or []
            if not isinstance(chunk, list):
                break
            records.extend(chunk)
            total = int(data.get("total") or 0)
            if not chunk or len(records) >= total or len(chunk) < page_size:
                break
            page += 1
        return records

    def list_groups(self, **filters: Any) -> list[dict[str, Any]]:
        extra = {k: v for k, v in filters.items() if v not in (None, "")}
        return self.paginate_get("/openapi/chat/group/list", extra_params=extra)

    def iter_messages(
        self,
        *,
        msg_time_start: int,
        msg_time_end: int,
        page_size: int = 100,
        last_seq: int | None = None,
    ):
        """按时间窗拉取会话存档。不传 type，拿到租户全量流后再在本地过滤。"""
        while True:
            params: dict[str, Any] = {
                "page_size": page_size,
                "msg_time_start": int(msg_time_start),
                "msg_time_end": int(msg_time_end),
            }
            if last_seq is not None:
                params["last_seq"] = last_seq
            payload = self._request("/openapi/chat/message/list", params=params)
            recs = (payload.get("data") or {}).get("records") or []
            if not recs:
                break
            yield from recs
            new_seq = recs[-1].get("seq")
            if new_seq is None or new_seq == last_seq:
                break
            last_seq = new_seq
            if len(recs) < page_size:
                break
            time.sleep(0.03)

    def list_timeout_rules(self) -> list[dict[str, Any]]:
        return self.paginate_post("/openapi/chat/timeout/rule_list_page", body_base={"keyword": ""})

    def get_timeout_rule_detail(self, rule_id: str) -> dict[str, Any]:
        payload = self._request(
            "/openapi/chat/timeout/rule_detail",
            method="POST",
            body={"rule_id": str(rule_id)},
        )
        data = payload.get("data") or {}
        return data if isinstance(data, dict) else {}

    def list_users(self) -> list[dict[str, Any]]:
        return self.paginate_get("/openapi/department/user/list")

    def list_departments(self) -> list[dict[str, Any]]:
        payload = self._request(
            "/openapi/department/info/list",
            method="POST",
            body={},
        )
        data = payload.get("data") or []
        if isinstance(data, list):
            return data
        return data.get("records") or []
