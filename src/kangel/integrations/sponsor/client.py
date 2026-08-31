"""爱发电（afdian）开放 API 客户端。

只用到 query-sponsor 一个接口：拉取赞助者列表用于站内感谢墙。
凭据（user_id / token）只在服务端使用，禁止出现在响应或日志中。

签名规则（爱发电开放 API）：
    sign = md5(token + "params" + params + "ts" + ts + "user_id" + user_id)
其中 params 必须是**同一个** JSON 字符串：既用于计算签名，也原样放进请求体。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Any

from config import settings

API_BASE = "https://afdian.com/api/open"
QUERY_SPONSOR_PATH = "/query-sponsor"


class AfdianError(Exception):
    """受控的爱发电调用失败；code 用于同步状态记录，不含任何凭据。"""

    expected_business_error = True

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


def build_sign(token: str, user_id: str, params: str, ts: int) -> str:
    """按爱发电规则计算 MD5 签名。"""
    raw = f"{token}params{params}ts{ts}user_id{user_id}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class AfdianClient:
    def __init__(self, clock=None):
        # clock 返回 unix 秒；注入后签名可被确定性地测试。
        self.clock = clock or (lambda: int(time.time()))

    def build_request_body(self, page: int) -> dict[str, Any]:
        config = settings.sponsor
        user_id = config.afdian_user_id.strip()
        token = config.afdian_token.strip()
        if not user_id or not token:
            raise AfdianError("missing_credentials", "爱发电凭据未配置")
        # 序列化一次并复用，保证签名串与传输串完全一致。
        params = json.dumps({"page": page}, separators=(",", ":"))
        ts = self.clock()
        return {
            "user_id": user_id,
            "params": params,
            "ts": ts,
            "sign": build_sign(token, user_id, params, ts),
        }

    def query_sponsor_page(self, page: int) -> dict[str, Any]:
        """拉取一页赞助者；返回 data 部分（含 list / total_count / total_page）。"""
        body = self.build_request_body(page)
        payload = self._post_json_sync(
            f"{API_BASE}{QUERY_SPONSOR_PATH}",
            body,
            settings.sponsor.sync_timeout_seconds,
        )
        if not isinstance(payload, dict):
            raise AfdianError("invalid_response", "爱发电返回体不是对象")
        if payload.get("ec") != 200:
            # em 由爱发电返回，可能含中文说明；不含我方凭据。
            raise AfdianError(
                "api_error", f"ec={payload.get('ec')} em={payload.get('em')}"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise AfdianError("invalid_response", "爱发电返回缺少 data")
        return data

    @staticmethod
    def _post_json_sync(url: str, payload: dict, timeout: int) -> Any:
        """阻塞式 POST；与 integrations/ai 一致，只用标准库，不新增依赖。

        调用方负责用 asyncio.to_thread 包装。
        """
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise AfdianError("http_error", f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise AfdianError("network_error", str(exc.reason)) from exc
        except TimeoutError as exc:
            raise AfdianError("timeout", "请求超时") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AfdianError("invalid_json", "响应不是合法 JSON") from exc


afdian_client = AfdianClient()
