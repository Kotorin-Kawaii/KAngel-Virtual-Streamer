"""OpenAI-compatible chat completions client with multi-provider support."""

import asyncio
import json
import logging
import socket
import time
import urllib.request
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from config import settings
from config.settings import AIProvider

from kangel.infrastructure.timing_trace import record_attempt_current

from .token_audit import token_audit_recorder

logger = logging.getLogger(__name__)

# 角色标识：调用方通过 role 选择供应商链，而非硬编码模型名。
Role = str  # 包括回复、分析、记忆及可选 stream_director/viewer_impression 角色

IMPRESSION_ROLES = frozenset({
    "viewer_memory_archaeologist", "viewer_impression_synthesizer",
    "viewer_impression", "viewer_impression_critic",
})

_VALID_ROLES = frozenset({
    "default", "qa_selector", "danmaku_selector", "impact_analysis", "intent_shadow",
    "moderation", "session_memory", "stream_director", "viewer_impression",
}) | IMPRESSION_ROLES

# Do not rely on urllib's default ``Python-urllib/*`` User-Agent.  Some
# OpenAI-compatible gateways reject that generic client signature at the WAF
# layer before the request reaches their API implementation.
_USER_AGENT = "KAngel-Server/0.1"

# Impression requests can last minutes. They must not occupy the default
# asyncio executor used by live reply HTTP/database work. The semaphore stays
# held until the physical request finishes, even if its coroutine is cancelled.
_IMPRESSION_HTTP_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="impression-http")
_IMPRESSION_HTTP_SLOTS = threading.BoundedSemaphore(4)


class AIBackgroundBusy(RuntimeError):
    pass


def _parse_hhmm(value: str) -> int:
    """将 HH:MM 转为当日分钟数（0-1439）。"""
    h, m = value.split(":")
    return int(h) * 60 + int(m)


class AIService:
    """统一调用支持 OpenAI Chat Completions 格式的模型服务。

    多供应商模式（providers 非空时生效）：
      1. 按启用状态和时间段过滤供应商
      2. 按 weight 降序排列
      3. 依次尝试，请求失败自动回退到下一供应商

    单供应商兼容模式（providers 为空时生效）：
      使用 base_url/api_key/default_model 及各角色 *_model 字段。
    """

    # ------------------------------------------------------------------
    #  内部工具
    # ------------------------------------------------------------------

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

    @staticmethod
    def _is_provider_active(provider: AIProvider, now: Optional[datetime] = None) -> bool:
        """检查供应商是否处于启用时间段内；支持跨午夜（如 22:00-06:00）。"""
        if not provider.enabled:
            return False
        now = now or datetime.now()
        current = now.hour * 60 + now.minute
        start = _parse_hhmm(provider.active_start)
        end = _parse_hhmm(provider.active_end)
        if start <= end:
            # 常规区间（如 08:00-22:00），端点包含
            return start <= current <= end
        # 跨午夜区间（如 22:00-06:00）
        return current >= start or current <= end

    @staticmethod
    def _get_model_for_role(provider: AIProvider, role: str) -> str:
        """根据角色获取模型；Viewer Impression 不允许静默回退普通主播模型。"""
        models = provider.models
        role_model = getattr(models, role, None)
        if role in IMPRESSION_ROLES:
            return role_model or ""
        return role_model or models.default

    def _select_providers(self, role: str) -> List[Tuple[AIProvider, str]]:
        """选择当前可用的 (provider, model_name) 列表，按 weight 降序排列。"""
        providers = settings.ai.providers
        if not providers:
            # 单供应商兼容模式：将旧字段映射为角色模型
            model_map = {
                "default": settings.ai.default_model,
                "qa_selector": settings.ai.qa_selector_model or settings.ai.default_model,
                "danmaku_selector": settings.ai.danmaku_selector_model or settings.ai.default_model,
                "impact_analysis": settings.ai.impact_analysis_model or settings.ai.default_model,
                "intent_shadow": settings.ai.intent_shadow_model or settings.ai.default_model,
                "moderation": settings.ai.moderation_model or settings.ai.default_model,
                "session_memory": settings.ai.session_memory_model or settings.ai.default_model,
                "stream_director": settings.ai.stream_director_model or settings.ai.default_model,
                "viewer_impression": settings.ai.viewer_impression_model,
            }
            model_map.update({r: getattr(settings.ai, f"{r}_model") for r in IMPRESSION_ROLES})
            model = model_map.get(role, settings.ai.default_model)
            if not model:
                # A role without an explicit model is intentionally unavailable
                # for privacy-sensitive side channels such as viewer_impression.
                # Do not expose a diagnostic route whose model is ``None``.
                return []
            pseudo = AIProvider(
                name="legacy",
                base_url=settings.ai.base_url,
                api_key=settings.ai.api_key,
            )
            return [(pseudo, model)]

        active: List[Tuple[AIProvider, str]] = []
        for p in providers:
            if not self._is_provider_active(p):
                continue
            model = self._get_model_for_role(p, role)
            if not model:
                continue
            active.append((p, model))

        active.sort(key=lambda x: x[0].weight, reverse=True)
        return active

    # ------------------------------------------------------------------
    #  公共 API
    # ------------------------------------------------------------------

    def has_role(self, role: str) -> bool:
        """检查是否有供应商能服务该角色；Viewer Impression 不含 default 回退。"""
        if role not in _VALID_ROLES:
            return False
        if not settings.ai.providers:
            model_map = {
                "default": settings.ai.default_model,
                "qa_selector": settings.ai.qa_selector_model,
                "danmaku_selector": settings.ai.danmaku_selector_model,
                "impact_analysis": settings.ai.impact_analysis_model,
                "intent_shadow": settings.ai.intent_shadow_model,
                "moderation": settings.ai.moderation_model,
                "session_memory": settings.ai.session_memory_model or settings.ai.default_model,
                "stream_director": settings.ai.stream_director_model or settings.ai.default_model,
                "viewer_impression": settings.ai.viewer_impression_model,
            }
            model_map.update({r: getattr(settings.ai, f"{r}_model") for r in IMPRESSION_ROLES})
            return bool(model_map.get(role))
        for p in settings.ai.providers:
            if p.enabled and self._get_model_for_role(p, role):
                return True
        return False

    def has_active_role(self, role: str) -> bool:
        """检查当前时刻是否有处于时间窗内的角色供应商。"""
        if role not in _VALID_ROLES:
            return False
        return bool(self._select_providers(role))

    def runtime_diagnostics(self) -> Dict[str, Any]:
        """返回无密钥的最终角色路由、timeout 与 reasoning 诊断。"""
        timeouts = {
            "default": settings.ai.timeout,
            "qa_selector": settings.ai.qa_selector_timeout,
            "danmaku_selector": settings.ai.danmaku_selector_timeout,
            "impact_analysis": settings.ai.impact_analysis_timeout,
            "intent_shadow": settings.ai.intent_shadow_timeout,
            "moderation": settings.ai.moderation_timeout,
            "session_memory": settings.ai.session_memory_timeout,
            "stream_director": settings.ai.stream_director_timeout,
            "viewer_impression": settings.ai.viewer_impression_timeout,
        }
        timeouts.update({r: getattr(settings.ai, f"{r}_timeout") for r in IMPRESSION_ROLES})
        roles: Dict[str, Any] = {}
        for role in sorted(_VALID_ROLES):
            roles[role] = {
                "timeout_seconds": int(timeouts[role]),
                "routes": [
                    {
                        "provider": provider.name,
                        "model": model,
                        "reasoning_effort": self._reasoning_effort(provider, role),
                    }
                    for provider, model in self._select_providers(role)
                ],
            }
        return {"roles": roles}

    async def _call_model(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float,
        stream: bool,
        response_format: Optional[Dict[str, Any]],
        timeout: int,
        reasoning_effort: Optional[str] = None,
        background: bool = False,
    ) -> Tuple[str, Optional[Dict[str, int]]]:
        """返回 (正文, token 用量)；用量缺失时第二项为 None（审计记为未上报）。"""
        if stream:
            raise ValueError("当前服务端只支持非流式 OpenAI-compatible 响应")

        url = base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": _USER_AGENT,
        }
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if reasoning_effort:
            # 仅由 provider capability 显式开启；不向所有兼容网关泄漏私有参数。
            payload["reasoning_effort"] = reasoning_effort

        # 兼容供应商对 JSON Schema 的实现并不统一；调用方会继续在后端校验。
        _ = response_format
        if background:
            if not _IMPRESSION_HTTP_SLOTS.acquire(blocking=False):
                raise AIBackgroundBusy("background_http_capacity")
            try:
                future = _IMPRESSION_HTTP_EXECUTOR.submit(self._post_json_sync, url, headers, payload, timeout)
            except BaseException:
                _IMPRESSION_HTTP_SLOTS.release()
                raise
            future.add_done_callback(lambda _: _IMPRESSION_HTTP_SLOTS.release())
            data = await asyncio.wrap_future(future)
        else:
            data = await asyncio.to_thread(self._post_json_sync, url, headers, payload, timeout)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("OpenAI-compatible 响应缺少 choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ValueError("OpenAI-compatible 响应缺少 choices[0].message")
        content = message.get("content")
        if content is None:
            raise ValueError("OpenAI-compatible 响应缺少 message.content")
        return str(content), self._parse_usage(data)

    @staticmethod
    def _reasoning_effort(provider: AIProvider, role: str) -> Optional[str]:
        if provider.reasoning_protocol != "openai":
            return None
        value = getattr(provider.reasoning, role, None) or provider.reasoning.default
        # OpenAI-compatible 常用关闭值是 none；配置层使用更直观的 off。
        return "none" if value == "off" else value

    @staticmethod
    def _parse_usage(data: Dict[str, Any]) -> Optional[Dict[str, int]]:
        """解析 OpenAI 兼容的 usage 块；缺失或不可用时返回 None，不猜数字。"""
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return None

        def as_int(*keys: str) -> Optional[int]:
            for key in keys:
                value = usage.get(key)
                if isinstance(value, bool):
                    continue
                if isinstance(value, int):
                    return value
                if isinstance(value, float):
                    return int(value)
            return None

        input_tokens = as_int("prompt_tokens", "input_tokens")
        output_tokens = as_int("completion_tokens", "output_tokens")
        total_tokens = as_int("total_tokens")
        if input_tokens is None and output_tokens is None and total_tokens is None:
            return None
        cached = 0
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            value = details.get("cached_tokens")
            if isinstance(value, int) and not isinstance(value, bool):
                cached = max(0, value)
        completion_details = usage.get("completion_tokens_details")
        reasoning_tokens: Optional[int] = None
        if isinstance(completion_details, dict):
            value = completion_details.get("reasoning_tokens")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                reasoning_tokens = max(0, int(value))
        if reasoning_tokens is None:
            value = usage.get("reasoning_tokens")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                reasoning_tokens = max(0, int(value))
        resolved_input = max(0, input_tokens or 0)
        resolved_output = max(0, output_tokens or 0)
        return {
            "input_tokens": resolved_input,
            "output_tokens": resolved_output,
            "cached_input_tokens": min(cached, resolved_input),
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": max(
                0, total_tokens if total_tokens is not None
                else resolved_input + resolved_output
            ),
        }

    @staticmethod
    def _error_kind(exc: Exception) -> str:
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError, socket.timeout)):
            return "provider_timeout"
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "provider_timeout"
        return type(exc).__name__

    @staticmethod
    def _audit(
        *, role: str, provider: str, model: str, status: str,
        started: float, usage: Optional[Dict[str, int]] = None,
        error_kind: Optional[str] = None,
    ) -> None:
        """记一次调用的 token 用量；审计失败绝不影响回复链路。"""
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            token_audit_recorder.record(
                role=role, provider=provider, model=model, status=status,
                usage=usage, error_kind=error_kind,
                latency_ms=latency_ms,
            )
        except Exception:  # pragma: no cover - 记账不允许向上冒泡
            logger.debug("P29 token 审计记录失败，已忽略")
        # 延迟优化 v1 §2：这里是唯一同时覆盖成功与失败的收口，所以 attempt 级
        # 时序也挂在这里——一次逻辑调用回退几家，就会自然产生几条 attempt。
        record_attempt_current(role=role, status=status, latency_ms=latency_ms)
        if role in IMPRESSION_ROLES:
            try:
                from kangel.memory.application.impression_metrics import impression_stage_metrics
                impression_stage_metrics.attempt(role=role, provider=provider, model=model,
                                                 status=status, latency_ms=latency_ms, usage=usage)
            except Exception:
                logger.debug("Impression stage aggregate audit unavailable")

    async def run(
        self,
        *,
        messages: List[Dict[str, str]],
        role: str = "default",
        model: Optional[str] = None,
        model_mode: str = "legacy",
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
        stream: bool = False,
        timeout: Optional[int] = None,
        background_preflight: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """调用模型生成回复。

        参数:
            role: 调用角色，决定供应商链中使用的模型名。
            model: 显式模型名覆盖（向后兼容）；默认走单供应商模式。
            model_mode: ``role_hint`` 表示 model 仅为角色模型的兼容提示，
                仍使用 role 对应的多供应商链；其他值保留旧显式覆盖语义。
        """
        selected_temperature = settings.ai.temperature if temperature is None else temperature
        effective_timeout = timeout or settings.ai.timeout

        # Streaming is a local protocol restriction, not a provider failure;
        # validate it before entering the fallback loop so callers receive the
        # stable ValueError contract.
        if stream:
            raise ValueError("当前服务端只支持非流式 OpenAI-compatible 响应")

        # 显式 model 仍走旧单供应商路径（向后兼容）
        if model is not None and model_mode != "role_hint":
            if role in IMPRESSION_ROLES and background_preflight is not None:
                await background_preflight()
            started = time.perf_counter()
            try:
                reply, usage = await self._call_model(
                    base_url=settings.ai.base_url,
                    api_key=settings.ai.api_key,
                    model=model,
                    messages=messages,
                    temperature=selected_temperature,
                    stream=stream,
                    response_format=response_format,
                    timeout=effective_timeout,
                    reasoning_effort=None,
                    background=role in IMPRESSION_ROLES,
                )
            except Exception as exc:
                self._audit(
                    role=role, provider="legacy", model=model, status="failed",
                    started=started, error_kind=self._error_kind(exc),
                )
                raise
            self._audit(
                role=role, provider="legacy", model=model, status="success",
                started=started, usage=usage,
            )
            return {
                "reply": reply,
                "model": model,
                "provider": "legacy",
                "message_id": str(uuid.uuid4()),
                "usage": usage,
            }

        # 多供应商模式：按权重选择，失败自动回退
        candidates = self._select_providers(role)
        if not candidates:
            raise RuntimeError(f"没有可用的供应商处理角色 '{role}'")

        last_error: Optional[Exception] = None
        for provider, model_name in candidates:
            if role in IMPRESSION_ROLES:
                # A foreground reply/SC may arrive while the previous provider
                # is timing out. Recheck before fallback, outside provider error
                # handling: yielding is not another failed model attempt.
                if background_preflight is not None:
                    await background_preflight()
                if not self._is_provider_active(provider):
                    continue
            started = time.perf_counter()
            try:
                reply, usage = await self._call_model(
                    base_url=provider.base_url,
                    api_key=provider.api_key,
                    model=model_name,
                    messages=messages,
                    temperature=selected_temperature,
                    stream=stream,
                    response_format=response_format,
                    timeout=effective_timeout,
                    reasoning_effort=self._reasoning_effort(provider, role),
                    background=role in IMPRESSION_ROLES,
                )
                self._audit(
                    role=role, provider=provider.name, model=model_name,
                    status="success", started=started, usage=usage,
                )
                return {
                    "reply": reply,
                    "model": model_name,
                    "provider": provider.name,
                    "message_id": str(uuid.uuid4()),
                    "usage": usage,
                }
            except AIBackgroundBusy:
                # Local capacity is not provider failure: never fan out more
                # fallback attempts while all background sockets are occupied.
                raise
            except Exception as exc:
                last_error = exc
                # 每次尝试各记一行：带回退的调用能直接看出重试成本。
                self._audit(
                    role=role, provider=provider.name, model=model_name,
                    status="failed", started=started, error_kind=self._error_kind(exc),
                )
                logger.warning(
                    "供应商 '%s' (模型 '%s', 角色 '%s') 请求失败，尝试回退: "
                    "timeout=%ss elapsed=%dms kind=%s",
                    provider.name, model_name, role,
                    effective_timeout,
                    int((time.perf_counter() - started) * 1000),
                    self._error_kind(exc),
                )
                continue

        # A protocol-shape ValueError is useful to direct callers and keeps
        # the historical single-call contract; transport/provider failures
        # remain wrapped so the role-level fallback boundary is observable.
        if isinstance(last_error, ValueError):
            raise last_error
        raise RuntimeError(
            f"所有供应商均请求失败 (角色 '{role}')，最后错误: {last_error}"
        ) from last_error


ai_service = AIService()
