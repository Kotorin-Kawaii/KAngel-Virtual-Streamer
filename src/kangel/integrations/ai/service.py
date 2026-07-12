"""OpenAI-compatible chat completions client."""

import asyncio
import json
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

from config import settings


class AIService:
    """统一调用支持 OpenAI Chat Completions 格式的模型服务。"""

    @staticmethod
    def _post_json_sync(
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout: int,
    ) -> Dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    async def _call_model(
        self,
        *,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float,
        stream: bool,
        response_format: Optional[Dict[str, Any]],
        timeout: int,
    ) -> str:
        if stream:
            raise ValueError("当前服务端只支持非流式 OpenAI-compatible 响应")

        url = settings.ai.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {settings.ai.api_key}"}
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }

        # 兼容供应商对 JSON Schema 的实现并不统一；调用方会继续在后端校验。
        _ = response_format
        data = await asyncio.to_thread(
            self._post_json_sync, url, headers, payload, timeout
        )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("OpenAI-compatible 响应缺少 choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ValueError("OpenAI-compatible 响应缺少 choices[0].message")
        content = message.get("content")
        if content is None:
            raise ValueError("OpenAI-compatible 响应缺少 message.content")
        return str(content)

    async def run(
        self,
        *,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
        stream: bool = False,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        selected_model = model or settings.ai.default_model
        selected_temperature = settings.ai.temperature if temperature is None else temperature
        reply = await self._call_model(
            model=selected_model,
            messages=messages,
            temperature=selected_temperature,
            stream=stream,
            response_format=response_format,
            timeout=timeout or settings.ai.timeout,
        )
        return {
            "reply": reply,
            "model": selected_model,
            "message_id": str(uuid.uuid4()),
        }


ai_service = AIService()
