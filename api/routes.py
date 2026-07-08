import asyncio
import hashlib
import json
import time
import unicodedata
import uuid
import secrets
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Dict, Any
from fastapi import (
    APIRouter, Depends, FastAPI, WebSocket, WebSocketDisconnect, HTTPException,
    Query, Request, Response,
)
from fastapi.responses import JSONResponse
from models.danmaku import DanmakuResponse, DanmakuBroadcast
from models.api import ServerStatus, RootResponse, ConfigResponse, ConfigUpdateRequest
from models.auth import (
    RegisterRequest, LoginRequest, AuthTokenResponse, AccountResponse,
    NicknameUpdateRequest, NicknameHistoryResponse,
    RateLimitErrorResponse,
)
from models.memory import (
    MemoryPreferenceUpdateRequest, MemoryPreferenceResponse,
    AccountMemoryResponse, AccountMemoryExportResponse,
)
from models.sc import (
    SCConfigResponse, SCSubmitRequest, SCSubmitResponse, SCStatusResponse,
)
from models.emote import EmoteConfigResponse
from core import (
    connection_manager, persona_engine, event_bus,
    danmaku_pool, danmaku_selector, mood_pusher,
    stream_metadata_pusher, persona_impact_analyzer,
    persona_dynamics, db_manager, danmaku_memory_manager, emotion_manager,
    audience_relationship_manager, persona_event_pipeline,
    viewer_presence_coordinator,
    DanmakuReceivedEvent, StreamLifecycleEvent
)
from core.auth_service import (
    auth_service, UsernameAlreadyExistsError, InvalidCredentialsError,
)
from core.bounded_work_gate import ai_reply_work_gate
from core.memory_governance import account_memory_governance_service
from core.sc_service import (
    SCCooldownError, SCContentRejectedError, SCIdConflictError, SCNotFoundError,
    SCQueueFullError,
    sc_service,
)
from core.sc_consumer import sc_consumer
from core.viewer_emote import viewer_emote_service
from core.security_metrics import security_metrics
from core.overload_protection import overload_protector
from core.rate_limiter import (
    RateLimitPolicy, concurrency_gate, login_failure_cooldown, rate_limiter,
)
from core.realtime_protection import (
    ProtectionDecision, danmaku_deduplicator, resolve_client_ip,
    websocket_rate_guard, ip_subnet_key,
)
from config import settings, config_manager
from plugins import plugin_manager
from utils.logger import logger

router = APIRouter()


class RateLimitExceeded(Exception):
    def __init__(self, scope: str, retry_after_seconds: int):
        self.scope = scope
        self.retry_after_seconds = retry_after_seconds
        super().__init__(scope)


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    return resolve_client_ip(
        peer,
        request.headers.get("x-forwarded-for", ""),
        settings.rate_limit.trusted_proxy_cidrs,
    )


def _websocket_client_ip(websocket: WebSocket) -> str:
    peer = websocket.client.host if websocket.client else "unknown"
    return resolve_client_ip(
        peer,
        websocket.headers.get("x-forwarded-for", ""),
        settings.rate_limit.trusted_proxy_cidrs,
    )


def _activity_nickname(identity, connection_id: str) -> str:
    if identity and identity.is_authenticated:
        return identity.current_nickname
    return f"用户_{connection_id[:8] if connection_id else 'unknown'}"


async def _receive_websocket_text(websocket: WebSocket, connected_at: float) -> str:
    """接收客户端消息；值为 0 的应用层超时表示禁用。"""
    idle_timeout = settings.rate_limit.ws_idle_timeout_seconds
    max_lifetime = settings.rate_limit.ws_max_lifetime_seconds
    timeouts = []
    lifetime_left = None
    if idle_timeout > 0:
        timeouts.append(float(idle_timeout))
    if max_lifetime > 0:
        lifetime_left = max_lifetime - (time.monotonic() - connected_at)
        if lifetime_left <= 0:
            await websocket.close(code=1001, reason="连接生命周期结束，请重新连接")
            raise WebSocketDisconnect(code=1001)
        timeouts.append(lifetime_left)
    if not timeouts:
        return await websocket.receive_text()
    try:
        return await asyncio.wait_for(websocket.receive_text(), timeout=min(timeouts))
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - connected_at
        lifetime_ended = max_lifetime > 0 and elapsed >= max_lifetime
        reason = "连接生命周期结束，请重新连接" if lifetime_ended else "连接空闲超时"
        await websocket.close(code=1001, reason=reason)
        raise WebSocketDisconnect(code=1001)


async def _cleanup_websocket_connection(websocket: WebSocket) -> None:
    """统一连接清理，并为登录账号合并多连接与短暂重连。"""
    await mood_pusher.unsubscribe(websocket)
    await stream_metadata_pusher.unsubscribe(websocket)
    connection = connection_manager.get_connection(websocket)
    connection_id = connection.id if connection else None
    identity = connection.identity if connection else None
    nickname = _activity_nickname(identity, connection_id)

    await connection_manager.disconnect(websocket)
    if connection_id and identity and identity.is_authenticated:
        if connection_manager.get_account_connection_count(identity.account_id) == 0:
            async def announce_leave() -> None:
                stream_metadata_pusher.record_user_leave(
                    user_id=presence_id,
                    nickname=nickname,
                )

            _, presence_id = await viewer_presence_coordinator.join(identity.subject_id)
            await viewer_presence_coordinator.leave(
                identity.subject_id,
                settings.rate_limit.ws_presence_grace_seconds,
                announce_leave,
            )
    elif connection_id:
        stream_metadata_pusher.record_user_leave(
            user_id=connection_id,
            nickname=nickname,
        )

    await audience_relationship_manager.forget_guest(identity)
    await event_bus.emit("client_disconnected", websocket)
    stream_metadata_pusher.update_viewer_count(connection_manager.get_connection_count())
    logger.info(f"客户端断开连接，当前连接数: {connection_manager.get_connection_count()}")


async def _send_ws_limit_event(
    websocket: WebSocket,
    decision: ProtectionDecision,
    *,
    message: str = "操作过于频繁，请稍后再试",
) -> None:
    metric_scope = decision.scope if decision.scope in {
        "ws_handshake", "danmaku_send", "ai_reply", "viewer_emote"
    } else "connection"
    security_metrics.record(
        metric_scope,
        "queue_full" if decision.code == "queue_full" else "cooldown",
    )
    await connection_manager.send_json_to(websocket, {
        "type": "rate_limited",
        "data": {
            "code": decision.code,
            "message": message,
            "retry_after_seconds": max(1, decision.retry_after_seconds),
            "scope": decision.scope,
            "action": decision.action,
            "request_id": str(uuid.uuid4()),
        },
    })


async def _handle_viewer_emote(websocket: WebSocket, message_data: dict) -> None:
    """纯展示旁路；不得调用任何弹幕、人格、记忆或活动服务。"""
    connection = connection_manager.get_connection(websocket)
    if not connection:
        await connection_manager.send_json_to(websocket, {
            "type": "error", "code": "emote_connection_missing",
            "message": "连接状态不可用，请重新连接",
        })
        return
    decision = viewer_emote_service.process(
        emote_id=message_data.get("emote_id"),
        client_event_id=message_data.get("client_event_id"),
        connection_id=connection.id,
        client_ip=connection.client_ip or "unknown",
        identity=connection.identity,
    )
    if decision.allowed:
        failures = await connection_manager.broadcast_json({
            "type": "viewer_emote",
            "data": {
                **decision.payload,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        })
        if isinstance(failures, int):
            viewer_emote_service.record_broadcast_failures(failures)
        return
    if decision.code == "rate_limited":
        await _send_ws_limit_event(
            websocket,
            ProtectionDecision(
                False, "viewer_emote", decision.retry_after_seconds,
                "cooldown", "rate_limited",
            ),
            message="表情发送过于频繁，请稍后再试",
        )
        return
    messages = {
        "invalid_emote": "未知或已禁用的表情",
        "invalid_emote_event": "表情事件 ID 格式无效",
        "duplicate_emote": "该表情事件已处理，请勿重复发送",
    }
    await connection_manager.send_json_to(websocket, {
        "type": "error", "code": decision.code,
        "message": messages.get(decision.code, "表情发送失败"),
    })


def _validate_admin_request(request: Request):
    """敏感接口默认隐藏；管理员密钥与普通账号令牌严格分离。"""
    if not settings.admin.enabled:
        raise HTTPException(status_code=404, detail="接口不存在")
    configured_value = settings.admin.api_key
    configured = (
        configured_value.get_secret_value()
        if hasattr(configured_value, "get_secret_value")
        else str(configured_value)
    )
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    supplied = token if scheme.casefold() == "bearer" else request.headers.get(
        "x-admin-key", ""
    )
    if not configured or not supplied or not secrets.compare_digest(configured, supplied):
        raise HTTPException(status_code=403, detail="管理员凭据无效")
    decision = rate_limiter.check(
        f"admin:ip:{_client_ip(request)}",
        RateLimitPolicy(
            settings.admin.rate_per_minute,
            settings.admin.burst,
            settings.rate_limit.rejection_cooldown_seconds,
        ),
    )
    if not decision.allowed:
        raise RateLimitExceeded("admin_api", decision.retry_after_seconds)
    lease = concurrency_gate.try_acquire("admin:request", settings.admin.concurrency)
    if lease is None:
        raise RateLimitExceeded("admin_api_capacity", 1)
    return lease


async def _require_admin(request: Request):
    lease = _validate_admin_request(request)
    try:
        yield
    finally:
        lease.release()


ADMIN_ONLY = [Depends(_require_admin)]


def _username_limit_key(username: str) -> str:
    normalized = unicodedata.normalize("NFKC", username or "").strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _limit_policy(rate: float, burst: int) -> RateLimitPolicy:
    return RateLimitPolicy(
        rate_per_minute=rate,
        burst=burst,
        cooldown_seconds=settings.rate_limit.rejection_cooldown_seconds,
    )


def _check_ai_reply_quota(selected) -> ProtectionDecision:
    """在进入完整外部 AI 链前原子检查身份、IP 与全局额度。"""
    config = settings.rate_limit
    identity = selected.viewer_identity
    subject = identity.subject_id if identity else f"danmaku:{selected.id}"
    client_ip = selected.client_ip or "unknown"
    decision = rate_limiter.check_many([
        (f"ai:reply:subject:{subject}", _limit_policy(
            config.ai_reply_subject_rate_per_minute,
            config.ai_reply_subject_burst,
        ), 1.0),
        (f"ai:reply:ip:{client_ip}", _limit_policy(
            config.ai_reply_ip_rate_per_minute,
            config.ai_reply_ip_burst,
        ), 1.0),
        ("ai:reply:global", _limit_policy(
            config.ai_reply_global_rate_per_minute,
            config.ai_reply_global_burst,
        ), 1.0),
    ])
    if decision.allowed:
        return ProtectionDecision(True, "ai_reply", 0, "allow", "allowed")
    return ProtectionDecision(
        False, "ai_reply", decision.retry_after_seconds, "cooldown", "quota_exceeded"
    )


def _rate_limit_response(scope: str, retry_after_seconds: int) -> JSONResponse:
    metric_scope = (
        "auth_register" if scope.startswith("auth_register")
        else "auth_login" if scope.startswith("auth_login")
        else "account_profile" if scope.startswith("account_profile")
        else "http"
    )
    security_metrics.record(metric_scope, "rejected")
    request_id = str(uuid.uuid4())
    body = RateLimitErrorResponse(
        message="请求过于频繁，请稍后再试",
        retry_after_seconds=max(1, retry_after_seconds),
        scope=scope,
        request_id=request_id,
    ).model_dump()
    return JSONResponse(
        status_code=429,
        content=body,
        headers={
            "Retry-After": str(body["retry_after_seconds"]),
            "X-Request-ID": request_id,
        },
    )


def _login_failure_key(request: Request, username: str) -> str:
    return f"{_client_ip(request)}:{_username_limit_key(username)}"


def _enforce_profile_rate_limit(account_id: str, *, write: bool) -> JSONResponse | None:
    config = settings.rate_limit
    decision = rate_limiter.check(
        f"profile:{'write' if write else 'read'}:account:{account_id}",
        _limit_policy(
            config.profile_write_rate_per_minute if write
            else config.profile_read_rate_per_minute,
            config.profile_write_burst if write else config.profile_read_burst,
        ),
    )
    if decision.allowed:
        return None
    return _rate_limit_response(
        "account_profile_write" if write else "account_profile_read",
        decision.retry_after_seconds,
    )


def _enforce_auth_rate_limit(
    request: Request, *, action: str, username: str = ""
) -> JSONResponse | None:
    if not settings.rate_limit.enabled:
        return None
    config = settings.rate_limit
    ip = _client_ip(request)
    if action == "register":
        subnet = ip_subnet_key(ip)
        checks = [
            (f"auth:register:ip:{ip}", _limit_policy(
                config.register_ip_rate_per_minute, config.register_ip_burst
            )),
            (f"auth:register:subnet:{subnet}", _limit_policy(
                config.register_subnet_rate_per_minute,
                config.register_subnet_burst,
            )),
            ("auth:register:global", _limit_policy(
                config.register_global_rate_per_minute, config.register_global_burst
            )),
        ]
        public_scope = "auth_register"
    else:
        username_key = _username_limit_key(username)
        checks = [
            (f"auth:login:ip:{ip}", _limit_policy(
                config.login_ip_rate_per_minute, config.login_ip_burst
            )),
            (f"auth:login:username:{username_key}", _limit_policy(
                config.login_username_rate_per_minute,
                config.login_username_burst,
            )),
            ("auth:login:global", _limit_policy(
                config.login_global_rate_per_minute, config.login_global_burst
            )),
        ]
        public_scope = "auth_login"

    decision = rate_limiter.check_many(
        [(key, policy, 1.0) for key, policy in checks]
    )
    if not decision.allowed:
        return _rate_limit_response(public_scope, decision.retry_after_seconds)

    if action == "login":
        progressive = login_failure_cooldown.check(
            _login_failure_key(request, username)
        )
        if not progressive.allowed:
            return _rate_limit_response(public_scope, progressive.retry_after_seconds)
    return None


@router.post(
    "/auth/register",
    response_model=AuthTokenResponse,
    status_code=201,
    responses={
        409: {"description": "用户名已存在"},
        429: {"model": RateLimitErrorResponse, "description": "注册请求过于频繁"},
    },
)
async def register_account(
    payload: RegisterRequest, response: Response, request: Request
):
    """创建账号并签发首个访问令牌。"""
    limited = _enforce_auth_rate_limit(request, action="register")
    if limited:
        return limited
    lease = concurrency_gate.try_acquire(
        "auth:register_hash", settings.rate_limit.register_hash_concurrency
    )
    if lease is None:
        return _rate_limit_response("auth_register_capacity", 1)
    try:
        result = await asyncio.to_thread(
            auth_service.register, payload.username, payload.password, payload.nickname
        )
        _set_auth_cookie(response, result)
        return result
    except UsernameAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        lease.release()


@router.post(
    "/auth/login",
    response_model=AuthTokenResponse,
    responses={
        401: {"description": "用户名或密码错误"},
        429: {"model": RateLimitErrorResponse, "description": "登录请求过于频繁"},
    },
)
async def login_account(payload: LoginRequest, response: Response, request: Request):
    """验证用户名和密码并签发新的访问令牌。"""
    limited = _enforce_auth_rate_limit(
        request, action="login", username=payload.username
    )
    if limited:
        return limited
    lease = concurrency_gate.try_acquire(
        "auth:login_hash", settings.rate_limit.login_hash_concurrency
    )
    if lease is None:
        return _rate_limit_response("auth_login_capacity", 1)
    failure_key = _login_failure_key(request, payload.username)
    try:
        result = await asyncio.to_thread(
            auth_service.login, payload.username, payload.password
        )
        login_failure_cooldown.clear(failure_key)
        _set_auth_cookie(response, result)
        return result
    except InvalidCredentialsError as exc:
        login_failure_cooldown.record_failure(
            failure_key,
            threshold=settings.rate_limit.login_failure_threshold,
            base_seconds=settings.rate_limit.login_failure_base_cooldown_seconds,
            max_seconds=settings.rate_limit.login_failure_max_cooldown_seconds,
        )
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    finally:
        lease.release()


@router.patch(
    "/auth/profile/nickname",
    response_model=AccountResponse,
    responses={401: {"description": "访问令牌无效或已过期"}},
)
async def update_account_nickname(payload: NicknameUpdateRequest, request: Request):
    """更新当前账号昵称并追加昵称历史版本。"""
    principal = await _require_http_principal(request)
    limited = _enforce_profile_rate_limit(principal.account_id, write=True)
    if limited:
        return limited
    account = await asyncio.to_thread(
        auth_service.update_nickname, principal.account_id, payload.nickname
    )
    connection_manager.update_account_nickname(
        principal.account_id, account["nickname"], account["nickname_version"]
    )
    return account


@router.get(
    "/auth/profile/nickname-history",
    response_model=NicknameHistoryResponse,
    responses={401: {"description": "访问令牌无效或已过期"}},
)
async def get_account_nickname_history(request: Request):
    """仅返回访问令牌所属账号的昵称历史。"""
    principal = await _require_http_principal(request)
    limited = _enforce_profile_rate_limit(principal.account_id, write=False)
    if limited:
        return limited
    history = await asyncio.to_thread(
        auth_service.list_nickname_history, principal.account_id
    )
    return {"account_id": principal.account_id, "history": history}


@router.delete(
    "/auth/profile/nickname-history/{version}",
    status_code=204,
    responses={
        401: {"description": "访问令牌无效或已过期"},
        404: {"description": "昵称版本不存在"},
        409: {"description": "当前昵称版本不能删除"},
    },
)
async def delete_account_nickname_history(version: int, request: Request):
    """物理删除自己的旧昵称版本，当前昵称不可删除。"""
    principal = await _require_http_principal(request)
    limited = _enforce_profile_rate_limit(principal.account_id, write=True)
    if limited:
        return limited
    history = await asyncio.to_thread(
        auth_service.list_nickname_history, principal.account_id
    )
    target = next((item for item in history if item["version"] == version), None)
    if target is None:
        raise HTTPException(status_code=404, detail="昵称版本不存在")
    if target["is_current"]:
        raise HTTPException(status_code=409, detail="当前昵称版本不能删除")
    deleted = await asyncio.to_thread(
        auth_service.delete_nickname_history, principal.account_id, version
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="昵称版本不存在")
    return Response(status_code=204)


@router.get(
    "/auth/profile/memory",
    response_model=AccountMemoryResponse,
    responses={401: {"description": "访问令牌无效或已过期"}},
)
async def get_account_memory(request: Request):
    """查询当前账号的人物关系记忆与记忆开关。"""
    principal = await _require_http_principal(request)
    limited = _enforce_profile_rate_limit(principal.account_id, write=False)
    if limited:
        return limited
    return await asyncio.to_thread(
        account_memory_governance_service.get_snapshot, principal.account_id
    )


@router.get(
    "/auth/profile/memory/export",
    response_model=AccountMemoryExportResponse,
    responses={401: {"description": "访问令牌无效或已过期"}},
)
async def export_account_memory(request: Request):
    """导出令牌所属账号的人物记忆和身份版本元数据。"""
    principal = await _require_http_principal(request)
    limited = _enforce_profile_rate_limit(principal.account_id, write=False)
    if limited:
        return limited
    return await asyncio.to_thread(
        account_memory_governance_service.export, principal.account_id
    )


@router.put(
    "/auth/profile/memory/preferences",
    response_model=MemoryPreferenceResponse,
    responses={401: {"description": "访问令牌无效或已过期"}},
)
async def update_account_memory_preference(
    payload: MemoryPreferenceUpdateRequest, request: Request
):
    """启用或退出账号长期记忆；退出会同时清除已有人物记忆。"""
    principal = await _require_http_principal(request)
    limited = _enforce_profile_rate_limit(principal.account_id, write=True)
    if limited:
        return limited
    return await asyncio.to_thread(
        account_memory_governance_service.set_enabled,
        principal.account_id,
        payload.long_term_memory_enabled,
    )


@router.delete(
    "/auth/profile/memory",
    status_code=204,
    responses={401: {"description": "访问令牌无效或已过期"}},
)
async def delete_account_memory(request: Request):
    """清除人物记忆但保留账号、登录会话、当前昵称与昵称历史。"""
    principal = await _require_http_principal(request)
    limited = _enforce_profile_rate_limit(principal.account_id, write=True)
    if limited:
        return limited
    await asyncio.to_thread(
        account_memory_governance_service.delete, principal.account_id
    )
    return Response(status_code=204)


@router.post(
    "/sc",
    response_model=SCSubmitResponse,
    status_code=202,
    responses={
        401: {"description": "访问令牌无效或已过期"},
        409: {"description": "sc_id 已被其他账号占用"},
        429: {"description": "账号 SC 冷却中"},
        503: {"description": "SC 队列已满"},
    },
)
async def submit_sc(payload: SCSubmitRequest, request: Request):
    """认证账号幂等提交 SC；成功仅表示持久化进入专用队列。"""
    principal = await _require_http_principal(request)
    existing = await asyncio.to_thread(
        sc_service.get_status_or_none, principal, payload.sc_id
    )
    if existing is not None:
        return existing
    decision = rate_limiter.check_many([
        (f"sc:submit:ip:{_client_ip(request)}", _limit_policy(
            settings.sc.submit_ip_rate_per_minute, settings.sc.submit_ip_burst
        ), 1.0),
        (f"sc:submit:account:{principal.account_id}", _limit_policy(
            settings.sc.submit_account_rate_per_minute, settings.sc.submit_account_burst
        ), 1.0),
        ("sc:submit:global", _limit_policy(
            settings.sc.submit_global_rate_per_minute, settings.sc.submit_global_burst
        ), 1.0),
    ])
    if not decision.allowed:
        raise RateLimitExceeded("sc_submit_rate", decision.retry_after_seconds)
    try:
        return await asyncio.to_thread(
            sc_service.submit, principal, payload.sc_id, payload.content
        )
    except SCCooldownError as exc:
        request_id = str(uuid.uuid4())
        return JSONResponse(
            status_code=429,
            content={
                "code": "sc_cooldown",
                "message": "SC 冷却中，请稍后再试",
                "retry_after_seconds": exc.retry_after_seconds,
                "scope": "sc_submit",
                "request_id": request_id,
            },
            headers={
                "Retry-After": str(exc.retry_after_seconds),
                "X-Request-ID": request_id,
            },
        )
    except SCQueueFullError:
        return JSONResponse(
            status_code=503,
            content={
                "code": "sc_queue_full",
                "message": "SC 队列繁忙，请稍后再试",
                "retry_after_seconds": 30,
            },
            headers={"Retry-After": "30"},
        )
    except SCContentRejectedError:
        return JSONResponse(
            status_code=422,
            content={
                "code": "sc_content_rejected",
                "message": "SC 内容未通过安全检查，请修改后重试",
                "scope": "sc_submit",
            },
        )
    except SCIdConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/sc/config", response_model=SCConfigResponse)
async def get_sc_config():
    """前端可见的 SC 输入与账号冷却规则。"""
    return {
        "cooldown_seconds": settings.sc.cooldown_seconds,
        "max_content_chars": settings.sc.max_content_chars,
        "max_content_bytes": settings.sc.max_content_bytes,
    }


@router.get("/emotes/config", response_model=EmoteConfigResponse)
async def get_emote_config():
    """前端仅按稳定 ID 映射本地静态表情资源。"""
    return {
        "allowed_ids": list(settings.emotes.allowed_ids),
        "cooldown_seconds": settings.emotes.cooldown_seconds,
    }


@router.get(
    "/sc/{sc_id}",
    response_model=SCStatusResponse,
    responses={
        401: {"description": "访问令牌无效或已过期"},
        404: {"description": "SC 不存在或不属于当前账号"},
    },
)
async def get_sc_status(sc_id: str, request: Request):
    """只允许令牌所属账号查询自己的 SC。"""
    principal = await _require_http_principal(request)
    if not 8 <= len(sc_id) <= 128:
        raise HTTPException(status_code=404, detail="SC 不存在")
    try:
        return await asyncio.to_thread(sc_service.get_status, principal, sc_id)
    except SCNotFoundError as exc:
        raise HTTPException(status_code=404, detail="SC 不存在") from exc


@router.get("/sc", response_model=list[SCStatusResponse])
async def list_my_sc(request: Request, limit: int = Query(default=50, ge=1, le=100)):
    """列出当前账号最近的 SC，供刷新或断线后恢复状态。"""
    principal = await _require_http_principal(request)
    return await asyncio.to_thread(sc_service.list_for_account, principal.account_id, limit)


@router.delete("/sc/history", status_code=204)
async def delete_my_sc_history(request: Request):
    """删除当前账号已结束的 SC；排队中项目继续履约。"""
    principal = await _require_http_principal(request)
    limited = _enforce_profile_rate_limit(principal.account_id, write=True)
    if limited:
        return limited
    await asyncio.to_thread(sc_service.delete_terminal_for_account, principal.account_id)
    return Response(status_code=204)


@router.get("/admin/sc/stats", dependencies=ADMIN_ONLY)
async def get_sc_stats():
    return await asyncio.to_thread(sc_service.get_stats)


@router.get("/admin/emotes/stats", dependencies=ADMIN_ONLY)
async def get_emote_stats():
    return viewer_emote_service.get_metrics()


@router.get("/admin/security/stats", dependencies=ADMIN_ONLY)
async def get_security_stats():
    """固定低基数安全指标；不含 IP、账号、昵称、令牌或事件 ID。"""
    gate = ai_reply_work_gate.snapshot()
    pressure = overload_protector.snapshot(
        connections=connection_manager.get_connection_count(),
        ai_active=gate["active"], ai_waiting=gate["waiting"],
    )
    return {
        **security_metrics.snapshot(),
        "pressure": pressure,
        "ai_reply_gate": gate,
    }


def _set_auth_cookie(response: Response, auth_result: dict) -> None:
    response.set_cookie(
        key=settings.auth.cookie_name,
        value=auth_result["access_token"],
        max_age=settings.auth.access_token_ttl_hours * 3600,
        httponly=True,
        secure=settings.auth.cookie_secure,
        samesite=settings.auth.cookie_samesite,
        domain=settings.auth.cookie_domain,
        path="/",
    )
    if settings.auth.cookie_partitioned:
        response.headers["set-cookie"] = response.headers["set-cookie"] + "; Partitioned"


def _http_access_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() == "bearer" and token:
        return token
    return request.cookies.get(settings.auth.cookie_name, "")


async def _require_http_principal(request: Request):
    token = _http_access_token(request)
    principal = await asyncio.to_thread(auth_service.authenticate_access_token, token)
    if principal is None:
        raise HTTPException(status_code=401, detail="访问令牌无效或已过期")
    return principal


def _websocket_access_token(websocket: WebSocket, query_token: str = None) -> str:
    if query_token:
        return query_token
    cookie_token = websocket.cookies.get(settings.auth.cookie_name)
    if cookie_token:
        return cookie_token
    authorization = websocket.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    return token if scheme.casefold() == "bearer" else ""

# 弹幕频率计数器
_danmaku_rate_counter = {
    "timestamps": []
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info("🚀 弹幕服务器启动中...")
    
    config_manager.update_settings(settings)
    await plugin_manager.load_plugins()
    await persona_event_pipeline.start()
    await persona_event_pipeline.publish(
        StreamLifecycleEvent(phase="started", source="fastapi_lifespan")
    )
    await mood_pusher.start()  # 启动心情推送服务
    await stream_metadata_pusher.start()  # 启动直播间元信息推送服务
    await sc_consumer.start()
    await event_bus.emit("server_startup")
    
    yield
    
    await persona_event_pipeline.publish(
        StreamLifecycleEvent(phase="ended", source="fastapi_lifespan")
    )
    await sc_consumer.stop()
    await persona_event_pipeline.stop()
    await mood_pusher.stop()  # 停止心情推送服务
    await stream_metadata_pusher.stop()  # 停止直播间元信息推送服务
    await viewer_presence_coordinator.clear()
    await event_bus.emit("server_shutdown")
    logger.info("🛑 弹幕服务器关闭")


@router.get("/", response_model=RootResponse)
async def root():
    """根路径，返回服务状态"""
    return {
        "status": "danmaku server running",
        "connections": connection_manager.get_connection_count(),
        "history_count": connection_manager.get_history_count(),
        "danmaku_pool": danmaku_pool.get_pool_stats()
    }


@router.get("/status", response_model=ServerStatus)
async def get_status():
    """获取服务器状态"""
    return {
        "status": "running",
        "active_connections": connection_manager.get_connection_count(),
        "message_history_count": connection_manager.get_history_count(),
        "danmaku_pool": danmaku_pool.get_pool_stats(),
        "mood_pusher": mood_pusher.get_stats(),
        "server_time": datetime.now().isoformat()
    }


@router.get("/config", response_model=ConfigResponse, dependencies=ADMIN_ONLY)
async def get_config():
    """获取当前配置"""
    try:
        config = config_manager.export_config()
        return ConfigResponse(success=True, config=config)
    except Exception as e:
        return ConfigResponse(success=False, message=str(e))


@router.put("/config", response_model=ConfigResponse, dependencies=ADMIN_ONLY)
async def update_config(request: ConfigUpdateRequest):
    """更新配置"""
    try:
        config_manager.set(request.key, request.value)
        config_manager.update_settings(settings)
        return ConfigResponse(success=True, config=config_manager.export_config())
    except Exception as e:
        return ConfigResponse(success=False, message=str(e))


@router.get("/plugins", dependencies=ADMIN_ONLY)
async def list_plugins():
    """列出所有插件"""
    return {
        "plugins": plugin_manager.list_plugins()
    }


@router.post("/plugins/{plugin_name}/enable", dependencies=ADMIN_ONLY)
async def enable_plugin(plugin_name: str):
    """启用插件"""
    success = await plugin_manager.enable_plugin(plugin_name)
    return {"success": success, "plugin": plugin_name}


@router.post("/plugins/{plugin_name}/disable", dependencies=ADMIN_ONLY)
async def disable_plugin(plugin_name: str):
    """禁用插件"""
    success = await plugin_manager.disable_plugin(plugin_name)
    return {"success": success, "plugin": plugin_name}


@router.get("/persona/state", dependencies=ADMIN_ONLY)
async def get_persona_state():
    """获取人格状态"""
    return {
        "mood": persona_engine.state.mood,
        "darkness": persona_engine.state.darkness,
        "stress": persona_engine.state.stress,
        "behavior": {
            "reply_aggressiveness": persona_engine.behavior.reply_aggressiveness,
            "ignore_probability": persona_engine.behavior.ignore_probability
        }
    }


@router.post("/persona/reset", dependencies=ADMIN_ONLY)
async def reset_persona_state():
    """重置人格状态"""
    persona_engine.reset_state()
    return {"success": True}


@router.get("/connections", dependencies=ADMIN_ONLY)
async def get_connections():
    """获取连接信息（用于调试）"""
    return connection_manager.get_connection_info()


@router.get("/danmaku/pool", dependencies=ADMIN_ONLY)
async def get_danmaku_pool_status():
    """获取弹幕池状态"""
    return danmaku_pool.get_pool_status()


@router.get("/danmaku/selector/stats", dependencies=ADMIN_ONLY)
async def get_danmaku_selector_stats():
    """获取弹幕选择器统计"""
    return danmaku_selector.get_selector_stats()


@router.get("/mood/pusher/stats", dependencies=ADMIN_ONLY)
async def get_mood_pusher_stats():
    """获取心情推送统计"""
    return mood_pusher.get_stats()


@router.get("/stream/metadata")
async def get_stream_metadata():
    """获取直播间元信息"""
    return stream_metadata_pusher.get_metadata().to_dict()


@router.get("/stream/activities", dependencies=ADMIN_ONLY)
async def get_stream_activities(limit: int = Query(20, ge=1, le=100)):
    """获取直播间活动记录"""
    activities = stream_metadata_pusher.get_recent_activities(limit)
    return {
        "activities": [activity.to_dict() for activity in activities],
        "total": len(activities)
    }


@router.get("/stream/metadata/stats", dependencies=ADMIN_ONLY)
async def get_stream_metadata_stats():
    """获取直播间元信息推送统计"""
    return stream_metadata_pusher.get_stats()


@router.get("/persona/impact/debug", dependencies=ADMIN_ONLY)
async def get_persona_impact_debug():
    """获取人格影响分析器调试信息"""
    return {
        "impact_analyzer": persona_impact_analyzer.get_debug_info(),
        "dynamics": persona_dynamics.get_debug_info()
    }


@router.get("/persona/impact/history", dependencies=ADMIN_ONLY)
async def get_persona_impact_history(limit: int = Query(10, ge=1, le=100)):
    """获取人格影响分析历史"""
    return {
        "history": persona_impact_analyzer.get_analysis_history(limit),
        "total_count": len(persona_impact_analyzer.get_analysis_history(100))
    }


@router.get("/persona/events/debug", dependencies=ADMIN_ONLY)
async def get_persona_event_debug():
    """查看人格事件流水线状态及最近归约记录。"""
    return persona_event_pipeline.get_debug_info()


@router.post("/persona/impact/analyze", dependencies=ADMIN_ONLY)
async def analyze_danmaku_impact(danmaku: Dict[str, Any]):
    """手动分析弹幕对人格的影响（调试用）"""
    content = danmaku.get("content", "")
    if not content:
        raise HTTPException(status_code=400, detail="缺少 content 字段")
    
    analysis = await persona_impact_analyzer.analyze_danmaku_impact(
        content,
        persona_engine.state
    )
    
    if analysis:
        return analysis.to_dict()
    
    raise HTTPException(status_code=500, detail="分析失败")


@router.post("/persona/impact/debug-mode", dependencies=ADMIN_ONLY)
async def set_persona_impact_debug_mode(enabled: bool):
    """设置人格影响分析器调试模式"""
    persona_impact_analyzer.set_debug_mode(enabled)
    return {"debug_mode": enabled}


@router.get("/database/stats", dependencies=ADMIN_ONLY)
async def get_database_stats():
    """获取数据库统计信息"""
    return db_manager.get_stats()


@router.get("/database/danmaku", dependencies=ADMIN_ONLY)
async def get_danmaku_records(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=1000000),
    start_time: str = None,
    end_time: str = None,
):
    """获取弹幕记录列表"""
    records = db_manager.get_danmaku_records(limit=limit, offset=offset, start_time=start_time, end_time=end_time)
    return {
        "records": records,
        "total": db_manager.get_danmaku_count(start_time=start_time, end_time=end_time)
    }


@router.get("/database/replies", dependencies=ADMIN_ONLY)
async def get_reply_records(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=1000000),
    start_time: str = None,
    end_time: str = None,
):
    """获取回复记录列表"""
    records = db_manager.get_reply_records(limit=limit, offset=offset, start_time=start_time, end_time=end_time)
    return {
        "records": records,
        "total": db_manager.get_reply_count(start_time=start_time, end_time=end_time)
    }


@router.get("/database/export", dependencies=ADMIN_ONLY)
async def export_danmaku_data(start_time: str = None, end_time: str = None):
    """结构化导出弹幕数据"""
    export_data = db_manager.export_danmaku_data(start_time=start_time, end_time=end_time)
    return export_data


@router.get("/memory/stats", dependencies=ADMIN_ONLY)
async def get_memory_stats():
    """获取弹幕记忆统计信息"""
    return danmaku_memory_manager.get_stats()


@router.get("/memory/context", dependencies=ADMIN_ONLY)
async def get_memory_context(limit: int = Query(10, ge=1, le=100)):
    """获取弹幕记忆上下文"""
    return await danmaku_memory_manager.get_memory_context(limit=limit)


@router.get("/memory/hot-topics", dependencies=ADMIN_ONLY)
async def get_hot_topics(limit: int = Query(5, ge=1, le=100)):
    """获取热门话题"""
    hot_topics = await danmaku_memory_manager.get_hot_topics(limit=limit)
    return {
        "hot_topics": [
            {"topic": topic, "heat": heat}
            for topic, heat in hot_topics
        ]
    }


@router.get("/memory/group-discussion", dependencies=ADMIN_ONLY)
async def analyze_group_discussion(topic: str):
    """分析群体讨论情况"""
    return await danmaku_memory_manager.analyze_group_discussion(topic)


@router.get("/memory/persona-impact", dependencies=ADMIN_ONLY)
async def get_persona_impact():
    """获取弹幕对人格的影响"""
    return await danmaku_memory_manager.calculate_persona_impact()


@router.get("/emotion/stats", dependencies=ADMIN_ONLY)
async def get_emotion_stats():
    """获取情绪管理器统计信息"""
    return emotion_manager.get_statistics()


@router.get("/emotion/list")
async def get_emotion_list():
    """获取所有可用情绪列表"""
    return {
        "available_emotions": emotion_manager.get_available_emotions()
    }


@router.get("/emotion/info/{emotion_name}")
async def get_emotion_info(emotion_name: str):
    """获取情绪详细信息"""
    info = emotion_manager.get_emotion_info(emotion_name)
    if not info:
        raise HTTPException(status_code=404, detail=f"情绪 '{emotion_name}' 不存在")
    return info


@router.post("/emotion/randomness", dependencies=ADMIN_ONLY)
async def set_emotion_randomness(randomness: float):
    """设置情绪随机强度 0-1"""
    if randomness < 0 or randomness > 1:
        raise HTTPException(status_code=400, detail="randomness 必须在 0-1 之间")
    emotion_manager.set_randomness(randomness)
    return {
        "success": True,
        "randomness": randomness
    }


@router.get("/emotion/randomness", dependencies=ADMIN_ONLY)
async def get_emotion_randomness():
    """获取当前情绪随机强度"""
    return {
        "randomness": emotion_manager.get_randomness()
    }


@router.post("/emotion/select", dependencies=ADMIN_ONLY)
async def select_emotions(mood: float, stress: float, darkness: float, count: int = 2):
    """根据人格状态选择情绪"""
    if mood < 0 or mood > 1:
        raise HTTPException(status_code=400, detail="mood 必须在 0-1 之间")
    if stress < 0 or stress > 1:
        raise HTTPException(status_code=400, detail="stress 必须在 0-1 之间")
    if darkness < 0 or darkness > 1:
        raise HTTPException(status_code=400, detail="darkness 必须在 0-1 之间")
    if count < 1 or count > 5:
        raise HTTPException(status_code=400, detail="count 必须在 1-5 之间")
    
    emotions = emotion_manager.select_emotions(mood, stress, darkness, count)
    return {
        "selected_emotions": emotions
    }


@router.post("/emotion/reset-history", dependencies=ADMIN_ONLY)
async def reset_emotion_history():
    """重置情绪使用历史"""
    emotion_manager.reset_recent_history()
    return {
        "success": True,
        "message": "情绪使用历史已重置"
    }


def _update_danmaku_rate() -> int:
    """更新并返回当前弹幕频率（条/分钟）"""
    global _danmaku_rate_counter
    
    now = datetime.now()
    
    # 移除超过1分钟的记录
    _danmaku_rate_counter["timestamps"] = [
        ts for ts in _danmaku_rate_counter["timestamps"]
        if (now - ts).total_seconds() < 60
    ]
    
    # 添加当前弹幕的时间戳
    _danmaku_rate_counter["timestamps"].append(now)
    
    # 返回当前1分钟内的弹幕数量
    return len(_danmaku_rate_counter["timestamps"])


@router.websocket("/danmaku")
async def websocket_endpoint(websocket: WebSocket, access_token: str = None):
    """WebSocket弹幕接口"""
    client_ip = _websocket_client_ip(websocket)
    verified_principal = None
    resolved_token = _websocket_access_token(websocket, access_token)
    if resolved_token:
        verified_principal = auth_service.authenticate_access_token(resolved_token)
        if verified_principal is None:
            await websocket.close(code=1008, reason="无效或已过期的访问令牌")
            return
    account_id = verified_principal.account_id if verified_principal else ""
    gate = ai_reply_work_gate.snapshot()
    pressure = overload_protector.snapshot(
        connections=connection_manager.get_connection_count(),
        ai_active=gate["active"], ai_waiting=gate["waiting"],
    )
    overload = overload_protector.admit(expensive=True, snapshot=pressure)
    if not overload.allowed:
        await websocket.close(code=1013, reason="服务器繁忙，请稍后重试")
        return
    handshake = websocket_rate_guard.check_handshake(
        client_ip, account_id, settings.rate_limit
    )
    if not handshake.allowed:
        await websocket.close(code=1013, reason="连接过于频繁，请稍后重试")
        return
    if (
        connection_manager.get_connection_count()
        >= settings.rate_limit.ws_max_global_connections
        or connection_manager.get_ip_connection_count(client_ip)
        >= settings.rate_limit.ws_max_ip_connections
        or (
            account_id
            and connection_manager.get_account_connection_count(account_id)
            >= settings.rate_limit.ws_max_account_connections
        )
    ):
        await websocket.close(code=1013, reason="连接数量已达上限")
        return
    await connection_manager.connect(
        websocket,
        verified_principal=verified_principal,
        resolved_client_ip=client_ip,
    )
    await event_bus.emit("client_connected", websocket)
    
    # 获取连接信息用于元信息推送
    connection_id = None
    connection_identity = None
    
    # 查找当前连接的ID
    for conn in connection_manager.active_connections.values():
        if conn.websocket == websocket:
            connection_id = conn.id
            connection_identity = conn.identity
            client_ip = conn.client_ip or client_ip
            break
    
    # 登录账号的多个连接和短暂重连只形成一次在房生命周期。
    should_announce_join = True
    activity_user_id = connection_id or str(id(websocket))
    if connection_identity and connection_identity.is_authenticated:
        should_announce_join, activity_user_id = await viewer_presence_coordinator.join(
            connection_identity.subject_id
        )
    if should_announce_join:
        stream_metadata_pusher.record_user_join(
            user_id=activity_user_id,
            nickname=_activity_nickname(connection_identity, connection_id),
            ip=client_ip,
        )
    
    # 更新在线人数
    stream_metadata_pusher.update_viewer_count(connection_manager.get_connection_count())
    
    # 订阅各种推送服务
    await mood_pusher.subscribe(websocket)
    await stream_metadata_pusher.subscribe(websocket)
    
    connected_at = time.monotonic()
    try:
        while True:
            data = await _receive_websocket_text(websocket, connected_at)

            if len(data.encode("utf-8")) > settings.rate_limit.ws_max_frame_bytes:
                await _send_ws_limit_event(
                    websocket,
                    ProtectionDecision(
                        False, "danmaku_frame", 1, "disconnect", "payload_too_large"
                    ),
                    message="消息体过大，连接已关闭",
                )
                await websocket.close(code=1009, reason="消息体过大")
                raise WebSocketDisconnect(code=1009)
            
            try:
                message_data = json.loads(data)
                if not isinstance(message_data, dict):
                    await websocket.send_text(json.dumps({
                        "type": "error", "code": "invalid_fields",
                        "message": "WebSocket 消息必须是 JSON 对象",
                    }, ensure_ascii=False))
                    continue

                if message_data.get("type") == "viewer_emote":
                    await _handle_viewer_emote(websocket, message_data)
                    continue
                
                if not all(key in message_data for key in ["nickname", "message", "danmakuID"]):
                    error_response = {
                        "type": "error",
                        "message": "缺少必需字段: nickname、danmakuID 或 message"
                    }
                    await websocket.send_text(json.dumps(error_response, ensure_ascii=False))
                    continue

                nickname = message_data.get("nickname")
                message = message_data.get("message")
                danmaku_id = message_data.get("danmakuID")
                sender_level = message_data.get("sender_level", 1)
                if not all(isinstance(value, str) for value in (
                    nickname, message, danmaku_id
                )):
                    await websocket.send_text(json.dumps({
                        "type": "error", "code": "invalid_fields",
                        "message": "nickname、message 和 danmakuID 必须是字符串",
                    }, ensure_ascii=False))
                    continue
                if (
                    not nickname.strip()
                    or len(nickname) > settings.rate_limit.ws_max_nickname_chars
                    or not message.strip()
                    or len(message) > settings.rate_limit.ws_max_message_chars
                    or not danmaku_id.strip()
                    or len(danmaku_id) > settings.rate_limit.ws_max_danmaku_id_chars
                    or not isinstance(sender_level, int)
                    or isinstance(sender_level, bool)
                    or not 1 <= sender_level <= 10
                ):
                    await websocket.send_text(json.dumps({
                        "type": "error", "code": "invalid_fields",
                        "message": "弹幕字段为空、过长或 sender_level 超出 1-10",
                    }, ensure_ascii=False))
                    continue

                connection = connection_manager.get_connection(websocket)
                message_limit = websocket_rate_guard.check_message(
                    connection.id if connection else "unknown",
                    client_ip,
                    connection.identity.account_id if (
                        connection and connection.identity.is_authenticated
                    ) else "",
                    settings.rate_limit,
                )
                if not message_limit.allowed:
                    await _send_ws_limit_event(websocket, message_limit)
                    continue

                # 昵称只更新展示属性；客户端消息中的账号字段不参与身份解析。
                if connection and connection.identity.is_authenticated:
                    message_data["nickname"] = connection.identity.current_nickname
                    viewer_identity = connection.identity
                else:
                    viewer_identity = connection_manager.update_connection_nickname(
                        websocket, message_data["nickname"]
                    )

                dedup_subject = (
                    viewer_identity.subject_id if viewer_identity
                    else f"connection:{connection.id if connection else 'unknown'}"
                )
                if not danmaku_deduplicator.claim(
                    f"{dedup_subject}:{danmaku_id}",
                    settings.rate_limit.ws_dedup_ttl_seconds,
                ):
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "code": "duplicate_danmaku",
                        "message": "该 danmakuID 已处理，请勿重复提交",
                    }, ensure_ascii=False))
                    continue
                
                # 更新弹幕频率
                current_rate = _update_danmaku_rate()
                
                # 添加到弹幕池
                await danmaku_pool.add_danmaku(
                    danmaku_id=message_data["danmakuID"],
                    nickname=message_data["nickname"],
                    message=message_data["message"],
                    sender_level=message_data.get("sender_level", 1),
                    viewer_identity=viewer_identity,
                    client_ip=client_ip,
                )
                
                # 记录弹幕到数据库
                db_manager.record_danmaku(
                    danmaku_id=message_data["danmakuID"],
                    nickname=message_data["nickname"],
                    message=message_data["message"],
                    client_ip=client_ip,
                    sender_level=message_data.get("sender_level", 1)
                )
                
                # 添加到弹幕记忆
                memory_item = await danmaku_memory_manager.add_danmaku(
                    danmaku_id=message_data["danmakuID"],
                    user_id=(viewer_identity.subject_id if viewer_identity
                             else message_data["nickname"]),
                    nickname=message_data["nickname"],
                    content=message_data["message"]
                )
                viewer_relationship = await audience_relationship_manager.observe_danmaku(
                    nickname=message_data["nickname"],
                    message=message_data["message"],
                    sentiment=memory_item.sentiment,
                    topics=memory_item.topic_keywords,
                    identity=viewer_identity,
                )
                await stream_metadata_pusher.consider_activity_suggestion(
                    message=message_data["message"],
                    identity=viewer_identity,
                    relationship=viewer_relationship,
                    sentiment=memory_item.sentiment,
                    danmaku_rate=current_rate,
                )
                await persona_event_pipeline.publish(
                    DanmakuReceivedEvent(
                        nickname=message_data["nickname"],
                        message=message_data["message"],
                        sentiment=memory_item.sentiment,
                        topics=tuple(memory_item.topic_keywords),
                        danmaku_rate=current_rate,
                        source="websocket",
                    )
                )
                
                plugin_results = await plugin_manager.emit_event("danmaku_received", message_data)
                if plugin_results:
                    message_data = plugin_results[-1]
                
                danmaku_message = DanmakuResponse(
                    nickname=message_data["nickname"],
                    message=message_data["message"],
                    type=message_data.get("type", "normal"),
                    timestamp=datetime.now().isoformat(),
                    danmakuID=message_data["danmakuID"]
                )
                
                logger.info(f"弹幕消息: {message_data['message']},发送者: {message_data['nickname']}, 频率: {current_rate}条/分钟")
                
                broadcast_data = danmaku_message.model_dump()
                plugin_results = await plugin_manager.emit_event("danmaku_broadcast", broadcast_data)
                if plugin_results:
                    broadcast_data = plugin_results[-1]
                    danmaku_message = DanmakuResponse(**broadcast_data)
                
                await connection_manager.broadcast_message(danmaku_message)
                
                confirmation = {
                    "type": "confirmation",
                    "message": "弹幕发送成功",
                    "timestamp": danmaku_message.timestamp,
                    "danmaku_rate": current_rate
                }
                await websocket.send_text(json.dumps(confirmation, ensure_ascii=False))
                
                # 弹幕选择逻辑
                # 先获取可用的弹幕
                available_danmaku = await danmaku_pool.get_available_danmaku_for_selection()
                logger.debug(f"可用弹幕数量: {len(available_danmaku)}")
                
                if available_danmaku:
                    # SC 由独立消费者直达回复链；有待处理 SC 时不再启动新的普通弹幕 AI 任务。
                    if await asyncio.to_thread(sc_service.has_pending):
                        logger.debug("检测到待处理 SC，本轮普通弹幕选择让行")
                        continue
                    # 检查是否应该选择弹幕
                    should_select = await danmaku_selector.should_select_danmaku(current_rate, has_available_danmaku=True)
                    logger.debug(f"是否选择弹幕: {should_select}")
                    
                    if should_select:
                        logger.debug("开始选择弹幕...")
                        selection_result = await danmaku_selector.select_danmaku(available_danmaku)
                        
                        if selection_result and selection_result.selected_danmaku:
                            selected = selection_result.selected_danmaku
                            
                            # 触发选择事件
                            await event_bus.emit("danmaku_selected", {
                                "danmaku_id": selected.id,
                                "nickname": selected.nickname,
                                "message": selected.message,
                                "confidence": selection_result.confidence_score,
                                "reason": selection_result.selection_reason
                            })
                            
                            # 发送选择通知给客户端
                            selection_notification = {
                                "type": "danmaku_selected",
                                "data": {
                                    "danmaku_id": selected.id,
                                    "nickname": selected.nickname,
                                    "message": selected.message[:50] + "..." if len(selected.message) > 50 else selected.message,
                                    "confidence": round(selection_result.confidence_score, 3),
                                    "processing_time_ms": round(selection_result.processing_time_ms, 2)
                                }
                            }
                            await websocket.send_text(json.dumps(selection_notification, ensure_ascii=False))
                            
                            logger.info(f"弹幕选择: [{selected.nickname}] {selected.message[:30]}... (置信度: {selection_result.confidence_score:.3f})")
                            
                            # 生成AI回复
                            logger.info(f"正在为弹幕生成回复: [{selected.nickname}] {selected.message}")
                            reply_lease = await ai_reply_work_gate.acquire(
                                limit=settings.rate_limit.ai_reply_concurrency,
                                max_waiters=settings.rate_limit.ai_reply_queue_size,
                                wait_timeout=settings.rate_limit.ai_reply_queue_wait_seconds,
                            )
                            if reply_lease is None:
                                await danmaku_pool.release_selection(selected.id)
                                await _send_ws_limit_event(
                                    websocket,
                                    ProtectionDecision(
                                        False, "ai_reply", 1, "drop", "queue_full"
                                    ),
                                    message="主播回复队列繁忙，本条弹幕暂未选中",
                                )
                                continue
                            reply_quota = _check_ai_reply_quota(selected)
                            if not reply_quota.allowed:
                                await reply_lease.release()
                                await danmaku_pool.release_selection(selected.id)
                                await _send_ws_limit_event(
                                    websocket,
                                    reply_quota,
                                    message="主播回复额度繁忙，请稍后继续互动",
                                )
                                continue
                            try:
                                try:
                                    reply_result = await persona_engine.generate_reply({
                                        "nickname": selected.nickname,
                                        "message": selected.message,
                                        "danmakuID": selected.id,
                                        "_viewer_identity": selected.viewer_identity,
                                    })
                                finally:
                                    await reply_lease.release()
                                
                                if reply_result and 'reply_data' in reply_result:
                                    reply_data = reply_result['reply_data']
                                    analysis = reply_result['analysis']
                                    
                                    # 标记为已回复，并存储回复内容
                                    reply_content = json.dumps(reply_data, ensure_ascii=False)
                                    await danmaku_pool.mark_as_replied(selected.id, reply_content)
                                    
                                    # 记录回复日志
                                    logger.info(f"AI回复生成成功: {reply_content}")
                                    
                                    # 记录回复到数据库
                                    try:
                                        # 获取弹幕记录ID
                                        danmaku_record = db_manager.get_danmaku_by_id(selected.id)
                                        danmaku_record_id = danmaku_record['id'] if danmaku_record else None
                                        
                                        # 记录回复
                                        db_manager.record_reply(
                                            danmaku_id=selected.id,
                                            danmaku_nickname=selected.nickname,
                                            danmaku_message=selected.message,
                                            ai_reply=reply_data,
                                            mood_before=reply_result['mood_before'],
                                            stress_before=reply_result['stress_before'],
                                            darkness_before=reply_result['darkness_before'],
                                            mood_impact=analysis.mood_impact if analysis else 0,
                                            stress_impact=analysis.stress_impact if analysis else 0,
                                            darkness_impact=analysis.darkness_impact if analysis else 0,
                                            mood_after=reply_result['mood_after'],
                                            stress_after=reply_result['stress_after'],
                                            darkness_after=reply_result['darkness_after'],
                                            emotional_tone=analysis.emotional_tone if analysis else None,
                                            content_intensity=analysis.content_intensity if analysis else None,
                                            context_relevance=analysis.context_relevance if analysis else None,
                                            analysis_reasoning=analysis.reasoning if analysis else None,
                                            key_factors=analysis.key_factors if analysis else None,
                                            danmaku_record_id=danmaku_record_id
                                        )
                                        logger.debug(f"回复已记录到数据库")
                                    except Exception as db_error:
                                        logger.error(f"记录回复到数据库时出错: {db_error}")
                                    
                                    # 通过WebSocket推送回复给所有客户端
                                    reply_broadcast = {
                                        "type": "ai_reply",
                                        "data": {
                                            "danmaku_id": selected.id,
                                            "nickname": selected.nickname,
                                            "original_message": selected.message,
                                            "reply": reply_data,
                                            "timestamp": datetime.now().isoformat()
                                        }
                                    }
                                    
                                    # 广播给所有连接的客户端
                                    await connection_manager.broadcast_json(reply_broadcast)
                                    logger.info("AI回复已广播给所有客户端")
                                else:
                                    logger.warning("AI回复生成失败，结果为空")
                                    # 即使回复生成失败，也标记为已回复，并存储空回复内容
                                    await danmaku_pool.mark_as_replied(selected.id, "")
                                    
                            except Exception as e:
                                logger.error(f"生成AI回复时出错: {e}")
                
            except json.JSONDecodeError:
                error_response = {
                    "type": "error",
                    "message": "无效的 JSON 格式"
                }
                await websocket.send_text(json.dumps(error_response, ensure_ascii=False))
            
            except Exception as e:
                request_id = str(uuid.uuid4())
                logger.exception("处理 WebSocket 消息失败 request_id=%s", request_id)
                error_response = {
                    "type": "error",
                    "code": "internal_error",
                    "message": "服务器处理失败，请稍后重试",
                    "request_id": request_id,
                }
                await websocket.send_text(json.dumps(error_response, ensure_ascii=False))
    
    except WebSocketDisconnect:
        await _cleanup_websocket_connection(websocket)
    
    except Exception as e:
        logger.error(f"WebSocket 连接异常: {e}")
        await _cleanup_websocket_connection(websocket)
